# 設計文件：長照法規／SOP 問答 RAG 系統（MVP）

> GD 步驟 3 產出。對應評估表：`docs/designs/2026-09-13-ltc-rag-backend-assessment.md`。
> 篇幅依 Tier B／A 規格撰寫（每節皆有內容）。
> 目標完整架構圖：[docs/architecture/rag-architecture-full.svg](../architecture/rag-architecture-full.svg)（第二節「目標與非目標」為此圖的 MVP 子集，Neo4j／Agentic／Elasticsearch／四段式量測留待第二階段對照此圖擴充）。

## 1. 背景與問題

依步驟 1 三問確認：

- **要解決什麼問題**：現行（規劃中）RAG 架構存在多資料庫（ChromaDB／Elasticsearch／Neo4j）資料不一致、無法量測召回率、生成內容缺乏忠實度把關、回傳資料正確性不足等問題。
- **誰會用**：長照照護員。查詢對象為長照 3.0 相關法規、政府規範，以及長照操作流程 SOP。
- **不做會怎樣**：照護員可能取得未經來源佐證、已失效或錯誤的法規／SOP 資訊，若依此執行照護行為，有違反衛福部規範或延誤正確處置的風險。

**硬性要求（步驟 1 補充）**：所有回覆內容須可追溯到具法律效力的來源（政府公告法規、衛福部規範），不得由模型憑參數知識生成。

## 2. 目標與非目標

**目標（本次 MVP）**：

- 建立單一 Naive RAG 問答路徑：法規／SOP 文字問答，回覆須附來源（法規名稱／條號／更新日期）
- 長照據點查詢改走官方 API 直接查詢，不經生成模型
- PostgreSQL 作為文件版本與一致性真相來源（簡化版 catalog／biz schema）
- 基礎召回率量測（golden queries + 人工標註，不做四段式全量測）
- 忠實度守門員（粗篩＋精篩兩階段，本次設計決定技術方案）

**非目標（本次明確不做，防止範圍蔓延）**：

- Neo4j 圖譜檢索、Agentic 多庫動態路由（留待 MVP 驗證後的第二階段）
- Elasticsearch 全文檢索（先以向量檢索為主）
- Google Books 後置補充查詢（非長照領域，非必要）
- 對外服務、多使用者權限管理（僅本地／實驗室內部 demo）
- 衛生福利資料科學中心資料（機構評鑑等）的**實際系統整合**——申請流程本次一併啟動，但不納入 MVP 開發時程，待核准後再設計整合方案

## 3. 現況與重用

