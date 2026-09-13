# Mock GPS Follow Live Nav

GPS Route Simulation and Live Navigation System

Mock GPS Follow Live Nav is a local testing system for simulating GPS routes and monitoring live navigation progress. It combines a Python and Flask web application, a mission worker, SQLite, Google Maps Directions API, Tailscale, and Android integration through MacroDroid and GPS JoyStick.

本專案供自有應用程式與私有測試環境使用，請勿用於偽造出勤、規避第三方服務控制或違反適用法律與服務條款的用途。本文件以 Windows + PowerShell 為基準撰寫，指令路徑皆為 `.venv\Scripts\`。

## Portfolio overview

This project was developed to explore asynchronous mission execution, route planning, real-time monitoring, Android integration, and persistent mission history.

## Key features

- Plans walking, transit, and motorcycle routes with Google Maps Directions API.
- Runs the Flask web application and mission worker through one local entry point.
- Sends GPS coordinates from the PC to an Android test device through Tailscale and MacroDroid.
- Stores mission state, logs, movement records, and historical sessions in SQLite.
- Provides automated code checks through GitHub Actions.

## Technology stack

Python 3.10 or later, Flask, SQLite, Google Maps Directions API, Tailscale, MacroDroid, Android, Pytest, Ruff, and GitHub Actions.

## Project status

This project is intended for controlled local testing environments. The full setup requires a Google Maps API key, a Windows PC, an Android test device, and Tailscale connectivity.

## Documentation

The sections below provide detailed setup and technical documentation, including system flow, installation, Android integration, API usage, logging, security, testing, and troubleshooting.

## 功能

- 任務控制：使用 `/start_task` 開始任務，使用 `/stop_task` 停止伺服器端任務
- GPS 推送：將 `lat` / `lng` 傳送到手機端 MacroDroid `/gps`；手機 HTTP 傳送與 movement logging 完全解耦
- 路線規劃：使用 Google Maps Directions API 取得步行、大眾運輸與機車路線
- 導航歷史：保留每次 Google Directions 規劃的交通型態、車種、路線、站點、距離與時間資訊
- 交通模式：支援 `walking`、`transit`、`motorcycle`
- 大眾運輸：`transit_type` 可指定 `AUTO`、`MRT` 或 `BUS`
- 位置維持：任務完成後保持最後位置，直到停止任務
- 任務歷史：每次任務建立獨立 log session 與 `movement.csv`
- 歷史分頁：選定 session 後開啟獨立頁面，地圖、狀態、路線、Navigation、CSV 與所有 log 都固定使用該 session
- Web 監控：地圖、任務狀態、最後座標、路線、Log 與任務歷史
- Web/Worker 分離：Web 只接受與查詢任務，單一 worker 執行導航
- SQLite：持久化任務、ETA、路線 revision、錯誤及手機健康狀態
- ETA 配速：任務開始前規劃全程，MRT 停站後動態補速但不超過 Google nominal speed 的 1.35 倍

## 系統流程

```mermaid
flowchart LR
    A["MacroDroid or API Client"] --> B["Flask Web API"]
    B --> C["SQLite planning mission (HTTP 202)"]
    C --> D["Single mission worker"]
    D --> E["Google full-mission planning"]
    E --> F["Tailscale HTTP"]
    F --> G["Android MacroDroid /gps"]
    G --> H["GPS JoyStick TELEPORT intent"]
    E --> I["Mission Logs and movement.csv"]
    I --> J["Web Dashboard"]
