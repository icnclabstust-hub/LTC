# 設計文件（DESIGN_DOC）：依後端組 API 合約串接（HttpBackend）

> Tier：B（新增本服務對後端的對外呼叫）
> 流程豁免：老師 2026-10-12「以後測試不用等我認可，就是 try」；提案學生確認沿用，會議報告
> 提案學生：吳佳杰　　版本：草稿 v0.1（2026-10-14）
> 依據：後端組簡報《小照前後端串接設計》（2026-10-03，①②③）、《小照 LINE 化 待決策事項簡報》（2026-10-12）、十月第一次會議紀錄

## 1. 需求（GD 步驟 1，2026-10-14 提案學生確認）

- **要解決什麼問題**：本服務只接模擬後端，送後端的 JSON 只寫入記錄檔。要依後端組（吳明叡）的 API 合約，讓錄音的問句、一輪紀錄能真的送到後端並取回回答。
- **誰會用**：照護員（回答）、後端組（收到請求）。
- **不做會怎樣**：無法和後端組實際串接測試。
- **決定**：
  - 「現在先把 API 都放上去」：只做本服務呼叫後端的部分。
  - **不動**：LINE 由誰接收（後端組已有 `/v1/line/webhook`）、綁定統一、督導回覆移到 Open WebUI。這些等和後端組討論後另案處理。

## 2. 現況（GD 步驟 2）

- `backend.py` 的 `Backend` 介面只有 `query`、`scene` 兩個方法，回傳 `BackendResult`；`build_backend()` 只支援 `mock`。
- `TrackedBackend` 會補上 `client_request_id`，並把請求寫入記錄檔。

後端組的 API 合約：

| 編號 | 端點 | 回傳 |
|---|---|---|
| ① | `POST /v1/query`：`request_id`（UUID，冪等）、`role`、`user_id`、完整文字、`keywords`、`client_intent: {code, confidence}`、`label_set_version`、`lexicon_version`、`client_timing_ms` | 202＋`request_id` |
| ② | `POST /v1/scene`：`request_id`、`caregiver_user_ids`、`scene`、`intent_counts`、`segment_count`、`caregiver_segment_count`、`keywords`、`duration_sec`、版本 | 202＋`request_id` |
| ③ | `GET /v1/requests/{id}` | `status`：`pending`／`answered`／`refused`／`human_answered`／`error`、`answer_spoken`、`answer_full`、`citations`、`help_request_id`、`human_answer` |

- 驗證：`Authorization: Bearer <token>`。

## 3. 方案比較

| | 方案 A：202＋輪詢（**採用**，依合約） | 方案 B：後端回呼本服務（webhook） |
|---|---|---|
| 做法 | 送出 ①②，拿到 `request_id` 後輪詢 ③，直到有結果或逾時 | 後端回答後呼叫本服務的接收端點 |
| 優點 | 與後端組設計一致；本服務不需新增對外端點 | 不需輪詢 |
| 缺點 | 輪詢間隔會增加延遲 | 合約沒有；本服務要多開一個對外端點 |

## 4. 設計

### 4.1 `HttpBackend`（`backend.py`）

- `.env`：
  - `BACKEND_MODE=http`；
  - `BACKEND_URL`（例如 `http://127.0.0.1:8001`）；
  - `BACKEND_TOKEN`；
  - `BACKEND_POLL_SECONDS`（預設 0.3）；
  - `BACKEND_TIMEOUT_SECONDS`（預設 10，對應合約 F2）。
- `query(payload)`：
  - 送 ① 後輪詢 ③，直到 `status` 不是 `pending`，或逾時。
  - `request_id` 沿用 `TrackedBackend` 產生的 `client_request_id`；同一請求重送時用同一個值。
- `scene(payload)`：送 ②，**不輪詢**（`scene` 的回應已不當作回答，`multi-intent-turn` 第 4.3 節）。
- 網路錯誤、5xx、401：記錄錯誤並回傳 `awaiting_human`（原因寫明「後端連線失敗」），讓問題轉到督導頁，不會遺失。
- 仍經過 `TrackedBackend`，送出的 JSON 與回應照樣寫入記錄檔。

### 4.2 欄位對應

| 合約欄位 | 本服務來源 |
|---|---|
| `request_id` | `client_request_id`（UUID） |
| `role` | `caregiver`（目前只有照護員使用；合約 C3） |
| `user_id` | **暫送 `null`**（本服務沒有後端組的照護員編號；待綁定統一） |
| `text` | 問句的文字（問句 window：相關陳述＋問句） |
| `keywords` | 空陣列 |
| `client_intent` | `{code: 意圖代碼, confidence}`，代碼見 4.3 節 |
| `label_set_version` | `awe-intent-7-v1` |
| `lexicon_version` | `null`（尚未使用後端詞表） |
| `client_timing_ms` | 從句子結束到送出的毫秒數（有的話） |
| 其他 | 本服務原有欄位照送（`turn_id`、`question_utterance_id`、`context_utterance_ids`、`caregiver_name`、`speaker_code`、`provisional`…），後端可忽略 |

