#!/usr/bin/env python3
"""Turns campaign output into everything the paper reports (Phase 4).

    sudo python3 campaign.py --profile paper            # ~9 h, unattended
    python3 metrics.py data                              # raw runs -> data/summary.csv (+ ts.csv per run)
    python3 make_paper_assets.py data paper/generated    # -> figures, tables, numbers.tex

Writes figures (PDF for LaTeX + PNG preview), LaTeX tables (tab_*.tex) and numbers.tex,
which defines every number and data-dependent phrase that paper/main.tex prints via \\R{key}.
No result in the paper is typed by hand: re-running this after new data updates the paper.
A section whose data is missing is skipped with a warning; its \\R{} keys then render as a
red ?? in the PDF, so nothing silently goes missing.
"""
import glob, json, os, platform, re, sys, traceback
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm, TwoSlopeNorm
from scipy import stats

try:
    import statsmodels
    import statsmodels.formula.api as smf
    from statsmodels.stats.anova import anova_lm
except ImportError:          # ANOVA table is skipped, everything else still works
    smf = None

QUEUES = ["fifo", "fq_codel"]
QN = {"fifo": "FIFO", "fq_codel": "FQ-CoDel"}
QC = {"fifo": "#D55E00", "fq_codel": "#0072B2"}            # Okabe-Ito (colour-blind safe)
CCC = {"cubic": "#E69F00", "bbr": "#009E73"}
CCN = {"cubic": "CUBIC", "bbr": "BBR"}
MIXN = {"cubic2": "CUBIC only", "bbr2": "BBR only", "1:1": "CUBIC vs BBR"}
VARIANTS = ["A0_fifo", "A1_fq_only", "A2_codel_only", "A3_fq_codel", "A4_fq_codel_ecn",
            "A5a_target10", "A5b_target20", "A7a_fifo_nopace", "A7b_fqcodel_nopace"]
VARN = {"A0_fifo": "A0 FIFO", "A1_fq_only": "A1 FQ only", "A2_codel_only": "A2 CoDel only",
        "A3_fq_codel": "A3 FQ-CoDel", "A4_fq_codel_ecn": "A4 FQ-CoDel + ECN",
        "A5a_target10": "A5a target 10 ms", "A5b_target20": "A5b target 20 ms",
        "A7a_fifo_nopace": "A7a FIFO, no fq", "A7b_fqcodel_nopace": "A7b FQ-CoDel, no fq"}
# Practical-significance thresholds fixed before the experiments (Phase 2, Step 9)
THR = {"jfi": 0.05, "qdelay_p95_ms": 5.0, "util": 0.02}

COLW, TEXTW = 3.45, 7.0        # IEEE column / text width (inches)
plt.rcParams.update({
    "font.family": "serif", "font.size": 8, "axes.titlesize": 8, "axes.labelsize": 8,
    "legend.fontsize": 7, "xtick.labelsize": 7, "ytick.labelsize": 7,
    "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
    "grid.alpha": 0.25, "grid.linewidth": 0.5, "savefig.dpi": 300, "pdf.fonttype": 42,
    "legend.frameon": False, "lines.linewidth": 0.9})


# ----------------------------------------------------------------- helpers
def esc(s):
    return re.sub(r"([_&%#$])", r"\\\1", str(s))


def num(x):
    return pd.to_numeric(pd.Series(x), errors="coerce").dropna().values.astype(float)


def ci(x):
    """mean, 95% t-interval half-width, n  (t-interval: honest at the small n used here)."""
    x = num(x)
    n = len(x)
    if n == 0:
        return np.nan, np.nan, 0
    if n < 2 or x.std(ddof=1) == 0:
        return x.mean(), (0.0 if n >= 2 else np.nan), n
    return x.mean(), stats.t.ppf(0.975, n - 1) * x.std(ddof=1) / np.sqrt(n), n


def mean(x):
    return ci(x)[0]


def f(x, d=2):
    return "--" if x is None or not np.isfinite(x) else f"{x:.{d}f}"


def pm(x, d=2):
    m, h, _ = ci(x)
    if not np.isfinite(m):
        return "--"
    return f"{m:.{d}f}" if not np.isfinite(h) else f"{m:.{d}f}\\,$\\pm$\\,{h:.{d}f}"


def fp(p):
    if p is None or not np.isfinite(p):
        return "n/a"
    return "$p<0.001$" if p < 0.001 else f"$p={p:.3f}$"


def welch(a, b):
    a, b = num(a), num(b)
    if len(a) < 2 or len(b) < 2 or (a.std() == 0 and b.std() == 0):
        return np.nan
    return float(stats.ttest_ind(a, b, equal_var=False).pvalue)


def cliffs(a, b):
    """Cliff's delta: P(a>b) - P(a<b)."""
    a, b = num(a), num(b)
    if not len(a) or not len(b):
        return np.nan
    return float(np.sign(a[:, None] - b[None, :]).mean())


def holm(ps):
    ps = np.asarray(ps, float)
    order = np.argsort(np.where(np.isnan(ps), np.inf, ps))
    m, adj, run = int((~np.isnan(ps)).sum()), np.full(len(ps), np.nan), 0.0
    for r, i in enumerate(order):
        if np.isnan(ps[i]):
            continue
        run = max(run, min(1.0, (m - r) * ps[i]))
        adj[i] = run
    return adj


def sigphrase(p, delta, thr):
    sig = p is not None and np.isfinite(p) and p < 0.05
    prac = delta is not None and np.isfinite(delta) and abs(delta) >= thr
    if sig and prac:
        return "statistically significant and above the pre-registered practical threshold"
    if sig:
        return "statistically significant but below the pre-registered practical threshold"
    if prac:
        return "above the practical threshold but not statistically significant"
    return "neither statistically significant nor above the practical threshold"


def verdict(conds):
    conds = [c for c in conds if c is not None]
    if not conds:
        return "not determinable"
    k = sum(bool(c) for c in conds)
    return "supported" if k == len(conds) else ("not supported" if k == 0 else "partially supported")


def sub(d, **kw):
    for k, v in kw.items():
        if k not in d:
            return d.iloc[0:0]
        d = d[d[k] == v]
    return d


def col(d, c):
    return d[c] if c in d else pd.Series(dtype=float)


def tabular(spec, header, rows):
    out = [f"\\begin{{tabular}}{{{spec}}}", "\\toprule"]
    out += header if isinstance(header, list) and header and header[0].endswith("\\\\") \
        else [" & ".join(header) + " \\\\"]
    out.append("\\midrule")
    for r in rows:
        out.append(r if isinstance(r, str) else " & ".join(map(str, r)) + " \\\\")
    out += ["\\bottomrule", "\\end{tabular}"]
    return "\n".join(out) + "\n"


SAMPLE_NOTE = "SAMPLE - random values, NOT experimental results"


