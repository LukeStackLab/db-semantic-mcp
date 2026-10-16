# MCP 即時 SQLite 執行紀錄規格

狀態：依使用者逐項確認的需求整理；本文件不代表程式已實作。

## 1. 目的與邊界

IT 人員直接開啟現有 SQLite，查看 Agent 透過本 MCP 做過哪些探索、驗證及 SQL 操作。單一 VIEW 顯示台灣時間當天的過程，包含尚未結束的操作。

- 全部由 MCP 自動記錄，不要求 Agent 填參數或增加 instructions。
- 不新增 MCP tool，不修改工具 input/output schema、工具說明或回應內容。
- 不提供 `task_id`、`is_core`、核心 SQL 判斷或事後標記功能。
- 每次呼叫自動產生 `call_id`；不能宣稱不同呼叫屬於同一 Agent 任務。
- 不做 Web Monitor、額外紀錄檔或跨 SQLite 彙整。
- 只涵蓋工具呼叫；不擴大到獨立 resource/prompt 請求、外部 Agent 動作或資料庫 trigger/procedure 內部 SQL。

## 2. 不影響原操作與效能原則

優先維持原 MCP 的授權、交易、回應、例外、取消與 shutdown 行為。紀錄是可遺失的 best-effort 觀察資料，不能用作完整稽核或重試依據。

- 停用時直接走原路徑，不建立 recorder、佇列、執行緒或序列化資料；不執行新增紀錄 DDL。
- 啟用時工具與 SQL 開始/結束只非阻塞入列，不等待 SQLite 寫入或清理。
- 每個程序使用單一專用背景 writer thread；以有上限的 thread-safe queue 接收事件，不能使用無上限 executor 工作佇列。
- writer 收到事件立即處理並提交，不刻意累積批次、不使用固定 flush 間隔；提交前 VIEW 看不到該事件。
- 寫入連線 `timeout=0`、`PRAGMA busy_timeout=0`。鎖定、磁碟滿、唯讀、路徑不可用、DDL/序列化失敗時捨棄紀錄，不等待、不持續重試、不向工具拋錯。
- 預設佇列最多 1024 個事件、待處理內容預算 8 MiB；單一事件超過預算時整筆捨棄。不得為符合預算截斷 SQL 或參數。序列化採有界輸出累積，超限中止；不先建立無限大的字串再檢查。
- 警告每程序每 60 秒最多一次，只含原因類別與捨棄數，不含 SQL、參數、driver traceback。
- 不強制變更既有 SQLite journal mode、synchronous 或 cache 連線 busy timeout；不新增 `VACUUM`。共用檔案仍有 I/O 與鎖競爭，不能宣稱零成本或保證原操作絕無延遲。
- shutdown 不延長等待以排空紀錄，不取消仍在執行的 DB worker。結束事件來不及寫入時可遺失。

## 3. 設定與現有 SQLite

`Config` 與 `ConnectionProfile` 新增 `execution_recording_enabled: bool = False`。固定配置 JSON 同名欄位；既有 JSON 缺少欄位視為 false。既有環境變數模式沿用 `SEMANTIC_MCP_EXECUTION_RECORDING_ENABLED`，固定 profile 仍不受環境覆蓋。

- 安裝精靈新增「啟用 SQLite 執行紀錄？」選項，新增預設否、編輯預設既有值；重用既有原子寫入、備份與失敗回復流程。
- 修改開關後重啟生效；停用只停止新紀錄，不立即刪除既有資料。
- 保留天數、queue 容量與警告頻率第一版為內部常數，不增加精靈選項。
- 使用有效配置的 `cfg.cache_path`，包括現有 source/policy scope；不更動 cache fingerprint，不讓開關切換造成 cache 路徑改變。
- 每次呼叫在入口固定使用當時 base 配置已解析的 `cfg.cache_path` 作為紀錄目的地，整次呼叫不換檔；source 路由期間讀取另一個 scope 的 cache 也寫入同一紀錄目的地。這讓 routing 尚未完成時就能顯示工具開始，並在同一 VIEW 查看整次呼叫。後續配置或權限切換造成 base cache 改變時，新呼叫才跟隨新路徑。不跨檔複製歷史。
- recorder 的 DDL 由背景 writer 獨立、冪等初始化，不接入 `init_store()` 的必要成功路徑。失敗時可停用該檔的紀錄直到下次程序啟動，不影響 MCP startup 或 source 初始化。
- 沿用既有檔案，不另建 SQLite；檔案不存在時允許丟棄事件，不為紀錄重建 cache。刪除或切換整個 cache 檔也會失去舊紀錄，不搬移。
- 新表不得加入 metadata 的 `SCHEMA_TABLES`、結構 fingerprint 或 cache refresh 刪除清單。

