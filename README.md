# Mock GPS Follow Live Nav

GPS Route Simulation and Live Navigation System

Mock GPS Follow Live Nav is a local testing system that simulates GPS routes and monitors live navigation progress across a Windows PC, an Android device, and a private Tailscale network.

The project combines a Python and Flask web application, a background mission worker, SQLite persistence, the Google Maps Directions API, MacroDroid, and GPS JoyStick. It is designed to help developers test route planning, asynchronous task execution, real-time monitoring, Android integration, and historical mission analysis in a controlled environment.

## Project overview

The system accepts a route task, plans the complete journey with the Google Maps Directions API, sends simulated coordinates to an Android test device, and records the execution for monitoring and later review.

The main workflow is:

1. Submit a route task through the web dashboard, API, or MacroDroid.
2. Plan the route asynchronously with the mission worker.
3. Forward simulated coordinates to the Android device through Tailscale and MacroDroid.
4. Monitor task status, ETA, route progress, logs, and phone health.
5. Review the completed session through historical data and logs.

## Portfolio context

This project was developed as a practical exploration of asynchronous processing, network communication, route planning, persistent storage, mobile integration, and automated testing. Its focus is not only simulating movement, but also designing a complete workflow that can be observed, verified, interrupted, and reviewed.

This project is intended for personal applications and private testing environments. Do not use it to falsify attendance, bypass third-party service controls, or violate applicable laws or terms of service.

This documentation is written for Windows and PowerShell. The command paths use .venv/Scripts/.

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

## Features

- Task control: use /start_task to start a task and /stop_task to stop the server-side task.
- GPS forwarding: send latitude and longitude to the Android MacroDroid /gps endpoint. Mobile HTTP delivery is decoupled from movement logging.
- Route planning: use Google Maps Directions API to obtain walking, transit, and motorcycle routes.
- Navigation history: preserve the travel mode, vehicle type, route, stops, distance, and duration returned by each Google Directions request.
- Travel modes: support walking, transit, and motorcycle.
- Public transit: use transit_type to select AUTO, MRT, or BUS.
- Position persistence: keep the last simulated position after a task finishes until the task is stopped.
- Task history: create an independent log session and movement.csv file for every task.
- History pages: open a selected session in a separate page. The map, status, route, navigation details, CSV data, and logs all remain bound to that session.
- Web monitoring: view the map, task status, last coordinate, route, logs, and task history.
- Web and worker separation: the web layer accepts and queries tasks while one worker executes navigation.
- SQLite persistence: store tasks, ETA values, route revisions, errors, and phone health status.
- ETA pacing: plan the complete route before starting. After an MRT stop, dynamically adjust pacing without exceeding 1.35 times Google's nominal speed.

## System flow

~~~mermaid
flowchart LR
    A[MacroDroid or API Client] --> B[Flask Web API]
    B --> C[SQLite: plan mission and return HTTP 202]
    C --> D[Single Mission Worker]
    D --> E[Google full-mission planning]
    D --> F[Tailscale HTTP]
    F --> G[Android MacroDroid /gps]
    G --> H[GPS JoyStick TELEPORT intent]
    D --> I[Mission logs and movement.csv]
    B --> J[Web Dashboard]
~~~

## Prerequisites

| Item | Purpose |
| --- | --- |
| Windows PC | Runs the dashboard and mission worker |
| Python 3.10 or later | Creates the virtual environment; CI validates the project with Python 3.10 |
| Google Cloud account | Provides billing and the Directions API |
| Android phone | Runs MacroDroid and GPS JoyStick |
| MacroDroid | Provides the HTTP server and Send Intent actions; some features require Pro |
| GPS JoyStick | Updates the simulated phone location; package name: com.theappninjas.fakegpsjoystick |
| Tailscale | Connects the PC and phone through the same tailnet |

The complete setup has eight stages:

