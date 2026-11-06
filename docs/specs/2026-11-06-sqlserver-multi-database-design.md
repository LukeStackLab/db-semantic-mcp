# 單一 MCP 的 SQL Server 多資料庫設計

日期：2026-11-06

狀態：使用者已核准並完成實作；本機及 MCP 協定驗證通過，SQL Server 實機驗證待執行。

驗證與目前保守寫入限制見 [多庫驗證紀錄](../validation-multi-database.md)。

## 1. 目的與已確認範圍

一個 MCP entry 與一個 MCP process，使用同一台 SQL Server 的同一組登入憑證，提供多個明確授權 Database 的探索與 SQL 操作。支援同一段 SELECT 中，以三段名稱引用已授權 Database 並進行 JOIN。

使用者已確認：

- 限同一台 SQL Server 上的多個 Database。
- 需要已授權 Database 之間的跨庫 JOIN。
- 每次工具呼叫指定目標 Database，不使用 process 共用的切換狀態。
- setup 延續彩色鍵盤介面，以 Space 勾選 Database。
- 保留單庫配置與呼叫方式。

本次不包含不同 SQL Server、linked server、SQL Server 與 Oracle 混用、Oracle 多庫模式，以及多庫寫入或分散式交易。Oracle 既有功能仍需通過回歸驗證。

## 2. 現況與架構選擇

目前 `ConnectionProfile` v2 只有 `database`。`configure()` 綁定 process 的基本 Config；`get_context()` 建立對應的 policy、query 與 workflow。啟動 warmup、背景補齊、工具、resources、metrics 與執行紀錄均使用這個身分。

既有 `source` 是 Oracle DB link 探索路由，不能直接拿來切換 SQL Server Database。policy 的 `allowed_databases` 可描述多個名稱，但目前 setup、連線與 runtime context 未提供完整的多庫路由；只放寬這個欄位不足以完成需求。

採用單一 process 加上每次呼叫的 Database context。相較於一個 MCP gateway 啟動多個子程序，此方式可共用登入配置、控制 worker 總量，並由 SQL Server 原生執行跨庫 JOIN。保留 Config 與 Context 的既有分層，新增 Database 選擇與授權邊界。

核心流程：

```text
MCP request(database, arguments)
  → 驗證 Database 參數與配置 allowlist
  → 擷取同一份 policy snapshot
  → 建立／取得該 Database 的 context 與 cache namespace
  → 綁定 request Config、Context 與執行紀錄
  → metadata / workflow / query handler
  → 跨庫物件授權與必要 metadata 解析
  → 使用起始 Database 的連線執行原始 SQL
  → 回傳 Database 身分、既有結果與完整性旗標
```

全域基本 Config 不因工具呼叫而改寫。所有背景與同步 worker 必須取得明確 Config 或正確傳遞的 request context。

## 3. 配置格式與相容性

新增 SQL Server 專用 v3 profile。v2 讀取與原有單庫行為不變；不在原有 v2 檔案中加入舊程式會忽略的新欄位。

v3 的資料庫欄位：

| 欄位 | 意義 |
|---|---|
| `format_version` | `3` |
| `database_type` | 本版 v3 固定為 `sqlserver` |
| `database` | 啟動驗證與初始 warmup 的 Database，必須在 `databases` 內 |
| `databases` | 非空、無重複的明確配置清單；不接受萬用字元或「全部」 |

`name`、server、port、user、TLS、credential、tool profile、權限模式及輸出限制沿用既有欄位。`database` 在配置多庫時不作為省略 request 參數的預設值。

範例僅呈現新增欄位與既有必要識別：

```json
{
  "format_version": 3,
  "database_type": "sqlserver",
  "name": "business",
  "server": "sql.example.internal",
  "port": 1433,
  "user": "reader",
  "database": "ERP",
  "databases": ["ERP", "CRM"],
  "readonly": true,
  "credential": {"storage": "env", "key": "BUSINESS_DB_PASSWORD"}
}
```

此配置仍需有效的 `policy.json`。憑證只存參照，不寫入 MCP Client entry。

