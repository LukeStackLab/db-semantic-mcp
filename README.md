# db-semantic-mcp

讓 AI agent 用 MCP 探索 SQL Server 與 Oracle、查找關聯，並在明確授權內執行 SQL。預設唯讀。支援 Codex、Claude Code、Claude Desktop 與 Cursor。

[English](README.en.md) · [Oracle 實機測試](docs/testing-oracle.md) · [0.8.0 驗證紀錄](docs/validation-0.8.0.md)

相容目標為 SQL Server 2017+、Oracle 19c+、Python 3.11+。本次實機測試使用 SQL Server 2022、Oracle 23.26.3，以及 Oracle Thin／Thick、TCP／TCPS。Oracle 19c 原版與 LDAP current-user DB link 尚未實機認證。

## 安裝 0.8.0

產品、CLI 與 MCP server 名稱為 `db-semantic-mcp`，Python module 為 `db_semantic_mcp`，distribution 為 **`lukestack-db-semantic-mcp`**。本版尚未發布到 PyPI，請從原始碼或自行建置的 wheel 安裝。PyPI 通用名稱已由其他專案使用。GitHub repository URL 維持原名。

先安裝 [uv](https://docs.astral.sh/uv/getting-started/installation/)，再執行：

```bash
git clone https://github.com/LukeStackLab/sqlserver-semantic-mcp.git db-semantic-mcp
cd db-semantic-mcp
# 僅 SQL Server，不需 Oracle driver 或 Oracle Client
uv tool install --python 3.11 --reinstall .
# Oracle 支援，仍包含 SQL Server
uv tool install --python 3.11 --reinstall '.[oracle]'
db-semantic-mcp --version
db-semantic-mcp --help
```

若 uv 顯示 PATH 提示，執行 `uv tool update-shell` 並重開終端機。也可自行建置：

```bash
uv build
uv tool install --reinstall './dist/lukestack_db_semantic_mcp-0.8.0-py3-none-any.whl[oracle]'
```

Oracle 預設 Thin，不需 Oracle Client。Thick 需安裝相容的官方 Oracle Client，明確設定 `driver_mode: "thick"`，必要時指定 `client_lib_dir`。模式屬於整個 process；切換模式時使用新的 MCP process。參考官方 [安裝](https://python-oracledb.readthedocs.io/en/stable/user_guide/installation.html)與 [初始化](https://python-oracledb.readthedocs.io/en/stable/user_guide/initialization.html)。

## 精靈與獨立配置

```bash
db-semantic-mcp setup
# 選擇較小的工具集合
db-semantic-mcp setup --tool-profile explore
```

精靈使用繁體中文。選擇 Client、資料庫類型與 MCP 名稱，再輸入端點及憑證。選擇唯讀或受限寫入，最後確認摘要。Oracle 支援 Thin／Thick、service／SID／TNS、TLS，以及明確授權既有 DB link。精靈只測試登入、metadata 與 MCP 握手，不寫入資料測試權限。Client 設定合併前會備份。Claude Code 請在目標專案執行精靈。完成後重新載入 MCP 設定。

每組連線使用獨立 process、憑證參照、policy 與 cache。以不同名稱建立 `erp_prod`、`oracle_test`、`reporting`，再於對話指定要用的 MCP server。固定 profile 不受 cwd `.env` 或繼承的 `SEMANTIC_MCP_*` 設定影響。

手動配置範例：

- [SQL Server v2 connection](config/connection.sqlserver.example.json) 與 [policy](config/policy.example.json)。
- [Oracle v2 connection](config/connection.oracle.example.json) 與 [remote scope policy](config/policy.oracle.example.json)。

將 connection 複製到私有資料夾，命名為 `connection.json`；配對 policy 命名為 `policy.json`。私下設定 `credential.key` 指定的密碼環境變數。JSON 與 Client 設定只保存參照。`readonly`、policy profile 名稱與 operations 必須一致。

```bash
db-semantic-mcp serve --config /absolute/private/connection.json
db-semantic-mcp doctor --config /absolute/private/connection.json
```

Client entry 使用安裝環境的 Python，參數為 `-I -m db_semantic_mcp.main serve --config ABSOLUTE_PATH`。移動環境後需重新安裝並執行 setup。Doctor 回報實際本機傳輸協定、server／driver 版本、source 可用性、完整 capabilities 與 MCP 工具數，不寫入資料測試權限。

| 平台 | 預設配置根目錄 |
|---|---|
| macOS | `~/Library/Application Support/db-semantic-mcp/connections/` |
| Windows | `%APPDATA%\db-semantic-mcp\connections\` |
| Linux | `$XDG_CONFIG_HOME/db-semantic-mcp/connections/`，預設 `~/.config/db-semantic-mcp/connections/` |

密碼優先存於系統 keyring。macOS／Linux 經明確同意可使用私有明文檔，目錄權限 `0700`、檔案 `0600`；Windows 使用 keyring。手動 profile 可使用密碼環境變數參照。環境變數啟動仍可用，見 [.env.example](.env.example)。

## Oracle 連線與來源授權

`oracle.service_name`、`oracle.sid`、`oracle.dsn` 必須擇一。service／SID 另需 `server` 與 `port`。TNS alias 使用適當的 `config_dir`。禁止在 DSN 放入憑證。`database` 用於連線與 cache 身分，Oracle option 決定實際 service／SID。

直連 `encrypt=false` 使用 TCP，`encrypt=true` 使用 TCPS，必須指定正確 listener port。Thin TCPS 使用 `wallet_location` 與選用的 `wallet_password_env`。Thick 使用 `config_dir` 下的 `sqlnet.ora` 設定 trust。強制 server DN 驗證，可用 `ssl_server_cert_dn` 指定預期 DN。DSN／TNS 所有位址的 protocol 必須與 encrypt 相同；混合 protocol 或降級會被拒絕。Doctor 驗證實際本機 NETWORK_PROTOCOL。DB link 傳輸由資料庫管理，不宣稱已驗證端到端 TLS。

`connection.json.sources` 只定義路由。遠端另需 `policy.scope.sources` 明確授權 schema 與 operations。未列出的 link 一律拒絕。readonly route 不接受寫入。產品使用既有 DB link，不建立 link 或修改 grant。

探索工具支援 `source`，預設 `local`。例如 `get_tables(source="remote")` 透過原登入探索遠端。`plan_or_execute_query` 使用 caller 原始 SQL，不加上 @link 或轉換方言：

```sql
SELECT AMOUNT FROM DBMCP_TEST.DBMCP_ORDERS@DBMCP.REMOTE.TEST;
SELECT AMOUNT FROM DBMCP_TEST.DBMCP_REMOTE_CHAIN;
```

無法證明遠端 CURRENT_SCHEMA 時，SQL 必須指定 owner。private／public 與鏈狀 synonym 保留使用名稱、解析鏈及 canonical target。整條路徑與最終目標都需授權。public synonym 可指向已授權且只存在遠端的 owner。循環、未知 link、無法使用的路由與未證明目標會被拒絕。各 source cache 依路由、principal、policy 與 driver options 隔離。每個 request 固定一份 policy snapshot；reload 只影響後續 request。

Oracle inventory 預設探索 `ORACLE_MAINTAINED=N` 應用 schemas；明確 `allowed_schemas` 可納入系統 schemas。Catalog 包含欄位 precision／scale／字元長度語意、註解、PK、有順序的複合 FK、索引、view、synonym 與靜態 dependencies。PACKAGE／PACKAGE BODY／PROCEDURE／FUNCTION 的 PL/SQL 定義一律隱藏，即使 local scope 無限制也相同。已授權 metadata 與靜態 dependencies 仍可用，IO completeness 明示 unknown。不宣稱完整動態 SQL 或 PL/SQL 控制流程分析。VIEW 定義需通過嚴格來源／函式／欄位證明；未證明的 MATERIALIZED VIEW 定義隱藏。

## SQL 與執行限制

兩種資料庫預設只允許 SELECT。明確啟用受限寫入後，允許單一 `INSERT ... VALUES` 或 `UPDATE ... WHERE`。拒絕 DELETE、DDL、procedure／PLSQL 執行、交易控制、不透明來源與不安全 batch。SQL Server 支援嚴格限制下的 scalar DECLARE／SET 後接一個 SELECT。Oracle 使用原生 SQL，不轉譯方言，並拒絕 SELECT FOR UPDATE、sequence、remote／自訂函式及未知的潛在副作用函式。MCP policy 不會授予資料庫權限。

| 限制 | 預設 | 設定 |
|---|---:|---|
| 回傳列數 | 1000 | `max_rows_returned`；每次 `max_rows` 可降低 |
| JSON 欄名與保留列 | 65536 bytes | `max_response_bytes` |
| 每格文字／binary hex | 2000 字元 | `max_cell_chars` |
| 清單項目 | 500 | `max_list_items`／工具 `limit` |
| 影響列數 | 100 | `max_rows_affected` |
| Timeout | 30 秒 | `query_timeout` |

每格截斷時保留最多 max_cell_chars 個內容字元，另加可見截斷標記。回應 envelope 另計。欄名本身超出上限時回受控錯誤。截斷會附旗標與原因；`row_count` 為保留列數，未讀完時 `total_row_count=null`。用 WHERE、較少欄位、TOP／FETCH 或 aggregate 縮小結果。Oracle LOB 在 connection 存活期間有限量讀取，不先讀完整 LOB；遠端 LOB 能力依 driver／server 而異。

Oracle NUMBER 保持 Decimal，不轉 float。Thin 只接受已證明 precision 0..6 的原生 TIMESTAMP；較高／未知 precision、TZ／LTZ 會拒絕並提示 TO_CHAR。Thick 保留 FF9 與時區文字，LTZ 以 UTC 輸出，因為資料庫未保存原始時區。可明確使用 `TO_CHAR(ts, 'YYYY-MM-DD HH24:MI:SS.FF9')`，TZ 使用 `TO_CHAR(tz, 'YYYY-MM-DD HH24:MI:SS.FF9 TZR TZD')`。Thin 的 LTZ 文字依登入 session timezone。SQLGlot 只分析，不改寫 SQL。

Oracle `query_timeout` 對應單次 network round trip 的 `call_timeout`，不是整個查詢總 deadline。`connect_timeout` 是每個位址 TCP 建連上限，不是總登入 deadline。取消 MCP 等待或斷線不會中斷已執行的同步 driver。Worker 保留容量直到 cleanup 完成，寫入可能在 caller 取消後完成。取消不能作為 rollback 證據。

固定 profile 超出影響列數上限一定 rollback。環境／policy 配置預設 report；可設 `strict_rows_affected_cap` 或每次 `affected_rows_policy="strict"` 啟用 rollback。Autocommit 關閉。Commit／rollback acknowledgement 不明時回 `transaction_state="unknown"`、`automatic_retry_allowed=false`。`executed=false` 不代表已 rollback。未知結果、timeout 或 client cancellation 後，先核對資料庫結果再手動重試。分散式寫入亦同，不強制處理 in-doubt transaction，也不自動重試。

`validate_result_schema=true` 可額外預檢已授權 SELECT 的結果 metadata，不代表業務邏輯正確。Oracle 不對未證明的 DDL 執行 parse。拒絕結果包含 rule／message／hint；受限定義附 omission／completeness 旗標。

## 工具、cache 與診斷

12 個工具為 get_tables、describe_table、describe_object、get_table_relationships、get_dependency_chain、detect_lookup_tables、find_join_path、discover_relevant_tables、plan_or_execute_query、get_execution_policy、refresh_schema_cache、tool_metrics。Tool profiles：`all` 12、`query` 2、`explore` 8、`maintenance` 3。工具集合不改變授權。Resources 支援 `?source=CONFIGURED_ID`。

每次回應包含精簡 provider／source／database 身分與 precision／driver 旗標。完整能力、timeout 語意、inventory 範圍與 temporal workaround 請呼叫 `get_execution_policy`，doctor 也會列出。不提供 summary／sample／detail 模式或 token budget，舊 `budget` 忽略。輸出有明確上限，並驗證 structuredContent／outputSchema。

Metadata、resources、dependencies 與查詢共用 scope。複合 FK 保留順序與 identity；無有效唯一性證據時 cardinality 明示 unknown。Discovery 使用本機 metadata 與選用的 `discovery_aliases`，不呼叫外部模型。Cache fingerprint 涵蓋註解、constraints、indexes；預設 TTL 300 秒，single-flight 背景複驗。Remote 按需 warmup。離線舊內容明示 stale。回應的 snapshot identity 對應實際內容，不套用另一個更新完成的 snapshot。預設 DB worker 上限 4。`tool_metrics` 記錄 bytes、latency、worker wait 與失敗類別，不保存 SQL、資料列或密碼。

## 舊版遷移

舊 CLI 與 Python namespace 不保留。先預覽，預覽不讀取憑證、不寫入：

```bash
db-semantic-mcp migrate --legacy-root /absolute/old/connections --config-dir /absolute/new/connections --dry-run
db-semantic-mcp migrate --legacy-root /absolute/old/connections --config-dir /absolute/new/connections --apply
```

可指定 `--client CLIENT --client-config PATH` 或重複 `--client-path CLIENT=PATH`。Apply 複製憑證至新 reference、保留名稱／scope／較嚴格 caps，建立 v2 profile 並驗證 MCP。只更新已正向認定屬於舊工具的 Client entry。保留原檔、備份與並行修改。舊 cache 不遷移，重新建立隔離 cache。

## 開發與驗證

```bash
uv sync --locked --extra dev --extra oracle
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv lock --check
uv build
```

沒有明確 disposable opt-in 時，live tests 會 skip。詳見 [可重現 fixture 與指令](docs/testing-oracle.md)及[實測紀錄](docs/validation-0.8.0.md)。Mock 測試不能替代實機認證。

[問題回報](https://github.com/LukeStackLab/sqlserver-semantic-mcp/issues) · [MIT License](LICENSE)