1. Obtain a Google Maps API key.
2. Install the project dependencies.
3. Configure .env.
4. Configure Tailscale.
5. Configure GPS JoyStick.
6. Configure MacroDroid.
7. Start the application.
8. Complete the first verification.

## Step 1: Obtain a Google Maps API key

1. Open Google Cloud Console and create a project.
2. Enable billing for the project. Directions API requires a billing account.
3. Open APIs and Services, then Library, and enable Directions API. This project only calls this API; Geocoding, Places, and Maps JavaScript API are not required.
4. Open Credentials, select Create credentials, choose API key, and copy the generated key.
5. Open the key edit page. Under API restrictions, select Restrict key and allow only Directions API. Because the key is called by the PC backend, you may also add an IP restriction if the PC has a fixed public IP address.

Put the key in GOOGLE_MAPS_API_KEY in .env during Step 3.

## Step 2: Install

Open PowerShell in the project directory and run:

~~~powershell
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -r requirements-lock.txt
~~~

requirements-lock.txt is the complete locked environment snapshot and should be used for normal installation. requirements.txt lists only direct dependencies and is used when updating the lock file.

Install development dependencies when you need to run tests or lint checks:

~~~powershell
.venv/Scripts/python -m pip install -r requirements-dev.txt
~~~

When the project is opened in VS Code, .vscode/tasks.json automatically creates the virtual environment and installs runtime dependencies before F5 starts the application.

## Step 3: Configure .env

Copy the example file:

~~~powershell
Copy-Item .env.example .env
~~~

Example .env content:

~~~dotenv
GOOGLE_MAPS_API_KEY="YOUR_GOOGLE_MAPS_API_KEY"
PHONE_TAILSCALE_IP="100.x.x.x"
API_SECRET_KEY="replace_with_a_long_random_secret"
API_ACCESS_KEY="replace_with_a_different_random_secret"
FLASK_SESSION_SECRET="replace_with_another_random_secret"
BIND_HOST="127.0.0.1"
FLASK_PORT=5050
TZ="Asia/Taipei"
~~~

| Setting | Required | Purpose |
| --- | --- | --- |
| GOOGLE_MAPS_API_KEY | Yes | Directions API key; route planning fails without it |
| PHONE_TAILSCALE_IP | Yes | Phone Tailscale IP used as the GPS delivery target |
| API_SECRET_KEY | Yes | Password used to sign in to the dashboard at /login |
| API_ACCESS_KEY | No | Header key for task control APIs; falls back to API_SECRET_KEY when omitted |
| FLASK_SESSION_SECRET | No | Signing key for the Flask session cookie; falls back to API_SECRET_KEY when omitted |
| BIND_HOST | No | Additional network interface; defaults to 127.0.0.1 |
| FLASK_PORT | No | Dashboard port; defaults to 5050 |
| TZ | No | Runtime timezone using an IANA name; defaults to Asia/Taipei |

The three key variables have different purposes:

- API_SECRET_KEY is used for browser sign-in.
- API_ACCESS_KEY is used by MacroDroid and other clients to call task APIs.
- FLASK_SESSION_SECRET is used only to sign the session cookie.

MacroDroid must use the effective API_ACCESS_KEY value.

## Step 4: Configure Tailscale

The PC and Android phone must sign in to the same tailnet. The PC calls the MacroDroid HTTP server through the phone's Tailscale IP, and the phone can call the task control API through the PC's Tailscale IP.

1. Install Tailscale on both devices and sign in with the same account.
2. Find the PC Tailscale IP:

~~~powershell
tailscale ip -4
~~~

3. Find the phone Tailscale IP in the Tailscale app. The home screen shows an address such as 100.x.x.x.
4. Put the phone IP in .env:

~~~dotenv
PHONE_TAILSCALE_IP="100.x.x.x"
~~~

5. If the phone must open the PC dashboard, put the PC Tailscale IP in BIND_HOST:

~~~dotenv
BIND_HOST="100.x.x.x"
~~~

