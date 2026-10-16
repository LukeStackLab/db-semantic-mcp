# DB Semantic MCP 0.8.0 最終驗收

產品已完成 SQL Server／Oracle adapter、共用 MCP 工具與 policy、來源隔離、synonym／DB link 解析、安裝精靈、doctor、v1 遷移、JSON v2 範例、文件與本機 Docker demo。Distribution 為 `lukestack-db-semantic-mcp`，CLI 為 `db-semantic-mcp`。本次建立本機 Git commits，未 push 或發布套件。

## 審查與修正

整個分支審查以 `fd9f3c3..4989628` 為範圍。最終集中修正為 `65c60d9`，聚焦複審範圍為 `4989628..6a30174`。三項 finding 全部 ADDRESSED，未確認新增 Critical、Important 或 Minor 問題。

| Finding | 修正與驗證 |
|---|---|
| I1：Oracle MAP／virtual column 隱含執行自訂程式碼 | 同一執行連線讀可信 SYS catalog，在 caller parse／execute 前證明整個 target。Thin／Thick 各六個負向案例均為零 caller SQL、零自主寫入，scalar 正例成功，臨時 schema 全部清除。 |
| I2：Oracle env local cache 共用連線身分 | 正式入口在 cache 初始化／重用前建立 provider、endpoint、principal、options、policy 與 source namespace。實際四次 MCP 啟動建立三個獨立 namespace，相同身分再次啟動穩定重用；SQLite relationship 回歸確認不跨 principal 讀取。 |
| M1：README 契約落差 | 三份 README 對齊 Oracle strict cap、非固定 SQL Server report、v2 budget 拒絕、migration、provider doctor 欄位與有界不完整 header 結果。 |

最終複審檢查修正與直接介面，沒有把過去的完整審查再做一次，也沒有把實作者提供的測試結果冒稱為 reviewer 重跑。

## 最終驗證

- 完整測試：**935 passed、14 skipped、無 warnings**。14 項為未開啟明確 live opt-in 的 SQL／Oracle 測試；實機矩陣另行執行。
- 新增聚焦回歸：57 passed。完整測試已包含最後的 alias VPD、內建 LOB index 與 MVIEW gate 修正。
- Ruff check、186 檔 format check、`uv lock --check`、wheel／sdist build 均通過。原有 lock 的 54 個適用版本全數保留。
- Python 3.11 clean wheel：無 Oracle driver 的 SQL-only、Oracle Thin、Oracle Thick，各自 doctor、12-tool MCP 與實際查詢通過。較早的 TCPS direct／TNS／wrong-DN／downgrade 與實際 MCP 證據保留在 [公開驗證紀錄](../validation-0.8.0.md)。
- Wheel 有 95 entries，含 `oracle_read_safety`；wheel／sdist 均無私有 SDD workspace、demo state 或舊 runtime namespace。已掃描測試憑證值，沒有納入 artifact 或提交內容。

以下 checksum 屬於改名前的驗收建置。後續 repository URL 與文件變更會使重新建置的 artifact 產生不同 checksum；本表不代表目前 `dist/` 的內容。

| Artifact | SHA-256 |
|---|---|
| `lukestack_db_semantic_mcp-0.8.0-py3-none-any.whl` | `72438cbeefb06a168d76a10855be34a5be5674642a4d34b1a2e39d11d7d2384c` |
| `lukestack_db_semantic_mcp-0.8.0.tar.gz` | `3683b50927bc27026e36a53a26803c8c4c7114129117fe870cde002f7053907b` |

## 保留的本機測試環境

| 容器 | 本機 endpoint | 狀態 |
|---|---|---|
| `db-semantic-demo-sql` | `127.0.0.1:21433` | healthy |
| `db-semantic-demo-oracle` | `127.0.0.1:21521` | healthy |
| `db-semantic-demo-remote` | `127.0.0.1:21522` | healthy |

Thin／Thick 共用 `21521`；第二個 Oracle 用於 DB link 與遠端 synonym。Profiles 位於 Git 忽略的 `.db-semantic-mcp-demo/{sql,oracle,oracle-remote}/connection.json`。私有 environment 由 wrapper 載入，不寫入 Client，不使用真實 keyring。

已依 label／ID 確認 ownership，移除本次早期的四個 `codex-db-mcp-*` 臨時容器與 `codex-db-mcp-test` 網路。新 demo 三個容器 ID 保持不變。清理後再執行下列命令，三個 provider profile 均通過 doctor、精確 Decimal、複合 FK、12-tool MCP 及真實查詢：

```bash
uv run --locked --extra oracle python scripts/demo.py check
```

啟動、範例資料、MCP wrapper、Thin／Thick 與停止／清理方式見 [本機 Docker 文件](../local-docker.md)。使用者原有資料庫、Client 設定與 credential store 未由本次工作啟停或改寫。

## 已揭露的支援邊界

實機版本為 Oracle 23.26.3、SQL Server 2022、python-oracledb 26.0.1、Instant Client 23.26.2。Oracle 19c+／SQL Server 2017+ 是相容目標，較舊版本未逐一實測。LDAP current-user link、真實 distributed in-doubt recovery 與原生 Windows Docker demo 尚未認證／支援。

Oracle safe-read 保守拒絕含 named type、virtual column、VPD、domain／function index、external／object table 等未證明 callback 路徑的整個 target；純 virtual 或合法 function index 也可能被拒絕。Stored scalar MVIEW 須證明 `ON_QUERY_COMPUTATION=N`。未宣稱 domain-index／VPD／real-time MVIEW 攻擊已實機重現。Thin 無法保真輸出的 timestamp 需明確 `TO_CHAR` 或適用的 Thick 模式。Opaque PL/SQL 僅提供授權 metadata／靜態相依；不提供完整 source。

Timeout 是每個 driver round trip 的上限，取消不能中止已執行的同步 driver；未知寫入結果須確認後再手動重試。DB link 傳輸由資料庫管理，本機 TCP demo 沒有 TCPS 宣稱。保守限制、完整原因與代價記錄於 [24 項架構決策](../decisions/2026-10-15-db-semantic-mcp.md)。
