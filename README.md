# 日中同時通訳支援システム

## セットアップ

```bash
python -m venv venv
venv\Scriptsctivate        # Windows
# source venv/bin/activate   # macOS / Linux

pip install -r requirements.txt

copy .env.example .env
```

`.env` を編集して API キーを設定してください。

---

## フェーズ 1 — B のマイク音声をサーバーに届ける

### 1. サーバーを起動

```bash
python -m uvicorn server.main:app --reload --host 0.0.0.0 --port 8000
```

### 2. B のマイク送信クライアントを起動（別ターミナル）

```bash
python client_b/mic_sender.py
```

マイクに向かって話すと、サーバーのターミナルにチャンク受信ログが流れます。

---

## フェーズ 2 — ダミー STT + 翻訳 + A 画面表示

### 1. 依存パッケージを更新

```bash
pip install -r requirements.txt
```

### 2. サーバーを起動

```bash
python -m uvicorn server.main:app --reload --host 0.0.0.0 --port 8000
```

### 3. ブラウザで A の画面を開く

http://localhost:8000/a

### 4. B のマイク送信クライアントを起動（別ターミナル）

```bash
python client_b/mic_sender.py
```

### 5. マイクに向かって話す

約3秒ごとにダミーの中国語テキストと翻訳結果がブラウザに流れます。

| 表示色 | 意味 |
|--------|------|
| 灰色   | 確定前（interim）— 同じ行が上書きされます |
| 白     | 確定済み（final）— 行として固定されます |

---

## フェーズ 3 — A → B 音声リレー

### 1. 依存パッケージを更新（`soundcard` が追加されています）

```bash
pip install -r requirements.txt
```

### 起動順序（この順番を守ること）

```bash
# [1] サーバー
python -m uvicorn server.main:app --reload --host 0.0.0.0 --port 8000

# [2] B: A の音声を受信して再生（別ターミナル）
python client_b/audio_player.py

# [3] B: マイク音声をサーバーに送信（別ターミナル）
python client_b/mic_sender.py

# [4] ブラウザで A の画面を開く
#     http://localhost:8000/a

# [5] A: スピーカー音声をループバックキャプチャして送信（別ターミナル）
python client_a/audio_capture.py
```

### 動作確認

- A の PC で YouTube・通知音などを鳴らすと、B のスピーカーから同じ音が聞こえる
- 同時に B がマイクで話すと、A のブラウザにダミー翻訳が表示される（フェーズ2の継続確認）
- サーバーログで `[from_A] chunks=50 b_subscribers=1` のようなログが流れる

### 注意

`audio_capture.py` は WASAPI ループバックを使用します。起動時にターミナルに
「`Loopback device: ...`」と表示されるので、正しいスピーカーが選ばれているか確認してください。

---

## フェーズ 5 — 本物の STT (Gladia) + 翻訳 (DeepL) への切り替え

### 1. 依存パッケージを更新（`httpx` が追加されています）

```bash
pip install -r requirements.txt
```

### 2. API キーの取得

| サービス | 登録URL | 無料枠 |
|---------|---------|--------|
| Gladia  | https://app.gladia.io | 600分/月 |
| DeepL   | https://www.deepl.com/pro-api | 500,000文字/月 |

### 3. .env ファイルの設定

```
GLADIA_API_KEY=取得したキー
DEEPL_API_KEY=取得したキー（末尾に :fx が付く Free キー）
STT_PROVIDER=gladia
TRANSLATION_PROVIDER=deepl
```

ダミーに戻す場合は `STT_PROVIDER=dummy` / `TRANSLATION_PROVIDER=dummy` に変更。

### 4. 起動と動作確認

```bash
# [1] サーバー（起動ログに "STT: GladiaSTTProvider" と表示されれば成功）
python -m uvicorn server.main:app --reload --host 0.0.0.0 --port 8000

# [2] B: audio_player.py（別ターミナル）
python client_b/audio_player.py

# [3] B: mic_sender.py（別ターミナル）
python client_b/mic_sender.py

# [4] ブラウザで A の画面を開く
#     http://localhost:8000/a
```

マイクに向かって中国語で話す（「你好」「今天天气很好」など）と、
ブラウザに実際の日本語翻訳が表示されます。

---

## リモート接続（ngrok 経由）

サーバーを ngrok 経由で外部に公開する手順:

### 1. ngrok を起動

```bash
ngrok http 8000
```

### 2. 表示された URL をメモ

```
Forwarding  https://xxxx.ngrok-free.dev -> http://localhost:8000
```

### 3. クライアント側の .env を更新

```
SERVER_URL=wss://xxxx.ngrok-free.dev
```

`ws://` ではなく `wss://`（TLS あり）であることに注意してください。

### 4. 各クライアントと URL

| 役割 | URL |
|------|-----|
| A の画面 | `https://xxxx.ngrok-free.dev/a` |
| B の画面 | `https://xxxx.ngrok-free.dev/b` |
| client_a/audio_capture.py | `.env` の `SERVER_URL` を参照 |
| client_b/mic_sender.py | `.env` の `SERVER_URL` を参照 |
| client_b/audio_player.py | `.env` の `SERVER_URL` を参照 |

ブラウザ画面（web_a / web_b）は表示元のオリジンから自動的に WebSocket URL を組み立てるため、`.env` の変更は不要です。

---

## ディレクトリ構成

```
myapp/
├── server/          # FastAPI + WebSocket 中央ハブ
├── client_a/        # A 側クライアント（フェーズ3〜）
├── client_b/        # B 側クライアント
├── web_a/           # A のテレプロンプター UI
├── web_b/           # B のフィードバック UI（フェーズ2〜）
├── shared/          # STT / 翻訳の抽象化ライブラリ
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```
