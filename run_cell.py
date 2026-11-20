#!/usr/bin/env python3
"""Runs ONE experiment cell once: build -> measure at 10 Hz -> save -> teardown.
Debug use:  sudo python3 run_cell.py exp1__1v1_fifo out_test
"""
import csv, json, os, re, subprocess, sys, threading, time
import config as C
import topology as T

BASE_PORT = 5201
ADDR_RE = re.compile(r"(\d+\.\d+\.\d+\.\d+):(\d+)\s+(\d+\.\d+\.\d+\.\d+):(\d+)")
UNIT = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9}
FIELDS = [("srtt_ms", r"\brtt:([\d.]+)/", float), ("minrtt_ms", r"\bminrtt:([\d.]+)", float),
          ("cwnd", r"\bcwnd:(\d+)", int), ("retrans_total", r"\bretrans:\d+/(\d+)", int),
          ("bytes_acked", r"\bbytes_acked:(\d+)", int)]


def parse_ss_info(s):
    d = {}
    m = re.search(r"\b(cubic|bbr\d?|reno|dctcp|prague)\b", s)
    d["cc"] = m.group(1) if m else ""
    for key, rx, cast in FIELDS:
        m = re.search(rx, s)
        if m:
            d[key] = cast(m.group(1))
    m = re.search(r"\bdelivery_rate ([\d.]+)([KMG]?)bps", s)
    if m:
        d["delivery_bps"] = float(m.group(1)) * UNIT[m.group(2)]
    m = re.search(r"bbr:\(bw:([\d.]+)([KMG]?)bps,mrtt:([\d.]+)", s)
    if m:
        d["bbr_bw_bps"] = float(m.group(1)) * UNIT[m.group(2)]
        d["bbr_mrtt_ms"] = float(m.group(3))
    return d


def ss_sample(i, port):
    t = time.time()
    out = T.nsx(T.sender_ns(i), f"ss -tinH state established '( dport = :{port} )'",
                check=False, timeout=5)
    socks, cur = [], None
    for line in out.splitlines():
        m = ADDR_RE.search(line)
        if m:
            cur = {"local_port": int(m.group(2)), "info": line[m.end():]}
            socks.append(cur)
        elif cur is not None:
            cur["info"] += " " + line
    rows = []
    for s in socks:
        r = {"t": t, "group": i, "local_port": s["local_port"]}
        r.update(parse_ss_info(s["info"]))
        rows.append(r)
    return rows


def tc_sample(state):
    t = time.time()
    q = T.bottleneck_child(T.qdisc_json(T.NS_R, T.BOTTLENECK))
    if not q:
        return []
    return [{"t": t, "rate_mbit": state["rate"], "backlog_b": q.get("backlog", 0),
             "qlen": q.get("qlen", 0), "drops": q.get("drops", 0),
             "sent_pkts": q.get("packets", 0), "sent_bytes": q.get("bytes", 0),
             "ecn_mark": q.get("ecn_mark", 0)}]


def cpu_sample(state):
    with open("/proc/stat") as f:
        v = list(map(int, f.readline().split()[1:]))
    idle, tot = v[3] + v[4], sum(v[:8])
    prev, state["cpu"] = state.get("cpu"), (idle, tot)
    if not prev or tot == prev[1]:
        return []
    return [{"t": time.time(), "cpu_pct": 100 * (1 - (idle - prev[0]) / (tot - prev[1]))}]


class Sampler(threading.Thread):
    def __init__(self, fn, hz, sink):
        super().__init__(daemon=True)
        self.fn, self.period, self.sink = fn, 1.0 / hz, sink
        self.halt, self.errors = threading.Event(), 0

    def run(self):
        nxt = time.time()
        while not self.halt.is_set():
            try:
                self.sink.extend(self.fn())
            except Exception:
                self.errors += 1
            nxt += self.period
            delay = nxt - time.time()
            if delay > 0:
                self.halt.wait(delay)
            else:
                nxt = time.time()


def write_csv(path, rows):
    keys = sorted({k for r in rows for k in r} or {"t"}, key=lambda k: (k != "t", k))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def env_info():
    g = lambda c: T.sh(c, check=False).strip()
    return {"kernel": g("uname -r"), "iperf3": g("iperf3 --version | head -1"),
            "iproute2": g("tc -V"), "host": g("hostname"),
            "cc_available": g("cat /proc/sys/net/ipv4/tcp_available_congestion_control")}


