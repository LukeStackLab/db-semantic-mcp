<div align="center">

# ⚡ sqlserver-semantic-mcp

### 不只是執行 SQL —— 這是為 AI Agent 量身打造的 **SQL Server 企業級語意與安全大腦**

讓 AI 在 0.5 秒內看懂你的資料庫、自動找出 Join 路徑、並被安全閘門牢牢守護。

[![PyPI](https://img.shields.io/pypi/v/sqlserver-semantic-mcp?color=blue&label=PyPI)](https://pypi.org/project/sqlserver-semantic-mcp/)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://pypi.org/project/sqlserver-semantic-mcp/)
[![Protocol](https://img.shields.io/badge/Protocol-MCP-8A2BE2)](https://modelcontextprotocol.io)
[![License](https://img.shields.io/badge/License-MIT-green)](./LICENSE)
[![SQL Server](https://img.shields.io/badge/SQL%20Server-2017%2B-CC2927?logo=microsoftsqlserver&logoColor=white)](#)

**[🚀 3 分鐘上手](#-3-分鐘極速上手)** · **[🎯 為什麼選它](#-為什麼你需要語意層而不是另一個-sql-執行器)** · **[🛠 殺手級工具箱](#-殺手級特性與工具箱)** · **[🛡 生產級防護](#-生產級安全配置)**

📖 完整技術文件:[English](./README.en.md) | [繁體中文](./README.zh-TW.md)

</div>

---

> 💡 **一句話總結:** 把這個 MCP 伺服器接上 Claude / Cursor,你的 AI 就從「對著幾百張表盲目試錯的實習生」變成「熟知 schema、會走最短 Join 路徑、且絕不越權的資深 DBA 搭檔」。

---

## 🎯 為什麼你需要「語意層」,而不是另一個 SQL 執行器?

把資料庫直接丟給 AI 的下場,每個試過的人都知道。**傳統 SQL MCP 只給 AI 一把刀;我們給的是一整個腦。**

| 😱 傳統 SQL MCP 的致命痛點 | ⚡ sqlserver-semantic-mcp 的解法 |
|---|---|
| AI **盲目瞎猜 Join 條件**,幻覺出不存在的外鍵,查詢結果一片混亂 | 🧭 **BFS 圖形演算法**走訪真實外鍵圖,`find_join_path` 自動探索兩張表之間的**最短 Join 路徑**(最多 5 hops) |
| 每個問題都**重複轟炸系統表**(`sys.*`、`INFORMATION_SCHEMA`),拖垮線上資料庫 | 🚀 **雙層 SQLite 快取 + 結構預熱**:結構與語意分析全部本地持久化,**毫秒級回應、線上 DB 零壓力**,並由輕量 L1 探針自動偵測 schema 漂移、精準失效 |
| AI 一個 `UPDATE` 忘了寫 `WHERE`,**刪庫跑路**不再是段子 | 🛡 **JSON Policy 安全閘門(Guardrails)**:預設唯讀、強制 `WHERE` 子句、最大回傳/影響列數上限、schema/table 白名單——每一句 SQL 都先過安檢 |
| 面對**上百張資料表**,AI 在 token 海裡迷路,上下文爆炸 | 🧠 **自動語意分類**:每張表自動標記為 Fact / Dimension / Lookup / Bridge / Audit,欄位自動辨識審計欄、軟刪除欄——**幫 AI 畫好重點再上場** |
| Schema 改了,AI 還在用三天前的舊認知回答 | 🔄 **三重 Hash 版本控制 + Catalog 指紋探針**:欄位型別從 `nvarchar(4)` 改成 `nvarchar(10)` 都逃不過,變更自動連鎖刷新,**永不過期、也永不浪費重抓** |

> 🎯 **結果:** 更少的 token 消耗、更少的錯誤重試、更快的正確答案——以及一個敢真正放進生產環境的 AI 資料庫工具。

---

## 🚀 3 分鐘極速上手

此原始碼版本支援互動式配置多個獨立 MCP：在專案目錄執行 `uv sync --extra dev`，再執行 `uv run sqlserver-semantic-mcp setup`。精靈逐步測試連線、保存憑證並合併 Client 設定。每組預設唯讀、預算 `medium`，各自使用獨立程序、policy 與快取。完整流程請見 [互動式安裝](./README.zh-TW.md#互動式安裝與多組獨立-mcp)。

以下是已發佈版本的手動設定方式。

不需要 `git clone`、不需要建虛擬環境、不需要安裝任何東西到專案裡。只要 [`uv`](https://docs.astral.sh/uv/getting-started/installation/)(一行指令安裝:`curl -LsSf https://astral.sh/uv/install.sh | sh`),`uvx` 會自動下載、快取並啟動伺服器。

> ⚠️ 把範例中的 `localhost` / `YourDatabase` / `sa` / `YourPassword` 換成你的 SQL Server 連線資訊即可。

### 🖥 Claude Desktop(複製貼上即用)

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

存檔後重啟 Claude Desktop —— 完成。✅

### ⌨️ Cursor(複製貼上即用)

在專案根目錄建立 `.cursor/mcp.json`(或全域 `~/.cursor/mcp.json`):

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

### 🤖 Claude Code CLI(一行指令)

```bash
claude mcp add sqlserver-semantic -- uvx sqlserver-semantic-mcp \
  -e SEMANTIC_MCP_MSSQL_SERVER=localhost \
  -e SEMANTIC_MCP_MSSQL_DATABASE=YourDatabase \
  -e SEMANTIC_MCP_MSSQL_USER=sa \
  -e SEMANTIC_MCP_MSSQL_PASSWORD=YourPassword
```

### 🧪 30 秒煙霧測試(可選)

```bash
SEMANTIC_MCP_MSSQL_SERVER=localhost \
SEMANTIC_MCP_MSSQL_DATABASE=YourDatabase \
SEMANTIC_MCP_MSSQL_USER=sa \
SEMANTIC_MCP_MSSQL_PASSWORD=YourPassword \
  uvx sqlserver-semantic-mcp
```

看到快取初始化與工具註冊的啟動日誌,就代表一切就緒。接著對你的 AI 說:

> *「幫我找出 Orders 和 Customers 之間怎麼 Join,然後算出上個月的客單價。」*

然後看著它**第一次就答對**。🎉

---

## 🛠 殺手級特性與工具箱

**12 個 MCP 工具**(由早期 29 個精簡整合而成,能力不減、token 更省),分為三大核心模組——每一個都為「讓 AI 少走彎路」而生:

### 🧭 1. 探索與語意模組 —— 讓 AI 真正「看懂」資料庫

| 工具 | 它為你做什麼 |
|---|---|
| 🔍 `discover_relevant_tables` | 用自然語言關鍵字直達相關資料表,告別人肉翻 schema |
| 🧬 `describe_table`(`detail`) | 回傳欄位、主鍵、FK,並**自動判定 Fact / Dimension / Lookup / Bridge / Audit** 角色;`detail=brief` 就內含分類與 Join/篩選重點欄位,`standard` / `full` 依需要升級到完整欄位與索引 |
| 🗺 `find_join_path`(`score`) | **BFS 走訪外鍵圖**,自動算出兩張表之間的最短 Join 鏈;`score=true` 再附上信心分數,排除 Bridge / Audit / Lookup 干擾——不再瞎猜 |
| 🪜 `get_dependency_chain` | 沿外鍵展開整條相依鏈,影響分析一目了然 |
| 📜 `describe_object`(`detail`) | 檢視表 / 預存程序 / 函式的定義、相依物件、讀寫拆解(哪些表被讀、哪些被寫);`detail=full` 才附上完整定義文字 |

### 🚀 2. 效能與快取模組 —— 毫秒級回應,線上 DB 零負擔

| 工具 / 機制 | 它為你做什麼 |
|---|---|
| ⚡ **雙層 SQLite 快取**(內建) | 結構快取 + 語意快取全部本地持久化,重啟免重抓(cache-first 啟動) |
| 📡 **L1 Catalog 指紋探針**(內建、全自動) | 純 catalog 查詢、毫秒級偵測 schema 漂移——欄位型別、長度、索引、檢視表內文變更全部捕捉,**只刷新真正變動的表** |
| 🔄 `refresh_schema_cache` | 一鍵強制全量刷新,語意分析自動連鎖重算 |
| 📊 `tool_metrics`(`action`) | `action=get` 內建每個工具的回應大小量測,token 成本看得見;`action=reset` 一鍵清空歷史紀錄 |

> 💡 下一步所需的上下文打包(原 `bundle_context_for_next_step`)現在是 MCP **Resource**——讀取 `semantic://bundle/joining/{schema}.{table}` 即可,不再需要額外呼叫工具。

### 🛡 3. 安全與執行防護模組 —— DBA 敢簽核的 AI 工具

| 工具 | 它為你做什麼 |
|---|---|
| 🚦 `plan_or_execute_query`(`mode`) | 智慧路由:`mode=auto` 安全的查詢直接執行、危險的自動轉入驗證;`mode=validate` 只驗證並回傳風險等級與原因;`mode=dry_run` 先看執行計畫與樣本、完全不動資料 |
| 🔐 `get_execution_policy`(`reload`) | 查看目前生效的執行政策:操作權限、`WHERE` 強制、列數上限;`reload=true` 立即重新載入 policy 檔案 |

---

## 🛡 生產級安全配置

> **這是讓企業主管與 DBA 點頭的章節。** 預設行為就是唯讀——AI 連一個 `UPDATE` 都碰不到,除非你明確授權。

### 環境變數防護(開箱即生效)

```bash
SEMANTIC_MCP_MAX_ROWS_RETURNED=1000    # SELECT 回傳列數上限
SEMANTIC_MCP_MAX_ROWS_AFFECTED=100     # DML 影響列數上限(超過即拒絕)
SEMANTIC_MCP_QUERY_TIMEOUT=30          # 查詢逾時秒數
SEMANTIC_MCP_PROBE_INTERVAL_S=60       # Schema 漂移探針節流窗口
```

### JSON Policy 安全閘門(進階分級授權)

用一個 JSON 檔定義 AI 的權限邊界,透過 `SEMANTIC_MCP_POLICY_FILE` 載入:

```json
{
  "active_profile": "readonly",
  "profiles": {
    "readonly": {
      "operations": { "select": true },
      "constraints": { "max_rows_returned": 1000, "query_timeout_seconds": 30 }
    },
    "read_write_safe": {
      "operations": { "select": true, "insert": true, "update": true },
      "constraints": {
        "require_where_for_update": true,
        "max_rows_affected": 100
      }
    }
  }
}
```

**安全設計三原則:**

- 🔒 **預設唯讀** —— policy 檔遺失或損壞時,自動回退為內建唯讀模式,絕不裸奔
- 🚧 **強制 `WHERE`** —— `require_where_for_update` 讓「忘了寫 WHERE 的 UPDATE」在執行前就被攔截
- 🎯 **範圍白名單** —— schema / table 層級的允許與拒絕清單,敏感表 AI 連看都看不到

完整 policy 範例請見 [`config/policy.example.json`](./config/policy.example.json)。

---

## 🧩 它是怎麼運作的?

```
AI Agent (Claude / Cursor / Codex)
        │  MCP (stdio)
        ▼
┌─────────────────────────────────────────────┐
│  sqlserver-semantic-mcp                     │
│                                             │
│  🧠 語意層    分類 / Join 路徑 / 相依分析     │
│  🛡 政策閘門  唯讀預設 / WHERE 強制 / 列數上限 │
│  ⚡ 快取層    雙層 SQLite + L1 漂移探針        │
└─────────────────┬───────────────────────────┘
                  │  只在必要時查詢
                  ▼
            🗄 SQL Server (2017+)
```

- **三重 Hash 版本控制** —— 結構 / 物件 / 註解三組指紋,任何變更精準觸發失效
- **逐表精準失效** —— 只有真正變動的表重新分析,其餘快取原封不動
- **背景漸進填入** —— 語意分析在背景批次收斂,啟動永遠是秒級

---

## 📚 想深入了解?

| 文件 | 內容 |
|---|---|
| 📖 [README.en.md](./README.en.md) | 完整英文技術文件:架構、12 個工具全覽、環境變數、部署、疑難排解 |
| 📖 [README.zh-TW.md](./README.zh-TW.md) | 完整繁體中文技術文件 |
| 🛡 [config/policy.example.json](./config/policy.example.json) | 三段式權限 Policy 範本(readonly / read_write_safe / admin) |

---

## ⭐ 喜歡這個專案?

如果它幫你的 AI 第一次就寫對 Join、或幫你的 DBA 睡了個好覺——

**請點一下右上角的 Star ⭐,讓更多人找到它!**

[回報問題](https://github.com/LukeStackLab/sqlserver-semantic-mcp/issues) · [貢獻指南](./README.en.md#development) · MIT License © LukeStackLab
