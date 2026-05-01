import base64
import hashlib
import hmac
import json
import os
import threading
import time
from io import BytesIO
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request
from PIL import Image, ImageDraw, ImageFont


LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
PRODUCT_FILE_PATH = Path("products.json")
LINE_RICHMENU_LIST_URL = "https://api.line.me/v2/bot/richmenu/list"
LINE_RICHMENU_CREATE_URL = "https://api.line.me/v2/bot/richmenu"


@dataclass
class Settings:
    channel_access_token: str
    channel_secret: str
    default_to: str
    daily_message_header: str
    send_time: str
    timezone: str
    monitor_interval_seconds: int
    http_port: int
    auto_setup_rich_menu: bool


def load_settings() -> Settings:
    load_dotenv()

    channel_access_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    channel_secret = os.getenv("LINE_CHANNEL_SECRET", "").strip()
    default_to = os.getenv("LINE_TO", "").strip()
    daily_message_header = os.getenv(
        "MESSAGE_TEXT",
        "おはようございます。Amazon商品の定期レポートです。",
    ).strip()
    send_time = os.getenv("SEND_TIME", "").strip()
    timezone = os.getenv("TIMEZONE", "Asia/Tokyo").strip()
    monitor_interval_seconds = int(os.getenv("MONITOR_INTERVAL_SECONDS", "300"))
    http_port = int(os.getenv("PORT", "8080"))
    auto_setup_rich_menu = os.getenv("AUTO_SETUP_RICH_MENU", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    missing = []
    if not channel_access_token:
        missing.append("LINE_CHANNEL_ACCESS_TOKEN")
    if not channel_secret:
        missing.append("LINE_CHANNEL_SECRET")
    if not default_to:
        missing.append("LINE_TO")
    if not send_time:
        missing.append("SEND_TIME")
    if missing:
        raise ValueError("次の環境変数が未設定です: " + ", ".join(missing))

    validate_send_time(send_time)
    _get_timezone(timezone)

    if monitor_interval_seconds < 60:
        raise ValueError("MONITOR_INTERVAL_SECONDSは60秒以上を指定してください。")

    return Settings(
        channel_access_token=channel_access_token,
        channel_secret=channel_secret,
        default_to=default_to,
        daily_message_header=daily_message_header,
        send_time=send_time,
        timezone=timezone,
        monitor_interval_seconds=monitor_interval_seconds,
        http_port=http_port,
        auto_setup_rich_menu=auto_setup_rich_menu,
    )


def validate_send_time(send_time: str) -> None:
    parts = send_time.split(":")
    if len(parts) != 2:
        raise ValueError("SEND_TIMEはHH:MM形式で指定してください。例: 09:00")

    hour, minute = parts
    if not hour.isdigit() or not minute.isdigit():
        raise ValueError("SEND_TIMEは数字のHH:MM形式で指定してください。")

    h = int(hour)
    m = int(minute)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("SEND_TIMEの時刻が不正です。0-23時、0-59分で指定してください。")


def _get_timezone(timezone_name: str) -> timezone | ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        if timezone_name == "Asia/Tokyo":
            print("[WARN] Asia/Tokyo が見つからないため固定JST(+09:00)を使用します。")
            return timezone(timedelta(hours=9), name="JST")
        raise


def _line_headers(settings: Settings) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.channel_access_token}",
    }


