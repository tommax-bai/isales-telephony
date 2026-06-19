"""mac --dev-no-modem 通话**自动判读**: engine+edge 日志 → PASS/WARN/FAIL 报告。

姊妹脚本分工:
  · ``call_timeline.py``  = forensic timeline (看延迟 / TTFT / 尾延迟空档)
  · ``mac_dev_auto_analyze.py`` (本脚本) = verdict (看**通没通** + 哪个环节挂)

目标: 把每次 mac 测试后人工 grep 日志 + 心算 "raw_max_rms 高但 final_rms<gate
→ 上行被滤" 这类判读固化成脚本, 一眼出 9 个检查点的 PASS/WARN/FAIL。

9 个检查点 (一通对话从拨号到收尾的关键环节):
  1. engine_health    engine 是否消费了 dial (没冻死 — 见 bug2 进程级 freeze)
  2. engine_rtc_join  engine DingRTC join rc=0
  3. edge_rtc_join    edge 真 join RTC (无 RtcError / ISALES_RTC_APP_ID 缺失)
  4. greeting_tts     开场白 TTS 合成 (text_len>0)
  5. uplink_audio     上行: raw_max_rms 有声 + final_rms 过 noise gate
                      (核心: 检出 downmix 削弱 + gate 误滤 = 2026-06-08 bug A)
  6. downlink_audio   下行: edge playback peak 出声 (检出 ATS/GSLB 静音 = bug B)
  7. mic_capture      edge mic 真采集到语音
  8. asr_recognition  ASR 识别出非空文字 (volcengine_asr_FINAL)
  9. hangup_finalize  session_finalized persisted (检出僵尸 call / silence_max)

用法::

    python scripts/mac_dev_auto_analyze.py --call-id 168 --since '10:40:00'
    python scripts/mac_dev_auto_analyze.py --call-id 168 --json
    # 起测前探测 engine 是否冻死 (返回码 0=活 / 3=冻死):
    python scripts/mac_dev_auto_analyze.py --probe-engine --token-file /tmp/isales-edge-01.jwt

被 ``mac_dev_no_modem_smoke.py`` import: ``analyze()`` / ``probe_engine_grpc()``。

依赖与 ``mac_dev_no_modem_smoke.py`` / ``call_timeline.py`` 同源 (ssh key / host)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_SSH_HOST = "root@121.89.85.150"
DEFAULT_SSH_KEY = str(Path.home() / "codes/isales-4.pem")
DEFAULT_EDGE_LOG = Path("/tmp/isales-edge-mac-dev.log")
DEFAULT_ENDPOINT = "121.89.85.150:50051"

# ── 判读阈值 (来源: engine/edge log event 词汇 + 2026-06-08 实测) ──────────────
NOISE_GATE_RMS = 1500       # ISALES_ASR_NOISE_GATE_RMS 默认; noise gate 拿 final_rms 比它
UPLINK_RAW_VOICE = 2000     # raw_max_rms 超此 = 上行确有声音到 engine
UPLINK_RAW_FLOOR = 500      # raw_max_rms 全程低于此 = 上行无信号 (edge 没 join)
DOWNMIX_ATTEN_SUSPECT = 4.0 # raw_peak / final_at_peak 超此 = downmix 削弱嫌疑 (相位抵消)
DOWNLINK_AUDIBLE_PEAK = 300 # edge playback peak 超此 = mac 扬声器真出声
DOWNLINK_SILENT_PEAK = 10   # edge playback peak 低于此 = 下行静音
MIC_VOICE_RMS = 2000        # edge mic max_rms 超此 = 用户真说话
MIC_FLOOR = 100             # edge mic max_rms 低于此 = mic 没采集

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_RANK = {PASS: 0, WARN: 1, FAIL: 2}
_ICON = {PASS: "✅", WARN: "⚠️ ", FAIL: "❌"}

# ── 行解析正则 (engine journalctl -o cat / edge log) ─────────────────────────
RE_DIAL = re.compile(r"dial_received call_record_id=(\d+)")
RE_JOIN = re.compile(r"DingRTC join result: rc=(\d+).*?channel=(\d+).*?elapsed_ms=(\d+)")
RE_TTS = re.compile(r"volcengine_tts_first_byte latency_ms=(\d+) text_len=(\d+)")
RE_INBOUND = re.compile(
    r"rtc_inbound_1s sid=(?P<sid>\d+) recv=(?P<recv>\d+) delta=(?P<delta>\d+) "
    r"raw_max_rms=(?P<raw>\d+) post_downmix_rms=(?P<post>\d+) final_rms=(?P<final>\d+)"
)
RE_GATE = re.compile(
    r"asr_noise_gate sid=(?P<sid>\d+) state=(?P<state>\S+) "
    r"final_rms=(?P<final>\d+) silence_ms=(?P<sil>[\d.]+) gate_rms=(?P<gate>\d+)"
)
# volcengine_asr_FINAL text 字段引号不确定, 宽松抓引号内或行尾
RE_ASR_FINAL = re.compile(r"volcengine_asr_FINAL\b.*?text=(['\"])(?P<text>.*?)\1")
RE_ASR_FINAL_LOOSE = re.compile(r"volcengine_asr_FINAL\b")
RE_FINALIZE = re.compile(
    r"session_finalized call_record_id=(?P<cid>\d+) "
    r"hangup_cause=(?P<cause>\S+) persisted=(?P<persisted>\S+)"
)
# edge
RE_PLAYBACK = re.compile(r"dev_playback_recv_1s frames=(\d+) peak=(\d+)")
RE_MIC = re.compile(r"dev_no_modem_mic_push_1s call_id=(?P<cid>\d+).*?max_rms=(?P<rms>\d+)")
RE_ATS = re.compile(r"-1022|App Transport Security|HttpRequest error")


def _ssh(host: str, key: str, remote: str, timeout: int = 40) -> str:
    cmd = ["ssh", "-i", key, "-o", "ConnectTimeout=10",
           "-o", "StrictHostKeyChecking=no", host, remote]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        sys.exit(f"ssh failed (rc={p.returncode}): {p.stderr.strip()}")
    return p.stdout


@dataclass
class Check:
    step: str
    status: str        # PASS / WARN / FAIL
    detail: str


# ── 日志拉取 + 结构化 ────────────────────────────────────────────────────────


def pull_engine(call_id: int, since: str, host: str, key: str) -> dict:
    """journalctl -u isales-engine, 解析本通 call 的关键 engine 事件。

    **按 call 段切割** (而非 grep call_id): 从 ``dial_received call_record_id=cid``
    那行起, 到下一个不同 call 的 ``dial_received`` 或本 call ``session_finalized``
    为止。段内的事件 (含不带 call_id 的 tts_first_byte / volcengine_asr_FINAL /
    silence_max_reached) 才算本通。grep call_id 会同时漏抓本通无 id 行 + 混入别通。
    """
    cid = str(call_id)
    remote = (
        f"journalctl -u isales-engine --since {since!r} -o cat --no-pager 2>/dev/null "
        f"| grep -E 'dial_received|DingRTC join result|volcengine_tts_first_byte|"
        f"rtc_inbound_1s|asr_noise_gate|volcengine_asr_FINAL|session_finalized|"
        f"silence_max_reached'"
    )
    lines = _ssh(host, key, remote).splitlines()
    st: dict = {
        "dial_received": False,
        "join_rc": None, "join_ms": None,
        "tts_text_lens": [],
        "inbound": [],          # list of (raw, post, final)
        "gate_events": 0,
        "asr_finals": [],       # list of recognized text (or "" if presence-only)
        "finalize": None,       # (cause, persisted)
        "silence_max": False,
    }

    start = next((i for i, ln in enumerate(lines)
                  if RE_DIAL.search(ln) and f"call_record_id={cid}" in ln), None)
    if start is None:
        return st  # 这通 call 在窗口内没出现 → dial_received False
    st["dial_received"] = True
    end = len(lines)
    for j in range(start + 1, len(lines)):
        md = RE_DIAL.search(lines[j])
        if md and f"call_record_id={cid}" not in lines[j]:
            end = j  # 下一通 call 开始 → 段结束
            break
        mf = RE_FINALIZE.search(lines[j])
        if mf and mf["cid"] == cid:
            end = j + 1  # 本 call finalize (含该行) → 段结束
            break

    for line in lines[start:end]:
        if (m := RE_JOIN.search(line)) and m.group(2) == cid:
            st["join_rc"], st["join_ms"] = int(m.group(1)), int(m.group(3))
        if m := RE_TTS.search(line):
            st["tts_text_lens"].append(int(m.group(2)))
        if (m := RE_INBOUND.search(line)) and m["sid"] == cid:
            st["inbound"].append((int(m["raw"]), int(m["post"]), int(m["final"])))
        if (m := RE_GATE.search(line)) and m["sid"] == cid:
            st["gate_events"] += 1
        if RE_ASR_FINAL_LOOSE.search(line):
            mt = RE_ASR_FINAL.search(line)
            st["asr_finals"].append(mt["text"] if mt else "")
        if (m := RE_FINALIZE.search(line)) and m["cid"] == cid:
            st["finalize"] = (m["cause"], m["persisted"])
        if "silence_max_reached" in line:
            st["silence_max"] = True
    return st


def pull_edge(call_id: int, edge_log: Path) -> dict:
    """读本地 edge log, 解析 playback / mic / RtcError / ATS。

    同样**按 call 段切割**: ``dev_no_modem_dial_accepted call_id=cid`` 那行起,
    到下一个不同 call 的 dial_accepted / 末尾。排除 dial 前的 edge 启动噪声
    (DingRTC 初始化期的 playback)。dial_accepted 标记缺失时 fallback 全量
    (旧 edge / 确定单通)。
    """
    st: dict = {
        "log_present": edge_log.exists(),
        "playback_peaks": [],
        "mic_rms": [],
        "rtc_error": None,      # RtcError 行 (app_id 缺失等)
        "rtc_join_failed": False,
        "ats_count": 0,
    }
    if not edge_log.exists():
        return st
    lines = edge_log.read_text(errors="replace").splitlines()
    cid = str(call_id)
    start = next((i for i, ln in enumerate(lines)
                  if "dev_no_modem_dial_accepted" in ln and f"call_id={cid}" in ln), None)
    if start is None:
        seg = lines  # 无 dial_accepted 标记 → 全量 fallback
    else:
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if "dev_no_modem_dial_accepted" in lines[j] and f"call_id={cid}" not in lines[j]:
                end = j
                break
        seg = lines[start:end]
    for line in seg:
        if m := RE_PLAYBACK.search(line):
            st["playback_peaks"].append(int(m.group(2)))
        if (m := RE_MIC.search(line)) and m["cid"] == cid:
            st["mic_rms"].append(int(m["rms"]))
        if "RtcError" in line and st["rtc_error"] is None:
            st["rtc_error"] = line.strip()[:160]
        if "dev_no_modem_rtc_join_failed" in line:
            st["rtc_join_failed"] = True
        if RE_ATS.search(line):
            st["ats_count"] += 1
    return st


# ── 9 个检查点 ───────────────────────────────────────────────────────────────


def _judge(eng: dict, edge: dict, gate_rms: int) -> list[Check]:
    checks: list[Check] = []

    # 1. engine_health — 消费了 dial = engine loop 没冻死
    if eng["dial_received"]:
        checks.append(Check("engine_health", PASS, "engine 消费了 dial (event loop 活)"))
    else:
        checks.append(Check("engine_health", FAIL,
                            "engine 未消费 dial — 可能冻死(bug2)/dial 没到/since 太晚"))

    # 2. engine_rtc_join
    if eng["join_rc"] == 0:
        checks.append(Check("engine_rtc_join", PASS,
                            f"engine join RTC rc=0 ({eng['join_ms']}ms)"))
    elif eng["join_rc"] is None:
        checks.append(Check("engine_rtc_join", FAIL, "无 DingRTC join 日志"))
    else:
        checks.append(Check("engine_rtc_join", FAIL, f"engine join rc={eng['join_rc']} (非0)"))

    # 3. edge_rtc_join
    if not edge["log_present"]:
        checks.append(Check("edge_rtc_join", WARN, "edge log 缺失, 跳过 edge 侧判读"))
    elif edge["rtc_error"]:
        hint = " (查 ISALES_RTC_APP_ID)" if "APP_ID" in edge["rtc_error"] else ""
        checks.append(Check("edge_rtc_join", FAIL, f"edge RtcError{hint}: {edge['rtc_error']}"))
    elif edge["rtc_join_failed"]:
        checks.append(Check("edge_rtc_join", FAIL, "edge dev_no_modem_rtc_join_failed"))
    elif edge["playback_peaks"] or edge["mic_rms"]:
        checks.append(Check("edge_rtc_join", PASS, "edge 已 join (有 playback/mic pump)"))
    else:
        checks.append(Check("edge_rtc_join", WARN, "edge 无 RtcError 但也无音频 pump 迹象"))

    # 4. greeting_tts
    nonzero = [t for t in eng["tts_text_lens"] if t > 0]
    if nonzero:
        checks.append(Check("greeting_tts", PASS,
                            f"TTS 合成 ×{len(nonzero)} (text_len 首={nonzero[0]})"))
    else:
        checks.append(Check("greeting_tts", WARN, "无 tts_first_byte — campaign 没开场白?"))

    # 5. uplink_audio — 核心: downmix 削弱 + noise gate 误滤
    inb = eng["inbound"]
    if not inb:
        checks.append(Check("uplink_audio", FAIL, "无 rtc_inbound — edge 没发上行"))
    else:
        raw_peak = max(r for r, _, _ in inb)
        # 取 raw 峰值那一秒的 final, 以及全程 final 峰值
        final_at_raw_peak = max(f for r, _, f in inb if r == raw_peak)
        final_peak = max(f for _, _, f in inb)
        if raw_peak < UPLINK_RAW_FLOOR:
            checks.append(Check("uplink_audio", FAIL,
                                f"上行无信号 raw_max_rms 峰值={raw_peak}<{UPLINK_RAW_FLOOR} "
                                f"(edge 没真 join / mic 没采集)"))
        elif raw_peak < UPLINK_RAW_VOICE:
            checks.append(Check("uplink_audio", WARN,
                                f"上行信号弱 raw_peak={raw_peak} (说话再大声 / 查 mic)"))
        elif final_peak >= gate_rms:
            checks.append(Check("uplink_audio", PASS,
                                f"上行有效过门 raw_peak={raw_peak} final_peak={final_peak}≥gate={gate_rms}"))
        else:
            atten = raw_peak / final_at_raw_peak if final_at_raw_peak else 999
            downmix = f"; downmix 削弱嫌疑(raw/final={atten:.0f}x≥{DOWNMIX_ATTEN_SUSPECT:.0f})" \
                if atten >= DOWNMIX_ATTEN_SUSPECT else ""
            checks.append(Check("uplink_audio", FAIL,
                                f"上行有声却被滤掉 raw_peak={raw_peak} 但 final_peak={final_peak}"
                                f"<gate={gate_rms} → ASR 收不到{downmix} [bug A]"))

    # 6. downlink_audio — edge playback peak + ATS
    if not edge["log_present"]:
        checks.append(Check("downlink_audio", WARN, "edge log 缺失"))
    elif not edge["playback_peaks"]:
        checks.append(Check("downlink_audio", FAIL, "无 dev_playback_recv — 下行帧没到 edge"))
    else:
        pk = max(edge["playback_peaks"])
        ats = f"; ATS 拦 GSLB ×{edge['ats_count']}" if edge["ats_count"] else ""
        if pk >= DOWNLINK_AUDIBLE_PEAK:
            checks.append(Check("downlink_audio", PASS, f"下行出声 peak={pk}{ats}"))
        elif pk < DOWNLINK_SILENT_PEAK:
            checks.append(Check("downlink_audio", FAIL,
                                f"下行静音 peak={pk}<{DOWNLINK_SILENT_PEAK} (AI 没传到扬声器){ats} [bug B]"))
        else:
            checks.append(Check("downlink_audio", WARN, f"下行音量低 peak={pk}{ats}"))

    # 7. mic_capture
    if not edge["log_present"]:
        checks.append(Check("mic_capture", WARN, "edge log 缺失"))
    elif not edge["mic_rms"]:
        checks.append(Check("mic_capture", FAIL, "无 mic_push — mic 没采集 (查权限)"))
    else:
        mp = max(edge["mic_rms"])
        if mp > MIC_VOICE_RMS:
            checks.append(Check("mic_capture", PASS, f"mic 采到语音 max_rms={mp}"))
        elif mp > MIC_FLOOR:
            checks.append(Check("mic_capture", WARN, f"mic 只有弱信号/底噪 max_rms={mp} (说大声点)"))
        else:
            checks.append(Check("mic_capture", FAIL, f"mic 近静音 max_rms={mp} (查 mic 权限/输入设备)"))

    # 8. asr_recognition
    texts = [t for t in eng["asr_finals"] if t]
    if texts:
        checks.append(Check("asr_recognition", PASS, f"ASR 识别 ×{len(texts)}: '{texts[0]}'"))
    elif eng["asr_finals"]:
        checks.append(Check("asr_recognition", WARN,
                            f"有 {len(eng['asr_finals'])} 条 FINAL 但没解析出 text"))
    else:
        checks.append(Check("asr_recognition", FAIL, "ASR 零识别 (上行被滤 / 没说话)"))

    # 9. hangup_finalize
    if eng["finalize"]:
        cause, persisted = eng["finalize"]
        if persisted.lower().startswith("t") and cause not in {"silence_max_reached", "SILENCE_MAX_REACHED"}:
            checks.append(Check("hangup_finalize", PASS, f"已 finalize cause={cause} persisted={persisted}"))
        elif cause in {"silence_max_reached", "SILENCE_MAX_REACHED"}:
            n_asr = len([t for t in eng["asr_finals"] if t])
            tail = f"识别了 {n_asr} 句对话后静音收尾" if n_asr else "全程零识别"
            checks.append(Check("hangup_finalize", WARN, f"silence_max 挂断 ({tail})"))
        else:
            checks.append(Check("hangup_finalize", WARN, f"finalize persisted={persisted}"))
    elif eng["silence_max"]:
        checks.append(Check("hangup_finalize", WARN, "silence_max 触发但无 session_finalized"))
    else:
        checks.append(Check("hangup_finalize", FAIL,
                            "未 finalize — 僵尸 call (bug2: 远端挂断不收尾)"))

    return checks


def analyze(call_id: int, since: str, *, ssh_host: str = DEFAULT_SSH_HOST,
            ssh_key: str = DEFAULT_SSH_KEY, edge_log: Path = DEFAULT_EDGE_LOG,
            gate_rms: int = NOISE_GATE_RMS) -> dict:
    """拉两端日志 → 判读 9 检查点 → 返回报告 dict。"""
    eng = pull_engine(call_id, since, ssh_host, ssh_key)
    edge = pull_edge(call_id, Path(edge_log))
    checks = _judge(eng, edge, gate_rms)
    overall = max((c.status for c in checks), key=lambda s: _RANK[s])
    texts = [t for t in eng["asr_finals"] if t]
    return {
        "call_id": call_id,
        "overall": overall,
        "checks": [asdict(c) for c in checks],
        "first_asr_text": texts[0] if texts else None,
        "final_hangup_cause": eng["finalize"][0] if eng["finalize"] else None,
        "stats": {
            "inbound_samples": len(eng["inbound"]),
            "uplink_raw_peak": max((r for r, _, _ in eng["inbound"]), default=0),
            "uplink_final_peak": max((f for _, _, f in eng["inbound"]), default=0),
            "downlink_peak": max(edge["playback_peaks"], default=0),
            "mic_peak": max(edge["mic_rms"], default=0),
            "ats_errors": edge["ats_count"],
        },
    }


def print_report(report: dict) -> None:
    """人类可读报告 (stderr 表格 + stdout 摘要)。"""
    print(f"\n{'='*72}\n通话判读  call_id={report['call_id']}   "
          f"overall={_ICON[report['overall']]}{report['overall']}\n{'='*72}", file=sys.stderr)
    for c in report["checks"]:
        print(f"  {_ICON[c['status']]} {c['step']:<17} {c['detail']}", file=sys.stderr)
    s = report["stats"]
    print(f"\n  指标: 上行 raw峰={s['uplink_raw_peak']} final峰={s['uplink_final_peak']} "
          f"(gate={NOISE_GATE_RMS}) │ 下行 peak={s['downlink_peak']} │ "
          f"mic峰={s['mic_peak']} │ ATS×{s['ats_errors']}", file=sys.stderr)
    if report["first_asr_text"]:
        print(f"  首句 ASR: '{report['first_asr_text']}'", file=sys.stderr)
    print(f"  hangup_cause: {report['final_hangup_cause']}", file=sys.stderr)


# ── 起测前: engine 冻死探测 (grpc Bidi initial_metadata, 见 bug2) ─────────────


async def _probe(endpoint: str, token: str, timeout: float = 8.0) -> tuple[bool, str]:
    import grpc
    from isales_common.proto import cloud_edge_pb2_grpc as g
    ch = grpc.aio.insecure_channel(endpoint)
    stub = g.CloudEdgeStub(ch)

    async def _empty():
        return
        yield  # noqa — async generator that yields nothing

    call = stub.Bidi(_empty(), metadata=(("authorization", f"Bearer {token}"),))
    try:
        await asyncio.wait_for(call.initial_metadata(), timeout=timeout)
        return True, "engine grpc handler 响应 (initial_metadata)"
    except asyncio.TimeoutError:
        return False, f"engine grpc handler {timeout:.0f}s 无响应 — event loop 冻死? (bug2)"
    except Exception as exc:  # noqa: BLE001 — auth/conn errors也是"engine 在响应"
        return True, f"engine 拒绝/异常但有响应 ({type(exc).__name__}: {str(exc)[:80]})"
    finally:
        with __import__("contextlib").suppress(Exception):
            await ch.close()


def probe_engine_grpc(endpoint: str, token: str, timeout: float = 8.0) -> tuple[bool, str]:
    """同步包装: True=engine 活, False=冻死。起 edge 前调一次, 避免对着冻死的 engine 测。"""
    return asyncio.run(_probe(endpoint, token, timeout))


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--call-id", type=int, help="要判读的 call_record_id")
    ap.add_argument("--since", default="15 min ago",
                    help="journalctl --since (e.g. '10:40:00' / '15 min ago')")
    ap.add_argument("--edge-log", default=str(DEFAULT_EDGE_LOG))
    ap.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    ap.add_argument("--ssh-key", default=DEFAULT_SSH_KEY)
    ap.add_argument("--gate-rms", type=int, default=NOISE_GATE_RMS,
                    help=f"noise gate 阈值 (默认 {NOISE_GATE_RMS}, 同 ISALES_ASR_NOISE_GATE_RMS)")
    ap.add_argument("--json", action="store_true")
    # probe 模式
    ap.add_argument("--probe-engine", action="store_true",
                    help="只探测 engine 是否冻死 (grpc Bidi), 不判读通话")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--token-file", help="探测用 JWT 文件 (edge-01)")
    args = ap.parse_args()

    if args.probe_engine:
        if not args.token_file:
            sys.exit("--probe-engine 需要 --token-file")
        token = Path(args.token_file).read_text().strip()
        alive, msg = probe_engine_grpc(args.endpoint, token)
        print(f"{'✅' if alive else '❌'} engine probe: {msg}", file=sys.stderr)
        return 0 if alive else 3

    if not args.call_id:
        sys.exit("需要 --call-id (或 --probe-engine)")
    report = analyze(args.call_id, args.since, ssh_host=args.ssh_host,
                     ssh_key=args.ssh_key, edge_log=Path(args.edge_log),
                     gate_rms=args.gate_rms)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)
    return 0 if report["overall"] != FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