```

## 前置需求

| 項目 | 說明 |
|---|---|
| Windows PC | 執行 Dashboard 與 mission worker |
| Python 3.10 以上 | `.vscode/tasks.json` 以 `py -3.12` 建立 venv；CI 以 3.10 驗證 |
| Google Cloud 帳號 | 需啟用帳單，並開啟 Directions API |
| Android 手機 | 執行 MacroDroid 與 GPS JoyStick |
| [MacroDroid](https://play.google.com/store/apps/details?id=com.arlosoft.macrodroid) | 需可使用 HTTP Server 與 Send Intent（部分功能需 Pro） |
| [GPS JoyStick](https://play.google.com/store/apps/details?id=com.theappninjas.fakegpsjoystick) | 套件名 `com.theappninjas.fakegpsjoystick`，實際更新手機定位的軟體 |
| [Tailscale](https://tailscale.com/) | PC 與手機需登入同一個 tailnet |

完整流程共八個步驟，請依序完成：取得 API Key → 安裝 → 設定 `.env` → Tailscale → GPS JoyStick → MacroDroid → 啟動 → 首次驗證。

## 步驟一：取得 Google Maps API Key

1. 進入 [Google Cloud Console](https://console.cloud.google.com/) 建立專案。
2. 於專案啟用帳單（Directions API 需要帳單帳戶）。
3. 進入「APIs & Services」→「Library」，啟用 **Directions API**。本專案只呼叫這一個 API，不需要 Geocoding、Places 或 Maps JavaScript API。
4. 進入「Credentials」→「Create credentials」→「API key」，複製產生的金鑰。
5. 點金鑰進入編輯頁，於「API restrictions」選「Restrict key」並只勾選 Directions API。金鑰由 PC 後端呼叫，若 PC 為固定對外 IP，可再加上 IP 限制。

金鑰填入 `.env` 的 `GOOGLE_MAPS_API_KEY`（步驟三）。

## 步驟二：安裝

在專案資料夾開啟 PowerShell，執行一次：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-lock.txt
```

`requirements-lock.txt` 是完整鎖定版本的環境快照，日常安裝一律使用它。`requirements.txt` 只列直接相依，供更新鎖定檔時使用。

