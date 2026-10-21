"""eval/golden_queries.csv → eval.golden_queries（2026-10-19 ltc-kb-graph 5.1 節）。

以 question_id upsert；CSV 仍是人工維護的來源，資料庫一份供評測迴圈查詢與告警比對。
relevant_chunk_ids 由人工標註流程補，匯入時不覆蓋。
"""

import csv
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import GoldenQuery
from app.db.session import SessionLocal
from ingestion.common import get_logger

CSV_PATH = Path(__file__).resolve().parents[2] / "eval" / "golden_queries.csv"

logger = get_logger("golden_queries")


def load_rows(path: Path = CSV_PATH) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [
            {"question_id": r["question_id"], "query": r["query"], "category": r["category"],
             "expected_behavior": r["expected_behavior"], "expected_refusal_reason": r["expected_refusal_reason"] or None,
             "escalate": r["escalate"].strip().lower() == "true"}
            for r in csv.DictReader(f)
        ]


async def run() -> bool:
    rows = load_rows()
    async with SessionLocal() as session:
        for row in rows:
            stmt = pg_insert(GoldenQuery).values(**row)
            stmt = stmt.on_conflict_do_update(
                index_elements=[GoldenQuery.question_id],
                set_={c: getattr(stmt.excluded, c) for c in row if c != "question_id"},
            )
            await session.execute(stmt)
        await session.commit()
    logger.info(f"題庫已匯入 eval.golden_queries：{len(rows)} 題")
    return True


if __name__ == "__main__":
    import asyncio

    asyncio.run(run())
