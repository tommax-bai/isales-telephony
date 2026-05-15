"""Edge daemon: single-process orchestration of modem + RTC + cloud-edge.

Spec: arch-cloud-edge-split / device-hardware § Requirement: audio-bridge
组件 + service-communication § "云-边控制面" — the edge side of the cloud-
edge split runs as ONE asyncio process that hosts:

- ``modem_controller`` (existing) — AT command channel + USB modem PCM.
- ``audio_bridge`` (existing) — modem PCM ↔ Aliyun RTC.
- ``transport.grpc_client`` (existing) — cloud-edge bidi.
- ``api`` (existing, optional) — loopback-only HTTP for boss-console
  device queries; **NOT** a control-plane entry point any more.

The pieces above are constructed in isolation by their own packages.
This ``edge`` package is the glue that wires them together at process
start, owns the per-call lifecycle, and handles SIGTERM. It does NOT
re-implement any of the building blocks.

Public surface:

- :class:`EdgeOrchestrator` — receives Cloud2Edge frames + drives the
  per-call audio path (AT dial → AudioBridge join → events upstream).
- :func:`isales_telephony.edge.main.run` — the entry point used by the
  ``isales-telephony-edge`` console script and ``python -m
  isales_telephony``.
"""

from __future__ import annotations

from isales_telephony.edge.orchestrator import EdgeOrchestrator

__all__ = ["EdgeOrchestrator"]