BIND_HOST is the only network binding setting. It adds an interface while the service continues to listen on 127.0.0.1. Both URLs can then be used:

~~~text
http://127.0.0.1:5050/map      # Local PC
http://100.x.x.x:5050/map      # Phone through Tailscale
~~~

Do not set BIND_HOST to 0.0.0.0. That would expose the unencrypted dashboard to the entire LAN. The service uses HTTP and does not set the Secure flag on the session cookie, so traffic is not protected by HTTPS.

If Tailscale is disconnected, binding the configured address fails and the application prints a warning. Connect Tailscale before starting the service. Use the HTTP service only through Tailscale or a trusted private network. Tailscale encrypts traffic between its nodes, but this application does not provide built-in HTTPS.

## Step 5: Configure GPS JoyStick on Android

GPS JoyStick is the application that updates the simulated phone location. MacroDroid only forwards coordinates from the PC to GPS JoyStick.

1. Install GPS JoyStick. Its package name is com.theappninjas.fakegpsjoystick.
2. Enable Android Developer options by opening Settings, opening About phone, and tapping Build number seven times.
3. Open Settings, System, Developer options, and Select mock location app. Choose GPS JoyStick.
4. Open GPS JoyStick, grant location permission, and move the joystick manually once. Confirm that the phone location changes before testing the PC integration.
5. Disable battery optimization for GPS JoyStick so that Android does not freeze it during a task.

GPS JoyStick must remain running in the background while a task is active.

## Step 6: Configure MacroDroid on Android

The repository root contains macrodroid-example.category, which can be imported as an example category.

1. Install MacroDroid and grant the requested permissions.
2. Enable MacroDroid's local HTTP Server and set its port to 8080. The PC sends to http://{PHONE_TAILSCALE_IP}:8080/gps, so this port is fixed. The HTTP server is an app-level setting and is not included in the imported category.
3. Transfer macrodroid-example.category to the phone and import it into MacroDroid. It provides three macros: Mission Controller (example), Move GPS (example), and Stop GPS (example).
4. Enable all three imported macros manually. They are disabled by default.
5. Update the imported macros:
   - Set the g_server_url local variable to http://{PC_TAILSCALE_IP}:5050. Mission Controller and Stop GPS each have their own copy, so update both.
   - Change the API-ACCESS-KEY HTTP request header from replace_with_API_ACCESS_KEY to the effective API_ACCESS_KEY value in .env.

### Mission Controller

Mission Controller lets the user enter task data on the phone and sends it to /start_task on the PC.

- Method: POST
- URL: {lv=g_server_url}/start_task
- Header: API-ACCESS-KEY: {API_ACCESS_KEY}
- Content-Type: application/json
- Timeout: 30 seconds is sufficient. The server validates the request and returns 202 immediately; the worker plans the Google route asynchronously.

Example task JSON:

~~~json
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
~~~

### Move GPS

Move GPS receives coordinates from the PC and forwards them to GPS JoyStick.

MacroDroid HTTP Server trigger:

- Method: GET
- Path or identifier: gps
- Query parameter dictionary: http_params
- Query parameters: lat and lng
- Port: 8080

Send Intent action:

| Field | Value |
| --- | --- |
| Action | theappninjas.gpsjoystick.TELEPORT |
| Package | com.theappninjas.fakegpsjoystick |
| Target | Service |
| Extra 1 | lat, type Float, value {lv=lat} |
| Extra 2 | lng, type Float, value {lv=lng} |

Extra values must use the Float type and Target must be Service. If either value is wrong, GPS JoyStick does not react.

Example request:

~~~text
http://{PHONE_TAILSCALE_IP}:8080/gps?lat=25.xxxxxxx&lng=121.xxxxxxx
~~~

### Stop GPS

Stop GPS calls /stop_task on the PC and stops the server-side task.

