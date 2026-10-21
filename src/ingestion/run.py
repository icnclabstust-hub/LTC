import argparse
import asyncio

from ingestion import (
    golden_queries,
    graph_sync,
    law_crawler,
    ltc1966_crawler,
    ltc_benefits,
    ltc_points_crawler,
    sop_crawler,
)
from ingestion.common import get_logger

logger = get_logger("run")

_CRAWLERS = {
    "law": law_crawler.run,
    "ltc_points": ltc_points_crawler.run,
    "sop": sop_crawler.run,
    # 2026-10-19 ltc-kb-graph
    "ltc_benefits": ltc_benefits.run,
    "ltc1966": ltc1966_crawler.run,
    "golden_queries": golden_queries.run,
    "graph_sync": graph_sync.run,
}


async def main(source: str) -> None:
    crawler = _CRAWLERS.get(source)
    if crawler is None:
        raise SystemExit(f"未知的來源：{source}（可選：{', '.join(_CRAWLERS)}）")
    try:
        await crawler()
    except Exception:
        logger.exception(f"{source} 擷取失敗")
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, choices=list(_CRAWLERS))
    args = parser.parse_args()
    asyncio.run(main(args.source))