## 4. 紀錄模型

表名 `mcp_execution_log`，VIEW 名 `v_mcp_execution_today`。沒有 MCP/連線名稱欄位。

| 欄位 | 型別／規則 |
|---|---|
| `id` | TEXT PRIMARY KEY，程序端 UUID；不用等 SQLite 回傳 ID |
| `call_id` | TEXT NOT NULL，每次工具呼叫一個 UUID |
| `sequence_no` | INTEGER NOT NULL，同呼叫內分配的遞增序號，須 thread-safe |
| `record_type` | TEXT NOT NULL，`tool` / `sql` |
| `tool_name` | TEXT NOT NULL，原工具名稱 |
| `step_name` | TEXT NOT NULL，固定簡短名稱，不呼叫模型產生 |
| `action_type` | TEXT NULL，`探索` / `驗證` / NULL |
| `source_type` | TEXT NULL，SQL 列為 `cache` / `db`；工具列留空 |
| `started_at` | TEXT NOT NULL，固定格式台灣時間 `YYYY-MM-DDTHH:MM:SS.ffffff+08:00` |
| `finished_at` | TEXT NULL，同上格式 |
| `duration_ms` | REAL NULL，以 monotonic clock 計算 |
| `status` | TEXT NOT NULL，`running` / `success` / `failed` / `cancelled` / `unknown` |
| `sql_text` | TEXT NULL，原始完整 SQL；工具列為 NULL |
| `parameters_json` | TEXT NULL，完整綁定參數與型別資訊；無參數為 NULL |
| `summary` | TEXT NOT NULL，最多 200 字元的極簡摘要 |

建立 `(started_at DESC, id DESC)` 與 `(call_id, sequence_no)` 索引。SQL/參數無字數截斷；summary 可截斷。

每次工具呼叫固定一列 `tool`，即使沒有 SQL、驗證拒絕或失敗仍有紀錄。每次實際 SQL execution 另有一列 `sql`，與工具列共用 `call_id`。同次呼叫同時查 cache 與 db，分開記錄每條 SQL，不以單一來源概括。工具列是整體步驟，SQL 列是明細；不要再製造一組重複的來源摘要列。

工具開始前配置 recorder context；避免在首次 SQL 之後才產生工具列。工具與 SQL 各自開始入列 `running`，結束更新原 `id`。若開始事件丟失，結束事件以完整 payload UPSERT 補成終態；若只有結束事件丟失，保留 running，不猜測仍在執行。SQLite recorder 更新自身不得被攔截。

開始/結束事件的 SQL 與參數快照共用不可變 payload，避免每次重複複製。後續事件不得把已結束紀錄降回 running。

## 5. 自動分類與記錄範圍

- 以下工具屬探索：`get_tables`、`describe_table`、`describe_object`、`get_table_relationships`、`get_dependency_chain`、`detect_lookup_tables`、`find_join_path`、`discover_relevant_tables`。
- `plan_or_execute_query` 屬驗證，包含 validate/dry_run/執行。這是操作分類，不代表已執行或結果正確。
- 其他工具分類 NULL；例如 `refresh_schema_cache` 仍記錄，只是不強行歸入探索。
- SQL 繼承工具分類；`source_type` 根據實際連線判定，SQLite 是 cache，目標 SQL Server/Oracle 是 db。純語法處理無 SQL，只有工具列。
- cache 列包含工具衍生的實際 SQLite SELECT；必要的 cache 讀寫 SQL 亦如實記錄，不能宣稱全部 SQL 卻只攔截 SELECT。
- 包含來源解析、inline metadata revalidation、按需 warmup、result schema 檢查等由當次請求直接觸發的 SQL。
- 排除啟動暖機、獨立背景更新、recorder DDL/INSERT/UPDATE/清理、既有 metrics 的記錄寫入。Agent 主動呼叫 `tool_metrics` 的查詢仍記錄。
- 背景 task 常會繼承 ContextVar：建立獨立背景工作時必須明確清除 tracing context；由呼叫等待的 source 初始化工作則保留因果 context。
- 已取消工具的同步 DB worker 可能仍繼續執行。保留 worker 的原 call context，不提前把 SQL 標記取消或已回滾；以實際 worker 結果更新 SQL。

