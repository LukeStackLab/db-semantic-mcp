# sqlserver-semantic-mcp

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-1.0%2B-purple.svg)](https://modelcontextprotocol.io)
[![Version](https://img.shields.io/badge/version-0.6.0-green.svg)](pyproject.toml)
[![English](https://img.shields.io/badge/lang-English-blue.svg)](README.en.md)

> **SQL Server 資料庫的語意智慧層,透過 MCP 對外提供。**
> 這不是 SQL 執行器,而是為 AI Agent 打造的資料庫理解引擎。

AI Agent 不需要赤裸的 `execute_sql`。它們需要理解 schema 結構、關聯、物件相依性,最重要的是,能在操作者定義的安全邊界內運作。

`sqlserver-semantic-mcp` 透過 12 個 MCP 工具、1 個 concrete MCP 資源與 5 個 MCP resource templates 提供以上能力,底層以雙層 SQLite 快取加速,並以 JSON 格式的 policy 系統保障安全。

---

## 快速開始

依你使用的 client 選一條路徑。所有路徑都用 [`uvx`](https://docs.astral.sh/uv/) — 不需要 `git clone`、不需要建虛擬環境、不需要手動安裝。`uvx` 會在第一次使用時下載並快取套件。

> **前置:** 安裝一次 [uv](https://docs.astral.sh/uv/getting-started/installation/)(`curl -LsSf https://astral.sh/uv/install.sh | sh`)。Python 3.11+ 由 `uv` 自動取得。

> **範例中的** `localhost` / `YourDatabase` / `sa` / `YourPassword` **請替換成你的實際 SQL Server 認證。**

### 🤖 Claude Code CLI

一行指令完成註冊,`uvx` 會在第一次使用時自動解析與快取 `sqlserver-semantic-mcp`:

```bash
claude mcp add sqlserver-semantic -- uvx sqlserver-semantic-mcp \
  -e SEMANTIC_MCP_MSSQL_SERVER=localhost \
  -e SEMANTIC_MCP_MSSQL_DATABASE=YourDatabase \
  -e SEMANTIC_MCP_MSSQL_USER=sa \
  -e SEMANTIC_MCP_MSSQL_PASSWORD=YourPassword
```

或將設定 commit 到專案的 `.mcp.json`,讓整個團隊共用:

```json
{
  "mcpServers": {
    "sqlserver-semantic": {
      "command": "uvx",
      "args": ["sqlserver-semantic-mcp"],
      "env": {
        "SEMANTIC_MCP_MSSQL_SERVER": "localhost",
        "SEMANTIC_MCP_MSSQL_DATABASE": "YourDatabase",
        "SEMANTIC_MCP_MSSQL_USER": "sa",
        "SEMANTIC_MCP_MSSQL_PASSWORD": "YourPassword"
      }
    }
  }
}
```

用 `claude mcp list` 確認註冊成功。Server 透過 stdio 與 Claude Code 溝通,在工作階段啟動時即可使用。

### 🛠 Codex CLI

把以下區塊加進 `~/.codex/config.toml`:

```toml
[mcp_servers.sqlserver-semantic]
command = "uvx"
args = ["sqlserver-semantic-mcp"]
env = { SEMANTIC_MCP_MSSQL_SERVER = "localhost", SEMANTIC_MCP_MSSQL_DATABASE = "YourDatabase", SEMANTIC_MCP_MSSQL_USER = "sa", SEMANTIC_MCP_MSSQL_PASSWORD = "YourPassword" }
```

接著執行 `codex`,server 會出現在 MCP 工具清單中。

### 🖥 Claude Desktop

編輯設定檔:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "sqlserver-semantic": {
      "command": "uvx",
      "args": ["sqlserver-semantic-mcp"],
      "env": {
        "SEMANTIC_MCP_MSSQL_SERVER": "localhost",
        "SEMANTIC_MCP_MSSQL_DATABASE": "YourDatabase",
        "SEMANTIC_MCP_MSSQL_USER": "sa",
        "SEMANTIC_MCP_MSSQL_PASSWORD": "YourPassword"
      }
    }
  }
}
```

存檔後重啟 Claude Desktop。

### 🧪 預先測試(可選,所有 client 通用)

在連到 host 之前先確認套件能跑:

```bash
SEMANTIC_MCP_MSSQL_SERVER=localhost \
SEMANTIC_MCP_MSSQL_DATABASE=YourDatabase \
SEMANTIC_MCP_MSSQL_USER=sa \
SEMANTIC_MCP_MSSQL_PASSWORD=YourPassword \
  uvx sqlserver-semantic-mcp
```

應該會看到啟動 log 顯示 cache 初始化與工具註冊。按 `Ctrl+C` 結束。

### 🧰 本機開發(僅貢獻者)

只想使用 server 的話請略過此段。

```bash
git clone https://github.com/LukeStackLab/sqlserver-semantic-mcp.git
cd sqlserver-semantic-mcp
cp .env.example .env             # 然後填入 MSSQL 認證
uv sync --dev                    # 建立 .venv 並安裝 dev 依賴
uv run python -m sqlserver_semantic_mcp.main
```

如果偏好 pip editable install:

```bash
pip install -e ".[dev]"
sqlserver-semantic-mcp
```

完整環境變數列表請見[設定](#設定)章節。

---

## 功能特色

- **12 個 MCP 工具**,分佈於 9 個能力群組(metadata、relationship、semantic、object、query、policy、cache、metrics、workflow)— 由早期 29 個工具精簡整合而成,被吸收的能力以參數形式保留(詳見 [MCP 工具](#mcp-工具))
- **雙層 SQLite 快取** — Structural Cache(啟動時預熱)+ Semantic Cache(延遲載入 + 背景填入)
- **Cache-first 啟動** — 預設重用既有 structural cache,避免每次程序重啟都強制全量預熱
- **自動結構漂移偵測(L1 schema 探針)** — 節流的純 catalog 指紋探針可偵測欄位型別/長度變更、表的新增/刪除、索引變更與 view/procedure/function 內文修改,並自動刷新快取
- **逐表精準失效** — 只有自身結構變更的表會被重新分析;依賴變更表的 view/function 透過 `sys.sql_expression_dependencies` 連鎖標記為 dirty
- **三重 hash schema 版本控制** — 偵測結構 / 物件 / 註解變更何時讓快取分析失效
- **Policy 閘門執行** — SELECT/INSERT/UPDATE/DELETE/… 權限、WHERE 子句要求、資料列上限、schema/table 白名單
- **語意分類** — 自動識別 fact / dimension / lookup / bridge / audit 表
- **Join 路徑探索** — 在 FK 圖上以 BFS 找出兩張表之間的關聯路徑
- **物件檢視** — view / procedure / function 定義與相依追蹤,並拆分 reads / writes
- **Workflow 快捷工具** — 候選表 discovery,以及內建 risk estimation 的 direct execution fast path(`plan_or_execute_query`);context bundling 現在是 MCP 資源而非工具
- **Payload metrics** — 內建每個工具回應大小量測
- **優雅降級** — policy 檔遺失或損壞時回退為唯讀;資料庫不可達時亦不會破壞快取

---

## 架構

五層架構,嚴格單向相依:

```
MCP Interface      (server/)          ← tool / resource 註冊
      ↓
Application        (services/)        ← 6 個服務編排快取 + policy + DB
      ↓
Policy / Domain    (policy/, domain/) ← 模型、SQL 意圖分析、強制執行
      ↓
Infrastructure     (infrastructure/)  ← pymssql + SQLite + 背景任務
      ↓
SQL Server + SQLite
```

### 快取模型

| 層級 | 內容 | 策略 | 失效條件 |
|---|---|---|---|
| **Structural Cache** | 表、欄位、PK/FK、索引、物件清單、註解 | 啟動時預熱,SQLite 持久化 | schema 探針偵測漂移、逐表 `structural_hash` 不一致 |
| **Semantic Cache** | 表分類、欄位語意、物件定義、相依性 | 延遲 + 背景漸進填入 | 逐表 hash 變更 → 受影響列標記為 `dirty` → 重新計算 |
| **指紋基準(Fingerprint Baseline)** | 逐表 / 逐物件的 catalog 指紋(`modify_date` + 欄位/索引 checksum + 模組定義 hash) | 每次預熱時寫入 | schema 探針逐窗口比對 |

### 結構漂移偵測(L1 探針)

快取採 hash 驅動而非 TTL 驅動:沒有任何東西因時間到期而失效,資料庫沒變動時就是零重抓成本。新鮮度由一個輕量探針保證,每 `SEMANTIC_MCP_PROBE_INTERVAL_S` 秒(預設 60)最多執行一次,由進來的工具呼叫惰性觸發:

1. **探測** — 五個純 catalog 查詢(`sys.tables`、`sys.columns`、`sys.indexes`、`sys.sql_modules`、`sys.sql_expression_dependencies`)在毫秒級算出逐表與逐物件指紋,完全不碰資料列。欄位型別變更(`int → bigint`)、長度變更(`nvarchar(4) → nvarchar(10)`)、精度/可空性/定序變更、索引變更、view/procedure/function 內文修改全部涵蓋。
2. **比對** — 指紋與 SQLite 中的基準逐筆 diff。沒有漂移 → 結束(回應永遠來自快取)。
3. **漂移才刷新** — 重抓完整結構快照,只把*變動的*表的語意列標記為 `dirty`、清除已刪除的表/物件,並把依賴變動表的模組(SQL Server 在底層表變更時不會更新它們)透過相依圖連鎖標記為 `dirty`。背景填入迴圈隨後重新計算 dirty 列。

重新驗證採 stale-while-revalidate:工具呼叫永遠立即從快取回應,探針(若偵測到漂移則含刷新)在背景執行。`refresh_schema_cache` 仍可用於立即強制刷新;停用快取(`SEMANTIC_MCP_CACHE_ENABLED=false`)時探針亦一併停用。

---

## 安裝

> 一般使用者請依[快速開始](#快速開始)使用 `uvx`,無需安裝步驟。本段針對貢獻者與離線/封閉環境。

需要 Python 3.11+。

**透過 uvx 一次性執行**(無需安裝,推薦給終端使用者):

```bash
uvx sqlserver-semantic-mcp
```

**安裝為全域 CLI 工具:**

```bash
uv tool install sqlserver-semantic-mcp
# 或:
pipx install sqlserver-semantic-mcp
```

**從原始碼以 pip editable 安裝**(註冊 `sqlserver-semantic-mcp` 指令到 PATH):

```bash
pip install -e ".[dev]"
```

**從原始碼以 uv 安裝:**

```bash
uv sync          # 或 uv sync --dev 包含開發依賴
```

**不安裝直接執行**(uv project 模式):

```bash
uv run python -m sqlserver_semantic_mcp.main
```

執行時依賴:

| 套件 | 角色 |
|---|---|
| `mcp` | MCP SDK(stdio transport) |
| `pymssql` | SQL Server wire driver(包裝 FreeTDS) |
| `pydantic` + `pydantic-settings` | 設定驗證、環境變數載入 |
| `aiosqlite` | 雙層快取使用的 async SQLite |

開發依賴:`pytest`、`pytest-asyncio`、`pytest-mock`。

> **Linux 注意:** `pymssql` 連結 FreeTDS。若 `pip install` 因編譯錯誤失敗,請先安裝系統 header — 詳見下方[疑難排解](#疑難排解)。

---

## 設定

所有設定透過 `SEMANTIC_MCP_` 前綴的環境變數進行。工作目錄下的 `.env` 檔也會自動載入。建議直接從 `.env.example` 開始。

一般使用只需設定連線資訊。權限預設為唯讀,回應預算統一預設為 `medium`,不需選擇額外級別。需要寫入時,再依下方「自訂 Policy 檔」啟用 `read_write_safe`。其他設定供進階使用。

### 必要項目

| 變數 | 說明 |
|---|---|
| `SEMANTIC_MCP_MSSQL_SERVER` | SQL Server 主機(支援 `(localdb)\Instance` 與 `*.database.windows.net`) |
| `SEMANTIC_MCP_MSSQL_DATABASE` | 目標資料庫名稱 |
| `SEMANTIC_MCP_MSSQL_USER` | SQL 認證使用者(`SEMANTIC_MCP_MSSQL_WINDOWS_AUTH=true` 時可省略) |
| `SEMANTIC_MCP_MSSQL_PASSWORD` | SQL 認證密碼 |

### 選用項目

| 變數 | 預設值 | 說明 |
|---|---|---|
| `SEMANTIC_MCP_MSSQL_PORT` | `1433` | TCP 連接埠 |
| `SEMANTIC_MCP_MSSQL_WINDOWS_AUTH` | `false` | 使用 Windows 驗證 |
| `SEMANTIC_MCP_MSSQL_ENCRYPT` | `false` | 強制 TLS(Azure SQL 自動啟用) |
| `SEMANTIC_MCP_CACHE_PATH` | `./cache/semantic_mcp.db` | SQLite 快取檔位置 |
| `SEMANTIC_MCP_CACHE_ENABLED` | `true` | 關閉可略過啟動預熱 |
| `SEMANTIC_MCP_STARTUP_MODE` | `cache_first` | `cache_first` 會在重啟時優先重用既有 cache;`full` 則每次都先向 SQL Server 重新抓結構 |
| `SEMANTIC_MCP_PROBE_INTERVAL_S` | `60` | 兩次 schema 探針之間的最小間隔秒數(節流窗口) |
| `SEMANTIC_MCP_BACKGROUND_BATCH_SIZE` | `5` | 每次背景批次處理的表數 |
| `SEMANTIC_MCP_BACKGROUND_INTERVAL_MS` | `500` | 批次之間的延遲 |
| `SEMANTIC_MCP_POLICY_FILE` | *(內建唯讀)* | Policy JSON 檔路徑 |
| `SEMANTIC_MCP_POLICY_PROFILE` | *(檔案的 active_profile)* | 覆寫啟用中的 profile |
| `SEMANTIC_MCP_MAX_ROWS_RETURNED` | `1000` | 覆寫 SELECT 回傳列數上限 |
| `SEMANTIC_MCP_MAX_ROWS_AFFECTED` | `100` | 覆寫 DML 受影響列數上限 |
| `SEMANTIC_MCP_QUERY_TIMEOUT` | `30` | 查詢逾時(秒) |
| `SEMANTIC_MCP_TOOL_PROFILE` | `all` | 以逗號分隔的工具群組: metadata、relationship、semantic、object、query、policy、cache、metrics、workflow |
| `SEMANTIC_MCP_WORKFLOW_TOOLS_ENABLED` | `true` | 關閉 workflow shortcut 工具 |
| `SEMANTIC_MCP_METRICS_ENABLED` | `true` | 啟用每個工具回應大小量測 |
| `SEMANTIC_MCP_DEFAULT_DETAIL` | `brief` | Agent-facing 工具的預設 detail tier |
| `SEMANTIC_MCP_DEFAULT_RESPONSE_MODE` | `summary` | 查詢執行的預設回應 shape |
| `SEMANTIC_MCP_DEFAULT_TOKEN_BUDGET_HINT` | `medium` | 預設回應預算;資料表清單最多 500 筆,查詢 sample 最多 50 列。這是筆數限制,非固定 Token 上限 |
| `SEMANTIC_MCP_DIRECT_EXECUTE_ENABLED` | `true` | 當 policy 允許時啟用 workflow fast path 直接執行 |
| `SEMANTIC_MCP_STRICT_ROWS_AFFECTED_CAP` | `true` | 預設在超出 rows-affected cap 時回滾 |
| `SEMANTIC_MCP_INTENT_ANALYZER` | `regex` | SQL 意圖分析器後端(`regex` 或 `ast`) |

---

## 連線情境

把對應的區塊複製到你的 `.env` 檔(或 MCP client 設定中的 `env` 區塊)。

### SQL 認證(預設)

最常見的設定。需要 SQL Server 上啟用 SQL Server Authentication。

```env
SEMANTIC_MCP_MSSQL_SERVER=localhost
SEMANTIC_MCP_MSSQL_DATABASE=YourDatabase
SEMANTIC_MCP_MSSQL_USER=sa
SEMANTIC_MCP_MSSQL_PASSWORD=YourPassword
```

### Windows 驗證

省略 `MSSQL_USER` 與 `MSSQL_PASSWORD`;執行行程必須以具有 SQL Server 存取權的 Windows 帳戶啟動。在 `.env` 檔中具名實例需使用雙反斜線。

```env
SEMANTIC_MCP_MSSQL_SERVER=MY-PC\\SQLEXPRESS
SEMANTIC_MCP_MSSQL_DATABASE=YourDatabase
SEMANTIC_MCP_MSSQL_WINDOWS_AUTH=true
```

> Windows 驗證僅在 Windows 平台可用。pymssql 在 Linux 與 macOS 不支援 Windows 驗證 — 請改用 SQL 認證。

### Azure SQL Database

當伺服器名稱以 `.database.windows.net` 結尾時 TLS 會自動啟用,不需顯式設定 `MSSQL_ENCRYPT`。

```env
SEMANTIC_MCP_MSSQL_SERVER=yourserver.database.windows.net
SEMANTIC_MCP_MSSQL_DATABASE=YourDatabase
SEMANTIC_MCP_MSSQL_USER=youradmin@yourserver
SEMANTIC_MCP_MSSQL_PASSWORD=YourPassword
```

### LocalDB(僅限 Windows)

LocalDB 透過 named pipe 通訊,不需 TCP 連接埠。預設使用 Windows 驗證。

```env
SEMANTIC_MCP_MSSQL_SERVER=(localdb)\MSSQLLocalDB
SEMANTIC_MCP_MSSQL_DATABASE=YourDatabase
SEMANTIC_MCP_MSSQL_WINDOWS_AUTH=true
```

### 自訂 Policy 檔

將 `SEMANTIC_MCP_POLICY_FILE` 指向你管理的 JSON policy 檔。未設定時會使用內建唯讀模式。

```env
SEMANTIC_MCP_POLICY_FILE=./config/policy.example.json
SEMANTIC_MCP_POLICY_PROFILE=read_write_safe
```

完整 policy 檔格式與可用 profile 請見 [Policy 系統](#policy-系統)。

### 自訂快取位置

當對多個資料庫運行多個 server 實例,或預設 `./cache/` 目錄不可寫時可使用。

```env
SEMANTIC_MCP_CACHE_PATH=/var/lib/sqlserver-mcp/mydb.db
```

---

## Policy 系統

**預設安全姿態:** 若未設定 policy 檔,server 採用內建的 **唯讀** 模式 — 不需任何設定即可生效。在此模式下:僅允許 `SELECT`、結果上限 1000 列、拒絕多敘述查詢,而且每個查詢仍會在抵達 `cursor.execute()` 前經過 policy 執行器。系統不存在不受限的 SQL 執行路徑。

如需自訂 policy,請建立 JSON 檔(參考 `config/policy.example.json`)並將 `SEMANTIC_MCP_POLICY_FILE` 指向該檔:

```json
{
  "active_profile": "read_write_safe",
  "profiles": {
    "readonly":        { "operations": { "select": true } },
    "read_write_safe": {
      "operations": { "select": true, "insert": true, "update": true },
      "constraints": {
        "require_where_for_update": true,
        "max_rows_affected": 100
      }
    },
    "admin": {
      "operations": { "select": true, "insert": true, "update": true, "delete": true },
      "constraints": { "allow_multi_statement": true }
    }
  }
}
```

### Policy 欄位

**Operations** — 10 個旗標(select / insert / update / delete / truncate / create / alter / drop / execute / merge)

**Constraints** — `require_where_for_update`、`require_where_for_delete`、`require_top_for_select`、`max_rows_returned`、`max_rows_affected`、`allow_multi_statement`、`query_timeout_seconds`

**Scope** — `allowed_databases`、`allowed_schemas`、`allowed_tables`、`denied_tables`

### Profile 快速對照

| Profile | SELECT | INSERT | UPDATE | DELETE | 需要 WHERE | 列數上限 |
|---|---|---|---|---|---|---|
| `readonly`(內建預設) | Yes | No | No | No | N/A | 回傳 1000 列 |
| `read_write_safe` | Yes | Yes | Yes | No | UPDATE 需 WHERE | 影響 100 列 |
| `admin` | Yes | Yes | Yes | Yes | No | 影響 10 000 列 |

> **安全提醒:** 當設定 `allowed_schemas` 時,若查詢引用的表未帶 schema 前綴(例如 `SELECT * FROM Users` 而非 `dbo.Users`)將被拒絕 — 無法以隱含預設值繞過 schema 級別的存取控制。

### 失效行為

| 條件 | 行為 |
|---|---|
| Policy 檔路徑未設定 | 內建唯讀,記錄警告 |
| Policy 檔缺失 | 內建唯讀,記錄警告 |
| Policy 檔無法讀取 | 內建唯讀,記錄錯誤 |
| Policy 檔 JSON 格式錯誤 | 內建唯讀,記錄錯誤 |
| Policy 檔 schema 驗證失敗 | 內建唯讀,記錄錯誤 |
| `active_profile` / 覆寫指向不存在的 profile | 伺服器拒絕啟動(顯露設定錯誤) |

---

## MCP 工具

工具數量由早期的 29 個精簡整合為 **12 個**,分佈於 9 個能力群組。被移除的單一用途工具都已折入最接近的現存工具,透過參數保留原有能力 — 詳見下表「被吸收的能力」欄。

| 群組 | 工具 | 被吸收的能力(透過參數) |
|---|---|---|
| `metadata`(2) | `get_tables`、`describe_table` | `describe_table(detail=brief\|standard\|full)` 取代 `get_columns`(完整欄位資訊);每個 detail 層級都內含分類結果,取代 `classify_table`;回應中的 `important_columns` 取代 `summarize_table_for_joining` |
| `relationship`(3) | `get_table_relationships`、`find_join_path`、`get_dependency_chain` | `find_join_path(score=true)` 取代 `score_join_candidate` |
| `semantic`(1) | `detect_lookup_tables` | `analyze_columns` 的逐欄語意標記現已併入 `describe_table` 的回應 |
| `object`(1) | `describe_object` | `describe_object(type=VIEW\|PROCEDURE\|FUNCTION, detail=...)` 取代 `describe_view`、`describe_procedure`、`trace_object_dependencies`、`summarize_object_for_impact` |
| `query`(1) | `plan_or_execute_query` | `plan_or_execute_query(mode=auto\|validate\|dry_run\|execute_if_safe)` 取代 `validate_query`、`run_safe_query`、`preview_safe_query`、`estimate_execution_risk` |
| `policy`(1) | `get_execution_policy` | `get_execution_policy(reload=true)` 取代 `refresh_policy`;`plan_or_execute_query(mode="validate")` 取代 `validate_sql_against_policy` |
| `cache`(1) | `refresh_schema_cache` | — |
| `metrics`(1) | `tool_metrics` | `tool_metrics(action=get\|reset)` 取代 `get_tool_metrics` / `reset_tool_metrics` |
| `workflow`(1) | `discover_relevant_tables` | `suggest_next_tool` 已移除 — 每個回應信封都已內含 `next_action` / `recommended_tool`;`bundle_context_for_next_step` 現在是 `semantic://bundle/joining` 這個 MCP **資源**,不再是工具 |

若要降低 prompt 成本,優先使用 `detail=\"brief\"` 與帶 filter 的 metadata calls;不確定的操作先呼叫 `plan_or_execute_query(mode=\"validate\")`。

---

## MCP 資源

自動列出的 concrete resources:

- `semantic://summary/database`

自動列出的 resource templates:

- `semantic://schema/tables/{qualified}`
- `semantic://analysis/classification/{qualified}`
- `semantic://summary/table/{qualified}`
- `semantic://summary/object/{type}/{qualified}`
- `semantic://bundle/joining/{qualified}`

另外也保留相容性的 direct reads:

- `semantic://schema/tables`
- `semantic://analysis/dependencies/{type}/{schema}.{name}`

---

## 啟動伺服器

**使用安裝後的 console script**(在 `pip install -e .` 之後):

```bash
sqlserver-semantic-mcp
```

**透過 uv 執行**(無需安裝):

```bash
uv run python -m sqlserver_semantic_mcp.main
```

**直接以 Python 執行**(套件已在 `sys.path` 上時):

```bash
python -m sqlserver_semantic_mcp.main
```

伺服器透過 stdio 以 MCP 通訊。啟動時會:

1. 開啟(或建立)SQLite 快取
2. 當 `SEMANTIC_MCP_STARTUP_MODE=cache_first` 時優先重用既有 Structural cache,否則再從 SQL Server 抓取新的快照
3. 將所有表加入 Semantic 分析佇列
4. 啟動背景填入任務
5. 接受 MCP 工具/資源呼叫 — 每次呼叫會惰性觸發節流的 schema 探針(見「結構漂移偵測」),伺服器執行期間的 schema 變更會被自動偵測

背景填入對持續性錯誤採用指數退避(2ⁿ 秒,上限 60 秒),避免日誌洪流或 CPU 空轉。

---

## 開發

### 執行測試

```bash
uv run --extra dev pytest tests/unit
uv run --extra dev pytest tests/integration -m integration
```

### 發佈版本(維護者)

套件發佈於 PyPI,讓終端使用者可直接 `uvx sqlserver-semantic-mcp` 而不需 clone 專案。發佈流程:

1. 在 `pyproject.toml` 中升 `version`(遵守語意化版本)。
2. 更新 `docs/` 中的 changelog 與本 README 上方的版本徽章。
3. Commit 並打 tag:
   ```bash
   git commit -am "chore: bump to vX.Y.Z"
   git tag vX.Y.Z
   git push origin main --tags
   ```
4. 在本機 build 並驗證 artifacts:
   ```bash
   uv build                       # 產出 dist/*.whl + dist/*.tar.gz
   uvx --from twine twine check dist/*
   ```
5. (建議)先在 TestPyPI smoke test:
   ```bash
   uvx --from twine twine upload --repository testpypi dist/*
   uvx --index-url https://test.pypi.org/simple/ sqlserver-semantic-mcp
   ```
6. 發佈到 PyPI:
   ```bash
   uv publish                     # 使用 UV_PUBLISH_TOKEN 或 ~/.pypirc
   ```

> 設定一次性的 PyPI API token:`export UV_PUBLISH_TOKEN=pypi-…`(或在 `~/.pypirc` 的 `[pypi]` 區塊配置)。

### 專案結構

```
sqlserver_semantic_mcp/
├── config.py                         — 以環境變數為基礎的 Pydantic 設定
├── main.py                           — stdio 伺服器 + 啟動 + 背景任務
├── domain/
│   ├── enums.py                      — TableType、ObjectType、CacheStatus、RiskLevel、SqlOperation
│   └── models/                       — Column、Table、ForeignKey、Index、Relationship、DbObject
├── policy/
│   ├── models.py                     — PolicyProfile / PolicyOperations / PolicyConstraints / PolicyScope
│   ├── loader.py                     — JSON 載入,附優雅回退
│   ├── analyzer.py                   — regex 為基礎的 SQL 意圖擷取
│   └── enforcer.py                   — policy 裁決(allow/reject + 原因)
├── infrastructure/
│   ├── connection.py                 — pymssql 連線與輔助工具
│   ├── background.py                 — 背景 semantic 填入迴圈含退避
│   ├── cache/
│   │   ├── store.py                  — SQLite DDL + 初始化
│   │   ├── structural.py             — hash + 預熱 + 快照持久化
│   │   ├── probe.py                  — L1 catalog 指紋 + 漂移 diff
│   │   ├── revalidation.py           — 節流的 stale-while-revalidate 編排
│   │   └── semantic.py               — 分析/定義 I/O + pending 佇列
│   └── queries/                      — SQL Server 查詢(metadata / 註解 / 物件 / 探針)
├── services/                         — 6 個服務(metadata / relationship / semantic / object / policy / query)
└── server/
    ├── app.py                        — MCP Server、工具註冊表、JSON envelope
    ├── tools/                        — 7 個工具模組(每個能力群組一個)
    └── resources/                    — schema / analysis / summary URI
```

### 測試慣例

- **單元測試** 使用記憶體或暫存目錄的 SQLite,並 mock pymssql。
- **整合測試** 標記為 `@pytest.mark.integration`,未設定 `SEMANTIC_MCP_MSSQL_SERVER` 時跳過。
- Pydantic 模型直接測試;infrastructure 層以 mocked connection 驗證。

---

## 疑難排解

### Linux 上 pymssql / FreeTDS 安裝失敗

`pymssql` 連結 FreeTDS。在 Debian/Ubuntu 上,執行 `pip install` 前先安裝必要系統套件:

```bash
sudo apt-get install -y libssl-dev libkrb5-dev freetds-dev
pip install pymssql
```

Alpine / Docker 環境:`apk add freetds-dev openssl-dev krb5-dev`。

### 「Cannot open server」或連線被拒

- 確認 server 名稱與連接埠正確(`SEMANTIC_MCP_MSSQL_SERVER`、`SEMANTIC_MCP_MSSQL_PORT`)。
- 在 SQL Server Configuration Manager 中檢查 TCP/IP 是否啟用。
- 若使用具名實例(例如 `MY-PC\SQLEXPRESS`),確認 SQL Server Browser 服務正在執行,以動態解析連接埠。
- 若使用非預設連接埠,請顯式設定 `SEMANTIC_MCP_MSSQL_PORT` — 連接埠固定時不需 SQL Server Browser。
- 檢查防火牆規則:port 1433(或自訂連接埠)必須能從執行 MCP server 的機器連通。

### 「Login failed for user」

- 確認 `SEMANTIC_MCP_MSSQL_USER` 與 `SEMANTIC_MCP_MSSQL_PASSWORD` 正確。
- 驗證 SQL Server 已啟用 SQL Server Authentication(Server Properties → Security → SQL Server and Windows Authentication mode)。
- Azure SQL 視 driver 版本而定,使用者格式可能需為 `user@servername`。

### Linux 或 macOS 無法使用 Windows 驗證

pymssql 在非 Windows 平台不支援 Windows Authentication(Kerberos/NTLM)。請改用 SQL 認證(`MSSQL_USER` + `MSSQL_PASSWORD`)。`SEMANTIC_MCP_MSSQL_WINDOWS_AUTH=true` 僅在 Windows 上有效。

### LocalDB 無法連線

- LocalDB 僅限 Windows,且使用 named pipe 而非 TCP。
- `SEMANTIC_MCP_MSSQL_SERVER` 必須採用 `(localdb)\MSSQLLocalDB` 格式(或你的實例名稱)。
- 設定 `SEMANTIC_MCP_MSSQL_WINDOWS_AUTH=true`;LocalDB 預設不支援 SQL 認證。
- 在 Windows 終端執行 `sqllocaldb info` 列出可用實例並確認其運行中。

### Policy 檔找不到或被忽略

當 policy 檔無法讀取時(缺失、無法存取或 JSON 損壞),server 會回退為內建唯讀模式並記錄警告。請檢查啟動 log 中含 `policy` 字樣的行。若 `SEMANTIC_MCP_POLICY_FILE` 設為相對路徑,會以行程工作目錄為基準解析 — 為避免歧義,建議使用絕對路徑。

### 伺服器啟動但工具回傳空結果

Structural Cache 可能尚未填入。檢查啟動 log 中的預熱進度。可呼叫 `refresh_schema_cache` MCP 工具強制重新整理。也請確認連線資料庫使用者具有 `VIEW DEFINITION` 權限;否則物件定義與註解不會出現在快取中。

---

## 安全設計

- **預設唯讀**:若未設定 policy,只允許 `SELECT`。
- **強制 SQL 驗證**:每個查詢在抵達 `cursor.execute()` 前,都會經過意圖分析器與 policy 執行器。
- **拒絕危險敘述**:`DROP` / `TRUNCATE` 被分類為 `CRITICAL` 風險等級;除非明確允許,否則封鎖。
- **Schema 感知存取控制**:`allowed_schemas` 會拒絕隱含 schema 的查詢,防止利用 schema 預設值繞過。
- **Policy 強化**:格式錯誤的 policy 檔會回退為唯讀,而不是讓伺服器崩潰。

---

## 限制 / 未來工作

- SQL 意圖分析器為 regex 基底,非完整 T-SQL parser — CTE 內定義的名稱可能被視為表。若有疑慮,請先使用 `plan_or_execute_query(mode="validate")`。
- 索引查詢使用的 `STRING_AGG` 需 SQL Server 2017+。更舊版本需替代查詢。探針對 `nvarchar(max)` 定義使用的 `HASHBYTES` 需 SQL Server 2016+。
- 探針的欄位/索引 checksum 使用 `CHECKSUM_AGG`/`BINARY_CHECKSUM`,屬 heuristic,理論上存在極微小的碰撞機率。`refresh_schema_cache` 永遠執行完整重抓,不受此影響。
- 加密模組(`WITH ENCRYPTION`)不暴露定義內文;其探針指紋退回僅用 `modify_date`。
- `sys.extended_properties` 的讀取需要 `VIEW DEFINITION` 權限;受限物件的註解不會出現在快取中。
- 背景填入採單一 worker;在非常龐大的 schema 上,Semantic Cache 可能需要時間才能收斂(使用 `refresh_schema_cache` 可強制結構重新整理;semantic 分類仍會延遲填入)。

---

## 授權

本專案採用 MIT 授權 — 詳見 `LICENSE`。
