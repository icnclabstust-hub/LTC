# 小照服務（LINE Bot＋LIFF 網頁）

設計文件：`docs/designs/2026-10-08-ltc-awe-liff-recording-and-line-cases.md`
評估表：`docs/designs/2026-10-08-ltc-awe-liff-recording-and-line-cases-assessment.md`

## 內容

| 檔案 | 用途 |
|---|---|
| `app.py` | FastAPI：LIFF 網頁 API、`/callback` LINE webhook、`/liff/` 靜態網頁 |
| `pipeline.py` | 錄音模式串流處理：KWS＋VAD 切輪、Paraformer、聲紋、意圖（BERT A1）、預取；今日狀況回報 |
| `endpointing.py` | 自適應切句／切輪門檻（`ENDPOINT_MODE=adaptive`，2026-10-12 spike） |
| `engines.py` | 模型封裝（sherpa-onnx、BERT A1_s42） |
| `line_service.py` | webhook 驗簽與去重、LINE 案件、reply／push、依身分切換選單 |
| `richmenu.py` | 建立／刪除三種身分的聊天室選單 |
| `services.py` | 帳號與綁定碼、督導請求、確認／修正紀錄（records）、保存期限清除 |
| `auth.py` | LIFF ID token 驗證、身分與權限 |
| `backend.py` | 後端呼叫端（目前只有模擬後端） |
| `static/` | LIFF 網頁（照護員、督導、管理員頁面；AudioWorklet 錄音） |

## 需要的檔案

- 模型：`android/AweIntentSpike/model-downloads/`（VAD、Paraformer、KWS、ERes2Net 聲紋）與 `exported_bert_care7/A1_s42/model`
- `.env`：複製 `.env.example` 後填入；**`LINE_ADMIN_USER_IDS` 須填管理員的 LINE userId**

## 安裝（Windows、Python 3.14）

```
py -3.14 -m venv .venv-xiaozhao
.venv-xiaozhao\Scripts\python -m pip install -r requirements-xiaozhao.txt --extra-index-url https://download.pytorch.org/whl/cpu
```

## 第一次設定 LINE（只做一次）

1. LINE Developers Console：
   - Messaging API channel → Webhook URL 設為 `https://<NGROK_DOMAIN>/callback`，開啟 Use webhook；關閉自動回覆訊息。
   - LINE Login channel → LIFF → Endpoint URL 設為 `https://<NGROK_DOMAIN>/liff/`，Scope 勾選 `openid`、`profile`。
2. 建立選單：`.venv-xiaozhao\Scripts\python -m xiaozhao.richmenu setup`（在 `src` 目錄下執行，或先設 `PYTHONPATH=src`）。

## 啟動與停止

```
.venv-xiaozhao\Scripts\python -m uvicorn xiaozhao.app:create_app --factory --app-dir src --host 127.0.0.1 --port 8010
ngrok http --url=<NGROK_DOMAIN> 8010
```

不用 ngrok 時，可改用 Cloudflare 免費通道（不需帳號，但每次網址會變）：先 `winget install --id Cloudflare.cloudflared`，
啟動服務後按兩下 `tools\start_cloudflare_tunnel.cmd`。腳本會自動把 LINE Webhook 設成新網址並測試；
LIFF 的 Endpoint URL 則要依腳本印出的網址到 LINE Login channel 手動更新。

第一次啟動載入模型約 20 秒；載入完成前，錄音相關 API 會回 503。
停止：Ctrl+C 結束服務並關閉 ngrok。

## 查看送給後端的 JSON

服務會把每次送給後端（目前為模擬後端）的 JSON 與回應寫入 `xiaozhao_data/backend_requests-YYYYMMDD.jsonl`（一行一筆，保存 1 天），終端機也會印出「送後端 query／scene：…」。`.env` 設 `BACKEND_LOG=0` 關閉。

## 測試

```
.venv-xiaozhao\Scripts\python -m pytest tests/xiaozhao
```

本機不經 LINE 試用網頁：`.env` 設 `XIAOZHAO_DEV_FAKE_AUTH=1`，瀏覽器開 `http://127.0.0.1:8010/liff/?dev=<任意 userId>`（只接受直接連 127.0.0.1 的請求；用手機測試前請關閉）。

## 自適應切句／切輪（2026-10-12 spike）

設計文件：`docs/designs/2026-10-12-ltc-awe-adaptive-endpointing-spike.md`

- `.env` 設 `ENDPOINT_MODE=adaptive` 開啟（預設 `fixed` 為原行為）。
- 離線比較兩種模式（錄音 16 kHz 單聲道 16-bit、須含「小照你好」，放 `endpoint_recordings/`，不進版控）：

```
.venv-xiaozhao\Scripts\python replay_endpointing.py endpoint_recordings\talk1.wav --out endpoint_recordings\talk1_out --ref endpoint_recordings\talk1_ref.csv
```

人工標註 CSV 欄位 `start_s,end_s,turn`（每句一列）；輸出 `fixed.csv`、`adaptive.csv`、`summary.json`（句子／輪邊界 F1、聲紋耗時）。

## 回滾與清理（評估表第七節）

- 停止服務並關閉 ngrok；`python -m xiaozhao.richmenu teardown` 刪除選單；Console 關閉 webhook。
- 刪除 `xiaozhao_data/`（SQLite：帳號、聲紋、所有紀錄）。

## 已知限制

- 後端為模擬後端（`BACKEND_MODE=mock`），回答內容不是真實法規；後端組介面確認後再接。
- 喚醒詞靈敏度 2.0／0.2（2026-10-13 以自錄 8 段調校），KWS 漏抓時以 ASR 補抓（輪外片段只轉開頭 2 秒）；誤觸發只測過 10 分鐘。
- 預取門檻暫定 0.9，須以交叉驗證資料訂定。
- 說話人：2026-10-13 起不再長期保存聲紋，改為每次錄音內匿名代號 01、02…（整句聲紋即時分群，門檻 0.5，錄音結束即丟）；照護員在「確認紀錄」分頁把代號對應到身分。舊資料庫啟動時會刪除 `voiceprints` 表，升級前請先備份 `xiaozhao_data/`。
- TTS 語音播放未實作（待辦）。
