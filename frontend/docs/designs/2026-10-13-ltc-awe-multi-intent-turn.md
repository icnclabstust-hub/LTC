# 設計文件（DESIGN_DOC）：一輪多個意圖（多標籤彙整與逐一回答）

> 對應任務：2026-10-12 論文對照第 4 點——一輪只取信心最高的一句，與三層定義「一輪可有多個意圖（多標籤）」不符
> Tier：B（改動經 ngrok 對外的 LIFF 服務，並改變送後端的用法）
> 流程豁免：老師 2026-10-12「以後測試不用等我認可，就是 try」；提案學生確認沿用，會議報告
> 提案學生：吳佳杰　　版本：草稿 v0.1（2026-10-13）

## 1. 需求（GD 步驟 1，2026-10-13 提案學生確認）

- **要解決什麼問題**：照護員問「阿嬤可以吃這個藥嗎？」、長輩接著說「我肚子痛」，這一輪同時有「規範詢問」與「身體不適陳述」，但現在只留信心最高的一個，另一個問題沒有被回答或轉督導。
- **誰會用**：照護員（看到每個問題的回答）、督導（收到每個需轉人工的問題）、後端（收到整輪的意圖清單）。
- **不做會怎樣**：「多人、多輪、多意圖」的多意圖沒有落實，被追問時無法說明。
- 決定：
  1. 彙整方式：**逐句意圖去重複後全部列出**（信心過低的不列），附依據句子、最高信心、說話人。
  2. **每個需要後端回答的意圖各自回答**。
  3. 意圖模型維持 A1，只改彙整與送出（N = 0 雙輸出頭模型另案）。

## 2. 現況（GD 步驟 2）

- 預取：每句意圖屬於「規範詢問／服務請求／身體不適陳述」且信心 ≥ 0.9 時，各意圖送一次 `query`，結果存在 `turn.prefetched[意圖]`。
- 輪結束：`_final_intent` 取上述三類中信心最高的一句；預取命中就沿用，否則送一次 `scene`（整輪紀錄）並以其結果作為**唯一**回答；`turns` 只有一個 `answer`／`answer_status`，轉督導也只建一筆。

## 3. 方案比較

| | 方案 A：每個意圖送一次 `query`，`scene` 只當整輪紀錄（**採用**） | 方案 B：只送一次 `scene`，由後端回傳每個意圖的結果 |
|---|---|---|
| 做法 | 有預取就沿用；沒有就以該意圖的依據句子送 `query`；`scene` 帶意圖清單，結果不再當回答 | `scene` 帶意圖清單，後端回傳 `[{intent, status, answer}]` |
| 優點 | 沿用預取的「每意圖一次 query」，後端只需一種回答介面；不需等後端改格式 | 請求數較少 |
| 缺點 | 請求數較多（每個意圖一次） | 須後端改 `scene` 回應格式，目前無法確認 |

## 4. 設計

### 4.1 一輪的意圖清單

```
_turn_intents(utterances) → [
  {"intent": "規範詢問", "utterance_ids": [0, 2], "confidence": 0.95,
   "speaker_codes": ["01"], "speaker_roles": ["照護員"]},
  …]
```
- 依第一次出現的順序；同一意圖的多句合併。
- 信心 < 0.5（`MIN_TURN_INTENT_CONF`）的句子不列入。

### 4.2 回答

- 需要回答的意圖（規範詢問／服務請求／身體不適陳述）：
  - 有預取結果 → 沿用；
  - 沒有 → 送 `query`：`{turn_id, utterance_ids, caregiver_name, text（依據句子以「；」相接）, intent, intent_conf, speaker_codes, provisional: false}`。
- 每個意圖的結果存在 `turns.intents`（新欄位，JSON：`[{intent, utterance_ids, confidence, status, answer, reason, request_id}]`）。
- `turns.answer`：合併為「【規範詢問】回答…」「【身體不適陳述】已轉督導」，每個意圖一行；`turns.answer_status`：任一轉人工即 `awaiting_human`，否則 `answered`，沒有需回答的意圖為 `no_question`。
- 每個 `awaiting_human` 的意圖各建一筆督導請求（問題＝依據句子）。
- `final_intent` 欄位保留（第一個需回答的意圖），供相容。

### 4.3 整輪紀錄

`scene` 每輪仍送一次，新增 `intents`（4.1 的清單），**結果不再作為回答**（只記錄）。串接說明同步更新。

### 4.4 資料庫

`turns` 新增 `intents TEXT`；舊資料庫啟動時以 `ALTER TABLE` 補欄位。

## 5. 測試

1. 單元：意圖去重複與依據句子；信心過低不列；兩個需回答的意圖各得一個回答（一個沿用預取、一個新送 `query`）；一個回答、一個轉督導時狀態為 `awaiting_human` 且只為該意圖建督導請求；只有閒聊時為 `no_question`；`scene` 帶 `intents`；舊資料庫遷移。
2. 既有測試全過。

## 6. 回滾

還原 `pipeline._end_turn`；`turns.intents` 欄位留著不影響。

## 7. 實作結果（2026-10-13）

- `pipeline._turn_intents` 取代 `_final_intent`；`_end_turn` 依第 4 節逐意圖回答、`scene` 帶 `intents` 且結果不作回答；`turns.intents`、`supervisor_requests.intent` 新欄位（舊資料庫啟動時 `ALTER TABLE` 補上，已以本機資料庫複本驗證，原有 26 輪保留）。
- **追加**：督導回覆原本會把整輪的回答換成督導回覆；一輪多個意圖時改為只更新轉督導的那一個意圖，其他意圖的回答保留，再重新合併顯示文字與狀態（仍有待回覆者為 `awaiting_human`，否則有督導回覆者為 `human_answered`）。
- 網頁：回答框改為保留換行（每個意圖一行）。
- 後端串接說明第 2 節同步更新（流程 2a／2b、`intents` 欄位、`scene` 回應不再當作回答）。
- 測試：133 項全過（新增 `test_multi_intent.py` 6 項；`test_prefetch_once_per_intent_and_reused_at_turn_end` 改為預期每輪送一次 `scene`）。待提案學生手機實測。
