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
- 起動時にリッチメニュー（`メニュー / 一覧 / 追加 / 削除 / キャンセル`）を自動作成・デフォルト適用

### Render本番起動（推奨）

Renderでは `Start Command` を以下にしてください。

```bash
gunicorn --workers 1 --threads 4 --bind 0.0.0.0:$PORT --factory line_daily_sender:create_app_for_gunicorn
```

## 4. LINE Webhook設定

1. 公開URL（例: RenderのURL）を用意
2. LINE DevelopersでWebhook URLを次に設定  
   `https://<your-domain>/callback`
3. Webhookを有効化

## 5. LINEコマンド

- `一覧` または `価格`  
  同じ動作です。自分の監視中商品一覧（現在価格・登録後最安値・前回比・状態・URL付き）を返します。
- `追加 商品名 URL`  
  自分の監視対象に新しい商品を追加します。
- `追加`  
  追加したいURLの入力待ちになります。次のメッセージでURLを送ると追加します。
- `削除 番号` または `削除 商品名`  
  自分の監視対象から商品を削除します。
- `削除`  
  削除したいURLの入力待ちになります。次のメッセージでURLを送ると削除します。
- `キャンセル`  
  `追加` / `削除` のURL入力待ち状態を取り消します。

※ `追加` / `削除` のURL入力待ちは3分で自動キャンセルされます。

例:

```text
追加 EchoDot https://www.amazon.co.jp/dp/XXXXXXXXXX 5000
削除 1
追加
https://www.amazon.co.jp/dp/XXXXXXXXXX
```

## 6. products.json

監視対象の商品は `products.json` に保存されます。  
保存形式は「ユーザーIDごとの商品リスト」です。

## 7. 注意点

- AmazonのHTMLは頻繁に変わるため、価格取得が失敗する場合があります。
- 取得失敗時は `last_status` に理由を記録します。
- 本番ではPA-APIなど正規APIの利用を推奨します。
- 既存のリッチメニュー運用を手動で行いたい場合は `AUTO_SETUP_RICH_MENU=false` を設定してください。
