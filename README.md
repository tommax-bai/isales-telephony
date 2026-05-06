# isales-telephony

`telephony-api` (FastAPI HTTP service) + `modem-controller` (asyncio daemon) for
the iSales platform. See the OpenSpec change `impl-telephony` in the
[`isales`](https://github.com/tommax-bai/isales) meta-repo for design and tasks.

## Processes

| binary               | role                                                          |
|----------------------|---------------------------------------------------------------|
| `telephony-api`      | HTTP API: `/devices` `/sim-cards` `/device-sim-bindings` CRUD + `POST /devices/select` |
| `modem-controller`   | Unix-socket IPC server + udev monitor + AT client (mock in v1) |

## Local development

```bash
# 1. install isales-common from local sibling (development snapshot of v0.1.2)
pip install -e ../isales-common

# 2. install this package + dev tools
pip install -e ".[dev]"

# 3. environment
export ISALES_DATABASE_URL=postgresql+asyncpg://bears@localhost:5432/isales_dev
export ISALES_JWT_SECRET=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')
export ISALES_REDIS_URL=redis://localhost:6379/0

# 4. run
telephony-api          # FastAPI on :8001
modem-controller       # IPC daemon (skip udev on macOS via ISALES_SKIP_UDEV=1)
```

## Tests

```bash
pytest -q
ruff check . && mypy isales_telephony
```
