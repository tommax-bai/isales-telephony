# macOS dev / QA Edge RUNBOOK

> **Scope (READ FIRST).** This document describes the **macOS dev / QA
> real-DingRTC edge form factor**, run by developers on their own Mac
> work machines to exercise the iSales state machine, AI pipeline,
> barge-in, and goal-partial logic against the real cloud engine
> (`121.89.85.150`) over a real Aliyun RTC PaaS (DingRTC) channel. It
> is **not** a v1.0 MVP acceptance path and **not** a customer-deployed
> form factor. The sole commercial form factor is **Windows + real GSM
> modem + real DingRTC**, anchored by `arch-cloud-edge-split` (cloud
> side) + `windows-artc-pybind11` (edge side, renamed to DingRTC vendor
> layer in `engine-rtc-dingrtc-migration § 7`). See
> `isales-telephony/deploy/edge/windows/STATE.md` for the commercial
> ground truth.

OpenSpec source: `openspec/changes/engine-rtc-dingrtc-migration` § 8
(macOS PyObjC binding vendor re-target to `DingRTC.framework`) +
`openspec/changes/macos-artc-pyobjc-binding` (archived precursor that
defined the binding shape against the legacy AliRTC SDK).

---

## 1. Prerequisites

### 1.1 macOS host

- macOS 13 (Ventura) or newer. Apple Silicon and Intel both work —
  `DingRTC.framework` ships as a universal Mach-O binary (x86_64 +
  arm64).
- Python 3.11+ in a virtualenv. The iSales meta-repo and the seven
  sub-repos must already be checked out side-by-side (`~/codes/isales`,
  `~/codes/isales-telephony`, etc.).

### 1.2 Aliyun DingRTC macOS SDK (vendor)

Download `DingRTC_macOS_SDK_3_9_0.zip` from the vendor OSS bucket
(the file is not checked into git) and unzip:

```bash
mkdir -p ~/codes/vendor
cd ~/codes/vendor
curl -fLO https://dingrtc.oss-cn-zhangjiakou.aliyuncs.com/sdk/mac/3.9.0/DingRTC_macOS_SDK_3_9_0.zip
shasum -a 256 DingRTC_macOS_SDK_3_9_0.zip
# expect: 197337f2bc2ff2476abc988672cf5b20b381f5e12e9069f37dbd3fd157f7020e
unzip -q DingRTC_macOS_SDK_3_9_0.zip -d DingRTC_macOS_SDK_3_9_0
```

Expected contents (one main framework + vendor sibling deps):

```
~/codes/vendor/DingRTC_macOS_SDK_3_9_0/
├── DingRTC.framework         ← main; Headers/, Modules/, Versions/, DingRTC binary
├── libffmpeg.dylib           ← pre-loaded via ctypes.CDLL by the bridge loader
├── libhbal_se.dylib
├── DingBeauty.framework
├── libSR.framework
├── MoziWhiteboard.framework
└── model/
```

Only `DingRTC.framework` + `libffmpeg.dylib` are needed for v1.0 audio
flow; the bridge loader pre-loads `libffmpeg.dylib` so the framework's
`@rpath` deps resolve without `DYLD_*` env vars. The other frameworks
(`DingBeauty`, `libSR`, `MoziWhiteboard`) are video / whiteboard / SR
features the cloud-edge MVP does not exercise.

Sanity check the architecture:

```bash
file ~/codes/vendor/DingRTC_macOS_SDK_3_9_0/DingRTC.framework/DingRTC
# expect: Mach-O universal binary with 2 architectures: [x86_64] [arm64]
```

The vendor directory is **gitignored** everywhere — both the meta-repo
and `isales-telephony` exclude `vendor/` from version control. Do not
copy the framework into the repository tree.

To use a custom location, export:

```bash
export ISALES_MACOS_DINGRTC_FRAMEWORK_PATH=/path/to/DingRTC.framework
```

The legacy `ISALES_MACOS_ARTC_FRAMEWORK_PATH` env var is honoured one
release post-dingrtc-migration with a WARN log; remove from your
profile and switch to the new name when you can.

### 1.3 Python dependencies

In `~/codes/isales-telephony` (or wherever you cloned the sub-repo):

```bash
cd ~/codes/isales-telephony
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,macos,macos-dingrtc]'
```

What each extras group covers:

