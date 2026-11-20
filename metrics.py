#!/usr/bin/env python3
"""Raw run folders -> ts.csv per run + summary.csv (Phase 2 Steps 3-4).
   python3 metrics.py data            (no sudo needed)
"""
import glob, json, os, re, sys
import numpy as np
import pandas as pd
import config as C

PING_RE = re.compile(r"\[(\d+\.\d+)\].*time=([\d.]+) ms")


def jain(x):
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    s2 = (x ** 2).sum()
    return float(x.sum() ** 2 / (len(x) * s2)) if len(x) and s2 > 0 else np.nan


def cap_at(cell, t):
    c = cell["rate_mbit"]
    for e in sorted(cell["events"], key=lambda e: e["t"]):
        if e["type"] == "rate" and t >= e["t"]:
            c = e["mbit"]
    return c


def steady_window(cell):
    if cell["events"]:                                   # pre-event steady state
        return C.TRIM_HEAD_S, min(e["t"] for e in cell["events"])
    late = max(g["start_s"] for g in cell["groups"])
    return max(C.TRIM_HEAD_S, late + C.TRIM_HEAD_S), cell["duration_s"] - C.TRIM_TAIL_S


def event_list(cell):
    ev = [(e["t"], f"{e['type']}{e.get('mbit', e.get('ms'))}") for e in cell["events"]]
    return ev + [(g["start_s"], "latejoin") for g in cell["groups"] if g["start_s"] > 0]


def settle(series, te, thr, hold=5):
    """Seconds after te until series >= thr for `hold` consecutive seconds (NaN = never)."""
    run = 0
    for sec, v in series[series.index >= te].items():
        run = run + 1 if v >= thr else 0
        if run >= hold:
            return float(sec - hold + 1 - te)
    return np.nan


def read_csv(path):
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame(columns=["t"])


def load_flows(run, meta):
    rows = []
    for i, g in enumerate(meta["cell"]["groups"]):
        js = json.load(open(os.path.join(run, f"iperf_g{i}.json")))
        off = meta["launch"][i] - meta["t0"]
        for iv in js.get("intervals", []):
            for st in iv["streams"]:
                mid = off + (st["start"] + st["end"]) / 2
                rows.append((int(np.floor(mid)), f"g{i}s{st['socket']}", i, g["cc"],
                             st["bytes"], st.get("retransmits", 0)))
    df = pd.DataFrame(rows, columns=["sec", "flow", "group", "cc", "bytes", "retrans"])
    per = df.groupby(["sec", "flow", "group", "cc"], as_index=False)[["bytes", "retrans"]].sum()
    per["mbps"] = per.bytes * 8 / 1e6
    return per


