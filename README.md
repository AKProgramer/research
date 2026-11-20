# TCP CUBIC/BBR coexistence under FIFO and FQ-CoDel

This is a controlled factorial testbed for measuring how TCP CUBIC and BBR share a bottleneck under
FIFO (pfifo) and FQ-CoDel queues. It varies buffer size, RTT, flow count, RTT heterogeneity and
network dynamics, and includes an ablation that separates flow isolation from CoDel. Everything
runs on one Ubuntu host using Linux network namespaces.

| File | Role |
|---|---|
| `config.py` | experiment definitions (Exp-0 to Exp-6) and run profiles |
| `topology.py` | namespace dumbbell: htb shaping, pfifo / fq_codel / codel, netem on the ACK path |
| `run_cell.py` | runs one cell: build, sample at 10 Hz (iperf3, ss, tc, ping), validity checks |
| `campaign.py` | randomised repetition blocks, resume, retries, log |
| `metrics.py` | raw runs to `summary.csv` and per-run `ts.csv` |
| `make_paper_assets.py` | `summary.csv` to every figure, table and number in the paper |
| `paper/` | IEEE LaTeX paper (`main.tex`, `refs.bib`) |

## Reproduce

```bash
sudo bash setup_host.sh                              # Ubuntu 22.04/24.04
sudo python3 campaign.py --profile paper             # ~402 runs, ~9 h, resumable
python3 metrics.py data
python3 make_paper_assets.py data paper/generated
cd paper && pdflatex main && bibtex main && pdflatex main && pdflatex main
```

`paper/example_generated/` is a watermarked format demo built from **random numbers**. It is not
experimental data.
