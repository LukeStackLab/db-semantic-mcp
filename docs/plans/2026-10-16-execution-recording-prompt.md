# 新 session 開發提示詞

將以下內容完整貼到本 repository 的新 session：

```text
請在 /Users/luke/Documents/Github/db-semantic-mcp 實作已確認的 MCP 即時 SQLite 執行紀錄功能。

先讀適用的 AGENTS.md，再完整讀取：
1. docs/specs/2026-10-16-execution-recording-design.md
2. docs/plans/2026-10-16-execution-recording.md

這次已授權實作，請依計畫逐項完成、驗證與 Git commit，不要停在重新提案或再逐項詢問已確認需求。預設在本 session 循序執行；不需要主動啟動多 Agent。若發現規格與既有架構有真正衝突，先說明具體證據及最小調整，不能默默改變範圍。

核心要求：
- 優先不影響原 MCP 正常操作，紀錄失敗與原結果隔離；降低額外效能成本。
- 預設關閉，JSON 與安裝/編輯精靈同步支援 execution_recording_enabled，修改後重啟生效。
- 沿用既有 SQLite，不另建檔案；新增 mcp_execution_log 與單一 VIEW v_mcp_execution_today。
- 每次工具一列、每條實際 SQL 一列，以自動 call_id 串接；含無 SQL 的工具呼叫。
- 開始時 running、結束更新，專用背景 writer 收到就立即嘗試提交；MCP 不等待寫入，鎖定/失敗/佇列滿可捨棄。
- 自動區分探索/驗證、cache/db、tool/sql；完整 SQL 與完整參數，不遮罩不截斷，不保存完整資料列。
- VIEW 只看台灣當天，不篩狀態，時間新到舊；底表保留今天與前六日，背景每日最多清理一次。
- 不新增 MCP tool、不改工具 schema/說明/回應、不要求 Agent 傳參數、不提供 task_id/is_core/事後補標記。
- 只記 Agent 工具呼叫及其直接衍生操作；排除獨立暖機/背景更新、recorder 自身與 metrics 記錄寫入。
- 取消、交易結果未知、Oracle routing 與 SQL Server/Oracle/cache 既有行為不得改變。

開始前檢查 git status，保留所有與本任務無關的修改。遵循計畫的測試與 SQL 入口盤點；用實測回報效能成本，不承諾零影響。不需要網路研究或升級套件，除非本機證據顯示必要。

任務完成後僅提交本任務變更，不 push。最終使用台灣繁體中文回報完成項目、主要檔案、測試結果、效能量測與限制、未解問題，以及 Git commit 短雜湊。
```