class Out:
    def __init__(self, d, sample=False):
        self.d, self.nums, self.sample = d, {}, sample
        os.makedirs(d, exist_ok=True)

    def __setitem__(self, k, v):
        self.nums[k] = v

    def head(self):
        return (f"% {SAMPLE_NOTE}\n" if self.sample else "") + "% generated by make_paper_assets.py -- do not edit\n"

    def table(self, name, body):
        with open(os.path.join(self.d, f"{name}.tex"), "w", encoding="utf-8") as fh:
            fh.write(self.head() + body)

    def fig(self, fig, name):
        if self.sample:
            fig.text(0.5, 0.5, "SAMPLE - NOT REAL DATA", rotation=15, fontsize=20, color="red",
                     alpha=0.3, ha="center", va="center", transform=fig.transFigure, zorder=100)
        for ext in ("pdf", "png"):
            fig.savefig(os.path.join(self.d, f"{name}.{ext}"), bbox_inches="tight")
        plt.close(fig)

    def close(self):
        if self.sample:
            self.nums["sample"] = "1"       # main.tex then watermarks every page
        with open(os.path.join(self.d, "numbers.tex"), "w", encoding="utf-8") as fh:
            fh.write(self.head())
            for k, v in sorted(self.nums.items()):
                fh.write(f"\\expandafter\\def\\csname r@{k}\\endcsname{{{v}}}\n")


def ts_runs(root, cell_id):
    exp = cell_id.split("__")[0]
    paths = [p for p in sorted(glob.glob(os.path.join(root, exp, cell_id, "rep*", "ts.csv")))
             if re.search(r"rep\d+$", os.path.basename(os.path.dirname(p)))]
    return [pd.read_csv(p, index_col="sec") for p in paths]


def cell_of(d, **kw):
    g = sub(d, **kw)
    return g.cell_id.iloc[0] if len(g) else None


def bars(ax, groups, labels, metric, k=1.0, data=None, ylabel=""):
    """groups: list of dicts of filters; one bar per queue in each group."""
    x, w = np.arange(len(groups)), 0.38
    for j, q in enumerate(QUEUES):
        st = [ci(col(sub(data, queue=q, **g), metric) * k) for g in groups]
        ax.bar(x + (j - 0.5) * w, [s[0] for s in st], w, color=QC[q], label=QN[q],
               yerr=np.nan_to_num([s[1] for s in st]), capsize=1.5, error_kw={"lw": 0.6})
    ax.set_xticks(x, labels)
    ax.set_ylabel(ylabel)
    ax.grid(axis="x", visible=False)


def ts_panel(ax, ts, events=(), cap=None, title="", qdelay=True, ylim=None):
    for cc in ("cubic", "bbr"):
        c = f"mbps_{cc}"
        if c in ts:
            ax.plot(ts.index, ts[c], color=CCC[cc], label=CCN[cc])
    if cap is not None:
        ax.plot(cap[0], cap[1], color="0.3", lw=0.6, ls="--", label="capacity")
    for t in events:
        ax.axvline(t, color="0.6", lw=0.5, ls="--")
    ax.set_title(title)
    if ylim:
        ax.set_ylim(*ylim)
    ax2 = None
    if qdelay and "qdelay_ms" in ts:
        ax2 = ax.twinx()
        ax2.plot(ts.index, ts.qdelay_ms, color="0.35", lw=0.5, ls=":", label="queue delay")
        ax2.grid(False)
        ax2.spines["right"].set_visible(True)
        ax2.spines["top"].set_visible(False)
    return ax2


SECTIONS = []


def section(fn):
    SECTIONS.append(fn)
    return fn


# ----------------------------------------------------------------- environment / validity
@section
def environment(df, root, out):
    out["nruns"] = len(df)
    out["ncells"] = df.cell_id.nunique()
    for e, g in df.groupby("exp"):
        reps = g.groupby("cell_id").size()
        out[f"nruns-{e}"] = len(g)
        out[f"ncells-{e}"] = g.cell_id.nunique()
        out[f"reps-{e}"] = f"{reps.min()}" if reps.min() == reps.max() else f"{reps.min()}--{reps.max()}"
    metas = sorted(glob.glob(os.path.join(root, "exp*", "*", "rep*", "meta.json")))
    env, cpu95 = {}, []
    for mp in metas:
        try:
            m = json.load(open(mp))
        except Exception:
            continue
        if m.get("status") == "ok":
            env = env or m.get("env", {})
            if m.get("cpu_p95") is not None:
                cpu95.append(float(m["cpu_p95"]))
    out["kernel"] = esc(env.get("kernel", "?"))
    out["iperf"] = esc(" ".join(env.get("iperf3", "?").split()[:2]))
    mt = re.search(r"iproute2-([\w.]+)", env.get("iproute2", ""))
    out["iproute"] = esc(mt.group(1) if mt else env.get("iproute2", "?"))
    out["cpu-p95-max"] = f(max(cpu95), 1) if cpu95 else "--"
    try:
        info = open("/proc/cpuinfo").read()
        out["cpu"] = esc(re.search(r"model name\s*:\s*(.+)", info).group(1).strip())
        mem = int(re.search(r"MemTotal:\s*(\d+)", open("/proc/meminfo").read()).group(1))
        out["ram"] = f"{mem / 1048576:.0f}"
    except Exception:
        out["cpu"], out["ram"] = esc(platform.processor() or "?"), "?"
    out["ncpu"] = os.cpu_count()
    out["python"] = platform.python_version()
    out["pandas"], out["numpy"] = pd.__version__, np.__version__
    out["scipy"] = __import__("scipy").__version__
    out["matplotlib"] = matplotlib.__version__
    out["statsmodels"] = statsmodels.__version__ if smf else "n/a"
    try:
        os_rel = dict(l.strip().split("=", 1) for l in open("/etc/os-release") if "=" in l)
        out["os"] = esc(os_rel.get("PRETTY_NAME", "?").strip('"'))
    except Exception:
        out["os"] = esc(platform.platform())
    logp = os.path.join(root, "campaign_log.csv")
    if os.path.exists(logp):
        log = pd.read_csv(logp)
        out["attempts"] = len(log)
        out["failattempts"] = int((log.status != "ok").sum())
    else:
        out["attempts"], out["failattempts"] = "--", "--"
    out["flag-util"] = int(df.get("flag_util_lt_90", pd.Series(dtype=bool)).fillna(False).astype(bool).sum())
    out["flag-sdrops"] = int(df.get("flag_sender_drops", pd.Series(dtype=bool)).fillna(False).astype(bool).sum())
    err = (df.base_rtt_g0 - df.rtt0).abs()
    out["rtt-err"] = f(err.median(), 2)
    out["rtt-err-max"] = f(err.max(), 2)


# ----------------------------------------------------------------- Exp-0 validation
@section
def validation(df, root, out):
    d = df[df.exp == "exp0"]
    rows = []
    for rep, lab in (("rep8", "Setting of [8]"), ("rep7", "Setting of [7]")):
        for q in QUEUES:
            g = d[d.cell_id.str.contains(f"{rep}_{q}$", regex=True)]
            if rep == "rep8":
                share = col(g, "bbr_share")
                harm = ((share < 0.2) | (share > 0.8)).sum()
                extra = [pm(share * 100, 1), f"{harm}/{len(share)}"]
                out[f"val8-share-{q}"] = f(mean(share) * 100, 1)
                out[f"val8-harm-{q}"] = f"{harm}/{len(share)}"
                out[f"val8-jfi-{q}"] = f(mean(col(g, "jfi")), 3)
                out[f"val8-d95-{q}"] = f(mean(col(g, "qdelay_p95_ms")), 1)
            else:
                ratio = col(g, "tput_g0") / col(g, "tput_g1")
                extra = [pm(ratio, 2), "--"]
                out[f"val7-ratio-{q}"] = f(mean(ratio), 2)
                out[f"val7-jfi-{q}"] = f(mean(col(g, "jfi")), 3)
                out[f"val7-d95-{q}"] = f(mean(col(g, "qdelay_p95_ms")), 1)
            rows.append([lab if q == "fifo" else "", QN[q], len(g), pm(col(g, "util"), 3),
                         pm(col(g, "jfi"), 3)] + extra + [pm(col(g, "qdelay_p95_ms"), 1)])
        rows.append("\\midrule")
    out.table("tab_validation", tabular(
        "llrccccc", ["Setting", "Queue", "$n$", "$U$", "$J$",
                     "$S_\\mathrm{BBR}$ (\\%) / $\\bar{x}_{10}/\\bar{x}_{50}$", "Harm", "$d_{95}$ (ms)"],
        rows[:-1]))


