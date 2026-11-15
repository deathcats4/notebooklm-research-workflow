# Research Output Selection & Operational Reference

Choose the right NotebookLM command for each research task. Cost percentages from `notebooklm usage --categories` are included solely as an optional reference if running large batches.

---

## Output & Tool Selection by Research Goal

| Research Goal | Recommended Command | Key Strengths & Caveats | Est. 5h Quota (Ref) |
|---|---|---|---|
| **Zotero pre-ingest triage** | `zotero_triage.py` (+ `zotero_54yyyu` MCP) | Filters `note`/review/off-target noise, runs pre-upload `inspect_pdf` & `audit_source_text`, builds clean upload whitelist | 0% |
| **Verified Zotero write-back** | `zotero_create_note` / `zotero_update_note` | Check `zotero_get_notes` first (idempotent); attach page-anchored verified notes using note-only status tags | 0% |
| **Verify PDF ingestion** | `source fulltext` / `check_fulltext.py` | Catches garbled text, dropped CJK chars, and mislabeled files | ~0% |
| **Exact quote / table lookup** | `source search` / `verify_outputs.py trace-claim` | `source search` gives semantic snippets (`[...]` array; misses bare numbers); `trace-claim` checks normalized `fulltext` deterministically | ~0% |
| **Verify exported CSV integrity** | `verify_outputs.py scan-csv` | Local check for glued bounds (`2.543.56`, `Fig13`/`Stage13`), unit/stat-metric mixing (`μM` vs `nM`, `mean` vs `median`), review dominance, off-target drift, and duplicate paper splits | 0% |
| **Verify `ask` coverage & tone** | `verify_outputs.py check-coverage` | Local check on `ask --json` for silent source omission, citation monopoly, and certainty inflation (`hedging_audit`) | 0% (~0% via `-n`) |
| **Group papers by theme** | `label generate --scope unlabeled` | Auto-clusters papers into research sub-topics safely for `-s` subset isolation | ~0% |
| **Single-paper overview** | `source guide` | Fast summary + 5 keywords; verify half-translated English terms | 0.13% |
| **Figure reading & deep Q&A** | `ask` (+ `configure --persona`) | Reads embedded charts/profiles; saves `cited_text` in JSON | 0.20% |
| **Suggested follow-up angles** | `suggest-next-steps` | Grounded research questions (prefer over `artifact suggestions`) | 0.23% |
| **Quick web literature scan** | `research discover` | ~8s synchronous scan; screen results before `research import` | 0.37% |
| **Deep external survey** | `source add-research --mode deep` | Multi-source web discovery + synthesis report | 8.33% |
| **Concept / evolution tree** | `generate mind-map` | Exports structured JSON tree for outlining reviews or talks | 0.53% |
| **Cross-paper data matrix** | `generate data-table -s <label_primary_IDs>` | Best for exploratory scans across 10+ sources (4–8 primary IDs/batch + `scan-csv`); for 1–8 core papers needing publication-exact tables, have local Agent read PDFs directly | 5.03% |
| **Structured academic review** | `generate report --format custom -s <primary_IDs>` | Great skeleton; exclude secondary reviews when testing empirical claims & cross-audit via review-excluded `ask -s` + `trace-claim` | 5.57% |
| **Summary figure** | `generate infographic` | Single visual overview for slides or outreach | 8.13% |
| **Review cards / self-test** | `generate flashcards` / `quiz` | Useful for teaching or exam prep | 2.20% / 2.67% |
| **Group meeting slides** | `generate slide-deck` + `revise-slide` | Export PDF/PPTX; edit single slides via `revise-slide` (3.57%) | 46.60% |
| **Audio / video briefings** | `generate audio` / `video` | Two-host podcast or explainer (`--format short` for vertical video) | 38.90% / 41.37% |

---

## Optional Batch & Quota Tips

- Normal daily reading (`fulltext`, `search`, `ask`, `mind-map`, `data-table`, `report`) uses a small fraction of rolling 5-hour/weekly quotas; only back-to-back audio, video, and slide-deck runs approach limits.
- On large notebooks with long prompts, `data-table` + `report` consumed **17.40%** of the 5-hour window in practice (vs. 10.60% baseline sum).
- Use `--later` on `audio`, `video`, `slide-deck`, `infographic`, `report`, `quiz`, and `flashcards` for unattended overnight queuing (`notebooklm usage --categories` shows live `Deferred left`).
- Skip `generate cinematic-video` unless on an AI Ultra account (`Insufficient` on standard plans).
