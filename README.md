# BlackDiamondHub

BlackDiamondHub is a Django-based web application designed to manage inventory, feedback, and sunpeaks webcams.

## Features

- Inventory management
- Feedback system
- Integration with Sunpeaks webcams
- User authentication and authorization

## Requirements

- Python 3.8+
- PostgreSQL
- Google Chrome and ChromeDriver
- libzbar0 (for barcode scanning)
- libpq-dev

## Installation

1. Clone the repository:

    ```sh
    git clone https://github.com/yourusername/BlackDiamondHub.git
    cd BlackDiamondHub
    ```

2. Create and activate a virtual environment:

    ```sh
    python -m venv venv
    source venv/bin/activate  # On Windows use `venv\Scripts\activate`
    ```

3. Install the required Python packages:

    ```sh
    pip install --upgrade pip
    pip install -r requirements.txt
    ```

4. Set up the environment variables. You can create a `.env` file in the root directory of the project with the following content:

    ```env
    DJANGO_SECRET_KEY=any-long-random-string
    DJANGO_DEBUG=true
    ALLOWED_HOSTS="Extra hosts to serve, e.g. the hub's IP or public name (local DNS names like bdh.bdl and *.local are always allowed)"
    DB_HOST=your_db_host
    DB_NAME=your_db_name
    DB_USER=your_db_user
    DB_PASSWORD=your_db_password
    # Optional: table-owner login for the container's startup migrations.
    # When set, DB_USER only needs SELECT/INSERT/UPDATE/DELETE.
    DB_MIGRATE_USER=your_db_owner
    DB_MIGRATE_PASSWORD=your_db_owner_password
    SPOTIFY_CLIENT_ID=your_spotify_client_id
    SPOTIFY_CLIENT_SECRET=your_spotify_client_secret
    ```

5. Apply the database migrations:

    ```sh
    python manage.py migrate
    ```

6. Create a superuser:

    ```sh
    python manage.py createsuperuser --username $ADMIN_USER --email admin@example.com
    ```

7. Run the development server:

    ```sh
    python manage.py runserver
    ```

8. Access the application at `http://127.0.0.1:8000/`.

## Sites

The same code runs at more than one house. Everything that differs lives in
`sites/<site>/`, and CI publishes one image per site:

| Site | Folder | Image tag |
|---|---|---|
| Sun Peaks (Black Diamond Lodge) | `sites/sunpeaks/` | `:latest` (also `:sunpeaks`, `:sha-<commit>`) |
| Mercer Island (Slivinski Hub) | `sites/mercerisland/` | `:mercerisland` (also `:sha-<commit>-mercerisland`) |

Each site folder has:

- `site.toml`: `[brand]` name, subtitle and monogram SVGs (the "BDH" monogram
  from `bdh-sdk` when none is given), `[features]`
  such as `feedback` (`false` hides the built-in feedback
  pill and inbox; see "Feedback" below), `[sections]` homepage section titles (`mountain`,
  `lodge` and `more`; "Outside", "Inside" and "More" unless set; staff can
  rename, add, remove and reorder groups and drag apps between them on
  Settings → Home layout), `[store]` settings for Settings → Apps
  (`memory_budget_mb`, and `channel`: see "Release channels" below), and
  `[apps.<app_id>]` tables of per-site app
  settings. Each key becomes the Django setting `BDH_<APP_ID>_<KEY>`, read by
  the app with `bdh_sdk.settings.app_setting` (a value on the Settings page or
  in the environment wins). For example `[apps.speakers]` sets which
  speakers, rooms and presets the music app shows. `runtime = "container"` in
  an app's table stops the hub loading it in-process, so it can be installed
  as a container app instead (see below). Container apps get the rest of the
  table through the broker's `settings.get`, after a value saved on the
  Settings page; they never see the hub's environment.
- `apps.txt`: the hashed app wheels baked into that site's image.

The image is built with `--build-arg HUB_SITE=<site>`, which also sets
`HUB_SITE` at runtime. On a server, pick the site's image with
`HUB_IMAGE_TAG=<tag>` in `.env` (unset is `:latest`, Sun Peaks).

To add a site: create `sites/<site>/site.toml` and `apps.txt`, then add it to
the matrices in `.github/workflows/docker-publish.yml` and `tests.yml`.

### Events

Apps subscribe to events with `bdh_sdk.events` (MQTT-style topics, `+` and `#`
wildcards). The hub feeds that bus from the sources in `[events.sources.<name>]`,
and every topic starts with the source name:

- `type = "home_assistant"`: one websocket to Home Assistant, watching only the
  entities that apps subscribe to. Publishes `<name>/state/<entity_id>` with
  `{entity_id, old, new, attributes}`. Uses `HOMEASSISTANT_URL` and
  `HOMEASSISTANT_ACCESS_TOKEN`; set `url_env`/`token_env` to read a second
  instance from other variables.
- `type = "webhook"`: `POST /hub/api/events/hook/<name>/<topic>` with a JSON
  body and an `X-Hub-Secret` header matching the variable named by
  `secret_env`. Publishes `<name>/<topic>`.