v2 profile 在內部視為只配置 `[database]`，維持原有省略參數的相容性。單庫 v3 也可省略 `database`。v1 仍走原本 migrate 流程；本次不自動改寫 v1 或 v2。使用者在 setup 明確擴充既有 SQL Server 配置時，才備份並升級為 v3。

未知版本、Oracle v3、空清單、重複名稱或不在清單內的啟動 Database 均拒絕。保留嚴格欄位驗證，拼錯限制欄位必須報錯。

## 4. 授權與每庫 scope

v3 的有效 Database 集合為配置 `databases` 與 policy `scope.allowed_databases` 的交集。v3 policy 必須有非空的明確 Database allowlist，空清單不表示允許整台伺服器。啟動 Database 也必須通過交集檢查。

新增 `scope.database_scopes`，以 Database 名稱對應每庫物件限制。每筆只包含 `allowed_schemas`、`allowed_tables`、`denied_tables`，未辨識欄位拒絕。v3 policy 對每個允許的 Database 都要有明確 scope entry；空物件代表該庫未額外限制物件，仍受全域限制與既有 SQL 規則約束。

```json
{
  "allowed_databases": ["ERP", "CRM"],
  "database_scopes": {
    "ERP": {"allowed_schemas": ["dbo"]},
    "CRM": {"allowed_schemas": ["sales"]}
  }
}
```

上例是 `scope` 片段，不是完整 policy 檔案。

授權採交集：物件必須同時通過配置 allowlist、policy Database allowlist、全域物件 scope 與該庫物件 scope。任一 deny 生效；每庫限制不能擴大全域權限。共同 operations 與 constraints 沿用現有 policy，不增加每庫不同的寫入模式。

Database 名稱由 SQL Server metadata 確認 canonical 名稱。不得直接用 `casefold()` 合併不同名稱；需遵守伺服器實際識別語意，無法確認時拒絕模糊匹配。SQL identifier 必須使用一致且經測試的引用方式。登入憑證、使用者輸入或物件名稱不得以未引用字串拼接到 SQL。

policy reload 只影響後續 request。單次跨庫 request 對所有來源使用同一份完整 policy snapshot。當 policy 收窄 Database 清單時，後續呼叫即刻拒絕被移除的庫，不能讀取先前的 cache。新增 Database 仍需更新配置，不能只藉 policy 擴充配置 allowlist。

## 5. MCP 工具契約

### Database 選擇

既有 Database 相關工具全部支援 `database: string`，包含 metadata、object、relationship、discovery、query、policy、cache 與 metrics 工具。

- v2／單庫 v3：參數可省略，沿用唯一 Database；明確傳入其他庫則拒絕。
- 配置兩個以上 Database 的 v3：`database` 為必要參數，即使當下 policy 僅授權其中一庫也相同。
- schema 宣告與 runtime 都驗證；不得只依賴 Client 對 JSON Schema 的檢查。
- 非字串、空字串、未配置或未授權的 Database，在 cache 讀取、warmup 與業務 SQL 前拒絕。
- 不從 SQL 文字自動推測起始 Database，不提供全域 `use_database` 工具。
- `source` 保留 Oracle 的既有語意；SQL Server 仍只接受 `local`。

新增 `list_databases`，在 v3 的所有 named tool profiles 中提供，不要求 `database`。v2 工具集合維持相容。它只回傳已配置且 policy 允許的名稱，不回傳整台 SQL Server 的資料庫清單，不輸出帳號、憑證或 policy 隱藏的名稱。此工具不依賴 SQL Server 線上狀態；名稱清單不保證當下可連線。

結果契約：

```json
{
  "databases": ["ERP", "CRM"],
  "selection_required": true
}
```

`selection_required` 依配置中的 Database 數量決定。工具說明要清楚指向後續呼叫的 `database` 參數。

### 呼叫範例

沿用現有 SQL 參數名稱 `query`，不新增 `sql` alias。

```text
get_tables(database="ERP")

describe_table(database="CRM", schema="sales", table="Customers")

plan_or_execute_query(
  database="ERP",
  query="SELECT o.OrderId, c.CustomerName FROM ERP.dbo.Orders AS o JOIN CRM.sales.Customers AS c ON o.CustomerId = c.CustomerId"
)
```

