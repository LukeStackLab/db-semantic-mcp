# 本機 Docker 示範環境

此環境建立新的 SQL Server、Oracle 與模擬資料，供 DB Semantic MCP 探索及唯讀查詢。預設兩個容器；`--remote` 加入第二個 Oracle，用來測試 DB link。Oracle Thin 與 Thick 是 Python driver 模式，連線到同一個 Oracle，不需要各建一個資料庫。

## 啟動與檢查

需要 Docker Engine / Docker Desktop、Compose v2，以及 `uv`。Controller 目前支援 macOS／Linux 的本機 Unix socket，包括 Docker Desktop；不支援原生 Windows `npipe`，WSL 尚未驗證。在 repository 根目錄執行：

```bash
uv sync --extra dev --extra oracle
uv run python scripts/demo.py up
uv run python scripts/demo.py check
uv run python scripts/demo.py doctor --provider sql
uv run python scripts/demo.py doctor --provider oracle
```

`up` 首次建立私有憑證、啟動資料庫、等待健康檢查，然後以管理者建立 fixture。後續 `up`／`init` 保留已有資料與 profile，不重新灌資料。健康檢查最多等待 600 秒；失敗不自動重設資料。初始化途中失敗時，後續執行會拒絕覆寫已存在物件。若確定可以丟棄自己的 demo 資料，再使用下方明確清除指令。

預設資源：

| 服務 | 容器 | localhost port | 記憶體上限 |
|---|---|---|---|
| SQL Server 2022 Developer | `db-semantic-demo-sql` | `21433` | 2 GiB |
| Oracle Free 23 | `db-semantic-demo-oracle` | `21521` | 3 GiB |
| 選配遠端 Oracle Free 23 | `db-semantic-demo-remote` | `21522` | 3 GiB |

建議 Docker 至少配置 8 GiB RAM；啟用 remote 時配置 12 GiB，並預留映像與資料卷的磁碟空間。SQL Server 映像為 `linux/amd64`，Apple Silicon 需要 Docker Desktop 的 amd64 模擬支援；Oracle 映像支援 ARM64。實測環境為 Apple Silicon／Docker Desktop。映像 tag 會更新，因此重新 pull 後的版本可能不同。SQL Server Developer 與 Oracle Free 僅用於符合其授權的開發／測試用途。

