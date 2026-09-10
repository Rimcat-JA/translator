"""Reviewed bilingual command knowledge, joined to the executable catalog at export time.

The cards describe goals rather than executable plans. Only ``definition`` comes
from the caller's authoritative command catalog. Prerequisites and input sources
are grounding context, deliberately excluded from lexical relevance scoring.
Anchors are stable card identifiers, e.g. ``command:runtime-start``.
"""
from __future__ import annotations

from copy import deepcopy


def _card(ja, terms, uses, prerequisites=(), inputs=None):
    return {"ja": ja, "terms": terms, "when_to_use": list(uses),
            "prerequisites": list(prerequisites), "input_sources": inputs or {}}


_RUNNING = "Use the same --data-dir as the running runtime; status discovers it. / status で同じプロファイルの起動状態を確認する。"
_SESSION = "Use an existing session_id returned by session-create or data.session.session_id in status; never invent an ID. / 作成結果か status の会話IDを使う。"
_REQUEST = "Optional caller-generated stable operation ID; omit to generate one. It does not authorize automatic mutation retries."

# A topic must be present for a lexical hit to be actionable. Generic Japanese
# phrases such as 調べたい or やり方 also occur in unrelated weather/recipe goals.
_TOPICS = {
    "search": "command operation capability コマンド 操作 機能 検索 方法",
    "catalog": "catalog command argument flag specification help カタログ コマンド 一覧 引数 仕様 ヘルプ",
    "status": "status running alive active runtime application app session identifier 稼働 起動 状態 動いて 会話 セッション",
    "runtime-start": "runtime application app server background detached headless browser launch boot 本体 アプリ 起動 立ち上 バックグラウンド 常駐 ブラウザ",
    "runtime-stop": "runtime application app server process shutdown translator 本体 アプリ プロセス 全体 常駐 シャットダウン",
    "settings-get": "setting preference option configuration revision language theme 設定 リビジョン 言語 テーマ",
    "settings-set": "setting preference option configuration revision language translation theme 設定 リビジョン 言語 翻訳 テーマ",
    "session-create": "session conversation demo live simulation 会話 セッション デモ 新規",
    "session-start": "session conversation demo live participant 会話 セッション デモ 参加者",
    "session-stop": "session conversation caption transcript 会話 セッション 通話 字幕",
    "session-snapshot": "caption subtitle transcript translation snapshot history conversation recognized utterance 字幕 翻訳文 文字起こし 履歴 会話内容 認識 発言",
    "invite-create": "invite invitation join link url participant guest 招待 参加 リンク 相手 ゲスト",
    "diagnostics": "diagnostics diagnostic failure error broken problem troubleshoot 不具合 診断 調査 エラー 故障 問題 原因",
    "devices": "device speaker headphone soundcard output デバイス 出力 スピーカー ヘッドホン 音声機器",
    "secret-set": "credential secret key token api gladia deepl ngrok APIキー キー 認証情報 トークン",
    "provider-test": "provider api key credential connection connectivity gladia deepl ngrok プロバイダー 接続 疎通 APIキー キー",
    "tunnel-start": "tunnel ngrok public internet remote external domain hub トンネル 公開 外部 インターネット 遠隔 リモート",
    "tunnel-stop": "tunnel ngrok public internet remote external hub トンネル 公開 外部 インターネット",
    "audio-share": "pc computer desktop system playback loopback output audio パソコン 再生音 ループバック 出力 システム 音声共有",
}