要跑測試與 lint 時再裝開發相依：

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
```

用 VS Code 開啟專案資料夾時，`.vscode/tasks.json` 會在 `F5` 啟動前自動完成建立 venv 與安裝執行相依這兩步，不必手動執行上面的指令。

## 步驟三：設定 `.env`

複製範例檔：

```powershell
Copy-Item .env.example .env
```

`.env` 內容：

```ini
GOOGLE_MAPS_API_KEY="YOUR_GOOGLE_MAPS_API_KEY"
PHONE_TAILSCALE_IP="100.x.x.x"
API_SECRET_KEY="replace_with_a_long_random_secret"
API_ACCESS_KEY="replace_with_a_different_random_secret"
FLASK_SESSION_SECRET="replace_with_another_random_secret"
BIND_HOST="127.0.0.1"
FLASK_PORT=5050
TZ="Asia/Taipei"
```

| 設定 | 必填 | 用途 |
|---|---:|---|
| `GOOGLE_MAPS_API_KEY` | 是 | Directions API 金鑰，缺少時任務規劃會失敗 |
| `PHONE_TAILSCALE_IP` | 是 | 手機的 Tailscale IP，PC 送座標的目標（步驟四） |
| `API_SECRET_KEY` | 是 | Dashboard `/login` 使用的密碼，長度不限 |
| `API_ACCESS_KEY` | 否 | 任務控制 API 的 header 金鑰；省略時沿用 `API_SECRET_KEY` |
| `FLASK_SESSION_SECRET` | 否 | Flask session cookie 簽章金鑰；省略時沿用 `API_SECRET_KEY` |
| `BIND_HOST` | 否 | 額外開放的網路介面，預設 `127.0.0.1`（步驟四） |
| `FLASK_PORT` | 否 | Dashboard 連接埠，預設 `5050` |
| `TZ` | 否 | 執行期時區（IANA 名稱），預設 `Asia/Taipei` |

三把金鑰的分工：`API_SECRET_KEY` 給人在瀏覽器登入，`API_ACCESS_KEY` 給 MacroDroid 與程式呼叫 API，`FLASK_SESSION_SECRET` 只用於簽 cookie。手機端 MacroDroid 要填的是實際生效的 `API_ACCESS_KEY`。

## 步驟四：Tailscale

PC 與 Android 手機需登入同一個 tailnet。PC 透過手機的 Tailscale IP 呼叫 MacroDroid HTTP Server，手機也可透過 PC 的 Tailscale IP 呼叫任務控制 API。

1. PC 與手機分別安裝 Tailscale 並以同一組帳號登入。
2. 查 PC 的 Tailscale IP：

   ```powershell
   tailscale ip -4
   ```

3. 查手機的 Tailscale IP：開啟手機的 Tailscale App，首頁即顯示本機的 `100.x.x.x`。
4. 把手機 IP 填入 `.env`：

   ```ini
   PHONE_TAILSCALE_IP="100.x.x.x"
   ```

5. 若要讓手機連進 PC 的 Dashboard，把 PC 自己的 Tailscale IP 填入 `BIND_HOST`：

   ```ini
   BIND_HOST="100.x.x.x"
   ```

`BIND_HOST` 是唯一的網路設定，只是「額外開放的介面」。填入 Tailscale IP 後服務會**同時**監聽 `127.0.0.1` 與該位址，兩個網址都可用：

```text
http://127.0.0.1:5050/map      # 本機
http://100.x.x.x:5050/map      # 手機經 Tailscale
```

不要填 `0.0.0.0`。那會把未加密的 Dashboard 暴露在整個 LAN 上；服務一律以 HTTP 提供，session cookie 沒有 `Secure` 旗標，會以明文傳輸。

Tailscale 未連線時，綁定該 IP 會失敗並印出提示；先確認 Tailscale 已連上再啟動。服務只提供 HTTP，沒有內建 HTTPS，請只在 Tailscale 或可信任內網使用；Tailscale 本身已對節點之間的流量加密。

## 步驟五：手機端 GPS JoyStick

GPS JoyStick 是實際更新手機定位的軟體，MacroDroid 只是把 PC 送來的座標轉交給它。

1. 安裝 GPS JoyStick（套件名 `com.theappninjas.fakegpsjoystick`）。
2. 開啟手機的開發人員選項：「設定」→「關於手機」→ 連點「版本號碼」七次。
3. 進入「設定」→「系統」→「開發人員選項」→「選取模擬位置應用程式」，選擇 **GPS JoyStick**。
4. 開啟 GPS JoyStick，授予定位權限，並在地圖上手動移動一次搖桿，確認手機定位確實被改變。這一步先單獨驗證，可避免之後把手機端問題誤判成 PC 端問題。
5. 關閉 GPS JoyStick 的電池最佳化，避免任務進行中被系統凍結。

任務進行期間 GPS JoyStick 需保持在背景執行。

## 步驟六：手機端 MacroDroid

專案根目錄提供 `macrodroid-example.category`，可匯入 MacroDroid 作為範例分類。

1. 安裝 MacroDroid，依提示授予所需權限。
2. 開啟 MacroDroid 的本機 HTTP Server，連接埠設為 **8080**。PC 端寫死 `http://<PHONE_TAILSCALE_IP>:8080/gps`，此埠號不可更改。HTTP Server 是 App 層設定，匯入分類不會一併帶入。
3. 把 `macrodroid-example.category` 傳到手機，於 MacroDroid 匯入該分類，會得到三個 macro：`Mission Controller(example)`、`Move GPS(example)`、`Stop GPS(example)`。
4. **匯入後三個 macro 預設為停用狀態，必須手動啟用**，否則不會有任何反應。
5. 修改匯入後的內容：
   - 區域變數 `g_server_url`：填 `http://<PC 的 Tailscale IP>:5050`。Mission Controller 與 Stop GPS 各有一份，兩邊都要改。
   - HTTP Request header `API-ACCESS-KEY`：範例值為 `replace_with_API_ACCESS_KEY`，改成 `.env` 中實際生效的 `API_ACCESS_KEY`。

### Mission Controller

用途：在手機上輸入任務資料，送到 PC 的 `/start_task`。

- Method：`POST`
- URL：`{lv=g_server_url}/start_task`
- Header：`API-ACCESS-KEY: <API_ACCESS_KEY>`
- Content-Type：`application/json`
- Timeout：30 秒即可；伺服器驗證後立即回 `202`，Google 路線由 worker 非同步規劃