| extras group | purpose |
|---|---|
| `dev` | pytest, ruff, mypy — required for the test suite |
| `macos` | `sounddevice` for the legacy `MacOSCoreAudioCapture` modem audio backend (unused by `--dev-no-modem` but harmless) |
| `macos-dingrtc` | `pyobjc-core`, `pyobjc-framework-Cocoa` — the PyObjC bridge into `DingRTC.framework` |

The deprecated `[macos-artc]` extras key resolves to the same pyobjc
deps for one release; new installs SHOULD use `macos-dingrtc`.

Validate the bridge can import:

```bash
python -c "import objc, Foundation; print('pyobjc ok')"
python -c "from isales_telephony.audio_bridge.macos_dingrtc_pyobjc import MacosDingRtcPyObjCSession; print('bridge ok')"
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
after (mic / speaker on mac are pumped via the audio_bridge external-
audio-source path — DingRTC takes PCM through
`push_audio` / `audio_frames`, not the SDK's internal Core Audio
capture).

**3.3 Listen + barge-in.**
Your phone rings → answer → you should hear the AI opening line on
the phone, and the engine's TTS audio on the mac speakers
simultaneously (the mac edge is just another DingRTC participant; the
real call leg lives between cloud engine and your phone via the
external SIP path). Wear headphones to avoid the mac mic capturing
the speaker TTS.

Interrupt the AI mid-sentence by speaking. **Barge-in log markers
(current, 2026-06-05)** — VAD-driven cancel is **disabled** (it
false-triggered on ambient noise, 2026-06-03); barge-in now runs off
ASR partial text length via `evaluate_partial`. Watch the engine log
for:

```
# (NOT the old `barge_in_detected` / `tts_cancel` strings — those are gone)
isales_engine.run_loop vad_monitor_cancel_disabled_prototype ...   # VAD path is off
isales_engine.realtime.interruption_detector evaluate_partial ... triggered=true
isales_engine ... CallStatus.INTERRUPTED reason="speaking_interrupted"
```

Barge-in only fires when you talk **over** the AI mid-sentence (the
listen pumps start *after* the greeting, so the greeting itself
cannot be barged in on — see `run_loop.py` greeting note). Clean
turn-taking (waiting for the AI to finish) will **never** produce an
`INTERRUPTED` marker. See § 7 for the repeatable test + how to read
the result.

**3.4 Hang up.**
SIGINT the edge daemon (`Ctrl-C`). The dev path emits
`Edge2Cloud.remote_hangup{hangup_cause="dev_terminate"}` for every
in-flight call before `rtc_session.leave()` runs. The cloud worker
classifies `dev_terminate` as non-retryable, so your test call does
not get rescheduled.

---

## 4. Boundary with the Windows commercial form factor

| dimension | macOS dev / QA real-DingRTC | Windows commercial |
|---|---|---|
| RTC stack | DingRTC macOS SDK 3.9.0 via PyObjC | DingRTC Windows SDK 3.9.0 via pybind11 |
| Modem | none (`--dev-no-modem`) | real SIM7600G-H GSM modem |
| Audio I/O | audio_bridge → DingRTC external-audio-source (mac mic / speaker via PortAudio) | modem PCM ↔ SerialPcm-over-COM ↔ audio_bridge → DingRTC |
| Acceptance use | dev / QA only | sole v1.0 MVP acceptance path |

**Strategy / policy behaviour generalises**: barge-in trigger logic,
VAD thresholds, 垫词 cadence, handoff, goal-partial — these live in
the cloud engine and do not depend on the edge form factor, so dev
sessions on macOS validate them faithfully.

**Absolute latency numbers do NOT generalise.** The mac path skips
the modem 8 kHz ↔ DingRTC 16 kHz resampling and the USB-serial PCM
stage. Final acceptance latency (mic → engine → speaker P95) must be
re-measured on Windows + real modem before any spec claim.

OpenSpec scenarios that bound this contract live in
`deployment-topology` (§ "macOS dev/QA 形态走真 DingRTC + 真 cloud
engine") and `device-hardware` (§ "dev / QA 形态边界").

---

## 5. Known macOS SDK behaviour differences (DingRTC 3.9.0)

Tracked as we hit them. Update this section in the same PR that
discovers a difference.

- **PyObjC delegate ABI MUST be annotated explicitly.** Every selector
  the SDK invokes from Objective-C with a non-`@` argument (e.g.
  `onJoinChannelResult:channel:userId:elapsed:` with `int / NSString *
  / NSString * / int`) needs an explicit
  `objc.selector(callable, signature=b'v@:i@@i')` wrap. PyObjC's
  default auto-detection treats every arg as `id` and the SDK's ARM64
  calling convention then dereferences integer-register values as
  pointers → SIGSEGV. See `macos_dingrtc_pyobjc.py` `_typed_selector`
  helper for the canonical pattern.
- **Best-effort protocol attachment via `__pyobjc_protocols__` does
  NOT replace explicit signatures.** PyObjC binds method-type-encoding
  metadata at class creation; post-hoc protocol attachment cannot
  retroactively rewrite it. Keep the explicit signatures.
- **App Transport Security blocks `http://` httpdns probes but the
  main `https://gslb.dingrtc.com` path succeeds independently.** The
  SDK logs `~10` `NSURLErrorDomain Code=-1022` warnings per join for
  httpdns retries against `203.x.x.x` / `resolvers-cn.httpdns.aliyuncs.com`;
  these are harmless. Do NOT add a process-wide
  `NSAllowsArbitraryLoads` — it is not required and increases attack
  surface.