COMMAND_CARDS = {
    "search": _card(
        "やりたいことを日本語・英語で検索し、候補のコマンド仕様を少数取得する。検索だけでは実行しない。",
        "find discover search retrieve command goal natural language operation capability 検索 探す やり方 目的 操作 コマンド 自然言語 何を使う 方法",
        ["Find which command fits a goal without reading the whole catalog.", "やりたい操作に合うコマンドを自然言語から探す。"],
        ["Offline; no runtime, credentials or data directory is required. / オフラインで検索できる。"],
        {"--query": "A single Japanese or English goal, 1–1024 characters; never include credentials.",
         "--limit": "Choose 1–5 candidates (default 3); ambiguity remains meaningful even with limit 1."}),
    "catalog": _card(
        "既知のコマンド名から正式な引数・副作用・例を取得する。省略すると全コマンド一覧を返す。",
        "catalog list commands all commands specification schema arguments help exact name reference flags カタログ 一覧 全コマンド 引数 仕様 ヘルプ オプション",
        ["Inspect the exact flags of a command whose name is already known.", "全コマンドの一覧、または既知の名前の正式仕様を読む。"],
        ["Offline; no runtime or credentials required. / アプリの起動は不要。"],
        {"--command": "Use an exact name returned by search candidates[].definition.name; omit only when the full catalog is needed."}),
    "status": _card(
        "アプリが起動中か確認し、現在の会話ID・状態・設定と字幕件数を取得する。字幕本文は返さない。",
        "status running alive active current runtime inspect discover session id identifier health 稼働 起動中 動いている 状態 現在 会話ID セッションID 生存確認",
        ["Check whether the app is running and discover the current session ID.", "アプリの稼働状態や現在の会話IDを確認する。"],
        ["Read-only; a missing runtime succeeds with data.running=false. / 未起動も正常な確認結果。"]),
    "runtime-start": _card(
        "アプリ本体をバックグラウンドで起動する。ブラウザを開かず、会話や音声は自動開始しない。",
        "start launch boot open run app application runtime server background detached headless browserless アプリ 本体 起動 立ち上げ バックグラウンド 常駐 ブラウザを開かず ヘッドレス",
        ["Launch the app in the background without opening a browser.", "ブラウザを開かずにアプリ本体をバックグラウンドで起動する。"],
        ["Choose one --data-dir profile. This does not create a session or start PC/microphone capture.",
         "Source startup may build missing frontend assets; it can require network access. / ソース実行は不足する画面資材をビルドする場合がある。"],
        {"--timeout": "Readiness wait in seconds, 1–300 (default 60); inspect status after an uncertain result."}),
    "runtime-stop": _card(
        "アプリ本体を安全に終了し、バックグラウンドのプロセスが停止するまで待つ。",
        "stop quit exit terminate shutdown close application app runtime server background process 終了 シャットダウン 停止 閉じる アプリ 本体 プロセス 常駐",
        ["Shut down the complete background runtime.", "会話だけでなくアプリ本体を終了する。"], [_RUNNING],
        {"--timeout": "Shutdown wait in seconds, 1–300 (default 30); inspect status if the outcome is uncertain."}),
    "settings-get": _card(
        "現在の設定・言語・テーマと設定リビジョンを読む。APIキーは設定有無だけを返す。",
        "read get show inspect current saved settings preferences options configuration revision language theme 設定 読む 取得 表示 確認 リビジョン 現在 言語 テーマ",
        ["Read saved preferences or obtain expected_revision before an update.", "設定値を確認する、または設定変更前に revision を取得する。"],
        [_RUNNING, "Provider metadata is data.secrets[provider]; configured means a key exists, not that it has passed provider-test. / キー設定済みと接続検証済みは異なる。"]),
    "settings-set": _card(
        "リビジョンを確認して言語・翻訳有効化・テーマなどの設定をJSONで変更する。",
        "set update change edit configure settings preferences language translation enable disable theme fontsize 設定 変更 更新 言語 翻訳 有効 無効 テーマ 字幕サイズ",
        ["Change source/target language, theme, caption size or translation preferences.", "設定や翻訳言語をJSONで変更する。"],
        [_RUNNING, "Read settings-get first; use its current revision and only documented preference fields. / 現在の revision を先に読む。"],
        {"--input": "UTF-8 JSON file or stdin (-), at most 32768 bytes: {expected_revision: <settings-get data.revision>, settings: {...changed preferences...}}. Never put API keys here."}),
    "session-create": _card(
        "demo または live を明示して新しい会話を作成し、会話IDを取得する。作成直後は待機状態。",
        "create new conversation session demo live offline simulation practice sample chinese japanese 作成 新規 会話 セッション デモ 練習 オフライン サンプル 中国語 日本語",
        ["Create an offline demonstration or a new live conversation with chosen languages.", "オフラインのデモ会話や、新しい実会話を作成する。"],
        [_RUNNING, "Requires no current session, or current status ended; end an existing conversation before creating another. / 既存の会話がある場合は終了してから新しく作成する。",
         "Choose demo or live explicitly. Creation is idle; use session-start separately. Demo needs no provider keys. / モードを明示し、開始は別操作。"],
        {"--mode": "User intent: demo for synthetic offline data, live for a real participant; required with no inferred default.",
         "--source-language": "User-selected source language code from definition.arguments choices; default zh.",
         "--target-language": "User-selected target language code from definition.arguments choices; default ja.",
         "--translation-enabled": "Explicit value true or false when changing the default true; this is not a presence-only flag."}),
    "session-start": _card(
        "作成済みの会話を開始する。demo は合成音声で字幕を生成し、live は参加者の接続を待つ。",
        "start begin prepare ready activate existing conversation session demo live prepared translation transcription 会話 セッション 開始 デモ 翻訳 準備済み 作成済み",
        ["Start a conversation after creating it; run the synthetic demo.", "作成済みの会話やデモを開始する。"],
        [_RUNNING, _SESSION, "Starts an idle session; waiting_for_peer/running/degraded returns unchanged. An ended session cannot restart. / idle から開始し、終了済みの会話は再開できない。",
         "Live startup waits without validating provider keys and does not capture the PC. B's later STT needs Gladia; enabled cross-language translation needs DeepL. / live の開始だけでキー検証や音声共有は始まらない。"]),
    "session-stop": _card(
        "現在の会話を終了し、音声・接続を解放してメモリ内の字幕を消去する。アプリ本体は残る。",
        "stop end finish close conversation session discard clear captions transcript 会話 セッション 終了 停止 終える 字幕 消去",
        ["End the conversation and release its providers while leaving the app running.", "アプリを残して会話を終了し、保持している字幕を消す。"],
        [_RUNNING, _SESSION, "This discards in-memory captions and revokes this session's invites. / 字幕と招待は終了後に再利用できない。"]),
    "session-snapshot": _card(
        "会話の状態と直近の字幕・翻訳文を読む。既定は最新10件、最大100件で本文量を制限する。",
        "read get show latest last recent recognized utterance captions subtitles transcript transcription translated text conversation snapshot history 字幕 最新 最後 直近 読む 見せ 認識 発言 翻訳文 文字起こし 履歴 会話内容",
        ["Read recent original and translated caption text or inspect a conversation snapshot.", "直近の字幕や文字起こし・翻訳文を取得する。"], [_RUNNING, _SESSION],
        {"--limit": "Number of latest caption entries, 1–100 (default 10). caption_count is total retained; captions_truncated reports omitted older entries."}),
    "invite-create": _card(
        "参加者B向けの一回限り・10分有効な招待URLを作る。URLは秘密情報として本人に渡す。",
        "create invitation invite join link url participant guest friend speaker connect 招待 参加 リンク URL 相手 友達 ゲスト 招く",
        ["Create a join link for the intended participant.", "会話に参加する相手へ渡す招待リンクを作成する。"],
        [_RUNNING, _SESSION, "The current session must not be stopping or ended. / 停止処理中や終了済みの会話には招待を発行できない。",
         "For a remote participant, start the public Hub tunnel first. The one-use URL is sensitive and expires after ten minutes.",
         "B opens /join and grants browser microphone permission; an invite does not start B's microphone. / マイク許可は参加者本人のブラウザで行う。"]),
    "diagnostics": _card(
        "不具合調査用にアプリの診断情報・構成要素の状態を取得する。APIキーや字幕本文は含まない。",
        "diagnose diagnostics troubleshoot debug failure error broken problem component redacted report 不具合 診断 調査 エラー 故障 問題 原因 動かない",
        ["Inspect a failure or gather a redacted runtime diagnostic report.", "不具合の原因を調べるために診断情報を読む。"], [_RUNNING]),
    "devices": _card(
        "PC音声の出力デバイス一覧とデバイスIDを取得する。列挙だけでは録音を開始しない。",
        "list enumerate find available audio output device speaker headphone soundcard id デバイス 一覧 出力 スピーカー ヘッドホン 音声機器 オーディオ 選択",
        ["Find an output device ID before choosing PC audio capture.", "共有したいスピーカーや音声出力デバイスのIDを調べる。"],
        [_RUNNING, "Native loopback capture is supported on Windows; device enumeration does not capture audio. / 一覧取得だけでは音を取得しない。"]),
    "secret-set": _card(
        "プロバイダーのAPIキーを標準入力かファイルから登録する。既定はメモリのみ、永続保存は明示する。",
        "set configure register save store enter credential secret token api key gladia deepl ngrok stdin memory persist APIキー 鍵 キー 登録 保存 設定 認証情報 トークン 標準入力 メモリ",
        ["Provide an API key from a protected input stream or file.", "Gladia・DeepL・ngrok のキーを登録し、必要ならOS資格情報ストアへ保存する。"],
        [_RUNNING, "Never include the key in argv, search queries, logs or source. Memory-only storage is the default. / キーを引数や検索文に書かない。"],
        {"--provider": "Choose gladia, deepl or ngrok according to the credential being configured.",
         "--input": "Protected UTF-8 plaintext key file or stdin (-), non-empty and at most 4096 bytes; not JSON.",
         "--persist": "Optional presence-only flag, without a value; use only when persistent OS credential storage is intended."}),
    "provider-test": _card(
        "登録済みの外部API接続を明示的に検査する。Gladia・DeepLの検査は通信や利用量を発生させ得る。",
        "test verify check validate provider api key credentials connectivity connection gladia deepl ngrok 検査 テスト 検証 接続 疎通 確かめ 確認 プロバイダー APIキー 有効",
        ["Test whether the configured speech or translation provider responds.", "登録済みのGladiaやDeepLのキーで接続できるか確認する。"],
        [_RUNNING, "Configure the selected provider first with secret-set. Testing can incur usage; ngrok publication validation occurs in tunnel-start. / ngrok の公開検証は tunnel-start。"],
        {"--provider": "Choose the configured provider to inspect: gladia, deepl or ngrok. ngrok here checks configuration only."}),
    "tunnel-start": _card(
        "ngrok で参加者用Hubをインターネットへ公開し、遠隔の相手が接続できるようにする。",
        "start open enable publish expose public internet remote external tunnel ngrok domain https hub 公開 外部 インターネット 遠隔 リモート トンネル ngrok ドメイン",
        ["Publish the participant Hub for someone outside the local computer.", "離れた場所の相手が参加できるようにHubを公開する。"],
        [_RUNNING, "Configure ngrok with secret-set; remote_domain in settings-set is an optional requested domain. Only participant Hub routes are published; local administrator routes stay local. / 管理画面は外部公開しない。"]),
    "tunnel-stop": _card(
        "現在所有している公開トンネルを閉じ、Hubのインターネット公開を終了する。",
        "stop close disable unpublish remove public internet remote external tunnel ngrok exposure 公開 終了 停止 閉じる 外部 トンネル ngrok 非公開",
        ["Turn off external access to the participant Hub.", "参加者Hubの外部公開を止める。"], [_RUNNING]),
    "audio-share": _card(
        "指定秒数だけA側PCの再生音をBへ共有する。ハートビートを維持し、終了時に所有する録音を止める。",
        "share capture stream broadcast send computer pc system desktop playback output loopback audio sound seconds duration 音声 音 再生音 共有 送る 配信 PC パソコン システム 出力 ループバック 秒間",
        ["Share what is playing on the PC for a fixed duration.", "A側PCで再生している音を指定秒数だけ相手へ共有する。"],
        [_RUNNING, _SESSION, "Requires a started live session and native Windows output capture; demo is not supported. The command must remain running to maintain its owned capture lease.",
         "PC output can include other apps and notifications. B microphone permission and translated TTS are separate unsupported host commands. / PC音には他アプリや通知も含み得る。"],
        {"--seconds": "Explicit integer duration from user intent, 1–3600 seconds; required.",
         "--device-id": "Exact output ID from devices; omit to use the current operating system default."}),
}


