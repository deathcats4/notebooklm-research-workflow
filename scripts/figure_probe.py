#!/usr/bin/env python
"""Verify NotebookLM visual figure reading with an automated plant-token guard.

Checks via `source fulltext` that expected visual tokens (`--expect`) appear 0
times in the indexed text layer (otherwise the query does not prove visual figure
reading), then runs `notebooklm ask -s ... --new --json` with 1 automatic retry
on transient errors such as DOCX `UNEXPECTED_ERROR`.

Usage:
    python figure_probe.py -n <NB> -s <SRC_ID> -q "<QUESTION>" --expect <TOK1> --expect <TOK2>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys


def _run_cli(args: list[str]) -> dict:
    proc = subprocess.run(
        ["notebooklm"] + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    m = re.search(r"\{.*\}", proc.stdout, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            if proc.returncode != 0 and "_error" not in data:
                data["_rc"] = proc.returncode
            return data
        except json.JSONDecodeError:
            pass
    if proc.returncode != 0:
        return {"_error": proc.stderr.strip() or proc.stdout.strip(), "_rc": proc.returncode}
    return {"_error": "No JSON object in stdout", "_raw": proc.stdout[:300]}


def check_text_leaks(notebook_id: str, sources: list[str], expect_tokens: list[str]) -> list[dict]:
    """Verify none of the expected visual tokens exist in any target source's text layer."""
    if not expect_tokens:
        return []
    leaks: list[dict] = []
    for sid in sources:
        ft = _run_cli(["source", "fulltext", sid, "-n", notebook_id, "--json"])
        if "_error" in ft and "content" not in ft:
            raise RuntimeError(f"Failed to fetch fulltext for source {sid}: {ft['_error']}")
        content = ft.get("content") or ft.get("text") or ""
        content_lower = content.lower()
        for tok in expect_tokens:
            cnt = content_lower.count(tok.lower())
            if cnt > 0:
                leaks.append({"source_id": sid, "token": tok, "count": cnt})
    return leaks


def evaluate_expected_tokens(answer: str, expect_tokens: list[str]) -> tuple[list[str], list[str]]:
    """Return (matched_tokens, missing_tokens) checking case-insensitive hits in answer."""
    ans_lower = answer.lower()
    matched: list[str] = []
    missing: list[str] = []
    for tok in expect_tokens:
        if tok.lower() in ans_lower:
            matched.append(tok)
        else:
            missing.append(tok)
    return matched, missing


def run_probe(
    notebook_id: str,
    sources: list[str],
    question: str,
    expect_tokens: list[str],
    strict: bool = False,
    new_conv: bool = False,
) -> tuple[dict, int]:
    """Execute plant-token fulltext check + `ask -s ... [--new] --json` with 1 retry."""
    leaks = check_text_leaks(notebook_id, sources, expect_tokens)
    if leaks and strict:
        payload = {
            "passed": False,
            "reason": "plant_token_text_leak",
            "text_leaks": leaks,
            "sources": sources,
            "question": question,
            "expect": expect_tokens,
        }
        return payload, 2

    cmd = ["ask", question, "-n", notebook_id, "--json"]
    if new_conv:
        cmd.append("--new")
    for sid in sources:
        cmd.extend(["-s", sid])

    attempts = 0
    ask_res: dict = {}
    for attempt in (1, 2):
        attempts = attempt
        ask_res = _run_cli(cmd)
        is_transient = (
            bool(ask_res.get("error"))
            or ask_res.get("code") == "UNEXPECTED_ERROR"
            or "_error" in ask_res
            or not (ask_res.get("answer") or "").strip()
        )
        if not is_transient:
            break

    answer = (ask_res.get("answer") or "").strip()
    ask_failed = bool(ask_res.get("error")) or "_error" in ask_res or not answer
    matched, missing = evaluate_expected_tokens(answer, expect_tokens) if answer else ([], list(expect_tokens))
    passed = (not ask_failed) and (not missing) and (not (strict and leaks))

    payload = {
        "passed": passed,
        "attempts": attempts,
        "sources": sources,
        "question": question,
        "expect": expect_tokens,
        "text_leaks": leaks,
        "matched_tokens": matched,
        "missing_tokens": missing,
        "references_count": len(ask_res.get("references") or []),
        "ask_response": ask_res,
    }
    return payload, (0 if passed else 1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify NotebookLM figure reading via ask -s + plant-token check.")
    parser.add_argument("-n", "--notebook", required=True, help="Notebook ID or unique prefix")
    parser.add_argument("-s", "--source", action="append", required=True, help="Target source ID (repeatable)")
    parser.add_argument("-q", "--question", required=True, help="Visual figure probe question")
    parser.add_argument("--expect", action="append", default=[], help="Expected figure-only token (repeatable)")
    parser.add_argument("--save", default="", help="Optional path to save full JSON probe result")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 2 immediately before calling ask if any --expect token already exists in source text layer",
    )
    parser.add_argument(
        "--new",
        action="store_true",
        help="Start a fresh server-side conversation before probing (deletes active chat history)",
    )
    args = parser.parse_args()

    try:
        payload, code = run_probe(
            args.notebook,
            args.source,
            args.question,
            args.expect,
            strict=args.strict,
            new_conv=args.new,
        )
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    print(f"Probe passed: {payload['passed']} | Attempts: {payload.get('attempts', 0)}")
    if payload["text_leaks"]:
        print(f"[WARN] Text-layer leak detected (token already in fulltext): {payload['text_leaks']}")
    else:
        print(f"Plant-token guard: 0 text-layer leaks across {len(args.source)} source(s)")

    if args.expect:
        print(f"Matched tokens: {payload.get('matched_tokens', [])} | Missing: {payload.get('missing_tokens', [])}")

    ask_res = payload.get("ask_response") or {}
    if ask_res:
        print(f"References cited: {payload.get('references_count', 0)}")
        ans = (ask_res.get("answer") or "").strip()
        if ans:
            print("\n--- Answer ---\n" + ans)
        elif ask_res.get("_error") or ask_res.get("code"):
            print(f"[ERROR] ask failed: {ask_res.get('code') or ask_res.get('_error')}")

    if args.save:
        save_dir = os.path.dirname(os.path.abspath(args.save))
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\nSaved probe output: {args.save}")

    return code


if __name__ == "__main__":
    raise SystemExit(main())