from datetime import date

import chromadb

from app.config import settings
from app.core.embedding import OllamaEmbeddingFunction
from app.core.models import Chunk

COLLECTION_NAME = "ltc_law_sop_chunks"


def get_chroma_client() -> chromadb.ClientAPI:
    """統一建立 ChromaDB client：資料路徑由設定帶入（與系統程式分開），並關閉匿名遙測避免資料相關事件外送。"""
    return chromadb.PersistentClient(
        path=settings.chromadb_persist_dir,
        settings=chromadb.config.Settings(anonymized_telemetry=False),
    )


def get_chunk_collection(client: chromadb.ClientAPI):
    """建立/取得 chunk collection，統一指定 cosine 距離空間。

    ChromaDB 預設用 squared L2（範圍不是 0-2），若不指定會讓「similarity = 1 - distance」
    這類假設 cosine 距離的計算完全失準（值可能是 -500 這種無意義的數字）。
    三處建立 collection 的程式（此處查詢路徑、load_real_law.py、dev_bootstrap.py）
    都要呼叫這個函式，避免各自建立時設定不一致。
    """
    return client.get_or_create_collection(
        COLLECTION_NAME,
        embedding_function=OllamaEmbeddingFunction(),
        metadata={"hnsw:space": "cosine"},
    )


class ChunkRetriever:
    def __init__(self) -> None:
        self._client = get_chroma_client()
        self._collection = get_chunk_collection(self._client)

    def search(self, query: str, top_k: int = 20) -> list[Chunk]:
        result = self._collection.query(query_texts=[query], n_results=top_k)
        chunks: list[Chunk] = []
        ids = result.get("ids", [[]])[0]
        documents = result.get("documents", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]
        for chunk_id, content, metadata, distance in zip(ids, documents, metadatas, distances):
            if metadata is None:
                continue
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    doc_id=metadata.get("doc_id", ""),
                    content=content,
                    law_name=metadata.get("law_name", ""),
                    article=metadata.get("article"),
                    source_url=metadata.get("source_url", ""),
                    updated_at=date.fromisoformat(metadata["updated_at"]) if metadata.get("updated_at") else date.today(),
                    similarity=1.0 - distance,
                )
            )
        return chunks
