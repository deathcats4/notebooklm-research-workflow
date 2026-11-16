#!/usr/bin/env python
"""Lightweight PDF inspection, page rendering, and figure recovery.

Uses `pymupdf` (and `pymupdf4llm` for intact vector-figure extraction) so an
agent can diagnose broken/scanned/vector-heavy PDFs, repair figures into clean
raster PDFs, and prepare clean inputs for `md2docx.py` on CPU with minimal deps.

Subcommands:
    inspect       Diagnose text layer, full-page scans, and vector-heavy pages
    render-pages  Render pages to crisp PNGs (+ raw text dump) for visual reading
    extract-figs  Extract intact composite figure PNGs via pymupdf4llm (+ stub md)
    crop          Crop a normalized [x0,y0,x1,y1] region of a PDF page at high DPI
    patch-fig     Flatten specific normalized figure regions in-place in a PDF copy
    pack-figs-pdf Pack extracted/cropped figure images (+ optional stub md) into a clean PDF
"""

from __future__ import annotations

import argparse
import html as htmlmod
import json
import os
import re
import sys

import pymupdf

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
IMG_LINK_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
IMG_TITLE_RE = re.compile(r"""\s+(?:"[^"]*"|'[^']*')\s*$""")
LEGACY_CJK_CMAP_PREFIXES = (
    "GBK",
    "GB-",
    "GBpc",
    "UniGB",
    "B5",
    "ETen",
    "HKscs",
    "UniCNS",
    "90ms",
    "83pv",
    "UniJIS",
    "KSC",
    "UniKS",
)


def _inspect_cjk_fonts(doc: pymupdf.Document) -> list[dict]:
    """Detect unembedded or non-ToUnicode Type0/CJK fonts that NotebookLM may misparse."""
    seen: set[tuple[str, str, str, bool]] = set()
    risky: list[dict] = []
    for page in doc:
        for xref, ext, ftype, name, _ref, enc in page.get_fonts():
            enc_str = enc or ""
            is_legacy_cmap = enc_str.startswith(LEGACY_CJK_CMAP_PREFIXES)
            is_unembedded_type0 = ftype == "Type0" and ext == "n/a"
            has_tounicode = "/ToUnicode" in doc.xref_object(xref) if xref > 0 else False
            is_type0_no_tounicode = ftype == "Type0" and not has_tounicode
            if not (is_legacy_cmap or is_unembedded_type0 or is_type0_no_tounicode):
                continue
            key = (name, ftype, enc_str, has_tounicode)
            if key in seen:
                continue
            seen.add(key)
            risky.append(
                {
                    "name": name,
                    "type": ftype,
                    "encoding": enc_str or "(none)",
                    "embedded": ext != "n/a",
                    "has_tounicode": has_tounicode,
                }
            )
    return risky


def _parse_pages(spec: str | None, total_pages: int) -> list[int]:
    """Parse 1-based page specs like '1,3-5' into 0-based sorted page indices."""
    if not spec:
        return list(range(total_pages))
    indices: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start = max(1, int(a))
            end = min(total_pages, int(b))
            indices.update(range(start - 1, end))
        else:
            idx = int(part) - 1
            if 0 <= idx < total_pages:
                indices.add(idx)
    return sorted(indices)


def _page_image_coverage(page: pymupdf.Page) -> float:
    """Return max fraction of page area covered by a single raster image."""
    page_area = max(page.rect.get_area(), 1.0)
    max_cov = 0.0
    for info in page.get_image_info(xrefs=False):
        bbox = pymupdf.Rect(info["bbox"])
        cov = bbox.get_area() / page_area
        if cov > max_cov:
            max_cov = cov
    return round(max_cov, 3)