任務 JSON：

```json
{
  "init_loc": "25.047800,121.517000",
  "stops": [
    {
      "name": "Taipei Main Station",
      "coord": "25.047800,121.517000",
      "mode": "transit",
      "transit_type": "MRT",
      "wait_time": "09:30",
      "skip_if_late": true
    }
  ]
}
```

### Move GPS

用途：接收 PC 傳來的座標，轉交 GPS JoyStick 更新模擬定位。

觸發（MacroDroid HTTP Server）：

- Method：`GET`
- Path / Identifier：`gps`
- Query params dictionary：`http_params`
- Query params：`lat`、`lng`
- 連接埠：`8080`（MacroDroid App 設定，見上方第 2 點）

動作（Send Intent）：

| 欄位 | 值 |
|---|---|
| Action | `theappninjas.gpsjoystick.TELEPORT` |
| Package | `com.theappninjas.fakegpsjoystick` |
| Target | `Service` |
| Extra 1 | `lat`，型別 `Float`，值 `{lv=lat}` |
| Extra 2 | `lng`，型別 `Float`，值 `{lv=lng}` |

Extra 型別必須是 `Float`、Target 必須是 `Service`，填錯 GPS JoyStick 不會有反應。

呼叫格式：

```text
http://<PHONE_TAILSCALE_IP>:8080/gps?lat=25.xxxxxxx&lng=121.xxxxxxx
```

### Stop GPS

用途：從手機呼叫 PC 的 `/stop_task`，停止伺服器端任務。

- Method：`POST`
- URL：`{lv=g_server_url}/stop_task`
- Header：`API-ACCESS-KEY: <API_ACCESS_KEY>`

停止後手機端會停留在最後一次收到的模擬座標。

## 步驟七：啟動

用 VS Code 開啟專案資料夾後按 `F5`，選擇 `Mock GPS Follow Live Nav (web + worker)`。或在終端機執行：

```powershell
.venv\Scripts\python start_local.py
```

啟動後會印出 Dashboard 網址與網路模式。`start_local.py` 是單一進程：Flask dashboard 跑在背景執行緒，mission worker 跑在主執行緒，兩者共用同一個 SQLite 連線池與 log 設定。同一個資料夾一次只能跑一個實例，`data/instance.lock` 會擋下第二個。

停止時按 `Ctrl+C`，或按 VS Code 的停止鍵。整個進程結束，不會留下任何背景服務。目前仍在 planning、queued、running 或 degraded 的任務會標記為 `interrupted`，下次啟動不會續跑，必須由使用者重新傳送指令。SQLite、歷史任務、movement CSV、logs 與封存檔都會保留。

即使進程被強制終止（例如按 VS Code 停止鍵而非 Ctrl+C），下次啟動時 worker 取得 lease 後會自動把殘留在 `running` 的任務回收成 `interrupted`，並在 log 記錄 `Reclaimed N orphaned mission(s) on worker startup`。

重啟後 Dashboard **不會**顯示上次被中斷的任務：狀態回到 `IDLE`，地圖上的規劃路線與軌跡都清空。該次執行的完整紀錄仍保留在任務歷史中，可從歷史頁面查看。任務表單則仍保留上次送出的站點，方便直接重新送出。

`completed`、`stopped`、`aborted`、`failed` 的任務不受影響，重啟後仍會顯示——那些是使用者主動造成的結果，需要被看到。

## 步驟八：首次驗證

依序確認每一段連線，出問題時就能直接定位在哪一段。

1. **Dashboard**：瀏覽器開啟 `http://127.0.0.1:5050/map`，會導向 `/login`，輸入 `.env` 的 `API_SECRET_KEY`，應看到地圖與 `IDLE` 狀態。
2. **手機端獨立驗證**：確認 Tailscale 兩端都已連線，在 PC 瀏覽器開啟

   ```text
   http://<PHONE_TAILSCALE_IP>:8080/gps?lat=25.0478&lng=121.5170
   ```

   頁面應回應 `OK`，且手機上的 GPS JoyStick 定位跳到該座標。這段成功代表 Tailscale、MacroDroid HTTP Server 與 GPS JoyStick intent 都正確。
