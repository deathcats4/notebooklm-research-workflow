#!/usr/bin/env python
"""Probe and download NotebookLM-generated interactive HTML webpages.

NotebookLM Studio's generated webpages are returned by ``LIST_ARTIFACTS`` as
type-10 (``ArtifactTypeCode.FILE``) rows, which ``notebooklm-py <= 0.8.6`` can
list (``artifact list``) but cannot yet download via ``notebooklm download``.
This script probes raw artifact rows, extracts the direct download URL at
``row[24][3]`` (``contribution.usercontent.google.com/download?...``) plus the
generation prompt at ``row[24][5][1]``, downloads each HTML file using the
saved NotebookLM session cookies, and writes a ``manifest.json`` index.

Usage:
    python download_webpages.py -n <NOTEBOOK_ID> --list [--json]
    python download_webpages.py -n <NOTEBOOK_ID> -o <OUTPUT_DIR> [--only <ID_OR_TITLE>]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_REEXEC_ENV = "_NLM_WEBPAGE_REEXEC"
_FILE_TYPE_CODE = 10
_COMPLETED_STATUS = 3
_TRUSTED_COOKIE_SUFFIXES = (".google.com", ".googleusercontent.com", ".googleapis.com")
_UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\r\n]+')
_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _find_notebooklm_python() -> str | None:
    """Locate the Python interpreter that hosts ``notebooklm-py``."""
    env_py = os.environ.get("NOTEBOOKLM_PYTHON")
    if env_py and Path(env_py).is_file():
        return env_py

    cli = shutil.which("notebooklm")
    if not cli:
        return None

    try:
        raw = Path(cli).read_bytes()
    except OSError:
        return None

    # POSIX script shebang: #!/path/to/python
    if raw.startswith(b"#!"):
        first_line = raw.splitlines()[0][2:].decode("utf-8", errors="ignore").strip()
        candidate = first_line.split()[0] if first_line else ""
        if candidate and Path(candidate).is_file():
            return candidate

    # Windows uv/pip .exe wrapper embeds the target python.exe path.
    for match in re.findall(rb"[A-Za-z]:\\[^\x00\r\n\"']+python\.exe", raw):
        candidate = match.decode("utf-8", errors="ignore")
        if Path(candidate).is_file():
            return candidate
    return None


def _ensure_dependencies() -> None:
    """Import ``notebooklm`` and ``httpx``, re-execing into tool Python if needed."""
    try:
        import httpx  # noqa: F401
        import notebooklm  # noqa: F401
        return
    except ModuleNotFoundError as exc:
        if not os.environ.get(_REEXEC_ENV):
            tool_py = _find_notebooklm_python()
            if tool_py and Path(tool_py).resolve() != Path(sys.executable).resolve():
                env = os.environ.copy()
                env[_REEXEC_ENV] = "1"
                proc = subprocess.run([tool_py, *sys.argv], env=env)
                sys.exit(proc.returncode)
        print(
            f"[ERROR] Missing Python dependency ({exc.name}). "
            "Run with your notebooklm-py Python interpreter or set NOTEBOOKLM_PYTHON.",
            file=sys.stderr,
        )
        sys.exit(2)


_ensure_dependencies()

import httpx  # noqa: E402
from notebooklm import NotebookLMClient  # noqa: E402
from notebooklm._auth.cookies import load_httpx_cookies  # noqa: E402
from notebooklm._types.common import _datetime_from_timestamp  # noqa: E402
from notebooklm.paths import get_storage_path  # noqa: E402


def _is_trusted_google_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return False
    return any(host == s.lstrip(".") or host.endswith(s) for s in _TRUSTED_COOKIE_SUFFIXES)


def _safe_stem(text: str, max_len: int = 60) -> str:
    cleaned = _UNSAFE_NAME_RE.sub("_", text).strip().strip(".")
    if cleaned.lower().endswith(".html"):
        cleaned = cleaned[:-5]
    return cleaned[:max_len].strip() or "webpage"


def _extract_html_title(html_text: str) -> str | None:
    match = _HTML_TITLE_RE.search(html_text)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip() or None


def _format_ts(block: Any) -> str | None:
    if not isinstance(block, list) or not block:
        return None
    sec = block[0]
    if not isinstance(sec, (int, float)) or isinstance(sec, bool):
        return None
    dt = _datetime_from_timestamp(sec)
    return dt.isoformat() if dt else None


def _parse_webpage_row(row: list[Any]) -> dict[str, Any] | None:
    """Extract metadata and download URL from one raw type-10 artifact row."""
    if not isinstance(row, list) or len(row) <= 2 or row[2] != _FILE_TYPE_CODE:
        return None

    block24 = row[24] if len(row) > 24 and isinstance(row[24], list) else []
    mime_type = block24[1] if len(block24) > 1 and isinstance(block24[1], str) else None
    download_url = block24[3] if len(block24) > 3 and isinstance(block24[3], str) else None
    viewer_url = block24[2] if len(block24) > 2 and isinstance(block24[2], str) else None
    if not viewer_url and len(row) > 23 and isinstance(row[23], str):
        viewer_url = row[23]

    prompt_block = block24[5] if len(block24) > 5 and isinstance(block24[5], list) else []
    prompt = prompt_block[1] if len(prompt_block) > 1 and isinstance(prompt_block[1], str) else None
    language = prompt_block[2] if len(prompt_block) > 2 and isinstance(prompt_block[2], str) else None

    return {
        "id": str(row[0]),
        "title": str(row[1]) if len(row) > 1 else "",
        "status_id": row[4] if len(row) > 4 else None,
        "created_at": _format_ts(row[15] if len(row) > 15 else None),
        "last_modified_at": _format_ts(row[10] if len(row) > 10 else None),
        "filename": block24[0] if len(block24) > 0 and isinstance(block24[0], str) else None,
        "mime_type": mime_type,
        "download_url": download_url,
        "viewer_url": viewer_url,
        "language": language,
        "prompt": prompt,
    }


async def _resolve_notebook(client: NotebookLMClient, notebook_id: str) -> tuple[str, str]:
    """Resolve an exact or prefix notebook ID to ``(id, title)``."""
    notebooks = await client.notebooks.list()
    exact = [nb for nb in notebooks if nb.id == notebook_id]
    if exact:
        return exact[0].id, exact[0].title
    prefix = [nb for nb in notebooks if nb.id.startswith(notebook_id)]
    if len(prefix) == 1:
        return prefix[0].id, prefix[0].title
    if len(prefix) > 1:
        raise ValueError(f"Ambiguous notebook prefix '{notebook_id}' ({len(prefix)} matches)")
    raise ValueError(f"Notebook '{notebook_id}' not found")


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    storage_path = Path(args.storage) if args.storage else get_storage_path(profile=args.profile)
    if not storage_path.exists():
        raise FileNotFoundError(
            f"Auth storage not found at {storage_path}. Run 'notebooklm login' first."
        )

    async with NotebookLMClient.from_storage(path=str(storage_path)) as client:
        nb_id, nb_title = await _resolve_notebook(client, args.notebook)
        raw_rows = await client.artifacts._list_raw(nb_id)

    items = [parsed for r in raw_rows if (parsed := _parse_webpage_row(r)) is not None]
    if args.only:
        needle = args.only.lower()
        items = [
            it
            for it in items
            if needle in it["id"].lower()
            or needle in it["title"].lower()
            or (it["prompt"] and needle in it["prompt"].lower())
        ]

    if args.list:
        for it in items:
            it.pop("download_url", None)
            it.pop("viewer_url", None)
        return {
            "notebook_id": nb_id,
            "notebook_title": nb_title,
            "count": len(items),
            "webpages": items,
        }

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cookies = load_httpx_cookies(path=storage_path)

    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(cookies=cookies, follow_redirects=True, timeout=60.0) as hc:
        for idx, item in enumerate(items, start=1):
            record: dict[str, Any] = {
                "index": idx,
                "id": item["id"],
                "title": item["title"],
                "created_at": item["created_at"],
                "last_modified_at": item["last_modified_at"],
                "mime_type": item["mime_type"],
                "language": item["language"],
                "prompt": item["prompt"],
            }
            dl_url = item.get("download_url")
            if item["status_id"] != _COMPLETED_STATUS:
                record["status"] = f"skipped_not_ready (status_id={item['status_id']})"
                results.append(record)
                continue
            if not dl_url or not _is_trusted_google_url(dl_url):
                record["status"] = "skipped_missing_or_untrusted_url"
                results.append(record)
                continue

            resp = await hc.get(dl_url)
            final_host = (resp.url.host or "").lower()
            if "accounts.google.com" in final_host:
                raise RuntimeError(
                    "Authentication expired during download (redirected to accounts.google.com). "
                    "Run 'notebooklm login' and retry."
                )
            resp.raise_for_status()

            html_text = resp.text
            if "<html" not in html_text.lower() and "<!doctype" not in html_text.lower():
                record["status"] = f"unexpected_body (content-type={resp.headers.get('content-type')})"
                results.append(record)
                continue

            html_title = _extract_html_title(html_text)
            stem = _safe_stem(html_title or item["title"] or item["filename"] or item["id"])
            filename = f"{idx:02d}_{item['id'][:8]}_{stem}.html"
            dest = out_dir / filename
            dest.write_bytes(resp.content)

            record.update(
                {
                    "status": "downloaded",
                    "html_title": html_title,
                    "file": filename,
                    "path": str(dest.resolve()),
                    "bytes": len(resp.content),
                }
            )
            results.append(record)

    manifest = {
        "notebook_id": nb_id,
        "notebook_title": nb_title,
        "count": len(results),
        "downloaded": sum(1 for r in results if r.get("status") == "downloaded"),
        "webpages": results,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path.resolve())
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe and download NotebookLM-generated HTML webpages (type-10 File artifacts)."
    )
    parser.add_argument("-n", "--notebook", required=True, help="Notebook ID or unique ID prefix.")
    parser.add_argument(
        "-o",
        "--output-dir",
        default="./notebooklm_webpages",
        help="Destination directory for downloaded HTML files and manifest.json.",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="Filter by artifact ID, title, or prompt substring (case-insensitive).",
    )
    parser.add_argument("--list", action="store_true", help="Probe and list webpages without downloading.")
    parser.add_argument("--json", action="store_true", help="Print output as JSON.")
    parser.add_argument("-p", "--profile", default=None, help="NotebookLM CLI profile name.")
    parser.add_argument("--storage", default=None, help="Explicit path to storage_state.json.")
    args = parser.parse_args()

    try:
        payload = asyncio.run(_run(args))
    except Exception as exc:
        if args.json:
            print(json.dumps({"error": True, "message": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Notebook: {payload['notebook_title']} ({payload['notebook_id']})")
        if args.list:
            print(f"Found {payload['count']} webpage artifact(s):")
            for w in payload["webpages"]:
                prompt_preview = (w.get("prompt") or "").replace("\n", " ")[:80]
                print(
                    f"  - {w['id']} | {w['created_at']} | {w['mime_type']} | "
                    f"prompt={prompt_preview!r}"
                )
        else:
            print(
                f"Downloaded {payload['downloaded']}/{payload['count']} webpage(s) "
                f"to {args.output_dir}"
            )
            for w in payload["webpages"]:
                if w.get("status") == "downloaded":
                    print(f"  - {w['file']} ({w['bytes']} bytes) — {w.get('html_title')}")
                else:
                    print(f"  - {w['id']} — {w.get('status')}")
            print(f"Manifest: {payload['manifest_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
