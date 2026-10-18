"""建立／刪除三種身分的聊天室選單（Rich Menu）。手動執行一次即可：

    python -m xiaozhao.richmenu setup    # 建立選單，id 存到 xiaozhao_data/richmenus.json
    python -m xiaozhao.richmenu teardown # 刪除本工具建立的選單（回滾用，評估表第七節）
"""

import argparse
import io
import json
import logging
from pathlib import Path

from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    MessagingApiBlob,
    PostbackAction,
    RichMenuArea,
    RichMenuBounds,
    RichMenuRequest,
    RichMenuSize,
    URIAction,
)

from .config import load_settings

logger = logging.getLogger(__name__)

WIDTH, HEIGHT = 2500, 843
FONT_CANDIDATES = ("C:/Windows/Fonts/msjhbd.ttc", "C:/Windows/Fonts/msjh.ttc")

# 身分 → [(按鈕文字, 動作)]；動作 "page:x" 開啟 LIFF 對應分頁，"postback:x" 由 Bot 回覆
MENUS = {
    "caregiver": [("錄音模式", "page:record"), ("今日狀況回報", "page:report"), ("使用說明", "postback:help")],
    "supervisor": [("督導頁面", "page:supervisor"), ("使用說明", "postback:help")],
    "admin": [("管理員頁面", "page:admin"), ("督導頁面", "page:supervisor"), ("錄音模式", "page:record"),
              ("使用說明", "postback:help")],
}
COLORS = ("#2E7D6B", "#3A6EA5", "#7A5C99", "#8A6D3B")


def render_image(labels: list[str]) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    font = next((ImageFont.truetype(p, 110) for p in FONT_CANDIDATES if Path(p).exists()), ImageFont.load_default())
    cell = WIDTH // len(labels)
    for i, label in enumerate(labels):
        x0 = i * cell
        draw.rectangle([x0 + 12, 12, x0 + cell - 12, HEIGHT - 12], fill=COLORS[i % len(COLORS)])
        box = draw.textbbox((0, 0), label, font=font)
        draw.text((x0 + (cell - (box[2] - box[0])) / 2, (HEIGHT - (box[3] - box[1])) / 2 - box[1]),
                  label, font=font, fill="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _action(spec: str, label: str, liff_url: str):
    kind, value = spec.split(":", 1)
    if kind == "page":
        return URIAction(label=label, uri=f"{liff_url}?page={value}")
    return PostbackAction(label=label, data=value, display_text=label)


def setup() -> None:
    settings = load_settings()
    path = settings.data_dir / "richmenus.json"
    if path.exists():
        raise SystemExit(f"{path} 已存在；要重建請先執行 teardown")
    configuration = Configuration(access_token=settings.channel_access_token)
    created = {}
    with ApiClient(configuration) as client:
        api, blob = MessagingApi(client), MessagingApiBlob(client)
        try:
            _create_all(api, blob, settings.liff_url, created)
        except Exception:
            # 中途失敗（例如連不上 api-data.line.me 上傳圖片）就刪掉已建立的選單，避免留下沒有圖片的選單
            for menu_id in created.values():
                api.delete_rich_menu(menu_id)
            raise
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(created, indent=2), encoding="utf-8")
    relink(settings, created)


def relink(settings, menus: dict[str, str]) -> None:
    """重建選單後，把已綁定的使用者與管理員重新連到對應身分的選單（舊選單刪除後連結會失效）。"""
    from .db import Database

    db = Database(settings.db_path)
    users = {r["line_user_id"]: r["role"] for r in db.all(
        "SELECT line_user_id, role FROM accounts WHERE status = 'active' AND line_user_id IS NOT NULL")}
    db.close()
    users.update({user_id: "admin" for user_id in settings.admin_user_ids})
    with ApiClient(Configuration(access_token=settings.channel_access_token)) as client:
        api = MessagingApi(client)
        for user_id, role in users.items():
            api.link_rich_menu_id_to_user(user_id, menus[role])
    logger.info("已重新連結 %d 位使用者的選單", len(users))


def _create_all(api: MessagingApi, blob: MessagingApiBlob, liff_url: str, created: dict[str, str]) -> None:
    for role, buttons in MENUS.items():
        cell = WIDTH // len(buttons)
        request = RichMenuRequest(
            size=RichMenuSize(width=WIDTH, height=HEIGHT), selected=True,
            name=f"xiaozhao-{role}", chat_bar_text="小照選單",
            areas=[
                RichMenuArea(bounds=RichMenuBounds(x=i * cell, y=0, width=cell, height=HEIGHT),
                             action=_action(spec, label, liff_url))
                for i, (label, spec) in enumerate(buttons)
            ],
        )
        menu_id = api.create_rich_menu(request).rich_menu_id
        created[role] = menu_id
        blob.set_rich_menu_image(
            menu_id, body=bytearray(render_image([b[0] for b in buttons])),
            _headers={"Content-Type": "image/png"},
        )
        logger.info("已建立 %s 選單：%s", role, menu_id)


def teardown() -> None:
    settings = load_settings()
    path = settings.data_dir / "richmenus.json"
    if not path.exists():
        raise SystemExit("沒有本工具建立的選單紀錄")
    configuration = Configuration(access_token=settings.channel_access_token)
    with ApiClient(configuration) as client:
        api = MessagingApi(client)
        for role, menu_id in json.loads(path.read_text(encoding="utf-8")).items():
            api.delete_rich_menu(menu_id)
            logger.info("已刪除 %s 選單：%s", role, menu_id)
    path.unlink()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["setup", "teardown"])
    args = parser.parse_args()
    setup() if args.command == "setup" else teardown()


if __name__ == "__main__":
    main()
