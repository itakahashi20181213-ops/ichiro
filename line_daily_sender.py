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
LINE_RICHMENU_BASE_URL = "https://api.line.me/v2/bot/richmenu"
_BACKGROUND_STARTED = False
_BACKGROUND_LOCK = threading.Lock()
settings_cache: Settings | None = None
PENDING_ACTION_TIMEOUT_SECONDS = 180
pending_actions: dict[str, dict[str, Any]] = {}


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

    title_font = ImageFont.load_default()
    title = "Amazon Price Monitor"
    draw.rectangle([(0, 0), (width, 220)], fill=(13, 63, 110))
    draw.text((80, 60), title, fill=(255, 255, 255), font=title_font)

    top = 220
    cell_w = width
    cell_h = (height - top) // 5
    labels = [("MENU", 0), ("LIST", 1), ("ADD", 2), ("DELETE", 3), ("CANCEL", 4)]

    # 5x7ブロックフォント（環境依存なし）
    glyphs: dict[str, list[str]] = {
        "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
        "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
        "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
        "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
        "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
        "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
        "M": ["10001", "11011", "10101", "10001", "10001", "10001", "10001"],
        "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
        "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
        "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
        "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    }

    def draw_block_text(text: str, x0: int, y0: int, w: int, h: int) -> None:
        rows = 7
        cols = sum(6 for ch in text if ch in glyphs) - 1
        if cols <= 0:
            return
        scale = min((w - 140) // cols, (h - 160) // rows)
        scale = max(20, scale)
        text_w = cols * scale
        text_h = rows * scale
        sx = x0 + (w - text_w) // 2
        sy = y0 + (h - text_h) // 2

        cursor = sx
        for ch in text:
            pattern = glyphs.get(ch)
            if not pattern:
                continue
            for r, row_bits in enumerate(pattern):
                for c, bit in enumerate(row_bits):
                    if bit == "1":
                        px0 = cursor + c * scale
                        py0 = sy + r * scale
                        draw.rectangle(
                            [(px0, py0), (px0 + scale - 2, py0 + scale - 2)],
                            fill=(255, 255, 255),
                        )
            cursor += 6 * scale

    for en_label, row in labels:
        x0 = 0
        y0 = top + row * cell_h
        x1 = x0 + cell_w
        y1 = height if row == 4 else y0 + cell_h
        fill = (42, 128, 196) if row % 2 == 0 else (34, 116, 180)
        draw.rectangle([(x0, y0), (x1, y1)], fill=fill)
        draw.rectangle([(x0, y0), (x1, y1)], outline=(255, 255, 255), width=5)
        draw_block_text(en_label, x0, y0, cell_w, y1 - y0)

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

    # 仕様変更時に古いレイアウトが残らないよう、同名メニューは作り直す。
    if rich_menu_id:
        _line_json_request(settings, "DELETE", f"{LINE_RICHMENU_BASE_URL}/{rich_menu_id}")
        rich_menu_id = None

    payload = {
        "size": {"width": 2500, "height": 1686},
        "selected": True,
        "name": rich_menu_name,
        "chatBarText": "メニュー",
        "areas": [
            {
                "bounds": {"x": 0, "y": 220, "width": 2500, "height": 293},
                "action": {"type": "message", "text": "メニュー"},
            },
            {
                "bounds": {"x": 0, "y": 513, "width": 2500, "height": 293},
                "action": {"type": "message", "text": "一覧"},
            },
            {
                "bounds": {"x": 0, "y": 806, "width": 2500, "height": 293},
                "action": {"type": "message", "text": "追加"},
            },
            {
                "bounds": {"x": 0, "y": 1099, "width": 2500, "height": 293},
                "action": {"type": "message", "text": "削除"},
            },
            {
                "bounds": {"x": 0, "y": 1392, "width": 2500, "height": 294},
                "action": {"type": "message", "text": "キャンセル"},
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


def load_products() -> dict[str, list[dict[str, Any]]]:
    if not PRODUCT_FILE_PATH.exists():
        return {}
    raw = PRODUCT_FILE_PATH.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if isinstance(data, list):
        # 旧形式(list)から新形式(dict[user_id]=list)へ移行
        return {"legacy_default": data}
    if not isinstance(data, dict):
        raise ValueError("products.json はオブジェクト形式である必要があります。")

    normalized: dict[str, list[dict[str, Any]]] = {}
    for user_id, products in data.items():
        if isinstance(user_id, str) and isinstance(products, list):
            normalized[user_id] = products
    return normalized


def normalize_user_products_map(
    data: dict[str, list[dict[str, Any]]],
    default_owner_id: str | None,
) -> dict[str, list[dict[str, Any]]]:
    if "legacy_default" in data and default_owner_id and default_owner_id not in data:
        data[default_owner_id] = data.pop("legacy_default")
    return data


def save_products(products: dict[str, list[dict[str, Any]]]) -> None:
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


def fetch_page_title(url: str) -> str | None:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    response = requests.get(url, headers=headers, timeout=20)
    if response.status_code != 200:
        return None
    soup = BeautifulSoup(response.text, "html.parser")
    if soup.title and soup.title.text:
        title = soup.title.text.strip()
        if title:
            return title[:100]
    return None


def format_product_line(product: dict[str, Any]) -> str:
    latest = product.get("last_price")
    latest_text = f"{latest:,}円" if isinstance(latest, int) else "不明"
    min_price = product.get("min_price")
    min_price_text = f"{min_price:,}円" if isinstance(min_price, int) else "-"
    return f"- {product.get('name', 'no-name')}: 現在 {latest_text} / 最安値 {min_price_text}"


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
    min_price = product.get("min_price")
    min_price_text = f"{min_price:,}円" if isinstance(min_price, int) else "-"
    price_diff = product.get("price_diff")
    if isinstance(price_diff, int):
        if price_diff > 0:
            diff_text = f"+{price_diff:,}円"
        elif price_diff < 0:
            diff_text = f"-{abs(price_diff):,}円"
        else:
            diff_text = "±0円"
    else:
        diff_text = "-"
    checked_at_text = _format_checked_at_text(product.get("last_checked_at"))
    status_text = str(product.get("last_status", "-"))
    url_text = str(product.get("url", "-"))
    prefix = f"[{index}] " if isinstance(index, int) else ""
    lines = [
        f"{prefix}{name}",
        f"  現在価格: {latest_text}",
        f"  登録後最安値: {min_price_text}",
        f"  前回比: {diff_text}",
        f"  最終確認: {checked_at_text}",
        f"  取得状態: {status_text}",
        f"  URL: {url_text}",
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
        "2) 一覧 または 価格",
        "   - 同じ動作です。監視中の商品一覧と価格を表示します。",
        "",
        "3) 追加 <商品名> <URL>",
        "   - 自分の監視リストに商品を追加します。",
        "   - または「追加」と送信後にURLを送っても追加できます。",
        "",
        "4) 削除 <番号 or 商品名>",
        "   - 自分の監視リストから商品を削除します。",
        "   - または「削除」と送信後にURLを送っても削除できます。",
        "",
        "5) キャンセル",
        "   - 追加/削除の入力待ち状態を解除します。",
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


def check_price_alert(settings: Settings, to: str, product: dict[str, Any], latest_price: int) -> None:
    diff = product.get("price_diff")
    if isinstance(diff, int) and diff != 0:
        direction = "値上がり" if diff > 0 else "値下がり"
        text = (
            f"価格更新: {product.get('name')}\n"
            f"現在価格 {latest_price:,}円 ({direction} {abs(diff):,}円)\n"
            f"{product.get('url')}"
        )
        send_line_push(settings, to, text[:4900])


def refresh_product_price(product: dict[str, Any], now: datetime) -> None:
    try:
        latest_price, status = fetch_amazon_price(product.get("url", ""))
        product["last_checked_at"] = now.isoformat()
        product["last_status"] = status
        if latest_price is not None:
            prev = product.get("last_price")
            if isinstance(prev, int):
                product["price_diff"] = latest_price - prev
            else:
                product["price_diff"] = None
            product["last_price"] = latest_price
            if prev != latest_price:
                product["last_changed_at"] = now.isoformat()
            min_price = product.get("min_price")
            if not isinstance(min_price, int) or latest_price < min_price:
                product["min_price"] = latest_price
    except Exception as exc:  # noqa: BLE001
        product["last_checked_at"] = now.isoformat()
        product["last_status"] = f"エラー: {exc}"


def monitoring_loop(settings: Settings) -> None:
    tz = _get_timezone(settings.timezone)
    last_daily_sent_date = None
    print("[INFO] 監視ループを開始しました。")

    while True:
        now = datetime.now(tz)
        user_products_map = normalize_user_products_map(
            load_products(),
            settings.default_to,
        )

        for owner_id, products in user_products_map.items():
            for product in products:
                prev_price = product.get("last_price")
                refresh_product_price(product, now)
                latest_price = product.get("last_price")
                if isinstance(latest_price, int) and latest_price != prev_price:
                    check_price_alert(settings, owner_id, product, latest_price)

        save_products(user_products_map)

        if now.strftime("%H:%M") == settings.send_time and last_daily_sent_date != now.date():
            for owner_id, products in user_products_map.items():
                try:
                    send_line_push(
                        settings,
                        owner_id,
                        build_report_text(settings, products),
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[ERROR] {now.isoformat()} 日次送信エラー owner={owner_id}: {exc}")
            last_daily_sent_date = now.date()
            print(f"[SUCCESS] {now.isoformat()} 日次レポート送信完了")

        time.sleep(settings.monitor_interval_seconds)


def verify_line_signature(channel_secret: str, body: bytes, x_line_signature: str) -> bool:
    digest = hmac.new(
        channel_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, x_line_signature)


def get_owner_id_from_event(event: dict[str, Any]) -> str:
    source = event.get("source", {})
    for key in ("userId", "groupId", "roomId"):
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return "legacy_default"


def get_user_products_map() -> dict[str, list[dict[str, Any]]]:
    data = load_products()
    default_owner_id = settings_cache.default_to if settings_cache else None
    return normalize_user_products_map(data, default_owner_id)


def handle_command(text: str, owner_id: str) -> str:
    user_products_map = get_user_products_map()
    products = user_products_map.get(owner_id, [])
    now = datetime.now()
    plain_text = text.strip()
    parts = plain_text.split(maxsplit=3)
    if not parts:
        return build_menu_message()

    pending = pending_actions.get(owner_id)
    if pending:
        started_at = pending.get("started_at")
        if isinstance(started_at, (int, float)) and (time.time() - started_at) > PENDING_ACTION_TIMEOUT_SECONDS:
            pending_actions.pop(owner_id, None)
            pending = None

    if pending and pending.get("action") in {"add_waiting_url", "delete_waiting_url"}:
        if plain_text.startswith("http://") or plain_text.startswith("https://"):
            action = str(pending.get("action"))
            pending_actions.pop(owner_id, None)
            if action == "add_waiting_url":
                title = fetch_page_title(plain_text) or f"商品{len(products) + 1}"
                products.append(
                    {
                        "name": title,
                        "url": plain_text,
                        "last_price": None,
                        "min_price": None,
                        "price_diff": None,
                        "last_status": "未確認",
                        "last_checked_at": None,
                        "last_changed_at": None,
                    }
                )
                user_products_map[owner_id] = products
                save_products(user_products_map)
                return f"商品を追加しました: {title}"

            removed = None
            for i, product in enumerate(products):
                if str(product.get("url", "")).strip() == plain_text:
                    removed = products.pop(i)
                    break
            if not removed:
                return "一致するURLの商品が見つかりませんでした。削除をやり直す場合は「削除」と送ってください。"
            user_products_map[owner_id] = products
            save_products(user_products_map)
            return f"商品を削除しました: {removed.get('name', 'no-name')}"
        return (
            "URL形式で送ってください。例: https://www.amazon.co.jp/dp/XXXXXXXXXX\n"
            "入力待ちは3分で自動キャンセルされます。"
        )

    cmd = parts[0]
    if cmd in {"キャンセル", "cancel", "CANCEL", "Cancel"}:
        if owner_id in pending_actions:
            pending_actions.pop(owner_id, None)
            return "入力待ちをキャンセルしました。"
        return "キャンセルする入力待ちはありません。"

    if cmd in {"メニュー", "ﾒﾆｭｰ", "ヘルプ", "help", "Help", "HELP"}:
        return build_menu_message()

    if cmd in {"一覧", "価格"}:
        if not products:
            return "監視中の商品はありません。"
        changed = False
        for product in products:
            if not isinstance(product.get("last_price"), int):
                refresh_product_price(product, now)
                changed = True
        if changed:
            user_products_map[owner_id] = products
            save_products(user_products_map)
        return build_product_list_message("監視中の商品一覧", products)

    if cmd == "追加":
        if len(parts) == 1:
            pending_actions[owner_id] = {
                "action": "add_waiting_url",
                "started_at": time.time(),
            }
            return (
                "追加したい商品のURLを送ってください。例: https://www.amazon.co.jp/dp/XXXXXXXXXX\n"
                "取り消す場合は「キャンセル」と送ってください（3分で自動キャンセル）。"
            )
        if len(parts) < 3:
            return "使い方: 追加 商品名 URL"

        name = parts[1]
        url = parts[2]

        products.append(
            {
                "name": name,
                "url": url,
                "last_price": None,
                "min_price": None,
                "price_diff": None,
                "last_status": "未確認",
                "last_checked_at": None,
                "last_changed_at": None,
            }
        )
        user_products_map[owner_id] = products
        save_products(user_products_map)
        return f"商品を追加しました: {name}"

    if cmd == "削除":
        if len(parts) == 1:
            pending_actions[owner_id] = {
                "action": "delete_waiting_url",
                "started_at": time.time(),
            }
            return "削除したい商品のURLを送ってください。取り消す場合は「キャンセル」と送ってください（3分で自動キャンセル）。"
        if len(parts) < 2:
            return "使い方: 削除 <番号 or 商品名>"
        if not products:
            return "監視中の商品はありません。"

        key = parts[1]
        removed = None
        if key.isdigit():
            idx = int(key) - 1
            if 0 <= idx < len(products):
                removed = products.pop(idx)
        else:
            for i, product in enumerate(products):
                if str(product.get("name", "")).lower() == key.lower():
                    removed = products.pop(i)
                    break

        if not removed:
            return f"削除対象が見つかりませんでした: {key}"

        user_products_map[owner_id] = products
        save_products(user_products_map)
        return f"商品を削除しました: {removed.get('name', 'no-name')}"

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
            owner_id = get_owner_id_from_event(event)
            reply = handle_command(text, owner_id)
            if reply_token:
                send_line_reply(settings, reply_token, reply)

        return "OK", 200

    return app


def ensure_product_file() -> None:
    if PRODUCT_FILE_PATH.exists():
        return
    save_products({})


def start_background_services(settings: Settings) -> None:
    global _BACKGROUND_STARTED

    with _BACKGROUND_LOCK:
        if _BACKGROUND_STARTED:
            return

        setup_rich_menu(settings)
        ensure_product_file()

        thread = threading.Thread(
            target=monitoring_loop,
            args=(settings,),
            daemon=True,
        )
        thread.start()
        _BACKGROUND_STARTED = True


def create_app_for_gunicorn() -> Flask:
    settings = load_settings()
    global settings_cache
    settings_cache = settings
    start_background_services(settings)
    return create_app(settings)


def main() -> None:
    try:
        settings = load_settings()
        global settings_cache
        settings_cache = settings
        start_background_services(settings)

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
