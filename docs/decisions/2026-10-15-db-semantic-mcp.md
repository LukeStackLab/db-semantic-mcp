# SQL Server 與 Oracle 共用 MCP 決策紀錄

本紀錄依決策時間排序。使用者已授權自主完成設計、實作、驗證及本機 Git commit。後續決策可修訂前項；以下保留修訂原因與代價。

| # | 決策 | 原因 | 判斷錯誤的代價 |
|---|---|---|---|
| 1 | 依使用者完整授權自主完成規格與實作，不再逐項詢問。 | 使用者要求直接取得完成產品。 | 偏好判斷不同時，需要從 Git 檢視並修改。 |
| 2 | 在既有 checkout 的 `feat/db-semantic-mcp` 分支工作。 | 保留使用者可見的工作目錄與原有 `uv.lock` 修改。 | 同時有其他 Git 寫入者時，需要額外協調。 |
| 3 | 本次不重新命名遠端 repository，也不發布 PyPI 或 push。 | 本次授權為產品開發與本機提交。 | 公開發布仍需獨立執行；GitHub 連結保留實際舊名稱。 |
| 4 | 共用平面連線欄位，Oracle 專屬選項使用型別化區塊。 | 減少重複屬性與安裝精靈的相容橋接。 | JSON 結構與早期討論的巢狀設計不同。 |
| 5 | 沒有明確的拋棄式遠端環境時，不宣稱已完成遠端實機驗證。 | Mock 無法證明 DB link、權限或 driver 的實際行為。 | 未實測的環境仍有相容風險；後續已建立自有雙 Oracle 節點驗證已列出的案例。 |
| 6 | Distribution 使用 `lukestack-db-semantic-mcp`；產品與 CLI 使用 `db-semantic-mcp`。 | 2026-10-15 查驗 PyPI 時，通用名稱已由無關專案持有。 | 套件安裝名稱與產品名稱不同；文件必須明確區分。 |
| 7 | Oracle 預設 inventory 排除 Oracle 維護的 schema，允許明確 policy 納入系統 schema。 | 低權限帳號仍可能看到大量公開系統物件與 source，增加啟動及維護成本。 | 系統 metadata 探索需要明確設定 scope；SQL Server 預設維持原行為。 |
| 8 | 經 live proof 確認的 `SYS.DUAL` 可作純量查詢 intrinsic。 | 提供 Oracle 19c 純量查詢能力；仍遵守 explicit deny 與 source 操作限制。 | 必須保留真實目標驗證，不能把私人同名物件視為 intrinsic。 |
| 9 | 無法證明遠端 `CURRENT_SCHEMA` 時，原生遠端 SQL 必須寫明 owner。 | `SYS.USER_USERS@link` 只能證明遠端 principal。 | 部分原先未限定 owner 的 SQL 需要調整。 |
| 10 | 共用結果整理器拒絕超過回應上限的欄位標頭。 | 已重現標頭本身超過 bytes cap，卻回傳未截斷的結果。 | 過寬 projection 會收到受控錯誤，需要縮小查詢。 |
| 11 | Oracle timestamp 必須保留時區與小數秒，不接受 driver 靜默失真。 | 真實 Thin 輸出曾失去時區及 FF9 精度。 | 受影響型別需要字串轉換或明確拒絕；wire 型別與原生 datetime 不同。 |
| 12 | Thin 無法保真時，回傳受控錯誤並提示 caller 使用 `TO_CHAR`，不改寫 SQL。 | 多種公開 output handler 轉換仍有精度或時區損失。 | Thin 的部分 `SELECT *` 必須改成明確 projection；Thick 保真另以實機證明。 |
| 13 | Thin 允許 metadata 可證明的普通 `TIMESTAMP(p<=6)`。 | 實測 metadata.scale 與 Python datetime 可完整保留這類值。 | 未來 driver 若無法證明 precision，必須繼續拒絕；TZ、LTZ、較高精度仍需轉換。 |
| 14 | 一律隱藏 Oracle PACKAGE、PACKAGE BODY、PROCEDURE、FUNCTION 的完整 source；保留授權 metadata 與可確認的靜態相依。 | 無法證明動態 PL/SQL 參照符合封閉的遠端權限；不新增不完整 parser 或繞過開關。 | MCP 無法提供這些物件的完整 source；無法證明的 MATERIALIZED VIEW 定義亦隱藏，VIEW 則須先證明。 |
| 15 | Oracle `encrypt` 決定 TCP／TCPS；DSN／TNS 全位址必須一致，TCPS 強制 DN 驗證。 | 消除 DSN 優先序造成的加密降級與不實加密宣稱。 | 明文 Oracle profile 必須設定 `encrypt=false`；無法明確解析的進階 DSN 可能被拒絕。 |
| 16 | 內部可信 catalog 查詢明確使用 `SYS.ALL_*`、`SYS.USER_*`、`SYS.PRODUCT_COMPONENT_VERSION` 及 `SYS.DUAL`。 | 真實私人同名 view 可遮蔽未限定 schema 的 catalog 查詢。 | 依賴 SYS 公開 view 的可見性；不授予額外 SYS 業務 scope，也不改寫 caller SQL。 |
| 17 | 每次回應只保留必要來源資訊與能力旗標，完整 capabilities 放在 `get_execution_policy` 與 `doctor`。 | 原先來源區塊單獨即有 1,189 UTF-8 bytes，重複說明增加每次工具呼叫的成本。 | 需要完整說明的 caller 必須另外查詢 policy 或 doctor。 |
| 18 | 補上可重跑的 Docker demo，預設一個 SQL Server 加一個 Oracle，遠端測試另選第二個 Oracle。 | 使用者補充要求在本機新建模擬資料庫測試；Thin／Thick 可共用同一 Oracle。 | 私有 demo 資料與資源會保留供重跑，需要使用者日後執行文件中的停止／清理指令。 |
| 19 | Docker demo 只接受 CLI 解析出的本機 Unix socket，並固定後續指令的 endpoint。 | 避免遠端 daemon ownership 與本機 SQL bootstrap 錯配；由 CLI 處理版本相關的 context 優先序。 | 原生 Windows named pipe demo 需要後續支援；目前本機 harness 以 macOS／Linux Unix socket 為範圍。 |

另有一項交付修正：每個 Oracle request 固定使用同版 policy，避免 proof 與 FK 過濾混用版本。