def summarise(run):
    meta = json.load(open(os.path.join(run, "meta.json")))
    if meta.get("status") != "ok":
        return None, None
    cell, t0, base = meta["cell"], meta["t0"], meta["base_rtt_ms"]
    per = load_flows(run, meta)

    tc = read_csv(os.path.join(run, "tc.csv")); tc["t"] -= t0
    tc["qdelay_ms"] = tc.backlog_b * 8 / (tc.rate_mbit * 1e6) * 1e3
    ss = read_csv(os.path.join(run, "ss.csv")); ss["t"] -= t0
    valid = {(int(g), p) for g, ps in meta["data_ports"].items() for p in ps}
    ss = ss[[(g, p) in valid for g, p in zip(ss.group, ss.local_port)]].copy()
    ss["base"] = ss.group.map(lambda g: base[int(g)])
    ping = pd.DataFrame([(float(m.group(1)) - t0, float(m.group(2)))
                         for m in PING_RE.finditer(open(os.path.join(run, "ping.txt")).read())],
                        columns=["t", "rtt"])

    q = cell["qparams"]
    m = {"exp": cell["exp"], "cell_id": cell["cell_id"], "mix": cell["mix"],
         "variant": cell["variant"], "queue": cell["queue"], "buffer_bdp": cell["buffer_bdp"],
         "limit_pkts": cell["limit_pkts"], "rtt": "/".join(str(g["rtt_ms"]) for g in cell["groups"]),
         "dynamics": cell["dynamics"], "host_ecn": cell["host_ecn"], "sender_fq": cell["sender_fq"],
         "target_ms": q.get("target_ms", 5), "tag": meta.get("tag", ""), "rep": meta["rep"],
         "kernel": meta["env"]["kernel"], "base_rtt_g0": base[0], "run": run}

    # ---------------- steady-state metrics ----------------
    a, b = steady_window(cell)
    w = per[(per.sec >= a) & (per.sec < b)]
    x = w.groupby(["flow", "cc"]).mbps.mean()
    ccs = x.index.get_level_values("cc")
    cap, N, tot = cap_at(cell, a), len(x), x.sum()
    m.update(win_start=a, win_end=b, n_flows=N, util=tot / cap, jfi=jain(x.values))
    n_bbr = int((ccs == "bbr").sum())
    if 0 < n_bbr < N:
        m["bbr_share"] = x[ccs == "bbr"].sum() / tot
        m["fair_share"] = n_bbr / N
        m["share_excess"] = m["bbr_share"] - m["fair_share"]
        s = w.groupby(["sec", "cc"]).mbps.sum().unstack(fill_value=0)
        m["osc_amp"] = float((s["bbr"] / s.sum(axis=1)).std())
    fair = cap / N
    for cc in sorted(set(ccs)):
        xc, wc = x[ccs == cc], w[w.cc == cc]
        m[f"tput_{cc}"] = xc.mean()
        m[f"harm_fs_{cc}"] = max(0.0, (fair - xc.mean()) / fair)
        m[f"retx_rate_{cc}"] = wc.retrans.sum() / max(1.0, wc.bytes.sum() / C.MSS)
        sc = ss[(ss.t >= a) & (ss.t < b) & (ss.cc == cc)]
        if len(sc):
            m[f"srtt_p50_{cc}"] = sc.srtt_ms.median()
            m[f"srtt_p95_{cc}"] = sc.srtt_ms.quantile(0.95)
            m[f"rtt_infl_p95_{cc}"] = (sc.srtt_ms / sc.base).quantile(0.95)
    for i in range(len(cell["groups"])):
        m[f"tput_g{i}"] = w[w.group == i].groupby("flow").mbps.mean().mean()
    m["retx_rate"] = w.retrans.sum() / max(1.0, w.bytes.sum() / C.MSS)
    m["tput_cov"] = w.groupby("flow").mbps.agg(
        lambda s: s.std() / s.mean() if s.mean() > 0 else np.nan).mean()

    tw = tc[(tc.t >= a) & (tc.t < b)]
    if len(tw) > 1:
        for p in (50, 95, 99):
            m[f"qdelay_p{p}_ms"] = float(np.percentile(tw.qdelay_ms, p))
        d_drop = tw.drops.iloc[-1] - tw.drops.iloc[0]
        d_sent = tw.sent_pkts.iloc[-1] - tw.sent_pkts.iloc[0]
        m["drop_rate"] = d_drop / max(1, d_drop + d_sent)
        m["ecn_mark_rate"] = (tw.ecn_mark.iloc[-1] - tw.ecn_mark.iloc[0]) / max(1, d_sent)
    pw = ping[(ping.t >= a) & (ping.t < b)]
    if len(pw):
        for p in (50, 95, 99):
            m[f"ping_p{p}_ms"] = float(np.percentile(pw.rtt, p))
        m["ping_infl_p95"] = m["ping_p95_ms"] / base[0]
    if "bbr_mrtt_ms" in ss:
        sb = ss[(ss.t >= a) & (ss.t < b) & ss.bbr_mrtt_ms.notna()]
        if len(sb):
            m["bbr_mrtt_infl"] = (sb.bbr_mrtt_ms / sb.base).median()
    m["flag_util_lt_90"] = bool(cell["dynamics"] == "static" and m["util"] < 0.9)
    m["flag_sender_drops"] = meta.get("sender_qdisc_drops", 0) > 0

    # ---------------- per-second series + transient metrics ----------------
    sec_tot = per.groupby("sec").mbps.sum()
    util_s = sec_tot / np.array([cap_at(cell, s + 0.5) for s in sec_tot.index])
    jfi_s = per.groupby("sec").mbps.apply(lambda v: jain(v.values))
    for te, lab in event_list(cell):
        m[f"{lab}_conv_util_s"] = settle(util_s, te, 0.9)
        m[f"{lab}_conv_jfi_s"] = settle(jfi_s, te, 0.9)
        m[f"{lab}_util_next10"] = util_s[(util_s.index >= te) & (util_s.index < te + 10)].mean()
        m[f"{lab}_ping_peak_ms"] = ping[(ping.t >= te) & (ping.t < te + 10)].rtt.max()
        m[f"{lab}_qdelay_peak_ms"] = tc[(tc.t >= te) & (tc.t < te + 10)].qdelay_ms.max()

    ts = pd.DataFrame({"util": util_s, "jfi": jfi_s})
    ts = ts.join(per.groupby(["sec", "cc"]).mbps.sum().unstack(fill_value=0).add_prefix("mbps_"))
    for i in range(len(cell["groups"])):
        ts[f"mbps_g{i}"] = per[per.group == i].groupby("sec").mbps.sum()
    tc["sec"] = np.floor(tc.t).astype(int)
    ts = ts.join(tc.groupby("sec").qdelay_ms.median())
    if len(ping):
        ping["sec"] = np.floor(ping.t).astype(int)
        ts = ts.join(ping.groupby("sec").rtt.median().rename("ping_ms"))
    if "bbr_mrtt_ms" in ss and ss.bbr_mrtt_ms.notna().any():
        ss["sec"] = np.floor(ss.t).astype(int)
        ts = ts.join(ss.groupby("sec").bbr_mrtt_ms.median())
    ts.index.name = "sec"
    return m, ts


def main(root):
    rows, bad = [], 0
    for mp in sorted(glob.glob(os.path.join(root, "**", "meta.json"), recursive=True)):
        run = os.path.dirname(mp)
        try:
            m, ts = summarise(run)
        except Exception as e:
            print(f"! {run}: {e!r}"); bad += 1; continue
        if m is None:
            bad += 1; continue
        ts.to_csv(os.path.join(run, "ts.csv"))
        rows.append(m)
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(root, "summary.csv"), index=False)
    print(f"{len(df)} good runs, {bad} failed/skipped -> {os.path.join(root, 'summary.csv')}")
    if len(df):
        cols = [c for c in ["cell_id", "util", "jfi", "bbr_share", "qdelay_p95_ms",
                            "ping_p95_ms", "drop_rate", "retx_rate"] if c in df]
        print(df[cols].groupby("cell_id").mean(numeric_only=True).round(3).to_string())


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data")