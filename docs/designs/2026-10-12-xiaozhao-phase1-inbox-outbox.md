# 設計文件：小照 LINE 化改造 階段 1（API 合約、inbox/outbox 冪等機制）

> GD 步驟 3 產出。對應評估表：`docs/designs/2026-10-12-xiaozhao-phase1-inbox-outbox-assessment.md`（待步驟 4 填寫）。
> 本項為 [小照前後端串接：會議決議深度分析.md]（2026-10-11 會議文件）七階段實作計畫的第 1 階段。
> 整體轉型（LINE Bot 對外服務＋照護事件回報）風險等級依會議決議視為已核可（Tier A 等級的整體專案），但本階段本身不對外、不碰個資，blast radius 有限。決議 7（語音回報，涉健康個資）機構書面同意尚未取得，**本階段與後續階段皆不包含決議 7 相關開發**，待取得同意後另立設計文件。

## 1. 背景與問題

- **要解決什麼問題**：LINE 作為新的對外通道，Webhook 可能因網路重試、LINE 平台重送等原因收到重複事件；若沒有冪等機制，同一則照護員提問或求助可能被處理多次（重複寫入對話紀錄、重複推播主管）。需要先建立通用的 inbox（去重收件）／outbox（可靠送達）機制，供階段 2 LINE 通道與未來其他通道（如決議 7 的語音回報）共用。
- **誰會用**：系統內部機制，不是終端使用者直接操作的功能；後續階段（LINE webhook、主管推播）都會依賴這層。
- **不做會怎樣**：階段 2 接 LINE webhook 時，重複事件會被重複處理，可能造成同一求助被推播多次、同一問題被重複記錄，影響主管信任與資料正確性。

## 2. 目標與非目標

**目標**：

- `inbox_requests` 表：記錄所有對外通道收到的原始請求，以外部事件 ID 去重（同一事件 ID 重送，第二次起直接忽略/回已處理狀態）
- `outbox_events` 表：記錄所有待送達的對外事件（如推播），具備狀態機（pending/sent/failed），供送達程序消費
- 定義 API 合約的共用資料結構（不綁定 LINE，channel 欄位區分來源，為階段 2 預留擴充）

**非目標（本次明確不做）**：

- LINE webhook 端點本身（階段 2）
- 喚醒詞、綁定碼、求助推送等業務邏輯（階段 2–3）
- 決議 7 語音回報相關任何開發（機構書面同意未取得前不得開始）
- Cloudflare Tunnel 或任何對外網路曝露設定（階段 2 才需要，本階段純資料庫/程式邏輯）

## 3. 現況與重用

- 既有的 `catalog.sync_events` 是資料擷取層的 outbox（PostgreSQL → ChromaDB 同步事件），用途與命名雖相近，但服務對象不同（一個是內部資料同步，一個是對外請求/推播），**不可共用同一張表**，避免語意混淆
- 既有 `biz.conversations`／`biz.messages` 記錄的是問答內容本身，不是「請求去重」或「送達狀態」，兩者互補不重疊

## 4. 方案比較

### 4-1｜去重鍵的設計方式

| | 方案 A：以（channel, external_event_id）複合唯一鍵 | 方案 B：用訊息內容雜湊去重 |
|---|---|---|
| 做法概述 | 每個通道自帶的事件 ID（如 LINE 的 webhookEventId）＋通道名稱做唯一鍵 | 對請求內容算 hash，相同內容視為重複 |
| 優點 | 符合各平台 webhook 本來就保證「同一 event ID 代表同一事件」的設計，可靠 | 不依賴外部平台提供 ID |
| 缺點／風險 | 需要每個通道都確實提供穩定的事件 ID（LINE 有提供） | 使用者連續問兩次一模一樣的話會被誤判為重複請求而遺漏 |

**選定方案：A**。理由：LINE webhook 本身就提供穩定的事件 ID，方案 B 的「內容去重」在真實情境下會誤殺合法的重複提問（照護員就是可能連續問兩次一樣的問題），不適合問答類場景。

### 4-2｜outbox 送達狀態追蹤方式

| | 方案 A：狀態欄位（pending/sent/failed）+ 重試次數 | 方案 B：完整的事件溯源（event sourcing）log |
|---|---|---|
| 做法概述 | 一張表，一列代表一個事件目前狀態，送達程序更新狀態 | 每次狀態變化都新增一列歷史紀錄，不更新既有列 |
| 優點 | 簡單、查詢當下狀態直接 | 可完整追溯所有狀態變化歷史 |
| 缺點／風險 | 歷史變化不可追溯 | 複雜度高，目前規模用不到 |

**選定方案：A**。理由：目前規模（照護員與主管數量有限）不需要完整事件溯源，簡單狀態機已足夠滿足「完成判準：同一訊息重送三次，資料庫只有一筆」的驗收需求；非目標：過度設計。

## 5. 系統設計

**資料模型**：

```
biz.inbox_requests
  request_id (PK, uuid)
  channel (text)              -- 'line' / 'api' 等，階段1先支援 'api'(測試用)
  external_event_id (text)    -- 來源平台的事件 ID
  payload (jsonb)
  received_at (timestamp)
  processed_at (timestamp, nullable)
  UNIQUE (channel, external_event_id)

biz.outbox_events
  event_id (PK, uuid)
  event_type (text)           -- 如 'push_notification'
  target (text)                -- 如使用者識別碼，階段1先留通用欄位
  payload (jsonb)
  status (text)                -- pending / sent / failed
  attempt_count (int, default 0)
  created_at (timestamp)
  sent_at (timestamp, nullable)
```

**對外介面**：本階段無新增 API port；提供內部函式 `record_inbox_request(channel, external_event_id, payload) -> (is_new: bool, request_id)` 與 `enqueue_outbox_event(event_type, target, payload)`，供階段 2 呼叫。

**錯誤處理與日誌**：重複請求（`is_new=False`）記錄一筆 info log 並直接回傳既有 `request_id`，不重新處理；outbox 送達失敗時 `attempt_count` 遞增，狀態轉 `failed`，由後續階段的重試機制處理（本階段不含重試邏輯本身，只建狀態機骨架）。

## 6. 安全與隱私考量

- 本階段 `payload` 欄位會原樣存放請求內容，階段 2 若接 LINE 訊息內容，payload 可能含使用者原文——待階段 2 設計時依 L5 原則（個案內容盡量不留）評估是否需要欄位層級加密或縮短保存期限；本階段先用測試資料驗證機制本身，不涉及真實個資

## 7. 測試策略

- **單元測試**：`record_inbox_request` 對同一 `(channel, external_event_id)` 呼叫三次，驗證只建立一筆記錄、回傳同一 `request_id`
- **整合測試**：`enqueue_outbox_event` 建立事件後查詢狀態為 `pending`，模擬送達成功/失敗分別驗證狀態轉移

## 8. 部署與運維

- 沿用現有 PostgreSQL／alembic migration 流程，不涉及新服務或新 port

## 9. 未解決問題

- `outbox_events` 的實際送達程序（如何觸發送達、重試排程）留待階段 2 接 LINE push API 時一併設計
- `target` 欄位的使用者識別方式（LINE userId）要等階段 2 的綁定機制（決議 4）確定後才能定型，本階段先用通用 text 欄位
