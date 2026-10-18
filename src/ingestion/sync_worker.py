"""讀取 catalog.sync_events 裡尚未處理的事件，把對應文件的 chunks upsert 到
ChromaDB，再標記事件已處理（Outbox 模式，見資料擷取層設計文件第 4-2 節）。"""

from datetime import datetime

from sqlalchemy import select

from app.core.retrieval import get_chroma_client, get_chunk_collection
from app.db.models import Chunk, Document, SyncEvent
from app.db.session import SessionLocal
from ingestion.common import get_logger

logger = get_logger("sync_worker")


async def run() -> int:
    collection = get_chunk_collection(get_chroma_client())

    async with SessionLocal() as session:
        result = await session.execute(
            select(SyncEvent).where(SyncEvent.processed_at.is_(None)).order_by(SyncEvent.created_at)
        )
        events = result.scalars().all()
        logger.info(f"待處理事件數：{len(events)}")

        processed = 0
        for event in events:
            doc = await session.get(Document, event.doc_id)
            if doc is None:
                continue

            if event.operation == "soft_delete" or doc.is_deleted:
                existing = collection.get(where={"doc_id": str(doc.doc_id)})
                if existing["ids"]:
                    collection.delete(ids=existing["ids"])
            else:
                chunk_result = await session.execute(select(Chunk).where(Chunk.doc_id == doc.doc_id))
                chunks = chunk_result.scalars().all()
                if chunks:
                    collection.upsert(
                        ids=[str(c.chunk_id) for c in chunks],
                        documents=[c.content for c in chunks],
                        metadatas=[
                            {
                                "doc_id": str(doc.doc_id),
                                "law_name": doc.source_name,
                                "article": c.article or "",
                                "source_url": doc.source_url,
                                "updated_at": doc.effective_date.isoformat(),
                            }
                            for c in chunks
                        ],
                    )

            event.processed_at = datetime.utcnow()
            processed += 1

        await session.commit()

    logger.info(f"已處理 {processed} 筆事件，同步至 ChromaDB 完成")
    return processed


if __name__ == "__main__":
    import asyncio

    asyncio.run(run())