3. **送出測試任務**：在 PC 的 PowerShell 執行

   ```powershell
   $body = '{"init_loc":"25.047800,121.517000","stops":[{"name":"Taipei 101","mode":"walking"}]}'
   Invoke-RestMethod -Uri http://127.0.0.1:5050/start_task -Method Post `
     -Headers @{ "API-ACCESS-KEY" = "<API_ACCESS_KEY>" } `
     -ContentType "application/json" -Body $body
   ```

   應回傳 `202` 與 `mission_id`。
4. **觀察 Dashboard**：狀態由 `planning` 轉為 `running`，地圖出現 Google 規劃路線，最後座標開始逐秒更新。
5. **觀察手機**：GPS JoyStick 的定位沿著路線移動。
6. **停止任務**：手機按 Stop GPS，或在 PC 執行

   ```powershell
   Invoke-RestMethod -Uri http://127.0.0.1:5050/stop_task -Method Post `
     -Headers @{ "API-ACCESS-KEY" = "<API_ACCESS_KEY>" }
   ```

任務規劃失敗時狀態會轉為 `failed`，原因可在 Dashboard 的 `last_error` 與錯誤 log 查看。

## 任務 API

任務控制 API 只接受 header 金鑰，不接受 Dashboard 登入 session：

```http
API-ACCESS-KEY: <API_ACCESS_KEY>
Content-Type: application/json
```

### 開始任務

```http
POST /start_task
```

成功回應為 `202 Accepted`，任務先進入 `planning`；規劃完成後由 worker 開始執行。規劃失敗時狀態改為 `failed`，原因可由系統狀態 API 與 Dashboard 的 `last_error` 查看。

欄位說明：

| 欄位 | 必填 | 說明 |
|---|---:|---|
| `init_loc` | 是 | 初始座標，格式為 `lat,lng` |
| `stops` | 是 | 任務站點陣列，至少 1 筆，最多 50 筆 |
| `stops[].name` | 是 | Google Maps 可辨識的地名或地址 |
| `stops[].mode` | 是 | `walking`、`transit`、`motorcycle`；`motorcycle` 對應 Google 的 `two_wheeler` |
| `stops[].transit_type` | 否 | `AUTO`、`MRT`、`BUS` 或空字串，只有 `transit` 使用 |
| `stops[].wait_time` | 否 | `HH:MM` 本地時刻，抵達該站後等到這個時間才出發 |
| `stops[].skip_if_late` | 否 | 抵達時已過 `wait_time` 時，`true` 立即出發，`false` 等到隔天同一時刻 |
| `stops[].coord` | 否 | 最終精準對位座標，格式為 `lat,lng`；先以地點名稱導航，再以 1.4 m/s 直線步行到此座標 |

### 停止任務

```http
POST /stop_task
```

## Web 監控

```text
http://localhost:5050/map
```

網頁可查看：

- 任務狀態：`idle`、`planning`、`queued`、`running`、`degraded`、`completed`、`interrupted`、`aborted`、`failed`
- 已完成站點 / 總站點
- 目前目標
- 最後送出的座標
- Tailscale P2P 目標
- 初始／最新 Google ETA、schedule debt 與手機健康狀態
- Google Maps 規劃路線
- Google Maps 導航歷史詳細資訊
- 即時 movement CSV
- 系統、路線、錯誤、安全 log
- 任務歷史列表

選取歷史 session 後會開啟 `/history/<date>/<session>` 新分頁。原 LIVE Dashboard 不會切換模式或停止輪詢；History 分頁中的所有資料按鈕只讀取 URL 指定的 session，缺少資料時不會退回顯示 LIVE 資料。CSV 按鈕以分頁資料表顯示內容，不提供原始檔下載。

網頁不顯示電腦硬體資訊。

## 監控 API

監控 API 可使用登入 session 或 `API-ACCESS-KEY`。

| API | 說明 |
|---|---|
| `GET /api/system_status` | 任務狀態、最後座標、P2P 目標、設定、log session |
| `GET /api/planned_route?route_token=<mission:revision>` | 目前規劃路線座標；版本未變回 304 |
| `GET /api/navigation_history` | 目前任務的導航歷史詳細資訊 |
| `GET /api/movements/current?offset=N&limit=250` | 以 byte cursor 分頁讀取目前 movement JSON |
| `GET /api/log/all` | 目前任務完整 log |
| `GET /api/log/route` | 路線 log |
| `GET /api/log/error` | warning / error log |
| `GET /api/log/security` | 安全事件 log |
| `GET /api/mission` | 目前任務資料 |
| `GET /api/history` | 任務歷史列表 |
| `GET /api/history/<date>/<session>/status` | 固定 session 的任務狀態與 ETA |
| `GET /api/history/<date>/<session>/planned_route` | 固定 session 的規劃路線 |
| `GET /api/history/<date>/<session>/navigation` | 固定 session 的 Navigation 詳細資料 |
| `GET /api/history/<date>/<session>/movements?offset=N&limit=250` | 歷史或封存任務 movement JSON |
| `GET /api/history/<date>/<session>/log/<log_name>` | 歷史任務 log |

movement record 的 `sequence` 是 session 內單調遞增列序號；`next_offset` 則是伺服器端 byte cursor，兩者不可混用。

## Log 系統

常駐 log：

```text
logs/app.log     # start_local.py（web + worker 同一進程）
```

每次任務會建立獨立 session：

```text
logs/YYYY-MM-DD/HH-MM-SS/
├─ all.log
├─ route.log
├─ error.log
├─ security.log
├─ mission.json
└─ movement.csv
```

`movement.csv` 欄位：

```csv
Sequence,Timestamp,Latitude,Longitude,Action,Note,TimestampISO,DeltaSeconds,DistanceMeters
```

- `Sequence`：session 內單調遞增的 movement 列序號。
- `Timestamp`：以 UTC 儲存，含毫秒與 `Z` 標記；Dashboard 依 `TZ` 轉換後顯示。
- `TimestampISO`：aware UTC ISO 時間，含毫秒；Dashboard 依 `TZ` 轉換後顯示。
- `DeltaSeconds`：與上一筆實際紀錄的時間差，單位秒。
- `DistanceMeters`：與上一筆實際紀錄座標的距離，單位公尺。

移動期間 CSV 以真實系統秒數記錄。任務完成後仍每秒傳送終點座標給手機，但 CSV 只在完成時及每 60 秒寫一筆 heartbeat。超過 30 日的 session 會在驗證 ZIP 完整性後刪除原目錄，ZIP 保存於 `logs/archives/` 並可由 Dashboard 按需解壓回放。

## `settings.json`

設定檔位置：

```text
mock_gps/resources/settings.json
```

主要設定：

| 設定 | 預設 | 說明 |
|---|---:|---|
| `mrt_station_groups` | 分組站點資料 | 依路線整理的台北捷運站座標 |

程式啟動時會自動將 `mrt_station_groups` 攤平成內部使用的 MRT 到站偵測資料庫。

## 專案結構

```text
mock-gps-follow-live-nav/
├─ start_local.py
├─ pyproject.toml
├─ requirements.txt
├─ requirements-dev.txt
├─ requirements-lock.txt
├─ README.md
├─ LICENSE
├─ .env.example
├─ .gitignore
├─ macrodroid-example.category
├─ .github/workflows/ci.yml
├─ .vscode/
│  ├─ launch.json
│  ├─ tasks.json
│  └─ settings.json
├─ tests/
├─ data/                        # 執行期產生
│  ├─ mock_gps.sqlite3
│  └─ instance.lock
├─ logs/                        # 執行期產生
└─ mock_gps/
   ├─ config.py
   ├─ db.py
   ├─ logger.py
   ├─ history.py
   ├─ api/
   ├─ core/
   ├─ resources/settings.json
   ├─ static/
   └─ templates/
