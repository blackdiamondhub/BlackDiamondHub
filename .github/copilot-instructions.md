# Copilot Instructions for BlackDiamondHub

## Project Overview

BlackDiamondHub is a Django-based home automation and management hub for a vacation property at Sun Peaks, BC. It runs on Django 5.1 with Python 3.11, uses PostgreSQL for data storage, and integrates with Home Assistant for device control. The app is served via Daphne/Channels (ASGI) to support WebSockets.

## Django Apps

- **hub_settings** — Staff-only Settings page at /settings/ (avatar menu → Settings): a Lodge section (Home layout, Apps list with versions) plus one page per app that declares `PluginSpec.settings`. Saved values live in `AppSetting` rows and override the environment; `store.lookup` is registered with bdh-sdk's `set_store` and cached for 30 s (cleared on save/delete)
Installed from their own repos via `sites/sunpeaks/apps.txt`:

- **wifi** (`blackdiamondhub/bdh-app-wifi`) — WiFi QR code generator with password display
- **sunpeaks_webcams** (`bdh-app-sunpeaks-webcams`) — Sun Peaks resort webcam viewer
- **lift_status** (`bdh-app-lift-status`) — Sun Peaks lift and trail status
- **snow_report** (`bdh-app-snow-report`) — Mountain Conditions: seasonal snow/weather report + Open-Meteo forecast
- **radiant** (`bdh-app-radiant`) — Radiant heating / heat-pump plant dashboard
- **device_control** (`bdh-app-device-control`) — the "Controls" tile: scenes (first tab and homepage quick actions), lights, blinds, fireplaces and Gemstone Lights via Home Assistant (`BDH_DEVICE_CONTROL_SCENES_FILTER`, legacy `BDH_SCENES_FILTER`/`SCENE_FILTER`; `BDH_DEVICE_CONTROL_GEMSTONE_EMAIL`/`_PASSWORD`, legacy `GEMSTONE_*`)
- **cameras** (`bdh-app-cameras`) — Live camera feeds via go2rtc + UniFi Protect Integration API (`BDH_CAMERAS_GO2RTC_URL`, `BDH_CAMERAS_PROTECT_{HOST,API_KEY,NAME}_n`; legacy `GO2RTC_URL`, `UNIFI_PROTECT_*_n`)
- **vacation_mode** (`bdh-app-vacation-mode`) — Automates switching between vacation and home modes via Home Assistant REST API (thermostats, water heater, hot tub, TVs, fridge/freezer eco modes). Includes post-action state verification since HA silently drops commands to offline devices (`BDH_VACATION_MODE_MODES_URL`/`_MODES_API_KEY`/`_PREHEAT`, legacy `BDL_MODES_*`)
- **speakers** (`bdh-app-speakers`, was `sonos_control`) — Sonos + Home Assistant media_player control (container app; settings under `[apps.speakers]` / `BDH_SPEAKERS_*` only)
- **stays** (`bdh-app-stays`) — no pages; its `stay` hook reads `sensor.bdl_stay_current` from the Stays AppDaemon app to put the homepage in guest (or owner-visit) mode, and its `card` hook provides the lodge card (`BDH_STAYS_ENTITY` and friends, see its README)
- **inventory** (`bdh-app-inventory`) — kitchen inventory with item photos and QR code labels at `/inventory/` (sign-in, no homepage tile). Its tests use pyzbar, so keep `pyzbar`/`libzbar0` in the hub requirements and CI
- **feedback** (`bdh-app-feedback`, container app) — guest feedback inbox. The top bar's Feedback pill/modal, inbox link and unread badge come from `hub/feedback.py` and appear only when this app is installed and enabled (the hub has no built-in feedback app any more; `hub/migrations/0001_drop_builtin_feedback.py` dropped its old table)

## Tech Stack

- **Backend**: Django 5.1, Python 3.11, PostgreSQL, Daphne/Channels (ASGI)
- **Frontend**: Bootstrap 5 (crispy-bootstrap5), Font Awesome 6 icons
- **Auth**: Django auth + social-auth-app-django (Spotify OAuth)
- **Home Assistant**: REST API at `192.168.10.212:8123`; URL and token are set on Settings → Home Assistant (`HAInstance` rows, token in the vault), not in `.env`
- **Speech**: the hub makes announcement/alert speech itself (`hub_settings/speech.py` → `bdh_sdk.speakers.set_synth`): Settings → Speech picks Google (default) or Azure Speech (key in the vault); falls back Azure → Google → HA TTS; clips cached in `SpeechClip`, pruned daily after 90 days unused
- **Containerization**: Docker + docker-compose (see Deployment section)
- **Environment**: `.env` file for secrets (not committed)

