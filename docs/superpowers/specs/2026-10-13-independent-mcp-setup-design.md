# 獨立 MCP 與互動式安裝設計

## 目標

使用者可用終端機問答建立多組 SQL Server 配置。每組配置註冊為獨立 MCP 程序，固定連線、權限、預算與快取。Agent 在對話中選用對應 MCP，不在伺服器內切換全域連線。

## 已確定的使用方式

- `sqlserver-semantic-mcp setup` 執行繁體中文安裝精靈，可連續建立多組配置。
- `sqlserver-semantic-mcp serve --config /absolute/connection.json` 啟動固定配置。
- `sqlserver-semantic-mcp doctor --config /absolute/connection.json` 檢查連線與 MCP 握手。
- 無參數啟動保留既有環境變數與 `.env` 行為。
- 新配置預設 `readonly=true`、`budget=medium`。只詢問是否允許新增與修改資料，不加入權限分級選單。
- 受限寫入只允許 SELECT、INSERT、UPDATE；UPDATE 要求 WHERE，保留影響列數上限。

## 配置與憑證

配置位於使用者設定目錄的 `connections/<name>/connection.json`。每組使用同目錄下的獨立 `policy.json` 與 `cache/<fingerprint>/semantic.db`。名稱只接受英文字母、數字、底線與連字號，長度 1–64。

JSON 保存主機、port、database、user、readonly、budget 與憑證參照，不保存明碼密碼。密碼用 getpass 輸入，優先保存於系統 keyring。無安全 keyring 時，POSIX 平台可由使用者明確選擇 0700 目錄、0600 憑證檔作為替代；Windows 不使用 chmod 作為 ACL 保證。也接受環境變數憑證參照供自動化環境使用。

指定配置啟動時，不讀取工作目錄 `.env`，也不使用環境變數覆寫連線或權限。只讀取配置明確指定的密碼環境變數。缺少配置、憑證或 policy 時拒絕啟動。設定路徑均轉為絕對路徑。以連線目標、帳號與 policy 指紋產生快取路徑，變更時不重用舊快取。

## 安裝流程

1. 偵測 Client；多個或未偵測到時，詢問安裝目標。支援 Codex、Claude Code、Claude Desktop、Cursor。
2. 詢問配置名稱、主機、port、database、user、密碼與是否允許受限寫入。
3. 測試登入、實際 database 名稱與 metadata 可見性。失敗只重問需要修正的連線資訊，保留其他答案。
4. 可新增下一組配置，並允許更新既有配置。更新前保留原配置供失敗復原。保留既有 scope、較嚴格限制與停用操作；只有明確從唯讀改為受限寫入時才啟用 INSERT/UPDATE。
5. 顯示不含密碼的摘要；確認後保存配置，完成 MCP initialize 與 tools/list 檢查。
6. 合併 Client 設定，保留其他 MCP 與設定；建立帶時間戳的備份。名稱衝突不得覆蓋其他 MCP。
7. 顯示成功、失敗與 Client 重新載入提示。取消不寫入 Client 設定。

啟動命令使用執行 setup 的 Python 環境及套件；不把密碼寫入命令參數或 Client 設定。移除該環境後需重新執行 setup。

## 實作邊界

設定檔與 Client 設定採 atomic replace、合作程序檔案鎖與 snapshot 檢查，拒絕使用者 symlink 寫入。接受 macOS 的指定系統路徑 alias。寫入與復原使用 inode receipt 核對所有權；外部編輯器不一定遵守鎖，保留備份供復原。新配置的列數上限不能被呼叫參數放寬；寫入 strict cap 不接受請求降級。Driver 落實 timeout 與要求加密。配置名稱與資料庫來源會附於新配置的 Tool 與 Resource 結果。

既有 SQL parser 與 metadata scope 的完整重構不屬於本次安裝功能。每組配置仍須搭配具備適當權限的 DB 帳號。

## 驗收

- 五組相同 database 名稱、不同主機或帳號的配置不共用快取與 policy。
- 工作目錄 `.env` 與繼承的 SEMANTIC_MCP_* 不會改變指定配置。
- 取消、連線失敗、憑證失敗、握手失敗、Client 設定損壞或名稱衝突不破壞既有設定。
- JSON 合併保留其他 MCP；Codex TOML 合併保留其他設定與註解。
- 密碼不出現在 Client 設定、配置 JSON、摘要或錯誤輸出。
- 互動僅在 setup 執行；serve 不詢問問題且 stdout 僅輸出 MCP 訊息。


## 實作與驗證紀錄

支援四種 Client adapter；新增 setup、serve、doctor。SDK 依賴限制為 `mcp>=1.0.0,<2.0.0`，避免 2.x 不相容介面。287 項 unit tests 通過。已使用暫存 Client 配置與模擬 DB 驗證五組配置、取消、重試、權限保留與並行復原；實際 MCP 子程序握手與乾淨 wheel 安裝已驗證。未操作使用者真實 Client 設定、系統憑證庫或 SQL Server。真實 TLS、DB 權限與 Windows 執行需在對應環境驗證。
