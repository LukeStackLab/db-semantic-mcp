# MCP 即時 SQLite 執行紀錄 Implementation Plan

> **For agentic workers:** 逐項執行本計畫，先讀規格與適用的 AGENTS.md。使用者指定在新的 session 人工啟動開發；本次只交付文件，不啟動實作。預設在新 session 直接循序執行，不自行要求改用多 Agent。

**Goal:** 自動將 MCP 工具與 SQL 開始/結束紀錄寫入現有 SQLite，讓 IT 使用單一當日 VIEW 查看。

**Architecture:** ContextVar 將工具呼叫串到 DB worker 與局部 SQL adapter。有限佇列交由專用 writer thread 立即逐事件提交；所有紀錄失敗與原操作隔離。設定、安裝精靈與舊配置同步支援，工具契約完全不變。

**Tech Stack:** 現有 Python >=3.11、sqlite3、aiosqlite、ContextVar、threading/queue、Pydantic、pytest/pytest-asyncio、ruff；不新增 runtime dependency。

**Spec:** [執行紀錄規格](../specs/2026-10-16-execution-recording-design.md)

## Global Constraints

- 預設 `execution_recording_enabled=False`；固定 profile 不受環境覆寫；停用不建立 recorder 或執行新增 DDL。
- 不新增 tool、工具參數、工具回應、`task_id`、`is_core`、Agent instructions 或外部監控服務。
- 沿用現有 cache 檔；每次呼叫入口固定紀錄路徑；不改 cache identity。
- 每次工具固定一列，實際每條 SQL 各一列；開始 running、結束更新，call_id 串接。
- SQL/參數完整保存，不能截斷；不保存結果資料列；summary 上限 200 字元。
- queue 上限 1024 事件與 8 MiB 內容預算；超限整筆丟棄；背景立即提交，不等待 batch interval。
- writer SQLite timeout 與 busy_timeout 為 0；無持續重試，stderr 每 60 秒最多一次。
- 停用原行為、政策、交易、取消、source scope 與 shutdown 不變；不等待紀錄排空。
- 台灣當日 VIEW，依 started_at DESC、id DESC；保留今日與前六個日曆日，每路徑每天清理最多一次。
- 不強制改 journal/synchronous，不 VACUUM，不把新增表加入 cache 清理/fingerprint。
- 只提交本次變更，不 push。每個任務完成可建立獨立 commit。

## Review Focus

1. Oracle routing/inline 初始化在 handler 之前執行：工具開始必須先被記錄；同次呼叫仍固定原紀錄目的地。由 Task 4 測試。
2. 已取消 caller 的同步 worker 仍執行：SQL 結果應依 driver 實際完成，不被誤標回滾/取消。由 Task 4 測試。
3. 完整參數包括 bytes、Decimal、named binds、stream：保留可序列化值；不能消耗 stream 或影響 driver。由 Task 2、3 測試。
4. 舊快取、唯讀/鎖定檔、遺失 start/end、source 換檔：失敗不影響工具且不製造錯誤終態。由 Task 1、2、4 測試。
5. 非台灣 host 時區、跨午夜、晚到終態及閒置七天：VIEW 邊界與保留期限須一致。由 Task 1、2 測試。

## 檔案與責任

新增模組：

- `db_semantic_mcp/infrastructure/recording/schema.py`：獨立 DDL、VIEW、保留期限常數。
- `db_semantic_mcp/infrastructure/recording/events.py`：不可變事件、呼叫 context、型別參數快照與序列化。
- `db_semantic_mcp/infrastructure/recording/writer.py`：有限 queue、獨立 SQLite writer、清理、失敗隔離。
- `db_semantic_mcp/infrastructure/recording/context.py`：tool/sql span、ContextVar、內部抑制 scope。
- `db_semantic_mcp/infrastructure/recording/dbapi.py`：同步 connection/cursor 局部代理。
- `db_semantic_mcp/infrastructure/recording/sqlite.py`：aiosqlite connection/cursor 局部代理與 connect factory。
- 各新增 package 補 `__init__.py`；避免把 writer/driver imports 放入停用路徑。

既有修改點：

