#!/usr/bin/env python
"""Export NotebookLM sources as clean Markdown with extracted figure files.

Fetches `source fulltext --format markdown` for ready sources in a notebook,
decodes embedded `data:image/...;base64,...` images into a `figures/` folder,
and writes clean Markdown files with relative image links.

Usage:
    python export_md_figures.py -n <NOTEBOOK_ID> -o <OUTPUT_DIR>
    python export_md_figures.py -n <NOTEBOOK_ID> -s <SOURCE_ID> -o <OUTPUT_DIR> --keep-raw
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import struct
import subprocess
import sys
import tempfile

DATA_IMG_RE = re.compile(
    r"!\[([^\]]*)\]\(data:image/([a-zA-Z0-9+.-]+);base64,([A-Za-z0-9+/=\s]+)\)"
)
UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|]+')


def safe_stem(title: str, max_len: int = 70) -> str:
    """Sanitize a source title into a safe Windows/POSIX filename stem."""
    cleaned = UNSAFE_NAME_RE.sub("_", title).strip().rstrip(".")
    if cleaned.lower().endswith(".pdf"):
        cleaned = cleaned[:-4]
    return (cleaned[:max_len].strip() or "untitled")


def run_cli(args: list[str], timeout: int = 60) -> tuple[int, dict, str]:
    """Run a `notebooklm` CLI command and parse any JSON object from stdout."""
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
        return 124, {}, f"CLI timeout after {timeout}s"
    payload: dict = {}
    m = re.search(r"\{.*\}", proc.stdout, re.S)
    if m:
        try:
            payload = json.loads(m.group(0))
        except json.JSONDecodeError:
            payload = {}
    err = proc.stderr.strip() or payload.get("message") or proc.stdout.strip()[:200]
    return proc.returncode, payload, err


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read pixel dimensions (width, height) from PNG or JPEG bytes."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                break
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return w, h
            if marker in (0xD8, 0xD9, 0x01) or (0xD0 <= marker <= 0xD7):
                i += 2
                continue
            seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
            i += 2 + seg_len
    return None


def split_base64_images(
    md_text: str, stem: str, fig_dir: str, rel_fig_dir: str = "figures"
) -> tuple[str, list[dict]]:
    """Decode embedded base64 images into `fig_dir` and rewrite Markdown links."""
    os.makedirs(fig_dir, exist_ok=True)
    extracted: list[dict] = []

    def _replace(match: re.Match[str]) -> str:
        idx = len(extracted) + 1
        alt = match.group(1) or f"图{idx}"
        ext = match.group(2).lower().split("+")[0]
        if ext == "jpeg":
            ext = "jpg"
        b64_data = re.sub(r"\s+", "", match.group(3))
        try:
            img_bytes = base64.b64decode(b64_data)
        except Exception:
            return match.group(0)

        fname = f"{stem}_fig{idx:02d}.{ext}"
        fpath = os.path.join(fig_dir, fname)
        with open(fpath, "wb") as f:
            f.write(img_bytes)

        dims = image_dimensions(img_bytes)
        extracted.append(
            {
                "file": fname,
                "bytes": len(img_bytes),
                "width": dims[0] if dims else None,
                "height": dims[1] if dims else None,
            }
        )
        return f"![{alt}]({rel_fig_dir}/{fname})"

    clean_md = DATA_IMG_RE.sub(_replace, md_text)
    return clean_md, extracted


def export_notebook(
    notebook_id: str,
    out_dir: str,
    source_filter: list[str] | None = None,
    keep_raw: bool = False,
    limit: int | None = None,
) -> dict:
    """Export ready sources in `notebook_id` to clean Markdown + `figures/`."""
    rc, listing, err = run_cli(["source", "list", "-n", notebook_id, "--json"])
    if rc != 0:
        raise RuntimeError(f"Failed to list sources: {err}")

    sources = listing.get("sources", [])
    if source_filter:
        prefixes = [s.lower() for s in source_filter]
        sources = [
            s for s in sources if any(s.get("id", "").lower().startswith(p) for p in prefixes)
        ]

    fig_dir = os.path.join(out_dir, "figures")
    raw_dir = os.path.join(out_dir, "raw_md")
    os.makedirs(out_dir, exist_ok=True)
    if keep_raw:
        os.makedirs(raw_dir, exist_ok=True)

    used_stems: dict[str, int] = {}
    records: list[dict] = []
    processed = 0

    for src in sources:
        sid = src.get("id", "")
        title = src.get("title", "")
        status = src.get("status", "")
        src_type = src.get("type", "")

        if status != "ready":
            records.append(
                {
                    "id": sid,
                    "title": title,
                    "type": src_type,
                    "status": status,
                    "ok": False,
                    "reason": f"status={status}",
                }
            )
            continue

        if limit is not None and processed >= limit:
            break
        processed += 1

        stem = safe_stem(title)
        count = used_stems.get(stem, 0) + 1
        used_stems[stem] = count
        if count > 1:
            stem = f"{stem}_{count}"

        if keep_raw:
            raw_path = os.path.join(raw_dir, f"{stem}.md")
            tmp_ctx = None
        else:
            tmp_ctx = tempfile.NamedTemporaryFile(suffix=".md", delete=False)
            raw_path = tmp_ctx.name
            tmp_ctx.close()

        try:
            rc, _payload, err = run_cli(
                [
                    "source",
                    "fulltext",
                    sid,
                    "-n",
                    notebook_id,
                    "-f",
                    "markdown",
                    "-o",
                    raw_path,
                    "--force",
                    "--json",
                ]
            )
            if rc != 0 or not os.path.exists(raw_path):
                records.append(
                    {
                        "id": sid,
                        "title": title,
                        "type": src_type,
                        "status": status,
                        "ok": False,
                        "reason": err or "fulltext failed",
                    }
                )
                continue

            with open(raw_path, "r", encoding="utf-8", errors="replace") as f:
                raw_md = f.read()

            if not raw_md.strip():
                records.append(
                    {
                        "id": sid,
                        "title": title,
                        "type": src_type,
                        "status": status,
                        "ok": False,
                        "reason": "empty markdown rendition",
                    }
                )
                continue

            clean_md, figs = split_base64_images(raw_md, stem, fig_dir)
            out_md_path = os.path.join(out_dir, f"{stem}.md")
            with open(out_md_path, "w", encoding="utf-8") as f:
                f.write(clean_md)

            records.append(
                {
                    "id": sid,
                    "title": title,
                    "type": src_type,
                    "status": status,
                    "ok": True,
                    "md_path": out_md_path,
                    "raw_bytes": len(raw_md.encode("utf-8")),
                    "clean_chars": len(clean_md),
                    "figure_count": len(figs),
                    "figures": figs,
                }
            )
        finally:
            if not keep_raw and os.path.exists(raw_path):
                os.remove(raw_path)

    manifest = {
        "notebook_id": notebook_id,
        "notebook_title": listing.get("notebook_title", ""),
        "total_sources": len(listing.get("sources", [])),
        "exported_count": sum(1 for r in records if r.get("ok")),
        "total_figures": sum(r.get("figure_count", 0) for r in records if r.get("ok")),
        "records": records,
    }
    manifest_path = os.path.join(out_dir, "_export_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export NotebookLM sources as clean Markdown + extracted figures."
    )
    parser.add_argument("-n", "--notebook", required=True, help="Notebook ID or unique prefix")
    parser.add_argument("-o", "--out", required=True, help="Output directory")
    parser.add_argument(
        "-s",
        "--source",
        action="append",
        default=[],
        help="Optional source ID prefix (repeatable)",
    )
    parser.add_argument(
        "--keep-raw",
        action="store_true",
        help="Also save raw Markdown with base64 images under <out>/raw_md/",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional max number of ready sources to export",
    )
    args = parser.parse_args()

    manifest = export_notebook(
        notebook_id=args.notebook,
        out_dir=os.path.abspath(args.out),
        source_filter=args.source or None,
        keep_raw=args.keep_raw,
        limit=args.limit,
    )

    print(
        f"Notebook: {manifest['notebook_title']} ({manifest['notebook_id']}) | "
        f"Exported: {manifest['exported_count']} | Figures: {manifest['total_figures']}"
    )
    print(f"{'ID':10} {'TYPE':10} {'CHARS':>8} {'FIGS':>5}  TITLE")
    print("-" * 80)
    for r in manifest["records"]:
        if r.get("ok"):
            print(
                f"{r['id'][:8]:10} {r['type']:10} {r['clean_chars']:>8} "
                f"{r['figure_count']:>5}  {r['title'][:48]}"
            )
        else:
            print(
                f"{r['id'][:8]:10} {str(r.get('type', '')):10} {'-':>8} "
                f"{'-':>5}  {r['title'][:36]} [SKIP: {r.get('reason')}]"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
