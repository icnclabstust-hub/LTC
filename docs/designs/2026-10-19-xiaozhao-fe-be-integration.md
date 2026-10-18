# 小照前後端合併與串接（P0＋P1）設計文件

> GD 步驟 3 產出。對應評估表：`docs/designs/2026-10-19-xiaozhao-fe-be-integration-assessment.md`
> 依據：〈補上三個端點，讓小照真正串起來〉整合報告（含 2026-10-19 需求決議）、`frontend/串接說明.md`、測試題庫 `eval/golden_queries.csv`。

## 1. 背景與問題

FE（`frontend/`，LINE bot＋LIFF）的 `HttpBackend` 呼叫 `POST /v1/query`、`POST /v1/scene`、`GET /v1/requests/{id}`，BE 一支都沒有，每題都 404 再被轉成「轉督導」：前後端實際接通的路徑是 0。另外兩邊各有一套 LINE webhook、綁定流程、喚醒詞判斷；BE 拒答只是把拒答句放進 `answer`，FE 無法分辨，拒答結果還會進快取。

## 2. 目標與非目標

- 目標（P0 接通）：BE 實作三支端點＋Bearer 驗證；LINE 只留 FE `/callback` 一個入口；逾時對齊。
- 目標（P1 身分與拒答）：
  - 拒答改為結構化 `refused`＋`refusal_reason`＋`escalate`，拒答不寫快取；
  - 敏感（醫療、用藥、個案安全）規則；
  - 原文引用 `citations[].quote` 由程式從資料庫內容直接放入，不經 LLM；
  - 文字訊息免喚醒詞（開頭有「小照你好／你好小照」則去掉）；
  - 緊急關鍵字固定回覆＋緊急督導請求＋通知督導；
  - 「轉交給督導」「直接問督導」；
  - 身分以 BE PostgreSQL 為準（建帳號、綁定碼、核銷、查詢、停用），FE SQLite 改成快取；
  - 送 LLM 與寫入資料庫前遮罩身分證字號、電話；照護員以匿名代號（`CG-0001`）表示；
  - 題庫進版控，作為規則判斷的回歸測試。
- 非目標（這次不做）：
  - 工單狀態機、SLA、Open WebUI Pipe／Action、「我的問題」postback（P2）；
  - pgvector、法規 JSON 同步、Open WebUI 上傳入庫、Neo4j（P3）；
  - 語音訊息與錄音流程的緊急關鍵字（P4；錄音流程輪外片段不轉文字是既有隱私設計，要改須另走 GD）；
  - `false_premise` 判斷（需要 LLM judge，P3）；
  - 姓名、地址遮罩（規則式做不準，P3 評估 NER）。

## 3. 現況與重用

| 既有元件 | 處理 |
|---|---|
| BE `core/pipeline.py`、`retrieval`、`rerank`、`evidence`、`generation`、`faithfulness` | 重用；pipeline 改回傳結構化狀態 |
| BE `db/binding.py`、`core/rate_limit.py` | 重用，改由新的身分 API 呼叫 |
| BE `api/routes_chat_completions.py`（Open WebUI） | 保留，改用新的結果欄位格式化 |
| BE `api/routes_line_webhook.py`、`core/line_client.py`、`core/line_security.py`、`core/wake_word.py` | **刪除**：LINE 統一由 FE 接收；喚醒詞錯字變體併入 FE |
| BE `api/routes_query.py`（`/api/query`） | **刪除**：無呼叫端、無驗證，由 `/v1/query` 取代 |
| BE `biz.inbox_requests`／`outbox_events` 表 | 保留（P2 通知督導要用）；只有 webhook 用到的程式隨 webhook 刪除，表不動 |
| FE `backend.py` `HttpBackend`、`tools/fake_backend.py` | 重用；讀 `escalate`、`refusal_reason`，送真實 `user_id`／`role` |
| FE `services.py` 帳號與綁定 | 保留本機模式（mock、測試）；http 模式改呼叫 BE |

## 4. 方案比較

### 4.1 合併方式

| | 方案 A：monorepo，兩個服務分開跑 | 方案 B：合成同一個 FastAPI 程式 |
|---|---|---|
| 做法 | BE 留在根目錄，FE 放 `frontend/`，各自的 venv 與 port | FE 路由掛進 BE app |
| 優點 | 改動最小；FE 用 Python 3.14＋torch，BE 用 3.12＋chromadb，依賴不衝突；契約測試放同一處 | 少一個行程 |
| 缺點 | 要跑兩個服務 | FE 是同步 SQLite＋sherpa-onnx，BE 是 async＋asyncpg，依賴版本互斥（fastapi 0.115 vs 0.143）；錄音推論會卡住 RAG |
| 成本 | 低 | 高，且要重寫大量 FE 測試 |

**選定 A**（學生 10/19 回覆第 1 題）。

