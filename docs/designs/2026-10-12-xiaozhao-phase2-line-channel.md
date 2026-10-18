# 設計文件：小照 LINE 化改造 階段 2（LINE 通道：Webhook、喚醒詞、綁定、reply/push）

> GD 步驟 3 產出。對應評估表：`docs/designs/2026-10-12-xiaozhao-phase2-line-channel-assessment.md`（待步驟 4 填寫）。
> 對應會議決議文件決議 2／3／4（部分）。整體轉型風險等級沿用 [階段 1 設計文件](2026-10-12-xiaozhao-phase1-inbox-outbox.md) 已記載之 Tier A／會議核可基礎。
> **本階段卡在兩個外部依賴**：LINE Developer 帳號憑證（Channel Secret／Access Token）與 Cloudflare Tunnel 皆尚未備妥，本次僅能完成不需要即時憑證即可開發與測試的部分（簽章驗證邏輯、喚醒詞比對、綁定碼機制、資料模型），實際對外連通與端對端驗證待兩項外部依賴到位後才能進行。

## 1. 背景與問題

- **要解決什麼問題**：依會議決議，LINE 要成為照護員的主要入口，需要 webhook 接收機制（含簽章驗證、冪等收件）、喚醒詞過濾（避免群組洗版/誤觸發）、使用者綁定機制（求助必須知道是誰），以及 reply/push 的回覆策略（省推播額度、應對回覆權杖時效）。
- **誰會用**：已綁定的照護員與主管，透過 LINE 一對一或群組對話。
- **不做會怎樣**：LINE 入口無法上線，整個轉型卡住；且若沒有簽章驗證/冪等機制就先接 webhook，會有偽造請求與重複處理的風險。

## 2. 目標與非目標

**目標**：

- `POST /v1/line/webhook`：驗證 `X-Line-Signature`，透過階段 1 的 `inbox_requests` 去重後，寫入待處理佇列（實際 RAG 處理邏輯本階段先以既有 `run_query_pipeline` 串接，不含喚醒詞/綁定前無法進入）
- 喚醒詞「小照你好」比對：規則式容錯（去標點空白）＋拼音容錯（小昭、小趙等同音錯字），群組與一對一皆需要（依 2026-10-11 決議，不提供一對一免喚醒詞的例外）
- 綁定機制：`binding_codes`（6 位數、10 分鐘效期）＋ `user_identities`（LINE userId ↔ 內部 user，含 caregiver／supervisor 角色）
- reply／push 判斷邏輯骨架：依回覆是否在逾時窗口內完成決定用 reply 或 push（push 的實際發送串接 Cloudflare Tunnel／LINE Messaging API 待憑證到位）

**非目標（本階段明確不做）**：

- Cloudflare Tunnel 實際設定與上線（待使用者提供）
- 真正呼叫 LINE Messaging API 收發訊息（待 Channel Secret／Access Token 到位，本階段用假資料/mock 測試簽章驗證與業務邏輯）
- 求助建單與主管推送（決議 5，階段 3 範圍）
- 七類關鍵字、refusal_reason（決議 1／6，階段 4 範圍）
- LINE 語音回報（決議 7，機構書面同意未取得，持續擱置）

## 3. 現況與重用

- 階段 1 的 `inbox_requests`（`channel`, `external_event_id` 去重）／`outbox_events` 可直接重用，LINE webhook 的 `channel='line'`，`external_event_id` 用 LINE 的 `webhookEventId`
- 既有 `run_query_pipeline`（`app/core/pipeline.py`）可直接重用於 LINE 收到訊息後的 RAG 處理，不需重寫
- 目前系統沒有「使用者／角色」概念（MVP 是匿名查詢），本階段需新增 `users`、`user_identities`、`binding_codes`

## 4. 方案比較

### 4-1｜喚醒詞容錯比對方式

| | 方案 A：正規化字串比對＋拼音對照表 | 方案 B：模糊字串相似度（如 Levenshtein distance） |
|---|---|---|
| 做法概述 | 去除空白標點後，比對固定字串「小照你好」；另維護已知錯字清單（小昭/小趙你好等）轉拼音比對 | 計算輸入與「小照你好」的編輯距離，低於門檻視為命中 |
| 優點 | 邏輯單純、無誤判疑慮、好測試 | 能涵蓋未預先列舉的錯字變體 |
| 缺點／風險 | 預先列舉清單涵蓋不到的錯字會漏接 | 編輯距離門檻不易拿捏，可能誤判其他正常對話為喚醒詞 |
| 成本 | 低 | 中 |