# ----------------------------------------------------------------- Exp-1 core 2x2
CORE = [("util", "$U$", 3, 1), ("jfi", "$J$", 3, 1), ("bbr_share", "$S_\\mathrm{BBR}$ (\\%)", 1, 100),
        ("qdelay_p95_ms", "$d_{95}$ (ms)", 1, 1), ("ping_p95_ms", "$\\mathrm{RTT}_{95}$ (ms)", 1, 1),
        ("drop_rate", "Drop (\\%)", 2, 100), ("retx_rate", "Retx (\\%)", 2, 100)]


@section
def core(df, root, out):
    d = df[df.exp == "exp1"]
    rows = []
    for mix in ["cubic2", "bbr2", "1:1"]:
        for q in QUEUES:
            g = sub(d, mix=mix, queue=q)
            rows.append([MIXN[mix] if q == "fifo" else "", QN[q]] +
                        [pm(col(g, c) * k, dd) for c, _, dd, k in CORE])
        rows.append("\\midrule")
    out.table("tab_core", tabular("ll" + "c" * len(CORE), ["Flows", "Queue"] + [h for _, h, _, _ in CORE],
                                  rows[:-1]))

    a, b = sub(d, mix="1:1", queue="fifo"), sub(d, mix="1:1", queue="fq_codel")
    out["e1-n"] = min(len(a), len(b))
    keys = [("util", "util", 3, 1), ("jfi", "jfi", 3, 1), ("bbr_share", "share", 1, 100),
            ("qdelay_p95_ms", "d95", 1, 1), ("qdelay_p50_ms", "d50", 1, 1), ("ping_p95_ms", "ping95", 1, 1),
            ("drop_rate", "drop", 2, 100), ("retx_rate", "retx", 2, 100), ("osc_amp", "osc", 3, 1)]
    ps = []
    for c, key, dd, k in keys:
        ma, mb = mean(col(a, c) * k), mean(col(b, c) * k)
        out[f"e1-{key}-fifo"], out[f"e1-{key}-fq"] = f(ma, dd), f(mb, dd)
        out[f"e1-{key}-fifo-ci"], out[f"e1-{key}-fq-ci"] = pm(col(a, c) * k, dd), pm(col(b, c) * k, dd)
        out[f"e1-{key}-cd"] = f(cliffs(col(b, c), col(a, c)), 2)
        ps.append(welch(col(a, c), col(b, c)))
    for (c, key, _, _), p in zip(keys, holm(ps)):
        out[f"e1-{key}-p"] = fp(p)
    pd_ = dict(zip([k[1] for k in keys], holm(ps)))
    dj = mean(col(b, "jfi")) - mean(col(a, "jfi"))
    dd95 = mean(col(b, "qdelay_p95_ms")) - mean(col(a, "qdelay_p95_ms"))
    du = mean(col(b, "util")) - mean(col(a, "util"))
    out["e1-jfi-dabs"] = f(dj, 3)
    out["e1-jfi-rel"] = f(dj / mean(col(a, "jfi")) * 100, 1)
    out["e1-d95-rel"] = f(dd95 / mean(col(a, "qdelay_p95_ms")) * 100, 1)
    # standing queue of BBR-only traffic in RTTmin units (comparison with [5])
    for q in QUEUES:
        g = sub(d, mix="bbr2", queue=q)
        out[f"e1-bbr2-d50norm-{q}"] = f(mean(col(g, "qdelay_p50_ms") / col(g, "rtt0")), 2)
    out["e1-util-dpp"] = f(du * 100, 1)
    out["e1-d95-ratio"] = f(mean(col(a, "qdelay_p95_ms")) / mean(col(b, "qdelay_p95_ms")), 1)
    out["e1-ping-red-pct"] = f((1 - mean(col(b, "ping_p95_ms")) / mean(col(a, "ping_p95_ms"))) * 100, 1)
    out["e1-jfi-sig"] = sigphrase(pd_["jfi"], dj, THR["jfi"])
    out["e1-d95-sig"] = sigphrase(pd_["d95"], dd95, THR["qdelay_p95_ms"])
    out["e1-util-sig"] = sigphrase(pd_["util"], du, THR["util"])
    sf, sq = mean(col(a, "bbr_share")), mean(col(b, "bbr_share"))
    who = lambda s: "BBR" if s > 0.55 else ("CUBIC" if s < 0.45 else "neither algorithm")
    out["e1-dom-fifo"], out["e1-dom-fq"] = who(sf), who(sq)

    # interaction as difference-in-differences against the homogeneous references
    for c, key, dd in (("jfi", "jfi", 3), ("qdelay_p95_ms", "d95", 1), ("util", "util", 3)):
        hom = lambda q: mean(pd.concat([col(sub(d, mix="cubic2", queue=q), c), col(sub(d, mix="bbr2", queue=q), c)]))
        did = (mean(col(b, c)) - mean(col(a, c))) - (hom("fq_codel") - hom("fifo"))
        out[f"e1-did-{key}"] = f(did, dd)

    # base-paper comparisons (numbers reported by [2]: J>0.9, U>0.98)
    jq, uq = mean(col(b, "jfi")), mean(col(b, "util"))
    out["cmp2-jfi-abs"] = f(jq - 0.90, 3)
    out["cmp2-jfi-rel"] = f((jq - 0.90) / 0.90 * 100, 1)
    out["cmp2-util-abs"] = f((uq - 0.98) * 100, 1)

    fig, axs = plt.subplots(1, 4, figsize=(TEXTW, 1.55), constrained_layout=True)
    groups = [{"mix": m} for m in ("cubic2", "bbr2", "1:1")]
    labels = ["CUBIC\nonly", "BBR\nonly", "CUBIC\nvs BBR"]
    for ax, (c, lab, k) in zip(axs, [("util", "Utilisation $U$", 1), ("jfi", "Jain index $J$", 1),
                                     ("qdelay_p95_ms", "$d_{95}$ queue delay (ms)", 1),
                                     ("retx_rate", "Retransmissions (%)", 100)]):
        bars(ax, groups, labels, c, k, d, lab)
    lows = [mean(col(sub(d, mix=g["mix"], queue=q), "util")) for g in groups for q in QUEUES]
    lows = [v for v in lows if np.isfinite(v)]
    axs[0].set_ylim(max(0.0, min(lows) - 0.08) if lows else 0, 1.0)
    axs[1].set_ylim(0, 1.02)
    h, l = axs[0].get_legend_handles_labels()
    fig.legend(h, l, loc="outside upper center", ncol=2)
    out.fig(fig, "fig_core")

    # qualitative example: one run per queue + latency CDF over all runs
    fig, axs = plt.subplots(1, 3, figsize=(TEXTW, 1.75), constrained_layout=True,
                            gridspec_kw={"width_ratios": [1, 1, 0.8]})
    for ax, q in zip(axs[:2], QUEUES):
        runs = ts_runs(root, cell_of(d, mix="1:1", queue=q) or "x__x")
        if runs:
            ax2 = ts_panel(ax, runs[0], title=f"{QN[q]}, rep. 0", ylim=(0, 105))
            if ax2 is not None:
                ax2.set_ylim(0, max(5, runs[0].qdelay_ms.max() * 1.1))
                if q == "fq_codel":
                    ax2.set_ylabel("Queue delay (ms)")
        ax.set_xlabel("Time (s)")
    axs[0].set_ylabel("Throughput (Mbit/s)")
    axs[0].legend(loc="upper right", ncol=2)
    for q in QUEUES:
        runs = ts_runs(root, cell_of(d, mix="1:1", queue=q) or "x__x")
        v = np.sort(np.concatenate([r.loc[(r.index >= 10) & (r.index < 58), "ping_ms"].dropna().values
                                    for r in runs if "ping_ms" in r] or [np.array([])]))
        if len(v):
            axs[2].plot(v, np.arange(1, len(v) + 1) / len(v), color=QC[q], label=QN[q])
    axs[2].set_xlabel("Probe RTT under load (ms)")
    axs[2].set_ylabel("CDF")
    axs[2].legend(loc="lower right")
    out.fig(fig, "fig_ts_core")


