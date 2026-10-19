# 設計文件：回答品質修正＋長照結構化資料分層（PostgreSQL catalog＋Neo4j）

> 日期：2026-10-19　｜　狀態：學生表示指導老師已同意豁免、先部署（見評估表第八節）
> 依據：[知識庫補強 GD 步驟 1–2 草稿](2026-10-19-kb-coverage-step1-2-draft.md)（118 題實測與瓶頸拆解）、[RAG 架構總覽圖](../architecture/rag-architecture-full.svg)（PostgreSQL catalog／biz／eval 分層、Neo4j 五種原子操作與 30 天軟到期）、[小照串接設計](2026-10-19-xiaozhao-fe-be-integration.md)

## 1. 背景與問題

題庫中預期直接回答的 118 題，實測只答出 13 題（11%）。拆解原因：

1. **忠實度檢查實作與設計不一致**：設計是「逐句」粗篩＋NLI 精篩，實作是「整段回答」比對「單一 chunk」的 bigram 重疊，證據足夠的 38 題有 28 題被擋。
2. **範圍外問題被誤轉督導**：模糊區（相似度 0.45–0.65）一律 `low_confidence`，沒有像「證據不足」分支一樣區分 `out_of_scope`。
3. **結構化資料不在知識庫**：補助額度、自付比例、交通分區規定在「長期照顧服務申請及給付辦法」**附表二、三、五**，現行爬蟲只收條文；據點的服務項目與服務範圍也沒有收錄。「給付與補助」0／21、「嘉義在地資源」2／7。
4. **說明性內容不在知識庫**：申請流程、喘息服務方式等只在衛福部 1966 長照專區。

學生指示：Neo4j **不做法規圖譜檢索**，改放**長照相關資料的分層**；PostgreSQL 依架構圖完成結構化分層。

## 2. 目標與非目標

**目標**
- 修正 1、2 兩項程式缺陷，不改變拒答政策（相似度門檻維持 0.55）。
- 補助額度、自付比例、交通分區、服務項目、據點服務範圍改由**結構化查詢直接回答**（不經 LLM，答案即資料、附出處）。
- PostgreSQL `catalog` 為結構化資料唯一真相來源，Neo4j 為訂閱者；補齊架構圖的 `catalog.sync_cursor`、`eval.alerts`，題庫匯入 `eval.golden_queries`。
- 1966 長照專區說明頁收進向量知識庫，標示為「說明文字（非法規）」。

**非目標**
- 不調整相似度門檻（需求決議「不確定一律轉督導」須老師另行決定）。
- 不導入 NLI 模型、cross-encoder（Advanced RAG 設計另案）。
- 不做法規條文的圖譜檢索；不部署 Elasticsearch。
- 不新增 Windows 排程（同步程式先手動執行，排程另行決定）。

## 3. 現況與重用

| 既有元件 | 處理 |
|---|---|
| `faithfulness.check_faithfulness` | 改為逐句、比對所有引用 chunk 的聯集；介面不變 |
| `pipeline.run_query_pipeline` | 新增結構化查詢分支；模糊區補 `out_of_scope` 判斷 |
| `catalog.ltc_points`、`ltc_points_crawler` | 擴充欄位（縣市／鄉鎮代碼、ABC、服務項目、服務範圍） |
| `ingestion/common.upsert_document`、`sync_worker` | 1966 說明頁沿用同一條 PostgreSQL→ChromaDB 路徑 |
| WSL 已安裝 Neo4j 2026.08.1、`neo4j` Python 驅動 5.24 | 沿用；啟用前改為只聽 127.0.0.1 |

## 4. 方案比較

### 4-1｜結構化資料的存放與查詢

