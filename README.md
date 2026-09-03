# Windows Firebase 即時桌面彈幕

## 目的

Windows 桌面 Client 直接使用 Firebase Realtime Database：每個 Client
同時寫入並以 SSE 訂閱同一個資料集合，不經 Discord Bot Proxy。

## Firebase 資料契約

- Room：`main_v3`
- Path：`rooms/main_v3/danmaku_submissions/{timestamp_ms}`
- Database URL：預設使用目前 Firebase；可由 `DANMAKU_FIREBASE_DATABASE_URL` 覆寫
- 認證：Firebase Anonymous Auth
- 權限：已登入客端可讀寫（完整規則見 [firebase_rule/database.rules.json](firebase_rule/database.rules.json)）
- `created_at_ms`：Firebase Server Timestamp，儲存後是 Unix epoch milliseconds 整數
- `request_id`：Client UUID，作為資料內容的去重識別
- 第一層 timestamp key：Client 產生的 Unix epoch milliseconds；同一毫秒後寫入者會覆蓋前一筆
- 不使用 `status`、`feed_id`、`dc_messages` 或 `latest_dc_feed_id`

Rules 檔是完整 RTDB 規則範例；部署前須與 Firebase 現有其他路徑規則合併，
直接部署可能覆蓋既有規則。

Payload：

```json
{
  "schema_version": 1,
  "request_id": "client-uuid",
  "text": "訊息",
  "created_at_ms": {".sv": "timestamp"},
  "auth_uid": "anonymous-uid"
}
```

Windows PowerShell 手動指定 Firebase Database URL：

```powershell
$env:DANMAKU_FIREBASE_DATABASE_URL = "https://your-project-default-rtdb.firebaseio.com"
Start-Process .\\start_danmaku.bat
```

未設定或設定為空白時，會使用：

`https://dc-danmaku-default-rtdb.asia-southeast1.firebasedatabase.app`

初次 SSE snapshot 不重播既有訊息；連線中斷重連後，已顯示的 `request_id` 不重複顯示，
新資料依 `created_at_ms`、`request_id` 排序。

## Windows 啟動

1. 將資料夾放在固定位置，例如 `C:\\danmaku\\`。
2. 安裝 Python 3.14.6 或更新的 Python 3.14 維護版本。
3. 雙擊 `start_danmaku.bat`。
4. 第一次啟動若缺少 PySide6，啟動器會自動安裝。

## 檔案

- `desktop_danmaku.py`：系統匣、透明多螢幕 Overlay、熱鍵與主程式。
- `firebase_auth.py`：Anonymous Auth、token 更新與 RTDB request。
- `firebase_feed.py`：直接訂閱 `danmaku_submissions` 的 SSE feed。
- `firebase_submit.py`：直接寫入 `danmaku_submissions`。
- `firebase_cleanup.py`：依第一層 timestamp 清理過期投稿。
- `cleanup_expired_submissions.bat`：一鍵清理批次檔。
- `overlay_position.py`：彈幕初始位置計算。
- `test_*.py`：單元測試。

## 清理過期資料

預設保留最近 24 小時，執行：

```bat
cleanup_expired_submissions.bat
```

手動指定保留時間（小時）：

```bat
cleanup_expired_submissions.bat 72
```

只預覽不刪除：

```bat
cleanup_expired_submissions.bat 72 --dry-run
```

清理範圍固定為 `rooms/main_v3/danmaku_submissions`，只會刪除第一層 key
為 epoch milliseconds 且早於保留期限的資料。Firebase URL 同樣遵循
`DANMAKU_FIREBASE_DATABASE_URL` 覆寫規則。