### 4.2 請求狀態存放

| | 方案 A：新表 `biz.xiaozhao_requests` | 方案 B：擴充 `biz.inbox_requests` |
|---|---|---|
| 優點 | 欄位語意清楚（狀態、答案、引用、拒答原因），以 FE 的 `request_id` 為主鍵天然冪等 | 不加表 |
| 缺點 | 多一張表 | `inbox_requests` 是「外部事件去重」，混入答案與狀態會讓 P2 的 outbox 語意變模糊；主鍵是 UUID，FE 送的是 32 字元 hex |

**選定 A。**

### 4.3 緊急關鍵字放哪一邊

| | 方案 A：FE | 方案 B：BE |
|---|---|---|
| 優點 | 在呼叫 BE 之前就處理，BE 或 Ollama 掛掉時仍能回安全指引；語音流程（P4）也在 FE | 單一實作 |
| 缺點 | Open WebUI 路徑沒有（督導用，不需要） | BE 掛掉時照護員收不到急救指引 |

**選定 A。** 敏感（非緊急）的醫療用藥判斷放 BE，因為 Open WebUI 也要套用。

### 4.4 LINE 回覆方式（免費方案每月 200 則 push）

FE 原本超過 3 秒就回「還在查詢」、答案好了再 push；BE 實測 9–33 秒，幾乎每題都會耗一則 push。改為先呼叫 LINE loading animation（不計費），最多等 `LINE_REPLY_WAIT_SECONDS`（預設 50 秒）再用 reply 回答；超過才退回「還在查詢＋push」。reply token 的有效時間 LINE 沒有公開，50 秒是保守值，要實測。

## 5. 系統設計

### 5.1 資料流

```
LINE ─► FE /callback ─► 身分（SQLite 快取 → miss 時 GET BE /v1/identities/line/{id}）
                      ─► 去掉開頭喚醒詞
                      ─► 緊急關鍵字？──是─► 固定回覆＋緊急督導請求＋push 督導（不呼叫 BE）
                      ─► 轉交給督導？──是─► 建督導請求（user_requested）
                      ─► POST BE /v1/query（202）─► 背景執行 pipeline
                      ─► 輪詢 GET /v1/requests/{id}
                           answered               → 回答（含原文引用）
                           refused, escalate=true → 安全提示＋建督導請求
                           refused, escalate=false→ 回拒答說明（out_of_scope 附「轉交給督導」按鈕）
```

BE pipeline 判斷順序（由便宜到昂貴）：遮罩 → 意圖（身分／據點／法規）→ 敏感規則 → 模糊問題規則 → 快取 → 檢索＋rerank → 證據門檻 → 生成 → 忠實度。

| 結果 | status | refusal_reason | escalate | 寫快取 |
|---|---|---|---|---|
| 證據足夠且忠實度通過 | answered | — | 否 | 是 |
| 醫療、用藥、個案安全 | refused | sensitive | 是 | 否 |
| 太短、只有代名詞（如「他可以用嗎？」） | refused | ambiguous | 否（回追問） | 否 |
| 證據不足且不含長照領域詞 | refused | out_of_scope | 否 | 否 |
| 證據不足但含長照領域詞 | refused | kb_missing | 是 | 否 |
| 證據模糊區，或忠實度未通過 | refused | low_confidence | 是（附引用給督導） | 否 |

證據模糊區原本「回答＋僅供參考」，依決議第 7 題「不確定都轉督導定案」改為拒答，並省下一次 LLM 生成。

### 5.2 對外介面（全部需 `Authorization: Bearer <XIAOZHAO_API_TOKEN>`；未設定權杖時一律 401，fail closed）

| 方法 | 路徑 | 說明 |
|---|---|---|
| POST | `/v1/query` | body 依 FE 合約（`request_id`、`text`、`role`、`user_id`、`client_intent`…，多餘欄位可忽略）。回 202 `{request_id, status:"pending"}`；同一 `request_id` 重送回既有狀態 |
| POST | `/v1/scene` | 一輪的統計；**不存逐字稿**（`utterances[].text` 丟棄，串接說明第 4 節第 4 題未定案前從嚴）。回 202 |
| GET | `/v1/requests/{id}` | `{request_id, status, answer_full, answer_spoken, citations[], refusal_reason, escalate, help_request_id, human_answer, confidence}` |
| POST | `/v1/users` | 建使用者 `{name, role}` → `{user_id, role, pseudonym}` |
| POST | `/v1/users/{user_id}/binding-codes` | 產生 6 位數綁定碼（10 分鐘） |
| POST | `/v1/bindings` | `{code, line_user_id}` 核銷；每個 LINE 帳號 10 分鐘 5 次 |
| GET | `/v1/identities/line/{line_user_id}` | `{user_id, role, pseudonym, name}`；查無 404 |
| POST | `/v1/users/{user_id}/disable` | 解除該使用者所有通道綁定 |

