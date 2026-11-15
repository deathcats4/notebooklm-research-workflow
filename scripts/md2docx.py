#!/usr/bin/env python
"""Convert OCR or extracted Markdown into a clean image-embedded DOCX.

Supports headings, HTML `<table>...</table>` and Markdown pipe tables,
both `![](...)` and `![alt](...)` image links (including `pymupdf4llm`
outputs with `<!-- Start of picture text -->` blocks stripped).

Usage:
    python md2docx.py <input.md> <output.docx> [img_base_dir]
"""

from __future__ import annotations

import html as htmlmod
import os
import re
import sys

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt

TAG_RE = re.compile(r"<[^>]+>")
DISPLAY_MATH_RE = re.compile(r"\$\$(.*?)\$\$", re.S)
MATH_RE = re.compile(r"\$([^$\n]*?)\$")
PIC_TEXT_RE = re.compile(r"<!--\s*Start of picture text\s*-->.*?<!--\s*End of picture text\s*-->", re.S)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
HTML_TABLE_RE = re.compile(r"(<table\b[^>]*>.*?</table>)", re.S | re.I)
IMG_RE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)$")
IMG_TITLE_RE = re.compile(r"""\s+(?:"[^"]*"|'[^']*')\s*$""")
HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)")
PIPE_SEP_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")

_LATEX_WRAPPER_CMD_RE = re.compile(
    r"\\(?:mathrm|textrm|text|mathbf|mathsf|mathtt|mathit|pmb|boldsymbol|operatorname|mathcal|mathbb"
    r"|scriptstyle|scriptscriptstyle|displaystyle|textstyle|limits)\b\s*"
)
_TAG_CMD_RE = re.compile(r"\\tag\s*\{([^{}]*)\}")
_SCRIPT_GROUP_RE = re.compile(r"\s*([_^])\s*\{([^{}]*)\}")
_LATEX_SYMBOLS = {
    "\\degreeCelsius": "℃",
    "\\textcelsius": "℃",
    "\\textperthousand": "‰",
    "\\leftrightarrow": "↔",
    "\\downarrow": "↓",
    "\\uparrow": "↑",
    "\\varepsilon": "ε",
    "\\rightarrow": "→",
    "\\leftarrow": "←",
    "\\setminus": "\\",
    "\\backslash": "/",
    "\\approx": "≈",
    "\\permil": "‰",
    "\\partial": "∂",
    "\\propto": "∝",
    "\\degree": "°",
    "\\bullet": "•",
    "\\infty": "∞",
    "\\alpha": "α",
    "\\gamma": "γ",
    "\\delta": "δ",
    "\\Delta": "Δ",
    "\\theta": "θ",
    "\\lambda": "λ",
    "\\sigma": "σ",
    "\\Sigma": "Σ",
    "\\omega": "ω",
    "\\Omega": "Ω",
    "\\times": "×",
    "\\simeq": "≃",
    "\\equiv": "≡",
    "\\cdotp": "·",
    "\\beta": "β",
    "\\circ": "°",
    "\\cdot": "·",
    "\\sqrt": "√",
    "\\perp": "⊥",
    "\\ast": "*",
    "\\quad": " ",
    "\\qquad": " ",
    "\\left": "",
    "\\right": "",
    "\\leq": "≤",
    "\\geq": "≥",
    "\\neq": "≠",
    "\\sim": "~",
    "\\div": "÷",
    "\\chi": "χ",
    "\\psi": "ψ",
    "\\phi": "φ",
    "\\rho": "ρ",
    "\\tau": "τ",
    "\\mu": "μ",
    "\\nu": "ν",
    "\\pi": "π",
    "\\pm": "±",
    "\\mp": "∓",
    "\\le": "≤",
    "\\ge": "≥",
    "\\ne": "≠",
    "\\lt": "<",
    "\\gt": ">",
    "\\ll": "≪",
    "\\gg": "≫",
    "\\to": "→",
}
_LATEX_SYM_RE = re.compile(
    "(?:" + "|".join(re.escape(k) for k in sorted(_LATEX_SYMBOLS, key=len, reverse=True)) + r")(?![A-Za-z])"
)
_STRAY_LATEX_RE = re.compile(
    r"\\(?:mathrm|textrm|text|mathbf|mathsf|mathtt|mathit|pmb|boldsymbol|operatorname|mathcal|mathbb"
    r"|scriptstyle|displaystyle|textstyle|frac|tag|sqrt|downarrow|uparrow|alpha|beta|gamma|delta|mu|times|sim|approx|pm|circ)\b"
)


