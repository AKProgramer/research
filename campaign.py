#!/usr/bin/env python3
"""Runs a campaign (Phase 2 Step 7): randomised repetition blocks, resume, retries, log.
   sudo python3 campaign.py --profile smoke
   sudo python3 campaign.py --profile pilot
   sudo python3 campaign.py --profile core            (add --only exp1,exp6 to restrict)
"""
import argparse, csv, json, os, random, sys, time, traceback
import config as C
import run_cell as R
import topology as T


def build_plan(profile, only):
    P = C.PROFILES[profile]
    per_exp = []
    for exp, reps in P["reps"].items():
        if only and exp not in only:
            continue
        cells = C.EXPERIMENTS[exp]()
        if P.get("filter"):
            cells = [c for c in cells if P["filter"](c)]
        if P.get("duration"):
            for c in cells:
                c["duration_s"] = P["duration"]
        per_exp.append((cells, reps))
    plan = []
    for r in range(max((reps for _, reps in per_exp), default=0)):
        block = [(c, r) for cells, reps in per_exp if r < reps for c in cells]
        random.Random(1000 + r).shuffle(block)          # randomised order within each block
        plan += block
    return plan


def done(d):
    try:
        return json.load(open(os.path.join(d, "meta.json")))["status"] == "ok"
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="pilot", choices=list(C.PROFILES))
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default="data")
    ap.add_argument("--tag", default="", help="e.g. bbrv3 when running on the BBRv3 kernel")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    plan = build_plan(a.profile, set(filter(None, a.only.split(","))))
    print(f"{len(plan)} runs, estimated {sum(c['duration_s'] + 12 for c, _ in plan) / 3600:.1f} h")
    if a.dry_run:
        for c, r in plan:
            print(f"  {c['cell_id']}  rep{r}  limit={c['limit_pkts']}p  dur={c['duration_s']}s")
        return
    if os.geteuid() != 0:
        sys.exit("Run with sudo.")
    T.sh("modprobe tcp_bbr", check=False)
    if "bbr" not in open("/proc/sys/net/ipv4/tcp_available_congestion_control").read():
        sys.exit("BBR is not available in this kernel.")

    root = os.path.join(a.out, a.tag) if a.tag else a.out
    os.makedirs(root, exist_ok=True)
    log_path = os.path.join(root, "campaign_log.csv")
    new = not os.path.exists(log_path)
    try:
        with open(log_path, "a", newline="") as lf:
            w = csv.writer(lf)
            if new:
                w.writerow(["time", "exp", "cell_id", "rep", "attempt", "status", "errors"])
            for k, (cell, rep) in enumerate(plan, 1):
                d = os.path.join(root, cell["exp"], cell["cell_id"], f"rep{rep:02d}")
                if done(d):
                    continue
                for attempt in range(1, 4):
                    if os.path.exists(d):
                        os.rename(d, f"{d}.failed_{int(time.time())}")
                    try:
                        meta = R.run(cell, d, rep, a.tag)
                    except Exception:
                        meta = {"status": "fail", "errors": [traceback.format_exc()]}
                    w.writerow([time.strftime("%F %T"), cell["exp"], cell["cell_id"], rep,
                                attempt, meta["status"], " | ".join(map(str, meta["errors"]))[:500]])
                    lf.flush()
                    print(f"[{k}/{len(plan)}] {cell['cell_id']} rep{rep} try{attempt}: {meta['status']}",
                          flush=True)
                    time.sleep(C.GAP_S)
                    if meta["status"] == "ok":
                        break
    finally:
        T.teardown()


if __name__ == "__main__":
    main()