| | 方案 A：PostgreSQL 型別表為真相來源＋Neo4j 分層圖譜（訂閱者） | 方案 B：只用 PostgreSQL 型別表 |
|---|---|---|
| 做法 | `catalog.ltc_*` 表存額度、比例、分區、服務項目；同步程式轉成 Neo4j 四層節點，查詢優先走 Neo4j | 同樣的表，查詢直接用 SQL |
| 優點 | 符合學生指示與架構圖（Neo4j 為訂閱者）；跨層關係查詢（等級→類別→比例→分區→據點）直觀；之後加關係不用改 schema | 少一個服務、少一段同步；部署最簡單 |
| 缺點 | 多一個服務要維運；需處理同步與一致性 | 不符合學生「Neo4j 做長照資料分層」的指示；多層關係查詢要寫多表 JOIN |

**選定方案 A**，並保留 B 當**降級路徑**：Neo4j 無法連線時，同一查詢改由 PostgreSQL 表回答，服務不中斷。

### 4-2｜忠實度檢查修正

| | 方案 A：逐句比對聯集（回歸原設計的粗篩） | 方案 B：LLM 自我檢查（TAIDE 判斷每句是否有依據） |
|---|---|---|
| 優點 | 不需新依賴、毫秒級；與原設計一致 | 語意判斷較準 |
| 缺點 | 仍是字面重疊，白話改寫多時可能誤擋 | 每題多一次 LLM 呼叫（+5～15 秒）；LLM 判斷本身也可能出錯 |

**選定方案 A**；B 列為之後 NLI 選型時的比較對象。

## 5. 系統設計

### 5.1 PostgreSQL 分層（migration `c3d4e5f6a7b8`）

**catalog（真相來源）**
- `ltc_benefit_categories`：給付類別（`care_pro` 照顧及專業服務 B/C 碼、`transport` 交通接送 D 碼、`assistive` 輔具及居家無障礙 E/F 碼、`respite` 喘息 G 碼），含週期、適用碼別。
- `ltc_quotas`：額度（類別 × 等級 × 分區／組別 → 金額、週期、`valid_from`、`valid_to`、出處）。
- `ltc_copay_rates`：部分負擔比率（類別 × 身分別 × 分區 → 百分比、出處）。
- `ltc_identity_classes`：長照身分別第一～三類定義（給付辦法第 14 條）。
- `ltc_transport_zones`：交通接送分區（縣市／偏遠鄉鎮 → 第一～四區，附表三）。
- `ltc_service_items`：服務項目（碼別、名稱、類別、價格、單位，例：GA03 日間照顧中心喘息服務-全日 1,250 元）。
- `ltc_points` 擴充：`county_code`、`town_code`、`town_name`、`abc_level`、`service_names text[]`、`service_area_codes text[]`。
- `sync_cursor`：各訂閱者（`neo4j`）的最後同步時間與版本（架構圖 catalog schema）。

**eval**
- `golden_queries` 補 `expected_behavior`、`expected_refusal_reason`、`question_id`（UNIQUE），由 `eval/golden_queries.csv` 匯入。
- `alerts`：評測告警（指標、數值、門檻、時間），架構圖「Recall/nDCG 下降 >10%、拒答率上升 >15%」寫入此表。

### 5.2 Neo4j 分層（長照資料，不含法規條文）

```
L1 制度層     (:Program {長照3.0})-[:HAS_CATEGORY]->(:BenefitCategory)
L2 服務層     (:BenefitCategory)-[:HAS_ITEM]->(:ServiceItem {code, name, price, unit})
L3 資格給付層 (:NeedLevel {level 2..8})-[:QUOTA {amount, period, zone?, group?}]->(:BenefitCategory)
              (:IdentityClass {1..3})-[:COPAY {rate_pct, zone?}]->(:BenefitCategory)
              (:EligibleGroup)-[:ELIGIBLE_FOR]->(:Program)
L4 資源地理層 (:County)<-[:IN_COUNTY]-(:Town)<-[:LOCATED_IN]-(:Provider {code, name, phone, abc})
              (:Provider)-[:SERVES_AREA]->(:Town)　(:Provider)-[:PROVIDES]->(:ServiceType)
              (:County|Town)-[:TRANSPORT_ZONE]->(:TransportZone {1..4})
```