**選定方案：A**。理由：喚醒詞是固定短語，方案 A 已能處理文件列出的已知變體（小照你好／小照 你好／小照你好！／小昭/小趙），且不會有模糊比對帶來的誤觸發風險；清單不足時可日後擴充，不需要一開始就上模糊演算法。

### 4-2｜使用者綁定狀態存放方式

| | 方案 A：獨立 `user_identities` 表（channel, external_id → user_id） | 方案 B：直接在 `users` 表加 `line_user_id` 欄位 |
|---|---|---|
| 做法概述 | 多對多友善設計，一個 user 可能未來有多個通道身分 | users 表直接存 LINE userId |
| 優點 | 未來若要加其他通道（如簡訊、App）不用改 schema | 查詢簡單，少一次 join |
| 缺點／風險 | 多一張表、多一次 join | 綁定其他通道時要加新欄位，擴充性差 |
| 成本 | 低 | 低 |

**選定方案：A**。理由：會議文件本身也用「user_identities」這個命名，且長照機構的使用情境未來不排除多通道（LINE＋其他），獨立表擴充性較好，join 成本可忽略。

## 5. 系統設計

**資料流**：

```
LINE 平台 → Cloudflare Tunnel（待設定）→ POST /v1/line/webhook
  → 驗證 X-Line-Signature（密鑰比對，防偽造請求）
  → record_inbox_request(channel='line', external_event_id=webhookEventId, payload)
      → 若 is_new=False：直接回 200，不重複處理
  → 查 user_identities 是否已綁定：
      ├─ 未綁定 + 訊息為「綁定 XXXXXX」→ 核對 binding_codes → 綁定成功/失敗回覆
      ├─ 未綁定 + 其他訊息 → 回固定說明訊息，不進 RAG、不寫 messages
      └─ 已綁定 → 喚醒詞比對
            ├─ 無喚醒詞（且不在 3 分鐘會話窗內）→ 一對一回提醒；群組靜默忽略
            └─ 有喚醒詞（或在會話窗內）→ 切掉喚醒詞 → run_query_pipeline() → 依逾時窗口決定 reply 或 enqueue_outbox_event(push)
```

**對外介面**：

- `POST /v1/line/webhook`：LINE 平台回呼端點（本階段僅本機測試，未對外曝露）

**資料模型／schema 變更**：

```
biz.users
  user_id (PK, uuid), name, role ('caregiver'|'supervisor'),
  supervisor_id (FK → users.user_id, nullable), created_at

biz.user_identities
  identity_id (PK, uuid), user_id (FK → users.user_id),
  channel (text), external_user_id (text),
  bound_at (timestamp), unbound_at (timestamp, nullable)
  UNIQUE (channel, external_user_id)

biz.binding_codes
  code_id (PK, uuid), code (text, 6 碼), created_by (FK → users.user_id),
  expires_at (timestamp), used_at (timestamp, nullable), used_by_identity_id (nullable)
```

**錯誤處理與日誌**：

- 簽章驗證失敗：回 401，記錄 warning（可能是偽造請求或密鑰設定錯誤）
- 綁定碼過期或錯誤：回覆照護員明確錯誤訊息，不洩漏其他有效綁定碼資訊
- `run_query_pipeline` 執行逾時：記錄原因，轉 push 佇列（實際 push 發送待階段 3／憑證到位）

## 6. 安全與隱私考量

- `X-Line-Signature` 驗證是本階段最關鍵的安全機制，必須在合併進主線前有對應單元測試（含「簽章錯誤應拒絕」的負向測試）
- 綁定碼為一次性、10 分鐘效期，使用後立即失效，避免重放攻擊
- `user_identities.external_user_id`（LINE userId）本身不含個資語意，但仍屬可識別使用者的代碼，比照既有去識別化原則處理

## 7. 測試策略

- **單元測試**：簽章驗證函式（正確/錯誤密鑰、竄改內容）、喚醒詞比對（文件列出的 20 種變體）、綁定碼產生與核銷邏輯、reply/push 逾時判斷
- **整合測試**：mock LINE webhook payload，端對端跑過 inbox 去重 → 綁定檢查 → 喚醒詞 → RAG pipeline
- **待補（需外部憑證後）**：實際 LINE Bot 收發驗證、Cloudflare Tunnel 連通性

## 8. 部署與運維

- 本階段不涉及對外曝露設定；Cloudflare Tunnel 設定方式待使用者提供帳號後另行補充本節

## 9. 未解決問題／實作結果（2026-10-12）

