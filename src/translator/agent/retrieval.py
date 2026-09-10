"""Pure local BM25 retrieval with Japanese/CJK ngrams and conservative routing.

Scores are corpus-relative ranking values, not probabilities. No query is sent,
stored, evaluated as code, or included in the result. Search returns reviewed
knowledge and exact catalog definitions; execution remains a separate operation.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
import re
import unicodedata

from .knowledge import knowledge_index


_WORDS = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*|[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff々ー]+")
_CJK = re.compile(r"[\u30a1-\u30fa\u3400-\u4dbf\u4e00-\u9fff]")
_STOP = set("a an the i me my we our you your it its is are be been being am to of for in on at by from with as and or but that this these those can could would should will want need please how do does did have has get use using without not no only then than which what something some into about already anything".split())
_INFLECTIONS = {"started": "start", "starting": "start", "stopped": "stop", "stopping": "stop",
                "running": "run", "creating": "create", "created": "create", "creation": "create",
                "reading": "read", "showing": "show", "sharing": "share", "shared": "share",
                "saving": "save", "saved": "save", "changing": "change", "changed": "change",
                "configuration": "configure", "configured": "configure", "configuring": "configure",
                "settings": "setting", "preferences": "preference", "diagnostic": "diagnostics",
                "invitation": "invite", "invitations": "invite", "inviting": "invite",
                "launching": "launch", "launched": "launch", "publishing": "publish",
                "published": "publish", "enumerating": "enumerate", "listing": "list",
                "retrieval": "retrieve", "searching": "search", "transcriptions": "transcript",
                "transcription": "transcript", "translated": "translation", "translating": "translation"}

_ACTIONS = {
    "read": r"\b(read|show|inspect|check|running|alive|status)\b|確認|読む|読み|見せ|取得|表示|稼働",
    "write": r"\b(set|apply|update|change|edit|configure|save|store|register)\b|設定する|変更|更新|書き換|書換|保存(?!され|済み)|登録(?!され|済み)",
    "start": r"\b(start|begin|launch|boot|open|run|prepare|ready)\b|起動|開始|始め|立ち上|準備",
    "stop": r"\b(stop|end|finish|quit|exit|terminate|shutdown|close|disable|unpublish)\b|\btake\b.*\boffline\b|終了|停止|止め|閉じ|取りやめ|落とし|落とす|非公開",
    "create": r"\b(create|new|make)\b|作成|新規|作り|作る",
    "share": r"\b(share|stream|broadcast|capture|send)\b|共有|配信|送る|送信",
    "test": r"\b(test|verify|validate|connectivity)\b|テスト|検査|検証|疎通|確かめ",
    "list": r"\b(list|enumerate|available|catalog|flag|argument)\b|一覧|列挙|引数|仕様|ヘルプ",
    "find": r"\b(find|search|discover|retrieve)\b|検索|探す|探し",
    "diagnose": r"\b(diagnose|diagnostics|troubleshoot|debug|failure|error|broken|problem)\b|不具合|診断|エラー|故障|原因|動かない",
}
_CARD_ACTIONS = {
    "search": {"find"}, "catalog": {"list", "read"}, "status": {"read"},
    "runtime-start": {"start"}, "runtime-stop": {"stop"},
    "settings-get": {"read"}, "settings-set": {"write"},
    "session-create": {"create"}, "session-start": {"start"}, "session-stop": {"stop"},
    "session-snapshot": {"read"}, "invite-create": {"create"},
    "diagnostics": {"diagnose"}, "devices": {"list", "find", "read"},
    "secret-set": {"write"}, "provider-test": {"test", "read"},
    "tunnel-start": {"start"}, "tunnel-stop": {"stop"}, "audio-share": {"share"},
}


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _tokens(text: str) -> list[str]:
    tokens = []
    for word in _WORDS.findall(_normalize(text)):
        if word[0].isascii():
            parts = [word, *word.split("-")] if "-" in word else [word]
            for part in parts:
                if part in _STOP or len(part) < 2:
                    continue
                stem = _INFLECTIONS.get(part, part)
                if stem == part and part.endswith("s") and len(part) > 4:
                    stem = part[:-1]
                tokens.append("w:" + stem)
        else:
            # Keep compounds across unsegmented Japanese; hiragana-only grams
            # are mostly grammatical fillers and produce misleading matches.
            for size in (2, 3):
                tokens.extend("j:" + word[i:i + size] for i in range(len(word) - size + 1)
                              if _CJK.search(word[i:i + size]))
            tokens.extend("j:" + char for char in word if char in "音声鍵")
    return tokens


def _document(card: dict) -> list[str]:
    # Positive goals carry most relevance. Input sources, prerequisites and
    # exact argument definitions are grounding, not search evidence.
    return (_tokens(card["id"]) * 2 + _tokens(card["search_terms"]) * 6 +
            _tokens(" ".join(card["when_to_use"])) * 2 +
            _tokens(" ".join(card["summary"].values())))


def _positive_text(query: str) -> str:
    # Remove explicit local negative clauses, preserving the requested positive
    # operation. This is conservative grammar handling, not instruction following.
    original = query
    query = re.sub(r"\b(?:keep|leave)\b[^,;.]*?(?:\bbut\b|[,;])", " ", query)
    query = re.sub(r"\b(?:do not|don't|without)\b[^,;.]*", " ", query)
    query = re.sub(r"\bnot\s+(?:a |the )?[a-z0-9_-]+", " ", query)
    query = re.sub(r"[^、。！？!?;,]*?(?:せずに|せず|しないで|使わず|なしで)[、,]?", " ", query)
    query = re.sub(r"マイク(?:ではありません|ではない|ではなく|以外|を使わない|不要)", " ", query)
    query = re.sub(r"[a-z0-9_-]+(?:ではなく|じゃなく)", " ", query)
    if re.search(r"\b(them|its|it)\b", query):
        # Resolve only the topic of a simple local anaphor, never a negated
        # action. This preserves "don't edit my options, show them" safely.
        context = re.findall(r"\b(?:do not|don't|without)\b([^,;.]*)", original)
        retained = set(_tokens(" ".join(context)))
        nouns = {term for card in ("setting preference option", "session conversation caption transcript",
                                  "runtime application app", "device", "tunnel ngrok") for term in _tokens(card)}
        query += " " + " ".join(term[2:] for term in sorted(retained & nouns))
    return query


def _rank(query: str, cards: list[dict]) -> list[tuple[float, int, dict]]:
    documents = [Counter(_document(card)) for card in cards]
    lengths = [sum(document.values()) for document in documents]
    average = sum(lengths) / len(lengths)
    frequency = Counter(term for document in documents for term in document)
    query_terms = set(_tokens(query))
    actions = {name for name, pattern in _ACTIONS.items() if re.search(pattern, query)}
    ranked = []
    for card, document, length in zip(cards, documents, lengths):
        score, matches = 0.0, 0
        for term in sorted(query_terms):
            count = document[term]
            if not count:
                continue
            matches += 1
            inverse = math.log(1 + (len(cards) - frequency[term] + 0.5) / (frequency[term] + 0.5))
            weight = 0.22 if term in {"j:音", "j:声", "j:鍵"} else 1.0
            score += weight * inverse * count * 2.2 / (count + 1.2 * (0.35 + 0.65 * length / average))
        topic_hits = query_terms & set(_tokens(card["search_topics"]))
        if not topic_hits and not _generic(query):
            score *= 0.15
        if actions and card["kind"] == "command":
            compatible = actions & _CARD_ACTIONS[card["id"]]
            score *= 1.5 if compatible else 0.45
        ranked.append((score, matches, card))
    return sorted(ranked, key=lambda item: (-item[0], item[2]["id"]))


def _exact_command(query: str, cards: list[dict]) -> str | None:
    cleaned = query.strip(" \t\r\n`'\".。!?！？")
    names = {card["id"] for card in cards if card["kind"] == "command"}
    if cleaned in names:
        return cleaned
    prefix = "translator agent "
    if cleaned.startswith(prefix) and cleaned[len(prefix):] in names:
        return cleaned[len(prefix):]
    return None


def _unsupported(query: str) -> str | None:
    credential = re.search(r"\b(?:api\s+)?key\b|\bsecret\b|\bcredential\b|\btoken\b|キー|認証情報|トークン", query)
    credential_action = re.search(r"\b(reveal|show|read|print|extract|export|delete|remove|erase|clear)\b|表示|開示|読む|読み|削除|消去|消す|見せ", query)
    metadata = re.search(r"metadata|configured|configuration|setting|status|設定|有無|登録状況", query)
    deletion = re.search(r"\b(delete|remove|erase|clear)\b|削除|消去|消す", query)
    if credential and credential_action and (deletion or not metadata):
        return "credential-read-delete"
    if (re.search(r"\btts\b|text[ -]to[ -]speech|speech synthes|voice clon|dubbing", query) or
            any(word in query for word in ("読み上げ", "音声合成", "合成音声", "翻訳音声", "吹き替え", "音声通訳")) or
            (re.search(r"translat|翻訳", query) and re.search(r"aloud|speak|spoken|synthesi|generate.*voice|音声で|声で|声に", query))):
        return "translated-tts"
    mic = re.search(r"\bmic\b|microphone|マイク", query)
    negated_mic = re.search(r"without (?:a |the )?(?:mic|microphone)|not (?:a |the )?(?:mic|microphone)|マイク(?:ではなく|以外|を使わず|なし|不要)", query)
    invitation = re.search(r"\binvit|\bjoin|招待|参加リンク", query)
    mic_action = re.search(r"permission|grant|allow|enable|activate|start|record|capture|control|許可|権限|開始|始め|録音|有効|操作|起動", query)
    if mic and not negated_mic and mic_action and not invitation:
        return "participant-browser-microphone"
    return None


def _generic(query: str) -> bool:
    # An action with no concrete object cannot distinguish runtime, session,
    # public tunnel, or captured audio. Evaluate this before result truncation.
    action = re.search(r"\b(start|begin|stop|end|open|close|run)\b|開始|停止|始め|止め|終了", query)
    if not action:
        return False
    objects = re.search(r"runtime|app|application|server|translator|session|conversation|demo|tunnel|ngrok|browser|caption|setting|pc|computer|desktop|playback|output|アプリ|本体|全体|会話|セッション|デモ|トンネル|公開|ブラウザ|字幕|設定|パソコン|再生|出力", query)
    return objects is None


def _candidate(score: float, card: dict) -> dict:
    return {**{key: deepcopy(value) for key, value in card.items() if not key.startswith("search_")},
            "score": round(max(0, score), 6)}


def search(query: str, commands: list[dict], limit: int = 3) -> dict:
    """Retrieve bounded grounded candidates without executing any operation.

    Invalid direct-library inputs raise ValueError without copying their value
    into the exception; the CLI validates these bounds before calling us.
    """
    if (not isinstance(query, str) or not query.strip() or len(query) > 1024 or
            any(ord(char) < 32 and char not in "\t\r\n" for char in query)):
        raise ValueError("Query must contain 1–1024 characters without control characters.")
    if type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError("Candidate limit must be an integer from 1 to 5.")
    index = knowledge_index(commands)
    digest = hashlib.sha256(json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    normalized = _normalize(query)
    positive = _positive_text(normalized)
    ranked = _rank(positive, index["cards"])
    exact = _exact_command(normalized, index["cards"])
    limitation = None if exact else _unsupported(positive)
    preferred = exact or limitation
    if preferred:
        score = ranked[0][0] + 10.0
        ranked = [(score, matches, card) for _, matches, card in ranked if card["id"] == preferred] + [item for item in ranked if item[2]["id"] != preferred]
    query_terms = set(_tokens(positive))
    generic = _generic(positive)
    useful = [item for item in ranked if item[0] >= 1.0 and item[1] > 0 and
              (generic or query_terms & set(_tokens(item[2]["search_topics"])))]
    if preferred:
        useful = [ranked[0]] + [item for item in useful if item[2]["id"] != preferred]
    if limitation:
        status = "unsupported"
        next_action = {
            "participant-browser-microphone": "B must open the intended invite and explicitly grant/start the microphone in the browser. The host CLI cannot perform this action. / マイク許可と開始は参加者本人のブラウザで行う。",
            "translated-tts": "Translated speech synthesis is not implemented; no TTS command can be executed. If text meets the goal, inspect translated captions with session-snapshot. / 翻訳音声の生成は未実装。字幕で目的を満たせるか確認する。",
            "credential-read-delete": "The CLI cannot reveal or delete stored key values. settings-get can inspect configuration metadata only; do not substitute provider-test or a secret-set mutation for this goal. / キー本文の表示・削除には非対応。設定有無のみなら settings-get で確認できる。",
        }[limitation]
    elif not useful:
        status = "no_match"
        next_action = "No supported operation matched. Rephrase one app operation in Japanese or English, or inspect catalog. Do not invent a command. / 対応する操作が見つからないため、目的を言い換える。"
    elif not exact and (generic or (len(useful) > 1 and useful[0][0] < useful[1][0] * 1.10)):
        status = "ambiguous"
        next_action = "Clarify the intended object and action before execution: runtime, conversation, PC output, participant microphone, or public tunnel. Review candidate distinctions; no operation is selected. / 対象と操作を確認してから選ぶ。"
    elif useful[0][2]["kind"] == "limitation":
        status = "unsupported"
        next_action = "This goal matches a documented limitation, not an executable command. Read its prerequisites and guidance before choosing an alternative. / 制限事項を確認し、未実装の操作を実行しない。"
    else:
        status = "matched"
        next_action = "Review the leading candidate's exact definition, effects, prerequisites and input_sources; obtain required IDs and values from observed results before executing a separate command. Search itself has executed nothing. / 仕様と前提条件を確認し、実際の取得値を使って別途実行する。"
    return {"retrieval_version": 1, "index_digest": digest, "strategy": "local-bm25-cjk",
            "status": status, "candidates": [_candidate(score, card) for score, _, card in useful[:limit]],
            "next_action": next_action}
