---
name: notebooklm-research-workflow
description: >-
  Scientific literature deep-reading and quality-controlled automation workflow
  using notebooklm-py. Use when the user wants to ingest academic PDFs into
  NotebookLM, audit parsing quality with source fulltext, repair broken or
  scanned Chinese/multilingual PDFs into image-embedded DOCX, run rigorous
  cross-paper comparisons with strict reviewer personas, or synthesize research
  outputs (mind-map, data-table, report, infographic, audio, video, slide-deck).
  Complements the base notebooklm skill with research QA and verification.
---

# NotebookLM Research Workflow (通用科研文献精读与防幻觉工作流)

This skill encodes a discipline-agnostic, research-first workflow for scientific literature synthesis with `notebooklm-py` (spanning biomedicine, CS/AI, chemistry/materials, social sciences, and earth sciences). Rather than treating NotebookLM as a passive black-box summarizer, the agent acts as an **active scientific guardrail** across three layers:

1. **Dual-Engine Routing (Use the right tool for the job — don't force NotebookLM to be a strict SQL database)**:
   - **Use NotebookLM for**: hosting/exploring 15–50+ long sources at minimal local context cost, theme clustering (`label`), narrative/dispute mapping across clean prose, human-clickable `[N]` citation notebooks (`--save-as-note`), and multimodal deliverables (`mind-map`, `slide-deck`, `audio`, `infographic`).
   - **Use Local Agent + PDF (`pymupdf` / vision) directly for**: deep reading of 1–8 core papers, extracting publication-grade quantitative tables from complex/multi-page PDF grids, statistical comparison (`p`-values, `mean±SD`, experimental conditions), and confidential/unpublished manuscripts. Bypass `generate data-table` when exact table reproduction is the main goal.
2. **Input Layer (Parsing QA)**: `pdf_prepare.py` + `check_fulltext.py` + `figure_probe.py` — never equate `status: "ready"` with valid parsing, and never assume fluent CJK/English prose preserved its embedded numbers.
3. **Isolation Layer (Source Governance)**: Physically separate **primary empirical studies** (`primary`), **secondary reviews/meta-analyses** (`review`), and **background/control references** (`background`) using `label` / `-s` subsets. Never dump mixed primary + review sources into one `data-table` or `report`.
4. **Output Layer (Deterministic Verification Gate)**: Run `verify_outputs.py scan-csv` + `check-coverage` + `trace-claim` + review-excluded `ask -s` audits before trusting NotebookLM-generated tables, reviews, or synthesis answers.

## Step 1: Data Isolation & Pre-flight Check

1. **Confidentiality boundary**: Never upload unpublished manuscripts, peer-review drafts, or confidential lab datasets to cloud notebooks unless explicitly instructed. Keep public-literature notebooks strictly isolated from active manuscript directories.
2. **Verify auth** before batch operations:
   ```bash
   notebooklm auth check --test --passive --json
   ```
   - **Why `--test --passive`**: Bare `auth check` only validates local cookie JSON structure/existence and misses server-side session expiry; `--test` checks live token fetch, and `--passive` keeps the probe strictly read-only (no `NOTEBOOKLM_REFRESH_CMD`, cookie rotation, or disk writes). Require `status == "ok"` and `checks.token_fetch == true`.
   - **Session lifetime & proxy troubleshooting**: Web cookies often expire after idle periods (~2h) or proxy switches. If `token_fetch` fails, inspect active proxy env vars (`HTTP_PROXY`/`HTTPS_PROXY` — dead local proxy ports block TLS before auth), then run `notebooklm login`: it reuses the isolated `browser_profile` without conflicting with open Chrome and often refreshes cookies in ~20s automatically.
   - **Browser-cookie lock pitfall**: `notebooklm auth refresh --browser-cookies chrome --verify` fails on Windows with `browser database is locked` whenever Chrome is open; Chrome must be closed completely first.
   - **Security boundary (`--master-token`)**: Never suggest `notebooklm login --master-token` for primary Google accounts; per `notebooklm/_auth/master_token.py`, it produces a full-account `infostealer-grade` token intended only for dedicated/throwaway accounts.

## Step 2: Pre-Flight Local PDF Check, Ingest & Mandatory `source fulltext` QA

**Never trust `status: "ready"` alone.** A source marked `ready` can still have garbled encoding, missing Chinese characters (unembedded `GBK-EUC-H` / missing `ToUnicode` font maps), truncated markdown, sliced vector charts, or mislabeled filenames.

1. **Local pre-flight check & optional Zotero triage (recommended before upload)**:
   - **Direct PDF check**:
     ```bash
     python scripts/pdf_prepare.py inspect <paper.pdf>
     ```
     Watch for **`BROKEN_FONT_ENCODING`** (local PyMuPDF decodes via built-in CJK CMaps, hiding font issues that cause NotebookLM PDF ingestion to drop/garble CJK characters) and review **`Page 1 head`** to catch mislabeled PDF filenames before uploading.
   - **Zotero collection triage (`zotero_54yyyu` MCP + `scripts/zotero_triage.py`)**: If curating from Zotero instead of manual folders, fetch `zotero_get_collection_items(detail="full")` + `zotero_get_items_children` (plus `zotero_get_item_metadata(format="bibtex")` for linked PDFs/DOIs — note BibTeX `file = {...}` exposes local paths even when `zotero_get_attachment_path` fails without `ZOTERO_LOCAL=true`; and check `zotero_get_annotations` on annotated PDFs for page-anchored highlights/figure captions). Run:
     ```bash
     python scripts/zotero_triage.py --collection-md <col.md> --children-md <children.md> [--bibtex <refs.bib> --storage-dir <ZOTERO_STORAGE> --fulltext-dir <ft_dir>] --primary-tags "<PrimaryTag>" --target-focus "<TargetTerm>" --out-whitelist whitelist.json --strict
     ```
     This automatically strips leaked `note`/`attachment` records from `zotero_search_by_tag` results, isolates `review`/`background`/`exclude`/`off-target` items before ingest, flags `GENERIC_FILENAME` (`main.pdf`) with clean `--title` labels, and runs `pdf_prepare.inspect_pdf` + `check_fulltext.audit_source_text` preemptively so broken/OCR scans (`REPAIR_BEFORE_INGEST`) never enter NotebookLM raw.
   - **Observed Zotero MCP caveats**: `scite_check_retractions` only resolves exact collection names (fails on 8-char keys) and its batch endpoint may return transient network errors while single/search `scite_enrich_item` / `scite_enrich_search` succeed for DOI-bearing papers; `zotero_semantic_search` reads a manually updated Chroma index (`zotero_get_search_database_status`) that skips unindexed/recent items.
2. **Upload sources and wait** (`source add` + `source wait`).
   - Pass `--title "<Author_Year_Title>"` for generic filenames (applies to `text` and uploaded-file sources; URL/YouTube titles are fetched automatically).
3. **Script-, noise- & digit-aware text-layer audit**: Extract and audit fulltext via the bundled checker:
   ```bash
   python scripts/check_fulltext.py -n <NOTEBOOK_ID> --keywords "关键词1,关键词2" [--only <SUBSTR> --limit <N> --json --strict]
   ```
   - Automatically classifies source script (`zh` / `latin` / `mixed`), handles `image`-only sources (`OK_IMAGE_SOURCE`), skips mismatched-language keywords, detects leaked PDF font-name noise (`SimSun`, `ZHIGZ*`, `FZ*`, `AdvTT*`, `AdvOT*`, `B4+SimSun`, etc.), control glyphs (`\x02`), `\ufffd` replacement noise, and **silently dropped digits** (after stripping URLs, UUIDs, and font tokens so URL hashes never fake digit density). Flags `FAIL_LOST_CJK` / `FAIL_LOST_DIGITS_OR_CTRL` / `FAIL_BROKEN_OR_EMPTY` / `WARN_SPARSE_DIGITS` / `WARN_ZERO_KEYWORD_HITS` / `WARN_FONT_NOISE`.
   - **Silent digit-loss warning**: Older Chinese PDFs (e.g., 1990s scans) can extract fluent Chinese sentences and hit domain keywords while silently stripping 100% of embedded digits/Roman numerals into empty gaps (`FAIL_LOST_DIGITS_OR_CTRL` / `pdf_prepare.py` `BROKEN_CHAR_OR_DIGIT_ENCODING`); always inspect digit counts alongside CJK counts.
4. **Visual figure audit (for vector-heavy PDFs)**: NotebookLM's `ask` reads the figure slices produced during backend PDF ingestion—not full-page PDF renders. Pure vector plots (e.g., InDesign/Illustrator charts) can be cropped in half or stripped of axis/legend annotations. Export Markdown + `figures/` via `export_md_figures.py` to inspect what NotebookLM actually sees—while keeping in mind that `source fulltext --format markdown` may truncate early before References on long PDFs (`source fulltext` plain text remains complete for prose and `trace-claim`).
   ```bash
   python scripts/export_md_figures.py -n <NOTEBOOK_ID> -o <OUT_DIR>
   ```
5. **Classify each source (parsing quality AND source role)**:
   - **Clean PDF**: `check_fulltext.py` returns `OK`, text/CJK/digit counts align with local `inspect`, domain terms hit > 0, and key figures in `figures/` are intact. Note that for modern journal PDFs, **narrative prose is generally well-preserved**, while **in-paper data tables** are flattened cell-by-cell (`1 cell per line`, losing header alignment).
   - **Clean text, chopped vector figures**: Keep the original PDF source intact and recover only the broken figures via **Step 3A**.
   - **Broken text layer / `BROKEN_FONT_ENCODING` / `BROKEN_CHAR_OR_DIGIT_ENCODING` / scanned PDF (`FAIL_LOST_CJK` or `FAIL_LOST_DIGITS_OR_CTRL` or `WARN_ZERO_KEYWORD_HITS`)**: Repair via **Step 3B** (multimodal page transcription) or **Step 3C** (batch OCR).
   - **Source Role (`primary` 一手实证 vs `review` 二手综述 vs `background` 背景对照)**: Explicitly record which sources contain **direct experimental/observational/benchmark data** on your target vs **secondary reviews/meta-analyses** or **background/analog literature**. Never mix secondary reviews with primary studies during parameter extraction or core claim synthesis—reviews monopolize `data-table` rows, inject off-target study subjects/populations, duplicate OnlineFirst vs print citations (`2019` vs `2020`), and seed stitched secondary-citation hallucinations and circular evidence (`1` primary study counted `N` times) in `report`.
   - **Table caveat**: Both embedded data tables inside `source fulltext` (flattened line-by-line, bilingual headers interleaved) and `markdown` tables (often noisy figure-plate OCR) lose grid fidelity. Never rely on NotebookLM-parsed tables for publication-grade numbers—have your local Agent extract key tables directly from the original PDFs.

## Step 3: Repair Broken PDFs & Chopped Vector Figures -> DOCX (Text/Tables) + Raster PDF (Figures)

**Why DOCX for text/tables vs. Raster PDF for figures**:
- Uploading raw OCR `.md` alone drops relative `![](images/...)` links; printing HTML to PDF via Chromium leaks font-name tokens (`MicrosoftYaHei`) into extracted text.
- Rebuilding transcribed text & tables into `.docx` via `scripts/md2docx.py` (which handles `#` headings, HTML `<table>` and Markdown pipe tables, strips LaTeX `$...$`/`$$...$$` wrappers cleanly) preserves 100% of prose and digits with zero font noise.
- **Verified DOCX vision caveat**: NotebookLM's visual pixel indexing of images embedded inside `.docx` is unreliable (verified via `figure_probe.py`: `.png`-embedded `.docx` files exposed only text captions and failed figure-only queries, and `source fulltext --format markdown` returns `0` figures for `.docx`). Therefore, **pack figures that must be read visually into a clean PyMuPDF raster PDF** (`pdf_prepare.py pack-figs-pdf`), which uses Unicode/ToUnicode headers (`0` CJK font-encoding errors, no Chromium font-name leakage) and triggers NotebookLM's native PDF page vision reliably.

First diagnose the local PDF on CPU (`pymupdf` + `pymupdf4llm`):
```bash
python scripts/pdf_prepare.py inspect <paper.pdf>
```

- **Step 3A — Clean text, only vector figures chopped (conserve the 50-source limit)**:
  - Do **not** rebuild the whole paper or upload one `.png` per figure (standalone images lack paper context and waste source slots), and do **not** pack supplement figures into `.docx` (risks zero visual pixel indexing).
  - Extract intact composite PNGs (with overlaid vector labels/legends preserved) via `pymupdf4llm`, or crop a normalized bounding box directly:
    ```bash
    python scripts/pdf_prepare.py extract-figs <paper.pdf> -p <PAGES> -o <WORK_DIR> --label "Author (Year) Title" --append-md <WORK_DIR>/missing_figures.md
    python scripts/pdf_prepare.py crop <paper.pdf> -p <PAGE> --rect x0,y0,x1,y1 -o <WORK_DIR>/figs/fig.png
    ```
  - Review/trim unwanted images in `missing_figures.md` (add exact figure numbers and captions above each image), then pack **all missing figures across the notebook into one single clean raster PDF** (`+1` source slot total, recommended default):
    ```bash
    python scripts/pdf_prepare.py pack-figs-pdf <WORK_DIR>/missing_figures.md -o <WORK_DIR>/缺失图件补充.pdf
    ```
  - **Optional single-source in-place patch (`patch-fig`)**: When strict source limits (`+0` net slots) or keeping text and figures inside one source ID is required and figure margins are cleanly separable from surrounding text, visually measure normalized `0..1` boxes from `render-pages` output and flatten only those regions in a patched PDF copy (strips chart-internal text inside the box unless `--keep-text` is passed):
    ```bash
    python scripts/pdf_prepare.py patch-fig <paper.pdf> --region <PAGE>:x0,y0,x1,y1 [--region <PAGE2>:x0,y0,x1,y1] -o <WORK_DIR>/patched.pdf
    ```

- **Step 3B — Broken text layer or scanned paper (5–15 pages, multimodal transcription)**:
  - Note: For full-page scanned PDFs with hidden OCR layers, `pymupdf4llm` treats the whole page as a picture and drops overlaid text; always inspect first.
  - Render crisp page PNGs (plus raw text dump) via `pymupdf`:
    ```bash
    python scripts/pdf_prepare.py render-pages <paper.pdf> -o <WORK_DIR> --dpi 180
    ```
  - Read the rendered `pages/page_*.png` files directly with multimodal vision, transcribe clean Markdown (headings + pipe/HTML tables), build `<output.docx>` via `scripts/md2docx.py`, upload and delete the broken PDF (`0` net source slots).
  - If the scanned paper contains key figures/profiles/diagrams that NotebookLM `ask` must inspect visually, crop them (`crop`) and append them to `<WORK_DIR>/missing_figures.md` -> `pack-figs-pdf` (so text/tables live losslessly in `.docx` while figures live reliably in the raster supplement PDF).

- **Step 3C — Large scanned batches (100+ pages) or external OCR outputs**:
  - When batch OCR outputs (e.g., Docling, `rapidocr-onnxruntime`, or MinerU) are available (`<ocr_markdown.md>` + `images/`), run `scripts/md2docx.py <ocr_markdown.md> <output.docx> [images_base_dir]` for lossless text/tables, and route any figures requiring visual `ask` inspection through `pack-figs-pdf`.

After uploading any repaired `.docx`, raster supplement PDF, or patched PDF, wait for `ready`, re-verify text via `check_fulltext.py` (e.g. `--only <ID_OR_TITLE>`) and `export_md_figures.py`, and run a visual plant-token probe on values visible **only inside a figure** (checks `source fulltext` has 0 leaks before running `ask -s`, with 1 automatic retry on transient `UNEXPECTED_ERROR`):
```bash
python scripts/figure_probe.py -n <NOTEBOOK_ID> -s <SOURCE_ID> -q "<图件专属数字/坐标问题>" --expect "<TOK1>" --expect "<TOK2>" [--strict --save probe.json]
```

## Step 4: Precision Retrieval & Critical Cross-Paper Reading

See [research-prompts.md](./references/research-prompts.md) for copy-ready prompt templates.

1. **Exact text/table lookup (`source search` & `verify_outputs.py trace-claim`) vs. targeted `ask -s`**:
   - Use `notebooklm source search "<term>" -n <NB> --limit 5 --json` for semantic snippet retrieval with character offsets, and use `python scripts/verify_outputs.py trace-claim -n <NB> -s <SID> --pair A:B` for deterministic numeric/citation verification.
   - **Observed CLI & retrieval boundaries**:
     - `source search --json` returns a **top-level JSON array `[...]`** (not a `{...}` dict), indexes text/table chunks only (**skips image-only chunks**), and ranks passages semantically—bare decimals like `"2.54"` or OCR/LaTeX spaced numbers often miss unless paired with domain terms (e.g., `"<value> <metric_or_method>"`).
     - `verify_outputs.py trace-claim` downloads/caches `source fulltext` locally and normalizes Unicode minus (`−` -> `-`), PDF font-noise tokens, and LaTeX `\sim`/whitespace spacing so exact numeric bounds and absent claims are verified deterministically at ~0 quota.
   - Use `ask` for visual figures, and pass repeatable `-s <SOURCE_ID>` flags to isolate single-paper, two-paper, or primary-only comparisons without cross-talk from reviews or unrelated studies. Beware **multimodal & chart-reading risks**: verify whether axes are linear vs log scale, inverted, or multi-panel (`a/b/c/d` swaps) against the local figure crop before quoting chart numbers.
   - **PowerShell & persistence tips**: For complex multi-line/CJK prompts, pass `--prompt-file <FILE>` (or `-` via stdin) to avoid shell quoting corruption; add `--save-as-note -t "<Note Title>"` to preserve answers with interactive `[N]` hover-citation links inside the notebook. Note `--new` clears the active server-side conversation before asking (`--json` skips confirmation).
2. **Topic grouping (`label generate`) & divide-and-conquer subsets**:
   - Run `notebooklm label generate -n <NB> --scope unlabeled --json` to cluster papers by research theme, then filter `-s <SOURCE_ID>` subsets per topic (4–8 **primary** papers at a time, excluding `review` and `background` sources).
   - **Why divide-and-conquer**: Full-library synthesis over 15+ mixed sources causes **silent omission & per-paper attention starvation** (dense empirical papers collapse into empty `"未提及"` placeholders or go completely uncited in `ask`) and review contamination; topic-scoped `-s` runs recover experimental conditions, quantitative ranges, negative/null results, and instrument/benchmark metadata.
   - Run `verify_outputs.py check-coverage <ask.json> -n <NB>` (or `--expected-id ...`) after `ask --json` calls to detect **uncited input sources**, **single-source citation dominance**, and **certainty inflation (`hedging_audit`)**.
   - Use `notebooklm source guide <SOURCE_ID> -n <NB> --json` for per-paper summaries and keywords. Watch for inconsistent translation of technical terms across bilingual libraries and standardize terminology manually.
3. **Persona-guided cross-paper synthesis & verification audits (`configure --persona` + `ask`)**:
   - Use the **80% preset + 20% domain adaptation** rule from [research-prompts.md](./references/research-prompts.md): pick a preset persona and adapt only discipline-specific target fields.
   - Note `configure` updates the **cloud notebook's persistent chat settings** (does not affect `report` / `data-table` / `audio`), and `--mode` cannot be combined with `--persona` / `--response-length`.
   - **Secondary-Citation Audit Loop**: Whenever a full-library `report` or `ask` cites an older paper via a relay (`A (Year1)` cited in `B (Year2)`), or blends multiple studies on the target, run **both**:
     1. `verify_outputs.py trace-claim` across the isolated primary sources to check verbatim numeric/author presence;
     2. A **review-excluded** `ask -s <primary_IDs_only> --new --json` using the Secondary-Citation Audit prompt to classify each claim as `真实存在` / `未见提及` / `张冠李戴` with exact `cited_text` and character offsets.
   - **Condition, Evidence-Strength & Timeline Audit**: Explicitly require `ask` and `report` outputs to tag claims as **直接实测 (measured)** vs **估算推论 (inferred/modeled)** vs **假说推测 (hypothesized)**, preserve hedging qualifiers (`may/preliminary/in vitro/n=...`), retain negative/null results, and mark whether older claims/methods were **later superseded or refuted (防时间线扁平化)**.
   - Save `ask --json` outputs and reset `configure` with bare `notebooklm configure -n <NB>` when finished if desired.
4. **Literature gap discovery & errata patching**:
   - Use `notebooklm research discover "<query>" -n <NB> --json` (fast synchronous probe) or `source add-research --mode deep` to uncover missing papers (especially newer replications, negative results, or errata), then selectively import with `research import` (`--cited-only` to avoid noisy web pages).
   - Use `notebooklm source append <SOURCE_ID> "<note>" -n <NB>` to attach errata, retraction notices, or missing captions without re-uploading.

## Step 5: Structured Outputs & Mandatory Verification Gates

**Treat every `data-table`, `ask`, or `report` artifact as an unverified draft until it passes the 7 Universal Scientific Risk Checks (Gates 1–3 below).**

1. **Generate the right output by isolated `-s` subset** (see [budget-tiers.md](./references/budget-tiers.md)):
   - **Parameter matrix (`generate data-table -s <primary_subset> --wait`)**: **Decision switch**—if you only have 1–8 target papers and need exact publication-grade numbers from PDF tables/figures, extract the table directly with local Agent + `pymupdf`/vision instead of generating a NotebookLM `data-table` and repairing it afterward. Use `generate data-table -s` for **10+ source exploratory scans** (4–8 primary sources per thematic `label` batch, excluding `review` and `background` sources), then merge locally.
     - **Verified stability boundary**: Even explicit prompt instructions (`"严禁吞并连接符"`) **do not reliably prevent** NotebookLM from dropping hyphens inside decimal intervals (`2.543.56`, `-1.80.4`), series codes (`Fig13`, `Stage13`, `Cohort12`), or mixing incompatible units (`μM` vs `nM`, `mean` vs `median`) across rows. Automated scanning is mandatory.
   - **Literature review (`generate report --format custom -s <primary_subset> --wait`)**: Avoid `artifact suggestions` (generic business templates) and exclude secondary reviews when synthesizing core empirical claims. **Cross-check**: full-library `report` drafts can synthesize fake `"二次转引"` chains (cross-paper date stitching, fabricated citations, or wrong-subject attribution), erase negative results, and inflate tentative hypotheses into settled facts; audit key claims against isolated `ask -s` + `trace-claim` before citing.
   - **Evolution & argument map (`generate mind-map`)**: Exports JSON node tree for structuring papers or talks.
   - **Presentation & outreach (`infographic`, `audio`, `video`, `slide-deck`)**: Only convert claims/numbers that already cleared Verification Gates 1–2; use `generate revise-slide` to tweak individual slides instead of regenerating the full deck.
2. **Gate 1 — Automated output & coverage audits (`verify_outputs.py`)**:
   ```bash
   python scripts/verify_outputs.py scan-csv <table.csv> --target-focus "<TargetTerm1,TargetTerm2>" [--code-prefixes "PrefixA,PrefixB"] [--strict]
   python scripts/verify_outputs.py check-coverage <ask_output.json> -n <NB> [--expected-id <SID1> ...] [--strict]
   ```
   - `scan-csv` flags **formatting & numeric destruction** (glued decimal pairs `2.543.56`, swallowed 5–6 digit ranges with CJK-safe regex, unit-glued bounds, negative double-minus ambiguity, merged series codes like `Fig13`/`Stage13` plus domain `--code-prefixes`), **source governance defects** (`>=25%` single-source dominance, off-target subject drift, duplicate OnlineFirst/print splits `2019` vs `2020`), and **unit / scale / metric conflation** (cross-row conflicts inside the same column across `molar_concentration`, `mass_concentration`, `temperature_scale`, `statistical_dispersion`, `central_tendency`, and `study_setting`).
   - `check-coverage` flags **silent source omission** (input `-s` sources or notebook sources with zero `[N]` citations in `ask`), **single-source dominance** (`>=45%` of citations from one source), and **certainty inflation (`hedging_audit`)** when `cited_text` snippets contain `>=3` hedging/limitation cues (`may/suggest/preliminary/可能/推测/初步/局限`, etc.) while the synthesized `answer` drops them (`0` cues or `<25%` retention ratio).
   - Treat `INFO_LARGE_MAGNITUDE` and `[HIGH]` anomalies as **human/fulltext verification targets**—never blind auto-splits.
3. **Gate 2 — Primary-text numeric, condition & citation audit (`verify_outputs.py trace-claim` + review-excluded `ask -s`)**:
   - Recover verbatim intervals, units, sample sizes (`n`), and experimental conditions for every flagged cell via `trace-claim -n <NB> -s <SID> --pair A:B` (normalized `source fulltext`), and run review-excluded `ask -s` verification on secondary citations, negative/null results, and superseded timeline claims.
4. **Gate 3 — Honest stratification & idempotent Zotero write-back**:
   - Explicitly partition final tables, reviews, and briefings into **Primary-Verified** (verified against primary PDF offsets/sample IDs, units, and conditions), **Corrected from Fulltext** (with original anomaly logged), and **Unverified / Review-Relayed (`待回原 PDF 核对`)**. Distinguish **直接实测**, **估算推论**, and **假说推测** explicitly, and never present unverified NotebookLM summaries or raw full-library baseline tables as settled scientific fact.
   - **Optional Zotero verified-card write-back (`zotero_create_note` / `zotero_update_note`)**: Write back ONLY `Primary-Verified` findings (with exact page/section/table anchors, sample `n`, units, exceptions, and conflicting/superseded updates). **Always inspect `zotero_get_notes(item_key=...)` first** so existing user/Obsidian notes stay untouched and repeated runs update the sentinel note cleanly via `zotero_update_note` instead of duplicating. Stick to safe HTML (`p, strong, em, ul/ol/li, blockquote, code`) and **never apply parent paper topic/role tags** onto child notes (use dedicated status tags such as `nlm-workflow/verified-card` + `review/primary-verified`) so `zotero_search_by_tag` paper queries stay clean.
