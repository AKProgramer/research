# Prompt for Claude (claude.ai): compile the paper to PDF

Attach these files to the chat:
- `main.tex`
- `refs.bib`
- after the experiments have run: the `generated/` folder, zipped as `generated.zip`

Then paste:

```text
I've attached the LaTeX source of my Computer Networks research paper (main.tex, refs.bib,
and possibly a generated.zip containing figures, tables and numbers.tex). Please compile it
into a PDF and give me the PDF file to download.

Instructions:
1. If generated.zip is attached, unzip it into a folder called "generated/" next to main.tex.
   main.tex loads every result from generated/numbers.tex, generated/tab_*.tex and
   generated/fig_*.pdf.
2. Compile with: pdflatex main → bibtex main → pdflatex main → pdflatex main.
   The document class is IEEEtran (conference). Required packages: cite, amsmath, amssymb,
   graphicx, booktabs, multirow, array, xcolor, algorithm, algpseudocode, tikz
   (positioning, arrows.meta, fit, backgrounds, calc), hyperref.
   If LaTeX is not installed in your environment, install it (e.g. texlive-latex-extra,
   texlive-science, texlive-publishers, texlive-pictures) or use Tectonic.
3. Fix only compilation problems: overfull tables, missing packages, syntax errors.
   Do not change wording, results, citations or the section structure.
4. Results: every result is printed by the macro \R{key}, which shows a red "??" when the
   key is missing from generated/numbers.tex. Do NOT replace any "??", figure placeholder
   or table placeholder with numbers of your own: the results must come only from my
   experiments. If generated/ is missing, compile the draft as it is, with the placeholders.
5. After compiling, tell me:
   - the page count,
   - any remaining "??" and which keys they belong to,
   - any LaTeX warnings about overfull boxes or undefined references,
   - anything you had to change to make it compile.
```
