# SQL Server Multi-Database Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 單一 MCP 提供同一 SQL Server 的多庫探索及已授權跨庫 SELECT JOIN。
**Architecture:** v3 profile 明確列出 databases，每次呼叫綁定獨立 Config、policy snapshot 與 cache。SQL 保留原文，逐一授權所有來源。
**Tech Stack:** Python、Pydantic、MCP stdio、pymssql、Questionary、Rich、pytest。
**Spec:** docs/specs/2026-11-06-sqlserver-multi-database-design.md

## Global Constraints

- v2、Oracle 與單庫呼叫保持相容。
- 多庫必須指定 database；沿用 query 參數。
- 不共用可變的目前 Database；worker 上限維持 process 總量。
- scope 採全域與每庫交集；未知來源拒絕；SQL 不改寫。
- 僅已明確授權的 Database 可使用；不新增 HTTP API。

## Review Focus

- 同名物件、大小寫與特殊識別字不能混用授權。
- 並行 request、policy reload 與背景工作不能共用錯誤 context。
- 不可用 Database 不得破壞其他庫；失敗不得觸發自動 SQL 重試。
- 更新配置不可丟失既有較嚴格 scope 或憑證。
- 錯誤、resources、metrics 與執行紀錄必須綁定正確 Database。

## Task 1: Profile 與 policy

Files: config.py, profiles.py, policy/models.py, policy/scope.py, policy/enforcer.py, services/policy_service.py；新增 tests/unit/test_multi_database_profiles.py。
Interfaces: ConnectionProfile.format_version 為 2|3；databases: list[str]；Config.databases: list[str]，Config.profile_version: int；PolicyScope.database_scopes: dict[str, DatabaseScope]。DatabaseScope 包含 allowed_schemas/allowed_tables/denied_tables。

- [x] 寫 v3 嚴格驗證、v2 相容及多庫 scope 交集測試，確認修改前失敗。
- [x] 實作 profile/policy 載入、序列化、更新與 scope 驗證；未知 Database 不放行。
- [x] 執行新增測試及原有 profiles/policy 測試。

## Task 2: Runtime 與 MCP 契約

Files: server/database_context.py（新增）, server/app.py, server/tools/__init__.py, server/tools/databases.py（新增）, server/resources/schema.py, server/prompts, main.py shutdown。
Interfaces: bind_database(database=None, reload=False) async context manager，選擇與授權後才初始化，將 _request_config/_request_context 綁定至 request。shutdown_database_contexts() 清理 lazy tasks；configured_databases(cfg) 與 available_databases(cfg, policy) 提供選庫。

- [x] 寫 Database 選擇錯誤、並行隔離、schema、list_databases、resources 與 reload 測試。
- [x] 實作 context 與有界 single-flight 初始化；Database 綁定在 recording 之前；單庫 Oracle 保持原路徑。
- [x] 所有工具宣告 database；多庫 mandatory；list_databases 不觸發 DB/cache。
- [x] 更新 resources/prompts、lazy background 與 shutdown；驗證工具／resource 相容。

## Task 3: Setup 與 doctor

Files: setup/sqlserver_wizard.py（新增）, setup/wizard.py, setup/terminal.py, setup/checks.py, main.py doctor；tests/unit/test_multi_database_setup.py。
Interfaces: gather_databases(profile,password,input_fn,emit) 回傳更新 profile；discover_databases(cfg) 列舉線上且可連線庫；check_database_set(cfg,database=None) 回傳逐庫診斷。

- [x] 寫鍵盤多選、手動名稱、既有 scope 保留、逐庫驗證與失敗復原測試。
- [x] 新版互動 setup 在 SQL Server 登入後選庫，建立一份 v3 profile 與單一 entry；保留程式化 v2 測試相容。
- [x] doctor 支援 --database 與逐庫安全診斷，失敗非零；摘要顯示 Database 清單。
- [x] 執行 setup/doctor 回歸與新增測試。

## Task 4: 跨庫 SQL 證據及整合

Files: services/sqlserver_database_proof.py（新增）, services/query_service.py, services/policy_service.py 的獨立整合 hooks, server/app.py query response hook；tests/unit/test_multi_database_query.py、MCP protocol tests、SQL Server integration fixture。
Interfaces: authorize_cross_database_query(cfg, policy, conn, sql) 回傳已授權 referenced_databases；使用執行連線 SYS catalog 驗證三段來源與 view/synonym 依賴；未知來源拒絕。

- [x] 寫已授權 JOIN、第三庫拒絕、間接依賴、特殊 identifier、單庫寫入限制與同名物件測試。
- [x] 授權所有來源，執行時在相同 connection 重驗證；所有模式使用相同 policy snapshot。
- [x] 確認 referenced_databases、metrics、recording 與原 SQL 保持正確。
- [x] 補充 README、v3 範例、執行完整單元／協定測試、Ruff、format、lock、build；如可用執行 disposable SQL Server 測試。
- [x] 完整差異審查，修正發現後提交；回報驗證範圍與未實機驗證項目。

## Execution ruling

使用者已明確要求「開始實作」，本計畫整理後直接執行，不再要求額外流程確認。依 subagent-driven-development 分工互不重疊檔案，整合階段再依測試與規格檢查。

## Verification record

詳見 `docs/validation-multi-database.md`。真實 SQL Server opt-in fixture 已補齊，當前環境未授權且 Docker daemon 未啟動，因此回報 skipped，不宣稱實機認證。
