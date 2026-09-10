Translator 0.2.0 preview — Windows x64

ZIPをすべて展開し、Translator.exeを起動してください。
ブラウザが開いたら「デモを試す」で、APIキーなしのローカルデモを利用できます。
起動しただけでは音声は取得・送信されません。

実通訳には設定画面でGladia / DeepLキーを入力します。
Bのマイク音声をGladiaへ、認識文をDeepLへ送信します。
PC音声共有を開始した場合は、通知音を含む選択出力機器の音声をBへ送信します。
遠隔参加にはngrokの認証とHTTPS公開が必要です。
音声・会話本文はファイルに保存しません。

この配布物は未署名のプレビューです。警告の無効化は推奨しません。
GitHubの配布元とSHA256SUMS.txtを確認してください。
実API・2台での遠隔音声・Python等のないクリーンVMの検証は未完了です。
対応保証された一般公開版ではありません。

設定・ログ: %LOCALAPPDATA%\Translator
終了: 診断画面の「アプリを終了」、または Translator.exe stop
再起動: Translator.exe をもう一度起動