def _line_json_request(
    settings: Settings,
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = requests.request(
        method=method,
        url=url,
        headers=_line_headers(settings),
        json=payload,
        timeout=20,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"LINE API失敗: {method} {url} status={response.status_code}, body={response.text}")
    if not response.text:
        return {}
    return response.json()


def send_line_push(settings: Settings, to: str, text: str) -> None:
    payload = {"to": to, "messages": [{"type": "text", "text": text}]}
    response = requests.post(
        LINE_PUSH_URL,
        headers=_line_headers(settings),
        json=payload,
        timeout=15,
    )
    if response.status_code != 200:
        raise RuntimeError(f"LINE Push失敗: status={response.status_code}, body={response.text}")


def send_line_reply(settings: Settings, reply_token: str, text: str) -> None:
    payload = {"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}
    response = requests.post(
        LINE_REPLY_URL,
        headers=_line_headers(settings),
        json=payload,
        timeout=15,
    )
    if response.status_code != 200:
        raise RuntimeError(f"LINE Reply失敗: status={response.status_code}, body={response.text}")


def _generate_rich_menu_image() -> bytes:
    width, height = 2500, 1686
    image = Image.new("RGB", (width, height), color=(20, 93, 160))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    title = "Amazon 価格チェック"
    draw.rectangle([(0, 0), (width, 220)], fill=(13, 63, 110))
    draw.text((80, 85), title, fill=(255, 255, 255), font=font)

    section_height = (height - 220) // 3
    labels = ["メニュー", "一覧", "価格"]
    for i, label in enumerate(labels):
        y0 = 220 + i * section_height
        y1 = 220 + (i + 1) * section_height
        fill = (42, 128, 196) if i % 2 == 0 else (34, 116, 180)
        draw.rectangle([(0, y0), (width, y1)], fill=fill)
        draw.text((120, y0 + section_height // 2 - 10), label, fill=(255, 255, 255), font=font)

    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def setup_rich_menu(settings: Settings) -> None:
    if not settings.auto_setup_rich_menu:
        return

    rich_menu_name = "amazon-monitor-main"
    menu_list = _line_json_request(settings, "GET", LINE_RICHMENU_LIST_URL).get("richmenus", [])
    rich_menu_id = None
    for item in menu_list:
        if item.get("name") == rich_menu_name:
            rich_menu_id = item.get("richMenuId")
            break

    if not rich_menu_id:
        payload = {
            "size": {"width": 2500, "height": 1686},
            "selected": True,
            "name": rich_menu_name,
            "chatBarText": "メニュー",
            "areas": [
                {
                    "bounds": {"x": 0, "y": 220, "width": 2500, "height": 488},
                    "action": {"type": "message", "text": "メニュー"},
                },
                {
                    "bounds": {"x": 0, "y": 708, "width": 2500, "height": 489},
                    "action": {"type": "message", "text": "一覧"},
                },
                {
                    "bounds": {"x": 0, "y": 1197, "width": 2500, "height": 489},
                    "action": {"type": "message", "text": "価格"},
                },
            ],
        }
        created = _line_json_request(settings, "POST", LINE_RICHMENU_CREATE_URL, payload)
        rich_menu_id = created.get("richMenuId")
        if not rich_menu_id:
            raise RuntimeError("rich menu作成に失敗しました。")

        content_url = f"https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content"
        image_bytes = _generate_rich_menu_image()
        response = requests.post(
            content_url,
            headers={
                "Authorization": f"Bearer {settings.channel_access_token}",
                "Content-Type": "image/png",
            },
            data=image_bytes,
            timeout=20,
        )
        if response.status_code >= 300:
            raise RuntimeError(
                f"rich menu画像アップロード失敗: status={response.status_code}, body={response.text}"
            )

    set_default_url = f"https://api.line.me/v2/bot/user/all/richmenu/{rich_menu_id}"
    _line_json_request(settings, "POST", set_default_url)
    print(f"[INFO] rich menu設定完了: {rich_menu_id}")


def load_products() -> list[dict[str, Any]]:
    if not PRODUCT_FILE_PATH.exists():
        return []
    raw = PRODUCT_FILE_PATH.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("products.json は配列形式である必要があります。")
    return data


def save_products(products: list[dict[str, Any]]) -> None:
    PRODUCT_FILE_PATH.write_text(
        json.dumps(products, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def parse_price_to_int(text: str) -> int | None:
    cleaned = text.replace(",", "").replace("￥", "").replace("¥", "").strip()
    digits = "".join(ch for ch in cleaned if ch.isdigit())
    if not digits:
        return None
    return int(digits)


def fetch_amazon_price(url: str) -> tuple[int | None, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    response = requests.get(url, headers=headers, timeout=20)
    if response.status_code != 200:
        return None, f"取得失敗(status={response.status_code})"

    soup = BeautifulSoup(response.text, "html.parser")
    selectors = [
        "#priceblock_dealprice",
        "#priceblock_ourprice",
        "#priceblock_saleprice",
        ".a-price .a-offscreen",
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if node and node.text:
            price = parse_price_to_int(node.text)
            if price is not None:
                return price, "OK"
    return None, "価格要素が見つかりませんでした"


def format_product_line(product: dict[str, Any]) -> str:
    latest = product.get("last_price")
    latest_text = f"{latest:,}円" if isinstance(latest, int) else "不明"
    target = product.get("target_price")
    target_text = f"{target:,}円" if isinstance(target, int) else "-"
    return f"- {product.get('name', 'no-name')}: 現在 {latest_text} / 目標 {target_text}"


def _format_checked_at_text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime("%m/%d %H:%M")
    except ValueError:
        return value


def format_product_card(product: dict[str, Any], index: int | None = None) -> str:
    name = str(product.get("name", "no-name"))
    latest = product.get("last_price")
    latest_text = f"{latest:,}円" if isinstance(latest, int) else "不明"
    target = product.get("target_price")
    target_text = f"{target:,}円" if isinstance(target, int) else "-"
    if isinstance(latest, int) and isinstance(target, int):
        diff = latest - target
        if diff <= 0:
            diff_text = f"目標達成 ({abs(diff):,}円安い)"
        else:
            diff_text = f"目標まであと {diff:,}円"
    else:
        diff_text = "-"
    checked_at_text = _format_checked_at_text(product.get("last_checked_at"))
    status_text = str(product.get("last_status", "-"))
    prefix = f"[{index}] " if isinstance(index, int) else ""
    lines = [
        f"{prefix}{name}",
        f"  現在価格: {latest_text}",
        f"  目標価格: {target_text}",
        f"  差分: {diff_text}",
        f"  最終確認: {checked_at_text}",
        f"  取得状態: {status_text}",
    ]
    return "\n".join(lines)


def build_product_list_message(title: str, products: list[dict[str, Any]]) -> str:
    if not products:
        return "監視中の商品はありません。"
    lines = [title, ""]
    for idx, product in enumerate(products, start=1):
        lines.append(format_product_card(product, idx))
        lines.append("")
    return "\n".join(lines).strip()[:4900]


def build_menu_message() -> str:
    lines = [
        "リッチメニューで使えるコマンド",
        "",
        "1) メニュー",
        "   - この一覧を表示します。",
        "",
        "2) 一覧",
        "   - 監視中の商品を一覧表示します。",
        "",
        "3) 価格",
        "   - 全商品の最新価格を表示します。",
        "",
        "※商品追加や詳細指定は管理者用の手入力コマンドです。",
    ]
    return "\n".join(lines)[:4900]


def build_report_text(settings: Settings, products: list[dict[str, Any]]) -> str:
    lines = [settings.daily_message_header]
    if not products:
        lines.append("監視対象の商品がまだありません。")
        return "\n".join(lines)
    lines.append("")
    lines.append("【監視中の商品レポート】")
    for idx, product in enumerate(products, start=1):
        lines.append(format_product_card(product, idx))
        lines.append("")
    return "\n".join(lines)[:4900]


def check_price_alert(settings: Settings, product: dict[str, Any], latest_price: int) -> None:
    target = product.get("target_price")
    if isinstance(target, int) and latest_price <= target:
        text = (
            f"価格アラート: {product.get('name')}\n"
            f"現在価格 {latest_price:,}円 が目標価格 {target:,}円 以下になりました。\n"
            f"{product.get('url')}"
        )
        send_line_push(settings, settings.default_to, text[:4900])


def refresh_product_price(product: dict[str, Any], now: datetime) -> None:
    try:
        latest_price, status = fetch_amazon_price(product.get("url", ""))
        product["last_checked_at"] = now.isoformat()
        product["last_status"] = status
        if latest_price is not None:
            prev = product.get("last_price")
            product["last_price"] = latest_price
            if prev != latest_price:
                product["last_changed_at"] = now.isoformat()
    except Exception as exc:  # noqa: BLE001
        product["last_checked_at"] = now.isoformat()
        product["last_status"] = f"エラー: {exc}"


def monitoring_loop(settings: Settings) -> None:
    tz = _get_timezone(settings.timezone)
    last_daily_sent_date = None
    print("[INFO] 監視ループを開始しました。")

    while True:
        now = datetime.now(tz)
        products = load_products()

        for product in products:
            prev_price = product.get("last_price")
            refresh_product_price(product, now)
            latest_price = product.get("last_price")
            if isinstance(latest_price, int) and latest_price != prev_price:
                check_price_alert(settings, product, latest_price)

        save_products(products)

        if now.strftime("%H:%M") == settings.send_time and last_daily_sent_date != now.date():
            try:
                send_line_push(
                    settings,
                    settings.default_to,
                    build_report_text(settings, products),
                )
                last_daily_sent_date = now.date()
                print(f"[SUCCESS] {now.isoformat()} 日次レポート送信完了")
            except Exception as exc:  # noqa: BLE001
                print(f"[ERROR] {now.isoformat()} 日次送信エラー: {exc}")

        time.sleep(settings.monitor_interval_seconds)


def verify_line_signature(channel_secret: str, body: bytes, x_line_signature: str) -> bool:
    digest = hmac.new(
        channel_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, x_line_signature)


def handle_command(text: str) -> str:
    products = load_products()
    now = datetime.now()
    parts = text.strip().split(maxsplit=3)
    if not parts:
        return build_menu_message()

    cmd = parts[0]
    if cmd in {"メニュー", "ﾒﾆｭｰ", "ヘルプ", "help", "Help", "HELP"}:
        return build_menu_message()

    if cmd == "一覧":
        return build_product_list_message("監視中の商品一覧", products)

    if cmd == "価格":
        if len(parts) == 1:
            if not products:
                return "監視中の商品はありません。"
            changed = False
            for product in products:
                if not isinstance(product.get("last_price"), int):
                    refresh_product_price(product, now)
                    changed = True
            if changed:
                save_products(products)
            return build_product_list_message("価格一覧", products)

        keyword = parts[1].lower()
        filtered = [p for p in products if keyword in str(p.get("name", "")).lower()]
        if not filtered:
            return f"'{parts[1]}' に一致する商品がありません。"
        changed = False
        for product in filtered:
            if not isinstance(product.get("last_price"), int):
                refresh_product_price(product, now)
                changed = True
        if changed:
            save_products(products)
        return build_product_list_message(f"価格一覧 (キーワード: {parts[1]})", filtered)

    if cmd == "追加":
        if len(parts) < 3:
            return "使い方: 追加 商品名 URL [目標価格]"

        name = parts[1]
        url = parts[2]
        target_price = None
        if len(parts) >= 4:
            target_price = parse_price_to_int(parts[3])
            if target_price is None:
                return "目標価格は数値で指定してください。例: 10000"

        products.append(
            {
                "name": name,
                "url": url,
                "target_price": target_price,
                "last_price": None,
                "last_status": "未確認",
                "last_checked_at": None,
                "last_changed_at": None,
            }
        )
        save_products(products)
        return f"商品を追加しました: {name}"

    return "不明なコマンドです。\n\n" + build_menu_message()


def create_app(settings: Settings) -> Flask:
    app = Flask(__name__)

    @app.get("/health")
    def health() -> Any:
        return jsonify({"ok": True})

    @app.post("/callback")
    def callback() -> Any:
        body = request.get_data()
        signature = request.headers.get("X-Line-Signature", "")
        if not verify_line_signature(settings.channel_secret, body, signature):
            abort(401, "invalid signature")

        payload = request.get_json(silent=True) or {}
        for event in payload.get("events", []):
            if event.get("type") != "message":
                continue
            message = event.get("message", {})
            if message.get("type") != "text":
                continue
            reply_token = event.get("replyToken")
            text = str(message.get("text", ""))
            reply = handle_command(text)
            if reply_token:
                send_line_reply(settings, reply_token, reply)

        return "OK", 200

    return app


def ensure_product_file() -> None:
    if PRODUCT_FILE_PATH.exists():
        return
    save_products([])


def main() -> None:
    try:
        settings = load_settings()
        setup_rich_menu(settings)
        ensure_product_file()

        thread = threading.Thread(
            target=monitoring_loop,
            args=(settings,),
            daemon=True,
        )
        thread.start()

        app = create_app(settings)
        print(f"[INFO] HTTPサーバー起動 port={settings.http_port}")
        app.run(host="0.0.0.0", port=settings.http_port)
    except KeyboardInterrupt:
        print("\n[INFO] 停止しました。")
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