- 每個節點與關係帶 `source_url`、`synced_version`、`valid_to`（NULL 為有效）。
- 同步採**全量比對**（資料量小）並以架構圖五種原子操作落地：ADD_ENTITY／UPDATE_ENTITY（`MERGE`＋`SET`）、ADD_RELATIONSHIP、EXPIRE_RELATIONSHIP（本次未出現者設 `valid_to`）、SOFT_DELETE（節點設 `valid_to`）；`valid_to` 超過 30 天者實際刪除。
- L4 據點：全國匯入，查詢時限嘉義縣市（服務範圍）。

### 5.3 查詢流程

```
pipeline：遮罩 → 身分 → 敏感 → 據點/結構化意圖 →（新）ltc_graph 結構化查詢 → 追問 → 快取 → RAG
```

- `app/core/ltc_graph.py` 以規則解析「類別（照顧／交通／輔具／喘息）＋問法（額度／自付／分區／價格）＋等級／身分別／縣市」；能確定時直接組答案，附出處（附表二／三／五、1966 頁面）；無法確定時回到原 RAG 路徑。
- 回答一律「資料即答案」，不經 LLM，故不跑忠實度檢查；`citations[].quote` 為來源表格的原文列。
- Neo4j 連不上時改查 PostgreSQL 型別表（降級），記錄一次警告。

### 5.4 忠實度與模糊區修正

- 回答依句號、問號、換行切句；去除少於 8 字的句子、引用行、免責聲明。
- 每句與「所有引用 chunk 的 bigram 聯集」比對，重疊 ≥ `faithfulness_citation_overlap_threshold`（0.4，不變）視為有依據。
- 有依據的句子比例 ≥ 0.8 即通過（新設定 `faithfulness_sentence_pass_ratio`，預設 0.8）。
- 模糊區：無領域詞 → `out_of_scope`（只記錄、不轉督導），有領域詞維持 `low_confidence`。

## 6. 安全與隱私考量
- Neo4j 只存公開資料（給付規定、據點公開資訊），不存使用者或對話資料。
- Neo4j 監聽位址改為 127.0.0.1（原設定 0.0.0.0）；帳密只放 `.env`。
- 1966 網站採政府資料開放授權條款第 1 版，回答附出處即符合授權條件。

## 7. 測試策略
- 單元測試：逐句忠實度、模糊區分流、結構化意圖解析與答案組裝（假資料源）、附表資料完整性（等級 2～8 皆有額度、比率 0～30%）、地址拆解。
- 整合驗證：Neo4j 同步後節點／關係數量與 PostgreSQL 一致；以 118 題重跑實測，記錄答出率與拒答原因分布，與 2026-10-19 基準（13／118）比較。

## 8. 部署與回滾
- 部署：`alembic upgrade head` → `python -m ingestion.run --source=ltc_benefits` → `--source=ltc_points` → `--source=ltc1966` → `--source=sync_worker` → `--source=graph_sync`。
- 回滾：`alembic downgrade a7c1d2e3f4b5`；Neo4j 執行 `MATCH (n) WHERE n.synced_version IS NOT NULL DETACH DELETE n`；`git revert`。結構化分支可用設定 `LTC_GRAPH_ENABLED=false` 關閉。

## 9. 未解決問題
- 長照「給付及支付基準」與預告中的「給付及支付標準」何者為現行版本，待確認；目前以給付辦法附表（全國法規資料庫，114-06-19 修正）為準。
- 據點「機構種類」代碼（1、2、BB…）官方對照表未取得，暫以「特約服務項目」中文名稱當服務類型，不使用推定的代碼名稱。
- 相似度門檻是否放寬、NLI 選型，須老師決定。
