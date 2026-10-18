# 設計文件：Open WebUI 整合與照護員回饋量測迴圈

> GD 步驟 3 產出。對應評估表：`docs/designs/2026-09-13-ltc-feedback-loop-assessment.md`（待步驟 4 填寫）。
> 提醒：[2026-09-13-ltc-data-ingestion.md](2026-09-13-ltc-data-ingestion.md) 的步驟 4 評估表尚未完成確認，建議之後一併補齊，避免累積多份未結案的 GD 文件。

## 1. 背景與問題

依步驟 1 確認：

- **要解決什麼問題**：目前系統沒有回饋閉環，照護員實際使用時遇到的答案品質問題無從得知，也無法用來改善召回率／忠實度判斷的門檻校準（MVP 端對端測試已經發現門檻值需要用真實回饋校準，而非人工試問猜測）。
- **誰會用**：照護員在 Open WebUI 問答介面上，對回答按讚／倒讚並填寫原因，正式使用情境（非僅開發測試）。
- **不做會怎樣**：MVP 已知的門檻校準問題會持續存在，且系統無法知道「哪方面問題最多」，改善方向只能憑猜測。

## 2. 目標與非目標

**目標**：

- Open WebUI 新增一個指向我們後端的 Connection，讓照護員在 Open WebUI 實際問答時，走的是我們的 RAG pipeline（證據判定、忠實度守門員、法規引用），而不是直接對話 Ollama
- 沿用 Open WebUI **內建**的回報機制（讚／倒讚＋reason＋comment），不自行開發前端
- 排程（沿用資料擷取層設計文件已定的 Windows 工作排程器模式）定期同步 Open WebUI 的回饋資料到我們的 PostgreSQL
- 建立最小版召回率量測：`eval.golden_queries` 表 + 一支對照腳本，讓回饋資料有地方可以派上用場

**非目標（本次明確不做）**：

- 完整四段式量測（pre_fusion/post_rrf/post_rerank/post_generation）全部指標，先只做 post_generation 層級的基本統計
- 自動化告警（`eval.alerts`）機制，先人工查看同步後的資料
- 開發 Open WebUI 客製化 Pipe／Function plugin（見第 4 節方案比較）
- 忠實度線上即時攔截記錄的完整儀表板

## 3. 現況與重用

- **Open WebUI 內建 feedback 機制**：實際查看本機已安裝套件原始碼（`open_webui/models/feedbacks.py`）確認有現成的 `feedback` 資料表，含 `rating`（讚/倒讚）、`reason`、`comment`、`chat_id`、`message_id`、`tags`；且有現成 REST API（`open_webui/routers/evaluations.py`，`GET /api/v1/evaluations/feedbacks/all/export` 可匯出全部資料）。**前端 UI 與儲存都不用自己開發，直接重用**。
- **Open WebUI 支援自訂後端連線**：確認 `open_webui/routers/openai.py` 有 `OPENAI_API_BASE_URLS` 設定，可在後台新增任意 OpenAI 相容的 base URL 作為連線來源——不需要寫 Open WebUI 專屬的 plugin 程式。
- **排程同步模式**：[資料擷取層設計文件](2026-09-13-ltc-data-ingestion.md) 已定案「Windows 工作排程器 → `wsl.exe`」模式，此次同步 feedback 資料可直接沿用同一套模式，不用重新設計。

## 4. 方案比較

### 4-1｜Open WebUI 如何接我們的 RAG 後端

| | 方案 A：後端提供 OpenAI 相容 `/v1/chat/completions` | 方案 B：Open WebUI Pipe／Function plugin |
|---|---|---|
| 做法概述 | 在 `src/app` 新增一支端點，把既有 RAG pipeline（intent/retrieval/rerank/evidence/faithfulness/generation）包裝成 OpenAI Chat Completions 格式回應；Open WebUI 後台新增 Connection 指向這個 URL | 在 Open WebUI 裡寫一支 Python plugin（Pipe），內部呼叫我們既有的 `/api/query` |
| 優點 | 標準化介面，之後要換別的前端（不只 Open WebUI）也能直接用；邏輯完全在我們自己的後端，不受 Open WebUI 版本更新影響 | 可以更緊密利用 Open WebUI 特有 UI 功能 |
| 缺點／風險 | 需要多寫一層格式轉換（含來源引用如何放進 OpenAI 回應格式，見第 9 節未解決問題） | 程式碼綁定在 Open WebUI 內部、跟著它的 plugin API 版本走，Open WebUI 升級可能 breaking；需要額外學習 Open WebUI 專屬開發方式 |
| 成本 | 低（純後端） | 中（需學習外部平台特定 API） |

**選定方案：A（OpenAI 相容端點）**。理由：符合目前「Backend API 對外提供服務」的既有架構定位，OpenAI Chat Completions 是產業標準介面，未來若要換前端或串接其他工具都能直接沿用，不綁定在 Open WebUI 這一個產品上。

## 5. 系統設計

**資料流**：