```

`data/` 與 `logs/` 由程式在第一次啟動時建立，兩者都不進版控。

## 開發與測試

`start_local.py` 是唯一的進入點，把 web 與 worker 跑在同一個進程，適合日常開發與除錯：VS Code 的中斷點在兩邊都有效。同一個資料夾一次只能跑一個實例——`data/instance.lock` 會擋下第二個，worker lease 也只允許一個持有者。

送出變更前執行：

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m compileall -q mock_gps start_local.py
.venv\Scripts\python -m pytest
```

`.github/workflows/ci.yml` 會在 ubuntu 與 windows 上以 Python 3.10 跑同一組檢查。

## 資訊安全

- 任務控制 API 必須使用獨立的 `API_ACCESS_KEY`，且不接受 Dashboard 登入 session
- Web dashboard 需登入或使用有效 API key
- API key 使用 constant-time comparison
- 登入失敗與「帶了錯誤 API key」的請求採滑動視窗節流：同一來源 5 分鐘內失敗 5 次即回 `429`。完全沒帶 key 只回 `401`，讓瀏覽器能正常導向 `/login`
- Flask session cookie 一律設定 `HttpOnly`、`SameSite=Strict`；因為服務以 HTTP 提供，不啟用 `Secure`
- Log 不記錄完整 API key，未授權請求只留遮罩後的前綴；登入成功與失敗事件會記入 security log
- Response 加入安全標頭：
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `Referrer-Policy: no-referrer`
  - `Cache-Control: no-store`
