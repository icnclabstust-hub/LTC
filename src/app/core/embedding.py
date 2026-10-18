import httpx

from app.config import settings


class OllamaEmbeddingFunction:
    """呼叫本機 Ollama 的多語 embedding 模型（預設 bge-m3），取代 ChromaDB 預設
    英文語料訓練的 all-MiniLM-L6-v2，避免繁中法規/SOP 語意檢索品質過差（見設計文件第 9 節）。
    """

    def __init__(self) -> None:
        self._client = httpx.Client(base_url=settings.ollama_base_url, timeout=60.0)

    def __call__(self, input: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in input]

    def _embed_one(self, text: str) -> list[float]:
        response = self._client.post(
            "/api/embeddings",
            json={"model": settings.ollama_embedding_model, "prompt": text},
        )
        response.raise_for_status()
        return response.json()["embedding"]

    def name(self) -> str:
        return f"ollama-{settings.ollama_embedding_model}"