# ----------------------------------------------------------------- Exp-2 regime sweep
@section
def regime(df, root, out):
    d = df[df.exp == "exp2"].copy()
    B = sorted(d.buffer_bdp.dropna().unique())
    R = sorted(d.rtt0.dropna().unique())
    grid = lambda q, c, k=1: np.array([[mean(col(sub(d, queue=q, buffer_bdp=b, rtt0=r), c) * k) for b in B] for r in R])

    fig, axs = plt.subplots(2, 2, figsize=(TEXTW * 0.72, 3.3), constrained_layout=True)
    sh = [grid(q, "bbr_share", 100) for q in QUEUES]
    dl = [grid(q, "qdelay_p95_ms") for q in QUEUES]
    allv = np.concatenate([x.ravel() for x in dl])
    allv = allv[np.isfinite(allv) & (allv > 0)]
    lnorm = LogNorm(vmin=max(0.1, allv.min()), vmax=max(1.0, allv.max())) if len(allv) else None
    for j, q in enumerate(QUEUES):
        for i, (Z, cmap, norm, fmt) in enumerate([(sh[j], "PuOr", TwoSlopeNorm(50, 0, 100), "{:.0f}"),
                                                  (dl[j], "viridis", lnorm, "{:.1f}")]):
            ax = axs[i, j]
            im = ax.imshow(Z, cmap=cmap, norm=norm, aspect="auto", origin="lower")
            for (r, b), v in np.ndenumerate(Z):
                if np.isfinite(v):
                    dark = abs(v - 50) > 30 if i == 0 else norm(v) < 0.5      # viridis is dark at the low end
                    ax.text(b, r, fmt.format(v), ha="center", va="center", fontsize=6.5,
                            color="white" if dark else "black")
            ax.set_xticks(range(len(B)), [f"{b:g}" for b in B])
            ax.set_yticks(range(len(R)), [f"{r:g}" for r in R])
            ax.grid(False)
            ax.set_title(f"{QN[q]}: " + ("BBR share $S_\\mathrm{BBR}$ (%)" if i == 0 else "$d_{95}$ (ms)"))
            if i == 1:
                ax.set_xlabel("Buffer (× BDP)")
            if j == 0:
                ax.set_ylabel("Base RTT (ms)")
            if j == 1:
                fig.colorbar(im, ax=axs[i, :], shrink=0.9, pad=0.02)
    out.fig(fig, "fig_regime")

    # table: J and U for every buffer x RTT x queue
    rows = []
    for b in B:
        for q in QUEUES:
            g = sub(d, buffer_bdp=b, queue=q)
            rows.append([f"{b:g}" if q == "fifo" else "", QN[q]] +
                        [f(mean(col(sub(g, rtt0=r), "jfi")), 3) for r in R] +
                        [f(mean(col(sub(g, rtt0=r), "util")), 3) for r in R] +
                        [f(mean(col(sub(g, rtt0=r), "drop_rate") * 100), 2) for r in R])
    hdr = ["\\multirow{2}{*}{Buffer}", "\\multirow{2}{*}{Queue}",
           f"\\multicolumn{{{len(R)}}}{{c}}{{Jain index $J$}}", f"\\multicolumn{{{len(R)}}}{{c}}{{Utilisation $U$}}",
           f"\\multicolumn{{{len(R)}}}{{c}}{{Drop rate (\\%)}}"]
    sub_hdr = ["", ""] + [f"{r:g} ms" for r in R] * 3
    n = len(R)
    cm = "".join(f"\\cmidrule(lr){{{3 + k * n}-{2 + (k + 1) * n}}}" for k in range(3))
    out.table("tab_regime", tabular("ll" + "c" * 3 * n,
                                    [" & ".join(hdr) + " \\\\", cm, " & ".join(sub_hdr) + " \\\\"], rows))

    # H1
    fifo = sub(d, queue="fifo").dropna(subset=["bbr_share"])
    rho, p = stats.spearmanr(fifo.buffer_bdp, fifo.bbr_share) if len(fifo) > 3 else (np.nan, np.nan)
    lo = mean(fifo[fifo.buffer_bdp <= 1].bbr_share)
    hi = mean(fifo[fifo.buffer_bdp >= 4].bbr_share)
    fq = sub(d, queue="fq_codel")
    jb = fq.groupby("buffer_bdp").jfi.mean()
    jmin, jbuf = (jb.min(), jb.idxmin()) if len(jb) else (np.nan, np.nan)
    c1 = (rho < 0 and p < 0.05) if np.isfinite(rho) else None
    c2 = (lo > 0.5 and hi < 0.5) if np.isfinite(lo) and np.isfinite(hi) else None
    c3 = (jmin >= 0.95) if np.isfinite(jmin) else None
    out["h1-rho"], out["h1-p"] = f(rho, 2), fp(p)
    out["h1-share-lo"], out["h1-share-hi"] = f(lo * 100, 1), f(hi * 100, 1)
    out["h1-jmin"], out["h1-jmin-buf"] = f(jmin, 3), f"{jbuf:g}" if np.isfinite(jbuf) else "--"
    out["h1-verdict"] = verdict([c1, c2, c3])
    out["h1-trend"] = "decreased" if (np.isfinite(rho) and rho < 0) else "did not decrease"
    out["h1-sentence"] = (
        f"Under FIFO the BBR share {out.nums['h1-trend']} with buffer size "
        f"(Spearman $\\rho={f(rho, 2)}$, {fp(p)}); BBR received {f(lo * 100, 1)}\\% of the capacity at "
        f"$\\le$1~BDP and {f(hi * 100, 1)}\\% at $\\ge$4~BDP; the lowest FQ-CoDel Jain index over all "
        f"buffer sizes was {f(jmin, 3)} (at {out.nums['h1-jmin-buf']}~BDP), which "
        f"{'meets' if c3 else 'does not meet'} the 0.95 criterion")
    # crossover buffer under FIFO (smallest buffer at which CUBIC holds the majority)
    sb = fifo.groupby("buffer_bdp").bbr_share.mean()
    cross = sb[sb < 0.5]
    out["h1-cross"] = f"{cross.index.min():g}" if len(cross) else "none of the tested sizes"

    # H4 (deep buffers: >= 2 BDP)
    ok, n, ratios, losses = 0, 0, [], []
    for b in [x for x in B if x >= 2]:
        for r in R:
            df_, dq = mean(col(sub(d, queue="fifo", buffer_bdp=b, rtt0=r), "qdelay_p95_ms")), \
                mean(col(sub(d, queue="fq_codel", buffer_bdp=b, rtt0=r), "qdelay_p95_ms"))
            uf, uq = mean(col(sub(d, queue="fifo", buffer_bdp=b, rtt0=r), "util")), \
                mean(col(sub(d, queue="fq_codel", buffer_bdp=b, rtt0=r), "util"))
            if not all(np.isfinite([df_, dq, uf, uq])):
                continue
            n += 1
            ratios.append(df_ / max(dq, 1e-3))
            losses.append(uf - uq)
            ok += (df_ / max(dq, 1e-3) >= 10) and (uf - uq < 0.05)
    out["h4-ncells"], out["h4-nok"] = n, ok
    out["h4-ratio-min"] = f(min(ratios), 1) if ratios else "--"
    out["h4-ratio-med"] = f(float(np.median(ratios)), 1) if ratios else "--"
    out["h4-uloss-max"] = f(max(losses) * 100, 1) if losses else "--"
    out["h4-verdict"] = "not determinable" if n == 0 else (
        "supported" if ok == n else ("partially supported" if ok >= n / 2 else "not supported"))

    # mechanism: BBR min-RTT inflation and oscillation vs buffer
    fig, axs = plt.subplots(1, 2, figsize=(COLW, 1.45), constrained_layout=True)
    for q in QUEUES:
        for ax, c in zip(axs, ("bbr_mrtt_infl", "osc_amp")):
            st = [ci(col(sub(d, queue=q, buffer_bdp=b), c)) for b in B]
            ax.errorbar(range(len(B)), [s[0] for s in st], yerr=np.nan_to_num([s[1] for s in st]),
                        color=QC[q], marker="o", ms=2.5, capsize=1.5, label=QN[q])
    for ax, lab in zip(axs, ("BBR min-RTT / base RTT", "Osc. amplitude of $S_\\mathrm{BBR}$")):
        ax.set_xticks(range(len(B)), [f"{b:g}" for b in B])
        ax.set_xlabel("Buffer (× BDP)")
        ax.set_ylabel(lab)
    axs[0].legend(loc="upper left")
    out.fig(fig, "fig_mech")
    for q in QUEUES:
        g = sub(d, queue=q)
        out[f"mech-mrtt-max-{q}"] = f(g.groupby("buffer_bdp").bbr_mrtt_infl.mean().max(), 2) if "bbr_mrtt_infl" in g else "--"
        out[f"mech-osc-{q}"] = f(mean(col(g, "osc_amp")), 3)
        out[f"e2-d95-max-{q}"] = f(g.groupby(["buffer_bdp", "rtt0"]).qdelay_p95_ms.mean().max(), 1)
        out[f"e2-util-min-{q}"] = f(g.groupby(["buffer_bdp", "rtt0"]).util.mean().min(), 3)
        out[f"e2-jfi-min-{q}"] = f(g.groupby(["buffer_bdp", "rtt0"]).jfi.mean().min(), 3)
    mf = sub(d, queue="fifo")
    if "bbr_mrtt_infl" in mf and mf.bbr_mrtt_infl.notna().sum() > 3:
        r_, p_ = stats.spearmanr(mf.bbr_mrtt_infl, mf.bbr_share, nan_policy="omit")
        out["mech-rho"], out["mech-p"] = f(r_, 2), fp(p_)

    # factorial ANOVA (+ rank-transform robustness check)
    if smf is None:
        return
    terms = ["C(queue)", "C(buffer_bdp)", "C(rtt0)", "C(queue):C(buffer_bdp)", "C(queue):C(rtt0)",
             "C(buffer_bdp):C(rtt0)", "C(queue):C(buffer_bdp):C(rtt0)"]
    tname = {"C(queue)": "Queue (Q)", "C(buffer_bdp)": "Buffer (B)", "C(rtt0)": "RTT (R)",
             "C(queue):C(buffer_bdp)": "Q$\\times$B", "C(queue):C(rtt0)": "Q$\\times$R",
             "C(buffer_bdp):C(rtt0)": "B$\\times$R", "C(queue):C(buffer_bdp):C(rtt0)": "Q$\\times$B$\\times$R"}
    models = [("jfi", "J", lambda y: y), ("bbr_share", "S", lambda y: y),
              ("qdelay_p95_ms", "D", lambda y: np.log10(np.clip(y, 0.01, None)))]
    res, agree, total, shap = {}, 0, 0, []
    for c, tag, tr in models:
        dd = d[[c, "queue", "buffer_bdp", "rtt0"]].dropna().copy()
        if len(dd) < 20:
            continue
        dd["y"] = tr(dd[c])
        fit = smf.ols("y ~ C(queue)*C(buffer_bdp)*C(rtt0)", data=dd).fit()
        a = anova_lm(fit, typ=2)
        ssr = a.loc["Residual", "sum_sq"]
        dd["y"] = dd.y.rank()
        ar = anova_lm(smf.ols("y ~ C(queue)*C(buffer_bdp)*C(rtt0)", data=dd).fit(), typ=2)
        if len(fit.resid) >= 3:
            shap.append(stats.shapiro(fit.resid).pvalue)
        for t in terms:
            if t in a.index:
                eta = a.loc[t, "sum_sq"] / (a.loc[t, "sum_sq"] + ssr)
                res[(t, tag)] = (a.loc[t, "F"], a.loc[t, "PR(>F)"], eta)
                total += 1
                agree += (a.loc[t, "PR(>F)"] < 0.05) == (ar.loc[t, "PR(>F)"] < 0.05)
    rows = []
    for t in terms:
        r = [tname[t]]
        for _, tag, _ in models:
            F, p, eta = res.get((t, tag), (np.nan,) * 3)
            r += [f(F, 1), ("$<$0.001" if p < 0.001 else f(p, 3)) if np.isfinite(p) else "--", f(eta, 2)]
        rows.append(r)
    hdr = ["\\multirow{2}{*}{Term}", "\\multicolumn{3}{c}{Jain index $J$}",
           "\\multicolumn{3}{c}{BBR share $S_\\mathrm{BBR}$}", "\\multicolumn{3}{c}{$\\log_{10} d_{95}$}"]
    cm = "\\cmidrule(lr){2-4}\\cmidrule(lr){5-7}\\cmidrule(lr){8-10}"
    sub_hdr = [""] + ["$F$", "$p$", "$\\eta_p^2$"] * 3
    out.table("tab_anova", tabular("l" + "rrr" * 3, [" & ".join(hdr) + " \\\\", cm, " & ".join(sub_hdr) + " \\\\"], rows))
    for tag in ("J", "S", "D"):
        for t, short in (("C(queue)", "q"), ("C(queue):C(buffer_bdp)", "qb"), ("C(queue):C(rtt0)", "qr")):
            F, p, eta = res.get((t, tag), (np.nan,) * 3)
            out[f"anova-{short}-{tag}-eta"], out[f"anova-{short}-{tag}-p"] = f(eta, 2), fp(p)
    out["anova-agree"], out["anova-total"] = agree, total
    out["anova-shapiro-min"] = fp(min(shap)) if shap else "n/a"
    out["anova-shapiro-word"] = ("deviated from" if shap and min(shap) < 0.05 else "were consistent with")