- ✅ 已實作並通過單元測試：`app/core/line_security.py`（簽章驗證，含正確/錯誤密鑰/竄改內容三種負向測試）、`app/core/wake_word.py`（喚醒詞比對，涵蓋文件列出的全部已知變體＋5 個不應誤觸發的負向案例）
- ✅ 已實作並通過整合測試（真實 PostgreSQL）：`app/db/binding.py`（綁定碼產生/核銷/過期/重複使用/未知代碼五種情境）
- ✅ `biz.users`／`biz.user_identities`／`biz.binding_codes` schema 已建立（含 `BindingCode.target_user_id`，設計文件原稿遺漏此欄位、實作時發現並補上——沒有這欄位系統不知道一組綁定碼該綁定給哪位照護員）
- ✅ `POST /v1/line/webhook` 已實作（`app/api/routes_line_webhook.py`）：簽章驗證 → inbox 去重 → 綁定檢查（含「綁定 XXXXXX」核銷流程）→ 喚醒詞 → `run_query_pipeline` → reply。App 已驗證可正常啟動並註冊此路由
- ✅ `app/core/line_client.py`：reply/push 已寫好真實的 LINE Messaging API 呼叫邏輯，但在 `LINE_CHANNEL_ACCESS_TOKEN` 未設定時會直接回傳 False 並略過呼叫，不會讓流程中斷
- ⏸️ **Cloudflare Tunnel 設定**（待使用者提供 Cloudflare 帳號／tunnel 權杖）——沒有這個就無法對外接收真實 LINE webhook
- ⏸️ **LINE Channel Secret／Access Token**（待使用者於 LINE Developers Console 申請）——沒有這個無法做端對端真實收發驗證，目前僅驗證過簽章演算法本身（用假密鑰測試），未驗證過真實 LINE 平台送來的簽章格式
- 回覆權杖的實際有效時間官方未明確文件化，目前程式碼固定呼叫 reply，**尚未實作「逾時改 push」的邏輯**（設計文件寫了方向，但判斷逾時需要真實量測 `run_query_pipeline` 執行時間與 LINE 回覆權杖的實際有效期，兩者都要有真實環境才能校準，本階段先略過，標記為後續工作）
- 3 分鐘會話窗（喚醒後免重複喚醒）**尚未實作**——目前每則訊息都要重新帶喚醒詞，`WAKE_WORD_SESSION_WINDOW_SECONDS` 設定已加入 `.env.example` 但程式邏輯未串接，需要設計「最後喚醒時間」要存在哪裡（`user_identities` 加欄位，或另開一張 session 表）
- 主管產生綁定碼目前只有 `create_binding_code()` 這支函式，沒有對外介面可以呼叫（規劃在 Open WebUI 操作，但 Open WebUI 沒有現成「產生綁定碼」功能），留待階段 3 與求助介面一併設計管理端點

### 2026-10-12 補實作（依本文件第 5、6 節，沿用本階段 GD 與豁免紀錄）

- ✅ **非同步處理＋push 備援**（對應第 5 節「逾時轉 push」）：webhook 驗證簽章後立即回 200，事件改在回應送出後處理（`BackgroundTasks`，自建資料庫 session）。回覆一律先試 reply，失敗（reply token 逾時或失效）就改用 push；群組訊息 push 回群組。採「reply 失敗才 push」而非預先量測逾時門檻，不需要真實環境校準。注意：push 會占用 LINE 官方帳號的每月推播額度。
- ✅ **處理失敗不再無聲遺失**（對應第 5 節錯誤處理）：單一事件處理拋出例外時記錄 log、rollback，並 push「系統暫時無法處理您的問題，請稍後再試一次」。因為已先回 200，LINE 不會重送，所以必須主動通知。`LineClient` 呼叫失敗（網路錯誤或非 200）一律記錄 warning。
- ✅ **綁定碼安全**（對應第 6 節）：改用 `secrets` 產生；產生時避開其他有效碼，避免核銷時對象不明；核銷查詢加上未過期條件；每個 LINE 帳號 10 分鐘內最多嘗試 5 次（Redis 計數，Redis 故障時視為受限）。
- 測試：`tests/test_line_webhook.py` 新增 7 項（立即回 200、簽章錯誤 401、reply 失敗改 push、處理失敗通知、綁定嘗試限流、限流計數、綁定碼格式）。
- 仍未處理：長答案超過 LINE 文字訊息上限、3 分鐘會話窗、綁定碼管理介面、Cloudflare Tunnel 只開放 webhook 路徑（設定時須注意）。
