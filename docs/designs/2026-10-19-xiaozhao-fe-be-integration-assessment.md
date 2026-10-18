# 開發前評估表（PRE_DEV_ASSESSMENT）

## 一、基本資料

- 任務名稱：小照前後端合併與串接（P0＋P1）
- 提案學生：（請填寫）
- 日期：2026-10-19
- 對應設計文件：`docs/designs/2026-10-19-xiaozhao-fe-be-integration.md`

## 二、問題定義（三問）

1. 要解決什麼問題？FE 呼叫的三支 BE 端點不存在，前後端實際接通路徑為 0，照護員每題都被轉督導。
2. 誰會用？多常用？嘉安居服的居服員與居服督導；試用期每天數題到數十題。
3. 不做會怎樣？兩邊只能各自展示，無法端對端 demo，整合度停在約 37.5%。

## 三、現況調查結論

- 既有可重用的實作／腳本：BE RAG pipeline、binding、rate_limit、chat_completions；FE `HttpBackend`、假後端、LINE webhook、督導頁（詳見設計文件第 3 節）。
- 可直接採用的現成套件／服務：LINE loading animation API（免 push 額度）；line-bot-sdk、FastAPI BackgroundTasks。
- 仍需自行開發的缺口：三支端點與狀態表、Bearer 驗證、結構化拒答、身分 API、FE 緊急與轉督導流程。
- □ 調查後結論為「不需開發」

## 四、風險分級

- Tier：□ A　■ B　□ C（AI 初判，待老師確認；`CODE_REVIEW_STANDARD.md` 不在 repo 內，無法對照正式定義）
- 分級理由：新增 8 支 HTTP 端點與一張表；處理照護員提問（可能含健康資訊，個資法第 6 條特種個資）；LINE 對外接收訊息；失效時照護員收不到回答（有轉督導兜底）。

## 五、成本與時程

- 預估工時：約 6–8 人日
- 新增依賴：無新套件；新增 BE 環境變數 `XIAOZHAO_API_TOKEN`、FE `LINE_REPLY_WAIT_SECONDS`、`AGENCY_PHONE`；無新 port、無新排程。
- 需要的機器資源：沿用現有（本機）。

## 六、成功指標與驗證方式

- 怎樣算做完：
  1. FE 設 `BACKEND_MODE=http` 後，LINE 文字提問能拿到 BE 真實答案（含原文引用）；
  2. 拒答依 `refusal_reason` 正確分流（轉督導或直接回覆）；
  3. 題庫「緊急關鍵字」18 題全部命中、「不該觸發」5 題全部不命中；
  4. BE、FE 既有測試全數通過。
- 怎麼驗證：見設計文件第 7 節。

## 七、回滾與退場

- 回滾：FE `BACKEND_MODE=mock`；BE `alembic downgrade be2c7233d3e9`；`git revert` 本分支。
- 中止清理：drop `biz.xiaozhao_requests`、`biz.users.pseudonym`、`biz.user_pseudonym_seq`（downgrade 會處理）。

## 八、豁免記載

- 是否有流程豁免：■ 有
- 事由與老師同意方式：學生於 2026-10-19 對話中表示「（老師）通過了，先跑」，即先實作、文件同步補齊。**AI 無法查證老師同意的方式與範圍，請學生在此補記（口頭／訊息／會議，日期）。**

## 九、簽核

| 角色 | 姓名 | 日期 | 結果 |
|---|---|---|---|
| 提案學生 | | | □ 確認 |
| 指導老師（Tier A/B 必填） | | | □ 核可　□ 退回 |

> 退回意見：