def inspect_pdf(pdf_path: str) -> dict:
    """Inspect a PDF per page and classify its optimal processing strategy."""
    doc = pymupdf.open(pdf_path)
    pages_info: list[dict] = []
    total_chars = 0
    total_cjk = 0
    total_bad = 0
    total_ctrl = 0
    total_digits = 0
    scan_pages: list[int] = []
    vector_heavy_pages: list[int] = []

    for i, page in enumerate(doc):
        text = page.get_text()
        chars = len(text.strip())
        cjk = len(CJK_RE.findall(text))
        bad = text.count("\ufffd")
        ctrl = len(CTRL_RE.findall(text))
        digits = len(re.findall(r"\d", text))
        imgs = len(page.get_images())
        cov = _page_image_coverage(page)
        drawings = len(page.get_drawings())
        xobjs = len(page.get_xobjects())

        total_chars += chars
        total_cjk += cjk
        total_bad += bad
        total_ctrl += ctrl
        total_digits += digits

        if cov >= 0.70:
            scan_pages.append(i + 1)
        if drawings >= 200 or xobjs >= 10:
            vector_heavy_pages.append(i + 1)

        pages_info.append(
            {
                "page": i + 1,
                "chars": chars,
                "cjk": cjk,
                "bad": bad,
                "ctrl": ctrl,
                "digits": digits,
                "imgs": imgs,
                "max_img_cov": cov,
                "drawings": drawings,
                "xobjects": xobjs,
            }
        )

    broken_cjk_fonts = _inspect_cjk_fonts(doc)
    page1_head = re.sub(r"\s+", " ", doc[0].get_text()).strip()[:300] if len(doc) else ""

    ctrl_ratio = total_ctrl / max(total_chars, 1)
    if total_chars < 300 or total_bad > 20:
        verdict = "SCANNED_OR_BROKEN_TEXT"
        advice = "Run `render-pages` and transcribe visually into Markdown -> md2docx.py."
    elif len(scan_pages) >= max(1, len(doc) // 2):
        verdict = "SCAN_WITH_EMBEDDED_OCR"
        advice = (
            "Full-page scan images detected (`pymupdf4llm` will drop overlaid text). "
            "Use `render-pages` for visual cleaning -> md2docx.py."
        )
    elif (total_chars >= 2000 and total_digits < 15) or (total_ctrl >= 20 and ctrl_ratio > 0.05) or (total_ctrl >= 10 and total_digits < 15):
        verdict = "BROKEN_CHAR_OR_DIGIT_ENCODING"
        advice = (
            f"Detected {total_ctrl} control chars ({ctrl_ratio*100:.1f}%) and {total_digits} digits across {total_chars} chars "
            "(digits/symbols likely encoded into broken control glyphs). Run `render-pages` -> OCR -> md2docx.py."
        )
    elif broken_cjk_fonts and total_cjk > 0:
        verdict = "BROKEN_FONT_ENCODING"
        advice = (
            f"{len(broken_cjk_fonts)} unembedded/non-ToUnicode CJK font(s) detected "
            "(PyMuPDF can read via built-in CMaps, but NotebookLM PDF ingestion often drops/mojibakes "
            "these characters). Run `render-pages` for visual transcription -> md2docx.py instead of "
            "uploading raw PDF."
        )
    elif vector_heavy_pages:
        verdict = "VECTOR_HEAVY_DIGITAL"
        advice = (
            f"Vector-dense pages {vector_heavy_pages}: upload original PDF, then audit "
            "`figures/` via export_md_figures.py and use `extract-figs` if vector plots are sliced."
        )
    else:
        verdict = "CLEAN_DIGITAL"
        advice = "Upload original PDF directly."

    return {
        "pdf": os.path.abspath(pdf_path),
        "pages": len(doc),
        "total_chars": total_chars,
        "total_cjk": total_cjk,
        "total_bad": total_bad,
        "total_ctrl": total_ctrl,
        "total_digits": total_digits,
        "scan_pages": scan_pages,
        "vector_heavy_pages": vector_heavy_pages,
        "broken_cjk_fonts": broken_cjk_fonts,
        "page1_head": page1_head,
        "verdict": verdict,
        "advice": advice,
        "per_page": pages_info,
    }


def render_pages(pdf_path: str, out_dir: str, pages_spec: str | None, dpi: int) -> dict:
    """Render selected pages to PNG and dump raw text for agent visual transcription."""
    doc = pymupdf.open(pdf_path)
    indices = _parse_pages(pages_spec, len(doc))
    pages_dir = os.path.join(out_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)

    rendered: list[str] = []
    raw_chunks: list[str] = []
    for idx in indices:
        page = doc[idx]
        pno = idx + 1
        out_png = os.path.join(pages_dir, f"page_{pno:03d}.png")
        page.get_pixmap(dpi=dpi).save(out_png)
        rendered.append(out_png)
        raw_chunks.append(f"===== PAGE {pno} =====\n{page.get_text().strip()}\n")

    raw_txt_path = os.path.join(out_dir, "raw_text.txt")
    with open(raw_txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(raw_chunks))

    return {
        "pdf": os.path.abspath(pdf_path),
        "rendered_pages": len(rendered),
        "pages_dir": pages_dir,
        "raw_text": raw_txt_path,
    }


def extract_figs(
    pdf_path: str,
    out_dir: str,
    pages_spec: str | None,
    dpi: int,
    paper_label: str | None,
    append_md: str | None,
) -> dict:
    """Extract intact composite vector/raster figure PNGs using `pymupdf4llm`."""
    import pymupdf4llm  # lazy import so `inspect`/`render-pages` stay fast

    doc = pymupdf.open(pdf_path)
    indices = _parse_pages(pages_spec, len(doc))
    fig_dir = os.path.join(out_dir, "figs")
    os.makedirs(fig_dir, exist_ok=True)

    md_text = pymupdf4llm.to_markdown(
        pdf_path,
        pages=indices,
        write_images=True,
        image_path=fig_dir,
        dpi=dpi,
    )

    label = paper_label or os.path.splitext(os.path.basename(pdf_path))[0]
    md_target = os.path.abspath(append_md) if append_md else os.path.join(out_dir, "figures_stub.md")
    md_parent = os.path.dirname(md_target)
    os.makedirs(md_parent, exist_ok=True)

    # Collect extracted images in Markdown appearance order and normalize paths relative to md_target.
    seen: set[str] = set()
    rel_imgs: list[str] = []
    for match in IMG_LINK_RE.finditer(md_text):
        raw_img = match.group(2).strip()
        abs_img = raw_img if os.path.isabs(raw_img) else os.path.abspath(raw_img)
        if not os.path.exists(abs_img) or abs_img in seen:
            continue
        seen.add(abs_img)
        rel_imgs.append(os.path.relpath(abs_img, md_parent).replace("\\", "/"))

    lines = [f"## 【所属文献】{label}", ""]
    for idx, rel in enumerate(rel_imgs, start=1):
        lines.append(f"### 图 {idx}（待核对图号与图注）")
        lines.append(f"![{label} 图{idx}]({rel})")
        lines.append("")

    mode = "a" if (append_md and os.path.exists(md_target)) else "w"
    with open(md_target, mode, encoding="utf-8") as f:
        if mode == "a":
            f.write("\n")
        f.write("\n".join(lines))

    raw_layout_md = os.path.join(out_dir, "_pymupdf4llm_raw.md")
    with open(raw_layout_md, "w", encoding="utf-8") as f:
        f.write(md_text)

    return {
        "pdf": os.path.abspath(pdf_path),
        "pages": [i + 1 for i in indices],
        "extracted_images": [os.path.relpath(p, md_parent) for p in sorted(seen)],
        "image_count": len(rel_imgs),
        "stub_md": md_target,
        "raw_layout_md": raw_layout_md,
    }


def _parse_rect(page: pymupdf.Page, rect_str: str) -> pymupdf.Rect:
    """Convert normalized 'x0,y0,x1,y1' coords (0..1) into page-space `Rect`."""
    coords = [float(x.strip()) for x in rect_str.split(",")]
    if len(coords) != 4:
        raise ValueError("--rect must be 4 comma-separated floats in [0, 1]: x0,y0,x1,y1")
    x0, y0, x1, y1 = coords
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        raise ValueError(f"Normalized coords must satisfy 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1: {coords}")
    pr = page.rect
    return pymupdf.Rect(
        pr.x0 + x0 * pr.width,
        pr.y0 + y0 * pr.height,
        pr.x0 + x1 * pr.width,
        pr.y0 + y1 * pr.height,
    )


def _parse_region(spec: str, total_pages: int) -> tuple[int, str]:
    """Parse 'PAGE:x0,y0,x1,y1' into a validated 1-based page number and rect string."""
    if ":" not in spec:
        raise ValueError(f"--region must follow 'PAGE:x0,y0,x1,y1': {spec!r}")
    page_part, rect_part = spec.split(":", 1)
    page_num = int(page_part.strip())
    if not (1 <= page_num <= total_pages):
        raise ValueError(f"Page {page_num} out of range (1..{total_pages})")
    return page_num, rect_part.strip()


def crop_region(pdf_path: str, page_num: int, rect_str: str, out_png: str, dpi: int) -> dict:
    """Render a normalized [x0,y0,x1,y1] region (0..1) of a 1-based page at high DPI."""
    doc = pymupdf.open(pdf_path)
    if not (1 <= page_num <= len(doc)):
        raise ValueError(f"Page {page_num} out of range (1..{len(doc)})")
    page = doc[page_num - 1]
    clip = _parse_rect(page, rect_str)
    os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
    pix = page.get_pixmap(dpi=dpi, clip=clip)
    pix.save(out_png)
    return {"out": os.path.abspath(out_png), "width": pix.width, "height": pix.height}


def patch_figures(
    pdf_path: str,
    regions: list[tuple[int, str]],
    out_pdf: str,
    dpi: int,
    keep_text: bool,
) -> dict:
    """Flatten specified figure boxes in-place while preserving surrounding page text."""
    src_abs = os.path.abspath(pdf_path)
    out_abs = os.path.abspath(out_pdf)
    if src_abs == out_abs:
        raise ValueError("Output path must differ from input PDF to protect the original file.")
    if not regions:
        raise ValueError("Provide at least one --region PAGE:x0,y0,x1,y1 or -p/--rect pair.")

    doc = pymupdf.open(src_abs)
    by_page: dict[int, list[str]] = {}
    for page_num, rect_str in regions:
        if not (1 <= page_num <= len(doc)):
            raise ValueError(f"Page {page_num} out of range (1..{len(doc)})")
        by_page.setdefault(page_num, []).append(rect_str)

    text_mode = pymupdf.PDF_REDACT_TEXT_NONE if keep_text else pymupdf.PDF_REDACT_TEXT_REMOVE
    page_reports: list[dict] = []

    for page_num in sorted(by_page):
        page = doc[page_num - 1]
        before = {
            "drawings": len(page.get_drawings()),
            "images": len(page.get_images()),
            "chars": len(page.get_text().strip()),
        }
        stamps: list[tuple[pymupdf.Rect, bytes, int, int]] = []
        # Render composite raster snapshots before applying redactions to the page.
        for rect_str in by_page[page_num]:
            rect = _parse_rect(page, rect_str)
            pix = page.get_pixmap(dpi=dpi, clip=rect, alpha=False)
            stamps.append((rect, pix.tobytes("png"), pix.width, pix.height))

        for rect, _, _, _ in stamps:
            page.add_redact_annot(rect, fill=False)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_REMOVE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
            text=text_mode,
        )

        stamped_dims: list[dict] = []
        for rect, png_bytes, width, height in stamps:
            page.insert_image(rect, stream=png_bytes, keep_proportion=False, overlay=True)
            stamped_dims.append(
                {
                    "rect_pt": [round(rect.x0, 1), round(rect.y0, 1), round(rect.x1, 1), round(rect.y1, 1)],
                    "pixels": [width, height],
                }
            )

        page_reports.append(
            {
                "page": page_num,
                "regions": stamped_dims,
                "drawings_before": before["drawings"],
                "drawings_after": len(page.get_drawings()),
                "images_before": before["images"],
                "images_after": len(page.get_images()),
                "chars_before": before["chars"],
                "chars_after": len(page.get_text().strip()),
            }
        )

    os.makedirs(os.path.dirname(out_abs), exist_ok=True)
    doc.save(out_abs, garbage=4, deflate=True)
    doc.close()

    return {
        "pdf": src_abs,
        "out": out_abs,
        "dpi": dpi,
        "keep_text": keep_text,
        "patched_pages": page_reports,
    }


