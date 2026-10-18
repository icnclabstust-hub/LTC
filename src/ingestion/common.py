import hashlib
import logging
import sys
from datetime import date

from sqlalchemy import select

from app.db.models import Chunk, Document, SyncEvent
from app.db.session import SessionLocal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)


def get_logger(source: str) -> logging.Logger:
    return logging.getLogger(f"ingestion.{source}")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def upsert_document(
    *,
    source_type: str,
    source_name: str,
    source_url: str,
    version: str,
    full_content_hash: str,
    effective_date: date,
    chunks: list[tuple[str | None, str]],
    logger: logging.Logger,
) -> bool:
    """寫入/更新一份文件與其 chunks，並記錄 sync_events；內容未變則跳過。回傳是否有異動。"""
    async with SessionLocal() as session:
        result = await session.execute(
            select(Document).where(Document.source_url == source_url, Document.is_deleted.is_(False))
        )
        existing = result.scalar_one_or_none()

        if existing and existing.content_hash == full_content_hash:
            logger.info(f"{source_name}：內容未變，略過")
            return False

        if existing:
            doc = existing
            doc.version = version
            doc.content_hash = full_content_hash
            doc.effective_date = effective_date
            await session.execute(Chunk.__table__.delete().where(Chunk.doc_id == doc.doc_id))
        else:
            doc = Document(
                source_type=source_type,
                source_name=source_name,
                source_url=source_url,
                version=version,
                content_hash=full_content_hash,
                effective_date=effective_date,
            )
            session.add(doc)
            await session.flush()

        for article, content in chunks:
            session.add(Chunk(doc_id=doc.doc_id, content=content, article=article, embedding_ref=""))

        session.add(SyncEvent(doc_id=doc.doc_id, operation="upsert"))
        await session.commit()
        logger.info(f"{source_name}：已寫入 {len(chunks)} 個 chunk，並記錄同步事件")
        return True