```
照護員在 Open WebUI 提問
  → Open WebUI 呼叫我們後端 POST /v1/chat/completions（OpenAI 相容格式）
      → 內部轉呼叫既有 RAG pipeline（intent→retrieval→rerank→evidence→faithfulness→generation）
      → 包裝成 OpenAI 回應格式回傳（含來源引用，格式見第 9 節待定）
  → 照護員對該則訊息按讚／倒讚＋填 reason/comment
      → 存進 Open WebUI 自己的 feedback 表（無需我們處理）

Windows 工作排程器（沿用資料擷取層設計）
  → wsl.exe -e python3 -m ingestion.sync_feedback
      → 呼叫 Open WebUI GET /api/v1/evaluations/feedbacks/all/export
      → 寫入 eval.feedback_sync（我們的 PostgreSQL）
  → 人工從負評回饋中挑選、標記 relevant_chunk_ids → 寫入 eval.golden_queries
  → 對照腳本（手動或排程執行）跑 golden_queries 對目前系統，計算基本指標
```

**對外介面**：

- 新增 `POST /v1/chat/completions`（OpenAI 相容），內部沿用既有 `/api/query` pipeline 邏輯（可重構共用函式，避免兩支端點邏輯重複）
- 新增排程 CLI：`python -m ingestion.sync_feedback`，供 Windows 工作排程器呼叫

**資料模型／schema 變更**：

- 新增 `eval` schema：
  - `eval.feedback_sync`（從 Open WebUI 同步進來的原始回饋：`feedback_id`, `rating`, `reason`, `comment`, `chat_id`, `message_id`, `synced_at`）
  - `eval.golden_queries`（`query`, `relevant_chunk_ids`, `category`, `created_at`，供人工從負評中挑選後填入）

**錯誤處理與日誌**：

- Open WebUI 服務未啟動或連線失敗時，同步任務記錄錯誤並跳過，不影響其他排程任務
- OpenAI 格式轉換失敗時，回傳標準 OpenAI 錯誤格式，避免 Open WebUI 端顯示不明錯誤

## 6. 安全與隱私考量

- Feedback 的 `comment` 欄位可能包含照護員填寫的問題描述文字，一般不含個資，但同步／儲存時仍需留意不要外洩
- Open WebUI 有自己的使用者帳號系統，同步資料若要做使用者層級分析需先去識別化
- `CODE_REVIEW_STANDARD.md` 表 B／D／E 完整內容仍未取得，待補齊後對照

## 7. 測試策略

- **手動驗收**：在 Open WebUI 實際問一個問題、對回答按倒讚並填寫原因，驗證能被同步腳本正確抓到並寫入 PostgreSQL
- **單元測試**：OpenAI 格式轉換邏輯（我們的 `QueryResponse` → OpenAI Chat Completions 回應）
- **整合測試**：mock Open WebUI 的 feedback export API，測試同步腳本端對端

## 8. 部署與運維

- 延續 WSL2 原生＋Windows 工作排程器模式，不使用 Docker
- 新增一個 Windows 工作排程器任務（`sync_feedback`），與資料擷取層的其他任務並列管理

## 10. 實作結果（2026-09-14）

- ✅ `POST /v1/chat/completions`、`GET /v1/models`（`src/app/api/routes_chat_completions.py`）已實作並測試通過，來源引用以文字附註方式放在回答末尾（第 9 節「未解決問題」原本待定，已採用附註方案，非 citations 附加欄位——待確認 Open WebUI 是否支援標準 citations 格式後可再優化）
- ✅ `/api/query` 與 `/v1/chat/completions` 共用邏輯已重構至 `app/core/pipeline.py`，避免重複
- ✅ 對話紀錄已接上：`biz.conversations`／`biz.messages` 現在每次查詢都會寫入，`cited_chunk_ids` 正確記錄，已實測驗證
- ✅ `ingestion/sync_feedback.py` 已寫好並註冊 Windows 工作排程器任務（`LTC-Ingestion-SyncFeedback`，每日 03:40）；**但需要先在 Open WebUI 後台建立管理員帳號並產生 API Key（Settings > Account > API Keys），填入 `.env` 的 `OPEN_WEBUI_API_KEY` 才能實際運作**——這步是操作設定，不是程式碼，需要學生手動完成
- ✅ `/health` 已改為真的檢查 PostgreSQL／ChromaDB／Ollama 連線狀態，不再是假的固定回應

## 9. 未解決問題

- Open WebUI 回報時 `reason` 欄位的實際 UI 選項（是預設固定幾類還是自由文字）需要實際打開 Open WebUI 介面操作一次才能確認，目前只確認了後端資料結構
- OpenAI Chat Completions 回應格式裡「來源引用」（法規名稱／條號／更新日期）怎麼呈現需要進一步設計——可能放在回答文字內附註，或查看 Open WebUI 是否支援標準的 citations 附加欄位
- 「哪方面問題最多」的分類維度尚未定義（用查詢意圖分類？人工標籤？法規條文？）
- `eval.golden_queries` 對照腳本要算哪些「基本指標」尚未定義（先做「是否成功引用來源」這類最簡單的二元指標，還是要做語意相似度分數，待實作時決定）
- [資料擷取層設計文件](2026-09-13-ltc-data-ingestion.md) 的步驟 4 評估表尚未完成，建議近期一併補齊