所有命令在讀寫私有 state 前，先用唯讀 `docker context inspect` 取得目前 Docker CLI 實際選用的 endpoint。`DOCKER_CONTEXT`、`DOCKER_HOST` 與設定檔的優先序交由已安裝 CLI 解析。不同版本可能有差異，實作依據可見 [Docker CLI endpoint 解析](https://github.com/docker/cli/blob/v29.8.2/cli/command/cli.go#L413)。只接受存在於本機的 Unix socket；SSH、TCP（含 localhost TCP）、npipe 或無法確認的 endpoint 均拒絕。通過後，所有 Docker／Compose 命令固定使用已驗證 socket 的 `--host`，移除 context／host／TLS environment overrides，不更改使用者的 Docker context。

所有對外 port 綁定 `127.0.0.1`。本示範採 TCP，profile 明確設定 `encrypt=false`。不會自動建立 TCPS 憑證；先前 TCPS 驗證見 [0.8.0 驗證紀錄](validation-0.8.0.md)。DB link 使用私有 Compose 網路，不能據此宣稱遠端傳輸已加密。

## 模擬資料與查詢

SQL Server database 為 `DBSemanticDemo`。`dbo.Customers` 有兩個租戶，`dbo.Orders` 有三筆訂單。Customers 的 `(TenantId, CustomerId)` 是複合 PK，Orders 用相同順序的複合 FK；另有索引與 `dbo.OrderSummary` view。金額使用 `decimal(19,4)`，包含 `123.4500`、`2.5000`、`987654321012345.6789`。

```sql
SELECT TenantId, OrderId, Amount FROM dbo.Orders ORDER BY TenantId, OrderId;
SELECT * FROM dbo.OrderSummary;
```

Oracle service 為 `FREEPDB1`。沿用 [公開 Oracle fixture](../tests/integration/fixtures/oracle/common.sql)：`DBMCP_TEST.DBMCP_CUSTOMERS`、`DBMCP_ORDERS`、複合 PK/FK、索引、中文註解、NUMBER(19,4)、CLOB/BLOB、timestamp、summary view、package metadata，以及 `DBMCP_LOCAL_ORDERS` → `DBMCP_CHAIN` synonym。兩筆金額為 `123.4500` 與 `2.5000`。Profile 的 cell cap 為 64 字元，長 LOB 會回傳截斷標記。

```sql
SELECT ORDER_ID, AMOUNT FROM DBMCP_TEST.DBMCP_CHAIN ORDER BY ORDER_ID;
SELECT NOTE, PAYLOAD FROM DBMCP_TEST.DBMCP_ORDERS WHERE ORDER_ID=1;
SELECT TO_CHAR(CREATED_AT, 'YYYY-MM-DD HH24:MI:SS.FF6 TZH:TZM') FROM DBMCP_TEST.DBMCP_ORDERS;
```

Thin 對無法保真輸出的原生 timestamp 回傳受控錯誤；可在原始 SQL 明確使用 `TO_CHAR`。Package 只探索 metadata，不執行 PL/SQL。更多限制見 [Oracle 驗證說明](testing-oracle.md)。

## 第二個 Oracle 與來源授權

```bash
uv run python scripts/demo.py up --remote
uv run python scripts/demo.py check
uv run python scripts/demo.py doctor --provider oracle-remote
```

remote 建立相同 common fixture，另有 `DBMCP_REMOTE_ONLY.DBMCP_ONLY_ORDERS`，內容標示 `remote-only-owner`、金額 `678.25`。本地建立 `DBMCP.REMOTE.TEST` DB link、private remote synonym chain 與 public remote synonym。這些都是 fixture 管理者建立；MCP 本身不建立 DB link。

`oracle` profile 保持本地 scope，`oracle-remote` profile 明確授權 remote 的 `DBMCP_TEST` 與 `DBMCP_REMOTE_ONLY`。`check` 也驗證本地 profile 拒絕遠端查詢。

```sql
SELECT AMOUNT FROM DBMCP_TEST.DBMCP_ORDERS@DBMCP.REMOTE.TEST;
SELECT AMOUNT FROM DBMCP_TEST.DBMCP_REMOTE_CHAIN;
SELECT AMOUNT FROM DBMCP_PUBLIC_REMOTE_ONLY;
```

## 私有配置與 MCP

`.db-semantic-mcp-demo/` 位於 repository 根目錄，已排除 Git。目錄權限為 `0700`，`.env`、ownership state、connection/policy JSON 為 `0600`。每次新建環境產生隨機密碼，不使用 keyring、不修改任何 Client 設定。請保留此目錄，失去 ownership token 後 controller 會拒絕接管同名 Docker 資源。

| Profile | 路徑 | 登入／權限 |
|---|---|---|
| SQL Server | `.db-semantic-mcp-demo/sql/connection.json` | `dbmcp_reader`，資料庫 SELECT／VIEW DEFINITION |
| Oracle Thin | `.db-semantic-mcp-demo/oracle/connection.json` | `DBMCP_TEST`，MCP readonly |
| Oracle Thin + remote | `.db-semantic-mcp-demo/oracle-remote/connection.json` | `DBMCP_TEST`，MCP readonly、明確來源 scope |

Oracle fixture owner 在資料庫中具有建立 fixture 所需權限；MCP policy 一律 readonly。此帳號只適合隔離的 demo。秘密只在私有 `.env`，profile 只記錄環境變數參照。Docker 管理者仍可讀取容器 environment。不要貼出 `.env`、`docker inspect` 全文、Compose resolved config 或私有檔案。

啟動可供 MCP Client 使用的 stdio server：

```bash
uv run python scripts/demo.py mcp --provider sql
uv run python scripts/demo.py mcp --provider oracle
uv run python scripts/demo.py mcp --provider oracle-remote
```

Wrapper 自動載入私有 environment，保持 stdout 給 MCP protocol。它不註冊 Client。手動配置 Client 時，使用上述命令與 repository 工作目錄。

若要使用 Thick，安裝符合平台的官方 Oracle Instant Client，另複製自己的私有 Oracle profile，將 `oracle.driver_mode` 改為 `thick`，並設定持久有效的 `oracle.client_lib_dir`。同一個 `127.0.0.1:21521/FREEPDB1` 即可使用兩種模式。Demo 不下載或散布 Oracle Client，也不引用任務暫存 workspace 中的 Client。

## 停止與刪除資料

```bash
# 只停止：保留所有資料、憑證與 profile
uv run python scripts/demo.py down
# 再啟動：保留資料
uv run python scripts/demo.py up
# 明確刪除本 demo 的容器、network、資料卷
uv run python scripts/demo.py down --purge
```

`--purge` 會永久刪除此 demo 的資料。私有憑證保留，舊 profile 移至帶時間戳記的私有備份目錄；下次 `up` 建立新資料與 profile。每次操作都先驗證固定名稱、Compose project label 與私有 ownership token；遇到外來同名容器／資料卷／network、額外 project resources 或占用 port 時拒絕操作。未執行 global prune，不操作既有 `1433` 或其他專案。

請使用 controller 管理；直接執行 `docker compose` 會繞過 ownership 檢查。不要同時執行兩個管理命令。

## 已執行驗證

2026-10-16：在獨立 `21433`／`21521`／`21522` 上建立真實資料庫。`check` 透過固定 profile 驗證連線、Decimal、複合 FK、Oracle LOB／synonym，以及實際 MCP child 的 12-tool handshake 與查詢。remote 額外驗證原生 `@DBMCP.REMOTE.TEST` SQL、synonym、public remote owner 解析與未授權來源拒絕。Thin／Thick 共用同一個本地 Oracle。重複 up／init 與 stop／up 保留資料。

此紀錄只涵蓋本機 demo，未認證 Oracle 19c 或 SQL Server 2017／2019。詳細 portable live tests 與 clean-wheel 方法見 [Oracle 驗證說明](testing-oracle.md)。

實測版本為 SQL Server `16.0.4265.3`、Oracle `23.26.3.0.0`。驗證結果：

| 檢查 | 結果 |
|---|---|
| `uv run --extra oracle pytest -q tests/unit/test_demo_contracts.py` | 37 passed，含 daemon locality 修正 |
| `uv run --extra oracle pytest -q` | 初次交付 858 passed、14 skipped；live tests 預設 opt-out |
| 新 demo 的 SQL + Oracle Thin portable live suite | 12 passed、2 skipped |
| 同一 Oracle 的 Thick portable live suite | 9 passed、2 skipped |
| clean-wheel base／Thin／Thick | 三組 CLI、doctor、12-tool handshake 與實際查詢通過 |
| `uv run --extra oracle ruff check .` | 通過 |
| `uv run --extra oracle ruff format --check .` | 182 files 通過 |
| `uv lock --check`、`uv build` | 通過；sdist 包含公開 demo 檔案，兩種產物不含私有狀態 |

live suite 的兩項 skip 為需要另外授權的寫入測試；本 demo profile 保持 readonly。Stop/up/init 與錯誤 ownership token 拒絕已做實機驗證；purge 的精確刪除範圍由 focused tests 驗證。所有既有非 demo 容器的 ID 在操作前後相同。一般使用只需執行上方 `up` 與 `check`；完整 live matrix 的 opt-in 方法沿用 [testing-oracle.md](testing-oracle.md)。

2026-10-16 locality 修正後另執行 37 項 focused tests、修改檔案 lint／format，以及真實 Desktop socket 的 repeat up／三組 profile check。暫存遠端 context 對 up/init/check/down/mcp/doctor 全部拒絕，遠端 DOCKER_HOST 也拒絕；私有 state 與使用者 current context 未變。完整 suite 未因這次局部修正重跑。
