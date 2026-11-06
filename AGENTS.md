# 專案工作規則

遵循全域 AGENTS.md 的溝通、變更範圍與自動提交規則。

# db-semantic-mcp：Commit 後必須更新全域安裝

適用範圍：distribution 為 `lukestack-db-semantic-mcp` 的專案，包含其分支與 worktree。

- 使用者已長期授權：每次 Git Commit 或 Amend 成功後，必須立即更新本機全域 `db-semantic-mcp` 安裝，不得省略，不需再次詢問。
- 文件、設定與程式變更一律適用；多次 Commit 必須每次更新，不能等到最後一次才處理。
- 固定順序：Commit 成功 → 更新全域安裝 → 驗證已安裝 Commit → 回報結果。啟用 Oracle extra，保留 Python 3.11 與既有 Oracle 支援。
- 必須安裝剛提交的完整 Commit，使用本機 Git repository 作為來源。即使尚未 Push，也要安裝該 Commit。
- 不得使用遠端 `@main` 或尚未提交的工作目錄代替剛提交的 Commit；前者可能落後，後者可能混入其他任務的修改。
- 安裝指令形式：`uv tool install --force --python 3.11 "lukestack-db-semantic-mcp[oracle] @ git+<本機 repository 的 file URI>@<完整 Commit hash>"`。
- 本機 repository URI 由目前工作目錄的 Git root 產生；路徑有空白或特殊字元時，使用 `Path.as_uri()` 正確編碼。
- 必須用全域工具環境的 Python 加 `-I` 讀取 distribution 的 `direct_url.json`，確認 `vcs_info.commit_id` 等於剛提交的完整 hash；另確認必要模組與 Oracle driver 可匯入。
- 不得只看 `--version` 判斷更新成功，因為不同 Commit 可能共用版本號。
- 安裝或驗證失敗時，必須明確回報 Commit 已建立但全域更新未完成，並繼續處理可安全修正的原因；不得宣稱整個任務已完成。
- 最終回報包含 Git 短 hash 與全域安裝驗證結果。
- 此授權不包含 Git Push；Push 仍須使用者明確要求。
- 任務沒有產生新 Commit 時，不得為觸發更新而建立空 Commit。
