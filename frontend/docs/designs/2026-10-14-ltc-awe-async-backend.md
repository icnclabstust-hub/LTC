# 設計文件（DESIGN_DOC）：後端呼叫改到背景執行

> Tier：B（改變錄音流程與 LINE webhook 的處理方式）
> 流程豁免：老師 2026-10-12「以後測試不用等我認可，就是 try」；提案學生確認沿用，會議報告
> 提案學生：吳佳杰　　版本：草稿 v0.1（2026-10-14）
> 前置：`2026-10-14-ltc-awe-http-backend.md` 第 7.1 節

## 1. 需求（GD 步驟 1）

- **要解決什麼問題**：`BACKEND_MODE=http` 時，每次 query 要輪詢到有結果，最長 10 秒。錄音流程在持有 session 鎖的狀態下同步等待，造成兩個問題：
  - 音訊片段無法處理、上傳堆積；
  - LINE webhook 要等後端回答後才回 200，LINE 可能逾時重送。
- **誰會用**：照護員（錄音不卡、回答陸續出現）；後端組串接。
- **不做會怎樣**：無法用真的後端長時間錄音。
- 提案學生 2026-10-14 同意接著做（「好」）。

## 2. 現況（GD 步驟 2）

| 呼叫點 | 現況 | 會卡住什麼 |
|---|---|---|
| 錄音預取（`pipeline._add_utterance`） | 同步，持有 session 鎖 | 音訊處理 |
| 輪結束（`_end_turn`：問句、陳述逐一 query、scene） | 同步，持有 session 鎖 | 音訊處理；下一輪開始 |
| LINE 文字訊息（`/callback` → `line_service`） | `asyncio.to_thread` 執行完才回 200 | webhook 回應 |
| LIFF 打字提問（`/api/residents/{id}/text-questions`） | 同步，請求等到結果 | 只有該次請求（照護員本來就在等回答） |

## 3. 方案比較

| | 方案 A：背景執行緒池（**採用**） | 方案 B：改成 asyncio 全非同步 |
|---|---|---|
| 做法 | 後端呼叫丟到共用的 `ThreadPoolExecutor`；結果回來後更新資料庫 | pipeline、backend 全改 async |
| 優點 | 改動集中在呼叫點；`Backend` 介面不變 | 資源使用較省 |
| 缺點 | 要處理「結果晚到」的狀態 | pipeline 在鎖內同步處理音訊，全面改寫風險高 |

## 4. 設計

### 4.1 共用執行緒池

- `backend.BackgroundRunner`：
  - 包住 `ThreadPoolExecutor`（`BACKEND_WORKERS`，預設 4）；
  - `submit(fn) -> Future`；
  - 例外記錄日誌，不往外丟。
- `inline=True` 時立即執行並回傳已完成的 `Future`。單元測試與 `BACKEND_MODE=mock` 用這種方式（模擬後端立即回覆，行為與現在完全相同）。
- 服務：`BACKEND_MODE=http` 時使用背景執行。

### 4.2 錄音

- **預取**：`_add_utterance` 只送出工作、存下 `Future`（`turn.prefetched[意圖]`、`turn.prefetched_questions[句子編號]`），不等結果。
- **輪結束**：
  - 鎖內（同步）：寫入 `ended_at`、`end_reason`；`answer_status = 'pending'`；建立確認紀錄；存輪內音訊。
  - 背景：等預取的 `Future`；沒有預取的問句或意圖送 query；送 scene；合併回答；更新 `turns.answer`、`answer_status`、`intents`；建立督導請求。
  - 回答出現在畫面的方式不變：錄音頁每 2 秒輪詢 `turns`，背景完成後就會顯示。
- **畫面**：`answer_status = 'pending'` 顯示「查詢中…」。
- **結束錄音**：`stop()` 不等背景工作；工作完成後照樣寫入資料庫。

### 4.3 LINE webhook

- `/callback` 驗證簽章後**立即回 200**，事件處理丟到背景。
- 回覆：沿用「回覆權杖，失敗改 push」（回覆權杖約 1 分鐘內有效，後端逾時 10 秒仍在期限內）。

### 4.4 不改

- LIFF 打字提問：照護員按下送出後本來就在等回答，維持同步（最長 10 秒），畫面顯示「送出中」。
- `Backend` 介面、`HttpBackend`、`MockBackend` 不改。

### 4.5 執行緒安全

- `Database` 已有鎖。
- 背景工作只寫入該輪的資料列與督導請求，不碰 `SessionProcessor` 的即時狀態（`turn` 物件在輪結束後不再被音訊流程修改）。

## 5. 測試

