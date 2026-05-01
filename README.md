# LINE 常時監視 + 毎日自動送信システム

このプロジェクトは以下を1つのプロセスで実現します。

- Amazon商品の価格監視（定期チェック）
- LINEでコマンド応答（Webhook）
- 毎日決まった時刻に自動レポート送信

## 1. 事前準備

1. LINE DevelopersでMessaging APIチャネルを作成
2. チャネルアクセストークン（長期）を取得
3. チャネルシークレットを取得
4. 送信先の `userId` または `groupId` を取得
5. ボットを送信先（友だち/グループ）に追加

## 2. セットアップ

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

`.env` を編集してください。

```env
LINE_CHANNEL_ACCESS_TOKEN=your_channel_access_token
LINE_CHANNEL_SECRET=your_channel_secret
LINE_TO=your_user_id_or_group_id
MESSAGE_TEXT=おはようございます。Amazon商品の定期レポートです。
SEND_TIME=09:00
TIMEZONE=Asia/Tokyo
MONITOR_INTERVAL_SECONDS=300
PORT=8080
AUTO_SETUP_RICH_MENU=true
```

## 3. 実行

```powershell
python line_daily_sender.py
```

起動すると以下が動作します。

- `MONITOR_INTERVAL_SECONDS` 間隔で `products.json` の商品を監視
- 毎日 `SEND_TIME` に `LINE_TO` 宛てで定期レポート送信
- `PORT` でWebhookサーバーを待ち受け
- 起動時にリッチメニュー（`メニュー / 一覧 / 価格`）を自動作成・デフォルト適用

## 4. LINE Webhook設定

1. 公開URL（例: RenderのURL）を用意
2. LINE DevelopersでWebhook URLを次に設定  
   `https://<your-domain>/callback`
3. Webhookを有効化

## 5. LINEコマンド

- `一覧`  
  監視中の商品一覧を返します。
- `価格`  
  監視中商品の最新価格を返します。
- `価格 キーワード`  
  商品名にキーワードが含まれるものだけ返します。
- `追加 商品名 URL [目標価格]`  
  新しい商品を監視対象に追加します。

例:

```text
追加 EchoDot https://www.amazon.co.jp/dp/XXXXXXXXXX 5000
```

## 6. products.json

監視対象の商品は `products.json` に保存されます。  
初期状態は空配列です。

## 7. 注意点

- AmazonのHTMLは頻繁に変わるため、価格取得が失敗する場合があります。
- 取得失敗時は `last_status` に理由を記録します。
- 本番ではPA-APIなど正規APIの利用を推奨します。
- 既存のリッチメニュー運用を手動で行いたい場合は `AUTO_SETUP_RICH_MENU=false` を設定してください。
