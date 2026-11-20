#!/usr/bin/env bash
# One-time host preparation for Ubuntu 22.04/24.04.  Run:  sudo bash setup_host.sh
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y iperf3 iproute2 ethtool iputils-ping python3 \
    python3-pandas python3-numpy python3-scipy python3-matplotlib python3-statsmodels

# Load congestion control + qdisc modules now and on every boot
for m in tcp_bbr sch_fq sch_fq_codel sch_codel sch_netem sch_htb; do modprobe "$m" || true; done
echo tcp_bbr > /etc/modules-load.d/bbr.conf

# Socket-buffer ceilings are global (not per namespace)
sysctl -w net.core.rmem_max=67108864 net.core.wmem_max=67108864
printf "net.core.rmem_max=67108864\nnet.core.wmem_max=67108864\n" > /etc/sysctl.d/90-cnet.conf

echo "------------- CHECKS (send me this output) -------------"
echo "kernel:   $(uname -r)"
echo "cc:       $(cat /proc/sys/net/ipv4/tcp_available_congestion_control)"
echo "iperf3:   $(iperf3 --version | head -1)"
echo "iproute2: $(tc -V)"
echo "cpus:     $(nproc)   ram: $(free -h | awk '/Mem/{print $2}')"
python3 -c "import pandas, numpy, scipy, matplotlib, statsmodels; print('python:   OK')"