Set `HUB_EVENTS=off` to start no sources.

### Container apps

Apps built for hub API 2 run in their own containers, managed by
[bdh-supervisor](https://github.com/blackdiamondlodge/bdh-supervisor) (a
compose service; the hub reaches it through `BDH_SUPERVISOR_SOCKET` with
`BDH_SUPERVISOR_TOKEN`). They are installed from the
[app catalog](https://github.com/blackdiamondlodge/bdh-app-catalog)
(`BDH_CATALOG_URL`, cached under `media/.catalog` and refreshed hourly), and
the hub proxies each one on its own port (9100 + n), starting it on the first
request and keeping it awake while it is used.

```sh
python manage.py catalog_refresh                     # fetch the catalog, list what can be installed
python manage.py app_install <id> [--version X] [--network none|outbound|lan] [--allow-lan]
python manage.py app_install <id> --approve          # update to a version that asks for more
python manage.py app_status                          # the hub's and the supervisor's view
python manage.py app_uninstall <id> [--purge]        # --purge deletes the app's data
```

#### Release channels

An app version is a **beta** when its version is a PEP 440 pre-release
(`2.3.0b1`, `2.3.0rc1`): its repo publishes it as a GitHub pre-release and the
catalog lists it with `"channel": "beta"`. Promoting it (the catalog's
`promote.yml`) flips that entry to `stable`, same image, no rebuild.

Each hub follows one channel, `[store] channel` in its `site.toml`:

- `stable` (the default): beta versions are invisible: the store
  doesn't list them, they can't be installed and they're never offered as
  updates.
- `beta` (both sites): stable and beta versions alike; the newest by PEP 440
  wins, and the store marks betas with a "Beta" badge.

So the release flow is: release `X.Y.ZbN`, let Mercer Island run it, then
promote it (or release `X.Y.Z`). A hub switched from beta back to stable keeps
the beta it has (it's never downgraded) and updates to the next stable version
newer than it.

### Feedback

The Feedback pill and modal in the top bar, and the owner menu's inbox link
and unread badge, come from `hub/feedback.py`. They use the
[feedback container app](https://github.com/blackdiamondlodge/bdh-app-feedback)
when it is installed: the hub posts each message to the app (adding the page
and the stay in progress), the inbox is the app's own page, and the badge
shows the unread count the app keeps in its app state. Otherwise they use the
built-in `feedback` app, unless the site sets `feedback = false` under
`[features]`; with neither, they are hidden.

To move a site from the built-in app to the container app, export the
messages while the built-in app still runs, then set `runtime = "container"`
under `[apps.feedback]` in the site's `site.toml`, deploy, install the app
(`app_install feedback`) and import them:

```sh
# on the hub, before the switch
sudo docker compose exec djangoapp .venv/bin/python -m bdh_sdk.appdata export feedback -o /app/media/feedback.json
# copy the file into the app's data volume, then in the app's container
python -m bdh_sdk.runtime.manage import_app_data /data/feedback.json
```

The app sends the hub its new unread count as the rows are imported.

### Secrets

App passwords, API keys and tokens live in an encrypted secret store
(`hub_settings/vault.py`, table `hub_settings_appsecret`), not in plain
`AppSetting` rows. Each value is encrypted with AES-256-GCM and bound to its
app and name (associated data `app|name`), so a ciphertext copied to another
row fails to decrypt. Each secret belongs to one app: apps read and write
their own through `bdh_sdk.secrets` (`get`, `set`, `delete`), in-process
straight from the database and in a container over the broker
(`secrets.get/set/delete`, which always use the app id from the token).
Broker calls are rate limited and audited with the app, the name and the
operation only.

Settings declared `type=SECRET` are write-only on the Settings page: it shows
"set" or "not set" with Replace and Clear and never renders the value. Secrets
are not in `AppSetting`, the app's `appdata` export or the Django admin, and
values are kept out of logs, audit details and error pages. Database backups
(`pg_dump`) hold only ciphertext.

The key is **not** in the database. It is read from `BDH_SECRETS_KEY_FILE`
(default `/etc/bdh/secrets.key`; the compose file mounts the host's
`/etc/bdh` read-only). The file has one `<key id>:<base64 32-byte key>` line
per key, and the last is current; every ciphertext records its key id, so old
keys keep working until rotated away. Without the file the hub runs normally,
but secrets can't be read or saved. Back the key up: without it the stored
secrets are lost. Key loading sits behind `vault.KeyProvider` so it can move
to a vTPM or unlock-at-boot later.

```sh
# first time, on the hub server (refuses to overwrite an existing key)
sudo install -d -m 0700 /etc/bdh
sudo docker compose run --rm --no-deps -v /etc/bdh:/keys djangoapp \
    .venv/bin/python manage.py generate_secrets_key --path /keys/secrets.key
# move plain-text passwords/API keys saved in Settings into the store
sudo docker compose exec djangoapp .venv/bin/python manage.py migrate_plain_secrets --dry-run
sudo docker compose exec djangoapp .venv/bin/python manage.py migrate_plain_secrets
# rotate: add a new current key, re-encrypt, then delete the old line from the file
sudo docker compose run --rm --no-deps -v /etc/bdh:/keys djangoapp \
    .venv/bin/python manage.py generate_secrets_key --path /keys/secrets.key --append
sudo docker compose exec djangoapp .venv/bin/python manage.py rotate_secrets_key
```

`migrate_plain_secrets` moves the settings listed in `vault.LEGACY_PLAIN`
(cameras `protect_password_n`/`protect_api_key_n`, device_control
`gemstone_password`, restaurants `google_places_api_key`, shuttle
`reminders_api_key`, vacation_mode `modes_api_key`) and deletes the plain
rows; it is safe to run again. Apps that still read those with `app_setting`
keep working, since the settings store finds them in the secret store, and
the Settings page already treats them as secrets. Usernames (cameras
`protect_username_n`) stay plain: alone they unlock nothing. The wifi app's
`password_n` stays plain on purpose; it is shown to guests.

Limits: anyone with root on the hub VM (or a shell in the hub container) can
read the key and so every secret, and an app can leak a secret it has been
given. The hub's own credentials (`DJANGO_SECRET_KEY`, `DB_PASSWORD`,
`HOMEASSISTANT_ACCESS_TOKEN`, `HA_TOKEN`, `NAPOLEON_*`, `GEMSTONE_PASSWORD`,
`UNIFI_PROTECT_API_KEY_n`, `BDH_CAMERAS_PROTECT_PASSWORD_n`,
`BDL_MODES_API_KEY`, `GOOGLE_PLACES_API_KEY`, `BDH_SHUTTLE_REMINDERS_API_KEY`,
`SPOTIFY_CLIENT_SECRET`, `HUB_REMOTE_TOKEN`, `BDH_SUPERVISOR_TOKEN`,
`WIFI_PASSWORD_FOR_*`, ...) still come from `.env`; environment values remain
the fallback when nothing is saved.

### Shared data between apps

A container app can publish a named share that it declares in its manifest
(`shares`: name, type, title; `text/calendar` today) and read shares the
admin granted it (`reads` lists the types it understands). Apps upload
content-addressed blobs with `shares.missing` / `shares.put_blob`, publish a
revision atomically with `shares.publish`, and readers use `shares.list` /
`shares.open`. Blobs live in the database (`broker_shareblob`, 50 MB per
provider); the latest two revisions are kept and unreferenced blobs older
than an hour are collected after each publish and hourly (`shares_gc`).
Image attachments of a kept revision are public at `/apps/_share/<sha256>`.
Each new revision is delivered as the event `_shares/<provider>/<name>` to the
provider and its granted readers only; apps can't publish `_` topics. Grants
are made on the provider's Settings → Apps page (Sharing card) and are
dropped when either app is uninstalled.
## Deploying a hub server

A hub server is an Ubuntu VM running the compose stack (hub,
bdh-supervisor, Watchtower) against a PostgreSQL server elsewhere on the LAN.

1. Database, on the Postgres host (creates the database plus a `hub_admin`
   owner login used only for migrations and a `hub_app` login the site runs
   as, with row access only):

    ```sh
    sudo -u postgres env HUB_ADMIN_PASSWORD=... HUB_APP_PASSWORD=... \
        bash deploy/create_db.sh homehub
    ```

    Then allow the hub server in `pg_hba.conf`.

2. Server:

    ```sh
    git clone https://github.com/blackdiamondlodge/BlackDiamondHub.git
    cd BlackDiamondHub && sudo deploy/bootstrap.sh
    ```

    The first run installs Docker, grows the root disk, copies `.env.example`
    to `.env` and sets `BDH_GO2RTC_CANDIDATES` to this host's IP (the WebRTC
    address of the cameras app's go2rtc companion). Fill in `.env` (`HUB_IMAGE_TAG`, `DB_USER=hub_app`,
    `DB_MIGRATE_USER=hub_admin`, ...) and run the script again to start the
    stack. Re-run it after changing `.env` or `docker-compose.yml`; Watchtower
    only picks up new images.

    Watchtower runs in label mode (`WATCHTOWER_LABEL_ENABLE=true`) and updates
    only the containers labelled `com.centurylinklabs.watchtower.enable=true`
    in `docker-compose.yml`: the hub and bdh-supervisor. App containers and
    their companions (e.g. go2rtc) are never updated by Watchtower. Each app is its own image,
    pinned by digest in the catalog; the supervisor pulls, verifies and
    upgrades them (it notices new digests on its own catalog poll, or
    immediately with `POST /catalog/refresh`). To switch an existing hub from
    the old name list, `git pull` and re-run `sudo deploy/bootstrap.sh`.

    Offline: running apps keep running and restart from their local images
    without internet. Installs and upgrades need ghcr.io (the app images are
    public, so no registry credentials are needed) and the catalog.

3. Admin login (admins see settings and admin-only app features):

    ```sh
    sudo docker compose exec djangoapp .venv/bin/python manage.py createsuperuser
    ```

## Running Tests

To run the tests, use the following command:

```sh
python manage.py test -v 2