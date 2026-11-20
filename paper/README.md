# Phase 4 paper: build steps

Every result in `main.tex` (numbers, tables, figures, hypothesis verdicts) is produced by
`make_paper_assets.py` from the campaign data. Nothing in the results is typed by hand.
Until the data exists, those places show a red **??** or a red "not found" box.

## 1. Run the experiments (Ubuntu 22.04/24.04, a VM is fine, about 9 h unattended)

```bash
sudo bash setup_host.sh
sudo python3 campaign.py --profile paper --dry-run   # shows the 402-run plan
sudo python3 campaign.py --profile paper             # resumable: re-run the same command after an interruption
```

To split the campaign over several sessions, use `--only exp1,exp0`, then `--only exp2,exp5`, and so on.

## 2. Build the results

```bash
python3 metrics.py data                              # -> data/summary.csv + ts.csv per run
python3 make_paper_assets.py data paper/generated    # -> figures, tables, numbers.tex
```

The script prints `ok` or `FAIL` for each section.

## 3. Compile

Upload the `paper/` folder (including `generated/`) to Overleaf, or run locally:

```bash
cd paper && pdflatex main && bibtex main && pdflatex main && pdflatex main
```

## Before submission

- Make https://github.com/AKProgramer/research public (the paper links to it as `\repourl`).
- Check the e-mail address in the author block.
- Search the PDF for **??**. Any that remain mean a section had no data, so check the script's output.
- Read Sections V–VIII against the actual numbers and adjust the interpretation where needed.
