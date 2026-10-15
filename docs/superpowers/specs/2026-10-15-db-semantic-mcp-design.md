# DB Semantic MCP 雙資料庫設計

## 授權與產品目標

使用者於 2026-10-15 授權 agent 自行決定設計、架構與應用性，必要時修訂先前決策，不再逐項詢問，交付可用、輕量、高效且可維護的 SQL Server + Oracle MCP。此授權取代本次設計及計畫的人工逐段審核；不包含 Git push、PyPI 發布或 GitHub repository 遠端更名。

產品名稱、CLI、MCP server name 為 `db-semantic-mcp`；distribution 為 `lukestack-db-semantic-mcp`，因中性 distribution 名稱已由另一個 PyPI 專案使用；Python namespace 為 `db_semantic_mcp`。版本為 0.8.0。不保留舊 CLI 或舊 Python namespace。提供明確的舊配置與 Client 註冊遷移指令，保留原始檔案與備份。歷史設計文件不改寫。

## 功能與相容性

- SQL Server 2017+ 既有的 12 個工具、scope、交易與輸出限制維持。
- Oracle 19c+；預設 Thin，可明確設定 Thick。需要的 driver 延遲載入，SQL Server-only 安裝不依賴 Oracle Client。
- 共用唯讀預設；明確開啟後允許單一 INSERT VALUES、UPDATE WHERE。禁止 DELETE、DDL、PL/SQL 執行、交易控制與不透明來源；遠端亦同。
- Oracle 探索欄位、PK、複合 FK、索引、註解、view、procedure、function、package、package body、synonym 及靜態 dependencies。PL/SQL 不透明定義一律不回傳，保留已授權 metadata／靜態 dependencies 與 IO unknown；remote 採明確授權，local 無範圍限制不能證明動態遠端依賴安全。VIEW 定義僅在來源／函式／欄位驗證成立後揭露，MATERIALIZED VIEW 未有同等證明則不回傳定義。
- 支援本機／public synonym、解析鏈、Oracle DB link 遠端 synonym、直接探索明確設定的 DB link。遠端也限 Oracle 19c+。不建立或修改 DB link。
- 不宣稱完整 PL/SQL 控制流程或動態 SQL 分析；無證據的讀寫、cardinality、相依資訊明示未知。無法確認完整定義的安全範圍時隱藏定義；local 無限制仍受 remote 明確授權邊界約束。

## 核心與邊界

採 Connection → Source → Object。每組 Connection 固定一個登入與本機端點。Source 是 local 或配置中明確列出的 Oracle DB link；物件身分保存來源、schema、名稱、類型。Synonym 保存使用名稱與解析鏈，授權檢查解析路徑與最終目標。所有 SQL 都由原始 Connection 執行，不自動將使用者 SQL 翻譯或加上 @link；遠端 SQL 使用原生 @link 或 synonym。

共用 services/workflows/cache，backend 負責 connect、catalog snapshot/fingerprint、definition/dependencies、result description、identifier 與 value conversion。SQL Server backend 保留現有 SQL 及授權邏輯。Oracle authorization 採 SQLGlot Oracle AST 加嚴格來源與函式檢查；解析失敗、未知語法與未知來源均拒絕。

Runtime Config 使用中性 server/database/user/password/port/windows_auth/encrypt 欄位；env prefix 維持中性的 SEMANTIC_MCP_。Profile v2 採共用平面欄位與 Oracle options，避免維護重複的屬性橋接。唯讀開關、caps、tool profile 等欄位在兩種 DB 相同。Oracle options 支援 service_name、SID、TNS DSN、driver_mode、client/config/wallet 位置及 wallet 憑證參照，互斥組合嚴格驗證。秘密不得寫進 connection.json、policy.json、Client 設定、log 或錯誤內容。

Source definition 僅表示路由，不授權。Policy 對 local 沿用現有 scope；對 remote 使用明確的 source scope map。未列出的 DB link 不可用。解析部分 link 名稱為 Oracle catalog 中完整名稱；不可把 link 名稱直接從 caller 拼入內部 SQL。DB link 是 identifier 不能 bind，只接受 catalog 驗證且安全引用的完整名稱。private/public、固定／current user 認證差異納入解析與快取隔離，無法證明的來源失敗關閉。

來源探索工具增加選用 `source`，預設 local；SQL 工具依 SQL 內每個物件解析其來源，不以 source 參數改寫 SQL。每個 source 使用獨立 cache namespace/path。不同來源、schema、policy、principal、driver options 不能混用快取。request context 使用 ContextVar，並行呼叫不得共享可變的 active source。既有 local response source 欄位相容，附 database_type/source id/capabilities 等必要資訊。

## 正確性與效率