def run(cell, outdir, rep=0, tag=""):
    os.makedirs(outdir, exist_ok=True)
    n, dur = len(cell["groups"]), cell["duration_s"]
    meta = {"cell": cell, "rep": rep, "tag": tag, "env": env_info(), "status": "fail", "errors": []}
    procs, files, samplers = [], [], []
    ss_rows, tc_rows, cpu_rows = [], [], []
    try:
        T.build(cell)
        meta["base_rtt_ms"] = [T.base_rtt_ms(i) for i in range(n)]
        for i in range(n):
            procs.append(subprocess.Popen(
                ["ip", "netns", "exec", T.NS_RCV, "iperf3", "-s", "-1", "-p", str(BASE_PORT + i)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(0.5)

        state = {"rate": cell["rate_mbit"]}
        samplers = [Sampler(lambda i=i: ss_sample(i, BASE_PORT + i), 10, ss_rows) for i in range(n)]
        samplers += [Sampler(lambda: tc_sample(state), 10, tc_rows),
                     Sampler(lambda: cpu_sample(state), 2, cpu_rows)]
        ping_f = open(os.path.join(outdir, "ping.txt"), "w"); files.append(ping_f)
        procs.append(subprocess.Popen(["ip", "netns", "exec", T.sender_ns(0), "ping", "-D", "-n",
                                       "-i", "0.1", T.RCV_IP], stdout=ping_f, stderr=subprocess.STDOUT))
        for s in samplers:
            s.start()

        t0 = time.time()
        meta["t0"] = t0
        launch, clients, ev_log = [None] * n, [None] * n, []

        def launcher(i, g):
            time.sleep(max(0.0, t0 + g["start_s"] - time.time()))
            out = open(os.path.join(outdir, f"iperf_g{i}.json"), "w")
            err = open(os.path.join(outdir, f"iperf_g{i}.err"), "w")
            files.extend([out, err])
            launch[i] = time.time()
            clients[i] = subprocess.Popen(
                ["ip", "netns", "exec", T.sender_ns(i), "iperf3", "-c", T.RCV_IP,
                 "-p", str(BASE_PORT + i), "-C", g["cc"], "-P", str(g["n"]),
                 "-t", str(int(dur - g["start_s"])), "-i", "0.1", "-J"], stdout=out, stderr=err)

        def events():
            for e in sorted(cell["events"], key=lambda e: e["t"]):
                time.sleep(max(0.0, t0 + e["t"] - time.time()))
                if e["type"] == "rate":
                    T.set_rate(e["mbit"]); state["rate"] = e["mbit"]
                elif e["type"] == "rtt":
                    for i in range(n):
                        T.set_rtt(i, e["ms"])
                ev_log.append({**e, "t_actual": time.time() - t0})

        threads = [threading.Thread(target=launcher, args=(i, g)) for i, g in enumerate(cell["groups"])]
        threads.append(threading.Thread(target=events))
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        rcs = [c.wait(timeout=dur + 30) for c in clients]
        time.sleep(0.5)

        meta.update(launch=launch, events_applied=ev_log, iperf_rc=rcs)
        meta["sender_qdisc_drops"] = sum(q.get("drops", 0) for i in range(n)
                                         for q in T.qdisc_json(T.sender_ns(i), f"s{i}-eth"))
        meta["bottleneck_final"] = T.bottleneck_child(T.qdisc_json(T.NS_R, T.BOTTLENECK))
    except Exception as e:
        meta["errors"].append(repr(e))
    finally:
        for s in samplers:
            s.halt.set()
        for p in procs:
            p.terminate()
        for f in files:
            f.close()
        T.teardown()

    write_csv(os.path.join(outdir, "ss.csv"), ss_rows)
    write_csv(os.path.join(outdir, "tc.csv"), tc_rows)
    write_csv(os.path.join(outdir, "cpu.csv"), cpu_rows)
    meta["sampler_errors"] = sum(s.errors for s in samplers)

    # ---------- validity checks (Phase 2 Step 3) ----------
    ports = {}
    for i in range(n):
        try:
            js = json.load(open(os.path.join(outdir, f"iperf_g{i}.json")))
            ports[i] = [c["local_port"] for c in js["start"]["connected"]]
            if js.get("error"):
                meta["errors"].append(f"g{i}: {js['error']}")
        except Exception as e:
            ports[i] = []
            meta["errors"].append(f"g{i} iperf json: {e!r}")
    meta["data_ports"] = ports
    cc_ok, meta["cc_seen"] = True, {}
    for i, g in enumerate(cell["groups"]):
        seen = {r.get("cc") for r in ss_rows if r["group"] == i and r["local_port"] in ports[i]} - {"", None}
        meta["cc_seen"][i] = sorted(seen)
        cc_ok &= seen == {g["cc"]}
    cpu = sorted(r["cpu_pct"] for r in cpu_rows)
    meta["cpu_p95"] = cpu[int(0.95 * (len(cpu) - 1))] if cpu else None
    meta["checks"] = {
        "iperf_ok": bool(meta.get("iperf_rc")) and all(rc == 0 for rc in meta["iperf_rc"]),
        "cc_match": cc_ok,
        "cpu_ok": meta["cpu_p95"] is not None and meta["cpu_p95"] < 80,
        "no_sender_drops": meta.get("sender_qdisc_drops", 1) == 0,     # flagged, not fatal
    }
    fatal = ("iperf_ok", "cc_match", "cpu_ok")
    meta["status"] = "ok" if all(meta["checks"][k] for k in fatal) and not meta["errors"] else "fail"
    with open(os.path.join(outdir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1, default=str)
    return meta


if __name__ == "__main__":
    cid, out = sys.argv[1], sys.argv[2]
    cell = next(c for f in C.EXPERIMENTS.values() for c in f() if c["cell_id"] == cid)
    m = run(cell, out)
    print(json.dumps({k: m.get(k) for k in ("status", "errors", "checks", "cc_seen",
                                             "base_rtt_ms", "cpu_p95")}, indent=1, default=str))