- **`Uploader::OnError: Access denied by authorizer's policy`** at
  leave time is the SDK's internal log uploader complaining that the
  OSS bucket policy is not configured for this AppId. No effect on
  call quality; ignore for dev / QA.

Cloud engine (Linux) uses the same DingRTC 3.9.0 vendor binary
(different binding: pybind11 in `isales-engine`, PyObjC here), so
selector / API drift between platforms is minimal — all three edges
target the same SDK release.

---

## 6. Troubleshooting

### 6.1 `RtcError: DingRTC framework not found at ...`

The framework is not where the bridge expected. Either unzip the SDK
to `~/codes/vendor/DingRTC_macOS_SDK_3_9_0/` or export
`ISALES_MACOS_DINGRTC_FRAMEWORK_PATH` to point at it. The path **must**
be the `DingRTC.framework` directory, not its parent.

### 6.2 `RtcError: Framework binary missing: .../DingRTC`

You unzipped only part of the SDK (probably just the `Headers/`
folder). Re-extract the full zip — the framework binary is required.

### 6.3 `RtcError: ctypes.CDLL failed for .../DingRTC: ...`

Usually means a Mach-O architecture mismatch or a missing sibling
dep. Confirm `file <framework>/DingRTC` reports `universal binary`. If
the error mentions `@rpath/libffmpeg.dylib not found`, the vendor's
`libffmpeg.dylib` is missing from `~/codes/vendor/DingRTC_macOS_SDK_3_9_0/`
— re-extract the zip.

### 6.4 `ImportError: pyobjc-core / pyobjc-framework-Cocoa not installed`

You missed the `[macos-dingrtc]` extras group:
`pip install -e '.[dev,macos,macos-dingrtc]'`. (The deprecated
`[macos-artc]` key also works for one release.)

### 6.5 WARN log: `macos_dingrtc_pyobjc_unavailable_fallback_to_mock`

The platform router fell back to `MacosRtcSession` in-process
loopback. Some prereq is missing — re-read § 1.2 / § 1.3. The fallback
keeps the import path working for unit-tests; it is **not** a real
DingRTC session, so `--dev-no-modem` joined to that fallback will not
talk to the cloud engine.

### 6.6 SIGSEGV during `session.join()`

You are running against a pre-`eb557da` build of the bridge that did
not annotate delegate selectors with explicit `objc.selector`
signatures. Pull `dingrtc-migration-macos` (or `main` post-archive)
and reinstall the package — § 5 explains the root cause.

### 6.7 Falling back to the in-process mock on purpose

If you want to exercise the orchestrator without the real SDK
(e.g. on a host with no Aliyun account), just drop the `--dev-no-modem`
flag — the existing CI / unit-test path uses `MacosRtcSession`
directly.

### 6.8 `--dev-no-modem 仅 macOS 支持` on startup

You ran the dev path on Linux or Windows. Windows commercial work
uses `deploy/edge/windows/build.ps1` to build the pybind11 binding;
Linux is not a supported edge platform in v1.0.

---

## 7. Repeatable e2e smoke test (start here — automated fast path)

§ 2–3 above are the *manual* launch. For a routine "does the whole AI
dialogue still work end-to-end" check, use the self-driving smoke
script `scripts/mac_dev_no_modem_smoke.py`. It spawns the edge, mints
its own JWT, injects a dial via Redis, polls `call_record`, and tears
the edge down. You only have to **wear headphones and talk**. This
section is the full checklist so a fresh session doesn't re-derive it.