## 6. SQL、參數與結果語意

- 記錄 driver 實際收到的原始 SQL 與綁定參數，分欄保存；不得用字串替換拼造「展開 SQL」。沒有參數時保存完整原文。
- tuple/list、named binds 與 executemany 多組參數都保留順序/名稱；型別涵蓋 NULL、bool、int、float、str、bytes/base64、Decimal、date/time/datetime。無法無損表示的 driver-specific 物件捨棄該紀錄，不以 repr 冒充完整參數，不執行任意物件方法、不消耗 iterator/LOB/file stream。
- 需要消耗參數 stream 才能記錄時，捨棄該紀錄，不能改變 driver 接收的參數。沒有「完整」資料時不得悄悄改成截斷版本。
- 驅動連線設定本身不記錄。SQL/參數內的敏感值依使用者要求原樣保存；不得因此新增到 stderr 或 MCP 回應。
- SQL 開始緊鄰 execute 前；結束定義為該次 execute/executemany 返回或拋錯。後續 fetch 時長包含在工具耗時，不假裝 SQL execute 耗時是完整資料傳輸耗時。
- SQL `success` 僅表示 execute 成功；summary 顯示「SQL 執行完成」，不得推定 transaction commit。工具 summary 從既有結果抽出回傳/影響筆數或交易結果未知等資訊，不額外 fetch、count、驗證或重跑 SQL。
- 已知失敗使用簡短安全錯誤類別；不為記錄增加 driver traceback。寫入結果未知沿用原服務語意為 unknown，絕不建議自動重試。
- 不保存完整資料列，不保存整份工具回應。工具摘要例如「找到 5 張表」「成功，回傳 20 筆」「失敗：policy_denied」。

## 7. VIEW 與保留期限

VIEW 僅按 `started_at` 的台灣當日邊界篩選，包含所有狀態與類型，不自動分組或只挑最新呼叫。時區固定 +08:00，不依 SQLite host 的 localtime。

邊界可由 SQLite `date('now', '+8 hours')` 推導後，組成與 `started_at` 相同格式的起訖常數；在 WHERE 使用 `started_at >= start AND started_at < end`，不要對每列套 date 函式，保留索引使用機會。顯示時 `ORDER BY started_at DESC, id DESC`；自行寫外層 SQL 要保證順序時仍明確加 ORDER BY。

今日以開始時間認定。跨午夜仍執行中的前一日步驟，不會出現在隔日 VIEW；不改成按更新時間篩選。

保留今天與前六個日曆日。每個 recorder 路徑在啟動及後續事件時檢查，台灣日曆日最多清理一次；背景刪除 `started_at < 今日零時減六日` 的資料。長時間完全閒置時不另設排程，期限到後等下次啟動/寫入才清理。開始事件已過保留期限的晚到更新不重建舊紀錄。

## 8. 架構與驗收界線

以專用 recorder 模組管理 ContextVar、呼叫/SQL span、有限 queue、型別序列化與 writer。DB-API connection/cursor 與 aiosqlite 的局部 adapter 負責攔截，禁止全域 monkeypatch 第三方套件。代理必須完整轉送屬性、context manager、async iterator、fetch、commit、rollback 與 close，不能改變 driver 行為。

Oracle connect 內有 session SQL，需在該 execute 邊界接入，不能只包 open_connection 後就宣稱涵蓋。SQLite connection factory 必須覆蓋 services 與 cache/probe 直接 connect 的路徑，並排除內部 metrics/recorder。

驗收必須證明：工具契約未變、off 零紀錄工作、on 故障隔離、開始事件可在操作完成前被另一 SQLite connection 讀到、多 SQL 與混合來源可追蹤、完整參數、跨時區/午夜與七天清理、source scope、取消及未知交易結果不變。

量測 off/on 的工具延遲、cache/DB 操作延遲、queue backlog/drop 與寫入可見延遲；不得為了通過數字放寬原有 query timeout 或改交易語意。沒有實測前不承諾效能百分比；量測結果與共用 SQLite 鎖競爭列入交付報告。
