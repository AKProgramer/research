"""Dumbbell built from Linux network namespaces (Phase 2 Step 2).

 tb-s0 --(fq pacing)--> r-s0 \                         /--(htb 100M + pfifo|fq_codel|codel)--> tb-rcv
 tb-s1 --(fq pacing)--> r-s1 -> [ tb-r router ] r-out
 Base RTT = netem delay on r-s<i> egress (the ACK path), so netem never carries data packets.
"""
import json, re, subprocess

NS_R, NS_RCV = "tb-r", "tb-rcv"
RCV_IP = "10.0.100.1"
BOTTLENECK = "r-out"
MEMLIMIT = 67108864


def sh(cmd, check=True, timeout=30):
    r = subprocess.run(cmd, shell=True, text=True, capture_output=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"command failed ({r.returncode}): {cmd}\n{r.stderr.strip()}")
    return r.stdout


def nsx(ns, cmd, **kw):
    return sh(f"ip netns exec {ns} {cmd}", **kw)


def sender_ns(i):
    return f"tb-s{i}"


def teardown():
    for line in sh("ip netns list", check=False).splitlines():
        name = line.split()[0] if line.strip() else ""
        if name.startswith("tb-"):
            sh(f"ip netns pids {name} | xargs -r kill -9", check=False)
            sh(f"ip netns del {name}", check=False)


def _veth(a, ns_a, b, ns_b):
    sh(f"ip link del {a}", check=False)
    sh(f"ip link add {a} type veth peer name {b}")
    sh(f"ip link set {a} netns {ns_a}")
    sh(f"ip link set {b} netns {ns_b}")
    for dev, ns in ((a, ns_a), (b, ns_b)):
        nsx(ns, f"ethtool -K {dev} tso off gso off gro off", check=False)
        nsx(ns, f"ip link set {dev} up")


def _host_sysctls(ns, host_ecn):
    for kv in ("net.ipv4.tcp_no_metrics_save=1", f"net.ipv4.tcp_ecn={host_ecn}",
               'net.ipv4.tcp_rmem="4096 131072 67108864"',
               'net.ipv4.tcp_wmem="4096 16384 67108864"'):
        nsx(ns, f"sysctl -qw {kv}")


def build(cell):
    teardown()
    n = len(cell["groups"])
    for ns in [NS_R, NS_RCV] + [sender_ns(i) for i in range(n)]:
        sh(f"ip netns add {ns}")
        nsx(ns, "ip link set lo up")
    nsx(NS_R, "sysctl -qw net.ipv4.ip_forward=1")

    _veth(BOTTLENECK, NS_R, "rcv-eth", NS_RCV)
    nsx(NS_R, f"ip addr add 10.0.100.254/24 dev {BOTTLENECK}")
    nsx(NS_RCV, f"ip addr add {RCV_IP}/24 dev rcv-eth")
    nsx(NS_RCV, "ip route add default via 10.0.100.254")
    _host_sysctls(NS_RCV, cell["host_ecn"])

    for i in range(n):
        s = sender_ns(i)
        _veth(f"r-s{i}", NS_R, f"s{i}-eth", s)
        nsx(NS_R, f"ip addr add 10.0.{i+1}.254/24 dev r-s{i}")
        nsx(s, f"ip addr add 10.0.{i+1}.1/24 dev s{i}-eth")
        nsx(s, f"ip route add default via 10.0.{i+1}.254")
        _host_sysctls(s, cell["host_ecn"])
    configure_queues(cell)


def child_qdisc(cell):
    q, p, L = cell["queue"], cell["qparams"], cell["limit_pkts"]
    tgt, itv = p.get("target_ms", 5), p.get("interval_ms", 100)
    ecn = "ecn" if p.get("ecn") else "noecn"
    if q == "fifo":
        return f"pfifo limit {L}"
    if q == "fq_codel":
        return f"fq_codel limit {L} target {tgt}ms interval {itv}ms {ecn} memory_limit {MEMLIMIT}"
    if q == "fq_nocodel":   # same scheduler, CoDel never triggers (target 4 s > any queue we build)
        return f"fq_codel limit {L} target 4s interval 8s noecn memory_limit {MEMLIMIT}"
    if q == "codel":
        return f"codel limit {L} target {tgt}ms interval {itv}ms {ecn}"
    raise ValueError(q)


def configure_queues(cell):
    R = cell["rate_mbit"]
    nsx(NS_R, f"tc qdisc replace dev {BOTTLENECK} root handle 1: htb default 10")
    nsx(NS_R, f"tc class add dev {BOTTLENECK} parent 1: classid 1:10 htb rate {R}mbit ceil {R}mbit quantum 60000")
    nsx(NS_R, f"tc qdisc add dev {BOTTLENECK} parent 1:10 handle 10: {child_qdisc(cell)}")
    for i, g in enumerate(cell["groups"]):
        nsx(NS_R, f"tc qdisc replace dev r-s{i} root netem delay {g['rtt_ms']}ms limit 100000")
        pacing = "fq" if cell["sender_fq"] else "pfifo limit 10000"
        nsx(sender_ns(i), f"tc qdisc replace dev s{i}-eth root {pacing}")


def set_rate(mbit):
    nsx(NS_R, f"tc class change dev {BOTTLENECK} parent 1: classid 1:10 htb rate {mbit}mbit ceil {mbit}mbit quantum 60000")


def set_rtt(i, ms):
    nsx(NS_R, f"tc qdisc change dev r-s{i} root netem delay {ms}ms limit 100000")


def base_rtt_ms(i):
    out = nsx(sender_ns(i), f"ping -c 20 -i 0.05 -n -q {RCV_IP}", timeout=30)
    return float(re.search(r"= ([\d.]+)/", out).group(1))      # min RTT of idle path


def qdisc_json(ns, dev):
    return json.loads(nsx(ns, f"tc -s -j qdisc show dev {dev}", timeout=5) or "[]")


def bottleneck_child(stats):
    return next((q for q in stats if q.get("parent") == "1:10"),
                next((q for q in stats if q.get("kind") != "htb"), None))