# ----------------------------------------------------------------- Exp-3 flow scaling
@section
def flows(df, root, out):
    d = df[df.exp == "exp3"]
    mixes = [m for m in ["1:4", "2:2", "4:4", "8:8", "4:1"] if m in set(d.mix)]
    rows = []
    for m in mixes:
        for b in (1.0, 4.0):
            g = sub(d, mix=m, buffer_bdp=b)
            fair = mean(col(g, "fair_share"))
            r = [m if b == 1 else "", f"{b:g}", f(fair * 100, 1)]
            for q in QUEUES:
                gq = sub(g, queue=q)
                r += [f(mean(col(gq, "bbr_share")) * 100, 1), f(mean(col(gq, "jfi")), 3),
                      f(mean(col(gq, "qdelay_p95_ms")), 1)]
            rows.append(r)
    hdr = ["\\multirow{2}{*}{C:B}", "\\multirow{2}{*}{Buf.}", "\\multirow{2}{*}{$F$ (\\%)}",
           "\\multicolumn{3}{c}{FIFO}", "\\multicolumn{3}{c}{FQ-CoDel}"]
    cm = "\\cmidrule(lr){4-6}\\cmidrule(lr){7-9}"
    sub_hdr = ["", "", ""] + ["$S_\\mathrm{BBR}$", "$J$", "$d_{95}$"] * 2
    out.table("tab_flows", tabular("llr" + "rrr" * 2, [" & ".join(hdr) + " \\\\", cm, " & ".join(sub_hdr) + " \\\\"], rows))

    fig, axs = plt.subplots(1, 2, figsize=(COLW, 1.5), constrained_layout=True, sharey=True)
    for ax, b in zip(axs, (1.0, 4.0)):
        bars(ax, [{"mix": m, "buffer_bdp": b} for m in mixes], mixes, "share_excess", 100, d)
        ax.axhline(0, color="0.3", lw=0.5)
        ax.set_title(f"{b:g} BDP")
        ax.set_xlabel("CUBIC:BBR flows")
    axs[0].set_ylabel("$S_\\mathrm{BBR}-F$ (pp)")
    axs[0].legend(loc="best")
    out.fig(fig, "fig_flows")
    for q in QUEUES:
        g = sub(d, queue=q)
        out[f"e3-excess-max-{q}"] = f(g.groupby(["mix", "buffer_bdp"]).share_excess.mean().abs().max() * 100, 1)
        out[f"e3-jfi-min-{q}"] = f(g.groupby(["mix", "buffer_bdp"]).jfi.mean().min(), 3)
        for b in (1.0, 4.0):
            gg = sub(g, mix="8:8", buffer_bdp=b)
            out[f"e3-jfi-8-{q}-b{b:g}"] = f(mean(col(gg, "jfi")), 3)
            out[f"e3-share-8-{q}-b{b:g}"] = f(mean(col(gg, "bbr_share")) * 100, 1)


