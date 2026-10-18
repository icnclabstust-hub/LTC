# 長照法規／SOP 問答 RAG 系統（MVP）

依 [docs/designs/2026-09-13-ltc-rag-backend.md](docs/designs/2026-09-13-ltc-rag-backend.md) 設計文件實作，通過 GD 設計閘門（見 [assessment](docs/designs/2026-09-13-ltc-rag-backend-assessment.md)）。

## 範圍

- 法規／SOP 問答（Naive RAG：ChromaDB 檢索 → Reranker → 證據判定 → 忠實度守門員 → Ollama 生成）
- 長照據點查詢（抓取衛福部長照 ABC 據點 CSV 全量資料並在地址欄位過濾，不經生成模型；此資料集**不是**可查詢式 REST API，見下方已知缺口）
- PostgreSQL 作為文件版本與一致性真相來源

不含（留待第二階段）：Neo4j 圖譜、Agentic 多庫路由、Elasticsearch 全文檢索、Google Books。

## 執行（本機原生安裝，不使用 Docker）

於 WSL2 Ubuntu 22.04 上以套件管理員原生安裝各項服務：

```bash
sudo apt install postgresql redis-server
curl -fsSL https://ollama.com/install.sh | sh
ollama pull hf.co/tetf/Llama-3.1-TAIDE-LX-8B-Chat-GGUF:Q4_K_M   # 繁中政府用語調校版，比通用 llama3.1 更適合本專案
```

- PostgreSQL：安裝後建立 `ltc_rag` 資料庫與 `ltc` 帳號，密碼自訂並只填在 `.env` 的 `POSTGRES_DSN`（`.env` 已列入 `.gitignore`，不會進版控）
- Redis：`redis-server` 預設監聽 6379
- ChromaDB：本專案採內嵌持久化模式（`PersistentClient`），資料存於本機 `CHROMADB_PERSIST_DIR` 指定路徑，**不需另外啟動 ChromaDB 服務**
- Ollama：`ollama serve` 常駐後即可供 `generation.py` 呼叫
- Open WebUI：一律用 `bash scripts/run_open_webui.sh` 啟動（設定資料目錄並關閉遙測），不要直接執行 `open-webui serve`

### 資料存放（資料留本機、與系統分開）

所有資料放在 repo 外的資料根目錄 `/srv/ltc-data`（WSL 原生檔案系統），repo 只放程式碼：

| 資料 | 路徑 |
|---|---|
| ChromaDB 向量庫 | `/srv/ltc-data/chromadb` |
| Open WebUI 帳號、對話紀錄、知識庫向量 | PostgreSQL `openwebui` 資料庫（含 pgvector；**不使用 SQLite**） |
| Open WebUI 上傳檔、快取、`.webui_secret_key` | `/srv/ltc-data/open-webui` |
| Open WebUI 資料庫連線設定（含密碼，不進 git） | `/srv/ltc-data/open-webui.env` |
| PostgreSQL | `/var/lib/postgresql/14/main`（套件預設） |
| Ollama 模型 | `/usr/share/ollama/.ollama/models`（套件預設） |

首次建立資料根目錄：`sudo mkdir -p /srv/ltc-data && sudo chown $USER:$USER /srv/ltc-data`。
ChromaDB 與 Open WebUI 的匿名遙測皆已關閉。

```bash
cp .env.example .env   # 填入實際設定
pip install -r requirements.txt
uvicorn app.main:app --app-dir src --reload --port 8001
```

## 測試

```bash
pip install -r requirements-dev.txt
pytest tests
```

## 已知缺口（見設計文件第 9 節）

- `CODING_STYLE.md`／`CODE_REVIEW_STANDARD.md` 尚未提供完整內容，本次實作依一般通用慣例撰寫，待補齊後需對齊調整
- NLI 精篩模型尚未選型，`src/app/core/faithfulness.py` 中以介面預留（`NliChecker`），MVP 先以字元 bigram 重疊率暫代粗篩與精篩兩階段的分數計算
- 證據判定（`evidence.py`）雖回傳「足夠／不足／模糊」三態，但 MVP 路由（`routes_query.py`）目前將「不足」與「模糊」一併導向拒答，尚未實作「模糊」的獨立處理（例如轉人工），為架構圖與此 MVP 的已知簡化
- 法規資料排程同步（`sync_law_documents`）與正式 PostgreSQL migration（如 alembic）尚未撰寫；目前僅有 `scripts/dev_bootstrap.py`（標註 throwaway）用 `Base.metadata.create_all` 建表供開發測試，不是正式 migration 流程
- **embedding 模型已換成本機 Ollama 的 `bge-m3`**（`src/app/core/embedding.py`，`OllamaEmbeddingFunction`），取代 ChromaDB 預設英文語料訓練的 all-MiniLM-L6-v2
- **重大 bug 已修正**：ChromaDB 建立 collection 若未指定 `hnsw:space: cosine`，預設用 squared L2 距離（數值可到 -500 這種無意義範圍），而 `evidence.py` 的 `similarity = 1 - distance` 假設是 cosine 距離（0~2），兩者不match 會導致幾乎所有查詢都被判定「證據不足」而拒答，即使檢索排序本身是對的。已在 `retrieval.py` 新增 `get_chunk_collection()` 共用函式統一指定 cosine 距離，`load_real_law.py`／`dev_bootstrap.py` 寫入時也必須呼叫同一函式，否則向量空間不一致
- 長照據點資料集（`data.gov.tw/dataset/88270`）**不是**可查詢式 REST API，是每日更新的 CSV 全量下載（`https://ltcpap.mohw.gov.tw/publish/abc.csv`，約 11MB），`ltc_points_api.py` 已改為下載後在地址欄位做子字串比對（記憶體快取 1 小時），縣市/區在原始資料是行政區代碼而非文字，且地址一律用「臺」非「台」（已做正規化）
- 意圖分類原本把「機構」列為據點查詢關鍵字，導致「長照機構評鑑是誰負責？」這類法規問題被誤判為據點查詢，已從 `LTC_POINTS` 關鍵字移除、改列入 `LAW_SOP`
- **已用真實「長期照顧服務法」全文（72 條，`scripts/load_real_law.py` 從 law.moj.gov.tw 網頁版解析）端對端驗證**：`pytest tests`（6/6 通過）；明確法規問題（如「長照機構評鑑是誰負責？」）能正確引用 Article 39/4 生成連貫答案；明顯無關問題（如「台灣總統的任期是幾年？」）正確拒答；但也觀察到部分**確實可從語料回答**的問題（如「家庭照顧者的定義是什麼？」，Article 3 有明確定義）因相似度落在「模糊」區間或忠實度檢查誤判而被拒答／降級——這是 `evidence_similarity_threshold`／`faithfulness_citation_overlap_threshold` 尚未用 golden queries 校準的預期結果，**不建議用少數幾筆人工試問直接調參**，應等第二階段建好 `eval.golden_queries` 召回率量測機制後再系統性校準
