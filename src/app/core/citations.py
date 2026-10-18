"""原文引用（2026-10-19 fe-be-integration 設計文件第 5.2 節）。

quote 一律取自檢索到的 chunk 原文（catalog.chunks 同步而來），不經 LLM，確保引用的條文逐字正確；
LLM 只負責白話摘要。
"""

from app.core.models import Chunk

# LINE 單則訊息上限 5000 字；附上全文會擠掉摘要，超過時截斷並標示，citations[].quote 仍保留全文
_QUOTE_DISPLAY_LIMIT = 600
_MAX_DISPLAY_CITATIONS = 3


def build_citations(sources: list[Chunk]) -> list[dict]:
    """同一法規同一條只列一次（rerank 後可能有同條的多個 chunk）。"""
    citations: list[dict] = []
    seen: set[tuple[str, str | None]] = set()
    for chunk in sources:
        key = (chunk.law_name, chunk.article)
        if key in seen:
            continue
        seen.add(key)
        citations.append(
            {
                "law_name": chunk.law_name,
                "article": chunk.article,
                "updated_at": chunk.updated_at.isoformat(),
                "source_url": chunk.source_url,
                "quote": chunk.content,
            }
        )
    return citations


def _heading(citation: dict) -> str:
    article = f" 第{citation['article']}條" if citation["article"] else ""
    return f"【{citation['law_name']}{article}】（更新於 {citation['updated_at']}）"


def format_answer_with_quotes(answer: str, citations: list[dict], disclaimer: str = "") -> str:
    if not citations:
        return f"{answer}\n\n{disclaimer}" if disclaimer else answer

    lines = [answer, "", "引用原文："]
    for citation in citations[:_MAX_DISPLAY_CITATIONS]:
        quote = citation["quote"]
        if len(quote) > _QUOTE_DISPLAY_LIMIT:
            quote = quote[:_QUOTE_DISPLAY_LIMIT] + "…（全文見出處）"
        lines += [_heading(citation), quote, f"出處：{citation['source_url']}", ""]
    if disclaimer:
        lines.append(disclaimer)
    return "\n".join(lines).rstrip()