## Deployment & Infrastructure

### Production Host
- **Server**: Ubuntu host `homehub` at `~/bdl/BlackDiamondHub`
- **Network**: Dual-subnet — Sun Peaks (`192.168.10.x`) and Mercer Island (`192.168.1.x`)
- **Docker Compose V2** (`docker compose`, not the old `docker-compose` v1)

### CI/CD Pipeline
1. **Code changes** go through feature branches → PRs → merge to `main`
2. **GitHub Actions** (`docker-publish.yml`) triggers on push to `main`:
   - Builds the Docker image from the Dockerfile
   - Pushes to GHCR: `ghcr.io/blackdiamondhub/blackdiamondhub/blackdiamondhub:latest`
3. **Watchtower** on production polls GHCR every 300s, pulls new images, and recreates the `BlackDiamondHub` and `bdh-supervisor` containers automatically
   - Watchtower runs in label mode (`WATCHTOWER_LABEL_ENABLE=true`) and only updates containers labelled `com.centurylinklabs.watchtower.enable=true`: `BlackDiamondHub` and `bdh-supervisor`. App containers (`bdh-app-*`, one digest-pinned image per app) and their companions (e.g. the cameras app's go2rtc) are managed only by the supervisor

### What auto-deploys vs. what doesn't
- **Auto-deploys** (via Watchtower): Any code change inside the Docker image — Django code, templates, static files, Python dependencies
- **Does NOT auto-deploy**: Changes to `docker-compose.yml` or `.env` on the server — these require SSH + `git pull` + `sudo deploy/bootstrap.sh` (idempotent; see README "Deploying a hub server", and `deploy/create_db.sh` for the database logins)

### Docker Services (docker-compose.yml)
- **djangoapp** (`BlackDiamondHub`) — Django app on port 8000, image from GHCR
  - Runs `docker/entrypoint.sh`: waits for the DB; if migrations are pending, `pg_dump`s to `./backups` (host volume, last 10 kept) and runs `migrate`; then serves with **Daphne** (not `runserver`). Static files are collected at image build and served by **WhiteNoise**.
  - It also runs `manage.py mdns_advertise` in the background (`hub/mdns.py`). This announces the hub over mDNS/DNS-SD as **"Black Diamond Hub"** (`_bdh-hub._tcp`, TXT `path`/`port`/`site`/`name`/`location`) so the BDH Display kiosk can find it without a hardcoded IP. It relies on the container's host networking. `BDH_MDNS=off` disables it; `BDH_MDNS_ADDRESS` overrides the announced LAN IP.
  - Requires `DJANGO_SECRET_KEY` in `.env`; `DJANGO_DEBUG` defaults to off (set `DJANGO_DEBUG=true` in a local `.env` for development).
  - `/healthz` (DB check, exempt from ALLOWED_HOSTS) backs the image's Docker `HEALTHCHECK`.
  - Rollback: images are also tagged `:sha-<commit>`; pin `image:` in compose to a previous sha and `docker compose up -d`. Watchtower does not roll back on its own, and an image rollback does not undo migrations (restore from `./backups` if needed).
- **bdh-supervisor** — Runs the container apps and their companions. The cameras app's go2rtc (`ghcr.io/blackdiamondhub/go2rtc-nack`, upstream + NACK fix) is a companion: API on 127.0.0.1:11900+slot (loopback only; browsers never reach it, the cameras app checks each visitor and proxies the signalling websocket), WebRTC media on 8555, advertising `BDH_GO2RTC_CANDIDATES` (this host's LAN IP:8555, from `.env`)
- **watchtower** (`containrrr/watchtower`) — Auto-updates the Django and bdh-supervisor containers from GHCR

## UniFi Protect Integration API