def _read_brace_arg(s: str, pos: int) -> tuple[str, int] | None:
    """Read one `{...}` balanced group or single token starting at/after whitespace from pos."""
    n = len(s)
    while pos < n and s[pos].isspace():
        pos += 1
    if pos >= n:
        return None
    if s[pos] != "{":
        return s[pos], pos + 1
    depth = 0
    start = pos + 1
    i = pos
    while i < n:
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return s[start:i], i + 1
        i += 1
    return None


def _replace_fracs(expr: str) -> str:
    """Convert nested `\\frac{num}{den}` expressions into readable `num/den` text."""
    out: list[str] = []
    i = 0
    n = len(expr)
    while i < n:
        if expr.startswith("\\frac", i) and (i + 5 == n or not expr[i + 5].isalpha()):
            arg1 = _read_brace_arg(expr, i + 5)
            if arg1:
                num, p1 = arg1
                arg2 = _read_brace_arg(expr, p1)
                if arg2:
                    den, p2 = arg2
                    num_c = _clean_math(num)
                    den_c = _clean_math(den)
                    if any(op in num_c for op in "+- ") and not (num_c.startswith("(") and num_c.endswith(")")):
                        num_c = f"({num_c})"
                    if any(op in den_c for op in "+- ") and not (den_c.startswith("(") and den_c.endswith(")")):
                        den_c = f"({den_c})"
                    out.append(f"{num_c}/{den_c}")
                    i = p2
                    continue
        out.append(expr[i])
        i += 1
    return "".join(out)


def _clean_math(expr: str) -> str:
    """Convert inline/display LaTeX math into clean Unicode plain text."""
    expr = expr.replace(r"\{", "").replace(r"\}", "")
    expr = _LATEX_WRAPPER_CMD_RE.sub("", expr)
    expr = _TAG_CMD_RE.sub(r"(\1)", expr)
    if "\\frac" in expr:
        expr = _replace_fracs(expr)
    expr = _LATEX_SYM_RE.sub(lambda m: _LATEX_SYMBOLS[m.group(0)], expr)
    expr = expr.replace(r"\%", "%").replace(r"\,", " ").replace(r"\;", " ").replace(r"\:", " ").replace(r"\!", "")
    for _ in range(4):
        new_expr = _SCRIPT_GROUP_RE.sub(lambda m: m.group(1) + re.sub(r"\s+", "", m.group(2)), expr)
        if new_expr == expr:
            break
        expr = new_expr
    expr = expr.replace("{", "").replace("}", "")
    expr = re.sub(r"\s*\^\s*", "^", expr)
    expr = re.sub(r"\s*_\s*", "_", expr)
    expr = re.sub(r"\s+", " ", expr)
    return expr.strip()


def clean(text: str) -> str:
    """Strip inline HTML/LaTeX/Markdown wrappers into clean plain text."""
    text = DISPLAY_MATH_RE.sub(lambda m: _clean_math(m.group(1)), text)
    text = MATH_RE.sub(lambda m: _clean_math(m.group(1)), text)
    if _STRAY_LATEX_RE.search(text):
        # Clean stray/unclosed LaTeX fragments left by OCR (e.g., unmatched `$` or unbracketed commands)
        text = _clean_math(text).replace("$", "")
    else:
        text = _LATEX_SYM_RE.sub(lambda m: _LATEX_SYMBOLS[m.group(0)], text)
    text = TAG_RE.sub("", text)
    text = htmlmod.unescape(text)
    text = text.replace("\\~", "~").replace("\\_", "_").replace("**", "")
    return text.strip()