# ----------------------------------------------------------------- Exp-4 RTT heterogeneity
@section
def rtt_het(df, root, out):
    d = df[df.exp == "exp4"]
    rows = []
    for mix, lab in (("cubic@10+bbr@50", "CUBIC 10 / BBR 50"), ("cubic@50+bbr@10", "CUBIC 50 / BBR 10")):
        for b in (1.0, 4.0):
            r = [lab if b == 1 else "", f"{b:g}"]
            for q in QUEUES:
                g = sub(d, mix=mix, buffer_bdp=b, queue=q)
                r += [pm(col(g, "bbr_share") * 100, 1), f(mean(col(g, "jfi")), 3), f(mean(col(g, "qdelay_p95_ms")), 1)]
                key = "c10" if mix.startswith("cubic@10") else "c50"
                out[f"e4-share-{key}-{q}-b{b:g}"] = f(mean(col(g, "bbr_share")) * 100, 1)
                out[f"e4-jfi-{key}-{q}-b{b:g}"] = f(mean(col(g, "jfi")), 3)
            rows.append(r)
    hdr = ["\\multirow{2}{*}{Base RTTs (ms)}", "\\multirow{2}{*}{Buf.}",
           "\\multicolumn{3}{c}{FIFO}", "\\multicolumn{3}{c}{FQ-CoDel}"]
    cm = "\\cmidrule(lr){3-5}\\cmidrule(lr){6-8}"
    sub_hdr = ["", ""] + ["$S_\\mathrm{BBR}$ (\\%)", "$J$", "$d_{95}$"] * 2
    out.table("tab_rtt", tabular("ll" + "crr" * 2, [" & ".join(hdr) + " \\\\", cm, " & ".join(sub_hdr) + " \\\\"], rows))
    for q in QUEUES:
        out[f"e4-jfi-min-{q}"] = f(sub(d, queue=q).groupby(["mix", "buffer_bdp"]).jfi.mean().min(), 3)


# ----------------------------------------------------------------- Exp-5 transients
def recovery(ts, cc, te=60, lo=70, hi=88, hold=3):
    c = f"mbps_{cc}"
    if c not in ts:
        return np.nan
    s = ts[c]
    target = s[(s.index >= lo) & (s.index < hi)].mean()
    run = 0
    for sec, v in s[s.index >= te].items():
        run = run + 1 if v >= 0.9 * target else 0
        if run >= hold:
            return float(sec - hold + 1 - te)
    return np.nan


def conv(s):
    s = pd.to_numeric(pd.Series(s), errors="coerce")
    n, k = len(s), int(s.notna().sum())
    if n == 0:
        return "--"
    if k == 0:
        return "n.c."
    txt = pm(s.dropna(), 1)
    return txt if k == n else f"{txt}$^{{({k}/{n})}}$"