LIMITATION_CARDS = {
    "credential-read-delete": {
        "summary": {"en": "The agent CLI cannot reveal saved API key plaintext or delete a stored key. It exposes configuration metadata and accepts non-empty replacement keys only.",
                    "ja": "エージェントCLIは保存済みAPIキーの本文表示やキー削除に対応していない。設定有無の確認と、空でないキーの登録・置換のみ可能。"},
        "terms": "reveal show read print extract export delete remove erase clear saved stored secret credential api key token キー 表示 開示 読む 削除 消去 消す APIキー 秘密 認証情報",
        "when_to_use": ["A request to retrieve the actual stored key or delete it through the host agent CLI.", "保存したAPIキーそのものを表示・取得したり削除したい場合の制限を確認する。"],
        "prerequisites": ["settings-get returns data.secrets[provider] metadata only, never the secret. secret-set requires a non-empty plaintext replacement; an empty input cannot delete a key.",
                          "登録状態だけなら settings-get を使える。キーの本文表示や削除の代わりとして provider-test や secret-set を実行しない。"],
        "input_sources": {},
    },
    "participant-browser-microphone": {
        "summary": {"en": "The host agent cannot start participant B's browser microphone or grant its permission. B must open the invite and explicitly allow and start the microphone.",
                    "ja": "ホストのCLIから参加者Bのマイクを開始したり、ブラウザのマイク許可を代行したりできない。B本人が招待を開き、許可して開始する。"},
        "terms": "participant guest browser microphone mic permission grant allow enable activate record start remote B browser マイク 許可 権限 ブラウザ 参加者 相手 遠隔 自動 録音 開始",
        "when_to_use": ["A request to activate or authorize B's browser microphone from the host.", "参加者のマイクをホストから開始・許可したい場合の制限を確認する。"],
        "prerequisites": ["Create and start a live session, then use invite-create to obtain the intended participant's join URL. B must perform the microphone action in the browser.",
                          "audio-share captures A's PC playback, not B's microphone. / PC音声共有は参加者のマイク操作を代行しない。"],
        "input_sources": {},
    },
    "translated-tts": {
        "summary": {"en": "Translated speech synthesis, reading translations aloud, and voice cloning are not implemented. The app provides translated captions and shares original PC output audio.",
                    "ja": "翻訳文の音声合成・読み上げ・声の複製は未実装。翻訳字幕と、PCで再生中の元の音の共有を提供する。"},
        "terms": "tts text to speech speech synthesis synthesize synthesized translated voice speak aloud read aloud dubbing voice cloning voice translation 音声合成 合成音声 読み上げ 翻訳音声 吹き替え 声 複製 音声通訳",
        "when_to_use": ["A request to speak a translation, synthesize a voice or dub translated audio.", "翻訳結果を音声で読み上げたり、翻訳した声を生成したい場合の制限を確認する。"],
        "prerequisites": ["There is no executable TTS command. Use session-snapshot only when translated text satisfies the goal; do not substitute audio-share for speech synthesis.",
                          "翻訳音声生成用のコマンドはない。字幕で目的を満たせる場合だけ session-snapshot を検討する。"],
        "input_sources": {},
    },
}

