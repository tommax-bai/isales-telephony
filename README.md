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

| variable                    | api  | modem | meaning                                              |
|-----------------------------|------|-------|------------------------------------------------------|
| `ISALES_DATABASE_URL`       | yes  | yes   | PG asyncpg URL (`postgresql+asyncpg://...`)          |
| `ISALES_REDIS_URL`          | yes  | -     | Redis URL — currently used by future PRs             |
| `ISALES_JWT_SECRET`         | yes  | -     | HS256 secret shared with isales-api (signs)          |
| `ISALES_MODEM_SOCKET`       | -    | opt   | IPC socket path (default `/var/run/isales/modem.sock`) |
| `ISALES_MODEM_SERIAL_PATH`  | -    | **req** in prod | tty path of the GSM modem (e.g. `/dev/ttyUSB-isales-modem`, `/dev/cu.usbmodem21301`); if unset, modem-controller refuses to start unless `ISALES_ALLOW_MOCK_AT=1` |
| `ISALES_MODEM_DRIVER`       | -    | opt   | driver hint `a7670` / `sim800c` / `quectel_uc20`; empty → auto-detect via `AT+GMI`/`AT+GMM` |
| `ISALES_ALLOW_MOCK_AT`      | -    | opt   | `1` → fall back to MockATClient when `ISALES_MODEM_SERIAL_PATH` unset; **CI / dev only**, never production |
| `ISALES_SKIP_UDEV`          | -    | opt   | `1` to skip udev (dev/macOS only)                    |
| `MOCK_DIAL_DELAY_MS`        | -    | opt   | mock AT dial→connected delay (default 1000)          |
| `MOCK_CALL_DURATION_MS`     | -    | opt   | mock connected→remote_hangup delay (default 5000)    |

## Real hardware smoke test

`scripts/at_smoke.py` exercises a single dial → connect → hangup cycle
against a real GSM modem without booting the full IPC server / database.
Use it after every fresh edge deployment, after replacing a modem, or after
a release that touches `at_client.py` / `drivers.py` / `serial_protocol.py`.

**Linux:**

```bash
# Discover the tty (udev rule should create the symlink already)
ls /dev/ttyUSB-isales-modem /dev/ttyUSB[0-9]

# Dial 13800138000, hold for 5s after connect, then hang up
.venv/bin/python scripts/at_smoke.py \
    --tty /dev/ttyUSB-isales-modem --number 13800138000
```

**macOS:**

```bash
# Discover the tty after plugging the modem
ls /dev/cu.usbmodem*

# Dial; supply --driver if auto-detect fails (e.g. SIM800C / Quectel firmware
# that doesn't answer AT+GMI/AT+GMM cleanly)
.venv/bin/python scripts/at_smoke.py \
    --tty /dev/cu.usbmodem21301 --number 13800138000 --driver a7670
```

Expected output is a chronological event log:

```
[  0.00s] opening /dev/cu.usbmodem21301 (driver_hint=<auto>)
[  0.42s] modem ready; calling dial('13800138000')
[  0.45s] dial accepted; call_id=ab12...; polling for connect
[  3.12s] EVENT connected call_id=ab12... cause=None
[  3.12s] connected; sleeping 5.0s then hanging up
[  8.13s] calling client.hangup(ab12...)
[  8.18s] EVENT remote_hangup call_id=ab12... cause='local_clearing'
[  8.18s] DONE connected=True hangup_cause=local_clearing
```

Exit 0 means the full sequence completed. Non-zero (with traceback) means
the dial failed before connect, the modem returned an unexpected URC, or
the tty was already locked by another process (typically a still-running
modem-controller — stop it first).

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