- 本 repo 原本僅有治理文件（CLAUDE.md 等），git repo 內**無既有程式碼可重用**；但實測發現本機（WSL2 Ubuntu 22.04）作業系統層級已預先安裝並開機自動啟動 PostgreSQL／Redis／Elasticsearch／Neo4j／Ollama，且 Ollama 已預先 pull 繁中政府用語調校版 TAIDE 模型（`hf.co/tetf/Llama-3.1-TAIDE-LX-8B-Chat-GGUF`）與多語 embedding 模型 `bge-m3`，補充了 CLAUDE.md 所述既有基礎設施的現況（詳見評估表 2026-09-13 探索性 spike 結果）。
- **長照據點資料**：[長照2.0 A、B、C 據點（含GIS座標XY)](https://data.gov.tw/dataset/88270)，政府資料開放平臺——**實測後更正**：此資料集並非可查詢式 REST API，而是每日更新的 CSV 全量下載（`https://ltcpap.mohw.gov.tw/publish/abc.csv`，約 11MB，縣市/區為行政區代碼、地址一律用「臺」非「台」），仍**不需自建爬蟲**，但需改為下載後在應用層過濾，已於 `ltc_points_api.py` 實作並加上記憶體快取。
- **法規全文**：[全國法規資料庫 Open API](https://law.moj.gov.tw/api/swagger/index.html)（法務部），提供 XML/JSON，法規庫清單每 7 天更新一次；不含所有行政規則／地方自治法規，長照相關地方性規則需另評估來源。
- **SOP 文件**：查無結構化開放資料／API，衛福部公告多為 PDF／網頁（如長照十年計畫 3.0 核定本），需新開發文件擷取元件。
- **機構評鑑等敏感資料**：衛生福利資料科學中心，屬申請制，非開放 API，本次啟動申請流程但不阻塞 MVP。
- **向量檢索 embedding 模型**：ChromaDB 預設 all-MiniLM-L6-v2 為英文語料訓練，繁中法規語意檢索品質存疑；本機已有 `bge-m3`（多語，含繁中）可透過 Ollama 呼叫，建議第二階段評估替換。

## 4. 方案比較

### 4-1｜整體開發策略

| | 方案 A：分階段 MVP | 方案 B：一次建置完整 18 節點架構 |
|---|---|---|
| 做法概述 | 先做單一 Naive RAG 路徑（法規/SOP），驗證忠實度與召回率機制有效後，再擴充 Agentic／Graph／多庫融合 | 依架構圖一次實作全部節點（RRF 融合、Neo4j、Agentic 路由、四段式量測全套） |
| 優點 | 風險低、可及早驗證核心痛點（資料不一致／忠實度）解法是否有效；符合 CLAUDE.md「先判斷再開發」精神 | 架構一步到位，不需二次改造 |
| 缺點／風險 | 部分節點（Neo4j、Agentic）需等第二階段 | 忠實度／量測機制尚未驗證就投入大量整合工作，時程與風險皆高，且落在 Tier A 需先取得老師核可才能開工，延誤更大 |
| 成本 | 低（單一路徑＋簡化 schema） | 高 |

**選定方案：A**（依步驟 1-2 確認結果，學生已選定 MVP 優先）。

### 4-2｜忠實度／安全守門員技術方案

| | 方案 A：規則式比對 | 方案 B：NLI 模型精篩 |
|---|---|---|
| 做法概述 | 檢查生成句子是否有對應引用 chunk、關鍵字／實體重疊度門檻 | 用開源 NLI（entailment）模型逐句比對生成內容與檢索 chunk 是否邏輯蘊含 |
| 優點 | 快、免額外 GPU、易除錯 | 能抓出規則式漏掉的語意改寫/過度推論 |
| 缺點／風險 | 對改寫式幻覆偵測力弱 | 需額外模型與運算資源，誤判需人工複核機制 |
| 成本 | 低 | 中（需本地跑 NLI 模型） |

**選定方案：混合（規則式粗篩 + NLI 精篩兩階段）**，對應架構圖「忠實度／安全守門員：逐句忠實度檢查（粗篩+NLI精篩）」設計。理由：粗篩先擋掉明顯無引用來源的句子（低成本、高效率），僅對通過粗篩的句子跑 NLI 精篩，兼顧成本與準確度。NLI 模型選型留待實作階段依可用 GPU 資源決定，非本次設計決議範圍。

### 4-3｜法規時效性追蹤機制

| | 方案 A：即時查詢法規 API | 方案 B：排程同步至 PostgreSQL |
|---|---|---|
| 做法概述 | 每次使用者查詢時即時呼叫全國法規資料庫 API 核對版本 | 排程（如每日）同步法規全文與版本號到 PostgreSQL，查詢時走本地版本並標示更新日期 |
| 優點 | 資料一定最新 | 回應延遲穩定、不受外部 API 可用性影響查詢體驗 |
| 缺點／風險 | 查詢延遲隨外部 API 波動、外部 API down 時無法查詢 | 更新有最長 7 天延遲（與法規庫本身更新頻率一致），需向使用者揭露「資料更新於 X 日」 |

**選定方案：B**。理由：對應架構圖 PostgreSQL 一致性層與「週期性一致性稽核」設計；法規庫本身即非即時（7 天更新），即時查詢無法換得更即時的資料，卻要承擔外部 API 不穩定的風險；排程同步並於回覆中標示資料時效，更符合「揭露資料時效性」的合規精神。

## 5. 系統設計（MVP 範圍）

**資料流**：

```
照護員提問
  → Query 前處理（意圖辨識：法規查詢 / SOP查詢 / 據點查詢）
      ├─ 據點查詢 → 直接呼叫 data.gov.tw 長照據點 API（含快取）→ 回應
      └─ 法規/SOP查詢 → ChromaDB 向量檢索 → Reranker → 證據判定
            ├─ 證據不足 → 拒答（標示「查無具法源效力之資料，請洽 1966 專線或原始公告」）
            └─ 證據足夠 → 忠實度守門員（粗篩→NLI精篩）→ Ollama 生成（附法源引用＋更新日期）→ 回應
```

**對外介面（MVP）**：

- `POST /api/query` `{query, session_id}` → `{answer, sources:[{law_name, article, updated_at, source_url}], confidence, disclaimer}`
- `GET /api/ltc-points?city=&district=` → 代理 data.gov.tw 據點 API 結果（含快取）

**資料模型／schema 變更**：

- PostgreSQL catalog schema（簡化版）：`documents(doc_id, source_type[law|sop], source_url, version, content_hash, effective_date, is_deleted)`、`chunks(chunk_id, doc_id, content, embedding_ref)`
- PostgreSQL biz schema：`conversations`、`messages(cited_chunk_ids ...)`（供未來召回率評測取樣，個資需去識別化，見第 6 節）

**錯誤處理與日誌**：

- 法規 API／據點 API 逾時或失敗 → 回覆明確標示「資料來源暫時無法連線，請勿以此作為法規依據」，不得無來源編造
- 忠實度守門員判定不通過 → 降級為「僅顯示檢索到的原文出處，不做 AI 摘要」或直接拒答

## 6. 安全與隱私考量

> 註：`CODE_REVIEW_STANDARD.md` 表 B／D／E 內容目前未提供給 AI，以下為一般性考量，待取得該文件後需逐項對照補充。

- 所有法規／SOP 回覆須可追溯來源；模型不得以參數知識回答法規類問題（無檢索佐證即拒答）
- 對外資訊需標示資料更新日期，明確揭露時效性
- 對話紀錄（`messages`）若含照護對象個資，需去識別化或加密儲存
- 外部政府 API 為系統可用性的相依風險點，需有 fallback 與明確錯誤訊息（見第 5 節）

## 7. 測試策略

- **單元測試**：意圖分類（法規/SOP/據點）、chunk 擷取、規則式忠實度粗篩
- **整合測試**：端到端問答流程；法規 API／據點 API 以 mock 測試異常情境（逾時、格式變更）
- **驗收測試**：建立 20–30 題 golden queries（涵蓋長照法規常見問題），人工標註 `relevant_chunk_ids`，量測召回率與答案正確性

## 8. 部署與運維

- 本地 WSL2 + Ubuntu 22.04，PostgreSQL／Redis／Ollama 皆原生安裝常駐（不使用 Docker），ChromaDB 採內嵌持久化模式
- 監控：MVP 階段以日誌 + 人工查看 `eval.alerts` 為主，不建自動通知
- 回滾：資料庫以 migration 版本控制，程式碼以 git tag 標記穩定版本，發現問題可 revert 至前一版本

## 9. 未解決問題

- 衛生福利資料科學中心資料申請流程時程未知，需另外追蹤，不納入 MVP 時程
- `CODING_STYLE.md`／`CODE_REVIEW_STANDARD.md` 完整內容尚未提供，安全／測試章節需在取得後對齊補充
- repo 是否有其他既有可重用元件，待學生／指導老師確認（機器層級既有服務已於探索性 spike 確認，見第 3 節）
- Neo4j 圖譜資料的實體／關係抽取來源尚無方案，留待第二階段設計
- **2026-09-13 端對端驗證結果（含真實語料）**：改用本機 `bge-m3` embedding，並改用真實「長期照顧服務法」全文（72 條，`scripts/load_real_law.py`）取代單一示範 chunk 測試。過程中發現並修正一個會讓幾乎所有查詢都誤判為「證據不足」的重大 bug：ChromaDB collection 未指定 `hnsw:space: cosine` 時預設用 squared L2 距離，與 `evidence.py` 假設的 cosine 距離（0~2）完全對不上（實測出現 -500 這類無意義相似度值），已修正並集中到 `retrieval.get_chunk_collection()` 共用。修正後：明確法規問題能正確引用條文生成答案、明顯無關問題正確拒答；但部分語料中確實有答案的問題仍因相似度落在「模糊」區間被拒答，屬於 `evidence_similarity_threshold`／忠實度門檻尚未用 golden queries 校準的預期狀態，不宜用少數人工試問直接調參，留待下方召回率量測機制建立後系統性校準
- 正式 PostgreSQL migration（如 alembic）尚未建立，目前僅有開發用 bootstrap 腳本（`Base.metadata.create_all`），非正式流程
- 意圖分類（`intent.py`）原本的「機構」關鍵字會把法規類問題誤判為據點查詢，已修正關鍵字歸類，但仍是粗略的關鍵字啟發式，非 ML 分類器