def _collect_md_figure_items(md_path: str, img_base: str | None) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Collect `(paper_label, caption, abs_img_path)` entries from a figures Markdown file."""
    base_dir = os.path.abspath(img_base) if img_base else os.path.dirname(os.path.abspath(md_path))
    items: list[tuple[str, str, str]] = []
    missing: list[str] = []
    paper_label = ""
    caption = ""

    with open(md_path, encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("## "):
                paper_label = line[3:].strip()
                caption = ""
                continue
            if line.startswith("### "):
                caption = line[4:].strip()
                continue
            m = IMG_LINK_RE.match(line)
            if m:
                alt = m.group(1).strip()
                raw_target = IMG_TITLE_RE.sub("", m.group(2).strip()).strip().strip("<>")
                img_path = raw_target if os.path.isabs(raw_target) else os.path.abspath(os.path.join(base_dir, raw_target))
                if not os.path.exists(img_path):
                    missing.append(raw_target)
                    continue
                items.append((paper_label, caption or alt, img_path))
                caption = ""
                continue
            if not line.startswith("#"):
                caption = f"{caption} {line}".strip() if caption else line

    return items, missing


def pack_figs_pdf(inputs: list[str], out_pdf: str, img_base: str | None = None) -> dict:
    """Pack figure images (or a Markdown figure manifest) into a clean raster supplement PDF.

    Uses PyMuPDF `insert_htmlbox` for Unicode/CJK-safe headers (avoiding legacy CJK
    CMaps and Chromium font-name leakage) so NotebookLM indexes every figure visually.
    """
    items: list[tuple[str, str, str]] = []
    missing: list[str] = []
    for entry in inputs:
        if entry.lower().endswith(".md"):
            md_items, md_missing = _collect_md_figure_items(entry, img_base)
            items.extend(md_items)
            missing.extend(md_missing)
        elif os.path.exists(entry):
            items.append(("", os.path.splitext(os.path.basename(entry))[0], os.path.abspath(entry)))
        else:
            missing.append(entry)

    if not items:
        raise FileNotFoundError(f"No valid images found to pack into PDF (missing: {missing})")

    doc = pymupdf.open()
    margin = 28.0
    page_w = 595.0
    content_w = page_w - 2 * margin
    packed: list[dict] = []

    for paper_label, caption, img_path in items:
        pix = pymupdf.Pixmap(img_path)
        w, h = pix.width, pix.height
        scale = min(content_w / max(w, 1), 700.0 / max(h, 1))
        img_w, img_h = w * scale, h * scale

        header_parts = []
        if paper_label:
            header_parts.append(f"<div style='font-size:9.5pt;color:#444;'>{htmlmod.escape(paper_label)}</div>")
        if caption:
            header_parts.append(f"<div style='font-size:10.5pt;font-weight:bold;margin-top:2px;'>{htmlmod.escape(caption)}</div>")
        header_html = "".join(header_parts)
        header_h = 54.0 if (paper_label and caption) else (34.0 if header_parts else 0.0)
        if len(paper_label) + len(caption) > 110:
            header_h += 18.0

        page_h = max(320.0, margin + header_h + img_h + margin)
        page = doc.new_page(width=page_w, height=page_h)
        y_img = margin
        if header_html:
            page.insert_htmlbox(pymupdf.Rect(margin, margin, page_w - margin, margin + header_h), header_html)
            y_img = margin + header_h + 6.0

        x_img = margin + max(0.0, (content_w - img_w) / 2.0)
        page.insert_image(pymupdf.Rect(x_img, y_img, x_img + img_w, y_img + img_h), filename=img_path)
        packed.append({"paper": paper_label, "caption": caption, "image": img_path, "width": w, "height": h})

    out_abs = os.path.abspath(out_pdf)
    os.makedirs(os.path.dirname(out_abs), exist_ok=True)
    doc.save(out_abs, garbage=4, deflate=True)
    doc.close()

    return {
        "out": out_abs,
        "pages": len(packed),
        "missing_images": missing,
        "packed": packed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Lightweight PyMuPDF/pymupdf4llm PDF prep tools.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_insp = sub.add_parser("inspect", help="Diagnose PDF text layer, scan coverage, and vector pages.")
    p_insp.add_argument("pdf", help="Input PDF path")
    p_insp.add_argument("--json", action="store_true", help="Emit JSON output")

    p_ren = sub.add_parser("render-pages", help="Render PDF pages to PNG + raw text dump.")
    p_ren.add_argument("pdf", help="Input PDF path")
    p_ren.add_argument("-o", "--out", required=True, help="Output directory")
    p_ren.add_argument("-p", "--pages", default="", help="1-based page spec, e.g. '1,3-5'")
    p_ren.add_argument("--dpi", type=int, default=180, help="Render DPI (default: 180)")

    p_fig = sub.add_parser("extract-figs", help="Extract intact figure PNGs via pymupdf4llm.")
    p_fig.add_argument("pdf", help="Input PDF path")
    p_fig.add_argument("-o", "--out", required=True, help="Output directory")
    p_fig.add_argument("-p", "--pages", default="", help="1-based page spec, e.g. '6,7'")
    p_fig.add_argument("--dpi", type=int, default=200, help="Raster DPI for vector figures (default: 200)")
    p_fig.add_argument("--label", default="", help="Paper title/citation header for stub markdown")
    p_fig.add_argument("--append-md", default="", help="Append figure blocks to this consolidated .md")

    p_crop = sub.add_parser("crop", help="Crop a normalized 0..1 bounding box from a PDF page.")
    p_crop.add_argument("pdf", help="Input PDF path")
    p_crop.add_argument("-p", "--page", type=int, required=True, help="1-based page number")
    p_crop.add_argument("--rect", required=True, help="Normalized box: x0,y0,x1,y1 (0..1)")
    p_crop.add_argument("-o", "--out", required=True, help="Output PNG path")
    p_crop.add_argument("--dpi", type=int, default=240, help="Render DPI (default: 240)")

    p_patch = sub.add_parser(
        "patch-fig",
        help="Rasterize specified figure boxes in-place while preserving surrounding PDF text.",
    )
    p_patch.add_argument("pdf", help="Input PDF path")
    p_patch.add_argument(
        "--region",
        action="append",
        default=[],
        help="Repeatable normalized region 'PAGE:x0,y0,x1,y1' (1-based page, 0..1 coords)",
    )
    p_patch.add_argument("-p", "--page", type=int, default=0, help="Single 1-based page (pair with --rect)")
    p_patch.add_argument("--rect", default="", help="Normalized box x0,y0,x1,y1 (pair with -p/--page)")
    p_patch.add_argument("-o", "--out", required=True, help="Output patched PDF path")
    p_patch.add_argument("--dpi", type=int, default=240, help="Raster DPI for patched figures (default: 240)")
    p_patch.add_argument(
        "--keep-text",
        action="store_true",
        help="Retain text layer inside patched boxes instead of stripping chart labels",
    )

    p_pack = sub.add_parser(
        "pack-figs-pdf",
        help="Pack a figures Markdown manifest (or image paths) into a clean raster PDF.",
    )
    p_pack.add_argument("inputs", nargs="+", help="Markdown file (e.g. missing_figures.md) or image paths")
    p_pack.add_argument("-o", "--out", required=True, help="Output supplement PDF path")
    p_pack.add_argument("--img-base", default="", help="Base directory for relative image paths in Markdown")

    args = parser.parse_args()

    if args.cmd == "inspect":
        res = inspect_pdf(args.pdf)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(
                f"Verdict: {res['verdict']} | Pages: {res['pages']} | "
                f"Chars: {res['total_chars']} (CJK: {res['total_cjk']}, Digits: {res['total_digits']}, "
                f"Bad: {res['total_bad']}, Ctrl: {res['total_ctrl']})"
            )
            print(f"Scan pages: {res['scan_pages']} | Vector-heavy pages: {res['vector_heavy_pages']}")
            if res["broken_cjk_fonts"]:
                sample_fonts = ", ".join(
                    f"{f['name']}[{f['encoding']}, emb={f['embedded']}, toUni={f['has_tounicode']}]"
                    for f in res["broken_cjk_fonts"][:4]
                )
                print(f"Risky CJK fonts ({len(res['broken_cjk_fonts'])}): {sample_fonts}")
            print(f"Page 1 head: {res['page1_head'] or '(empty)'}")
            print(f"Advice: {res['advice']}")
        return 0

    if args.cmd == "render-pages":
        res = render_pages(args.pdf, os.path.abspath(args.out), args.pages or None, args.dpi)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "extract-figs":
        res = extract_figs(
            args.pdf,
            os.path.abspath(args.out),
            args.pages or None,
            args.dpi,
            args.label or None,
            args.append_md or None,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "crop":
        res = crop_region(args.pdf, args.page, args.rect, os.path.abspath(args.out), args.dpi)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "patch-fig":
        doc_tmp = pymupdf.open(args.pdf)
        total_pages = len(doc_tmp)
        doc_tmp.close()
        regions = [_parse_region(spec, total_pages) for spec in args.region]
        if args.page or args.rect:
            if not (args.page and args.rect):
                parser.error("-p/--page and --rect must be provided together.")
            regions.append((args.page, args.rect))
        if not regions:
            parser.error("Provide --region PAGE:x0,y0,x1,y1 or -p/--page with --rect.")
        res = patch_figures(args.pdf, regions, args.out, args.dpi, args.keep_text)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "pack-figs-pdf":
        res = pack_figs_pdf(args.inputs, args.out, args.img_base or None)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