`citations[]`：`{law_name, article, updated_at, source_url, quote}`，`quote` 是檢索到的 chunk 原文（來自 `catalog.chunks`，不經 LLM）。`answer_full` 附上原文，單段超過 600 字截斷並標示「…（全文見出處）」；`citations[].quote` 保留全文。

### 5.3 schema 變更（migration `a7c1d2e3f4b5`）

- 新表 `biz.xiaozhao_requests`：`request_id varchar(64) PK`、`kind`、`status`、`channel`、`user_id uuid NULL FK biz.users`、`role`、`query_text`（遮罩後）、`payload jsonb`（去掉姓名與逐字稿）、`answer_full`、`answer_spoken`、`citations jsonb`、`refusal_reason`、`escalate`、`confidence`、`help_request_id`、`human_answer`、`error`、`created_at`、`updated_at`、`completed_at`。
- `biz.users` 加 `pseudonym varchar(16) UNIQUE NULL`；新 sequence `biz.user_pseudonym_seq`。

FE SQLite（沿用既有 `_add_column` 補欄位）：`accounts.be_user_id`、`accounts.pseudonym`；`supervisor_requests.priority`（normal／urgent）、`supervisor_requests.category`。

### 5.4 錯誤處理與日誌

- 背景 pipeline 例外 → `status=error`，FE 轉督導（既有行為）。BE 重啟時還在 pending 的請求不會續跑，FE 逾時後轉督導；P2 再補重試。
- FE 預設逾時 30 → 120 秒（與 BE 生成逾時 120 秒對齊）。
- 日誌不印權杖、不印遮罩前原文。

## 6. 安全與隱私

- 新增端點全部 Bearer 驗證，`secrets.compare_digest` 比對；BE 只供 FE 呼叫，不對外開 port（本機 docker 網路或 localhost）。
- 遮罩：身分證字號、手機與市話在 BE pipeline 第一步換成「[身分證]」「[電話]」，快取鍵、資料庫、LLM 都只看到遮罩後文字。
- BE 不再收照護員姓名：`caregiver_name` 欄位在寫入前移除；以 `user_id`＋`pseudonym` 識別。
- 緊急固定回覆的文字**未經督導或護理人員審定**，程式內標註，上線前必須審定。
- 仍未處理：`/v1/chat/completions`（Open WebUI）沒有驗證，維持「不可對外」限制。

## 7. 測試策略

- BE：TestClient＋假 pipeline 測三支端點（202、冪等、輪詢、401）；pipeline 判斷表（5.1）以假 retriever／generator 測；身分 API 走本機 PostgreSQL（沿用 `test_binding.py` 的作法）。
- FE：`HttpBackend` 對 refused／escalate 的解讀；`line_service` 免喚醒詞、緊急、轉交督導；身分 http 模式以 `httpx.MockTransport` 測。
- 題庫回歸：`eval/golden_queries.csv` 中「緊急關鍵字」「緊急關鍵字（不該觸發）」由 FE 測、「敏感與醫療」「需追問」由 BE 規則測；規則命中率寫進測試輸出，個別已知誤判以 xfail 標示，不靜默略過。
- 契約：BE 回應欄位與 FE `HttpBackend` 讀取的欄位，以 FE 測試對 BE 的回應範例驗證。

## 8. 部署與運維（本機）

1. BE：`alembic upgrade head`；`.env` 設 `XIAOZHAO_API_TOKEN`；`uvicorn app.main:app --port 8001`。
2. FE：`frontend/.env` 設 `BACKEND_MODE=http`、`BACKEND_URL=http://localhost:8001`、`BACKEND_TOKEN`（與 BE 相同）；`python -m xiaozhao`（port 8010）。
3. LINE Developers Console → Messaging API → Webhook URL 設 `https://<ngrok 網域>/callback`（只設這一個）。
4. 回滾：FE 設 `BACKEND_MODE=mock` 即與 BE 脫鉤；BE `alembic downgrade be2c7233d3e9`；刪除的檔案可從 git 歷史還原。

## 9. 未解決問題

- 綁定碼由 FE 的 8 碼／24 小時改為 BE 的 6 位數／10 分鐘：較安全，但管理員要在 10 分鐘內交給照護員，需要確認是否可接受。
- reply token 有效時間未公開，`LINE_REPLY_WAIT_SECONDS=50` 要實測。
- 規則式 sensitive／ambiguous／out_of_scope 是暫代，P3 以 sufficient-context judge 取代；門檻以題庫 coverage–accuracy 曲線訂定。
- FE 舊有的「醫療、用藥、**補助金額**轉人工」：題庫 v0.4 把補助金額列為「回答」，本次以題庫為準，FE 歡迎詞同步修改。
- 緊急固定回覆文字待居服督導／護理人員審定；機構電話 05-2778785 取自店面招牌，待確認。