- Method: POST
- URL: {lv=g_server_url}/stop_task
- Header: API-ACCESS-KEY: {API_ACCESS_KEY}

After stopping, the phone remains at the last simulated coordinate it received.

## Step 7: Start the application

Open the project directory in VS Code, press F5, and select Mock GPS Follow Live Nav (web + worker). Alternatively, run:

~~~powershell
.venv/Scripts/python start_local.py
~~~

The application prints the dashboard URL and network mode after startup. start_local.py is a single process: the Flask dashboard runs in a background thread while the mission worker runs in the main thread. Both share the same SQLite connection pool and logging configuration.

Only one instance can run in a project directory. data/instance.lock prevents a second instance.

Stop the application with Ctrl+C or the VS Code stop button. The entire process exits without leaving a background service. Tasks that are still planning, queued, running, or degraded are marked interrupted. They are not resumed at the next startup and must be submitted again. SQLite data, task history, movement CSV files, logs, and archives are preserved.

If the process is forcefully terminated, the worker reclaims leftover running tasks after obtaining its lease, changes them to interrupted, and writes a log entry such as Reclaimed N orphaned mission(s) on worker startup.

After a restart, the dashboard returns to IDLE and does not display the interrupted task on the live map. The complete execution record remains available on the history page. The task form keeps the previously submitted stops so that the task can be resubmitted easily.

Tasks with completed, stopped, aborted, or failed status are not removed during restart and remain visible as records of user-triggered outcomes.

## Step 8: First verification

Verify each connection separately so that a failure can be located quickly.

### Verification 1: Dashboard

Open http://127.0.0.1:5050/map in a browser. It redirects to /login. Enter API_SECRET_KEY from .env and confirm that the map and IDLE status are visible.

### Verification 2: Android connection

Confirm that Tailscale is connected on both devices. From a PC browser, open:

~~~text
http://{PHONE_TAILSCALE_IP}:8080/gps?lat=25.0478&lng=121.5170
~~~

The page should return OK and GPS JoyStick should move to the coordinate. This verifies Tailscale, the MacroDroid HTTP server, and the GPS JoyStick intent independently.

### Verification 3: Submit a test task

Run the following command in PowerShell:

~~~powershell
$body = '{"init_loc":"25.047800,121.517000","stops":[{"name":"Taipei 101","mode":"walking"}]}'
Invoke-RestMethod -Uri http://127.0.0.1:5050/start_task -Method Post -Headers @{ "API-ACCESS-KEY" = "{API_ACCESS_KEY}" } -ContentType "application/json" -Body $body
~~~

The response should contain 202 and mission_id.

### Verification 4: Observe the dashboard

The status should change from planning to running, the planned route should appear, and the last coordinate should update every second.

### Verification 5: Observe the phone

GPS JoyStick should move along the route.

### Verification 6: Stop the task

Stop the task with Stop GPS on the phone or run the following command in PowerShell:

~~~powershell
Invoke-RestMethod -Uri http://127.0.0.1:5050/stop_task -Method Post -Headers @{ "API-ACCESS-KEY" = "{API_ACCESS_KEY}" }
~~~

If route planning fails, the status changes to failed. Check last_error on the dashboard and the error log for the cause.

## Task API

Task control APIs accept the header key and do not accept the dashboard login session.

~~~http
API-ACCESS-KEY: {API_ACCESS_KEY}
Content-Type: application/json
~~~

### Start a task

~~~http
POST /start_task
~~~

A successful response is 202 Accepted. The task first enters planning, and the worker starts execution after route planning completes. If planning fails, the task changes to failed and the reason is available through the system status API and dashboard last_error.