validate、dry_run、auto 與 execute_if_safe 共用相同的 Database 選擇與跨庫授權規則；沿用各模式的既有資料庫存取與執行語意。

既有回應的 `source.database` 表示本次起始 Database。跨庫 query 回應另以頂層 `referenced_databases` 列出已解析且通過授權的實際資料來源名稱，去重並採固定排序；欄位在 SQL Server v3 query 成功授權時提供，純運算 SELECT 為空陣列。授權失敗的回應不提供部分來源清單，以免誤認為完整證據。未執行的驗證結果同樣不得宣稱已執行。

工具 input/output schema、structuredContent、文字 JSON 與文件必須一致。錯誤使用既有 `CallToolResult.isError` 與安全訊息，分辨缺少 Database、非法目標、scope 拒絕與 Database 不可用。

## 6. 跨庫 SQL 與物件證據

跨庫 SELECT 使用 SQL Server 原生三段名稱 `Database.Schema.Object`。不改寫 caller SQL，不插入 `USE`，不拆分查詢後在 Python 做 JOIN。

每個來源都要：

1. 以 AST 解析來源；保留 CTE、alias、subquery 與 UNION 的既有解析保證。
2. 未帶 Database 的物件使用本次起始 Database。跨庫來源必須有明確 schema。
3. 驗證該來源的 Database 與全域／每庫物件 scope。
4. 使用同一登入身分與執行連線，從正確 Database 解析必要 SYS catalog 證據，避免將 ERP 的 metadata 套到 CRM 同名物件。
5. 若經由 view、synonym 或靜態依賴引用其他物件，檢查完整已解析來源鏈。未知、循環、不可證明、超出既有物件能力或指向未授權庫的路徑均拒絕，不因表面名稱已授權而放行。
6. 確認所有來源後，才允許執行；缺少權限或 metadata 不足時，不用舊 cache 取代授權證據。

實作審查補充：v3 要求每庫 VIEW DEFINITION 與 ALTER ANY SECURITY POLICY，以避免物件級 DENY 隱藏 RLS catalog。這是完整性證據要求；產品不自動 grant，缺少則安全拒絕。後者屬較高權限，需由管理者評估。

四段名稱、linked server、OPENQUERY／OPENROWSET、動態 SQL 與既有不允許的 SQL 形式維持拒絕。SELECT INTO 與其他可能寫入的 SELECT 形式不得因跨庫支援而放行。查詢執行錯誤或 collation 不相容時回報安全錯誤，不自動加 COLLATE 或改寫 SQL。

本版只擴充跨庫讀取。既有受限 INSERT VALUES／UPDATE WHERE 仍限本次起始 Database 的單一目標；跨庫寫入、跨庫寫入來源及跨庫交易控制不在本版範圍。原有 row cap、timeout、strict rollback、unknown transaction 與不得自動重試的保證不變。

本版不宣稱從 metadata 自動證明跨 Database 的業務關聯。relationship／join-path 探索維持所選庫範圍；使用者可用明確跨庫 SQL JOIN，但不據欄位同名憑空產生 FK 或 cardinality 證據。

## 7. Runtime、cache、metrics 與執行紀錄

新增 Database context 管理層，置於工具 dispatch 與既有 source context 之前。生命週期如下：

- process 保存不可被 request 改寫的基本配置與已配置 Database 清單。
- request 先選 Database、擷取 policy snapshot，再綁定 Config／Context。
- 執行紀錄在 Database context 綁定後建立，不能記入啟動 Database 的 cache。
- context 及初始化任務依 Database、principal、endpoint 與有效 policy 身分隔離；同一身分的並行初始化採 single-flight。
- 各庫獨立暖機、TTL、重新驗證、semantic queue、metrics 與執行紀錄；政策變更沿用現有 cache namespace 隔離原則。
- process 的 DB worker 上限為總量，不可因配置 N 個庫而變成 N 倍。
- context cache 大小受已配置 Database 數量限制；reload 不無限保留舊 context。進行中的 request 持有原有 snapshot 至結束。
- 背景初始化與重驗證不繼承 caller 的執行紀錄；取消等待不誤認底層 worker 已停止。

