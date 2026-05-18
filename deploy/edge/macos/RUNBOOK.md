# macOS dev / QA Edge RUNBOOK

> **Scope (READ FIRST).** This document describes the **macOS dev / QA
> real-ARTC edge form factor**, run by developers on their own Mac
> work machines to exercise the iSales state machine, AI pipeline,
> barge-in, and goal-partial logic against the real cloud engine
> (`121.89.85.150`) over a real Aliyun RTC channel. It is **not** a
> v1.0 MVP acceptance path and **not** a customer-deployed form factor.
> The sole commercial form factor is **Windows + real GSM modem +
> real ARTC**, anchored by `arch-cloud-edge-split` (cloud side) +
> `windows-artc-pybind11` (edge side). See
> `isales-telephony/deploy/edge/windows/STATE.md` for the commercial
> ground truth.

OpenSpec source: `openspec/changes/macos-artc-pyobjc-binding/`
(`device-hardware` + `deployment-topology` deltas).

---

## 1. Prerequisites

### 1.1 macOS host

- macOS 13 (Ventura) or newer. Apple Silicon and Intel both work —
  `AliRTCSdk.framework` ships as a universal Mach-O binary.
- Python 3.11+ in a virtualenv. The iSales meta-repo and the seven
  sub-repos must already be checked out side-by-side (`~/codes/isales`,
  `~/codes/isales-telephony`, etc.).

### 1.2 Aliyun ARTC macOS SDK (vendor)

Download `AliRTCSdk_7.8.10000-SNAPSHOT.zip` from Aliyun (see
`reference_artc_sdk.md` in the agent memory for the canonical URL —
the file is not checked into git) and unzip:

```bash
mkdir -p ~/codes/vendor/AliRTCSdk_macos
unzip -d ~/codes/vendor/AliRTCSdk_macos AliRTCSdk_7.8.10000-SNAPSHOT.zip
```

Expected contents (three frameworks):

```
~/codes/vendor/AliRTCSdk_macos/
├── AliRTCSdk.framework      ← main; Headers/, AliRTCSdk binary
├── UTDID.framework
└── PluginAAC.framework
```

Sanity check the architecture:

```bash
file ~/codes/vendor/AliRTCSdk_macos/AliRTCSdk.framework/AliRTCSdk
# expect: Mach-O universal binary with 2 architectures: [x86_64] [arm64]
```

The vendor directory is **gitignored** everywhere — both the meta-repo
and `isales-telephony` exclude `vendor/` from version control. Do not
copy the framework into the repository tree.

To use a custom location, export:

```bash
export ISALES_MACOS_ARTC_FRAMEWORK_PATH=/path/to/AliRTCSdk.framework
```

### 1.3 Python dependencies

In `~/codes/isales-telephony` (or wherever you cloned the sub-repo):

```bash
cd ~/codes/isales-telephony
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,macos,macos-artc]'
```

What each extras group covers:

| extras group | purpose |
|---|---|
| `dev` | pytest, ruff, mypy — required for the test suite |
| `macos` | `sounddevice` for the legacy `MacOSCoreAudioCapture` modem audio backend (unused by `--dev-no-modem` but harmless) |
| `macos-artc` | `pyobjc-core`, `pyobjc-framework-Cocoa` — the PyObjC bridge into `AliRTCSdk.framework` |

Validate the bridge can import:

```bash
python -c "import objc, Foundation; print('pyobjc ok')"
python -c "from isales_telephony.audio_bridge.macos_artc_pyobjc import MacosArtcPyObjCSession; print('bridge ok')"
```

The bridge does **not** load the framework on import — only on
`session.join()`. A missing framework will surface as an `RtcError`
the first time you dial.

### 1.4 Cloud-edge JWT

The edge daemon needs an edge-device JWT minted by the cloud-side
`isales-edge-token-mint` helper. See `deploy/cloud/STATE.md` § "Cloud-
edge gRPC end-to-end smoke" in the meta-repo for the canonical mint
procedure. The minted token goes into `ISALES_EDGE_DEVICE_TOKEN`.

---

## 2. One-line launch

The dev daemon reads gRPC endpoint + JWT from environment variables
(production parity); dev-specific labels come from CLI flags:

```bash
export ISALES_CLOUD_EDGE_ENDPOINT=121.89.85.150:50051
export ISALES_EDGE_DEVICE_TOKEN="$(cat ~/.isales/edge-dev.jwt)"

isales-telephony-edge \
  --dev-no-modem \
  --dev-channel demo-mac-01 \
  --dev-uid mac-dev-01 \
  --dev-peer-uid engine-mac-01
```

Behaviour on startup:

1. argparse validates `--dev-no-modem` is running on `sys.platform ==
   "darwin"`. Non-macOS hosts fail fast with a stderr message and
   exit code `2`.
2. `EdgeOrchestrator` is constructed with `dev_no_modem=True` — no
   `SerialATClient`, no USB watcher, no CPCMREG, no capture / playback
   pumps.
3. Cloud-edge gRPC stream comes up against the configured endpoint.
4. The orchestrator registers its `Cloud2Edge` callback and waits for
   a `DialCommand`.

The `--dev-channel` / `--dev-uid` / `--dev-peer-uid` flags are
**informational labels** logged for correlation; the actual
`rtc_channel` / `rtc_token` / `rtc_uid_edge` used for `join` come from
the cloud-supplied `DialCommand` (same wire shape as the Windows
commercial path).

---

## 3. Dev walkthrough (step-by-step)

The goal is to dial your own phone, hear the AI 开场白, interrupt
it, and confirm the engine logs every event correctly.

**3.1 Confirm edge ↔ cloud gRPC is up.**
In the edge daemon's stdout you should see a structured log line:

