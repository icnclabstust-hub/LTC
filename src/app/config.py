from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 帳密只放 .env，不寫死在程式碼（避免進版控）
    postgres_dsn: str = "postgresql+asyncpg://ltc@localhost:5432/ltc_rag"
    # 資料與系統分開存放，一律用絕對路徑，避免依啟動目錄不同而讀到不同（或空的）向量庫
    chromadb_persist_dir: str = "/srv/ltc-data/chromadb"
    redis_url: str = "redis://localhost:6379/0"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"
    ollama_embedding_model: str = "bge-m3"
    ltc_points_csv_url: str = "https://ltcpap.mohw.gov.tw/publish/abc.csv"
    law_api_base_url: str = "https://law.moj.gov.tw/api"
    evidence_similarity_threshold: float = 0.55
    faithfulness_citation_overlap_threshold: float = 0.4
    # 逐句檢查：有依據的句子比例達此值才算通過（2026-10-19 ltc-kb-graph 第 5.4 節）
    faithfulness_sentence_pass_ratio: float = 0.8
    open_webui_base_url: str = "http://localhost:8080"
    open_webui_api_key: str = ""
    # 小照 FE（frontend/）呼叫 /v1/* 用的服務間權杖；未設定時一律 401（fail closed），
    # 避免忘了設定就讓端點無驗證對外（見 2026-10-19 fe-be-integration 設計文件第 6 節）
    xiaozhao_api_token: str = ""
    # 長照結構化資料（2026-10-19 ltc-kb-graph）：Neo4j 為 catalog 的訂閱者，連不上時改查 PostgreSQL；
    # 設 false 則整個結構化分支關閉，回到原 RAG 路徑（回滾用）
    ltc_graph_enabled: bool = True
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