def _render_table(doc: Document, cells: list[list[str]]) -> bool:
    """Render a 2D cell grid into a styled Word table."""
    ncol = max((len(r) for r in cells), default=0)
    if ncol == 0:
        return False
    tab = doc.add_table(rows=0, cols=ncol)
    tab.style = "Table Grid"
    for row_vals in cells:
        row = tab.add_row().cells
        for idx, val in enumerate(row_vals[:ncol]):
            row[idx].text = val
            for p in row[idx].paragraphs:
                for run in p.runs:
                    run.font.size = Pt(8)
    return True


def add_html_table(doc: Document, block: str) -> bool:
    """Parse an HTML `<table>` block and render it as a Word table."""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", block, re.S | re.I)
    if not rows:
        return False
    cells = [
        [clean(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S | re.I)]
        for r in rows
    ]
    return _render_table(doc, cells)


def _split_pipe_row(line: str) -> list[str]:
    """Split a Markdown pipe-table row into cleaned cell values."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [clean(c) for c in stripped.split("|")]


def _add_image(doc: Document, img_path: str, rel_label: str) -> None:
    """Embed an image centered at 15.5 cm width, or write a visible placeholder."""
    if not os.path.exists(img_path):
        doc.add_paragraph(f"[缺图: {rel_label}]")
        return
    par = doc.add_paragraph()
    par.alignment = WD_ALIGN_PARAGRAPH.CENTER
    try:
        par.add_run().add_picture(img_path, width=Cm(15.5))
    except Exception as exc:
        par.add_run(f"[图片插入失败: {exc}]")


def _process_markdown_lines(doc: Document, chunk: str, img_base: str) -> None:
    """Process non-HTML-table Markdown lines, including pipe tables and images."""
    lines = chunk.split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i].rstrip()
        stripped = raw.strip()
        if not stripped:
            i += 1
            continue

        # Detect Markdown pipe table: header row + separator row (| --- | --- |)
        if (
            "|" in stripped
            and i + 1 < len(lines)
            and PIPE_SEP_RE.match(lines[i + 1].strip())
        ):
            table_rows = [_split_pipe_row(stripped)]
            i += 2
            while i < len(lines) and "|" in lines[i].strip() and lines[i].strip():
                table_rows.append(_split_pipe_row(lines[i]))
                i += 1
            _render_table(doc, table_rows)
            continue

        m = HEADING_RE.match(raw)
        if m:
            doc.add_heading(clean(m.group(2)), level=min(len(m.group(1)), 4))
            i += 1
            continue

        m = IMG_RE.match(stripped)
        if m:
            raw_target = IMG_TITLE_RE.sub("", m.group(2).strip())
            rel = raw_target.strip().strip("<>")
            img_path = rel if os.path.isabs(rel) else os.path.join(img_base, rel)
            _add_image(doc, img_path, rel)
            i += 1
            continue

        cleaned = clean(raw)
        if cleaned:
            doc.add_paragraph(cleaned)
        i += 1


def convert(md_path: str, out_path: str, img_base: str | None = None) -> str:
    """Convert `md_path` to DOCX at `out_path`, resolving relative images from `img_base`."""
    if img_base is None:
        img_base = os.path.dirname(os.path.abspath(md_path))
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "Microsoft YaHei"
    st.font.size = Pt(10.5)

    with open(md_path, encoding="utf-8-sig") as f:
        body = f.read()

    body = PIC_TEXT_RE.sub("", body)
    body = HTML_COMMENT_RE.sub("", body)

    for part in HTML_TABLE_RE.split(body):
        if not part:
            continue
        if part.lower().startswith("<table"):
            add_html_table(doc, part)
        else:
            _process_markdown_lines(doc, part, img_base)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    doc.save(out_path)
    return out_path


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: python md2docx.py <input.md> <output.docx> [img_base_dir]")
        return 2
    md_path = sys.argv[1]
    out_path = sys.argv[2]
    img_base = sys.argv[3] if len(sys.argv) > 3 else None
    result = convert(md_path, out_path, img_base)
    print(f"Saved DOCX: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
