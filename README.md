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
| 探索結構與關聯，快取 metadata 與語意分析。 | 先看摘要，需要時再取欄位、索引與樣本。 | 每組使用自己的帳號、權限、預算與快取，預設唯讀。 |

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

每組連線使用不同名稱。以下五組可在同一次精靈問答中建立，也可日後再執行 `setup` 新增。新配置的回應預算全部預設為 `medium`。

| MCP 名稱 | 用途範例 | 權限 | 回應預算 |
|:---|:---|:---|:---|
| `erp_prod` | 正式 ERP | 唯讀 | `medium` |
| `erp_test` | 測試 ERP | 受限寫入 | `medium` |
| `reporting` | 報表資料庫 | 唯讀 | `medium` |
| `crm` | 客戶資料庫 | 唯讀 | `medium` |
| `warehouse` | 倉儲資料庫 | 唯讀 | `medium` |

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

受限寫入不開放 `DELETE`、DDL、`EXEC`、寫入子查詢、CTE 或 `OUTPUT`。新配置預設最多回傳 1,000 列、最多影響 100 列，查詢逾時為 30 秒。寫入超過影響列數上限時會回滾。

MCP 的 policy 不會授予 SQL Server 權限。每組連線應使用符合用途的專用帳號，正式環境建議由資料庫端限制為唯讀。精靈會測試登入與 metadata 讀取，不會實際寫入來驗證權限。

### 回應預算

**保持 `medium` 即可。** `get_tables` 預設最多列出 500 張表；查詢使用 `sample` 模式時最多提供 50 列樣本。這是回應筆數控制，並非固定 Token 或金額上限。

減少 AI 成本時，先請 Agent 找相關資料表，再取必要欄位；查詢先看摘要或樣本，避免直接要求完整資料。可用 `tool_metrics` 查看工具回應的位元組大小，實際費用仍由 Client、模型與用量決定。啟用多個 MCP 可能增加工具描述的上下文，建議只在 Client 啟用需要的連線。

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
uv run pytest tests/unit
```

</details>

---

<div align="center">

[回報問題](https://github.com/LukeStackLab/sqlserver-semantic-mcp/issues) · [MIT License](LICENSE)

</div>
