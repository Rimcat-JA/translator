# 日中同時通訳支援システム

## システム概要

2人のオペレーター A と B が連携する通訳システム。

- **A**: Zoom で日本語の通話相手と会話。Zoom 音声をキャプチャしてサーバー経由で B に中継する。B が訳した日本語をテレプロンプター形式の画面で受け取り、それを読み上げて Zoom のマイクから通話相手に届ける。
- **B**: A から中継された日本語音声を聞き、頭の中で中国語に通訳して発話する。B のマイク音声は STT で中国語テキストに変換され、さらに翻訳 API で日本語に訳されて A の画面に表示される。

## データフロー

```
1. Zoom の通話相手(日本語) → A の PC
2. A の PC で Zoom 音声をキャプチャ → サーバー
3. サーバー → B の PC で再生(B が日本語を聞く)
4. B が中国語で発話 → B のマイク → サーバー
5. サーバー → STT API(中国語認識)
6. 認識結果 → 翻訳 API(中→日)
7. 翻訳結果 → A の PC のテレプロンプター画面
8. A が画面の日本語を読み上げ → Zoom マイク → 通話相手
```

## プロジェクト構造

```
myapp/
├── server/
│   └── main.py                  # FastAPI + WebSocket 中央ハブ
├── client_a/
│   ├── audio_capture.py         # VB-Cable 等の仮想デバイスから Zoom 音声取得
│   └── mic_player.py            # (将来用) サーバーから音声を受信して再生
├── client_b/
│   ├── audio_player.py          # サーバーから A の音声を受信して再生
│   └── mic_sender.py            # マイク音声をサーバーに送信
├── web_a/
│   └── index.html               # A のテレプロンプター画面
├── web_b/
│   └── index.html               # B のフィードバック画面
├── shared/
│   ├── stt_provider.py          # STT 抽象化 (Dummy / Gladia 等を切替)
│   └── translation_provider.py  # 翻訳抽象化 (Dummy / DeepL 等を切替)
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

## 主要コンポーネント詳細

### server/
FastAPI + WebSocket の中央ハブ。

| エンドポイント       | 役割                                      |
|---------------------|-------------------------------------------|
| `/audio/from_A`     | A からの音声受信                          |
| `/audio/to_B`       | B への音声配信                            |
| `/audio/from_B`     | B からの音声受信(STT に流す)             |
| `/captions/to_A`    | A への字幕配信(翻訳結果)                 |

### client_a/
A 側の Python スクリプト群。

- `audio_capture.py`: VB-Cable 等の仮想オーディオデバイスから Zoom 音声を取得し、WebSocket でサーバーに送信する。
- `mic_player.py`: (将来用) サーバーから音声ストリームを受信してスピーカーで再生する。

### client_b/
B 側の Python スクリプト群。

- `audio_player.py`: サーバーから A の音声ストリームを受信してスピーカーで再生する。
- `mic_sender.py`: B のマイク音声を WebSocket でサーバーに送信する。

### web_a/
A のテレプロンプター (HTML/CSS/JS)。

- 確定行・現在行・予測行・仮テキストを色分け表示する。
- WebSocket でサーバーから字幕を受信してリアルタイム更新する。

### web_b/
B のフィードバック画面 (HTML/CSS/JS)。

- 自分の中国語 STT 結果と、A に送られた日本語翻訳を表示する。

### shared/
共通ライブラリ。抽象クラスを介して実装を差し替え可能にする。

- `stt_provider.py`: STT を抽象化。`DummySTTProvider` と `GladiaSTTProvider` を持つ。
- `translation_provider.py`: 翻訳を抽象化。`DummyTranslationProvider` と `DeepLTranslationProvider` を持つ。

## 技術スタック

| 分類             | 採用技術                                              |
|-----------------|-------------------------------------------------------|
| 言語             | Python 3.13                                           |
| サーバー          | FastAPI, uvicorn, websockets                          |
| 音声処理          | sounddevice (マイク/スピーカー), opuslib (圧縮)       |
| STT              | 初期: ダミー実装 / 本番: Gladia API (ストリーミング)  |
| 翻訳             | 初期: ダミー実装 / 本番: DeepL API                   |
| フロントエンド    | 素の HTML / JavaScript (フレームワーク不要)           |
| 設定管理          | python-dotenv (.env からAPIキー読み込み)              |

## 開発フェーズ

各フェーズは動作確認が取れた状態で完了とし、**ユーザーの明示的な許可があるまで次フェーズに進まないこと**。

### フェーズ 1: プロジェクト構造 + サーバー骨格
- ディレクトリ構成・requirements.txt・.env.example を作成する。
- `mic_sender.py` (client_b) と `server/main.py` を実装する。
- B のマイク音声がサーバーに届き、ログに出力されることを確認する(STT なし)。

### フェーズ 2: ダミー STT + ダミー翻訳 + A 画面表示
- `shared/stt_provider.py` と `shared/translation_provider.py` のダミー実装を作成する。
- B の音声を受け取ったら固定文字列を A の画面 (`web_a/`) に表示することを確認する。

### フェーズ 3: A → B 音声リレー
- `client_a/audio_capture.py` を実装する。
- A の音声をキャプチャしてサーバー経由で B に届け、B の `audio_player.py` で再生することを確認する。

### フェーズ 4: テレプロンプター UI の作り込み
- `web_a/` の UI を仕上げる。
- 行ごとの状態管理 (`history` / `current` / `upcoming` / `interim`) を実装する。

### フェーズ 5: 本物の STT / 翻訳 API 接続 (Gladia + DeepL)
- `shared/` の抽象化を活かしてダミー実装から差し替える。
- `.env` に `GLADIA_API_KEY` と `DEEPL_API_KEY` を追加する。

### フェーズ 6: 統合テスト・遅延計測
- エンドツーエンドの遅延を計測し、ボトルネックを特定する。
- README.md に計測結果と改善策を記載する。

## 重要な制約

### API キー管理
- API キーは**絶対にコードに直書きしない**。
- 必ず `.env` ファイルから `python-dotenv` で読み込む。
- `.env` は `.gitignore` に追加する。
- リポジトリには `.env.example`(プレースホルダー入り)のみ含める。

### 設計原則
- 各コンポーネントは抽象クラス / インターフェースを介して結合する(ダミーから本物の API に差し替えやすくする)。
- 仮想環境 (`venv`) を使用し、`requirements.txt` を常に最新に保つ。
- `README.md` に起動手順を必ず記載する。
