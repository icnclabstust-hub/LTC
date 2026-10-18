from app.core.models import Chunk


class Reranker:
    """MVP 版本：以檢索相似度排序做為 Top-N 收斂。

    Cross-encoder 精排模型尚未選型（見設計文件第 9 節未解決問題），
    介面保留於此，之後可替換 rerank() 內部實作而不影響呼叫端。
    """

    def rerank(self, query: str, chunks: list[Chunk], top_n: int = 5) -> list[Chunk]:
        return sorted(chunks, key=lambda chunk: chunk.similarity, reverse=True)[:top_n]
