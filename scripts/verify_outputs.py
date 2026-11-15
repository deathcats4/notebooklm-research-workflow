#!/usr/bin/env python
"""Verify NotebookLM research outputs against universal scientific integrity gates.

Provides three deterministic checks designed to catch NotebookLM's native
hallucinations, unit/baseline drift, silent source omission, and hyphen dropping
across any scientific discipline:

1. `scan-csv`: Audit downloaded `generate data-table` CSV files for glued numeric
   ranges (`2.543.56`, `90275`, `Fig13`), single-source dominance (`>=25%` rows),
   off-target study drift (`--target-focus`), duplicate citation splits (`2019` vs
   `2020`), and mixed units/metrics across rows in the same column.
2. `check-coverage`: Audit saved `ask --json` responses for silent omission of
   expected sources, single-source citation dominance, and uncertainty/hedging
   stripping between `cited_text` snippets and the synthesized `answer`.
3. `trace-claim`: Search normalized `source fulltext` locally (with optional cache)
   after unifying Unicode minus signs, PDF font noise, and OCR LaTeX spacing.

Usage:
    python verify_outputs.py scan-csv table.csv --target-focus "TargetA,TargetB" [--code-prefixes "Py,Mar"]
    python verify_outputs.py check-coverage answer.json -n <NB> [--json --strict]
    python verify_outputs.py trace-claim -n <NB> -s <SID> --pair -1.8:0.4 --term "Author(1998)"
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import re
import subprocess
import sys

# --- Normalization & Anomaly Rules ---

UNICODE_MINUS_RE = re.compile(r"[\u2212\u2013\u2014\u2010]")
UNICODE_TILDE_RE = re.compile(r"[\uff5e\u223c]")
LATEX_TILDE_RE = re.compile(r"\\mathrm\{\\sim\}|\{\\sim\}|\\sim")
PDF_FONT_NOISE_RE = re.compile(
    r"(?:TimesNewRoman(?:PS(?:MT)?)?|SimSun|NSimSun|FZ[A-Z0-9-]{2,}|ZHIGZ[A-Z0-9-]*|"
    r"CchtfrAdvTT[0-9a-fA-F]+|Adv(?:TT|OT|PS|P)[0-9A-Za-z_.]+|Nimbus(?:San|Rom)[A-Za-z0-9-]*|B\d+\+[A-Za-z0-9-]+)"
)

DEFAULT_CODE_PREFIXES = (
    "Fig", "Table", "Tab", "Step", "Stage", "Phase", "Group", "Cohort",
    "Batch", "Exp", "Trial", "Cycle", "Round", "Day", "Week", "Grade", "Sample",
)

BASE_NUMERIC_RULES: list[tuple[str, str, re.Pattern[str], str]] = [
    (
        "glued_double_dot",
        "HIGH",
        re.compile(r"-?\d+\.\d+\.-?\d+"),
        "Two decimals concatenated without a range separator (e.g., 2.543.56).",
    ),
    (
        "glued_unit_plus_num",
        "HIGH",
        re.compile(r"[‰℃%][+\-]?\d"),
        "Unit symbol directly glued to next numeric bound (e.g., 1.7‰+4.9‰).",
    ),
    (
        "negative_hyphen_ambiguity",
        "HIGH",
        re.compile(r"(?<![.\d-])-\d+(?:\.\d+)?-\d+(?:\.\d+)?"),
        "Leading negative bound chained with '-' instead of '~' (second sign ambiguous).",
    ),
    (
        "glued_5_6_digits",
        "HIGH",
        re.compile(r"(?<![\d.])\d{5,6}(?![\d.])"),
        "Unseparated 5-6 digit integer (often two merged bounds such as 90~275 -> 90275).",
    ),
]

MAGNITUDE_UNIT_SUFFIX_RE = re.compile(
    r"^\s*(?:ppm|ppb|cps|μm|um|nm|mm|wt%|g/t|mg/kg|mg/L|μg/mL|ug/mL|"
    r"μM|uM|nM|mM|kDa|bp|kb|Mb|Gb|Hz|kHz|MHz|GHz|ms|"
    r"samples|patients|cases|subjects|cells|epochs|tokens|rows|items)\b",
    re.I,
)
YEAR_RE = re.compile(r"((?:19|20)\d{2})([a-z])?", re.I)
CJK_SURNAME_PREFIX_RE = re.compile(r"^([\u4e00-\u9fff]{2,3})")

# Disjoint scientific scale/metric families; seeing >1 label across rows in a single column flags risk.
UNIT_FAMILIES: list[tuple[str, list[tuple[str, re.Pattern[str]]]]] = [
    (
        "molar_concentration",
        [
            ("nM", re.compile(r"\bnM\b")),
            ("μM", re.compile(r"(?:μM|\buM\b)")),
            ("mM", re.compile(r"\bmM\b")),
        ],
    ),
    (
        "mass_concentration",
        [
            ("ppb", re.compile(r"\bppb\b", re.I)),
            ("ppm", re.compile(r"\bppm\b", re.I)),
            ("wt%", re.compile(r"\bwt\s*%", re.I)),
        ],
    ),
    (
        "temperature_scale",
        [
            ("°C", re.compile(r"(?:℃|°C)")),
            ("K", re.compile(r"\d\s*K\b")),
            ("°F", re.compile(r"(?:℉|°F)")),
        ],
    ),
    (
        "statistical_dispersion",
        [
            ("SD", re.compile(r"\bSD\b")),
            ("SE/SEM", re.compile(r"\bSEM?\b")),
            ("95%CI", re.compile(r"(?:95%\s*CI|\bCI\b)")),
            ("IQR", re.compile(r"\bIQR\b")),
        ],
    ),
    (
        "central_tendency",
        [
            ("mean/均值", re.compile(r"(?:\bmean\b|均值|平均值)", re.I)),
            ("median/中位数", re.compile(r"(?:\bmedian\b|中位数)", re.I)),
        ],
    ),
    (
        "study_setting",
        [
            ("in_vitro", re.compile(r"\bin\s+vitro\b|体外", re.I)),
            ("in_vivo", re.compile(r"\bin\s+vivo\b|体内|动物模型", re.I)),
            ("clinical", re.compile(r"\bclinical\b|临床|队列", re.I)),
        ],
    ),
]

HEDGING_RE = re.compile(
    r"(?:\bmay\b|\bmight\b|\bcould\b|\bsuggest(?:s|ed)?\b|\bpossibl[ey]\b|\blikely\b|"
    r"\bpreliminary\b|\bhypothesi[sz]e[ds]?\b|\bspeculat|\bassum(?:e[ds]?|ption)\b|"
    r"\bin\s+vitro\b|\blimitation|\buncertain|"
    r"可能|或许|推测|假说|假设|初步|局限|尚不明确|假定|有限|仅限于)",
    re.I,
)


def run_cli(args: list[str]) -> dict:
    """Invoke `notebooklm` CLI and parse its JSON stdout payload safely."""
    proc = subprocess.run(
        ["notebooklm"] + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        return {"_error": proc.stderr.strip() or proc.stdout.strip(), "_rc": proc.returncode}
    # Support both dict `{...}` and top-level array `[...]` payloads.
    m = re.search(r"([{\[].*[}\]])", proc.stdout, re.S)
    if not m:
        return {"_error": "No JSON payload in stdout", "_raw": proc.stdout[:300]}
    try:
        parsed = json.loads(m.group(1))
        return {"_items": parsed} if isinstance(parsed, list) else parsed
    except json.JSONDecodeError as exc:
        return {"_error": f"JSON parse error: {exc}"}


def _write_json_out(out_path: str, payload: dict) -> None:
    if not out_path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def normalize_for_match(text: str) -> tuple[str, str]:
    """Return (spaced_normalized, compact_normalized) text for robust claim tracing.

    Unifies Unicode dashes/tildes, removes injected PDF font-name noise, and
    collapses LaTeX OCR digit spacing (e.g. `$9 0 { \\sim } 2 7 5$` -> `$90~275$`).
    """
    unified = UNICODE_MINUS_RE.sub("-", text)
    unified = UNICODE_TILDE_RE.sub("~", unified)
    cleaned = PDF_FONT_NOISE_RE.sub(" ", unified)
    spaced = re.sub(r"\s+", " ", cleaned).strip()
    compact = re.sub(r"\s+", "", cleaned)
    compact = LATEX_TILDE_RE.sub("~", compact)
    return spaced, compact


def _extract_author_key(citation_cell: str) -> tuple[str, list[tuple[int, str]]]:
    """Extract normalized primary surname token + (year, suffix) tuples for duplicate detection."""
    cleaned = citation_cell.strip().strip('"').strip("'")
    years = [(int(y), suf.lower()) for y, suf in YEAR_RE.findall(cleaned)]
    m_cjk = CJK_SURNAME_PREFIX_RE.match(cleaned)
    if m_cjk and not re.match(r"^[A-Za-z]", cleaned):
        return m_cjk.group(1), years

    name_part = re.split(r"[\(,（\d]", cleaned, maxsplit=1)[0]
    name_part = re.sub(r"\b(?:et\s+al\.?|等)\b", "", name_part, flags=re.I).strip()
    tokens = re.findall(r"[A-Za-z]{2,}", name_part)
    if not tokens:
        return cleaned[:8].lower(), years
    # "Fang An" -> "an", whereas "An et al." / "Zhai" -> "an" / "zhai".
    surname = tokens[1] if (len(tokens) >= 2 and "," not in name_part and "等" not in citation_cell) else tokens[0]
    return surname.lower(), years


def _locate_column(
    header: list[str],
    exact_names: set[str],
    substr_patterns: list[str],
    fallback_idx: int,
    prefer_rightmost: bool = False,
) -> int:
    indices = range(len(header) - 1, -1, -1) if prefer_rightmost else range(len(header))
    for idx in indices:
        if header[idx].strip().lower() in exact_names:
            return idx
    for idx in indices:
        col_low = header[idx].lower()
        if any(p in col_low for p in substr_patterns):
            return idx
    return fallback_idx if 0 <= fallback_idx < len(header) else len(header) - 1


def _build_csv_rules(extra_code_prefixes: list[str] | None) -> list[tuple[str, str, re.Pattern[str], str]]:
    prefixes = list(DEFAULT_CODE_PREFIXES) + [p.strip() for p in (extra_code_prefixes or []) if p.strip()]
    escaped = "|".join(re.escape(p) for p in prefixes)
    code_rule = (
        "glued_label_range",
        "MEDIUM",
        re.compile(rf"(?<![A-Za-z0-9_])(?:{escaped})\d{{2,}}(?![A-Za-z0-9_])"),
        "Series/label index possibly missing hyphen (e.g., Fig1-3 -> Fig13 or index >= 10).",
    )
    return BASE_NUMERIC_RULES + [code_rule]


def _audit_column_units(header: list[str], data: list[list[str]]) -> list[dict]:
    """Detect conflicting scientific units, scales, or statistical metrics across rows in a column."""
    warnings: list[dict] = []
    for col_idx, col_name in enumerate(header):
        col_cells = [r[col_idx] for r in data if col_idx < len(r)]
        for family_name, members in UNIT_FAMILIES:
            # Count distinct rows exhibiting ONLY one scale so mixed scales across rows stand out.
            exclusive_labels: collections.Counter[str] = collections.Counter()
            for cell in col_cells:
                hits = [lbl for lbl, pat in members if pat.search(cell)]
                if len(hits) == 1:
                    exclusive_labels[hits[0]] += 1
            if len(exclusive_labels) >= 2:
                warnings.append(
                    {
                        "column": col_name,
                        "unit_family": family_name,
                        "labels_by_row_count": dict(exclusive_labels),
                    }
                )
    return warnings


def scan_csv_file(
    csv_path: str,
    target_focus: list[str] | None = None,
    dominance_threshold: float = 0.25,
    extra_code_prefixes: list[str] | None = None,
) -> dict:
    """Audit a data-table CSV for glued numbers, review dominance, target drift, splits, and mixed units."""
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if len(rows) < 2:
        raise ValueError(f"CSV has fewer than 2 rows: {csv_path}")

    header, data = rows[0], rows[1:]
    total_rows = len(data)
    rules = _build_csv_rules(extra_code_prefixes)
    src_col = _locate_column(
        header, {"source", "sources", "来源", "引用来源"}, ["source", "引用来源"], len(header) - 1, prefer_rightmost=True
    )
    target_col = _locate_column(
        header,
        {"研究对象", "研究区", "矿床", "研究区/矿床", "样本队列", "数据集", "实验体系", "subject", "target", "cohort", "dataset", "study area"},
        ["研究对象", "研究区", "矿床", "队列", "数据集", "target", "cohort", "dataset", "deposit", "region", "area"],
        1,
    )

    cell_findings: list[dict] = []
    source_counts: collections.Counter[str] = collections.Counter()
    off_target_rows: list[dict] = []
    author_index: dict[str, list[tuple[int, str, list[tuple[int, str]]]]] = collections.defaultdict(list)
    focus_lower = [t.lower() for t in (target_focus or []) if t.strip()]

    for row_idx, row in enumerate(data, start=2):
        paper_label = row[0] if row else f"line_{row_idx}"
        src_tag = row[src_col].strip() if src_col < len(row) else ""
        if src_tag:
            source_counts[src_tag] += 1

        if focus_lower and target_col < len(row):
            target_val = row[target_col]
            if not any(tok in target_val.lower() for tok in focus_lower):
                off_target_rows.append(
                    {"csv_line": row_idx, "paper": paper_label, "target_value": target_val, "source": src_tag}
                )

        surname, years = _extract_author_key(paper_label)
        if surname and years:
            author_index[surname].append((row_idx, paper_label, years))

        for col_idx, cell in enumerate(row):
            col_name = header[col_idx] if col_idx < len(header) else f"col_{col_idx}"
            for rule_name, base_sev, pattern, desc in rules:
                for m in pattern.finditer(cell):
                    sev = base_sev
                    if rule_name == "glued_5_6_digits" and MAGNITUDE_UNIT_SUFFIX_RE.match(cell[m.end() :]):
                        sev = "INFO_LARGE_MAGNITUDE"
                    cell_findings.append(
                        {
                            "csv_line": row_idx,
                            "paper": paper_label,
                            "column": col_name,
                            "rule": rule_name,
                            "severity": sev,
                            "match": m.group(0),
                            "description": desc,
                            "cell_excerpt": cell[:160] + ("..." if len(cell) > 160 else ""),
                        }
                    )

    dominant_sources = [
        {"source": tag, "rows": cnt, "ratio": round(cnt / total_rows, 4)}
        for tag, cnt in source_counts.items()
        if cnt >= 3 and (cnt / total_rows) >= dominance_threshold
    ]

    duplicate_splits: list[dict] = []
    for surname, entries in author_index.items():
        if len(entries) < 2:
            continue
        entries_sorted = sorted(entries, key=lambda x: min(y for y, _ in x[2]))
        for i in range(len(entries_sorted)):
            for j in range(i + 1, len(entries_sorted)):
                line_a, lbl_a, yrs_a = entries_sorted[i]
                line_b, lbl_b, yrs_b = entries_sorted[j]
                is_dup = any(
                    abs(ya - yb) <= 1 and not (ya == yb and sa and sb and sa != sb)
                    for ya, sa in yrs_a
                    for yb, sb in yrs_b
                )
                if is_dup:
                    duplicate_splits.append(
                        {
                            "author_key": surname,
                            "row_a": {"csv_line": line_a, "paper": lbl_a, "years": [f"{y}{s}" for y, s in yrs_a]},
                            "row_b": {"csv_line": line_b, "paper": lbl_b, "years": [f"{y}{s}" for y, s in yrs_b]},
                        }
                    )

    unit_variations = _audit_column_units(header, data)
    high_count = sum(1 for f in cell_findings if f["severity"] == "HIGH")
    medium_count = sum(1 for f in cell_findings if f["severity"] == "MEDIUM")
    info_count = sum(1 for f in cell_findings if f["severity"] == "INFO_LARGE_MAGNITUDE")
    passed = high_count == 0 and not dominant_sources and not off_target_rows and not duplicate_splits

    return {
        "file": csv_path,
        "data_rows": total_rows,
        "summary": {
            "passed": passed,
            "high_numeric_anomalies": high_count,
            "medium_numeric_anomalies": medium_count,
            "info_large_magnitudes": info_count,
            "dominant_sources": len(dominant_sources),
            "off_target_rows": len(off_target_rows),
            "possible_duplicate_splits": len(duplicate_splits),
            "mixed_unit_warnings": len(unit_variations),
        },
        "source_distribution": dict(source_counts),
        "dominant_sources": dominant_sources,
        "off_target_rows": off_target_rows,
        "possible_duplicate_splits": duplicate_splits,
        "mixed_unit_warnings": unit_variations,
        "cell_findings": cell_findings,
    }


def _resolve_expected_sources(notebook_id: str | None, expected_ids: list[str]) -> list[tuple[str, str]]:
    """Return [(full_or_prefix_id, title)] from live notebook listing or explicit expected IDs."""
    id_to_title: dict[str, str] = {}
    ready_sources: list[tuple[str, str]] = []
    if notebook_id:
        listing = run_cli(["source", "list", "-n", notebook_id, "--json"])
        if "_error" in listing:
            raise RuntimeError(f"Failed to list sources: {listing['_error']}")
        for s in listing.get("sources", []):
            if s.get("status") == "ready":
                id_to_title[s["id"]] = s.get("title", "")
                ready_sources.append((s["id"], s.get("title", "")))
        if not expected_ids:
            return ready_sources

    resolved: list[tuple[str, str]] = []
    for raw_id in expected_ids:
        matches = [fid for fid in id_to_title if fid.startswith(raw_id)]
        fid = matches[0] if matches else raw_id
        resolved.append((fid, id_to_title.get(fid, "")))
    return resolved


def check_ask_coverage(
    ask_json_path: str,
    notebook_id: str | None = None,
    expected_ids: list[str] | None = None,
    dominance_threshold: float = 0.45,
) -> dict:
    """Audit an `ask --json` response for silent source omission, dominance, and certainty inflation."""
    with open(ask_json_path, encoding="utf-8") as f:
        data = json.load(f)

    answer = data.get("answer") or ""
    refs = data.get("references") or []
    cited_counts: collections.Counter[str] = collections.Counter()
    cited_hedging_hits = 0

    for ref in refs:
        sid = ref.get("source_id") or ""
        if sid:
            cited_counts[sid] += 1
        cited_hedging_hits += len(HEDGING_RE.findall(ref.get("cited_text") or ""))

    answer_hedging_hits = len(HEDGING_RE.findall(answer))
    total_refs = sum(cited_counts.values())

    expected_pairs = _resolve_expected_sources(notebook_id, expected_ids or [])
    uncited: list[dict] = []
    cited_matched: list[dict] = []

    if expected_pairs:
        for exp_id, title in expected_pairs:
            matched_cnt = sum(cnt for c_sid, cnt in cited_counts.items() if c_sid.startswith(exp_id) or exp_id.startswith(c_sid))
            entry = {"source_id": exp_id[:8], "full_id": exp_id, "title": title, "ref_count": matched_cnt}
            if matched_cnt > 0:
                cited_matched.append(entry)
            else:
                uncited.append(entry)
    else:
        cited_matched = [{"source_id": sid[:8], "full_id": sid, "title": "", "ref_count": cnt} for sid, cnt in cited_counts.items()]

    dominant_citations = [
        {"source_id": sid[:8], "refs": cnt, "share": round(cnt / max(total_refs, 1), 4)}
        for sid, cnt in cited_counts.items()
        if total_refs >= 5 and (cnt / total_refs) >= dominance_threshold
    ]

    certainty_inflation_risk = cited_hedging_hits >= 3 and (
        answer_hedging_hits == 0 or (answer_hedging_hits / cited_hedging_hits) < 0.25
    )

    exp_total = len(expected_pairs) if expected_pairs else len(cited_counts)
    coverage_ratio = round(len(cited_matched) / max(exp_total, 1), 4)
    passed = (len(uncited) == 0) and (not dominant_citations) and (not certainty_inflation_risk)

    return {
        "file": ask_json_path,
        "total_references": total_refs,
        "unique_cited_sources": len(cited_counts),
        "expected_sources": exp_total if expected_pairs else None,
        "coverage_ratio": coverage_ratio if expected_pairs else None,
        "passed": passed,
        "uncited_sources": uncited,
        "dominant_citations": dominant_citations,
        "hedging_audit": {
            "cited_text_hedging_cues": cited_hedging_hits,
            "answer_hedging_cues": answer_hedging_hits,
            "certainty_inflation_risk": certainty_inflation_risk,
        },
        "cited_sources": cited_matched,
    }


def _load_fulltext(notebook_id: str, source_id: str, cache_dir: str | None) -> tuple[str, str]:
    """Fetch source fulltext title + content via local cache or `notebooklm source fulltext`."""
    cache_path = os.path.join(cache_dir, f"{source_id}.json") if cache_dir else ""
    if cache_path and os.path.isfile(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("title", source_id), data.get("content") or data.get("text") or ""

    data = run_cli(["source", "fulltext", source_id, "-n", notebook_id, "--json"])
    if "_error" in data and "content" not in data:
        raise RuntimeError(f"Failed to fetch fulltext for {source_id}: {data['_error']}")
    _write_json_out(cache_path, data)
    return data.get("title", source_id), data.get("content") or data.get("text") or ""


def _build_pair_regex(pair_spec: str) -> re.Pattern[str]:
    """Compile normalized range query `A:B` into boundary- and connector-aware regex."""
    if ":" not in pair_spec:
        raise ValueError(f"Expected A:B interval syntax, got: {pair_spec}")
    left, right = pair_spec.split(":", 1)
    left_n = UNICODE_MINUS_RE.sub("-", left.strip())
    right_n = UNICODE_MINUS_RE.sub("-", right.strip())
    left_guard = r"(?<![\d.])" if left_n.startswith("-") else r"(?<![\d.\-])"
    return re.compile(
        rf"{left_guard}{re.escape(left_n)}(?=[^\d.]{{0,20}}(?:~|-|to|and|至))[^\d.]{{0,20}}{re.escape(right_n)}(?![\d.])",
        re.I,
    )


def trace_claims(
    notebook_id: str,
    source_ids: list[str],
    terms: list[str],
    pairs: list[str],
    use_regex: bool = False,
    cache_dir: str | None = None,
) -> dict:
    """Trace literal terms or numeric interval pairs across normalized `source fulltext`."""
    resolved_ids = list(source_ids)
    if not resolved_ids:
        listing = run_cli(["source", "list", "-n", notebook_id, "--json"])
        if "_error" in listing:
            raise RuntimeError(f"Failed to list sources: {listing['_error']}")
        resolved_ids = [s["id"] for s in listing.get("sources", []) if s.get("status") == "ready"]

    compiled_queries: list[tuple[str, re.Pattern[str]]] = []
    for t in terms:
        t_norm = UNICODE_TILDE_RE.sub("~", UNICODE_MINUS_RE.sub("-", t.strip()))
        pat_str = t_norm if use_regex else re.escape(re.sub(r"\s+", "", t_norm))
        compiled_queries.append((t, re.compile(pat_str, re.I)))
    for p in pairs:
        compiled_queries.append((f"pair:{p}", _build_pair_regex(p)))

    per_source: list[dict] = []
    totals: collections.Counter[str] = collections.Counter()

    for sid in resolved_ids:
        title, raw = _load_fulltext(notebook_id, sid, cache_dir)
        _, compact = normalize_for_match(raw)
        matches_by_query: dict[str, list[dict]] = {}
        for q_label, pat in compiled_queries:
            found = []
            for m in pat.finditer(compact):
                s = max(0, m.start() - 35)
                e = min(len(compact), m.end() + 45)
                found.append({"compact_offset": m.start(), "match": m.group(0), "context": compact[s:e]})
                if len(found) >= 5:
                    break
            total_hits = len(list(pat.finditer(compact)))
            totals[q_label] += total_hits
            matches_by_query[q_label] = found if total_hits else []
        per_source.append(
            {
                "source_id": sid[:8],
                "full_id": sid,
                "title": title,
                "query_hits": {q: len(list(pat.finditer(compact))) for q, pat in compiled_queries},
                "samples": matches_by_query,
            }
        )

    absent_queries = [q for q, _ in compiled_queries if totals[q] == 0]
    return {
        "notebook_id": notebook_id,
        "sources_checked": len(per_source),
        "total_hits": dict(totals),
        "absent_in_all_checked_sources": absent_queries,
        "per_source": per_source,
    }


def _print_csv_report(res: dict) -> None:
    s = res["summary"]
    print(f"CSV Audit: {res['file']} ({res['data_rows']} data rows) -> PASSED={s['passed']}")
    print(
        f"  HIGH numeric: {s['high_numeric_anomalies']} | MEDIUM: {s['medium_numeric_anomalies']} | "
        f"INFO magnitude: {s['info_large_magnitudes']} | Dominance: {s['dominant_sources']} | "
        f"Off-target: {s['off_target_rows']} | Duplicate splits: {s['possible_duplicate_splits']} | "
        f"Mixed units: {s['mixed_unit_warnings']}"
    )
    if res["dominant_sources"]:
        print("\n[WARN] Single-source dominance (likely secondary review monopolizing table):")
        for d in res["dominant_sources"]:
            print(f"  {d['source']} -> {d['rows']}/{res['data_rows']} rows ({d['ratio']*100:.1f}%)")
    if res["off_target_rows"]:
        print("\n[WARN] Off-target subject/study rows:")
        for o in res["off_target_rows"]:
            print(f"  Line {o['csv_line']} ({o['source']}) | {o['paper']} | target={o['target_value']}")
    if res["possible_duplicate_splits"]:
        print("\n[WARN] Possible duplicate paper splits (e.g. preprint/OnlineFirst vs print or review vs primary):")
        for dup in res["possible_duplicate_splits"]:
            a, b = dup["row_a"], dup["row_b"]
            print(f"  Key '{dup['author_key']}': Line {a['csv_line']} '{a['paper']}' vs Line {b['csv_line']} '{b['paper']}'")
    if res["mixed_unit_warnings"]:
        print("\n[INFO/WARN] Cross-row unit, scale, or statistical metric variation in the same column:")
        for uw in res["mixed_unit_warnings"]:
            print(f"  [{uw['column']}] {uw['unit_family']}: {uw['labels_by_row_count']}")
    if res["cell_findings"]:
        print("\n[CELL FLAGS]")
        for f in res["cell_findings"]:
            print(f"  [{f['severity']}] Line {f['csv_line']} | {f['paper']} | [{f['column']}] {f['rule']}: {f['match']}")


def _print_coverage_report(res: dict) -> None:
    cov = f"{res['coverage_ratio']*100:.1f}% ({res['unique_cited_sources']}/{res['expected_sources']})" if res["expected_sources"] else f"{res['unique_cited_sources']} unique"
    print(f"Coverage & Hedging Audit: {res['file']} -> PASSED={res['passed']}")
    print(f"  References: {res['total_references']} | Source coverage: {cov}")
    ha = res["hedging_audit"]
    print(
        f"  Hedging cues -> cited_text: {ha['cited_text_hedging_cues']} | answer: {ha['answer_hedging_cues']} "
        f"| certainty_inflation_risk={ha['certainty_inflation_risk']}"
    )
    if res["uncited_sources"]:
        print("\n[WARN] Uncited expected sources (silent omission risk):")
        for u in res["uncited_sources"]:
            print(f"  - {u['source_id']} | {u['title'][:70]}")
    if res["dominant_citations"]:
        print("\n[WARN] Citation dominance:")
        for d in res["dominant_citations"]:
            print(f"  - {d['source_id']} -> {d['refs']}/{res['total_references']} refs ({d['share']*100:.1f}%)")


def _print_trace_report(res: dict) -> None:
    print(f"Normalized Claim Trace ({res['sources_checked']} source(s))")
    print(f"  Total hits: {res['total_hits']}")
    if res["absent_in_all_checked_sources"]:
        print(f"  ABSENT in selected sources: {res['absent_in_all_checked_sources']}")
    for src in res["per_source"]:
        print(f"\n- {src['source_id']} | {src['title'][:60]} -> hits={src['query_hits']}")
        for q, samples in src["samples"].items():
            for samp in samples[:2]:
                print(f"    [{q}] @{samp['compact_offset']}: {samp['context']}")


def _normalize_negative_option_values(argv: list[str]) -> list[str]:
    """Attach negative numeric values like `--pair -1.8:0.4` as `--pair=-1.8:0.4` for argparse."""
    out: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in {"--pair", "--term"} and i + 1 < len(argv) and re.match(r"^-\d", argv[i + 1]):
            out.append(f"{tok}={argv[i + 1]}")
            i += 2
            continue
        out.append(tok)
        i += 1
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify NotebookLM outputs against scientific integrity gates.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_csv = sub.add_parser("scan-csv", help="Audit a data-table CSV for glued numbers, dominance, drift, and mixed units.")
    p_csv.add_argument("csv_path", help="Path to CSV exported from notebooklm download data-table")
    p_csv.add_argument("--target-focus", default="", help="Comma-separated expected study target terms (e.g. TargetA,TargetB)")
    p_csv.add_argument("--code-prefixes", default="", help="Extra domain series prefixes for hyphen-drop scan (e.g. Py,Mar)")
    p_csv.add_argument("--dominance-threshold", type=float, default=0.25, help="Max allowed ratio per source tag (0.25)")
    p_csv.add_argument("--json", action="store_true", help="Print JSON output")
    p_csv.add_argument("--out", default="", help="Optional path to save JSON report")
    p_csv.add_argument("--strict", action="store_true", help="Exit 1 if any HIGH numeric anomaly or structural warning exists")

    p_cov = sub.add_parser("check-coverage", help="Audit ask --json for uncited sources, citation dominance, and certainty inflation.")
    p_cov.add_argument("ask_json", help="Path to saved output from `notebooklm ask ... --json`")
    p_cov.add_argument("-n", "--notebook", default="", help="Notebook ID to resolve full ready source list and titles")
    p_cov.add_argument("--expected-id", action="append", default=[], help="Expected source ID prefix (repeatable; empty = all ready)")
    p_cov.add_argument("--dominance-threshold", type=float, default=0.45, help="Max citation share for one source (0.45)")
    p_cov.add_argument("--json", action="store_true", help="Print JSON output")
    p_cov.add_argument("--out", default="", help="Optional path to save JSON report")
    p_cov.add_argument("--strict", action="store_true", help="Exit 1 if uncited sources, dominance, or certainty risk is detected")

    p_tr = sub.add_parser("trace-claim", help="Trace terms or A:B numeric intervals in normalized source fulltext.")
    p_tr.add_argument("-n", "--notebook", required=True, help="Notebook ID or unique prefix")
    p_tr.add_argument("-s", "--source", action="append", default=[], help="Source ID (repeatable; empty = all ready)")
    p_tr.add_argument("--term", action="append", default=[], help="Phrase or regex term to search (repeatable)")
    p_tr.add_argument("--pair", action="append", default=[], help="Numeric range pair A:B (e.g. -1.8:0.4)")
    p_tr.add_argument("--regex", action="store_true", help="Interpret --term arguments as Python regular expressions")
    p_tr.add_argument("--cache-dir", default="", help="Optional directory to cache downloaded fulltext JSONs")
    p_tr.add_argument("--json", action="store_true", help="Print JSON output")
    p_tr.add_argument("--out", default="", help="Optional path to save JSON report")
    p_tr.add_argument("--strict", action="store_true", help="Exit 1 if any queried term/pair has 0 total hits")

    args = parser.parse_args(_normalize_negative_option_values(sys.argv[1:]))

    if args.cmd == "scan-csv":
        focus = [x.strip() for x in args.target_focus.split(",") if x.strip()]
        extra_pf = [x.strip() for x in args.code_prefixes.split(",") if x.strip()]
        res = scan_csv_file(
            args.csv_path,
            target_focus=focus,
            dominance_threshold=args.dominance_threshold,
            extra_code_prefixes=extra_pf,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2)) if args.json else _print_csv_report(res)
        _write_json_out(args.out, res)
        return 1 if (args.strict and not res["summary"]["passed"]) else 0

    if args.cmd == "check-coverage":
        res = check_ask_coverage(
            args.ask_json,
            notebook_id=args.notebook or None,
            expected_ids=args.expected_id,
            dominance_threshold=args.dominance_threshold,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2)) if args.json else _print_coverage_report(res)
        _write_json_out(args.out, res)
        return 1 if (args.strict and not res["passed"]) else 0

    if not args.term and not args.pair:
        print("[ERROR] trace-claim requires at least one --term or --pair", file=sys.stderr)
        return 2
    res = trace_claims(
        args.notebook,
        args.source,
        terms=args.term,
        pairs=args.pair,
        use_regex=args.regex,
        cache_dir=args.cache_dir or None,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2)) if args.json else _print_trace_report(res)
    _write_json_out(args.out, res)
    return 1 if (args.strict and res["absent_in_all_checked_sources"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