**②** 送 `caregiver_user_ids: []`、`intent_counts`（逐句意圖統計）、`segment_count`（句數）、`caregiver_segment_count`（說話人為照護員的句數）、`duration_sec`、`scene: null`（本服務不判定場合），並照送原有的 `utterances`、`intents`。

**是否送逐字稿待後端組確認**：合約原設計不含原文；本服務先照送，設定 `BACKEND_SCENE_TEXT=0` 時不送 `utterances` 的文字。

**③ 狀態對應**：

| 合約 `status` | `BackendResult.status` | 回答 |
|---|---|---|
| `answered` | `answered` | `answer_full`（沒有時用 `answer_spoken`） |
| `refused` | `awaiting_human` | 原因：後端拒答轉人工（附 `help_request_id`） |
| `human_answered` | `answered` | `human_answer` |
| `error`／逾時 | `awaiting_human` | 原因：後端錯誤或逾時 |

### 4.3 意圖代碼（草案，待後端組確認）

| 意圖 | 代碼 |
|---|---|
| 情緒安撫或閒聊 | `emotional_support` |
| 拒絕或抗拒 | `refusal` |
| 服務請求 | `service_request` |
| 確認或同意 | `confirmation` |
| 規範詢問 | `regulation_inquiry` |
| 資訊告知或交接 | `information_handover` |
| 身體不適陳述 | `discomfort_report` |

### 4.4 不在本次範圍

- ⑥ `POST /v1/incidents`（今日狀況回報）：機構書面同意（R2）擱置中，待確認。
- ⑦ LINE webhook、⑧ 求助清單、綁定統一、督導回覆移到 Open WebUI。
- 轉人工後由後端回傳主管回答（`human_answered`）的後續輪詢：待確認主管在哪個介面回答。

## 5. 測試

1. 單元：用假的 HTTP 傳輸（不連網）模擬後端：
   - 202 後輪詢到 `answered`；
   - `refused` → `awaiting_human`；
   - 逾時、5xx、401 → `awaiting_human`；
   - `request_id`、`Authorization`、欄位對應正確；
   - `scene` 不輪詢；
   - `BACKEND_SCENE_TEXT=0` 時不含文字。
2. 本機假後端：`tools/fake_backend.py`（依合約的 ①②③，供手機實測與後端組對照）。
3. 既有測試全過；`BACKEND_MODE=mock` 時行為不變。

## 6. 回滾

`.env` 設 `BACKEND_MODE=mock`。

## 7. 實作結果（2026-10-14）

| 檔案 | 內容 |
|---|---|
| `backend.py` | `HttpBackend`（①② 202、③ 輪詢、狀態對應、錯誤與逾時轉督導、意圖代碼 `INTENT_CODES`、`LABEL_SET_VERSION`）；`build_backend` 支援 `http` |
| `config.py`、`app.py`、`.env.example` | `BACKEND_URL`、`BACKEND_TOKEN`、`BACKEND_POLL_SECONDS`、`BACKEND_TIMEOUT_SECONDS`、`BACKEND_SCENE_TEXT` |
| `pipeline.py` | `scene` 的每句加上 `start_ms`、`end_ms`（計算 `duration_sec`） |
| `tools/fake_backend.py`（新增） | 本機假後端（依合約；含權杖檢查、pending、refused） |
| `tests/xiaozhao/test_http_backend.py`（新增） | 9 項：欄位對應、輪詢、狀態對應、逾時與錯誤、scene、不送文字、假後端往返、模式切換 |

驗證：
- 單元測試 193 項全過。
- 實際啟動假後端（`FAKE_BACKEND_TOKEN=demo`，port 8011）連線測試：
  - 一般問題 202 → pending → answered，約 1.2 秒（假後端延遲 1 秒）；
  - 含「藥」→ 拒答轉人工（附求助單編號）；
  - scene 送出；
  - 錯誤權杖 → 401 轉督導。

### 7.1 已知問題：後端呼叫會卡住音訊處理

`pipeline` 在處理音訊的同一個流程（持有 session 的鎖）中同步呼叫後端：

- 預取：`_add_utterance`；
- 輪結束：`_question_results`、`scene`。

模擬後端立即回覆，所以沒有影響；改用 `http` 後，每次 query 要輪詢到有結果，最長 `BACKEND_TIMEOUT_SECONDS`（10 秒）。這段時間該錄音的音訊片段無法處理，上傳會堆積，回答也會延後顯示。

建議另案修正（Tier B）：後端呼叫改到背景執行緒，回答取回後再更新資料庫與畫面（預取、輪結束回答、轉督導都要調整）。修正前，`http` 模式只適合串接測試，不建議手機長時間錄音使用。