- `config.py`、`profiles.py`、`setup/wizard.py`、`config/connection.sqlserver.example.json`：設定流。
- `server/app.py`：在 `_dispatch_tool` 最外層開始/結束呼叫，不只包 `_dispatch_bound_tool`。
- `infrastructure/connection.py`、`infrastructure/backends/oracle.py`：同步連線與 connect 內 session SQL。
- `services/{metadata,semantic,relationship,metrics}_service.py`、`services/oracle_metadata.py` 與 `infrastructure/cache/{structural,semantic,probe}.py`：盤點所有 aiosqlite.connect 並改用局部 factory。
- `infrastructure/cache/revalidation.py`、`infrastructure/background.py`、`server/source_context.py`、`main.py`：背景 context、生命週期、scope 與清理排除驗證。
- `README.zh-TW.md`、`README.en.md`、`README.md`：依現有三份文件關係同步設定與 VIEW 用法。

修改清單是已定位入口；實作開始先用 rg 完整盤點，新增找到的入口必須記入交付報告，不可只修改列出的檔案就宣稱全覆蓋。

### Task 1：Schema、當日 VIEW 與保留邊界

**Files:** 新增 schema.py、`tests/unit/test_recording_schema.py`。不修改 `cache/store.py` 必要初始化成功路徑。

**Interfaces:**
- `initialize_recording_schema(conn: sqlite3.Connection) -> None`，冪等建立規格表、兩個索引與單一 VIEW。
- `retention_cutoff(now: datetime) -> str`，aware datetime 轉台灣今日零時減六日，回傳規格固定格式。

- [ ] 寫失敗測試：舊 cache 可新增表且原表內容不變；連續初始化兩次不重建資料。斷言欄位與索引符合規格。
- [ ] 寫 VIEW 測試：以測試自建 SQLite connection 覆寫日期 clock function，固定 now 驗證台灣 00:00 邊界、所有狀態、tool/sql、排序；正式 VIEW 仍使用 SQLite now，不依 Python 註冊函式。
- [ ] 寫 retention 測試：今天與前六日保留，第七日前刪除；跨午夜依 started_at 分日。
- [ ] 執行 `python3 -m pytest tests/unit/test_recording_schema.py -q`，確認新增介面不存在時失敗。
- [ ] 實作 DDL、日期 helper 與 VIEW；WHERE 使用可索引起訖比較。測試以明確 ORDER BY 的外層查詢驗證穩定順序。
- [ ] 重跑測試至通過，確認 EXPLAIN QUERY PLAN 使用時間索引；提交此任務檔案。

### Task 2：非阻塞 recorder 與故障隔離

**Files:** 新增 events.py、writer.py、context.py 與 `tests/unit/test_recording_writer.py`、`tests/unit/test_recording_context.py`。

**Interfaces:**
- `RecordingEvent`：不可變開始/終態 payload，涵蓋規格全部欄位與固定 sink_path。
- `Recorder.try_emit(event: RecordingEvent) -> bool`：非阻塞、有界；不拋例外到 caller。
- `get_recorder() -> Recorder`：只在啟用路徑 lazy 建立；程序單一 writer、按既有路徑快取連線。
- `record_tool(cfg: Config, tool_name: str)`：async context manager，產生 call_id 並 expose `finish(status: str, summary: str) -> None`。
- `record_sql(source_type: str, sql: str, parameters: object = None)`：sync context manager，同上 finish；無啟用 context 時為 no-op。
- `suppress_recording()`：sync context manager，供 recorder/metrics/獨立背景工作使用。
- `stop_recording() -> None`：非阻塞停止接收，不等待排空；writer thread 可 daemon，不能留下未關閉的主線程資源。