| Field | Required | Description |
| --- | --- | --- |
| init_loc | Yes | Initial coordinate in lat,lng format |
| stops | Yes | Array of task stops; at least 1 and at most 50 stops |
| stops[].name | Yes | Place name or address recognized by Google Maps |
| stops[].mode | Yes | walking, transit, or motorcycle; motorcycle maps to Google's two_wheeler mode |
| stops[].transit_type | No | AUTO, MRT, BUS, or an empty string; used only with transit |
| stops[].wait_time | No | Local HH:MM time. The task waits until this time after arriving at the stop |
| stops[].skip_if_late | No | If the arrival time has passed wait_time, true departs immediately and false waits until the same time on the next day |
| stops[].coord | No | Final coordinate in lat,lng format. The task first navigates by place name, then walks in a straight line at 1.4 m/s to this coordinate |

### Stop a task

~~~http
POST /stop_task
~~~

## Web monitoring

Open http://localhost:5050/map after signing in.

The dashboard displays:

- Task status: idle, planning, queued, running, degraded, completed, interrupted, aborted, or failed
- Completed stops and total stops
- Current target
- Last coordinate sent to the phone
- Tailscale peer-to-peer target
- Initial and latest Google ETA, schedule debt, and phone health status
- Google Maps planned route
- Detailed Google Maps navigation history
- Live movement CSV data
- System, route, error, and security logs
- Task history list

Selecting a history session opens /history/{date}/{session} in a new tab. The live dashboard continues its normal polling and does not switch modes or stop. All data buttons on a history page read only the session identified by the URL. If a historical data source is missing, the page does not fall back to live data. The CSV button displays a paginated table instead of downloading the original file.

The web page does not display computer hardware information.

## Monitoring API

Monitoring APIs accept either a login session or API-ACCESS-KEY.

| API | Description |
| --- | --- |
| GET /api/system_status | Task status, last coordinate, peer target, settings, and log session |
| GET /api/planned_route?route_token={mission:revision} | Current planned route coordinates; returns 304 when the revision is unchanged |
| GET /api/navigation_history | Detailed navigation history for the current task |
| GET /api/movements/current?offset=N&limit=250 | Reads the current movement JSON with a byte cursor |
| GET /api/log/all | Complete log for the current task |
| GET /api/log/route | Route log |
| GET /api/log/error | Warning and error log |
| GET /api/log/security | Security event log |
| GET /api/mission | Current task data |
| GET /api/history | Task history list |
| GET /api/history/{date}/{session}/status | Task status and ETA for a fixed session |
| GET /api/history/{date}/{session}/planned_route | Planned route for a fixed session |
| GET /api/history/{date}/{session}/navigation | Detailed navigation data for a fixed session |
| GET /api/history/{date}/{session}/movements?offset=N&limit=250 | Movement JSON for a historical or archived task |
| GET /api/history/{date}/{session}/log/{log_name} | Log for a historical task |

The movement record sequence is a monotonically increasing row number within a session. next_offset is a server-side byte cursor. These values must not be used interchangeably.

## Log system

Persistent application log:

~~~text
logs/app.log     # start_local.py: web and worker in one process
~~~

Each task creates an independent session:

~~~text
logs/YYYY-MM-DD/HH-MM-SS/
├─ all.log
├─ route.log
├─ error.log
├─ security.log
├─ mission.json
└─ movement.csv
~~~

movement.csv columns:

~~~text
Sequence,Timestamp,Latitude,Longitude,Action,Note,TimestampISO,DeltaSeconds,DistanceMeters
~~~

- Sequence: monotonically increasing movement row number within the session.
- Timestamp: UTC timestamp with milliseconds and a Z marker; the dashboard converts it using TZ.
- TimestampISO: aware UTC ISO timestamp with milliseconds; the dashboard converts it using TZ.
- DeltaSeconds: time difference in seconds from the previous recorded movement.
- DistanceMeters: distance in meters from the previous recorded coordinate.

During movement, CSV records use real system seconds. After a task completes, the phone still receives the final coordinate every second, but the CSV writes only a completion record and one heartbeat every 60 seconds.

Sessions older than 30 days are deleted from their original directories after ZIP integrity is verified. The ZIP files are stored in logs/archives/ and can be extracted by the dashboard for playback.

