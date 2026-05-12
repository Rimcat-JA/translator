SUPPORTED_LANGUAGES = [
    {"code": "ar", "name_ja": "アラビア語",       "name_en": "Arabic",               "deepl_source": "AR",    "deepl_target": "AR"},
    {"code": "bg", "name_ja": "ブルガリア語",      "name_en": "Bulgarian",            "deepl_source": "BG",    "deepl_target": "BG"},
    {"code": "cs", "name_ja": "チェコ語",          "name_en": "Czech",                "deepl_source": "CS",    "deepl_target": "CS"},
    {"code": "da", "name_ja": "デンマーク語",      "name_en": "Danish",               "deepl_source": "DA",    "deepl_target": "DA"},
    {"code": "de", "name_ja": "ドイツ語",          "name_en": "German",               "deepl_source": "DE",    "deepl_target": "DE"},
    {"code": "el", "name_ja": "ギリシャ語",        "name_en": "Greek",                "deepl_source": "EL",    "deepl_target": "EL"},
    {"code": "en", "name_ja": "英語",              "name_en": "English",              "deepl_source": "EN",    "deepl_target": "EN-US"},
    {"code": "es", "name_ja": "スペイン語",        "name_en": "Spanish",              "deepl_source": "ES",    "deepl_target": "ES"},
    {"code": "et", "name_ja": "エストニア語",      "name_en": "Estonian",             "deepl_source": "ET",    "deepl_target": "ET"},
    {"code": "fi", "name_ja": "フィンランド語",    "name_en": "Finnish",              "deepl_source": "FI",    "deepl_target": "FI"},
    {"code": "fr", "name_ja": "フランス語",        "name_en": "French",               "deepl_source": "FR",    "deepl_target": "FR"},
    {"code": "hu", "name_ja": "ハンガリー語",      "name_en": "Hungarian",            "deepl_source": "HU",    "deepl_target": "HU"},
    {"code": "id", "name_ja": "インドネシア語",    "name_en": "Indonesian",           "deepl_source": "ID",    "deepl_target": "ID"},
    {"code": "it", "name_ja": "イタリア語",        "name_en": "Italian",              "deepl_source": "IT",    "deepl_target": "IT"},
    {"code": "ja", "name_ja": "日本語",            "name_en": "Japanese",             "deepl_source": "JA",    "deepl_target": "JA"},
    {"code": "ko", "name_ja": "韓国語",            "name_en": "Korean",               "deepl_source": "KO",    "deepl_target": "KO"},
    {"code": "lt", "name_ja": "リトアニア語",      "name_en": "Lithuanian",           "deepl_source": "LT",    "deepl_target": "LT"},
    {"code": "lv", "name_ja": "ラトビア語",        "name_en": "Latvian",              "deepl_source": "LV",    "deepl_target": "LV"},
    {"code": "nb", "name_ja": "ノルウェー語",      "name_en": "Norwegian",            "deepl_source": "NB",    "deepl_target": "NB"},
    {"code": "nl", "name_ja": "オランダ語",        "name_en": "Dutch",                "deepl_source": "NL",    "deepl_target": "NL"},
    {"code": "pl", "name_ja": "ポーランド語",      "name_en": "Polish",               "deepl_source": "PL",    "deepl_target": "PL"},
    {"code": "pt", "name_ja": "ポルトガル語",      "name_en": "Portuguese",           "deepl_source": "PT",    "deepl_target": "PT-PT"},
    {"code": "ro", "name_ja": "ルーマニア語",      "name_en": "Romanian",             "deepl_source": "RO",    "deepl_target": "RO"},
    {"code": "ru", "name_ja": "ロシア語",          "name_en": "Russian",              "deepl_source": "RU",    "deepl_target": "RU"},
    {"code": "sk", "name_ja": "スロバキア語",      "name_en": "Slovak",               "deepl_source": "SK",    "deepl_target": "SK"},
    {"code": "sl", "name_ja": "スロベニア語",      "name_en": "Slovenian",            "deepl_source": "SL",    "deepl_target": "SL"},
    {"code": "sv", "name_ja": "スウェーデン語",    "name_en": "Swedish",              "deepl_source": "SV",    "deepl_target": "SV"},
    {"code": "tr", "name_ja": "トルコ語",          "name_en": "Turkish",              "deepl_source": "TR",    "deepl_target": "TR"},
    {"code": "uk", "name_ja": "ウクライナ語",      "name_en": "Ukrainian",            "deepl_source": "UK",    "deepl_target": "UK"},
    {"code": "zh", "name_ja": "中国語(簡体)",      "name_en": "Chinese (Simplified)", "deepl_source": "ZH",    "deepl_target": "ZH-HANS"},
]


def get_language(code: str) -> dict | None:
    for lang in SUPPORTED_LANGUAGES:
        if lang["code"] == code:
            return lang
    return None
