# EXAMPLE ONLY: format demo built from random numbers

These files show what `make_paper_assets.py` writes into `paper/generated/`. Every value
comes from a random-number generator, not from the testbed. They are **not** experimental
results and must not be reported as results.

- Each file starts with the line `% SAMPLE - random values, NOT experimental results`.
- Every figure carries a red "SAMPLE - NOT REAL DATA" stamp.
- `numbers.tex` defines `r@sample`, which makes `main.tex` print a "SAMPLE DATA" watermark on every page.

| File | What it holds | Used in the paper as |
|---|---|---|
| `numbers.tex` | ~360 numbers and data-dependent phrases, read with `\R{key}` | all numbers in the text, abstract and conclusion |
| `tab_validation.tex` | Exp-0 replication of published settings | Table: testbed validation |
| `tab_core.tex` | Exp-1 core 2x2 (U, J, BBR share, delay, loss) | Table: core results |
| `tab_regime.tex`, `tab_anova.tex` | Exp-2 buffer x RTT sweep, factorial ANOVA | Appendix table, ANOVA table |
| `tab_flows.tex`, `tab_rtt.tex` | Exp-3 flow scaling, Exp-4 RTT heterogeneity | Tables |
| `tab_transient.tex` | Exp-5 convergence after capacity/RTT steps and late joins | Table |
| `tab_ablation.tex` | Exp-6 nine-variant ablation | Ablation table |
| `tab_failures.tex` | failure-criteria counts | Discussion |
| `fig_*.pdf` / `fig_*.png` | figures (PDF for LaTeX, PNG for preview) | Figures 3-9 |

The real folder is made by:

```bash
sudo python3 campaign.py --profile paper
python3 metrics.py data
python3 make_paper_assets.py data paper/generated
```
