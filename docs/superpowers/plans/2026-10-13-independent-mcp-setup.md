# Independent MCP Setup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立終端機安裝精靈，讓多組 SQL Server 配置以獨立 MCP 程序運作。

**Architecture:** 每個程序在啟動時載入一份固定配置。Profile、credential storage、Client registration 與互動流程分開實作，沿用現有 query、policy 與 cache 服務。

**Tech Stack:** Python 3.11+、argparse、getpass、Pydantic、keyring、tomlkit、MCP Python SDK、pytest。

**Spec:** `docs/superpowers/specs/2026-10-13-independent-mcp-setup-design.md`

**Execution:** 使用者已要求開始作業。本 session 直接執行、驗證與提交；不重新詢問執行授權。

## Global Constraints

- 每組獨立程序、policy、快取；readonly=true、budget=medium。
- 保留無參數啟動；指定配置時隔離 .env 與繼承設定。
- 密碼使用 keyring；POSIX 檔案替代需要精靈內明確選擇。
- 不修改本機真實 Client 設定或連線資料；測試全部使用暫存目錄與模擬連線。
- 只提交本次變更，不 push。

## Review Focus

- 同名 database 的不同配置：獨立 cache path 與 fingerprint，測試於 profiles。
- 憑證參照路徑與 symlink：拒絕越界與不安全檔案，測試於 credentials/files。
- 原有 Client 設定與並行寫入：備份、保留註解、衝突拒絕，測試於 clients。
- CLI 取消與部分失敗：不註冊未驗證配置，復原更新，測試於 wizard。
- MCP stdio 與敏感錯誤：握手與 stdout 不含問答或密碼，測試於 checks/main。

## Task 1: 固定配置啟動與隔離

**Files:** 建立 `profiles.py`、`credentials.py`、`setup/files.py`；修改 `config.py`、`main.py`、`infrastructure/connection.py`、`services/query_service.py`、Tool/Resource 來源資訊；測試 `test_profiles.py`、`test_credentials.py`、`test_cli.py`。

**Interfaces:** `ConnectionProfile`、`load_profile(path) -> Config`、`profile_bytes(profile)`、`policy_bytes(profile, ...)` 與 atomic writes；`configure(cfg)` 設定單一程序 runtime。

- [x] 寫隔離環境變數、缺少憑證、名稱與路徑驗證、唯讀／受限寫入測試。
- [x] 實作配置與安全憑證、獨立 policy/cache、CLI serve 路由。
- [x] 落實 driver timeout/encryption、請求不得放寬 profile 上限，保留重要狀態。
- [x] 執行相關測試並檢查差異。

## Task 2: 安裝精靈與 Client 註冊

**Files:** 建立 `setup/clients.py`、`setup/checks.py`、`setup/wizard.py`；修改 `main.py` 與 `pyproject.toml`；測試 `test_setup_clients.py`、`test_setup_wizard.py`、`test_setup_checks.py`。

**Interfaces:** `ClientTarget`、`register_servers(target, entries, original, *, receipt=None)`、`check_connection(cfg)`、`check_mcp(path)`、`run_setup(...) -> int`。

- [x] 寫 JSON/TOML 保留、備份、名稱衝突與並行修改測試。
- [x] 實作自動偵測、可覆寫設定路徑與安全合併。
- [x] 寫互動取消、五組配置、連線重試、握手失敗與更新復原測試。
- [x] 實作 setup、doctor、繁體中文問答與最終驗證。
- [x] 執行 setup 測試與 subprocess CLI 驗證。

## Task 3: 文件、完整驗證與提交

**Files:** `README.md`、`README.zh-TW.md`、`README.en.md`、`.env.example`、本設計與計畫。

- [x] 文件說明首次安裝、五個 MCP 的路由方式、憑證保存與 Client 生效步驟。
- [x] 執行全部 unit tests、CLI help、打包與隔離 subprocess 測試。
- [x] 自行審查安全路徑、失敗復原與需求覆蓋；修正發現問題。
- [x] 檢查 staged 範圍並 commit；回報短雜湊及 live DB 驗證限制。
