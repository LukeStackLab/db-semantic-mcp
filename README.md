<div align="center">

# sqlserver-semantic-mcp

**讓 AI Agent 探索 SQL Server、找出資料表關聯，並在設定的權限內查詢資料。**

[![PyPI](https://img.shields.io/pypi/v/sqlserver-semantic-mcp?color=blue&label=PyPI)](https://pypi.org/project/sqlserver-semantic-mcp/)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://pypi.org/project/sqlserver-semantic-mcp/)
[![Protocol](https://img.shields.io/badge/Protocol-MCP-8A2BE2)](https://modelcontextprotocol.io)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![SQL Server](https://img.shields.io/badge/SQL%20Server-2017%2B-CC2927?logo=microsoftsqlserver&logoColor=white)](#快速安裝)

[![繁體中文](https://img.shields.io/badge/Language-%E7%B9%81%E9%AB%94%E4%B8%AD%E6%96%87-2563EB?style=for-the-badge)](README.zh-TW.md)
[![English](https://img.shields.io/badge/Language-English-64748B?style=for-the-badge)](README.en.md)

[快速安裝](#快速安裝) · [安裝精靈](#安裝精靈) · [多組連線](#多組連線) · [權限與成本](#權限與成本) · [配置與維護](#配置與維護)

支援 **Codex · Claude Code · Claude Desktop · Cursor**

</div>

| 探索資料庫 | 控制回應大小 | 獨立管理連線 |
|:---|:---|:---|
| 探索結構與關聯，快取 metadata 與語意分析。 | 直接取得完整欄位、索引與查詢結果，超量時自動截斷並標示。 | 每組使用自己的帳號、權限、輸出上限與快取，預設唯讀。 |

---

## 快速安裝

> **開始前**：準備 Git、可連線的 SQL Server 2017+，以及 SQL 帳號與密碼。精靈使用 SQL 認證；帳號須具備所需的讀取與 metadata 權限。

### 1. 安裝 uv

**macOS / Linux**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows PowerShell**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

安裝後重新開啟終端機。其他方式請見 [uv 官方安裝說明](https://docs.astral.sh/uv/getting-started/installation/)。

### 2. 安裝 MCP

以下指令適用於 Windows、macOS 與 Linux。若已取得本專案，直接在專案目錄執行最後一行。

```bash
git clone https://github.com/LukeStackLab/sqlserver-semantic-mcp.git
cd sqlserver-semantic-mcp
uv tool install --python 3.11 --reinstall .
```

目前請從包含 `setup` 功能的原始碼版本安裝。uv 會在需要時下載 Python，並建立持久工具環境。若出現 PATH 提示，執行 `uv tool update-shell` 後重新開啟終端機，詳見 [uv 工具環境說明](https://docs.astral.sh/uv/concepts/tools/)。

## 安裝精靈

若使用 Claude Code，先切換到要使用 MCP 的專案目錄，精靈會將設定寫入該專案的 `.mcp.json`。

```bash
sqlserver-semantic-mcp setup
```

1. 選擇 MCP Client。只偵測到一個 Client 時，精靈會自動選用。
2. 輸入 MCP 名稱、SQL Server 主機、port、資料庫、帳號與密碼。名稱使用英文字母、數字、底線或連字號，例如 `erp_prod`。預設 port 為 `1433`，密碼輸入不顯示。
3. 回答「允許新增與修改資料？」。新連線直接按 Enter 使用唯讀；只有需要寫入時才回答 `y`。
4. 連線測試成功後，回答「再新增一組連線？」。輸入 `y` 繼續新增，輸入 `n` 完成。
5. 確認摘要。精靈會保存配置、驗證 MCP 握手，再合併 Client 設定並備份原檔。

> **完成後**：重新載入 MCP 設定或重新啟動 Client。Claude Code 的專案信任提示仍由 Client 處理。Claude Desktop 的預設設定路徑支援 macOS 與 Windows。

## 多組連線

每組連線使用不同名稱。以下五組可在同一次精靈問答中建立，也可日後再執行 `setup` 新增。每組連線可在 `connection.json` 調整輸出上限（`max_response_bytes`）。

| MCP 名稱 | 用途範例 | 權限 |
|:---|:---|:---|
| `erp_prod` | 正式 ERP | 唯讀 |
| `erp_test` | 測試 ERP | 受限寫入 |
| `reporting` | 報表資料庫 | 唯讀 |
| `crm` | 客戶資料庫 | 唯讀 |
| `warehouse` | 倉儲資料庫 | 唯讀 |

在 AI 對話中指定 MCP 名稱：

> 使用 `erp_prod`，找出訂單與客戶資料表的關聯。
>
> 接下來改用 `reporting`，統計上個月的銷售額。

Agent 依名稱選用對應工具。每個 MCP 程序固定自己的配置；回應中的 `source` 會標示連線名稱與資料庫。

## 權限與成本

### 操作權限

只需決定是否允許受限寫入：

| 設定 | 允許操作 |
|---|---|
| 唯讀，預設 | `SELECT` |
| 受限寫入 | `SELECT`、單一 `INSERT ... VALUES`、帶 `WHERE` 的單一 `UPDATE` |

受限寫入不開放 `DELETE`、DDL、`EXEC`、寫入子查詢、CTE 或 `OUTPUT`。新配置預設最多回傳 1,000 列、最多影響 100 列，查詢逾時為 30 秒。這些固定連線配置的寫入超過影響列數上限時會回滾。

MCP 的 policy 不會授予 SQL Server 權限。每組連線應使用符合用途的專用帳號，正式環境建議由資料庫端限制為唯讀。精靈會測試登入與 metadata 讀取，不會實際寫入來驗證權限。

### 輸出上限

`response_mode`、`return_mode`、`detail` 與 `token_budget_hint` 已移除。舊 `connection.json` 的 `budget` 可繼續載入，但會被忽略；請改用下表的輸出上限。查詢結果沒有「摘要／樣本」等模式，也沒有詳細度分級。`plan_or_execute_query` 執行 `SELECT` 後回傳欄位與資料列，並套用三道硬性上限，先到者生效：

| 上限 | 預設 | 設定 |
|:---|:---|:---|
| 列數 | 1000 | `max_rows_returned`，呼叫時可用 `max_rows` 調低 |
| 欄位名稱與資料列的序列化位元組 | 65536 | `max_response_bytes` |
| 單一儲存格字元 | 2000 | `max_cell_chars`（二進位以十六進位截斷） |

`max_response_bytes` 計算欄位名稱與保留資料列的 JSON 大小，不含 validation、cache 等回應封裝欄位。

被截斷時回應包含 `truncated=true` 與 `truncation_reason`（`row_limit` 或 `byte_limit`）；此時 `total_row_count` 為 `null`。縮小結果的方式是減少欄位、加 `WHERE`／`TOP`，或改用聚合查詢。`get_tables` 預設最多列出 500 張表（`limit` 可調整）。

這些是輸出大小的硬上限，並非固定 Token 或金額上限，實際費用仍由 Client、模型與用量決定。可用 `tool_metrics` 查看工具回應位元組。啟用多個 MCP 可能增加工具描述的上下文，建議只在 Client 啟用需要的連線。

<details>
<summary><b>查詢、關聯與工具集合</b></summary>

- `row_count` 表示本次保留的列數。只有讀完結果集時，`total_row_count` 才是精確總數，否則為 `null`。`truncated` 表示結果仍有省略。需要完整總數時，明確查詢 `COUNT_BIG`。
- `describe_table` 一律回傳完整欄位、鍵、索引與說明；`columns=["Id", "Amount"]` 可只取需要的欄位，保留精確 SQL 型別、完整 `join_constraints` 與必要 JOIN 欄位。產生 SQL 前先確認欄位與完整外鍵條件。
- 複合外鍵以全部 `column_pairs` 表示。存在多個合理的 FK 角色時，`find_join_path` 回傳候選與 `ambiguous`，不自動選擇。分數是 heuristic；唯一性證據不足時 cardinality 為未知。
- 索引含 XML／spatial 資訊；cardinality 推論仍只採用有效的唯一鍵證據。分類依目前 scope 可見的 FK 計算，受限分類不跨 scope 共用 cache。
- 中文搜尋使用本機名稱、欄位、註解與別名。明確指定的 `schema.table` 會優先於一般文字比對。可在 `connection.json` 設定 `"discovery_aliases": {"供應商": ["Vendor", "Supplier"]}`，不呼叫外部模型。
- 被拒絕的 SQL 會附 `denial`：`layer`（`policy`／`keyword_guard`／`fixed_profile`）、`rule`、`message`、`hint`，關鍵字攔截另有 `matches`（關鍵字與行、欄位）。依 `rule` 與 `hint` 修改 SQL，不需要猜。
- 允許單一 `SELECT` 前面加純量 `DECLARE @v type [= 運算式]` 與 `SET @v = 運算式`。初始值不可讀取物件（子查詢、資料表、自訂函數），不支援表變數與 `SET NOCOUNT` 等選項。仍不支援 `SELECT INTO`、暫存表、`EXEC` 與多個結果集。
- 受限寫入超過 `max_rows_affected` 時，固定連線配置（精靈建立的 `connection.json`）一律 rollback。自行用環境變數或 policy 檔的配置，預設只回報 `exceeded_cap`；設 `SEMANTIC_MCP_STRICT_ROWS_AFFECTED_CAP=true` 或呼叫時帶 `affected_rows_policy="strict"` 可改為 rollback。
- `plan_or_execute_query(validate_result_schema=true)` 可在執行前向 SQL Server 檢查結果 metadata。這會增加一次資料庫查詢；暫存表等語法可能無法描述。預設不啟用，也不保證業務答案正確。

新連線可在安裝時選擇工具集合：

```bash
sqlserver-semantic-mcp setup --tool-profile explore
```

| 集合 | 工具數 | 用途 |
|---|---:|---|
| `all`，預設 | 12 | 全部功能 |
| `query` | 2 | 已知 SQL 的 policy 檢查與執行 |
| `explore` | 8 | 找表、欄位與關聯，再執行查詢 |
| `maintenance` | 3 | policy、快取更新與 metrics |

既有配置可修改 `connection.json` 的 `tool_profile` 後重新啟動。省略 setup 的選項會保留原設定。工具集合不會授予資料庫權限。工具的 JSON 清單結果統一放在 `data`，並提供 MCP `structuredContent` 與 `outputSchema`；失敗使用 `isError`。

</details>

<details>
<summary><b>授權邊界、快取與相容性</b></summary>

所有 SQL 授權固定使用 T-SQL AST，並檢查全部 statement 與物件來源。無法確認的語法、remote sources、動態 SQL、交易控制及 `SELECT INTO` 會被拒絕。`FOR SYSTEM_TIME` 與 external DDL 暫不支援；必須能解析並授權實際歷史表或外部來源後才能開放。執行目前只接受單一 statement；`validate` 可分析 policy 允許的 batch。即使設定 `INTENT_ANALYZER=regex`，授權仍使用 AST。

SQL、metadata、resources、FK 與物件依賴共用 scope。`allowed_tables` 可使用表名、`schema.name` 或 `database.schema.name`；名稱含點或右括號時使用 T-SQL quoted identifiers。allow 保留大小寫以避免放寬 case-sensitive 資料庫權限；deny 會拒絕大小寫變體。受限 scope 會保守拒絕可能另行讀取物件 metadata 的函式，包括 `FileTableRootPath` 與 `STATS_DATE`。正式環境仍須由專用 SQL 帳號限制實際存取。

明確指定但遺失或無效的 policy 會停止啟動，不再回退至無 scope 的 readonly。未知 policy 欄位也會拒絕。server strict affected-row cap 不能被 caller 的 `report` 降低。寫入已確認 commit 後，即使關閉連線失敗仍保留成功結果。若 commit／rollback 回覆不確定，會標示 `transaction_state="unknown"` 與 `automatic_retry_allowed=false`；此時 `executed=false` 不代表已回滾，必須先確認資料庫結果，不能直接重試。無法確認安全範圍的物件定義會省略，並附 `definition_withheld`／`dependencies_omitted`。

既有 SQLite cache 自動 migration。缺少 FK constraint identity 或有效 unique index 證據的舊 metadata 會標示不完整；重新整理後才取得完整判斷。Schema／comments snapshot 預設最多 300 秒，工具與 resources 都會觸發 single-flight 背景更新；回應 `cache` 的擷取時間與 stale 狀態對應本次回傳內容使用的 snapshot；背景更新不會把舊內容標為新鮮。refreshing／refresh_failed 則反映目前更新狀態。內容未提供 snapshot 身分時，會標示 `cache.snapshot_unknown=true`，最新快取狀態另放在 `cache.latest_snapshot`；該時間不代表內容的 snapshot。每次完整更新都會比較舊 fingerprint，並同步失效 body 已變更的物件定義與依賴分析，包含手動更新。資料庫無法連線時可讀舊 cache，並明示狀態。

每個程序預設最多 4 個 DB worker。取消等候不會立即中止同步 driver；工作完成並關閉連線後才釋放容量。`connection.json` 可設定 `db_worker_limit` 與 `schema_cache_ttl_s`。`tool_metrics` 記錄 bytes、耗時分位數、worker 等待、DB 工作耗時、取得列數、可觀察的 cache hit 與錯誤類型，不記錄 SQL、資料列或密碼。DB 工作耗時包含連線與讀取，並非 SQL Server CPU 時間。

工具說明已精簡，仍保留完整 `outputSchema` 與伺服器回應驗證。AST 路由共用同一次分析結果；關鍵字檢查與 cache 新鮮度計算也已去除重複。未使用的內部 domain models 與 enum 已移除。已知 SQL 可選 `query` 集合降低上下文。實際 Token 與效能須依 Client 及資料庫負載量測。執行環境需要 MCP Python SDK 1.27+。

</details>

## 配置與維護

每組配置位於 `connections/<MCP 名稱>/`，包含 `connection.json`、`policy.json` 與獨立快取。密碼優先保存於系統憑證庫。

<details>
<summary><b>配置位置與密碼保存</b></summary>

| 平台 | 預設配置根目錄 |
|---|---|
| macOS | `~/Library/Application Support/sqlserver-semantic-mcp/connections/` |
| Windows | `%APPDATA%\sqlserver-semantic-mcp\connections\` |
| Linux | `$XDG_CONFIG_HOME/sqlserver-semantic-mcp/connections/`；未設定時使用 `~/.config/sqlserver-semantic-mcp/connections/` |

無法使用憑證庫時，macOS / Linux 可明確同意改用明碼憑證檔，目錄權限為 `0700`、檔案權限為 `0600`；Windows 使用系統憑證庫。密碼不寫入 Client 設定或 `connection.json`。

</details>

<details>
<summary><b>更新、診斷與既有啟動方式</b></summary>

- **更新連線**：重新執行 `setup`，輸入既有 MCP 名稱並確認更新。更新後重新載入 Client。
- **檢查連線**：將下列路徑換成精靈顯示的 `connection.json` 完整路徑。

```bash
sqlserver-semantic-mcp doctor --config "PATH_TO_CONNECTION_JSON"
```

Client 註冊會使用安裝時的 Python 環境。移除或搬動該環境後，需重新安裝並執行 `setup`。若要更新已安裝套件，在新版原始碼目錄執行 `uv tool install --python 3.11 --reinstall .`。

既有環境變數與 `.env` 啟動方式仍可使用，範例見 [`.env.example`](.env.example)。精靈建立的固定配置不受工作目錄 `.env` 或繼承的 `SEMANTIC_MCP_*` 覆寫。

</details>

<details>
<summary><b>開發與測試</b></summary>

```bash
uv sync --locked --extra dev
uv run pytest
uvx ruff check .
uvx ruff format .
```

</details>

---

<div align="center">

[回報問題](https://github.com/LukeStackLab/sqlserver-semantic-mcp/issues) · [MIT License](LICENSE)

</div>
