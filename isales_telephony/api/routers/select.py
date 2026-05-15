"""POST /devices/select — pick the longest-idle device for a campaign.

This route is on an *internal* router (no JWT) because it is called by
``isales-scheduler`` over a loopback / private network only. Per the
architecture spec, telephony-api SHOULD bind to ``127.0.0.1`` (or an internal
subnet); the systemd unit and deployment doc enforce that boundary.

**Deprecated (arch-cloud-edge-split task 7.7)**: scheduler no longer
calls this endpoint in the cloud-edge topology — it queries the cloud-
side PG directly with the same ``FOR UPDATE SKIP LOCKED`` algorithm
(see ``isales_scheduler/device_selection.py::pick_idle_device``, task
6.1). The endpoint stays for boss-console / CLI debugging on the edge,
and emits a ``Deprecation: 1`` response header per
RFC 8594 § 3 so any leftover callers surface in logs. Plan to remove
once C2 multi-tenant work lands.

Algorithm (PR #4 task 4.2): single transaction —

  SELECT device.id, sim_card.phone_number
    FROM device
    JOIN campaign_device ON campaign_device.device_id = device.id
    JOIN device_sim_binding ON device_sim_binding.device_id = device.id
                            AND device_sim_binding.is_active = true
    JOIN sim_card ON sim_card.id = device_sim_binding.sim_card_id
   WHERE campaign_device.campaign_id = :cid
     AND device.status = 'idle'
   ORDER BY device.last_call_at ASC NULLS FIRST
   LIMIT 1
   FOR UPDATE OF device SKIP LOCKED;

  -- followed by:
  UPDATE device SET last_call_at = now(), status = 'dialing' WHERE id = :picked_id;

`SKIP LOCKED` ensures concurrent callers pick *different* idle devices instead
of contending on the same row. NULLS FIRST preserves the spec's "never-called
devices go first" requirement.

Status flip note: tasks.md 4.2 originally said "only update last_call_at"; in
implementation we found that without flipping status, two concurrent /select
calls separated in time (so locks don't overlap) would pick the same row
(since status stays 'idle'). The contract is that scheduler MUST follow up
with a dial; setting status=dialing here makes /select the authoritative
"reservation" point and matches downstream PR #7 (dial → connected → idle).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status
from isales_common.enums import DeviceStatus
from isales_common.models import (
    CampaignDevice,
    Device,
    DeviceSimBinding,
    SimCard,
)
from isales_common.schemas.device import DeviceSelectRequest, DeviceSelectResponse
from sqlalchemy import select, update
from sqlalchemy.sql.functions import now as sql_now

from isales_telephony.api.deps import DBSession

# Internal router: no JWT dependency. Mounted at root; full path is
# POST /devices/select.
router = APIRouter(prefix="/devices", tags=["devices-internal"])


@router.post("/select", response_model=DeviceSelectResponse)
async def select_device(
    payload: DeviceSelectRequest,
    session: DBSession,
    response: Response,
) -> DeviceSelectResponse:
    stmt = (
        select(Device.id, SimCard.phone_number)
        .join(CampaignDevice, CampaignDevice.device_id == Device.id)
        .join(
            DeviceSimBinding,
            (DeviceSimBinding.device_id == Device.id)
            & (DeviceSimBinding.is_active.is_(True)),
        )
        .join(SimCard, SimCard.id == DeviceSimBinding.sim_card_id)
        .where(
            CampaignDevice.campaign_id == payload.campaign_id,
            Device.status == DeviceStatus.IDLE,
        )
        .order_by(Device.last_call_at.asc().nulls_first())
        .limit(1)
        .with_for_update(skip_locked=True, of=Device)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="no_idle_device",
        )
    device_id, phone_number = row
    if phone_number is None:
        # An active binding pointed at a SIM with no number — degenerate seed
        # data; surface as 503 rather than returning a half-formed response.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="no_idle_device",
        )
    await session.execute(
        update(Device)
        .where(Device.id == device_id)
        .values(last_call_at=sql_now(), status=DeviceStatus.DIALING)
    )
    # RFC 8594 § 3 deprecation signalling. Cloud-edge scheduler does
    # NOT call this any more — see module docstring. Anyone still
    # reaching this path (legacy single-host fallback / CLI debug)
    # will surface the header in their logs.
    response.headers["Deprecation"] = "1"
    return DeviceSelectResponse(device_id=device_id, phone_number=phone_number)