- [ ] 測試停用不啟動 writer、不序列化；開始提交後另一 connection 可在 finish 前看到 running。
- [ ] 測試 writer execute 以 barrier 阻塞時，try_emit/tool 操作可完成；queue 1024/8 MiB/單事件超限會丟棄且不截斷資料。
- [ ] 測試唯讀、鎖定、DDL 失敗、磁碟錯誤與不可序列化物件不影響原結果；warn 限頻且不洩漏 SQL/參數。
- [ ] 測試 end-only UPSERT、start-only running、終態不倒退、晚到過期事件不復活；每日清理觸發與失敗不忙迴圈。
- [ ] 測試 Decimal/date/bytes/named binds 完整型別表示，無參數 NULL；stream 不被讀取、單事件過大安全丟棄。設定 queue byte budget 的測試使用注入小值，正式常數仍為 8 MiB。
- [ ] 執行 `python3 -m pytest tests/unit/test_recording_writer.py tests/unit/test_recording_context.py -q` 確認先失敗。
- [ ] 實作有界參數快照、writer SQLite 短交易及抑制 scope。用 monotonic clock 算耗時；正常執行不持有 writer mutex 跨越原 DB 呼叫。
- [ ] 重跑至通過並提交。記錄 producer 快照成本與無法完整序列化時的捨棄政策，不改成無界資料複製。

### Task 3：攔截實際 SQL，完整轉送 driver 行為

**Files:** 新增 dbapi.py、sqlite.py、`tests/unit/test_recording_dbapi.py`、`tests/unit/test_recording_sqlite.py`；修改前述 connection/backend/services/cache SQL 入口。

**Interfaces:**
- `instrument_db_connection(conn: Any) -> Any`：有 context 時回局部代理，否則原物件；source=db。
- `recording_connect(database: str, **kwargs)`：相容原 aiosqlite.connect 的 awaitable/async-context-manager 工廠；source=cache，無 context 時原生連線。
- 保留 execute/executemany/executescript 的真實語意；executescript 若不能逐 statement 攔截，內部 schema/maintenance 應明確抑制，Agent 路徑若存在則改為已知 SQL 清單逐條執行，不能自行用分號切 SQL。

- [ ] 測試 sync/async execute、executemany、cursor.execute、connection.execute、async iterator、await connect、async with connect，以及屬性設定、fetch、commit/rollback、close 均只呼叫原方法一次且結果/例外一致。
- [ ] 測試三條 SQL 得三列、cache+db 各自來源、相同 call_id 與不重複攔截；execute 返回即結束，fetch 不額外消耗。
- [ ] 測試 SQL 長度大於既有 output cap 仍完整保存、參數型別順序不變、不改 placeholder、不消耗 iterator。
- [ ] 測試 Oracle catalog borrowed conn、result-schema SQL、Oracle thick-mode session SQL、SQLite metadata/service/probe 入口都能記錄。
- [ ] 執行新增 adapter 測試，確認先失敗，再實作局部代理與 factory；不得 monkeypatch sqlite3/aiosqlite/oracledb/pymssql 全域 API。
- [ ] 用 `rg -n 'aiosqlite.connect|cursor.execute|executemany|executescript|\.connect\(' db_semantic_mcp` 逐項標記覆蓋或排除原因；補缺口並加入對應測試。
- [ ] 執行新增測試及 `python3 -m pytest tests/unit/test_connection.py tests/unit/test_oracle_backend.py tests/unit/test_oracle_catalog.py tests/unit/test_structural_cache.py tests/unit/test_metadata_contract.py -q`，通過後提交。

### Task 4：工具生命週期、背景與 source 整合

**Files:** 修改 app.py、main.py、workers.py（僅必要 context 整合）、source_context.py、revalidation.py、background.py、metrics_service.py；新增 `tests/unit/test_execution_recording.py`。

**Interfaces:** 使用 Task 2 context API；沿用 workers.py 的 copy_context，不建立第二組 DB executor。摘要 helper `summarize_tool(tool_name: str, result: object) -> str` 放 context.py，只讀既有結果，不能查 DB。

