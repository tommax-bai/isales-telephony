"""整理一通 mac --dev-no-modem 通话的端到端时间线 (人输入 ↔ AI 输出)。

把**两端**日志按 wall-clock 合并成一条时间线:

  edge 端 (mac 本地, /tmp/isales-edge-mac-dev.log):
    · dev_no_modem_mic_push_1s  → 👤 人对着 mic 说话 (max_rms 抬高 = 有声)
    · dev_playback_recv_1s      → 🔊 DingRTC 在 mac 扬声器真正出声 (peak 抬高)
                                  ← 这才是"DingRTC 输出时刻", 真端到端的下游锚点

  engine 端 (ECS, journalctl -u isales-engine):
    · dial_received / DingRTC join / session_finalized → 会话边界
    · rtc_inbound_1s            → 📥 engine 收到上行音频 (raw / post_downmix rms)
    · turn_timing              → 🧠 ASR finalize 后的一轮: 用户整句 + AI 回复 +
                                  engine 内部各段延迟 (TTFT / 首句 / 首音频)

为什么要两端: engine 的 ``turn_timing.first_audio_ms`` 只量到 "engine 把第一个
PCM 块推给 DingRTC", **不含** 下行 RTC + mac jitter buffer + 扬声器出声。人真正
体感的端到端 = 👤 mic 说完 → 🔊 mac 扬声器出声, 必须用 edge 的 playback peak 才
量得到。本脚本把两者并排, 既给 engine 内部精确 ms, 也给 edge 侧 ~1s 粒度的真出声
时刻, 并自动标出 "人说了话但迟迟没回应" 的空档 (ASR finalize 尾延迟)。

时钟: mac 与 ECS 均 NTP 同步, 偏移通常 < 100ms。``--skew-ms`` 可手动校正
(engine_clock - edge_clock, 正值表示 ECS 快; 会从 engine 时间里减掉)。edge 是
1 秒聚合, 所以真 e2e 精度本就 ~1s, 几十 ms 偏移可忽略。

用法::

    python scripts/call_timeline.py --call-id 149
    python scripts/call_timeline.py --call-id 149 --since '10:19:00' --asr-debug
    python scripts/call_timeline.py --call-id 149 --json > /tmp/tl-149.json

依赖与 mac_dev_no_modem_smoke.py 同源 (ssh key / host)。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_SSH_HOST = "root@121.89.85.150"
DEFAULT_SSH_KEY = str(Path.home() / "codes/isales-4.pem")
DEFAULT_EDGE_LOG = Path("/tmp/isales-edge-mac-dev.log")

# "有声" 判定阈值 (经验值, 见 2026-06-04 handoff: 人声 RMS 应 > ~2000, 背景 ~800)
MIC_VOICE_RMS = 2000        # edge mic max_rms 超此值算 👤 在说话
SPK_AUDIBLE_PEAK = 800      # edge playback peak 超此值算 🔊 扬声器出声
INBOUND_VOICE_RMS = 2000    # engine rtc_inbound raw_max_rms 超此值算上行有声

# ── 行解析正则 ──────────────────────────────────────────────────────────────
# engine: "2026-06-05 10:20:06,426 INFO logger msg..."
RE_ENG = re.compile(r"^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d),(\d{3}) ")
# edge:   "10:20:08.429 INFO logger msg..."
RE_EDGE = re.compile(r"^(\d\d:\d\d:\d\d)\.(\d{3}) ")

RE_TURN = re.compile(
    r"turn_timing user=(?P<user>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\") "
    r"reply=(?P<reply>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\") "
    r"recv_to_proc_ms=(?P<rtp>\S+) llm_first_token_ms=(?P<tok>\S+) "
    r"llm_first_sentence_ms=(?P<sent>\S+) first_audio_ms=(?P<fa>\S+) "
    r"recv_to_audio_ms=(?P<rta>\S+) main_dur_ms=(?P<dur>\S+)"
)
RE_INBOUND = re.compile(
    r"rtc_inbound_1s sid=(?P<sid>\d+).*?raw_max_rms=(?P<raw>\d+) "
    r"post_downmix_rms=(?P<post>\d+)"
)
RE_MIC = re.compile(r"dev_no_modem_mic_push_1s call_id=(?P<cid>\d+).*?max_rms=(?P<rms>\d+)")
RE_SPK = re.compile(r"dev_playback_recv_1s .*?peak=(?P<peak>\d+)")


# ── 数据模型 ────────────────────────────────────────────────────────────────
@dataclass(order=True)
class Event:
    t: datetime
    lane: str = field(compare=False)      # MIC / RX / TURN / SPK / SYS
    icon: str = field(compare=False)
    text: str = field(compare=False)
    meta: dict = field(compare=False, default_factory=dict)


def _ssh(host: str, key: str, remote: str, timeout: int = 40) -> str:
    cmd = ["ssh", "-i", key, "-o", "ConnectTimeout=10",
           "-o", "StrictHostKeyChecking=no", host, remote]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        sys.exit(f"ssh failed (rc={p.returncode}): {p.stderr.strip()}")
    return p.stdout


def _unquote(s: str) -> str:
    """剥掉 Python %r 的外层引号 + 反转义 (保留 UTF-8 中文, 不走 unicode_escape)。"""
    if len(s) < 2:
        return s
    q, body = s[0], s[1:-1]
    return body.replace("\\" + q, q).replace("\\\\", "\\")


def pull_engine(args) -> list[Event]:
    """journalctl -u isales-engine, grep call_id, 解析为 Event。"""
    cid = args.call_id
    remote = (
        f"journalctl -u isales-engine --since {args.since!r} -o cat --no-pager 2>/dev/null "
        f"| grep -E '{cid}|turn_timing|session_finalized' "
        f"| grep -vE 'vad_monitor_cancel_disabled_prototype'"
    )
    raw = _ssh(args.ssh_host, args.ssh_key, remote)
    skew = timedelta(milliseconds=args.skew_ms)
    events: list[Event] = []
    for line in raw.splitlines():
        m = RE_ENG.match(line)
        if not m:
            continue
        t = datetime.strptime(f"{m[1]} {m[2]}.{m[3]}", "%Y-%m-%d %H:%M:%S.%f") - skew

        if f"call_record_id={cid}" in line and "dial_received" in line:
            events.append(Event(t, "SYS", "📞", f"dial_received (call {cid})"))
        elif f"channel={cid}" in line and "DingRTC join" in line:
            jm = re.search(r"elapsed_ms=(\d+)", line)
            events.append(Event(t, "SYS", "🔗", f"DingRTC join rc=0 ({jm[1] if jm else '?'}ms)"))
        elif f"call_record_id={cid}" in line and "session_finalized" in line:
            hm = re.search(r"hangup_cause=(\S+)", line)
            events.append(Event(t, "SYS", "🛑", f"session_finalized hangup_cause={hm[1] if hm else '?'}"))
        elif (mt := RE_TURN.search(line)):
            user = _unquote(mt["user"])
            reply = _unquote(mt["reply"])
            fa = mt["fa"]
            rta = mt["rta"]
            events.append(Event(
                t, "TURN", "🧠",
                f"AI回复: {reply}",
                meta={
                    "user": user, "reply": reply,
                    "recv_to_proc_ms": mt["rtp"], "llm_first_token_ms": mt["tok"],
                    "llm_first_sentence_ms": mt["sent"], "first_audio_ms": fa,
                    "recv_to_audio_ms": rta, "main_dur_ms": mt["dur"],
                },
            ))
        elif (mi := RE_INBOUND.search(line)) and mi["sid"] == str(cid):
            raw_rms = int(mi["raw"])
            if raw_rms >= INBOUND_VOICE_RMS:
                events.append(Event(
                    t, "RX", "📥",
                    f"engine 收到上行有声 raw_rms={raw_rms} post_downmix={mi['post']}",
                    meta={"raw_rms": raw_rms, "post_rms": int(mi["post"])},
                ))
    return events


def pull_edge(args, ref_date: str) -> list[Event]:
    """读本地 edge log, 解析 mic / playback。edge 只有时分秒, 用 ref_date 补日期。"""
    p = Path(args.edge_log)
    if not p.exists():
        print(f"  ⚠️  edge log 不存在: {p} (跳过 edge 侧, 只有 engine 时间线)", file=sys.stderr)
        return []
    events: list[Event] = []
    cid = str(args.call_id)
    for line in p.read_text(errors="replace").splitlines():
        m = RE_EDGE.match(line)
        if not m:
            continue
        t = datetime.strptime(f"{ref_date} {m[1]}.{m[2]}", "%Y-%m-%d %H:%M:%S.%f")
        if (mm := RE_MIC.search(line)) and mm["cid"] == cid:
            rms = int(mm["rms"])
            if rms >= MIC_VOICE_RMS:
                events.append(Event(t, "MIC", "👤", f"人说话 mic_rms={rms}", meta={"rms": rms}))
        elif (ms := RE_SPK.search(line)):
            peak = int(ms["peak"])
            if peak >= SPK_AUDIBLE_PEAK:
                events.append(Event(t, "SPK", "🔊", f"mac扬声器出声 peak={peak} (DingRTC输出)", meta={"peak": peak}))
    return events


def derive_metrics(events: list[Event]) -> dict:
    """真端到端 + ASR finalize 尾延迟分析。"""
    turns = [e for e in events if e.lane == "TURN"]
    spk = [e for e in events if e.lane == "SPK"]
    mic = [e for e in events if e.lane == "MIC"]
    out = {"turns": [], "silence_gaps": []}

    for tn in turns:
        # engine 内部 (精确 ms)
        eng_e2e = tn.meta.get("recv_to_audio_ms")
        # 真 e2e 下游锚点: 该 turn 打点后第一个 mac 扬声器出声秒
        spk_after = next((s for s in spk if s.t >= tn.t - timedelta(seconds=1)), None)
        # ASR finalize 尾延迟: turn 前最后一段连续 mic-active → turn 打点 (engine 侧)
        mic_before = [m for m in mic if m.t <= tn.t]
        last_voice = mic_before[-1].t if mic_before else None
        out["turns"].append({
            "engine_log_at": tn.t.strftime("%H:%M:%S.%f")[:-3],
            "user": tn.meta.get("user"),
            "reply": tn.meta.get("reply", "")[:40],
            "engine_internal_recv_to_audio_ms": eng_e2e,
            "llm_first_token_ms": tn.meta.get("llm_first_token_ms"),
            "first_audio_ms": tn.meta.get("first_audio_ms"),
            "mac_speaker_onset_at": spk_after.t.strftime("%H:%M:%S") if spk_after else None,
        })

    # 空档: 相邻有声段之间 ≥10s 没有任何 turn, 且期间 engine RX 也收到了有声上行
    # (= 用户音频确实到了 engine 却没产生一轮 → ASR finalize 尾延迟的签名;
    #  RX 条件滤掉"开场白播放期用户没说话"那种假阳性)。
    rx = [e for e in events if e.lane == "RX"]
    voiced_secs = sorted({m.t.replace(microsecond=0) for m in mic})
    turn_secs = [t.t for t in turns]
    for i in range(1, len(voiced_secs)):
        a, b = voiced_secs[i - 1], voiced_secs[i]
        delta = (b - a).total_seconds()
        if delta < 10:
            continue
        if any(a <= ts <= b for ts in turn_secs):
            continue
        if not any(a <= r.t <= b for r in rx):
            continue  # 期间 engine 没收到有声上行 → 不算"说了没回应"
        out["silence_gaps"].append({
            "from": a.strftime("%H:%M:%S"), "to": b.strftime("%H:%M:%S"),
            "gap_s": round(delta, 1),
            "note": "用户音频到了engine却没出turn (ASR finalize 尾延迟 / silence_max)",
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--call-id", type=int, required=True)
    ap.add_argument("--edge-log", default=str(DEFAULT_EDGE_LOG))
    ap.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    ap.add_argument("--ssh-key", default=DEFAULT_SSH_KEY)
    ap.add_argument("--since", default="20 min ago",
                    help="journalctl --since (e.g. '10:19:00' / '20 min ago')")
    ap.add_argument("--skew-ms", type=int, default=0,
                    help="engine_clock - edge_clock 偏移ms (正=ECS快, 从engine时间减掉)")
    ap.add_argument("--asr-debug", action="store_true",
                    help="附带打印 ASR partial/silence_eos/promote 原始行 (诊断空档)")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    eng = pull_engine(args)
    if not eng:
        sys.exit(f"engine 侧没找到 call {args.call_id} 的事件 (调 --since?)")
    ref_date = eng[0].t.strftime("%Y-%m-%d")
    edge = pull_edge(args, ref_date)
    events = sorted(eng + edge)
    metrics = derive_metrics(events)

    if args.json:
        print(json.dumps({
            "call_id": args.call_id,
            "timeline": [{"t": e.t.strftime("%H:%M:%S.%f")[:-3], "lane": e.lane,
                          "icon": e.icon, "text": e.text, **({"meta": e.meta} if e.meta else {})}
                         for e in events],
            "metrics": metrics,
        }, ensure_ascii=False, indent=2))
        return 0

    # ── 人类可读时间线 ──
    print(f"\n{'='*78}\n通话时间线  call_id={args.call_id}   "
          f"(edge=mac本地 ~1s粒度 / engine=ECS精确ms / skew={args.skew_ms}ms)\n{'='*78}")
    print(f"{'时刻':<13}{'通道':<7} 事件")
    print("-" * 78)
    lane_tag = {"MIC": "👤mic", "RX": "📥rx", "TURN": "🧠turn",
                "SPK": "🔊spk", "SYS": "⚙️sys"}
    NOISY = {"MIC", "SPK", "RX"}        # 每秒一条的连续流 → 折叠成区间
    peak_key = {"MIC": "rms", "SPK": "peak", "RX": "raw_rms"}

    def flush(run: list[Event]) -> None:
        if not run:
            return
        lane = run[0].lane
        if len(run) == 1:
            e = run[0]
            print(f"{e.t.strftime('%H:%M:%S.%f')[:-3]:<13}{lane_tag[lane]:<7} {e.text}")
            return
        k = peak_key[lane]
        pk = max(int(r.meta.get(k, 0)) for r in run)
        label = {"MIC": "人说话", "SPK": "mac扬声器出声(DingRTC输出)", "RX": "engine收上行有声"}[lane]
        span = f"{run[0].t.strftime('%H:%M:%S')}→{run[-1].t.strftime('%H:%M:%S')}"
        print(f"{run[0].t.strftime('%H:%M:%S.%f')[:-3]:<13}{lane_tag[lane]:<7} "
              f"{label}  ×{len(run)}  [{span}]  peak={pk}")

    run: list[Event] = []
    last_t = None
    for e in events:
        if last_t and (e.t - last_t).total_seconds() >= 5 and not run:
            print(f"   … 静默 {(e.t - last_t).total_seconds():.0f}s …")
        if e.lane in NOISY and run and run[-1].lane == e.lane \
                and (e.t - run[-1].t).total_seconds() <= 2.5:
            run.append(e)
        else:
            flush(run)
            run = []
            if e.lane in NOISY:
                run = [e]
            else:
                if e.lane == "TURN":
                    txt = (f"👤“{e.meta['user']}”  →  🤖“{e.meta['reply'][:48]}”  "
                           f"[内部e2e {e.meta['recv_to_audio_ms']}ms / TTFT {e.meta['llm_first_token_ms']}ms]")
                else:
                    txt = e.text
                print(f"{e.t.strftime('%H:%M:%S.%f')[:-3]:<13}{lane_tag.get(e.lane, e.lane):<7} {txt}")
        last_t = e.t
    flush(run)

    # ── 派生指标 ──
    print(f"\n{'─'*78}\n真端到端 vs engine内部 (每轮)\n{'─'*78}")
    for i, tr in enumerate(metrics["turns"], 1):
        print(f"[{i}] {tr['engine_log_at']}  👤“{tr['user']}”")
        print(f"     🤖“{tr['reply']}…”")
        print(f"     engine内部 recv→首音频={tr['engine_internal_recv_to_audio_ms']}ms "
              f"(TTFT {tr['llm_first_token_ms']}ms, first_audio {tr['first_audio_ms']}ms) "
              f"│ mac扬声器实际出声≈{tr['mac_speaker_onset_at'] or 'n/a'}")
    if metrics["silence_gaps"]:
        print(f"\n{'─'*78}\n⚠️  疑似空档 (人说话却没回应 — ASR finalize 尾延迟)\n{'─'*78}")
        for g in metrics["silence_gaps"]:
            print(f"  {g['from']} → {g['to']}  ({g['gap_s']}s)  {g['note']}")

    if args.asr_debug:
        print(f"\n{'─'*78}\nASR 原始诊断行 (partial/silence_eos/promote)\n{'─'*78}")
        remote = (f"journalctl -u isales-engine --since {args.since!r} -o cat --no-pager 2>/dev/null "
                  f"| grep -iE 'silence_eos|promote|partial|NONEMPTY|is_final' | tail -40")
        print(_ssh(args.ssh_host, args.ssh_key, remote) or "  (无)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