@section
def transients(df, root, out):
    d = df[df.exp == "exp5"].copy()
    d["scen"] = d.cell_id.str.split("__").str[1].str.split("_").str[0]
    events = [("capstep", "rate50", "Capacity 100$\\rightarrow$50 Mbit/s"),
              ("capstep", "rate100", "Capacity 50$\\rightarrow$100 Mbit/s"),
              ("rttstep", "rtt60", "RTT 20$\\rightarrow$60 ms"),
              ("bbrlate", "latejoin", "BBR joins at 20 s"),
              ("cubiclate", "latejoin", "CUBIC joins at 20 s")]
    rows = []
    for scen, lab, desc in events:
        for q in QUEUES:
            g = sub(d, scen=scen, queue=q)
            post = pm(col(g, "bbr_share") * 100, 1) if lab == "latejoin" else "--"
            rows.append([desc if q == "fifo" else "", QN[q], conv(col(g, f"{lab}_conv_util_s")),
                         pm(col(g, f"{lab}_util_next10"), 3), conv(col(g, f"{lab}_conv_jfi_s")),
                         pm(col(g, f"{lab}_ping_peak_ms"), 1), post])
            k = f"{scen}-{lab}-{q}"
            out[f"e5-tu-{k}"] = f(mean(col(g, f"{lab}_conv_util_s")), 1)
            out[f"e5-u10-{k}"] = f(mean(col(g, f"{lab}_util_next10")), 3)
            out[f"e5-tj-{k}"] = f(mean(col(g, f"{lab}_conv_jfi_s")), 1)
            out[f"e5-peak-{k}"] = f(mean(col(g, f"{lab}_ping_peak_ms")), 1)
            if lab == "latejoin":
                out[f"e5-share-{scen}-{q}"] = f(mean(col(g, "bbr_share")) * 100, 1)
                out[f"e5-jfi-{scen}-{q}"] = f(mean(col(g, "jfi")), 3)
        rows.append("\\midrule")
    out.table("tab_transient", tabular(
        "llccccc", ["Event", "Queue", "$T_U$ (s)", "$\\bar U_{10}$", "$T_J$ (s)", "Peak RTT (ms)",
                    "$S_\\mathrm{BBR}$ after (\\%)"], rows[:-1]))

    for q in QUEUES:
        g = sub(d, scen="rttstep", queue=q)
        out[f"e5-rttdip-{q}"] = f((1 - mean(col(g, "rtt60_util_next10"))) * 100, 1)
        out[f"e5-capconv-{q}"] = f(mean(col(sub(d, scen="capstep", queue=q), "rate100_conv_util_s")), 1)

    # per-algorithm recovery after the capacity increase (H3)
    rec = {}
    for q in QUEUES:
        runs = ts_runs(root, cell_of(d, scen="capstep", queue=q) or "x__x")
        for cc in ("cubic", "bbr"):
            v = [recovery(r, cc) for r in runs]
            rec[(cc, q)] = np.nanmean(np.where(np.isnan(v), 30.0, v)) if len(v) else np.nan
            out[f"h3-rec-{cc}-{q}"] = f(rec[(cc, q)], 1)
    cu = lambda q: np.nan_to_num(mean(col(sub(d, scen="capstep", queue=q), "rate100_conv_util_s").fillna(30.0)), nan=np.nan)
    a = (cu("fq_codel") > cu("fifo")) if np.isfinite(cu("fq_codel")) and np.isfinite(cu("fifo")) else None
    b = (rec[("cubic", "fq_codel")] > rec[("cubic", "fifo")]) if rec and np.isfinite(rec.get(("cubic", "fifo"), np.nan)) else None
    c = (rec[("bbr", "fq_codel")] < rec[("cubic", "fq_codel")]) if rec and np.isfinite(rec.get(("bbr", "fq_codel"), np.nan)) else None
    out["h3-verdict"] = verdict([a, b, c])
    out["h3-sentence"] = (
        f"after the capacity increase, aggregate utilisation took {f(cu('fq_codel'), 1)}~s to settle under FQ-CoDel "
        f"versus {f(cu('fifo'), 1)}~s under FIFO; CUBIC recovered in {out.nums.get('h3-rec-cubic-fq_codel', '--')}~s "
        f"under FQ-CoDel versus {out.nums.get('h3-rec-cubic-fifo', '--')}~s under FIFO, and BBR in "
        f"{out.nums.get('h3-rec-bbr-fq_codel', '--')}~s under FQ-CoDel")

    fig, axs = plt.subplots(3, 2, figsize=(TEXTW, 3.7), constrained_layout=True, sharex=True)
    spec = [("capstep", [30, 60], ([0, 30, 30, 60, 60, 90], [100, 100, 50, 50, 100, 100]), "capacity step"),
            ("rttstep", [30], None, "RTT step 20$\\rightarrow$60 ms"),
            ("bbrlate", [20], None, "BBR joins at 20 s")]
    for i, (scen, ev, cap, lab) in enumerate(spec):
        for j, q in enumerate(QUEUES):
            runs = ts_runs(root, cell_of(d, scen=scen, queue=q) or "x__x")
            ax = axs[i, j]
            if runs:
                ax2 = ts_panel(ax, runs[0], events=ev, cap=cap, title=f"{QN[q]}: {lab}", ylim=(0, 105))
                if ax2 is not None and j == 1:
                    ax2.set_ylabel("Queue delay (ms)")
            if j == 0:
                ax.set_ylabel("Mbit/s")
            if i == 2:
                ax.set_xlabel("Time (s)")
    axs[0, 0].legend(loc="lower left", ncol=3)
    out.fig(fig, "fig_transient")