> SSH note: a normal human shell uses
> `ssh -i ~/codes/isales-4.pem root@121.89.85.150 '<cmd>'`.
> A Claude Code session runs as root uid and must add
> `-o UserKnownHostsFile=/Users/bears/.ssh/known_hosts -o IdentitiesOnly=yes`
> (else `Host key verification failed`). The smoke *script* already
> passes `StrictHostKeyChecking=no`, so it works either way.

### 7.1 Pre-flight (~1 min — skipping this is what burns an afternoon)

```bash
# (a) NO leftover edge process. Stale ones hold the CoreAudio device →
#     next edge hangs at startup with "ready timeout", 0 log lines.
pgrep -fl isales-telephony-edge        # MUST print nothing
#     if any: kill them, and if a later edge still hangs:
#     sudo killall coreaudiod   (or log out / run in your own clean Terminal)

# (b) JWT not expired (TTL 24h). Decode exp:
python3 - <<'PY'
import base64,json,time
t=open('/Users/bears/.isales/edge-dev.jwt').read().strip();p=t.split('.')[1];p+='='*(-len(p)%4)
d=json.loads(base64.urlsafe_b64decode(p));print('remaining_h',round((d['exp']-time.time())/3600,1))
PY
#     if ≤0 → re-mint (sources api.env on ECS for ISALES_JWT_SECRET):
ssh -i ~/codes/isales-4.pem root@121.89.85.150 \
  'set -a; . /etc/isales/env/api.env; set +a; \
   /opt/isales/current/venv/bin/isales-edge-token-mint --device-id edge-mac-dev-01 --ttl 24h 2>&1 | tail -1' \
  > ~/.isales/edge-dev.jwt && chmod 600 ~/.isales/edge-dev.jwt
#     NB: the smoke script mints its OWN token for device-id `edge-01`
#     (matches engine.env ISALES_ENGINE_EDGE_DEVICE_ID) — the file above
#     is only needed for the *manual* § 2 launch.

# (c) ECS engine is up AND running the code you think it is. ECS git
#     HEAD is unreliable (deploys are scp-overwrite, working tree is
#     permanently dirty — see meta-repo feedback_ecs_deploy_scp). Trust
#     md5, not `git log`:
ssh -i ~/codes/isales-4.pem root@121.89.85.150 \
  'systemctl is-active isales-engine; \
   md5sum /opt/isales/current/isales-engine/isales_engine/run_loop.py \
          /opt/isales/current/isales-engine/isales_engine/providers/asr_volcengine.py'
md5 -q ~/codes/isales-engine/isales_engine/run_loop.py \
       ~/codes/isales-engine/isales_engine/providers/asr_volcengine.py
#     The two md5 sets MUST match (else scp the mac branch up + restart;
#     see meta-repo feedback_ecs_deploy_scp for the scp-overwrite flow).

# (d) Vendor SDK + pyobjc present on mac:
~/codes/isales-telephony/.venv/bin/python -c "import objc; print('pyobjc', objc.__version__)"
ls -d ~/codes/vendor/DingRTC_macOS_SDK_3_9_0/DingRTC.framework
```

### 7.2 Launch (background, non-interactive)

The interactive listen-check prompt can't take y/n through a non-tty,
so run with `--no-listen-check` and judge the result from the engine
log instead.

```bash
cd ~/codes/isales-telephony
rm -f /tmp/mac-smoke.log
nohup .venv/bin/python scripts/mac_dev_no_modem_smoke.py \
      --no-listen-check --timeout 120 > /tmp/mac-smoke.log 2>&1 &
# tail /tmp/mac-smoke.log; the call is LIVE once you see
#   "[4/6] inject DialRequest ... ✓ injected ... lead=2"
# → the AI greeting plays on the mac speaker within ~2 s. PUT HEADPHONES ON.
```

Other flags: `--phase1-only` (cloud sanity only, no edge/no call),
`--dry-run`, `--skip-edge-start` (edge already running externally),
`--timeout N` (poll longer).

### 7.3 Live monitoring (from a second shell, while the call runs)

Note the call's `sid` / `call_record id` from `/tmp/mac-smoke.log`
(`call_record id=NNN`), then watch the engine journal. Useful greps —
drop the per-second noise:

