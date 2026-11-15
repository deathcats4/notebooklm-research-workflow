#!/usr/bin/env python
"""Audit NotebookLM source parsing quality via `source fulltext`.

Checks each ready source in a notebook for total character count, CJK character
count, script/language class (`zh`/`latin`/`mixed`), leaked PDF font-name noise,
replacement characters, script-aware domain keyword hits, and a text preview.

Usage:
    python check_fulltext.py -n <NOTEBOOK_ID> [--keywords "kw1,kw2"] [--out report.json]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]")
IMG_RE = re.compile(r"!\[[^\]]*\]\(")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
URL_LINE_RE = re.compile(r"https?://\S+", re.I)
UUID_LINE_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
CJK_FONT_NOISE_RE = re.compile(
    r"\b(?:SimSun|NSimSun|SimHei|FangSong|KaiTi|MicrosoftYaHei|STSong|STHeiti|STKaiti|STFangsong"
    r"|FZ[A-Z0-9-]{2,}|ZHIGZ[A-Z0-9-]*|DY\d+|B\d+\+[A-Za-z0-9-]+|B\d+)\b"
)
FONT_NOISE_RE = re.compile(
    r"\b(?:SimSun|NSimSun|SimHei|FangSong|KaiTi|MicrosoftYaHei|STSong|STHeiti|STKaiti|STFangsong"
    r"|TimesNewRoman(?:PS(?:MT)?)?|Calibri|Helvetica|Cambria|CourierNew"
    r"|FZ[A-Z0-9-]{2,}|ZHIGZ[A-Z0-9-]*|DY\d+|B\d+\+[A-Za-z0-9-]+|B\d+"
    r"|CchtfrAdvTT[0-9a-fA-F]+|Adv(?:TT|OT|PS|P)[0-9A-Za-z_.]+|Nimbus(?:San|Rom)[A-Za-z0-9-]*)\b"
)


def run_cli(args: list[str], timeout: int = 45) -> dict:
    try:
        proc = subprocess.run(
            ["notebooklm"] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"_error": f"CLI timeout after {timeout}s", "_rc": 124}
    if proc.returncode != 0:
        return {"_error": proc.stderr.strip() or proc.stdout.strip(), "_rc": proc.returncode}
    m = re.search(r"\{.*\}", proc.stdout, re.S)
    if not m:
        return {"_error": "No JSON object in stdout", "_raw": proc.stdout[:300]}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        return {"_error": f"JSON parse error: {exc}"}


def _classify_language(text: str, cjk_chars: int) -> tuple[str, float]:
    non_ws = len(re.sub(r"\s+", "", text))
    cjk_ratio = round(cjk_chars / max(non_ws, 1), 4)
    if cjk_ratio >= 0.15:
        return "zh", cjk_ratio
    if cjk_ratio >= 0.02 or cjk_chars >= 100:
        return "mixed", cjk_ratio
    return "latin", cjk_ratio


def _split_keywords(keywords: list[str], lang: str) -> tuple[list[str], list[str]]:
    applicable: list[str] = []
    skipped: list[str] = []
    for kw in keywords:
        has_cjk = bool(CJK_RE.search(kw))
        has_latin = bool(LATIN_RE.search(kw))
        if lang == "latin" and has_cjk:
            skipped.append(kw)
        elif lang == "zh" and has_latin and not has_cjk:
            skipped.append(kw)
        else:
            applicable.append(kw)
    return applicable, skipped


def _count_keyword(text: str, text_lower: str, kw: str) -> int:
    if CJK_RE.search(kw):
        return text.count(kw)
    return text_lower.count(kw.lower())


def audit_source_text(
    sid: str,
    title: str,
    src_type: str | None,
    status: str,
    text: str,
    keywords: list[str],
) -> dict:
    """Compute script, noise, clean digit density, keyword hits, and quality verdict."""
    chars = len(text)
    cjk_chars = len(CJK_RE.findall(text))
    replacement_chars = text.count("\ufffd")
    ctrl_chars = len(CTRL_RE.findall(text))
    images = len(IMG_RE.findall(text))
    lang, cjk_ratio = _classify_language(text, cjk_chars)
    title_expects_cjk = len(CJK_RE.findall(title)) >= 4
    font_noise = len(FONT_NOISE_RE.findall(text))
    cjk_font_noise = len(CJK_FONT_NOISE_RE.findall(text))
    noise_density = round(font_noise / max(chars, 1), 5)
    cjk_noise_density = round(cjk_font_noise / max(chars, 1), 5)
    rep_ratio = round(replacement_chars / max(chars, 1), 5)
    sample = re.sub(r"\s+", " ", text).strip()[:120]

    # Strip URLs, UUIDs, and leaked font tokens before measuring scientific numeric density.
    clean_body = FONT_NOISE_RE.sub(" ", UUID_LINE_RE.sub(" ", URL_LINE_RE.sub(" ", text)))
    clean_digits = len(re.findall(r"\d", clean_body))
    multi_digit_nums = len(re.findall(r"\d{2,}", clean_body))
    digit_ratio = round(clean_digits / max(len(clean_body), 1), 4)

    text_lower = text.lower()
    keyword_hits = {kw: _count_keyword(text, text_lower, kw) for kw in keywords}
    applicable_keywords, skipped_keywords = _split_keywords(keywords, lang)

    if src_type == "image" and "lh3.googleusercontent.com/" in text and chars < 600:
        verdict = "OK_IMAGE_SOURCE"
    elif (
        chars < 500
        or rep_ratio > 0.15
        or (replacement_chars > 10 and (chars < 2000 or (title_expects_cjk and cjk_chars < 200)))
    ):
        verdict = "FAIL_BROKEN_OR_EMPTY"
    elif cjk_chars < 100 and (title_expects_cjk or (cjk_font_noise >= 10 and cjk_noise_density > 0.002)):
        verdict = "FAIL_LOST_CJK"
    elif chars >= 2000 and (
        ctrl_chars >= 10
        or clean_digits == 0
        or (font_noise >= 10 and noise_density > 0.002 and multi_digit_nums <= 2)
    ):
        verdict = "FAIL_LOST_DIGITS_OR_CTRL"
    elif applicable_keywords and sum(keyword_hits[k] for k in applicable_keywords) == 0:
        verdict = "WARN_ZERO_KEYWORD_HITS"
    elif replacement_chars > 10:
        verdict = "WARN_REPLACEMENT_NOISE"
    elif font_noise >= 10 and noise_density > 0.002:
        verdict = "WARN_FONT_NOISE"
    elif chars >= 3000 and multi_digit_nums <= 2 and digit_ratio < 0.003:
        verdict = "WARN_SPARSE_DIGITS"
    else:
        verdict = "OK"

    return {
        "id": sid[:8],
        "full_id": sid,
        "title": title,
        "type": src_type,
        "status": status,
        "chars": chars,
        "cjk_chars": cjk_chars,
        "cjk_ratio": cjk_ratio,
        "lang": lang,
        "title_expects_cjk": title_expects_cjk,
        "replacement_chars": replacement_chars,
        "ctrl_chars": ctrl_chars,
        "font_noise": font_noise,
        "noise_density": noise_density,
        "clean_digits": clean_digits,
        "multi_digit_nums": multi_digit_nums,
        "digit_ratio": digit_ratio,
        "images": images,
        "keyword_hits": keyword_hits,
        "applicable_keywords": applicable_keywords,
        "skipped_keywords": skipped_keywords,
        "sample": sample,
        "verdict": verdict,
    }


def _matches_only(src: dict, only_filters: list[str]) -> bool:
    if not only_filters:
        return True
    sid = (src.get("id") or "").lower()
    title = (src.get("title") or "").lower()
    return any(f.lower() in sid or f.lower() in title for f in only_filters)


def audit_notebook(
    notebook_id: str,
    keywords: list[str],
    only: list[str] | None = None,
    limit: int = 0,
) -> list[dict]:
    listing = run_cli(["source", "list", "-n", notebook_id, "--json"])
    if "_error" in listing:
        raise RuntimeError(f"Failed to list sources: {listing['_error']}")

    sources = [s for s in listing.get("sources", []) if _matches_only(s, only or [])]
    if limit > 0:
        sources = sources[:limit]

    results = []
    for src in sources:
        sid = src.get("id", "")
        title = src.get("title", "")
        status = src.get("status", "")
        if status != "ready":
            results.append(
                {
                    "id": sid[:8],
                    "full_id": sid,
                    "title": title,
                    "type": src.get("type"),
                    "status": status,
                    "chars": 0,
                    "cjk_chars": 0,
                    "cjk_ratio": 0.0,
                    "lang": "unknown",
                    "title_expects_cjk": len(CJK_RE.findall(title)) >= 4,
                    "replacement_chars": 0,
                    "font_noise": 0,
                    "noise_density": 0.0,
                    "images": 0,
                    "keyword_hits": {},
                    "applicable_keywords": [],
                    "skipped_keywords": [],
                    "sample": "",
                    "verdict": "SKIP_NOT_READY",
                }
            )
            continue

        ft = run_cli(["source", "fulltext", sid, "-n", notebook_id, "--json"])
        if "_error" in ft:
            results.append(
                {
                    "id": sid[:8],
                    "full_id": sid,
                    "title": title,
                    "type": src.get("type"),
                    "status": status,
                    "chars": 0,
                    "cjk_chars": 0,
                    "cjk_ratio": 0.0,
                    "lang": "unknown",
                    "title_expects_cjk": len(CJK_RE.findall(title)) >= 4,
                    "replacement_chars": 0,
                    "font_noise": 0,
                    "noise_density": 0.0,
                    "images": 0,
                    "keyword_hits": {},
                    "applicable_keywords": [],
                    "skipped_keywords": [],
                    "sample": "",
                    "verdict": f"ERROR: {ft['_error'][:80]}",
                }
            )
            continue

        text = ft.get("content") or ft.get("text") or ""
        results.append(audit_source_text(sid, title, src.get("type"), status, text, keywords))

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit NotebookLM sources using source fulltext.")
    parser.add_argument("-n", "--notebook", required=True, help="Notebook ID or unique prefix")
    parser.add_argument("--keywords", default="", help="Comma-separated keywords to count per source")
    parser.add_argument(
        "--only",
        nargs="*",
        action="extend",
        default=[],
        help="Only audit sources whose ID or title matches any substring",
    )
    parser.add_argument("--limit", type=int, default=0, help="Max matched sources to audit (0 = all)")
    parser.add_argument("--json", action="store_true", help="Print JSON array to stdout")
    parser.add_argument("--out", default="", help="Optional path to save JSON report")
    parser.add_argument("--strict", action="store_true", help="Exit 1 if any FAIL_* or ERROR source exists")
    args = parser.parse_args()

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    rows = audit_notebook(args.notebook, keywords, only=args.only, limit=args.limit)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(
            f"{'ID':10} {'STATUS':7} {'LANG':6} {'CHARS':>8} {'CJK':>7} {'DIGITS':>7} {'BAD':>4} {'NOISE':>5} {'VERDICT':25} TITLE",
            flush=True,
        )
        print("-" * 124, flush=True)
        for r in rows:
            print(
                f"{r['id']:10} {str(r['status']):7} {r['lang']:6} {r['chars']:>8} {r['cjk_chars']:>7} "
                f"{r.get('clean_digits', 0):>7} {r['replacement_chars']:>4} {r['font_noise']:>5} "
                f"{r['verdict']:25} {r['title'][:36]}",
                flush=True,
            )
            if keywords and r["applicable_keywords"]:
                hits = ", ".join(f"{k}:{r['keyword_hits'][k]}" for k in r["applicable_keywords"])
                print(f"           keywords -> {hits}")
            if r["skipped_keywords"]:
                print(f"           skipped ({r['lang']}) -> {', '.join(r['skipped_keywords'])}")
            if r["verdict"].startswith(("FAIL_", "WARN_")) and r["sample"]:
                print(f"           sample -> {r['sample']}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        if not args.json:
            print(f"\nSaved audit report: {args.out}")

    has_fail = any(r["verdict"].startswith(("FAIL_", "ERROR:")) for r in rows)
    return 1 if (args.strict and has_fail) else 0


if __name__ == "__main__":
    raise SystemExit(main())