# ----------------------------------------------------------------- Exp-6 ablation
@section
def ablation(df, root, out):
    d = df[df.exp == "exp6"].copy()
    d["abs_excess"] = d.share_excess.abs() if "share_excess" in d else np.nan
    conds = [(m, b) for m in ("1:1", "4:4") for b in (1.0, 4.0)]
    M = lambda v, c, m=None, b=None: mean(col(sub(d, variant=v, **({"mix": m} if m else {}),
                                                  **({"buffer_bdp": b} if b else {})), c))

    rows = []
    for m in ("1:1", "4:4"):
        rows.append(f"\\multicolumn{{11}}{{l}}{{\\emph{{{'1 CUBIC vs 1 BBR' if m == '1:1' else '4 CUBIC vs 4 BBR'}}}}} \\\\")
        for v in VARIANTS:
            r = [VARN[v]]
            for c, dd, k in (("jfi", 3, 1), ("abs_excess", 1, 100), ("qdelay_p95_ms", 1, 1), ("util", 3, 1),
                             ("retx_rate", 2, 100)):
                r += [f(M(v, c, m, b) * k, dd) for b in (1.0, 4.0)]
            rows.append(r)
        rows.append("\\midrule")
    hdr = ["\\multirow{2}{*}{Variant}", "\\multicolumn{2}{c}{$J$}", "\\multicolumn{2}{c}{$|S_\\mathrm{BBR}-F|$ (pp)}",
           "\\multicolumn{2}{c}{$d_{95}$ (ms)}", "\\multicolumn{2}{c}{$U$}", "\\multicolumn{2}{c}{Retx (\\%)}"]
    cm = "".join(f"\\cmidrule(lr){{{2 + 2 * k}-{3 + 2 * k}}}" for k in range(5))
    sub_hdr = [""] + ["1 BDP", "4 BDP"] * 5
    out.table("tab_ablation", tabular("l" + "rr" * 5, [" & ".join(hdr) + " \\\\", cm, " & ".join(sub_hdr) + " \\\\"],
                                      rows[:-1]))

    fig, axs = plt.subplots(1, 3, figsize=(TEXTW, 1.75), constrained_layout=True)
    x, w = np.arange(len(VARIANTS)), 0.38
    for ax, (c, lab, k) in zip(axs, [("jfi", "Jain index $J$", 1), ("abs_excess", "$|S_\\mathrm{BBR}-F|$ (pp)", 100),
                                     ("qdelay_p95_ms", "$d_{95}$ queue delay (ms)", 1)]):
        for j, b in enumerate((1.0, 4.0)):
            st = [ci(col(sub(d, variant=v, buffer_bdp=b), c) * k) for v in VARIANTS]
            ax.bar(x + (j - 0.5) * w, [s[0] for s in st], w, yerr=np.nan_to_num([s[1] for s in st]),
                   color=["#999999", "#000000"][j], alpha=0.85 if j else 0.6, label=f"{b:g} BDP",
                   capsize=1.2, error_kw={"lw": 0.5})
        ax.set_xticks(x, [v.split("_")[0] for v in VARIANTS], rotation=0)
        ax.set_ylabel(lab)
        ax.grid(axis="x", visible=False)
        if c == "qdelay_p95_ms":
            ax.set_yscale("log")
    axs[0].legend(loc="lower left")
    out.fig(fig, "fig_ablation")

    # H2: attribute FQ-CoDel's fairness gain and delay reduction to its two components
    gain = lambda v: np.nanmean([M(v, "jfi", m, b) - M("A0_fifo", "jfi", m, b) for m, b in conds])
    red = lambda v: np.nanmean([M("A0_fifo", "qdelay_p95_ms", m, b) - M(v, "qdelay_p95_ms", m, b) for m, b in conds])
    g3, r3 = gain("A3_fq_codel"), red("A3_fq_codel")
    fq_fair = gain("A1_fq_only") / g3 if np.isfinite(g3) and g3 >= 0.02 else np.nan
    cd_fair = gain("A2_codel_only") / g3 if np.isfinite(g3) and g3 >= 0.02 else np.nan
    cd_del = red("A2_codel_only") / r3 if np.isfinite(r3) and r3 >= 5 else np.nan
    fq_del = red("A1_fq_only") / r3 if np.isfinite(r3) and r3 >= 5 else np.nan
    out["h2-gain"], out["h2-red"] = f(g3, 3), f(r3, 1)
    out["h2-fq-frac"], out["h2-codel-fair-frac"] = f(fq_fair * 100, 0), f(cd_fair * 100, 0)
    out["h2-codel-frac"], out["h2-fq-del-frac"] = f(cd_del * 100, 0), f(fq_del * 100, 0)
    c1 = (fq_fair >= 0.5) if np.isfinite(fq_fair) else None
    c2 = (cd_del >= 0.5) if np.isfinite(cd_del) else None
    out["h2-verdict"] = verdict([c1, c2])
    fair_src = ("flow isolation" if np.isfinite(fq_fair) and fq_fair >= 0.5 else
                "CoDel's dropping" if np.isfinite(cd_fair) and cd_fair >= 0.5 else "neither component alone")
    del_src = ("CoDel's sojourn-time control" if np.isfinite(cd_del) and cd_del >= 0.5 else
               "flow isolation" if np.isfinite(fq_del) and fq_del >= 0.5 else "neither component alone")
    out["h2-fair-src"], out["h2-del-src"] = fair_src, del_src

    # why BBR may still dominate under FQ-CoDel ([6]): compare A1 vs A3 and per-CCA loss under A3
    pooled = lambda v, c: np.nanmean([M(v, c, m, b) for m, b in conds])
    for v, k in (("A0_fifo", "a0"), ("A1_fq_only", "a1"), ("A2_codel_only", "a2"), ("A3_fq_codel", "a3"),
                 ("A4_fq_codel_ecn", "a4"), ("A5a_target10", "a5a"), ("A5b_target20", "a5b"),
                 ("A7a_fifo_nopace", "a7a"), ("A7b_fqcodel_nopace", "a7b")):
        out[f"abl-jfi-{k}"] = f(pooled(v, "jfi"), 3)
        out[f"abl-excess-{k}"] = f(np.nanmean([M(v, "share_excess", m, b) for m, b in conds]) * 100, 1)
        out[f"abl-d95-{k}"] = f(pooled(v, "qdelay_p95_ms"), 1)
        out[f"abl-util-{k}"] = f(pooled(v, "util"), 3)
        out[f"abl-retx-{k}"] = f(pooled(v, "retx_rate") * 100, 2)
        out[f"abl-retxc-{k}"] = f(pooled(v, "retx_rate_cubic") * 100, 2)
        out[f"abl-retxb-{k}"] = f(pooled(v, "retx_rate_bbr") * 100, 2)
        out[f"abl-ecn-{k}"] = f(pooled(v, "ecn_mark_rate") * 100, 2)
    rc, rb = pooled("A3_fq_codel", "retx_rate_cubic"), pooled("A3_fq_codel", "retx_rate_bbr")
    out["abl-loss-word"] = ("more heavily on CUBIC" if rc > rb * 1.1 else
                            "more heavily on BBR" if rb > rc * 1.1 else "about equally on both algorithms")


# ----------------------------------------------------------------- failure analysis (Phase 2 Step 11)
@section
def failures(df, root, out):
    d = df[df.exp.isin(["exp0", "exp1", "exp2", "exp3", "exp4"]) & df.queue.isin(QUEUES)]
    cells = d.groupby(["cell_id", "queue"]).mean(numeric_only=True).reset_index()
    mixed = cells[cells.fair_share.notna()] if "fair_share" in cells else cells.iloc[0:0]

    def harm(r):
        F, S = r.fair_share, r.bbr_share
        return not (0.4 <= S / F <= 1.6 and 0.4 <= (1 - S) / (1 - F) <= 1.6)
    crit = [("Jain index $J<0.8$", mixed, lambda r: r.jfi < 0.8),
            ("Group share outside 0.4--1.6$\\times$ fair share", mixed, harm),
            ("$d_{95}>100$ ms", cells, lambda r: r.qdelay_p95_ms > 100),
            ("Utilisation $U<0.9$", cells, lambda r: r.util < 0.9),
            ("Retransmission rate $>1$\\%", cells, lambda r: r.retx_rate > 0.01)]
    rows = []
    for lab, data, fn in crit:
        r = [lab]
        for q in QUEUES:
            g = data[data.queue == q]
            k = int(sum(bool(fn(x)) for x in g.itertuples()))
            r.append(f"{k}/{len(g)}")
        rows.append(r)
    out.table("tab_failures", tabular("lcc", ["Failure criterion (cell mean)", "FIFO", "FQ-CoDel"], rows))
    for q in QUEUES:
        g = mixed[mixed.queue == q]
        hk = [x for x in g.itertuples() if harm(x)]
        out[f"fail-harm-{q}"] = f"{len(hk)}/{len(g)}"
        infl = [x for x in hk if getattr(x, "bbr_mrtt_infl", np.nan) > 1.25]
        out[f"fail-harm-infl-{q}"] = len(infl)
        out[f"fail-harm-n-{q}"] = len(hk)
        out[f"fail-harm-shallow-{q}"] = sum(1 for x in hk if x.buffer_bdp <= 1 and x.bbr_share > x.fair_share)
        out[f"fail-delay-{q}"] = int((cells[cells.queue == q].qdelay_p95_ms > 100).sum())


# ----------------------------------------------------------------- main
def main(root, dest, sample=False):
    path = os.path.join(root, "summary.csv")
    if not os.path.exists(path):
        sys.exit(f"{path} not found - run `python3 metrics.py {root}` first.")
    df = pd.read_csv(path)
    if "tag" in df:
        out_tag = df.tag.astype(object).fillna("").astype(str)
        df = df[out_tag.isin(["", "nan"])].copy()        # BBRv3 (tagged) runs are kept out of the main analysis
    df["buffer_bdp"] = pd.to_numeric(df.buffer_bdp, errors="coerce")
    df["rtt0"] = df.rtt.astype(str).str.split("/").str[0].astype(float)
    out = Out(dest, sample)
    for fn in SECTIONS:
        try:
            fn(df, root, out)
            print(f"  ok   {fn.__name__}")
        except Exception:
            print(f"  FAIL {fn.__name__} (its \\R{{}} keys will show ?? in the PDF):")
            traceback.print_exc()
    out.close()
    print(f"{len(out.nums)} numbers, tables and figures written to {dest}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--sample"]     # --sample: format demo from synthetic data
    main(args[0] if args else "data", args[1] if len(args) > 1 else os.path.join("paper", "generated"),
         sample="--sample" in sys.argv)