原生 Oracle SQL 型別須保留 NUMBER precision/scale、字元長度語意、timestamp/time zone；Decimal 不得轉為 float。若 driver 公開 API 無法保留 TIMESTAMP 精度或時區，必須明確拒絕該原生輸出並提示 caller 在原始 SQL 使用 TO_CHAR；不得回傳看似完整的失真值或自行重寫 SQL。Thick 保真轉換須經實機證明。LOB 在 connection 存活時有限量讀取；禁止先完整讀取大 LOB 再截斷。SQLGlot 只分析，不轉譯原始 SQL。拒絕 SELECT FOR UPDATE、sequence 變更、remote functions、SQL 內 PL/SQL、未知／可能有副作用的自訂函式。內建純函式採明確可審核集合。Oracle 零參數函式可看似 column reference，須以 live 欄位／查詢 scope 證據驗證。經 live 解析確定的 SYS.DUAL 可視為純量查詢 intrinsic，但 explicit deny 及來源操作限制仍生效，private shadow 不適用。遠端無 owner 且無可靠 CURRENT_SCHEMA 證據時要求 owner.table@link。

Oracle metadata 取 ALL_* 可見 catalog，先按來源及 policy 過濾再寫 cache。預設 inventory 僅取 ALL_USERS.ORACLE_MAINTAINED=N 的應用 schemas；明確 policy.allowed_schemas 可納入系統 schema。capabilities 與文件揭露此探索範圍，避免掃描大量公開系統 package source。複合 FK 保留順序與 constraint identity；disabled、not validated、deferrable 或 functional index 證據不足時不推論可信唯一性。snapshot fingerprint 必須涵蓋 constraint/index/comment/definition 變更；可先採 catalog-only snapshot hashing，並以 TTL/worker bound 限制成本，不能僅依 LAST_DDL_TIME 而漏掉變更。

columns header 本身超過 max_response_bytes 時回受控的結果過寬錯誤，不回超出上限的欄位清單，也不宣稱完整。

保留 bounded synchronous driver workers，不同 driver 不強迫混用 async API。不在每個 tool 引入全庫即時掃描。Remote source on-demand warmup、single-flight、stale-while-revalidate；離線舊 cache 明示狀態。query_timeout 的實際語意需如實揭露；Oracle call_timeout 是單次 round trip，不能誤稱整個查詢總 deadline。

autocommit 關閉。超過 affected-row cap 則 rollback；commit/rollback acknowledgement 不明即 transaction_state=unknown、automatic_retry_allowed=false。Oracle remote/distributed write 同樣處理，不進行 FORCE COMMIT/ROLLBACK 或自動重試。結果 metadata 預檢僅限已授權 SELECT；Cursor.parse 對 DDL 會執行的行為不得觸發。

## Setup、doctor 與遷移

精靈先選 DB，沿用現有 client detection、credential storage、atomic write、backup、MCP handshake、ownership-aware rollback。Oracle 依 driver mode 顯示對應問題；可選擇及命名既有 DB link，建立最小明確 policy。手動 v2 JSON 同樣可用。Doctor 檢查 target、mode、catalog、已配置遠端來源及能力；不藉由真實寫入測試權限。

`migrate` 使用舊 config root 或指定來源目錄，讀取 v1 SQL Server profile/policy，將憑證複製到新 service 或新私有檔案，建立 v2 profile、重新驗證 MCP，僅更新確認為舊工具持有的 Client entry。保留 MCP 名稱、scope 與更嚴格 caps，不放寬權限；dry-run 可預覽且不寫入。失敗不得刪除舊檔或他人修改。新配置根目錄採 db-semantic-mcp。不遷移舊 cache，重新建立隔離 cache。

## 驗證與交付

保留原有 586 項 unit tests 的語意，新增 driver/config/catalog/dialect/synonym/source context/migration/LOB/distributed outcome 測試。Mock 是契約驗證，不取代 Oracle 實機認證。提供明確 opt-in 的 SQL Server 與 Oracle live tests，以及兩個獨立 Oracle source 的 DB link 測試 fixture/步驟；不自動讀取或操作使用者未知正式 DB。使用本任務建立的兩個獨立 Oracle disposable 節點與獨立 SQL Server 驗證；交付報告仍須列出實際版本及未認證的 live matrix。

檢查完整 pytest、ruff、build、wheel 安裝、CLI help、可重現 lockfile；README 三份、.env.example、policy/profile 範例、migration、Thin/Thick、DB link 與限制同步。僅提交本任務檔案，不 push。保留既有 uv.lock 差異的 snapshot；若新套件身分與 optional dependency 要求重建 lock，從新 pyproject 獨立求解，保留仍適用的既有內容並記錄整合原因。

## 官方依據

- https://python-oracledb.readthedocs.io/en/stable/user_guide/installation.html
- https://python-oracledb.readthedocs.io/en/stable/user_guide/initialization.html
- https://python-oracledb.readthedocs.io/en/stable/api_manual/cursor.html
- https://python-oracledb.readthedocs.io/en/stable/user_guide/lob_data.html
- https://docs.oracle.com/en/database/oracle/oracle-database/19/sqlrf/CREATE-DATABASE-LINK.html
- https://docs.oracle.com/en/database/oracle/oracle-database/19/refrn/ALL_SYNONYMS.html
- https://docs.oracle.com/en/database/oracle/oracle-database/19/refrn/ALL_CONSTRAINTS.html
- https://docs.oracle.com/en/database/oracle/oracle-database/19/admin/managing-distributed-transactions.html
