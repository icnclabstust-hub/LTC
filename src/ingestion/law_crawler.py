"""全國法規資料庫沒有單一法規查詢 API（見資料擷取層設計文件第 3 節），改抓
law.moj.gov.tw 網頁版全文並依 HTML 結構（.row > .col-no + .col-data）解析條文。

涵蓋法規清單（2026-10-12 擴充，見對話紀錄需求：提高長照法規覆蓋範圍）：
長期照顧服務法本身 + 最常被照護員問到的三部子法（施行細則、申請及給付辦法、
人員訓練認證辦法）。其餘子法（機構設立管理、法人條例等）較偏機構/法人事務，
非第一線照護員日常提問重點，暫不納入，之後有需要再擴充。
"""

import html as html_lib
import re
from datetime import date

import httpx

from ingestion.common import content_hash, get_logger, upsert_document

LAWS = [
    {"pcode": "L0070040", "name": "長期照顧服務法"},
    {"pcode": "L0070043", "name": "長期照顧服務法施行細則"},
    {"pcode": "L0070059", "name": "長期照顧服務申請及給付辦法"},
    {"pcode": "L0070045", "name": "長期照顧服務人員訓練認證繼續教育及登錄辦法"},
]

_LINE_RE = re.compile(r'<div class="line-\d+[^"]*">(.*?)</div>', re.S)
_FLNO_RE = re.compile(r"flno=(\d+)")
_ARTICLE_NO_RE = re.compile(r"第\s*(\d+(?:-\d+)?)\s*條")

logger = get_logger("law_crawler")


def _strip_tags(text: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def fetch_articles(pcode: str) -> list[dict]:
    url = f"https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode={pcode}"
    response = httpx.get(url, timeout=30.0)
    response.raise_for_status()
    html_text = response.text

    rows = html_text.split('<div class="row">')[1:]
    articles = []
    for row in rows:
        flno_match = _FLNO_RE.search(row)
        article_match = _ARTICLE_NO_RE.search(row)
        if not flno_match or not article_match:
            continue
        lines = [_strip_tags(m.group(1)) for m in _LINE_RE.finditer(row)]
        content = "\n".join(line for line in lines if line)
        if not content:
            continue
        articles.append({"flno": flno_match.group(1), "article": article_match.group(1), "content": content})
    return articles


async def _run_one(pcode: str, name: str) -> bool:
    url = f"https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode={pcode}"
    articles = fetch_articles(pcode)
    logger.info(f"{name}：抓到 {len(articles)} 條條文")

    if not articles:
        logger.warning(f"{name}：解析結果為空，略過（檢查 pcode={pcode} 是否正確）")
        return False

    full_text = "\n".join(a["content"] for a in articles)
    chunks = [(a["article"], a["content"]) for a in articles]

    return await upsert_document(
        source_type="law",
        source_name=name,
        source_url=url,
        version=date.today().isoformat(),
        full_content_hash=content_hash(full_text),
        effective_date=date.today(),
        chunks=chunks,
        logger=logger,
    )


async def run() -> bool:
    any_changed = False
    for law in LAWS:
        changed = await _run_one(law["pcode"], law["name"])
        any_changed = any_changed or changed
    return any_changed


if __name__ == "__main__":
    import asyncio

    asyncio.run(run())