跨庫 SQL 是一次 request、一次查詢執行與一個起始 Database 的執行紀錄。不得在每個來源庫重複記為多次執行；必要來源證據使用上述 `referenced_databases` 回應欄位。保留既有 SQL 紀錄開關與本機敏感資料處理方式。

啟動維持先驗證配置、policy 與啟動 Database，並依 startup_mode 執行該庫初始 warmup。其他庫首次使用才初始化。啟動 Database 無法存取時回報啟動失敗；正常啟動後，另一庫離線只使該庫 request 或依賴該庫的 JOIN 失敗，不終止其他庫的 MCP 服務。shutdown 必須處理所有初始化、背景任務與 worker。

## 8. Resources 與 prompts

Database 專屬 resource templates 增加 `database` query parameter，並沿用 Oracle 的 `source`。例如：

```text
semantic://summary/database?database=ERP
semantic://schema/tables/dbo.Orders?database=ERP
semantic://summary/table/sales.Customers?database=CRM
```

多庫模式缺少 Database 的 resource read 回傳可操作的選擇錯誤；不默默使用啟動庫。`list_resources` 在多庫模式改列可明確識別 Database 的 summary URI，其他模板以 `{?database,source}` 宣告。單庫 URI 與 Oracle URI 相容。

resource read 使用與工具相同的 Database context、scope 與 cache 邊界。prompts 中 Database 相關引導同步加入選擇步驟；不得引導 agent 呼叫無 Database 的多庫工具。輸出中的名稱需正確 URI encoding，不能藉 query parameter 夾帶另一個目標。

## 9. Setup、doctor 與升級體驗

SQL Server 設定流程調整為：

1. 沿用多 Client 勾選與 MCP 名稱輸入。
2. 輸入主機、port、帳號與隱藏密碼。
3. 以這組登入取得可見且可連線的 Database 候選，按名稱排序；自動清單排除系統庫。
4. 顯示 Space 多選清單，至少選一個；既有配置預先勾選原值。另提供手動輸入名稱的明確入口，供無法列舉或需明確加入系統庫的使用者使用。手動名稱仍需逐庫連線及 metadata 驗證，不因手動輸入略過限制。
5. 以單選決定啟動驗證庫；只有一庫時自動使用唯一選項。
6. 選取共同的唯讀／受限寫入與 SQL 紀錄選項。
7. 摘要列出一個 MCP、所有 Database、起始庫、權限與 Client，說明跨庫 SELECT 的授權範圍。
8. 確認後保存一份 v3 profile、一個憑證參照與一份 policy；在選取的每個 Client 各註冊一個 MCP entry。

候選可見不等於使用者已授權。只有明確勾選、驗證成功並確認套用的 Database 才能進入配置與 policy。任一選取庫驗證失敗，讓使用者重試或移除該庫後重新確認，不能默默減少清單後套用。

沿用同名更新、備份、檔案權限、符號連結拒絕、並行修改檢查、Client 交易式復原與 Ctrl+C 清理。升級保留既有較嚴格 operations、constraints、schema/table scope 與 deny 規則；只經使用者明確確認增加 Database 授權。若原有 allowlist 使新增庫沒有可用物件，顯示限制並保留原值，不自動清空。

`doctor --config PATH` 對 v3 逐庫驗證並逐項回報，不只檢查啟動庫；可用 `--database NAME` 限定已授權庫。維持只讀驗證，列出有效 Database、metadata 存取及 MCP 工具契約結果，不執行業務寫入。任一要求驗證的庫失敗時回傳非零 exit code，並保留其他庫的檢查結果。

v2 的 setup／doctor 行為繼續支援。v3 的 MCP 握手驗證涵蓋 `list_databases`、多庫必要參數與實際工具集合，不再假設固定為 12 個工具。

## 10. 實作影響範圍

