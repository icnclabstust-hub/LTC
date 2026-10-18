"""衛福部 1966 長照專區「照顧服務相關指引」欄目：僅抓此欄目列出的項目（見設計文件
第 2 節目標範圍），詳細頁本身無全文，需下載 PDF 附件並擷取文字。"""

import re
from datetime import date, datetime
from io import BytesIO

import httpx
import pdfplumber

from ingestion.common import content_hash, get_logger, upsert_document

LIST_URL = "https://1966.gov.tw/LTC/lp-7565-207.html"

_ITEM_RE = re.compile(
    r'<a href="(https://1966\.gov\.tw/LTC/cp-7565-\d+-207\.html)" title="([^"]+)">.*?</a><time>([\d-]+)</time>',
    re.S,
)
_PDF_LINK_RE = re.compile(r'<a[^>]+href="(https://www\.mohw\.gov\.tw/dl-[^"]+)"[^>]*>([^<]*)</a>')

logger = get_logger("sop_crawler")


def _roc_date_to_iso(roc_date: str) -> date:
    year, month, day = (int(part) for part in roc_date.split("-"))
    return date(year + 1911, month, day)


def list_items() -> list[dict]:
    response = httpx.get(LIST_URL, timeout=30.0)
    response.raise_for_status()
    items = []
    for match in _ITEM_RE.finditer(response.text):
        items.append({"url": match.group(1), "title": match.group(2), "date": match.group(3)})
    return items


def _pick_guideline_pdf(detail_html: str) -> str | None:
    candidates = [(url, text) for url, text in _PDF_LINK_RE.findall(detail_html)]
    for url, text in candidates:
        if "號函" not in text:
            return url
    return candidates[0][0] if candidates else None


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]
    return "\n".join(pages).strip()


_MAX_CHUNK_CHARS = 1500


def _split_into_chunks(text: str) -> list[str]:
    """PDF 全文可能長達數萬字（超過 embedding 模型單次可處理長度，見實測），
    依段落切成 <= _MAX_CHUNK_CHARS 的區塊，段落本身過長則再硬切。"""
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        if len(paragraph) > _MAX_CHUNK_CHARS:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            for i in range(0, len(paragraph), _MAX_CHUNK_CHARS):
                chunks.append(paragraph[i : i + _MAX_CHUNK_CHARS])
            continue
        if len(buffer) + len(paragraph) + 1 > _MAX_CHUNK_CHARS:
            chunks.append(buffer)
            buffer = paragraph
        else:
            buffer = f"{buffer}\n{paragraph}" if buffer else paragraph
    if buffer:
        chunks.append(buffer)
    return chunks


async def run() -> bool:
    items = list_items()
    logger.info(f"抓到 {len(items)} 筆 SOP 指引項目")

    any_changed = False
    for item in items:
        detail_response = httpx.get(item["url"], timeout=30.0)
        detail_response.raise_for_status()
        pdf_url = _pick_guideline_pdf(detail_response.text)
        if not pdf_url:
            logger.warning(f"{item['title']}：找不到 PDF 附件連結，略過")
            continue

        pdf_response = httpx.get(pdf_url, timeout=60.0)
        pdf_response.raise_for_status()
        text = _extract_pdf_text(pdf_response.content)
        if not text:
            logger.warning(f"{item['title']}：PDF 文字擷取結果為空，略過")
            continue

        pieces = _split_into_chunks(text)
        changed = await upsert_document(
            source_type="sop",
            source_name=item["title"],
            source_url=item["url"],
            version=item["date"],
            full_content_hash=content_hash(text),
            effective_date=_roc_date_to_iso(item["date"]),
            chunks=[(f"段{i + 1}/{len(pieces)}", piece) for i, piece in enumerate(pieces)],
            logger=logger,
        )
        any_changed = any_changed or changed

    return any_changed


if __name__ == "__main__":
    import asyncio

    asyncio.run(run())
