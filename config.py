"""Experiment definitions = Phase 2 Step 1 (factors) and Step 7 (experiments)."""
import re

RATE_MBIT = 100        # bottleneck rate (fixed parameter)
PKT_BYTES = 1500       # MTU, used to convert BDP to packets
MSS = 1448
TRIM_HEAD_S = 10       # drop slow start / BBR Startup
TRIM_TAIL_S = 2
STATIC_DUR = 60
TRANSIENT_DUR = 90
GAP_S = 5              # idle gap between runs
QUEUES = ["fifo", "fq_codel"]


def bdp_packets(rtt_ms, rate_mbit=RATE_MBIT):
    return rate_mbit * 1e6 * (rtt_ms / 1e3) / (8 * PKT_BYTES)


def G(cc, n=1, rtt=50, start=0):
    """A sender group: n flows of one congestion control, one base RTT, one start time."""
    return {"cc": cc, "n": n, "rtt_ms": rtt, "start_s": start}


def mix_groups(spec, rtt=50):
    """'cubic2' | 'bbr2' | 'C:B' = C CUBIC flows vs B BBR flows. CUBIC is always group 0."""
    if spec.startswith("cubic"):
        return [G("cubic", int(spec[5:]), rtt)]
    if spec.startswith("bbr"):
        return [G("bbr", int(spec[3:]), rtt)]
    c, b = (int(v) for v in spec.split(":"))
    return [G("cubic", c, rtt), G("bbr", b, rtt)]


def safe(s):
    return re.sub(r"[^A-Za-z0-9_.-]", "v", str(s))


def make_cell(exp, name, groups, queue="fifo", buffer_bdp=2.0, buffer_pkts=None,
              qparams=None, dynamics="static", events=None, duration=None,
              host_ecn=2, sender_fq=True, variant=None, mix=None):
    bdp_rtt = max(g["rtt_ms"] for g in groups)          # BDP uses the longest base RTT
    limit = buffer_pkts or max(2, round(buffer_bdp * bdp_packets(bdp_rtt)))
    if duration is None:
        duration = STATIC_DUR if dynamics == "static" else TRANSIENT_DUR
    return {
        "exp": exp, "cell_id": f"{exp}__{safe(name)}", "mix": mix,
        "variant": variant or queue, "groups": groups, "queue": queue,
        "qparams": qparams or {}, "buffer_bdp": None if buffer_pkts else buffer_bdp,
        "limit_pkts": int(limit), "bdp_rtt_ms": bdp_rtt, "rate_mbit": RATE_MBIT,
        "dynamics": dynamics, "events": events or [], "duration_s": int(duration),
        "host_ecn": host_ecn, "sender_fq": sender_fq,
    }


# ---------------- Experiments ----------------
def exp0():
    """Held-out validation: replicate published settings ([8] and [7])."""
    cells = []
    for q in QUEUES:
        cells.append(make_cell("exp0", f"rep8_{q}", mix_groups("1:1", 10), q, 2.0, mix="1:1"))
        cells.append(make_cell("exp0", f"rep7_{q}", [G("cubic", 5, 10), G("cubic", 5, 50)], q,
                               buffer_pkts=1000, mix="cubic5@10ms+cubic5@50ms"))
    return cells


def exp1():
    """Core 2x2 (+ homogeneous references for HARM)."""
    return [make_cell("exp1", f"{m}_{q}", mix_groups(m, 50), q, 2.0, mix=m)
            for m in ["cubic2", "bbr2", "1:1"] for q in QUEUES]


def exp2(rtts=(10, 50, 100), buffers=(0.5, 1, 2, 4, 8)):
    """Regime sweep: buffer x RTT."""
    return [make_cell("exp2", f"1v1_{q}_b{b}_r{r}", mix_groups("1:1", r), q, b, mix="1:1")
            for q in QUEUES for b in buffers for r in rtts]


def exp3():
    """Flow-count scaling."""
    return [make_cell("exp3", f"{m}_{q}_b{b}", mix_groups(m, 50), q, b, mix=m)
            for m in ["2:2", "4:4", "8:8", "1:4", "4:1"] for q in QUEUES for b in (1, 4)]