- 建議只在 Tailscale 或可信任網路中使用

## GitHub 上傳前檢查

可上傳的範例與設定：

- `.env.example`
- `macrodroid-example.category`
- `.vscode/launch.json`、`.vscode/tasks.json`、`.vscode/settings.json`
- `mock_gps/resources/settings.json`

不應上傳的本機資料：

- `.env`
- `logs/`
- `data/`
- `*.log`
- `*.csv`
- `.venv/`
- `AI.md`
- 其他 IDE 本機設定

## 疑難排解

| 問題 | 檢查項目 |
|---|---|
| 無法啟動伺服器 | 檢查 `.env` 是否存在，`API_SECRET_KEY` 是否已設定 |
| 啟動時提示無法綁定位址 | 確認 Tailscale 已連線、`BIND_HOST` 是本機真的擁有的位址、`FLASK_PORT` 未被占用 |
| 啟動時提示已在執行 | 同資料夾只能跑一個實例，先關閉既有進程 |
| Google Maps 沒有路線 | 檢查 `GOOGLE_MAPS_API_KEY`、Directions API 是否啟用、Google Cloud 帳單是否正常 |
| 手機沒收到座標 | 依步驟八第 2 點單獨驗證：檢查 Tailscale、`PHONE_TAILSCALE_IP`、MacroDroid HTTP Server 是否開啟且為 8080、手機防火牆 |
| 手機收到 `OK` 但定位沒變 | 檢查模擬位置應用程式是否選為 GPS JoyStick、GPS JoyStick 是否在背景執行、Send Intent 的 Target 是否為 `Service`、`lat`/`lng` 型別是否為 `Float` |
| MacroDroid 完全沒反應 | 確認匯入後的三個 macro 已啟用（匯入時預設停用） |
| MacroDroid 任務送出失敗 | 檢查 `g_server_url`、`API-ACCESS-KEY`、Tailscale 連線 |
| `/api/*` 回傳 401 | 重新登入 `/login` 或確認 `API-ACCESS-KEY` |
| 登入或 API 回傳 429 | 同來源 5 分鐘內驗證失敗 5 次會被節流，等視窗過期再試 |
| 地圖空白 | 檢查本機 vendor 靜態資源與瀏覽器 console |
| 沒有歷史 CSV | 確認任務已開始，並檢查 `logs/YYYY-MM-DD/HH-MM-SS/` |

## 授權

本專案採用 MIT License，詳細內容請見 [LICENSE](LICENSE)。

## 作者

Yang Sheng-Wen

[https://github.com/YangShengWen-0505](https://github.com/YangShengWen-0505)
