# 執行紀錄驗證

日期：2026-10-16。功能預設關閉。這份紀錄是 best-effort 觀察，可以遺失，不能當作完整稽核或重試依據。

## 開關與檔案

`execution_recording_enabled` 預設 `false`。固定 profile 寫在 `connection.json`。安裝精靈用「記錄工具執行的 SQL 與參數到本機 cache？」詢問，略過就是否。變更後要重啟該 MCP process。

固定 profile 忽略 cwd `.env` 與繼承的 `SEMANTIC_MCP_*`。只有舊的環境變數啟動方式接受 `SEMANTIC_MCP_EXECUTION_RECORDING_ENABLED`。

紀錄寫入該連線當時的 SQLite cache，也就是 `cfg.cache_path`。不另建資料庫，cache 檔不存在時不建立。同一次工具呼叫固定寫入進入呼叫時的 cache，source 改連到別的 scope 也不換檔。

查今天的列：

```sql
SELECT * FROM v_mcp_execution_today ORDER BY started_at DESC, id DESC;
```

VIEW 用台灣日曆日，固定 `+08:00`，包含所有狀態。`started_at` 用範圍比較。保留今天與前 6 個日曆日，下次寫入時才清理。SQL 與參數完整保存，不遮罩、不截斷。無法無損表示的參數會整筆捨棄。不保存結果列。摘要最多 200 字。

`source_type` 為 `cache` 或 `db`。`action_type` 為 `探索`、`驗證` 或 NULL。`status` 為 `running`、`success`、`failed`、`cancelled`、`unknown`。`write_outcome_unknown` 維持 unknown。

Writer 佇列 1024 筆事件、8 MiB。超過就整筆丟棄。SQLite `timeout` 與 `busy_timeout` 都是 0，不重試。cache 檔被其他連線鎖住時，這筆紀錄會丟，工具結果仍照原樣返回。stderr 最多每 60 秒一行，只含類別與丟棄數量。

## 這次量測

機器：macOS 26.6.2 arm64，CPython 3.13.15。負載是程序內的 `record_tool` 加一條帶名參數的 `record_sql`，沒有連正式資料庫。

有間隔的 100 次（每次後等待 5 ms，讓 writer 寫完）：

| 狀態 | p50 | p95 |
|---|---|---|
| 關閉 | 0.0068 ms | 0.0195 ms |
| 開啟 | 0.1125 ms | 0.1473 ms |

這 110 次工具列與 110 次 SQL 列都留在檔案裡（含 10 次暖機）。關閉與開啟都沒有連到 SQL Server 或 Oracle，所以這組數字是入列與序列化成本，不是正式查詢延遲。

另外 20 次在呼叫返回後輪詢同一檔，看到完成列的時間約 1.28–1.36 ms。輪詢自己會開第二個 SQLite 連線；這 20 次裡有 1 次因 locked 被丟棄。

同一機器上，獨立 SQLite 檔的 100 次 commit 寫入 p50 是 0.2415 ms，主鍵讀取 p50 是 0.0104 ms。

不留間隔的 300 次連發會超過 1024 佇列，並在同時讀檔時碰到 locked。該輪只留下 86 列，stderr 出現 queue 與 locked。這是上限，不是日常單次工具呼叫的結果。

## 規格對照

| 規格 | 結果 |
|---|---|
| 預設關閉，重啟後生效，固定 profile 忽略 env | `test_config.py`、`test_profiles.py`、精靈測試 |
| 不改工具回應、descriptor、交易與 unknown | `test_execution_recording.py`、`test_execution_recording_e2e.py` |
| 每呼叫一列 tool，每條 SQL 一列，call_id 相同 | adapter 與 e2e 測試 |
| 台灣當日 VIEW、七天清理、晚到不復活 | `test_recording_schema.py`、`test_recording_writer.py` |
| 取消後 worker SQL 用實際結果 | `test_cancelled_caller_leaves_the_worker_sql_result` |
| 背景與啟動暖機不繼承呼叫；inline revalidation 與按需 source init 保留 | `test_execution_recording.py` |
| cache 刷新不刪紀錄、不改 fingerprint | `test_cache_refresh_does_not_delete_execution_rows`、`test_execution_recording_flag_does_not_change_cache_identity` |
| 真實 SQL Server / Oracle 連線 | 這次沒跑。既有 integration 測試仍是 opt-in |

## SQL 入口

記錄：

- cache：`recording_connect` 用於 probe、semantic、store、structural、metadata、semantic service、relationship、oracle metadata，以及 `query_top_tools`。
- db：`open_connection` 包住的 cursor，包含 query、object、policy、catalog 與 SQL Server `describe_result_schema`。
- Oracle thick session SQL 在 `connect` 裡用 `record_sql` 記一次。

不記錄：

- `record_metric` 的 INSERT 與 `clear_metrics` 的 DELETE。
- recorder 自己的 DDL、INSERT、UPDATE、清理。
- `setup/checks.py` 的安裝檢查。沒有工具 context。
- 啟動暖機與 `background_fill_loop`。進入時會清掉繼承的 call context。
- `executescript` 不依分號拆開，代理內抑制紀錄。正式 Agent 路徑沒有使用它。

沒有新增 MCP tool，也沒有改 tool schema。
