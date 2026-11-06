# SQL Server 多資料庫驗證紀錄

日期：2026-11-06

## 驗證範圍

- v3 配置與全域／每庫 scope 交集；v2、Oracle 與原有啟動方式相容。
- request Database、Config、policy snapshot、cache、metrics 與執行紀錄隔離。
- 真實 MCP stdio subprocess：initialize、list_tools、list_databases、必要參數拒絕、並行路由與 resources URI。
- 模擬 DB-API session：原生跨庫 JOIN、view／synonym 依賴、未授權庫、metadata 缺漏、原始 SQL 不改寫，以及驗證／預覽／執行模式。
- 實際 Questionary key bindings：Client／Database 多選、預設勾選、取消、逐庫驗證與失敗復原。

MCP 協定與 mock-driver 測試不表示 SQL Server 原生執行已通過認證。

## 審查修正

- 配置與 policy 交集為空時立即拒絕，避免空 allowlist 的 v2 unrestricted 語意。
- 保留 v2 大小寫相容；v3 canonical Database 以 SQL Server DB_ID 證明。
- Lazy context 建立 semantic queue 後才啟動背景分析。
- Resource 初始化錯誤遮蔽密碼，未知 driver 訊息使用固定診斷。
- 跨庫寫入防護同時涵蓋直接與間接目標；未證明的隱含寫入依賴保守拒絕。目前 v3 寫入限直接實體表，trigger/default/check/FK/bound rule/temporal 目標及 view/synonym 寫入均拒絕；包含尚未證明安全的常數 default 等情況。
- 來源證據需足夠的 metadata 可見性，不以不可見的 security policy catalog 空結果宣稱沒有 RLS。Database VIEW DEFINITION 仍可能被 policy 的物件級 DENY 覆蓋，因此 v3 額外要求 ALTER ANY SECURITY POLICY 作為完整可見性證據，缺少即拒絕。此為較高權限，程式不會自動 grant；管理者必須評估登入適用性。

## 既有不穩定測試

整合測試曾出現 `test_sequence_numbers_are_unique_across_copied_contexts` 失敗，錯誤為 `cannot enter context ... is already entered`。原測試讓多個 thread 同時進入同一個 Context，與測試名稱所描述的 copied contexts 不符。改為每個 thread 使用 `parent.copy().run(...)`，保留同一個 span 與共享 sequence lock，驗證實際的並行序號唯一性；未修改產品 recording 行為。

## 真實 SQL Server 測試

本環境未啟動 Docker daemon，且未設定 disposable SQL Server 測試授權，因此真實跨庫測試未執行。

新增 `tests/integration/test_live_multi_database.py`。執行前必須同時啟用既有 SQL disposable 測試與額外多庫建立授權：

```bash
SEMANTIC_MCP_DISPOSABLE_SQL_TESTS=1 \
SEMANTIC_MCP_DISPOSABLE_MULTI_DATABASE_TESTS=1 \
SEMANTIC_MCP_TEST_SQL_PROFILE=/absolute/disposable/connection.json \
uv run pytest tests/integration/test_live_multi_database.py -q
```

另設 `SEMANTIC_MCP_DISPOSABLE_LOWPRIV_TESTS=1` 可執行低權限 RLS metadata 測試。該測試在自己建立的資料庫內建立 WITHOUT LOGIN user，暫時 impersonate 後驗證拒絕規則，最後 REVERT 並清理。

測試只建立、使用、刪除自己的隨機名稱資料庫。測試登入需要建立測試物件與執行清理的權限；產品正常使用不需要資料庫建立權限。不應使用業務資料庫或正式環境作為這個測試目標。

## 安裝套件驗證

以建置完成的 wheel 安裝到獨立的 Python 3.11.16 測試環境，保留 Oracle extra。已確認新 runtime、SQL source proof、Oracle driver 均可匯入，v3 profile 與 policy 可建立。此測試未改動使用者的全域安裝。

## 可重跑檢查

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv lock --check
uv build
```

本次結果：`uv run pytest -q` 為 **1126 passed, 16 skipped**。16 個 skipped 為未啟用的 disposable integration tests，包含新增跨庫與低權限 RLS 案例。Ruff、format、lock、Git diff whitespace 檢查及 sdist／wheel build 通過。真實 MCP stdio 測試已通過，真實 SQL Server 認證尚未執行。