def exp4():
    """RTT heterogeneity: CUBIC and BBR on different base RTTs."""
    cells = []
    for rc, rb in [(10, 50), (50, 10)]:
        for q in QUEUES:
            for b in (1, 4):
                cells.append(make_cell("exp4", f"c{rc}_b{rb}_{q}_b{b}",
                                       [G("cubic", 1, rc), G("bbr", 1, rb)], q, b,
                                       mix=f"cubic@{rc}+bbr@{rb}"))
    return cells


def exp5():
    """Transients: capacity step, RTT step, late joiners."""
    cells = []
    for q in QUEUES:
        cells.append(make_cell("exp5", f"capstep_{q}", mix_groups("1:1", 50), q, 2.0,
                               dynamics="cap_step", mix="1:1",
                               events=[{"t": 30, "type": "rate", "mbit": 50},
                                       {"t": 60, "type": "rate", "mbit": 100}]))
        cells.append(make_cell("exp5", f"rttstep_{q}", mix_groups("1:1", 20), q, 2.0,
                               dynamics="rtt_step", mix="1:1",
                               events=[{"t": 30, "type": "rtt", "ms": 60}]))
        cells.append(make_cell("exp5", f"bbrlate_{q}", [G("cubic", 1, 50, 0), G("bbr", 1, 50, 20)],
                               q, 2.0, dynamics="stagger", mix="1:1"))
        cells.append(make_cell("exp5", f"cubiclate_{q}", [G("cubic", 1, 50, 20), G("bbr", 1, 50, 0)],
                               q, 2.0, dynamics="stagger", mix="1:1"))
    return cells


VARIANTS = {
    "A0_fifo":            dict(queue="fifo"),
    "A1_fq_only":         dict(queue="fq_nocodel"),
    "A2_codel_only":      dict(queue="codel"),
    "A3_fq_codel":        dict(queue="fq_codel"),
    "A4_fq_codel_ecn":    dict(queue="fq_codel", qparams={"ecn": True}, host_ecn=1),
    "A5a_target10":       dict(queue="fq_codel", qparams={"target_ms": 10}),
    "A5b_target20":       dict(queue="fq_codel", qparams={"target_ms": 20}),
    "A7a_fifo_nopace":    dict(queue="fifo", sender_fq=False),
    "A7b_fqcodel_nopace": dict(queue="fq_codel", sender_fq=False),
}


def exp6():
    """Ablation (A6 = BBRv3 is the same cells re-run on the BBRv3 kernel with --tag bbrv3)."""
    return [make_cell("exp6", f"{v}_{safe(m)}_b{b}", mix_groups(m, 50), buffer_bdp=b,
                      variant=v, mix=m, **kw)
            for v, kw in VARIANTS.items() for m in ["1:1", "4:4"] for b in (1, 4)]


EXPERIMENTS = {"exp0": exp0, "exp1": exp1, "exp2": exp2, "exp3": exp3,
               "exp4": exp4, "exp5": exp5, "exp6": exp6}

PROFILES = {
    "smoke": {"reps": {"exp1": 1}, "filter": lambda c: c["mix"] == "1:1", "duration": 20},
    "pilot": {"reps": {"exp1": 3, "exp5": 1}},
    "core":  {"reps": {"exp0": 10, "exp1": 10, "exp2": 5, "exp3": 5,
                       "exp4": 5, "exp5": 10, "exp6": 5}},
    "full":  {"reps": {e: 10 for e in EXPERIMENTS}},
    # Overnight budget (~402 runs, ~9 h): every experiment, more repetitions where the
    # paper's headline tests live (exp1 core 2x2, exp5 transients, exp0 validation).
    "paper": {"reps": {"exp0": 5, "exp1": 10, "exp2": 3, "exp3": 3,
                       "exp4": 3, "exp5": 5, "exp6": 3}},
    "a6_bbrv3": {"reps": {"exp1": 5, "exp6": 5},
                 "filter": lambda c: c["exp"] == "exp1" or c["variant"] in ("A0_fifo", "A3_fq_codel")},
}