```bash
SSH="ssh -i ~/codes/isales-4.pem root@121.89.85.150"   # add the root-uid opts in a Claude session

# your speech recognised (the #1 thing that fails — see 7.4):
$SSH 'journalctl -u isales-engine --since "2 min ago" --no-pager | grep "volcengine_asr_FINAL"'

# barge-in fired? (only if you talked OVER the AI):
$SSH 'journalctl -u isales-engine --since "2 min ago" --no-pager \
      | grep -iE "INTERRUPTED|speaking_interrupted|evaluate_partial.*trigger"'

# inbound signal level (raw_max_rms / post_downmix_rms) + downmix sanity:
$SSH 'journalctl -u isales-engine --since "2 min ago" --no-pager | grep "rtc_inbound_1s" | tail'

# how the call ended:
$SSH 'journalctl -u isales-engine --since "2 min ago" --no-pager \
      | grep -iE "session_finalized|hangup_cause|state_transition_unusual"'
```

### 7.4 Reading the result — green vs the known traps

| Signal | Green | Trap / meaning |
|---|---|---|
| `volcengine_asr_FINAL text='…'` | one per thing you said, real Chinese | **empty / none** → your voice too quiet. Check `post_downmix_rms`: need **>2000-ish**; ~600 = vendor recognises nothing (06-04 call 139). Speak up + headphones on. |
| `rtc_inbound_1s ... channels=2 ... after_resample_bytes=320` | always 2-ch in, 320 B out (16k mono 20ms) | the stereo→mono downmix fix; if `after_resample_bytes` looks like 2× this, the downmix regressed. |
| barge-in `INTERRUPTED` | present **iff** you deliberately talked over the AI | absent on clean turn-taking is **expected, not a bug** — barge-in can only fire mid-AI-speech. To test it you must cut the AI off mid-sentence. |
| `hangup_cause=` | `user_hangup` / `goal_reached` | `silence_max_reached` = you went quiet and the `campaign.silence_threshold_ms` (currently 12000) timer expired. Fine if you meant to stop; not a failure of the pipeline. |
| smoke JSON `"ok"` | — | **Known script bug**: success check compares `status in {"ended"}` but the real terminal status is `"end"`, so `ok:false` even on a perfect run. Judge by `status:"end"` + a non-empty `transcript_len`, not by `ok`. |

### 7.5 Recording the call (both channels)

The edge-local recorder now works on the **dev-no-modem path** too (see
OpenSpec change `edge-recording-dev-no-modem`). Set the recording dir
**before launching** — the smoke spawns the edge with `{**os.environ,…}`,
so an exported var reaches the daemon:

```bash
export ISALES_EDGE_RECORDINGS_DIR="$HOME/isales-recordings"   # unset → recording off
export ISALES_EDGE_MAX_RECORDINGS=10                          # rolling; 0 also disables
# then run § 7.2 as usual. On hangup the edge writes:
#   $ISALES_EDGE_RECORDINGS_DIR/<call_id>.wav   (stereo 16 kHz)
#   L = your mic (upstream)   R = AI TTS (downstream, downmixed 48k→16k)
# look for `recording_finalized call_id=… path=…` in the edge log.
afplay "$HOME/isales-recordings/<call_id>.wav"
```

This is the real "full call with the AI's voice" recording. It stays
**edge-local** — no OSS upload, and `call_record.recording_url` stays
NULL in v1.0 (the OSS write-back is a deferred v1.x path).

Separately, the engine keeps a **DIAG dump of the first 15 s of inbound
audio** (your mic only), written unconditionally and overwritten every
call, in its systemd PrivateTmp namespace — handy when the edge recorder
is off:

```bash
$SSH 'find /tmp/systemd-private-*isales-engine*/tmp -name inbound_raw_48k_stereo.pcm'
# pull it down + wrap to a playable WAV (48k stereo s16le, no re-encode):
scp -i ~/codes/isales-4.pem \
  "root@121.89.85.150:$(…the path above…)" /tmp/call_inbound_15s.pcm
python3 -c "import wave;r=open('/tmp/call_inbound_15s.pcm','rb').read();\
w=wave.open('/tmp/call_inbound_15s.wav','wb');w.setnchannels(2);w.setsampwidth(2);\
w.setframerate(48000);w.writeframes(r);w.close()"
afplay /tmp/call_inbound_15s.wav
```

That dump is gated by the `DIAG-REMOVE-AFTER-MIC-DEBUG` markers in
`rtc_telephony.py` and goes away when those are removed (trigger:
`joint-mvp-gate-13301035545` Windows real-dial acceptance passes).

### 7.6 Cleanup

The smoke script terminates its own edge daemon on exit. If you
launched the edge manually (§ 2), `Ctrl-C` it (emits
`remote_hangup{dev_terminate}` → not rescheduled). Always re-run 7.1(a)
before the next test — a leftover edge is the single most common reason
the next run hangs.