- [ ] 測試所有已註冊工具固定有 tool 列；無 SQL、政策拒絕、來源拒絕、未知工具均能保留簡短結果，且原 MCP 回應與停用時相同。
- [ ] 測試 outer dispatch 在 bind_source 前已入列開始；工具/衍生 SQL 固定寫入入口 cache_path，後續呼叫配置切換才換檔；不在 source 綁定後新增第二列工具。
- [ ] 測試分類清單、step_name、摘要最多 200 字；cache refresh 不刪紀錄、不改 fingerprint。
- [ ] 測試 caller 取消後 DB worker 繼續，工具 cancelled 與 SQL 實際終態可分開；write_outcome_unknown 不改寫為 success/rollback。
- [ ] 測試 startup 暖機、獨立背景 work、metrics INSERT、recorder SQL 不自我記錄；inline revalidation/按需 source 初始化則有紀錄。獨立背景 task 即使由呼叫內建立也不繼承 call context。
- [ ] 執行新增測試確認先失敗，再接 outer tool span、context 清理與 shutdown。原生錯誤傳遞/例外控制流程不因 recorder catch-all 改變；catch-all 僅包紀錄程式。
- [ ] 執行 `python3 -m pytest tests/unit/test_execution_recording.py tests/unit/test_workers.py tests/unit/test_source_context.py tests/unit/test_revalidation.py tests/unit/test_call_tool_metrics.py tests/unit/test_query_results.py -q`，通過後提交。

### Task 5：配置與安裝精靈

**Files:** 修改 config.py、profiles.py、setup/wizard.py、config 範例；擴充 test_config.py、test_profiles.py、test_setup_wizard.py、test_cache_identity.py。

**Interfaces:** `Config.execution_recording_enabled: bool = False`；`ConnectionProfile.execution_recording_enabled: bool = False`；`config_from_profile()` 明確傳遞；精靈沿用 `_confirm`。

- [ ] 測試舊 JSON 缺欄位為 false、新增可選 true/false、編輯保留值、安裝失敗 rollback 還原原設定。
- [ ] 測試固定 profile 忽略 env/dotenv；legacy env 模式接受現有 prefix；true/false 不改 cache identity、tool profile 或 policy。
- [ ] 執行上述四組測試確認新增案例先失敗，再接設定與 wizard prompt，更新 scripted inputs 不可刪掉舊測試涵蓋。
- [ ] 重跑四組測試及 Task 4 啟用/停用測試至通過，提交設定整合。

### Task 6：完整驗收、效能與操作文件

**Files:** 更新 README 三語檔與新增 `docs/validation-execution-recording.md`；可新增 `tests/integration/test_execution_recording.py`（真 DB 沿用既有 opt-in，不可自動連正式 DB）。

- [ ] 用實際暫存 SQLite 與可控 fake DB barrier 跑端到端：工具未完成時能查到 running、cache+db 多 SQL、結束後摘要、鎖定仍回傳原工具結果、沒有新增 MCP schema 欄位。
- [ ] 與基準比較 list_tools descriptor 與完整工具回應；不是只比 tool 數量，確認沒有增加 Agent token payload。
- [ ] 量測同負載 off/on 工具延遲 p50/p95、producer 入列時間、commit 可見延遲、drop 數、SQLite 讀寫延遲；不設未經量測的改善百分比。報告硬體/樣本數/負載與共用檔案鎖競爭限制。量測不得讓正式 response 增加診斷欄位。
- [ ] 文件說明開關、重啟、檔案定位、VIEW 與完整 SQL/參數、來源/分類/狀態、七天保留及資料可遺失。給出 `SELECT * FROM v_mcp_execution_today ORDER BY started_at DESC, id DESC;`。
- [ ] 執行 `python3 -m pytest tests/unit -q`、`python3 -m ruff check .`、`python3 -m ruff format --check .`。若 repo 基準已有失敗，分別列出，不擅自修無關內容。實際 DB 測試未跑要明示。
- [ ] 完成規格逐條對照與 SQL 入口覆蓋清單，更新驗證文件，僅 stage 本任務檔案並 commit，不 push。
- [ ] 最終回報完成項目、主要檔案、測試、效能量測限制、尚未解決問題與 commit 短雜湊。

## 計畫自我審查

- 規格 1–8 節均有 Task 對應；設定在 Task 5、紀錄/保留在 Task 1–2、SQL 在 Task 3、生命週期在 Task 4、契約/效能在 Task 6。
- 全部 review focus 都有指定測試；同次呼叫以固定 sink 保證開始可見與資料可查，不在 source routing 後移動歷史。
- 所有新增介面都在首次產生的 Task 定義；後續 Task 只消費同名介面。
- 不包含 runtime 套件升級、Web、額外 MCP tool、Agent task/core 標記與完整 row 保存。