```
edge_daemon_started_dev_no_modem endpoint=121.89.85.150:50051 dev_channel=demo-mac-01 ...
```

Cross-check on the cloud side:

```bash
ssh root@121.89.85.150 'tail -n 20 /opt/isales/log/isales-telephony-api/cloud-edge.log'
# expect a "device_online ... device_id=<your dev device>" line within a few seconds
```

**3.2 Trigger an outbound call.**
Use the `isales-api` admin UI (or its CLI) to enqueue a call with
your own phone number against the lead-list bound to your dev device.
The cloud scheduler will send `Cloud2Edge.dial`; the edge emits
`DialAck(accepted=true)` immediately and `CallEvent(connected)` right
after (mic / speaker on mac are wired to the ARTC SDK's default audio
device — no modem PCM stage in dev mode).

**3.3 Listen + barge-in.**
Your phone rings → answer → you should hear the AI opening line on
the phone, and the engine's TTS audio on the mac speakers
simultaneously (the mac edge is just another RTC participant; the
real call leg lives between cloud engine and your phone via the
external SIP path). Wear headphones to avoid the mac mic capturing
the speaker TTS.

Interrupt the AI mid-sentence by speaking. Watch the engine log for:

```
barge_in_detected vad_active_ms=... policy=immediate_cancel
tts_cancel call_id=...
```

The VAD active duration before cancel + the round-trip from
detection to playback stop are the policy knobs that dev work tunes
in this form factor.

**3.4 Hang up.**
SIGINT the edge daemon (`Ctrl-C`). The dev path emits
`Edge2Cloud.remote_hangup{hangup_cause="dev_terminate"}` for every
in-flight call before `rtc_session.leave()` runs. The cloud worker
classifies `dev_terminate` as non-retryable, so your test call does
not get rescheduled.

---

## 4. Boundary with the Windows commercial form factor

| dimension | macOS dev / QA real-ARTC | Windows commercial |
|---|---|---|
| RTC stack | macOS ARTC SDK (SNAPSHOT) via PyObjC | Windows ARTC SDK (GA) via pybind11 |
| Modem | none (`--dev-no-modem`) | real SIM7600G-H GSM modem |
| Audio I/O | ARTC SDK default device (mac mic / speaker) | modem PCM ↔ SerialPcm-over-COM |
| Acceptance use | dev / QA only | sole v1.0 MVP acceptance path |

**Strategy / policy behaviour generalises**: barge-in trigger logic,
VAD thresholds, 垫词 cadence, handoff, goal-partial — these live in
the cloud engine and do not depend on the edge form factor, so dev
sessions on macOS validate them faithfully.

**Absolute latency numbers do NOT generalise.** The mac path skips
the modem 8 kHz ↔ RTC 16 kHz resampling and the USB-serial PCM
stage. Final acceptance latency (mic → engine → speaker P95) must be
re-measured on Windows + real modem before any spec claim.

OpenSpec scenarios that bound this contract live in
`deployment-topology` (§ "macOS dev/QA 形态走真 ARTC + 真 cloud
engine") and `device-hardware` (§ "dev / QA 形态边界").

---

## 5. Known macOS SDK behaviour differences (SNAPSHOT 7.8.10000)

Tracked as we hit them. Update this section in the same PR that
discovers a difference.

- *(none yet — populate after first real-channel dev sessions)*

The Linux Python wrapper (used by the cloud engine) is **7.10.2 GA**.
Expect minor selector-name drift and possibly different error codes;
the bridge tolerates both `pushExternalAudioFrame_` and
`pushExternalAudioFrameRawData_sampleRate_channels_timestamp_`
spellings to absorb that.

---

## 6. Troubleshooting

### 6.1 `RtcError: AliRTCSdk framework not found at ...`

The framework is not where the bridge expected. Either unzip the SDK
to `~/codes/vendor/AliRTCSdk_macos/` or export
`ISALES_MACOS_ARTC_FRAMEWORK_PATH` to point at it. The path **must**
be the `AliRTCSdk.framework` directory, not its parent.

### 6.2 `RtcError: Framework binary missing: .../AliRTCSdk`

You unzipped only part of the SDK (probably just the `Headers/`
folder). Re-extract the full zip — the framework binary is required.

### 6.3 `RtcError: objc.loadBundle failed for ...: ...`

Usually means a Mach-O architecture mismatch. Confirm
`file <framework>/AliRTCSdk` reports `universal binary`. If you are
on Apple Silicon and only see `x86_64`, you have an old SDK build —
ask Aliyun for the universal SNAPSHOT.

### 6.4 `ImportError: pyobjc-core / pyobjc-framework-Cocoa not installed`

You missed the `[macos-artc]` extras group:
`pip install -e '.[dev,macos,macos-artc]'`.

### 6.5 WARN log: `macos_artc_pyobjc_unavailable_fallback_to_mock`

The platform router fell back to `MacosRtcSession` in-process
loopback. Some prereq is missing — re-read § 1.2 / § 1.3. The fallback
keeps the import path working for unit-tests; it is **not** a real
RTC session, so `--dev-no-modem` joined to that fallback will not
talk to the cloud engine.

### 6.6 Falling back to the in-process mock on purpose

If you want to exercise the orchestrator without the real SDK
(e.g. on a host with no Aliyun account), just drop the `--dev-no-modem`
flag — the existing CI / unit-test path uses `MacosRtcSession`
directly.

### 6.7 `--dev-no-modem 仅 macOS 支持` on startup

You ran the dev path on Linux or Windows. Windows commercial work
uses `deploy/edge/windows/build.ps1` to build the pybind11 binding;
Linux is not a supported edge platform in v1.0.
