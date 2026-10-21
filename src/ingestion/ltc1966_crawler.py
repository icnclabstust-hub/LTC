"""衛福部 1966 長照專區說明頁 → 向量知識庫（2026-10-19 ltc-kb-graph 第 2 節）。

申請流程、服務方式等白話說明只在 1966 網站，不在法規條文裡。網站採政府資料開放授權條款第 1 版，
回答時須註明出處；文件名稱一律加「1966長照專區」前綴，與法規條文區分（說明文字，非法源）。
"""

import html
import re
from datetime import date

import httpx

from ingestion.common import content_hash, get_logger, upsert_document

BASE = "https://1966.gov.tw/LTC/"
# 直接收錄的單頁
PAGES = (
    "cp-6533-70777-207.html",  # 申請長照服務
    "cp-6451-69935-207.html",  # 照顧服務
    "cp-6451-70123-207.html",  # 專業服務
    "cp-6452-69937-207.html",  # 交通接送服務
    "cp-6453-69940-207.html",  # 輔具及居家無障礙環境改善
    "np-6454-207.html",        # 喘息服務
    "cp-6460-69943-207.html",  # 聘僱外看家庭可使用長照服務
)
# 收錄列表下的所有文章：家庭照顧者支持服務、失智照護、住宿式機構服務、醫療銜接長照
LIST_PAGES = ("lp-6455-207.html", "lp-6456-207.html", "lp-6457-207.html", "lp-6458-207.html")

_MAX_CHUNK_CHARS = 800
_UPDATED = re.compile(r"更新時間[：:]\s*(\d{2,3})-(\d{1,2})-(\d{1,2})")

logger = get_logger("ltc1966_crawler")


def _get(client: httpx.Client, path: str) -> str:
    response = client.get(BASE + path)
    response.raise_for_status()
    return response.text


def list_articles(list_page: str, list_html: str) -> list[str]:
    """列表頁裡屬於同一分類的文章（cp-<分類編號>-<文章編號>-207.html）。"""
    category = re.match(r"lp-(\d+)-207", list_page).group(1)
    return sorted(set(re.findall(rf"cp-{category}-\d+-207\.html", list_html)))


def page_text(raw_html: str) -> tuple[str, list[str], date | None]:
    """回傳（標題, 內文段落, 更新日期）；只取「回上一頁」與「收合」之間的內文。"""
    start = raw_html.find("回上一頁")
    end = raw_html.find("收合", start)
    body = raw_html[start:end if end > 0 else None]
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", body, flags=re.S)
    body = re.sub(r"</(p|div|li|tr|h\d|table)>|<br\s*/?>", "\n", body)
    body = re.sub(r"</t[dh]>", "｜", body)
    lines = [l.strip() for l in html.unescape(re.sub(r"<[^>]+>", "", body)).splitlines()]
    lines = [l for l in lines if l and not l.startswith(("回上一頁", "友善列印", "資料來源", "建檔日期"))]
    updated = None
    for line in lines:
        m = _UPDATED.search(line)
        if m:
            updated = date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
    lines = [l for l in lines if not _UPDATED.search(l)]
    title = lines[0] if lines else ""
    return title, lines[1:], updated


def split_chunks(paragraphs: list[str]) -> list[str]:
    chunks, buffer = [], ""
    for paragraph in paragraphs:
        if buffer and len(buffer) + len(paragraph) + 1 > _MAX_CHUNK_CHARS:
            chunks.append(buffer)
            buffer = ""
        buffer = f"{buffer}\n{paragraph}" if buffer else paragraph
        while len(buffer) > _MAX_CHUNK_CHARS:
            chunks.append(buffer[:_MAX_CHUNK_CHARS])
            buffer = buffer[_MAX_CHUNK_CHARS:]
    if buffer:
        chunks.append(buffer)
    return chunks


async def run() -> bool:
    any_changed = False
    with httpx.Client(timeout=30.0, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True) as client:
        pages = list(PAGES)
        for list_page in LIST_PAGES:
            pages += [p for p in list_articles(list_page, _get(client, list_page)) if p not in pages]
        logger.info(f"1966 長照專區共 {len(pages)} 頁")

        for page in pages:
            title, paragraphs, updated = page_text(_get(client, page))
            if not title or not paragraphs:
                logger.warning(f"{page}：解析不到內文，略過")
                continue
            pieces = split_chunks(paragraphs)
            full_text = "\n".join(paragraphs)
            changed = await upsert_document(
                source_type="ltc1966",
                source_name=f"1966長照專區：{title}",
                source_url=BASE + page,
                version=(updated or date.today()).isoformat(),
                full_content_hash=content_hash(full_text),
                effective_date=updated or date.today(),
                chunks=[(f"段{i + 1}/{len(pieces)}", piece) for i, piece in enumerate(pieces)],
                logger=logger,
            )
            any_changed = any_changed or changed
    return any_changed


if __name__ == "__main__":
    import asyncio

    asyncio.run(run())