## settings.json

Configuration file:

~~~text
mock_gps/resources/settings.json
~~~

| Setting | Default | Description |
| --- | --- | --- |
| mrt_station_groups | Grouped station data | Taipei MRT station coordinates organized by route |

At startup, the application flattens mrt_station_groups into the internal MRT arrival-detection data.

## Project structure

~~~text
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
├─ data/                        # Generated at runtime
│  ├─ mock_gps.sqlite3
│  └─ instance.lock
├─ logs/                        # Generated at runtime
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
~~~

data/ and logs/ are created by the application on the first startup and are not committed to version control.

## Development and testing

start_local.py is the single entry point for the web application and worker, which makes it suitable for daily development and debugging. VS Code breakpoints work in both components. Only one instance can run in a directory; data/instance.lock and the worker lease prevent multiple owners.

Before submitting changes, run:

~~~powershell
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m compileall -q mock_gps start_local.py
.venv/Scripts/python -m pytest
~~~

.github/workflows/ci.yml runs the same checks on Ubuntu and Windows with Python 3.10.

## Security

- Task control APIs require a separate API_ACCESS_KEY and do not accept the dashboard login session.
- The web dashboard requires a login session or a valid API key.
- API keys are compared using a constant-time comparison.
- Login failures and requests with an incorrect API key are rate limited in a sliding window. Five failures from the same source within five minutes return 429. Requests with no key return 401 so that browsers can redirect to /login.
- Flask session cookies use HttpOnly and SameSite=Strict. Secure is not enabled because the service uses HTTP.
- Logs never record a complete API key. Unauthorized requests keep only a masked prefix, and successful and failed login events are written to the security log.
- Responses include X-Content-Type-Options: nosniff, X-Frame-Options: DENY, Referrer-Policy: no-referrer, and Cache-Control: no-store.
- Use the application only through Tailscale or another trusted private network.

## GitHub upload checklist

Safe example and configuration files:

- .env.example
- macrodroid-example.category
- .vscode/launch.json, .vscode/tasks.json, and .vscode/settings.json
- mock_gps/resources/settings.json

Local files that must not be uploaded:

- .env
- logs/
- data/
- *.log
- *.csv
- .venv/
- AI.md
- Other IDE-specific local settings

## Troubleshooting

| Problem | What to check |
| --- | --- |
| The server does not start | Confirm that .env exists and API_SECRET_KEY is set |
| The application cannot bind the configured address | Confirm that Tailscale is connected, BIND_HOST belongs to the local machine, and FLASK_PORT is available |
| The application reports that it is already running | Only one instance is allowed in a directory; stop the existing process |
| Google Maps returns no route | Check GOOGLE_MAPS_API_KEY, Directions API activation, and Google Cloud billing |
| The phone receives no coordinate | Follow the independent Android verification in Step 8. Check Tailscale, PHONE_TAILSCALE_IP, the MacroDroid HTTP server on port 8080, and the phone firewall |
| The phone returns OK but its location does not change | Confirm GPS JoyStick is selected as the mock location app, is running in the background, uses Target Service, and receives Float values for lat and lng |
| MacroDroid does nothing | Confirm that all three imported macros are enabled; they are disabled by default |
| MacroDroid cannot submit a task | Check g_server_url, API-ACCESS-KEY, and the Tailscale connection |
| /api/* returns 401 | Sign in again at /login or provide API-ACCESS-KEY |
| Login or API requests return 429 | Five verification failures from the same source within five minutes trigger rate limiting; wait for the window to expire |
| The map is blank | Check local vendor assets and the browser console |
| No historical CSV is available | Confirm that a task has started and check logs/YYYY-MM-DD/HH-MM-SS/ |

## License

This project is licensed under the MIT License. See LICENSE for the full text.

## Author

Yang Sheng-Wen

https://github.com/YangShengWen-0505