| 區域 | 預期責任 |
|---|---|
| `profiles.py`、`config.py` | v3、讀取相容、Database allowlist 與 request Config |
| `policy/models.py`、`scope.py`、`enforcer.py`、`services/policy_service.py` | 每庫 scope、交集、跨庫來源證據與 snapshot |
| `server/app.py`、新增 Database context 模組、`source_context.py` | request 路由、參數驗證與生命週期 |
| `server/tools`、resources、prompts | Database 參數、list_databases、輸出 schema 與引導 |
| SQL Server backend、metadata／object／query／workflow services | 正確庫的連線、metadata、scope 與原生 SQL 執行 |
| `main.py`、cache、background、workers、recording | 多庫啟停、lazy warmup、總容量與紀錄隔離 |
| `setup`、CLI doctor、README 與 config 範例 | 多庫選取、升級、診斷與文件 |
| 單元／MCP 協定／SQL Server integration tests | 下列驗收條件與既有回歸 |

HTTP API、Postman workspace 或外部服務不在本功能資料路徑；契約以 repository 中的 MCP schemas 與實際協定測試維護，不為本功能新增 HTTP facade。

## 11. 驗收與測試證據

### 配置與授權

- v2 原樣讀取與執行；v3 空／重複／未知欄位／非法啟動庫／Oracle v3 拒絕。
- profile 與 policy 的交集生效；空 allowlist 不擴權；缺少每庫 scope 拒絕。
- 每庫同名 schema/table 不混用；global deny 與 database deny 皆生效。
- 政策收窄後不能透過舊 cache 讀取；一次 request 的跨庫判斷使用同一 snapshot。

### 工具與並行

- 真正 MCP stdio handshake、list_tools、list_databases 與 call_tool 契約驗證。
- 多庫工具缺少 database、非法型別、未知或未授權目標在業務 SQL 與 cache 前拒絕。
- ERP、CRM 並行呼叫，刻意以同步屏障交錯，檢查實際 connect kwargs、metadata、結果、cache、metrics 與執行紀錄全部歸屬正確 Database。
- 檢查 context cache 有界、初始化 single-flight、worker 總量、取消與 shutdown 清理。
- resources 選庫、URI encoding、多庫必要參數與舊單庫 URI 相容。

### SQL Server 原生跨庫測試

使用經明確啟用的 disposable SQL Server fixture 建立 ERP、CRM、Forbidden 三庫與同名物件；只有測試初始化程序可建立資料與 grant。產品驗證流程不以寫入測試權限。

- ERP 與 CRM 的授權 JOIN 回傳已知結果；Query API 使用 `query`，實際連線起始庫與 request 一致。
- Forbidden 來源、schema/table deny、view／synonym 間接跨庫與未解析依賴均有正反案例；不應放行的 SQL 必須在執行前拒絕。
- CTE、alias、subquery、UNION、帶空白／方括號／點號的識別字正確解析；區分大小寫的案例依 fixture collation 驗證。
- 四段名稱、linked server、SELECT INTO、USE、動態 SQL、跨庫寫入與多庫交易維持拒絕。
- Database 無權限、離線、消失或查詢 collation 不相容，安全回報且不自動改寫或重試。
- 既有輸出限制、寫入 row cap 與 unknown transaction 狀態不退化。

### Setup 與回歸

- 實際鍵盤事件驗證候選清單、多選、手動名稱、更新、取消及摘要。
- 一份 profile／憑證可註冊多個 Client，各 Client 只有一個對應 MCP entry。
- 任一 profile／policy／Client 寫入失敗或 Ctrl+C，驗證復原與保留並行修改。
- 跑既有單元測試、Ruff、格式、lock、build 與可執行的 MCP 協定測試。
- Oracle、v2 單庫、環境變數啟動與既有 tool profiles 不退化。

回報分開列出單元、模擬 driver、MCP 協定及真實 SQL Server 結果。缺少可用的 disposable SQL Server 時，不宣稱已完成真實跨庫認證。發現既有不穩定測試需明示首次結果與重跑結果，不把重跑當成從未失敗。

## 12. 審查後交付

本規格通過使用者審查後，才撰寫按依賴順序執行的實作計畫。實作完成需包含程式、測試、範例與雙語使用文件；依任務授權提交 Git。Push 與本機全域安裝更新需依當次明確指示執行。規格提交本身不代表多庫功能已可使用。
