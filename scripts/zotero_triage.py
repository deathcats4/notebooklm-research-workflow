#!/usr/bin/env python
"""Pre-ingest Zotero triage, isolation governance, and PDF/fulltext QA gate.

Reuses `pdf_prepare.inspect_pdf` and `check_fulltext.audit_source_text` to audit
Zotero collections BEFORE uploading sources into NotebookLM or feeding a local
agent. Filters out non-literature items (notes/attachments), separates `primary`
empirical papers from `review`, `background`, `excluded`, and `off_target` noise,
resolves local PDF paths (from Better BibTeX `file` fields or Zotero `storage/`),
and emits actionable upload whitelists + repair queues.

Usage:
    python zotero_triage.py --collection-md col.md --children-md children.md \
        --bibtex refs.bib --storage-dir D:/Zotero/ZoteroData/storage \
        --primary-tags "主题/本体研究" --target-focus "TargetA,TargetB" \
        --out-json triage.json --out-whitelist whitelist.json
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import Counter
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import check_fulltext  # noqa: E402
import pdf_prepare  # noqa: E402

DEFAULT_EXCLUDE_TAGS = (
    "剔除",
    "排除",
    "跑题文献",
    "exclude",
    "excluded",
    "reject",
    "rejected",
    "off-topic",
    "off_topic",
    "irrelevant",
    "duplicate",
)
DEFAULT_REVIEW_TAGS = (
    "综述",
    "区域综述",
    "文献综述",
    "review",
    "meta-analysis",
    "systematic-review",
    "systematic review",
    "survey",
)
DEFAULT_BACKGROUND_TAGS = (
    "邻区矿床",
    "对比文献",
    "区域关键文献",
    "支撑文献",
    "background",
    "analog",
    "control",
    "comparison",
)

REVIEW_TITLE_RE = re.compile(
    r"(?:\ba\s+review\b|\bsystematic\s+review\b|\bmeta-analysis\b|\bliterature\s+review\b"
    r"|\bsurvey\s+of\b|研究综述|综述|研究进展|评述)",
    re.I,
)
GENERIC_PDF_STEM_RE = re.compile(
    r"^(?:main|paper|article|manuscript|document|download|fulltext|preprint|accepted|\d+(?:\.\d+)*)$",
    re.I,
)
REPAIR_VERDICTS = {
    "SCANNED_OR_BROKEN_TEXT",
    "SCAN_WITH_EMBEDDED_OCR",
    "BROKEN_CHAR_OR_DIGIT_ENCODING",
    "BROKEN_FONT_ENCODING",
}
LATEX_TEXT_CLEAN_RE = re.compile(r"[{}]")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _split_csv_arg(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in values or []:
        for part in raw.split(","):
            tok = part.strip()
            if tok:
                out.append(tok)
    return out


def _norm_title(title: str) -> str:
    cleaned = LATEX_TEXT_CLEAN_RE.sub("", title or "")
    return re.sub(r"[\W_]+", "", cleaned.lower())


def _extract_year(date_str: str) -> str:
    m = re.search(r"(?:19|20)\d{2}", date_str or "")
    return m.group(0) if m else ""


def _first_author_label(authors: str) -> str:
    if not authors:
        return "Unknown"
    first = authors.split(";")[0].split(" and ")[0].strip()
    if CJK_RE.search(first):
        cleaned = re.sub(r"[\s,{}\u3000]+", "", first)
        return cleaned or "Unknown"
    if "," in first:
        surname = first.split(",", 1)[0].strip()
    else:
        parts = first.split()
        surname = parts[0] if parts else first
    surname = re.sub(r"[\s{}]+", "", surname)
    return surname or "Unknown"


def build_clean_title(item: dict[str, Any]) -> str:
    author = _first_author_label(item.get("authors", ""))
    year = _extract_year(item.get("date", "")) or "n.d."
    raw_title = re.sub(r"\s+", " ", LATEX_TEXT_CLEAN_RE.sub("", item.get("title", ""))).strip()
    short_title = raw_title[:64].rstrip()
    if len(raw_title) > 64:
        short_title += "..."
    return f"{author} ({year}) {short_title}".strip()


def parse_collection_md(text: str) -> list[dict[str, Any]]:
    """Parse markdown produced by `zotero_get_collection_items`."""
    items: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_abstract = False

    def _flush() -> None:
        if current is not None:
            current["abstract"] = current.get("abstract", "").strip()
            items.append(current)

    for line in text.splitlines():
        hdr = re.match(r"^##\s+\d+\.\s+(.+?)\s*$", line)
        if hdr:
            _flush()
            current = {
                "title": hdr.group(1).strip(),
                "item_type": "",
                "item_key": "",
                "date": "",
                "authors": "",
                "abstract": "",
                "tags": [],
                "doi": "",
                "attachments_summary": "",
            }
            in_abstract = False
            continue
        if current is None:
            continue

        if line.startswith("**Type:**"):
            in_abstract = False
            current["item_type"] = line.split("**Type:**", 1)[1].strip()
        elif line.startswith("**Item Key:**"):
            in_abstract = False
            current["item_key"] = line.split("**Item Key:**", 1)[1].strip()
        elif line.startswith("**Date:**"):
            in_abstract = False
            current["date"] = line.split("**Date:**", 1)[1].strip()
        elif line.startswith("**Authors:**"):
            in_abstract = False
            current["authors"] = line.split("**Authors:**", 1)[1].strip()
        elif line.startswith("**Attachments:**"):
            in_abstract = False
            current["attachments_summary"] = line.split("**Attachments:**", 1)[1].strip()
        elif line.startswith("**Tags:**"):
            in_abstract = False
            current["tags"] = re.findall(r"`([^`]+)`", line.split("**Tags:**", 1)[1])
        elif line.startswith("**Abstract:**"):
            in_abstract = True
            current["abstract"] = line.split("**Abstract:**", 1)[1].strip()
        elif in_abstract:
            if line.startswith("*Showing ") or line.startswith("---"):
                in_abstract = False
            elif line.strip():
                current["abstract"] = (current["abstract"] + " " + line.strip()).strip()

    _flush()
    return items


def parse_children_md(text: str) -> dict[str, dict[str, Any]]:
    """Parse markdown produced by `zotero_get_items_children` keyed by parent item key."""
    children: dict[str, dict[str, Any]] = {}
    current_key = ""
    for line in text.splitlines():
        hdr = re.match(r"^##\s+.+?\(`([A-Z0-9]{8})`\)\s*$", line.strip())
        if hdr:
            current_key = hdr.group(1)
            children.setdefault(current_key, {"attachments": [], "notes_count": 0})
            continue
        if not current_key:
            continue
        att_m = re.match(
            r"^\s*-\s+\[([A-Z0-9]{8})\]\s+Attachment:\s+(.+?)\s+\(([^)]+)\)(?:\s+\[([^\]]+)\])?\s*$",
            line,
        )
        if att_m:
            children[current_key]["attachments"].append(
                {
                    "key": att_m.group(1),
                    "filename": att_m.group(2).strip(),
                    "mime": att_m.group(3).strip(),
                    "link_mode": (att_m.group(4) or "").strip(),
                }
            )
            continue
        if re.match(r"^\s*-\s+\[([A-Z0-9]{8})\]\s+Note:", line):
            children[current_key]["notes_count"] += 1
    return children


def _unescape_bib_path(raw: str) -> str:
    return raw.replace("\\\\", "\\").replace("\\:", ":").replace("\\;", ";").strip()


def _extract_pdf_paths_from_file_field(raw_file: str) -> list[str]:
    """Extract candidate local PDF paths from Better BibTeX `file = {...}` strings."""
    if not raw_file:
        return []
    # Split on unescaped semicolons while keeping `\;` intact before unescaping.
    raw_entries = re.split(r"(?<!\\);", raw_file)
    pdfs: list[str] = []
    for entry in raw_entries:
        part = entry.strip()
        if not part:
            continue
        # Handle optional Better BibTeX `description:path:mime` triple.
        unescaped_chunks = re.split(r"(?<!\\):", part)
        if len(unescaped_chunks) >= 3 and ("pdf" in unescaped_chunks[-1].lower() or "/" in unescaped_chunks[-1]):
            candidate = _unescape_bib_path(":".join(unescaped_chunks[1:-1]))
        elif len(unescaped_chunks) == 2 and len(unescaped_chunks[0].strip()) > 1:
            candidate = _unescape_bib_path(unescaped_chunks[1])
        else:
            candidate = _unescape_bib_path(part)
        if candidate.lower().endswith(".pdf"):
            pdfs.append(candidate)
    return pdfs


def parse_bibtex_text(text: str) -> list[dict[str, Any]]:
    """Balanced-brace BibTeX parser for `title`, `author`, `year`, `doi`, `keywords`, `file`."""
    entries: list[dict[str, Any]] = []
    idx = 0
    n = len(text)
    while idx < n:
        at_pos = text.find("@", idx)
        if at_pos < 0:
            break
        brace_pos = text.find("{", at_pos)
        if brace_pos < 0:
            break
        entry_type = text[at_pos + 1 : brace_pos].strip().lower()
        if entry_type in ("comment", "preamble", "string"):
            idx = brace_pos + 1
            continue

        depth = 0
        end_pos = brace_pos
        while end_pos < n:
            ch = text[end_pos]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            end_pos += 1
        if depth != 0:
            break

        body = text[brace_pos + 1 : end_pos]
        idx = end_pos + 1
        comma_pos = body.find(",")
        if comma_pos < 0:
            continue
        citekey = body[:comma_pos].strip()
        rest = body[comma_pos + 1 :]

        fields: dict[str, str] = {}
        pos = 0
        m = len(rest)
        while pos < m:
            while pos < m and rest[pos] in " \t\r\n,":
                pos += 1
            if pos >= m:
                break
            eq = rest.find("=", pos)
            if eq < 0:
                break
            fname = rest[pos:eq].strip().lower()
            pos = eq + 1
            while pos < m and rest[pos] in " \t\r\n":
                pos += 1
            if pos >= m:
                break
            if rest[pos] == "{":
                fdepth = 0
                start = pos + 1
                while pos < m:
                    if rest[pos] == "{":
                        fdepth += 1
                    elif rest[pos] == "}":
                        fdepth -= 1
                        if fdepth == 0:
                            break
                    pos += 1
                fval = rest[start:pos]
                pos += 1
            elif rest[pos] == '"':
                pos += 1
                start = pos
                while pos < m:
                    if rest[pos] == '"' and rest[pos - 1] != "\\":
                        break
                    pos += 1
                fval = rest[start:pos]
                pos += 1
            else:
                start = pos
                while pos < m and rest[pos] not in ",\r\n":
                    pos += 1
                fval = rest[start:pos].strip()
            fields[fname] = re.sub(r"\s+", " ", fval).strip()

        raw_title = LATEX_TEXT_CLEAN_RE.sub("", fields.get("title", "")).strip()
        keywords = [
            k.strip()
            for k in fields.get("keywords", "").split(",")
            if k.strip() and k.strip() not in ("/", "/-")
        ]
        entries.append(
            {
                "citekey": citekey,
                "title": raw_title,
                "norm_title": _norm_title(raw_title),
                "authors": fields.get("author", ""),
                "year": _extract_year(fields.get("year") or fields.get("date") or ""),
                "doi": fields.get("doi", "").strip(),
                "keywords": keywords,
                "pdf_paths": _extract_pdf_paths_from_file_field(fields.get("file", "")),
            }
        )
    return entries


def strip_zotero_fulltext_banner(text: str) -> str:
    """Strip `zotero_get_item_fulltext` metadata header down to `## Full Text` body."""
    marker = re.search(r"^---\s*\n+## Full Text\s*\n+", text, re.M)
    if marker:
        return text[marker.end() :].lstrip()
    return text


def _load_bibtex_entries(bib_paths: list[str]) -> list[dict[str, Any]]:
    resolved: list[str] = []
    for p in bib_paths:
        if os.path.isdir(p):
            resolved.extend(sorted(glob.glob(os.path.join(p, "*.bib"))))
        elif os.path.exists(p):
            resolved.append(p)
    entries: list[dict[str, Any]] = []
    for bp in resolved:
        with open(bp, encoding="utf-8", errors="replace") as f:
            entries.extend(parse_bibtex_text(f.read()))
    return entries


def _match_bibtex_entry(item: dict[str, Any], bib_entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    item_key = (item.get("item_key") or "").strip().lower()
    ntitle = _norm_title(item.get("title", ""))
    iyear = _extract_year(item.get("date", ""))
    candidates = [e for e in bib_entries if e.get("norm_title") and e["norm_title"] == ntitle]
    if not candidates and item_key:
        candidates = [e for e in bib_entries if item_key in (e.get("citekey") or "").lower()]
    if not candidates:
        return None
    if len(candidates) == 1 or not iyear:
        return candidates[0]
    for c in candidates:
        if c.get("year") == iyear:
            return c
    return candidates[0]


def _resolve_local_pdf(
    item: dict[str, Any],
    child_info: dict[str, Any] | None,
    bib_entry: dict[str, Any] | None,
    storage_dir: str,
) -> tuple[str | None, str, str]:
    """Return `(existing_pdf_path_or_None, attachment_filename, resolution_note)`."""
    candidate_paths: list[str] = []
    if item.get("pdf_path"):
        candidate_paths.append(item["pdf_path"])
    if bib_entry:
        candidate_paths.extend(bib_entry.get("pdf_paths", []))

    att_filename = ""
    if child_info:
        for att in child_info.get("attachments", []):
            is_pdf = "pdf" in att.get("mime", "").lower() or att.get("filename", "").lower().endswith(".pdf")
            if not is_pdf:
                continue
            fname = att.get("filename", "")
            if fname and fname != "(no filename)" and not att_filename:
                att_filename = fname
            if storage_dir and att.get("key") and fname and fname != "(no filename)":
                candidate_paths.append(os.path.join(storage_dir, att["key"], fname))

    for p in candidate_paths:
        if p and os.path.exists(p) and os.path.isfile(p):
            return os.path.abspath(p), att_filename or os.path.basename(p), "resolved"

    if candidate_paths:
        return None, att_filename or os.path.basename(candidate_paths[0]), f"missing_on_disk:{candidate_paths[0]}"
    return None, att_filename, "no_pdf_attachment"


def _matches_any_tag(tags: list[str], patterns: list[str]) -> list[str]:
    lowered_pats = [p.lower() for p in patterns if p]
    hits: list[str] = []
    for tag in tags:
        tl = tag.lower()
        for pat in lowered_pats:
            if tl == pat or pat in tl:
                hits.append(tag)
                break
    return hits


def classify_item_role(
    item: dict[str, Any],
    *,
    primary_tags: list[str],
    review_tags: list[str],
    background_tags: list[str],
    exclude_tags: list[str],
    target_focus: list[str],
) -> tuple[str, str]:
    """Classify a Zotero item into role + short justification reason."""
    itype = (item.get("item_type") or "").strip().lower()
    if itype in ("note", "attachment", "annotation"):
        return "SKIP_NON_LITERATURE", f"item_type={itype}"

    tags = item.get("tags", [])
    title = item.get("title", "")
    abstract = item.get("abstract", "")

    excl_hits = _matches_any_tag(tags, exclude_tags)
    if excl_hits:
        return "EXCLUDE", f"exclude_tag={excl_hits[0]}"

    rev_hits = _matches_any_tag(tags, review_tags)
    prim_hits = _matches_any_tag(tags, primary_tags) if primary_tags else []
    if rev_hits:
        return "REVIEW", f"review_tag={rev_hits[0]}"
    if not prim_hits and REVIEW_TITLE_RE.search(title):
        return "REVIEW", "review_title_cue"

    bg_hits = _matches_any_tag(tags, background_tags)
    if bg_hits:
        return "BACKGROUND", f"background_tag={bg_hits[0]}"

    searchable = f"{title}\n{abstract}\n{' '.join(tags)}".lower()
    has_focus = (not target_focus) or any(tf.lower() in searchable for tf in target_focus)

    if primary_tags:
        if prim_hits:
            return "PRIMARY", f"primary_tag={prim_hits[0]}"
        if not has_focus:
            return "UNCLASSIFIED_OFF_TARGET", "missing_primary_tag_and_target_focus"
        return "UNCLASSIFIED_CANDIDATE", "missing_primary_tag"

    if not has_focus:
        return "UNCLASSIFIED_OFF_TARGET", "missing_target_focus"
    return "PRIMARY", "default_collection_candidate"


def _compute_ingest_decision(
    role: str,
    pdf_path: str | None,
    pdf_audit: dict[str, Any] | None,
    ft_audit: dict[str, Any] | None,
) -> tuple[str, str]:
    """Map role + pre-ingest inspections to an actionable ingest & repair status."""
    if role == "SKIP_NON_LITERATURE":
        return "SKIP_NON_LITERATURE", "Omit note/attachment record from paper pool."
    if role == "EXCLUDE":
        return "DROP_EXCLUDED", "Excluded by tag gate; never ingest."
    if role == "UNCLASSIFIED_OFF_TARGET":
        return "HOLD_OFF_TARGET", "Untagged/off-target item; confirm scope before ingest."
    if role == "UNCLASSIFIED_CANDIDATE":
        return "HOLD_REVIEW_ROLE", "Candidate missing primary role tag; confirm primary vs review/background."
    if role == "REVIEW":
        return "ISOLATE_REVIEW", "Secondary review; isolate via `-s` / label and never mix into primary data-table."
    if role == "BACKGROUND":
        return "ISOLATE_BACKGROUND", "Background/analog study; isolate from primary target synthesis."

    # PRIMARY item health routing:
    if not pdf_path and not ft_audit:
        return "NEED_PDF", "No readable local PDF or Zotero fulltext available."

    pdf_verdict = (pdf_audit or {}).get("verdict", "")
    ft_verdict = (ft_audit or {}).get("verdict", "")
    ft_broken = ft_verdict.startswith("FAIL_")

    if pdf_verdict in REPAIR_VERDICTS or ft_broken:
        reasons = [x for x in (pdf_verdict, ft_verdict if ft_broken else "") if x]
        return (
            "REPAIR_BEFORE_INGEST",
            f"Do NOT upload raw PDF ({', '.join(reasons)}). Run Step 3B/3C -> `md2docx.py` first.",
        )
    if pdf_verdict == "VECTOR_HEAVY_DIGITAL":
        return (
            "INGEST_AND_CHECK_FIGURES",
            "Text clean, vector-heavy pages detected; upload PDF then audit `export_md_figures.py` / `extract-figs`.",
        )
    if pdf_verdict == "CLEAN_DIGITAL" or (not pdf_audit and ft_verdict == "OK"):
        return "READY_FOR_INGEST", "Pre-flight PDF/text checks passed; safe to ingest or read locally."
    return "INGEST_WITH_WARN", f"Check warning before citing ({ft_verdict or pdf_verdict})."


def triage_items(
    raw_items: list[dict[str, Any]],
    *,
    children_map: dict[str, dict[str, Any]] | None = None,
    bib_entries: list[dict[str, Any]] | None = None,
    storage_dir: str = "",
    fulltext_dir: str = "",
    primary_tags: list[str] | None = None,
    review_tags: list[str] | None = None,
    background_tags: list[str] | None = None,
    exclude_tags: list[str] | None = None,
    target_focus: list[str] | None = None,
    keywords: list[str] | None = None,
) -> dict[str, Any]:
    """Run role triage, local PDF inspection, and Zotero fulltext QA across items."""
    children_map = children_map or {}
    bib_entries = bib_entries or []
    p_tags = primary_tags or []
    r_tags = review_tags if review_tags is not None else list(DEFAULT_REVIEW_TAGS)
    b_tags = background_tags if background_tags is not None else list(DEFAULT_BACKGROUND_TAGS)
    e_tags = exclude_tags if exclude_tags is not None else list(DEFAULT_EXCLUDE_TAGS)
    t_focus = target_focus or []
    kw_list = keywords or []

    records: list[dict[str, Any]] = []
    role_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()

    for item in raw_items:
        key = (item.get("item_key") or "").strip()
        bib_entry = _match_bibtex_entry(item, bib_entries) if bib_entries else None
        child_info = children_map.get(key) if key else None

        doi = (item.get("doi") or (bib_entry or {}).get("doi") or "").strip()
        tags = list(dict.fromkeys((item.get("tags") or []) + (bib_entry or {}).get("keywords", [])))
        merged_item = {**item, "doi": doi, "tags": tags}

        role, role_reason = classify_item_role(
            merged_item,
            primary_tags=p_tags,
            review_tags=r_tags,
            background_tags=b_tags,
            exclude_tags=e_tags,
            target_focus=t_focus,
        )
        role_counts[role] += 1

        pdf_path, att_filename, path_status = _resolve_local_pdf(merged_item, child_info, bib_entry, storage_dir)

        pdf_audit: dict[str, Any] | None = None
        if role not in ("SKIP_NON_LITERATURE", "EXCLUDE") and pdf_path:
            raw_inspect = pdf_prepare.inspect_pdf(pdf_path)
            pdf_audit = {
                "verdict": raw_inspect["verdict"],
                "pages": raw_inspect["pages"],
                "total_chars": raw_inspect["total_chars"],
                "total_cjk": raw_inspect["total_cjk"],
                "total_digits": raw_inspect["total_digits"],
                "total_bad": raw_inspect["total_bad"],
                "total_ctrl": raw_inspect["total_ctrl"],
                "scan_pages": raw_inspect["scan_pages"],
                "vector_heavy_pages": raw_inspect["vector_heavy_pages"],
                "broken_cjk_fonts": len(raw_inspect["broken_cjk_fonts"]),
                "advice": raw_inspect["advice"],
            }

        ft_audit: dict[str, Any] | None = None
        if fulltext_dir and key and role not in ("SKIP_NON_LITERATURE", "EXCLUDE"):
            for ext in (".txt", ".md"):
                ft_path = os.path.join(fulltext_dir, f"{key}{ext}")
                if os.path.exists(ft_path):
                    with open(ft_path, encoding="utf-8", errors="replace") as f:
                        ft_body = strip_zotero_fulltext_banner(f.read())
                    ft_raw = check_fulltext.audit_source_text(
                        key,
                        merged_item.get("title", ""),
                        "pdf",
                        "ready",
                        ft_body,
                        kw_list,
                    )
                    ft_audit = {
                        "verdict": ft_raw["verdict"],
                        "chars": ft_raw["chars"],
                        "cjk_chars": ft_raw["cjk_chars"],
                        "clean_digits": ft_raw["clean_digits"],
                        "multi_digit_nums": ft_raw["multi_digit_nums"],
                        "replacement_chars": ft_raw["replacement_chars"],
                        "ctrl_chars": ft_raw["ctrl_chars"],
                    }
                    break

        action, action_advice = _compute_ingest_decision(role, pdf_path, pdf_audit, ft_audit)
        action_counts[action] += 1

        meta_flags: list[str] = []
        if role not in ("SKIP_NON_LITERATURE", "EXCLUDE"):
            if not doi:
                meta_flags.append("NO_DOI")
            if "metadata/needs-abstract" in tags or not (merged_item.get("abstract") or "").strip():
                meta_flags.append("NEEDS_ABSTRACT")
            stem = os.path.splitext(att_filename or "")[0]
            if stem and GENERIC_PDF_STEM_RE.match(stem):
                meta_flags.append("GENERIC_FILENAME")
            if path_status.startswith("missing_on_disk:"):
                meta_flags.append("PDF_MISSING_ON_DISK")

        clean_title = build_clean_title(merged_item)
        records.append(
            {
                "item_key": key,
                "citekey": (bib_entry or {}).get("citekey", ""),
                "title": merged_item.get("title", ""),
                "clean_upload_title": clean_title,
                "authors": merged_item.get("authors", ""),
                "date": merged_item.get("date", ""),
                "year": _extract_year(merged_item.get("date", "")),
                "doi": doi,
                "role": role,
                "role_reason": role_reason,
                "ingest_action": action,
                "action_advice": action_advice,
                "pdf_path": pdf_path,
                "attachment_filename": att_filename,
                "path_status": path_status,
                "notes_count": (child_info or {}).get("notes_count", 0),
                "metadata_flags": meta_flags,
                "pdf_inspect": pdf_audit,
                "zotero_fulltext_audit": ft_audit,
            }
        )

    primary_records = [r for r in records if r["role"] == "PRIMARY"]
    primary_ready = [r for r in primary_records if r["ingest_action"] in ("READY_FOR_INGEST", "INGEST_AND_CHECK_FIGURES")]
    primary_repair = [r for r in primary_records if r["ingest_action"] == "REPAIR_BEFORE_INGEST"]
    n_primary = len(primary_records)

    if n_primary <= 8:
        dual_engine = (
            f"Primary corpus has {n_primary} papers (<=8): consider reading original PDFs directly "
            "with Local Agent for exact tables/numbers, or upload clean whitelist to NotebookLM for narrative Q&A."
        )
    else:
        dual_engine = (
            f"Primary corpus has {n_primary} papers (>8): upload repaired/clean primary whitelist to NotebookLM "
            "for clustering & synthesis, and route complex multi-column tables back to Local Agent."
        )

    whitelist = {
        "primary_ready": [
            {
                "item_key": r["item_key"],
                "title": r["clean_upload_title"],
                "pdf_path": r["pdf_path"],
                "ingest_action": r["ingest_action"],
                "doi": r["doi"],
            }
            for r in primary_ready
        ],
        "primary_repair_queue": [
            {
                "item_key": r["item_key"],
                "title": r["clean_upload_title"],
                "pdf_path": r["pdf_path"],
                "pdf_verdict": (r["pdf_inspect"] or {}).get("verdict", ""),
                "fulltext_verdict": (r["zotero_fulltext_audit"] or {}).get("verdict", ""),
                "advice": r["action_advice"],
            }
            for r in primary_repair
        ],
        "review_isolated": [r["item_key"] for r in records if r["role"] == "REVIEW"],
        "background_isolated": [r["item_key"] for r in records if r["role"] == "BACKGROUND"],
        "excluded_or_held": [
            {"item_key": r["item_key"], "role": r["role"], "reason": r["role_reason"], "title": r["title"]}
            for r in records
            if r["role"] in ("EXCLUDE", "UNCLASSIFIED_OFF_TARGET", "UNCLASSIFIED_CANDIDATE", "SKIP_NON_LITERATURE")
        ],
    }

    return {
        "summary": {
            "total_input_items": len(raw_items),
            "role_counts": dict(role_counts),
            "action_counts": dict(action_counts),
            "primary_total": n_primary,
            "primary_ready_count": len(primary_ready),
            "primary_repair_count": len(primary_repair),
            "dual_engine_guidance": dual_engine,
        },
        "whitelist": whitelist,
        "items": records,
    }


def render_markdown_summary(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        "# Zotero 入库前分诊与文献体检报告",
        "",
        f"- 输入条目总数：`{s['total_input_items']}` | 核心一手文献：`{s['primary_total']}`（直接过检：`{s['primary_ready_count']}`，需先修复：`{s['primary_repair_count']}`）",
        f"- 角色分布：`{json.dumps(s['role_counts'], ensure_ascii=False)}`",
        f"- 双引擎分工建议：{s['dual_engine_guidance']}",
        "",
        "## 一手文献入库门禁总表",
        "",
        "| Key | 文献（上传建议标题） | PDF 体检 | 全文预检 | 放行决策 | 备注/风险标记 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for r in report["items"]:
        if r["role"] != "PRIMARY":
            continue
        pv = (r["pdf_inspect"] or {}).get("verdict", "N/A")
        fv = (r["zotero_fulltext_audit"] or {}).get("verdict", "N/A")
        flags = ", ".join(r["metadata_flags"]) or "-"
        lines.append(
            f"| `{r['item_key']}` | {r['clean_upload_title']} | `{pv}` | `{fv}` | **`{r['ingest_action']}`** | {flags} |"
        )

    lines.extend(["", "## 隔离池与拦截条目（不混入一手表）", ""])
    for r in report["items"]:
        if r["role"] == "PRIMARY":
            continue
        lines.append(
            f"- `{r['item_key']}` **[{r['role']}]** -> `{r['ingest_action']}` | {r['title']} ({r['role_reason']})"
        )
    lines.append("")
    return "\n".join(lines)


def _print_console_table(report: dict[str, Any]) -> None:
    s = report["summary"]
    print(f"Total items: {s['total_input_items']} | Roles: {s['role_counts']}")
    print(f"Actions: {s['action_counts']}")
    print(f"Routing: {s['dual_engine_guidance']}\n")
    print(f"{'KEY':9} {'ROLE':23} {'ACTION':24} {'PDF_INSPECT':29} {'FT':22} TITLE")
    print("-" * 136)
    for r in report["items"]:
        pv = (r["pdf_inspect"] or {}).get("verdict", "-")
        fv = (r["zotero_fulltext_audit"] or {}).get("verdict", "-")
        print(
            f"{(r['item_key'] or '-'):9} {r['role']:23} {r['ingest_action']:24} "
            f"{pv:29} {fv:22} {r['title'][:40]}"
        )


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Pre-ingest Zotero triage and PDF/fulltext gate.")
    parser.add_argument("--collection-md", default="", help="Markdown from zotero_get_collection_items")
    parser.add_argument("--items-json", default="", help="Optional JSON list or {'items': [...]} input")
    parser.add_argument("--children-md", default="", help="Optional markdown from zotero_get_items_children")
    parser.add_argument("--bibtex", nargs="*", default=[], help="BibTeX file(s) or directory for DOI & file paths")
    parser.add_argument("--storage-dir", default="", help="Zotero storage directory (contains <ATT_KEY>/<file>)")
    parser.add_argument("--fulltext-dir", default="", help="Directory of <ITEM_KEY>.txt/.md Zotero fulltexts")
    parser.add_argument("--primary-tags", action="append", default=[], help="Comma-separated primary role tags")
    parser.add_argument("--review-tags", action="append", default=[], help="Extra review role tags")
    parser.add_argument("--background-tags", action="append", default=[], help="Extra background/analog role tags")
    parser.add_argument("--exclude-tags", action="append", default=[], help="Extra exclusion tags")
    parser.add_argument("--target-focus", action="append", default=[], help="Expected target terms to catch drift")
    parser.add_argument("--keywords", default="", help="Comma-separated domain keywords for fulltext audit")
    parser.add_argument("--out-json", default="", help="Save full JSON triage report")
    parser.add_argument("--out-whitelist", default="", help="Save clean whitelist & isolation JSON")
    parser.add_argument("--out-md", default="", help="Save markdown summary table")
    parser.add_argument("--json", action="store_true", help="Print JSON report to stdout")
    parser.add_argument("--strict", action="store_true", help="Exit 1 if any PRIMARY item needs repair or lacks PDF")
    args = parser.parse_args()

    raw_items: list[dict[str, Any]] = []
    if args.items_json:
        with open(args.items_json, encoding="utf-8") as f:
            payload = json.load(f)
        raw_items.extend(payload["items"] if isinstance(payload, dict) else payload)
    if args.collection_md:
        with open(args.collection_md, encoding="utf-8", errors="replace") as f:
            raw_items.extend(parse_collection_md(f.read()))
    if not raw_items:
        parser.error("Provide --collection-md and/or --items-json")

    children_map: dict[str, dict[str, Any]] = {}
    if args.children_md:
        with open(args.children_md, encoding="utf-8", errors="replace") as f:
            children_map = parse_children_md(f.read())

    bib_entries = _load_bibtex_entries(args.bibtex) if args.bibtex else []
    review_tags = list(DEFAULT_REVIEW_TAGS) + _split_csv_arg(args.review_tags)
    background_tags = list(DEFAULT_BACKGROUND_TAGS) + _split_csv_arg(args.background_tags)
    exclude_tags = list(DEFAULT_EXCLUDE_TAGS) + _split_csv_arg(args.exclude_tags)

    report = triage_items(
        raw_items,
        children_map=children_map,
        bib_entries=bib_entries,
        storage_dir=args.storage_dir,
        fulltext_dir=args.fulltext_dir,
        primary_tags=_split_csv_arg(args.primary_tags),
        review_tags=review_tags,
        background_tags=background_tags,
        exclude_tags=exclude_tags,
        target_focus=_split_csv_arg(args.target_focus),
        keywords=_split_csv_arg([args.keywords]),
    )

    if args.out_json:
        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    if args.out_whitelist:
        with open(args.out_whitelist, "w", encoding="utf-8") as f:
            json.dump(report["whitelist"], f, ensure_ascii=False, indent=2)
    if args.out_md:
        with open(args.out_md, "w", encoding="utf-8") as f:
            f.write(render_markdown_summary(report))

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_console_table(report)

    has_primary_blockers = any(
        r["role"] == "PRIMARY" and r["ingest_action"] in ("REPAIR_BEFORE_INGEST", "NEED_PDF")
        for r in report["items"]
    )
    return 1 if (args.strict and has_primary_blockers) else 0


if __name__ == "__main__":
    raise SystemExit(main())