1. 單元：
   - `inline` 模式下，既有測試全過（行為不變）；
   - 背景模式下，用「會延遲的後端」：
     - 輪結束後 `answer_status` 先是 `pending`，背景完成後變成 `answered`；
     - 預取尚未完成時，輪結束會等它，不重送；
     - 背景期間仍能繼續處理下一段音訊；
     - 轉人工時建立督導請求；
     - 後端例外不影響音訊處理。
   - LINE `/callback` 立即回應，背景完成後才回覆。
2. 本機假後端（回答延遲 3 秒）＋手機實測：錄音不卡、回答陸續出現。

## 6. 回滾

`.env` 設 `BACKEND_ASYNC=0`，所有後端呼叫恢復同步。

## 7. 回覆時間目標 3 秒（2026-10-14，提案學生指示與確認）

- **提案學生**：打字或語音提問（轉督導的除外），送出問題到收到回覆的上限是 3 秒。這與 10/05 會議決議（回應時效三秒內，以 P95 計）一致。
- **超過 3 秒的處理**：提案學生選 **A**，不轉督導、不丟棄：
  - 先告知「小照還在查詢」，背景繼續等到 `BACKEND_TIMEOUT_SECONDS`（30 秒）；
  - 回答到了就補上，30 秒仍沒有回覆才轉督導。
- **3 秒的計算範圍**：送出問題到收到回覆，不含說話、ASR、意圖判定的時間。先量測這段時間。

| 項目 | 做法 |
|---|---|
| 輪詢間隔 | 0.3 → **0.1 秒**（`BACKEND_POLL_SECONDS`） |
| 回覆目標 | `REPLY_TARGET_SECONDS=3` |
| 錄音 | 一輪結束後 `answer_status = pending`：前 3 秒顯示「查詢中…」，之後顯示「小照還在查詢，回答會稍後出現…」 |
| LIFF 打字提問 | 請求最多等 3 秒；還沒好就回 `pending`，畫面自動更新，回答到了寫入 |
| LINE 文字提問 | 3 秒內有回覆就直接回覆；沒有就先用回覆權杖告知「小照還在查詢，回答好了會再通知你。」，回答到了用 push 送出 |
| 量測 | `BackendResult.elapsed_ms`（`TrackedBackend` 量測送出到收到回覆）寫入記錄檔；`text_questions.reply_ms`、`line_cases.reply_ms`；錄音每個問題寫在 `turns.intents` 的 `elapsed_ms` |
| 畫面 | 錄音的每一輪、打字提問都顯示「回覆時間 ○.○ 秒」，超過 3 秒標「（超過 3 秒）」 |
| 統計 | `tools/backend_latency.py`：依來源列出次數、P50、P95、最大值、3 秒內比例 |

## 8. 實作結果（2026-10-14）

| 檔案 | 內容 |
|---|---|
| `backend.py` | `BackgroundRunner`（背景執行；`inline` 立即執行）；`BackendResult.elapsed_ms`；`TrackedBackend` 量測並記錄 `elapsed_ms`；`HttpBackend` 預設輪詢 0.1 秒、最長 30 秒 |
| `pipeline.py` | 預取存為 Future；輪結束先寫入結束與 `pending`、建立確認紀錄、存音訊，回答在背景取回後更新（`_finish_turn_backend`）；後端失敗時標為轉督導 |
| `line_service.py` | `parse_events`／`process_events`；LINE 案件超過 3 秒先告知，回答到了 push；`reply_ms` |
| `app.py` | `State.runner`（http 且 `BACKEND_ASYNC=1` 時背景執行）；`/callback` 驗證簽章後立即回應、事件背景處理；打字提問最多等 3 秒 |
| `db.py` | `text_questions.reply_ms`、`line_cases.reply_ms`；舊資料庫補欄位 |
| `config.py`、`.env.example` | `BACKEND_ASYNC`、`BACKEND_WORKERS`、`REPLY_TARGET_SECONDS`；輪詢與逾時預設值 |
| `static/app.js` | 查詢中提示、回覆時間、打字提問自動更新 |
| `tools/backend_latency.py`（新增） | 回覆時間統計 |

驗證：

- 單元測試 199 項全過（新增 `test_async_backend.py` 6 項；既有測試在立即執行模式下行為不變）：
  - 後端被擋住時音訊照常處理，一輪先是 `pending`、放行後變 `answered`；
  - 預取不重送；
  - 打字提問超過目標回 `pending`、之後寫入回答與 `reply_ms`；
  - LINE 先回「還在查詢」、之後 push 回答；
  - 3 秒內直接回覆；
  - 記錄檔含 `elapsed_ms`。
- 本機假後端（回答延遲 1 秒）：回覆時間約 1.04 秒（輪詢間隔造成的額外延遲約 0.04 秒），`backend_latency.py` 統計正常。
- 待：與後端組的真實後端串接後，以手機量測 P95。