_LIMITATION_TOPICS = {
    "credential-read-delete": "secret credential api key token キー 認証情報 トークン",
    "participant-browser-microphone": "microphone mic マイク",
    "translated-tts": "tts synthesis dubbing cloning 音声合成 合成音声 読み上げ 翻訳音声 吹き替え 音声通訳",
}


def knowledge_index(commands: list[dict]) -> dict:
    """Build a fresh deterministic index; reject missing, duplicate or extra cards."""
    if not isinstance(commands, list) or any(not isinstance(item, dict) for item in commands):
        raise ValueError("Command catalog must be a list of definitions.")
    names = [item.get("name") for item in commands]
    if any(not isinstance(name, str) for name in names) or len(set(names)) != len(names):
        raise ValueError("Command catalog names must be unique strings.")
    if set(names) != set(COMMAND_CARDS):
        raise ValueError("Command knowledge coverage does not match the executable catalog.")
    definitions = {item["name"]: item for item in commands}
    cards = []
    for name, metadata in sorted(COMMAND_CARDS.items()):
        definition = definitions[name]
        arguments = {item["name"] for item in definition["arguments"]}
        inputs = deepcopy(metadata["input_sources"])
        if "--session-id" in arguments:
            inputs["--session-id"] = "Exact session_id from session-create data.session_id or status data.session.session_id; never guess."
        if "--request-id" in arguments:
            inputs["--request-id"] = _REQUEST
        if set(inputs) != arguments:
            raise ValueError("Knowledge input sources do not match command arguments.")
        cards.append({"id": name, "kind": "command",
                      "summary": {"en": definition["summary"], "ja": metadata["ja"]},
                      "when_to_use": deepcopy(metadata["when_to_use"]),
                      "prerequisites": deepcopy(metadata["prerequisites"]),
                      "input_sources": inputs, "definition": deepcopy(definition),
                      "source": {"path": "src/translator/agent/knowledge.py", "anchor": f"command:{name}"},
                      "search_terms": metadata["terms"], "search_topics": _TOPICS[name]})
    for name, metadata in sorted(LIMITATION_CARDS.items()):
        cards.append({"id": name, "kind": "limitation",
                      **{key: deepcopy(metadata[key]) for key in ("summary", "when_to_use", "prerequisites", "input_sources")},
                      "definition": None,
                      "source": {"path": "src/translator/agent/knowledge.py", "anchor": f"limitation:{name}"},
                      "search_terms": metadata["terms"], "search_topics": _LIMITATION_TOPICS[name]})
    return {"retrieval_version": 1, "strategy": "local-bm25-cjk", "cards": cards}