- **API Docs**: https://developer.ui.com/protect/v6.2.88/get-v1metainfo (sidebar has all endpoints)
- **Auth**: API key via `X-API-KEY` header, key generated at unifi.ui.com → Settings → API Keys
- **Base URL**: `https://{UNIFI_PROTECT_HOST}/proxy/protect/integration/v1`
- **Camera list**: `GET /v1/cameras` — returns id, name, state, featureFlags (note: PTZ flags NOT exposed)
- **RTSPS streams**: `GET /v1/cameras/{id}/rtsps-stream` and `POST /v1/cameras/{id}/rtsps-stream` with `{"qualities": ["high"]}`
- **PTZ endpoints** (for PTZ cameras only, non-PTZ returns 400 "Camera is not a type of PTZ"):
  - `POST /v1/cameras/{id}/ptz/goto/{slot}` — Move to preset (slots 0-4, 204 on success, 404 if preset doesn't exist)
  - `POST /v1/cameras/{id}/ptz/patrol/start/{slot}` — Start patrol
  - `POST /v1/cameras/{id}/ptz/patrol/stop` — Stop patrol
- **go2rtc**: Proxies RTSPS streams to browser via WebRTC/MSE. The browser loads `video-stream.js` and opens `/api/ws` through the cameras app (`cameras/api/ws`, which checks `policy.can_view` and relays to go2rtc on loopback); `video-stream.js` web component (set src/mode/background as JS properties, NOT HTML attributes). Docker image: `ghcr.io/blackdiamondhub/go2rtc-nack` (upstream `alexxit/go2rtc` source + a patch so the WebRTC sender answers NACKs)

## CI/CD

- **Test workflow** (`tests.yml`): Runs on every push/PR
  - Three jobs: `unit-tests` (excludes selenium tag), `selenium-tests` (selenium tag only, with retry via nick-fields/retry), and `image-smoke-test` (builds the Docker image, boots it against Postgres 16 and checks backup, migrate, health, pages and static files)
  - Test jobs run on `ubuntu-22.04` with a PostgreSQL service container
  - Selenium tests use headless Chrome with shared helpers in `tests/selenium_helpers.py`
- **Docker publish workflow** (`docker-publish.yml`): Runs on push to `main`
  - Builds Docker image and pushes to GHCR (`ghcr.io/blackdiamondhub/blackdiamondhub/blackdiamondhub:latest` and `:sha-<commit>`)
  - Watchtower on production auto-pulls the new image (see Deployment section)

## Branch & Git Workflow

- **All changes must go into a feature branch** — never commit directly to `main`
- **Only I (the user) merge PRs to main** — Copilot should push branches and may suggest creating a PR, but should not merge
- Branch naming: `feature/description`, `fix/description`
- `main` branch has protection rules: require PR, require status checks (unit-tests, selenium-tests), no force push, no deletion

## Pull Requests

- **Always use the GitHub CLI (`gh`) for PR operations** — do not use MCP tools, API calls, or browser-based approaches
- Create PRs: `gh pr create --title "..." --body "..." --base main`
- View PR status: `gh pr view`
- List PRs: `gh pr list`
- Check CI status: `gh pr checks`
- The `gh` CLI is already authenticated on this machine

## Testing

- Install the site's extracted apps into the venv first (once, and after each `apps.txt` bump): `venv\Scripts\python.exe -m pip install --require-hashes --no-deps -r sites\sunpeaks\apps.txt`. Their own tests run in each app repo's CI, not here.
- Run unit tests: `venv\Scripts\python.exe manage.py test --exclude-tag=selenium --verbosity 2`
- Run one app's tests only: `venv\Scripts\python.exe manage.py test hub --verbosity 2` (installed apps need a module label, e.g. `vacation_mode.tests`)
- Run all tests: `$env:PGPASSWORD='postgres'; venv\Scripts\python.exe manage.py test --verbosity 2`
- Selenium tests extend `StaticLiveServerTestCase` and use shared `get_chrome_options()` from `tests/selenium_helpers.py`
- Always run relevant tests after making changes

## Communication Preferences

- **Do NOT use the ask_questions tool with selectable options** — if you have questions, just ask them directly in the chat as plain text
- Keep explanations concise and direct
- When making changes, implement them rather than just suggesting

## Key Files

- `BlackDiamondHub/settings.py` — Django settings, database config, and the `HUB_PLUGINS` list of hub apps
- `BlackDiamondHub/urls.py` — core URLs; hub app URLs are mounted automatically from their `PluginSpec`
- `hub/plugins.py` — plugin contract (`PluginSpec`, `Tile`, modes, sections) that every hub app implements
- `hub/loader.py` / `hub/modes.py` — plugin discovery/validation, websocket route collection and homepage mode (guest / dashboard / owner) + tile lookup
- `hub/spotify_auth.py` — platform-owned Spotify social-auth hooks (`spotify_token` context processor, disconnect-pipeline steps) referenced from core settings
- `bdh_sdk.alerts` — pop-up alerts on the hub display (e.g. the doorbell camera): `push(Alert(app, key, title, embed_url, body="", ttl=60, wake=True))` (or `await apush(...)`) from any thread, non-blocking. `hub/alerts.py` installs the sender with `set_sender` and serves `ws/hub/alerts/` (a core route in `asgi.py`); `hub/static/hub/alerts.js` (loaded by `base.html`, display-sized screens only) shows one alert at a time in a modal with the `embed_url` iframe, dedupes by `key`, wakes/pauses the screensaver (`BDHSaver.wake/pause/resume`). A new (not repeated) alert plays its `sound` through `hub/static/hub/chimes.js` (`window.BDHChimes.play(id|path, volume)`, Web Audio; ids = `bdh_sdk.alerts.CHIMES`, `None` = `ding-dong`, `"none"` = silent) at Settings → Display → Pop-up sound volume (`chime_volume`, in the `bdh-saver-cfg` JSON), and stays silent while `BDHSaver.isNight()`. `chimes.js` loads on every page so settings pages can preview. Staff test hook: `/hub/api/alerts/test/` (`?embed=/path/&ttl=N&sound=<id>`). The alert's own iframe can `parent.postMessage({bdhAlert: 'hold'|'release'|'hangup'}, '*')`: hold pauses the countdown (like Keep open, 600 s cap), release restarts a 30 s grace countdown, hangup closes the alert (the cameras pop-up holds while the mic is on). The update reload in `base.html` (`bdhClock`, after a hub or app update) waits while an alert is open (`BDHAlerts.isOpen()`).
- `display/static/display/saver.js` — the screensaver (`{% screensaver %}` in `base.html`, display-sized screens only; idle time from display settings). A page that must stay on screen (e.g. live cameras) opts out with `<meta name="bdh-screensaver" content="off">` in its `extra_head` block.
- `bdh_sdk.ha` — shared Home Assistant client (`get_entity_states`, `get_history`, `call_service`, `ws_commands`…), in bdh-sdk so extracted apps can use it. Apps import it from `bdh_sdk.ha`, never from another app (`hub/ha_client.py` and `device_control.ha_client` are compatibility re-exports)
- **Remote control (development only).** Set `HUB_REMOTE_TOKEN` in the hub's `.env` to turn it on; leave it unset in production and the websocket, the API and the script are all off. `base.html` then loads `hub/static/hub/remote.js` on every page, which connects to `ws/hub/remote/` (`hub/remote.py`). `GET /hub/api/remote/` with header `X-Hub-Token` lists the connected screens; `POST` `{target: ip or screen id, cmd, args, timeout}` runs a command on the newest connection from that screen and returns its reply (504 on timeout). Commands: `state`, `query`, `tap` (`selector`/`text`/`x,y`, `fingers`, `hold_ms`), `swipe`, `key`, `type`, `scroll`, `measure` (frame rate, long tasks, per-video decoded/dropped fps), `logs`, `navigate`, `reload`, `back`, `eval` (async function body). Synthetic taps have no user activation, so a refused `requestFullscreen` falls back to a stand-in that applies the page's `:fullscreen` rules to `.hub-remote-fs` and fakes `document.fullscreenElement`.
- `tests/selenium_helpers.py` — Shared Selenium test utilities (login, Chrome options, network waits)
- `.env` — Environment variables (Django secret, DB credentials, supervisor token, etc.)
- `requirements.txt` — Python dependencies

## Base Template & Navbar (`templates/base.html`)

`templates/base.html` is the public shell contract for hub apps: every app page does `{% extends 'base.html' %}` and fills its blocks (`content`, `navbar_title`, `navbar_extras`, `extra_head`, `body_class`). Keep those block names stable — apps in other repos depend on them. Core templates (`base.html`, `index.html`, `registration/login.html`) stay in the project `templates/` folder.

All pages extend `base.html`, which provides:
- **Navbar**: Bootstrap 5 `navbar-dark bg-dark fixed-top`. Left: a round home button (`#home-button`, hidden on the homepage) and the lodge brand (monogram + "BLACK DIAMOND LODGE / SUN PEAKS · BC", also links home). Centre: the app title (`{% block navbar_title %}`, default `hub_nav_title` from `hub.context_processors.nav_title` = the app's tile title). Right: `navbar_extras`, then the Feedback pill and either the login icon or, when signed in, the avatar menu (#user-menu-button: Feedback inbox with the unread .badge count, Settings and Admin for staff, Sign out)
- **Phones (≤600px)**: one-row bar. App pages drop the monogram and left-align the full title; signed-in users lose the Feedback pill (the avatar menu gets "Send feedback", `.hub-menu-fb`). `navbar_extras` moves to a fixed, sideways-scrolling row under the bar (content offset 118px). The `<nav>` carries `is-home` / `is-owner` classes for this; the homepage keeps its extras inline
- **Title bar only**: the app name and lodge brand appear in the navbar only. App pages must not render their own brand header or a visible page-level title; keep a `<h1 class="visually-hidden">` for accessibility. Section headings and sub-page titles (e.g. "Edit Item") are fine
- **Touch targets**: All navbar buttons/links have 48×48px minimum tap targets for touchscreen use
- **Navbar colour**: `.nav-link` elements inside the navbar have explicit `color: rgba(255,255,255,0.85)` because they sit outside a `.navbar-nav` wrapper — Bootstrap's `navbar-dark` rules don't reach them otherwise
- **Content wrapper**: `<div class="container-fluid content-wrapper">` with `margin-top: 62px` to clear the fixed navbar
- **Stay key**: every template gets `hub_stay_key` (`hub.context_processors.stay_key`), an opaque hash of the stay in progress or `""`. It's a callable, so the stay is only looked up on pages that render it. Apps use it to reset per-stay browser data when new guests arrive (the restaurants shortlist does). `hub.modes.stay_key(stay)` is the only place the key is made (it calls `bdh_sdk.stays.key`); the visitor identity sent to container apps (`request.bdh_stay_key`) and the `stays.current` broker op use the same one

### `{% block navbar_extras %}`

A template block in the navbar between the brand and the right-side icon group. Pages that need unit-conversion toggles (or other page-specific navbar controls) fill this block:

- **Landing page** (`index.html`): `°C/°F` button — the **hub-wide units setting**. `toggleUnits()` converts temperature/elevation in-place via `data-metric` attributes, then POSTs to `hub_units` (`/hub/api/units/`, guests allowed), which saves `AppSetting(app_id="hub", key="units")` and clears cached plugin status/stats (`hub.status.forget_units()`). The same value is on Settings → General → Units (the `location` page). A site's default comes from `[apps.hub] units = "imperial"` in `site.toml`.
- **Mountain Conditions** (`snow_report/snow_report.html`): `°C, m` / `°F, ft` buttons — server-side reload via `?units=metric` / `?units=imperial` query param
- **All other pages**: Block is empty, no toggle shown

Apps read the hub units with `bdh_sdk.units.units()` (`temp_text`, `length_text`, …) and must not keep their own units setting; a view-only local switch is fine, but only the hub toggle changes the system value.

Use the `.navbar-toggle-btn` class for toggle buttons in this block. When adding a new page that needs a unit toggle or similar control, fill `{% block navbar_extras %}` with the appropriate buttons — don't add inline toggles to the page body.

`base.html` also has `{% block extra_head %}` (page styles in `<head>`) and `{% block body_class %}` (classes on `<body>`).

## Hub Apps (plugins)

The hub is a platform; each feature is an app that describes itself in `<app>/plugin.py` with a `PluginSpec` (`hub/plugins.py`). The hub uses the specs to add apps to `INSTALLED_APPS`, mount their URLs at `<id>/`, and build the homepage tiles. Apps in their own repos register a `blackdiamondhub.plugins` entry point instead of being listed in `settings.HUB_PLUGINS`.

To add an app: create `<app>/plugin.py` with `spec = PluginSpec(...)`, add `'<app>.plugin.spec'` to `HUB_PLUGINS`, and put its tile icon at `<app>/static/<app>/hub/icon.svg` (64×64 rounded tile, tinted gradient, white 2.6-stroke glyph — match the existing icons).

Homepage modes: `owner` when signed in, `guest` while an app's `stay` hook reports a stay, otherwise `dashboard`. `Tile.modes` only hides tiles — owner-only views must still enforce `@login_required` themselves. During an owner visit (`Stay.owner`), `hub.owner_visit.OwnerVisitMiddleware` makes `request.user` the `HUB_OWNER_VISIT_USER` account (blank = off); nothing is stored in the session, so it ends with the visit. In guest mode the homepage never mentions owner features beyond the navbar login icon; the page reloads itself when the polled mode changes.

Homepage hooks (optional, dotted paths in the spec, conventionally in `<app>/homepage.py`):
- `status` → `Status(text, level)` (levels `info`/`on`/`warn`) replaces the tile subtitle. Hooks run in a thread pool with a 6 s deadline and are cached 60 s; on failure the last good value is shown faded as stale. A tile with `Tile.icon_on` (bdh-sdk 0.44+) shows that icon instead of `icon` while the level is `on` (e.g. the Fireplace app's animated flames); `templates/index.html` `swapIcon` only changes the `src` when it differs, so SVG animations don't restart on each poll. A hook that takes a `mode` argument is called with the homepage mode and cached per mode (e.g. cameras counts every site only for owners).
- A `Status` may also carry live `rows` (`StatusRow(id, title, detail, image, dimmed, actions)`), tile-wide `actions` (`Action(id, icon, label, primary)`) and `refresh` seconds (min 2; also caps the status cache). The tile then renders the rows instead of its icon (L: up to 3 rows or one in full, M: the first row, S: a summary with the first tile action). Buttons `POST /hub/api/action/<plugin>/` (CSRF, form `row` — "" for tile-wide — and `action`) to the `run_action(row_id, action_id[, mode])` hook; the hub 404s buttons the current status doesn't offer, then busts the status cache. Image URLs must be `http(s)://` or a site path.
- `shortcuts` → list of `Shortcut` for the Quick actions row, run via `run_shortcut(id)` from `POST /hub/api/shortcut/<plugin>/<id>/` (CSRF). Shortcuts are owner-only unless they set `modes` explicitly; the hub rejects shortcuts not offered in the caller's mode.
- The homepage polls `GET /hub/api/status/` every minute.
- `stay` → `Stay(guest, arrival, checkout, owner)` or None (`hub.status.current_stay`). Run like `status` but with a 3 s deadline since every homepage request needs it; a stale last-good stay is dropped once its checkout passes. Never put door PINs in a `Stay`. Container apps read it through the broker op `stays.current` (permission `stays.read`, `bdh_sdk.stays.current()`, SDK 0.45+), which returns `{"stay": null}` or `{"stay": {guest, arrival, checkout, owner, key}}`.
- `card` → `Card(title, detail, readings, alerts, url_name, level)` or None (`hub.status.lodge_card`): the lodge card at the top of the homepage in every mode, from the first app with the hook. Same 3 s deadline; also returned by the status API and re-rendered on each poll. Guests get it without alerts or the link. Without a card app, guests get the plain welcome line.
- `tools` / `run_tool` (bdh-sdk 0.46+, both or neither) → assistant tools: `tools([mode])` returns `bdh_sdk.tools.Tool(id, title, description, params, modes, confirm, readonly)` (owner-only unless `modes` says otherwise; `params` is a flat JSON-schema object) and `run_tool(tool_id, args[, mode])` returns a JSON-serializable answer for the model (raise `ToolError("message")` to tell the model why it failed).
- `websocket_routes` → dotted path to a list of Channels URL patterns (e.g. `"sonos_control.routing.websocket_urlpatterns"`). `BlackDiamondHub/asgi.py` collects them from every installed plugin with `hub.loader.websocket_urlpatterns()` after Django is set up, and serves them behind `AllowedHostsOriginValidator(AuthMiddlewareStack(...))`. Never import an app's routing from core code.

### Assistant tool gateway (`hub/tools.py`)

The voice assistant (a container app) never calls other apps directly; it goes through the broker ops `tools.open` / `tools.list` / `tools.call` (permission `tools`, admin-granted; `bdh_sdk.tools.open_session/list_tools/call_tool`).
- `tools.open {kind: "voice", display}` mints a signed session token (`django.core.signing`, salt `bdh.assistant.session`, 15 min) bound to the calling app. The hub picks the mode, never the caller: voice is `guest` while a stay is active, else `dashboard` — never `owner` — and it is recomputed on every list/call. Forged, expired or other apps' tokens fail with `bad_session` (400).
- The catalog per mode is: app tools (`<plugin>__<tool>`), Quick-action shortcuts offered in that mode (`<plugin>__shortcut__<id>`, honouring `quick.app_allows`), status buttons the current status offers (`<plugin>__action__<row>__<action>`, via `status.run_action`) and the read-only `hub__status` summary. Names match OpenAI's `^[a-zA-Z0-9_-]{1,64}$` (`bdh_sdk.tools.tool_name`; long or colliding names get a hash suffix). Hub setting `assistant_dashboard_control` (Settings → Location, default on): when off, `dashboard` sessions only get `readonly` tools.
- Only apps on the hub setting `assistant_apps` (Settings → Location → Assistant "Apps the assistant can use"; default `scenes` + `fireplace` when never saved) contribute tools, shortcuts or actions — for every consumer, including the owner list. An app with `PluginSpec.assistant=False` (container apps declare it in their manifest; `RemoteSpec` carries it) offers nothing even when allowlisted. `hub__status` only describes allowlisted, opted-in apps (plus the lodge card unless its app opted out). Calls re-check the allowlist at run time.
- Every call re-checks the tool is offered, validates args with `bdh_sdk.tools.validate_args` before the app is called, runs with a 25 s deadline and returns `{ok, result}` / `{ok: false, error}`. The hub publishes `assistant/<sid>/open` and `assistant/<sid>/call` events (`assistant` is a hub-only topic apps can't publish to, webhook sources and HA instances can't use) for audit.
- `GET /hub/api/tools/` (staff) lists the owner catalog for testing.

### Rules that keep apps extractable into their own repos

- **Namespaced templates.** An app's templates live in `<app>/templates/<app_label>/` and are always referenced with the prefix — `render(request, "wifi/wifi_qr.html")`, `{% include "sonos_control/partials/spotify.html" %}`, `assertTemplateUsed(r, "vacation_mode/vacation_mode.html")`. Never add un-prefixed templates to an app; they collide across apps.
- **Side-effect-free `plugin.py`.** `<app>/plugin.py` may import only `hub.plugins` (and the stdlib). It must not import the app's models, views or homepage, `requests`, or anything that calls Home Assistant/HTTP or starts threads — specs are loaded while settings are still being built. Point hooks at code with dotted strings. `hub/tests/test_plugin_entrypoints.py` enforces this in a fresh interpreter.
- **No cross-app imports.** Apps import from `bdh_sdk` (e.g. `bdh_sdk.ha`, `bdh_sdk.settings`), `hub` and Django, not from each other. Shared helpers belong in `hub`.

### SDK, per-site apps and acceptance smoke test

- The contract (`PluginSpec`, `Tile`, `Status`, `Shortcut`, modes, sections) lives in **`bdh-sdk`** (github.com/blackdiamondhub/bdh-sdk), pinned in `requirements.txt` by GitHub release wheel URL. `hub/plugins.py` only re-exports it — change the contract in bdh-sdk, release it, then bump the pin. bdh-sdk also provides `bdh_sdk.settings.app_setting(app_id, key, legacy=...)` (reads a value saved on the Settings page, then `BDH_<ID>_<KEY>`, then legacy env names, then Django settings). Apps that want editable settings list `Setting(key, label, type=BOOL/INT/STR/CHOICE/TIME, ...)` in `PluginSpec.settings` (types from `bdh_sdk.plugins`) and read them with `app_setting`, the Home Assistant client `bdh_sdk.ha` and the reusable app CI workflow `app-ci.yml`.
- Apps from their own repos (`blackdiamondhub/bdh-app-<id>`) are installed per site from `sites/<site>/apps.txt` (hashed wheel URLs, `pip install --require-hashes --no-deps`); the image is built with `--build-arg HUB_SITE=<site>` and tagged `:<site>` (`:sunpeaks` and `:latest` today). An app's third-party deps must be in the hub's `requirements.txt`. When an app is extracted, remove it from `HUB_PLUGINS` in the same PR that adds it to the site's `apps.txt`. Hub CI installs `sites/sunpeaks/apps.txt` and runs `hub_smoke` before the unit tests; the app's CI (bdh-sdk `app-ci.yml`) deletes any in-tree copy from the hub checkout so it can be tested before the hub drops it.
- `python manage.py hub_smoke [--app <id>]` is the acceptance check: hook paths import, tile icons resolve via static finders, and every tile page renders (<500) for a signed-in superuser against a throwaway test DB. The reusable app CI runs it after installing the app's wheel next to the hub.
