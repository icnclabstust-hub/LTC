import argparse
import asyncio

from ingestion import law_crawler, ltc_points_crawler, sop_crawler
from ingestion.common import get_logger

logger = get_logger("run")

_CRAWLERS = {
    "law": law_crawler.run,
    "ltc_points": ltc_points_crawler.run,
    "sop": sop_crawler.run,
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
