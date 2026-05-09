# isales-telephony

`telephony-api` (FastAPI HTTP service) + `modem-controller` (asyncio daemon)
for the iSales platform. See the OpenSpec change `impl-telephony` in the
[`isales`](https://github.com/tommax-bai/isales) meta-repo for design and
tasks.

## Processes

| binary               | role                                                                            |
|----------------------|---------------------------------------------------------------------------------|
| `telephony-api`      | HTTP API: `/devices` `/sim-cards` `/device-sim-bindings` CRUD + `POST /devices/select` (internal) + `/health` |
| `modem-controller`   | Unix-socket IPC server + udev monitor + AT client (mock in v1 stage 2)          |

## Local development

```bash
# 1. install isales-common from the local sibling (development snapshot of v0.1.2)
pip install -e ../isales-common

# 2. install this package + dev tools + the platform-appropriate hardware extras.
#    Linux dev / production host:
pip install -e ".[dev,linux]"
#    macOS Apple Silicon dev / production host (impl-deploy-macos):
pip install -e ".[dev,macos]"

# 3. environment
export ISALES_DATABASE_URL=postgresql+asyncpg://bears@localhost:5432/isales_dev
export ISALES_JWT_SECRET=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')
export ISALES_REDIS_URL=redis://localhost:6379/0
# Legacy escape hatch (pre-impl-deploy-macos): set on macOS dev hosts to
# skip the USB watcher entirely. With the macos extras installed, you can
# now leave this unset and the IOKit watcher will start (impl-deploy-macos
# PR #3 lands the real IOKit subscription; until then it raises NotImplementedError).
export ISALES_SKIP_UDEV=1

# 4. run (two terminals)
telephony-api          # FastAPI on :8001
modem-controller       # IPC daemon (socket at /var/run/isales/modem.sock by default)
```

For the modem-controller socket on macOS:
```bash
export ISALES_MODEM_SOCKET=/tmp/isales-modem.sock
```

## Tests

```bash
# Local Postgres reachable as ISALES_TEST_DATABASE_URL (or ISALES_DATABASE_URL)
export ISALES_TEST_DATABASE_URL=postgresql+asyncpg://bears@localhost:5432/isales_telephony_test
pytest -q
ruff check . && mypy isales_telephony
```

Integration tests are skipped automatically when no Postgres is reachable.

## Production deployment

Linux + systemd, single host. Both processes run as the `isales` user.

```bash
sudo useradd --system --create-home isales
sudo usermod -aG plugdev,dialout isales   # for /dev/ttyUSB* access (stage 6)

sudo install -d -o isales -g isales /opt/isales
sudo -u isales python3.11 -m venv /opt/isales/venv
sudo -u isales /opt/isales/venv/bin/pip install \
    "isales-common @ git+https://github.com/tommax-bai/isales-common@v0.1.2" \
    isales-telephony

sudo install -m 0644 deploy/isales-telephony-api.service \
    /etc/systemd/system/isales-telephony-api.service
sudo install -m 0644 deploy/isales-modem-controller.service \
    /etc/systemd/system/isales-modem-controller.service

# DB / Redis / JWT secret overrides
sudo systemctl edit isales-telephony-api      # set ISALES_JWT_SECRET, real DB URL
sudo systemctl edit isales-modem-controller   # same DB URL

sudo systemctl daemon-reload
sudo systemctl enable --now isales-modem-controller isales-telephony-api

# Migration is run from isales-common (separate package install).
ISALES_DATABASE_URL=... /opt/isales/venv/bin/alembic -c \
    /opt/isales/venv/lib/python3.11/site-packages/isales_common/../alembic.ini \
    upgrade head
```

### Required environment

| variable                  | api  | modem | meaning                                              |
|---------------------------|------|-------|------------------------------------------------------|
| `ISALES_DATABASE_URL`     | yes  | yes   | PG asyncpg URL (`postgresql+asyncpg://...`)          |
| `ISALES_REDIS_URL`        | yes  | -     | Redis URL — currently used by future PRs             |
| `ISALES_JWT_SECRET`       | yes  | -     | HS256 secret shared with isales-api (signs)          |
| `ISALES_MODEM_SOCKET`     | -    | opt   | IPC socket path (default `/var/run/isales/modem.sock`) |
| `ISALES_SKIP_UDEV`        | -    | opt   | `1` to skip udev (dev/macOS only)                    |
| `MOCK_DIAL_DELAY_MS`      | -    | opt   | mock AT dial→connected delay (default 1000)          |
| `MOCK_CALL_DURATION_MS`   | -    | opt   | mock connected→remote_hangup delay (default 5000)    |

### Network policy

`telephony-api` binds to `127.0.0.1:8001` by default. `POST /devices/select`
is on an internal router with no JWT — it is reachable only on loopback.
External clients (the management UI in stage 7) MUST go through `isales-api`,
which is the only signing authority for JWT.

### Logs

```bash
journalctl -fu isales-telephony-api
journalctl -fu isales-modem-controller
```
