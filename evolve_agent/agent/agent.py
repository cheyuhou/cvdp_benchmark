#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Evolve-Agent contributors
# SPDX-License-Identifier: Apache-2.0

"""
Evolutionary CVDP agent backed by Qwen3.

The CVDP agentic harness mounts:

    /code/prompt.json    task spec (JSON with a ``prompt`` field)
    /code/docs/          spec/docs (read-only)
    /code/rtl/           RTL sources to edit
    /code/verif/         verification harness (read-only)
    /code/rundir/        scratch space the agent may write to

Agent strategy:

    1. Read prompt and current RTL state.
    2. Plan with Qwen3.
    3. Generate candidate file edits as JSON ``{path: content}``.
    4. Run a local lint check (iverilog -t null if available) and feed any
       errors back to Qwen3 for repair.
    5. Repeat up to ``EVOLVE_MAX_PASSES`` times, then write the best candidate
       to /code/rtl.

All Qwen calls go through the OpenAI-compatible REST API; we use ``requests``
so the agent image does not need the openai SDK.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import requests  # base image already installs this

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] agent: %(message)s",
)
log = logging.getLogger("agent")


CODE_ROOT = Path("/code")
RTL_DIR = CODE_ROOT / "rtl"
DOCS_DIR = CODE_ROOT / "docs"
RUNDIR = CODE_ROOT / "rundir"
PROMPT_PATH = CODE_ROOT / "prompt.json"


# ---------------------------------------------------------------------------
# Qwen REST client
# ---------------------------------------------------------------------------


class QwenClient:
    def __init__(self) -> None:
        self.api_key = (
            os.environ.get("QWEN_API_KEY")
            or os.environ.get("DASHSCOPE_API_KEY")
            or os.environ.get("OPENAI_USER_KEY")
        )
        if not self.api_key:
            raise RuntimeError(
                "Agent: QWEN_API_KEY / DASHSCOPE_API_KEY not set in container env"
            )
        self.base_url = os.environ.get(
            "QWEN_BASE_URL",
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        ).rstrip("/")
        self.model = os.environ.get("QWEN_MODEL", "qwen3-max")
        self.timeout = int(os.environ.get("QWEN_TIMEOUT", "180"))
        self.temperature = float(os.environ.get("QWEN_TEMPERATURE", "0.2"))

    def chat(self, system: str, user: str, *, temperature: float | None = None) -> str:
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature if temperature is not None else self.temperature,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        for attempt in range(3):
            try:
                resp = requests.post(
                    url, headers=headers, json=payload, timeout=self.timeout,
                )
                if resp.status_code in (429, 502, 503, 504):
                    raise requests.HTTPError(f"retryable {resp.status_code}")
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"] or ""
            except Exception as exc:  # noqa: BLE001
                wait = 2 ** attempt
                log.warning("qwen call failed (%s), retry in %ss", exc, wait)
                time.sleep(wait)
        log.error("qwen call exhausted retries")
        return ""


# ---------------------------------------------------------------------------
# Workspace helpers
# ---------------------------------------------------------------------------


def _read_prompt() -> str:
    try:
        return json.loads(PROMPT_PATH.read_text(encoding="utf-8")).get("prompt", "")
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.error("could not read /code/prompt.json: %s", exc)
        return ""


def _gather_rtl(max_files: int = 20, max_chars: int = 60_000) -> dict[str, str]:
    out: dict[str, str] = {}
    if not RTL_DIR.exists():
        return out
    files = sorted(p for p in RTL_DIR.rglob("*") if p.is_file())
    used = 0
    for path in files[:max_files]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(RTL_DIR))
        out[rel] = text
        used += len(text)
        if used > max_chars:
            break
    return out


def _gather_docs(max_chars: int = 20_000) -> str:
    if not DOCS_DIR.exists():
        return ""
    chunks: list[str] = []
    used = 0
    for path in sorted(DOCS_DIR.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunks.append(f"--- {path.relative_to(DOCS_DIR)} ---\n{text}")
        used += len(text)
        if used > max_chars:
            break
    return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# Lint feedback (best-effort)
# ---------------------------------------------------------------------------


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _lint_candidate(files: dict[str, str]) -> str:
    """Return concatenated compiler errors for a candidate, or empty string."""
    if not files:
        return ""
    scratch = RUNDIR / "lint_scratch"
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for rel, content in files.items():
        path = scratch / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)

    if _has("iverilog"):
        cmd = ["iverilog", "-g2012", "-tnull", "-o", "/dev/null", *map(str, written)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            return f"iverilog errors:\n{proc.stderr.strip()}"
        return ""
    if _has("yosys"):
        script = "; ".join([f"read_verilog -sv {p}" for p in written])
        proc = subprocess.run(
            ["yosys", "-q", "-p", script],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            return f"yosys errors:\n{proc.stderr.strip()}"
        return ""
    log.info("no iverilog/yosys available, skipping local lint")
    return ""


# ---------------------------------------------------------------------------
# Qwen prompt blocks
# ---------------------------------------------------------------------------


PLAN_SYS = (
    "You are a senior RTL architect. Produce a tight numbered plan to solve "
    "the given Verilog/SystemVerilog problem. Cover I/O, FSM, edge cases, "
    "and known lint pitfalls. <=10 bullets."
)

GEN_SYS = (
    "You are an expert RTL engineer. Return your solution as a single JSON "
    "object mapping relative file paths under rtl/ to the FULL file content. "
    "Use this exact shape, no extra commentary:\n"
    '{"files": {"path/to/file.sv": "module ..."}}\n'
    "Only include files that need to be created or rewritten."
)

REPAIR_SYS = (
    "You are an expert RTL engineer fixing a candidate solution. Address "
    "every error from the lint output. Return the same JSON shape "
    '{"files": {...}} containing the corrected full files only.'
)


def _extract_files_json(text: str) -> dict[str, str]:
    """Pull the ``files`` mapping out of a Qwen response that may contain prose."""
    if not text:
        return {}
    # Strip markdown fences first.
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    candidate = fence.group(1) if fence else text
    # Find the largest balanced JSON object.
    start = candidate.find("{")
    if start == -1:
        return {}
    depth = 0
    for i in range(start, len(candidate)):
        ch = candidate[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = candidate[start : i + 1]
                try:
                    payload = json.loads(blob)
                except json.JSONDecodeError:
                    return {}
                files = payload.get("files") if isinstance(payload, dict) else None
                if isinstance(files, dict):
                    return {k: v for k, v in files.items() if isinstance(v, str)}
                return {}
    return {}


def _plan(client: QwenClient, prompt: str, docs: str, current_rtl: dict[str, str]) -> str:
    user = (
        f"# Spec\n{prompt}\n\n"
        f"# Docs (excerpts)\n{docs[:8000] or '(none)'}\n\n"
        f"# Current RTL files\n{', '.join(current_rtl.keys()) or '(empty)'}"
    )
    return client.chat(PLAN_SYS, user) or "(no plan)"


def _generate(
    client: QwenClient,
    prompt: str,
    plan: str,
    docs: str,
    current_rtl: dict[str, str],
) -> dict[str, str]:
    rtl_blob = "\n".join(
        f"--- {name} ---\n{content}" for name, content in current_rtl.items()
    )[:40_000]
    user = (
        f"# Spec\n{prompt}\n\n"
        f"# Plan\n{plan}\n\n"
        f"# Docs\n{docs[:8000]}\n\n"
        f"# Current RTL\n{rtl_blob or '(empty)'}\n\n"
        "Produce the JSON object now."
    )
    raw = client.chat(GEN_SYS, user)
    return _extract_files_json(raw)


def _repair(
    client: QwenClient,
    prompt: str,
    candidate: dict[str, str],
    lint_log: str,
) -> dict[str, str]:
    rtl_blob = "\n".join(
        f"--- {name} ---\n{content}" for name, content in candidate.items()
    )[:40_000]
    user = (
        f"# Spec\n{prompt}\n\n"
        f"# Lint output\n{lint_log[:6000]}\n\n"
        f"# Candidate\n{rtl_blob}\n\n"
        "Return the corrected JSON object now."
    )
    raw = client.chat(REPAIR_SYS, user, temperature=0.15)
    fixed = _extract_files_json(raw)
    return fixed or candidate


# ---------------------------------------------------------------------------
# Apply / commit
# ---------------------------------------------------------------------------


def _apply_files(files: dict[str, str]) -> int:
    written = 0
    for rel, content in files.items():
        # Block path traversal and absolute paths.
        if rel.startswith("/") or ".." in Path(rel).parts:
            log.warning("rejecting unsafe path %s", rel)
            continue
        target = RTL_DIR / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written += 1
    return written


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main() -> int:
    log.info("evolve-agent starting")
    if not PROMPT_PATH.exists():
        log.error("no prompt.json at %s", PROMPT_PATH)
        return 1

    RUNDIR.mkdir(parents=True, exist_ok=True)
    prompt = _read_prompt()
    if not prompt:
        log.error("empty prompt")
        return 1

    try:
        client = QwenClient()
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    docs = _gather_docs()
    current_rtl = _gather_rtl()

    plan = _plan(client, prompt, docs, current_rtl)
    log.info("plan generated (%d chars)", len(plan))

    candidate = _generate(client, prompt, plan, docs, current_rtl)
    if not candidate:
        log.error("generator returned no parsable files; aborting")
        return 1
    log.info("initial candidate: %d files", len(candidate))

    max_passes = int(os.environ.get("EVOLVE_MAX_PASSES", "3"))
    for pass_idx in range(1, max_passes + 1):
        lint = _lint_candidate(candidate)
        if not lint:
            log.info("pass %d: lint clean", pass_idx)
            break
        log.info("pass %d: lint reported issues, repairing", pass_idx)
        candidate = _repair(client, prompt, candidate, lint)

    written = _apply_files(candidate)
    log.info("wrote %d files into /code/rtl", written)

    summary = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": client.model,
        "files_written": written,
        "passes_used": pass_idx if candidate else 0,
    }
    (RUNDIR / "evolve_agent_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
