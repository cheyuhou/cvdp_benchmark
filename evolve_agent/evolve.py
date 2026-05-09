#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Evolve-Agent contributors
# SPDX-License-Identifier: Apache-2.0

"""
Evolutionary outer-loop orchestrator for the CVDP benchmark.

Pipeline (one iteration):

    1. Run benchmark with current responses (or fresh model call on iter 0).
    2. Parse work-dir raw_result.json + per-issue harness logs.
    3. Build a "feedback packet" per failed problem from the simulator log.
    4. Ask Qwen to repair the response using that feedback.
    5. Write a refined responses.jsonl, increment iteration.

Problems that already pass are passed through unchanged; we only rewrite the
completion for failing ids, so total Qwen spend grows only with failures.

Two backing model paths are supported:

    --mode local     (default) uses the stock local_export / local_import
                     pathway, so iteration N only re-evaluates failing items.
    --mode direct    bypasses run_benchmark.py and calls Qwen directly.
                     Faster to iterate during development.

Invocation:

    python evolve_agent/evolve.py \
        -f example_dataset/cvdp_v1.1.0_example_nonagentic_code_generation_no_commercial.jsonl \
        --iterations 4 \
        --workdir work_evolve

The harness commands assume you run this from the repo root.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Lazy import so that running --help works without the full deps in path.

logger = logging.getLogger("evolve")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class IterationStats:
    iteration: int
    total: int
    passed: int
    failed_ids: list[str] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return (self.passed / self.total) if self.total else 0.0


@dataclass
class FailureFeedback:
    issue_id: str
    prompt: str
    last_completion: str
    error_excerpt: str


# ---------------------------------------------------------------------------
# Helpers around the CVDP work directory
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            out.append(json.loads(raw))
    return out


def _write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _load_dataset_index(dataset_file: Path) -> dict[str, dict]:
    """Map issue id -> raw datapoint, used to re-extract original prompts."""
    index: dict[str, dict] = {}
    for row in _read_jsonl(dataset_file):
        # CVDP datapoints are keyed by id at top level.
        ident = row.get("id") or row.get("issue_id")
        if ident is None and len(row) == 1:
            # Some samples wrap the issue under its own id key.
            ident = next(iter(row))
        if ident:
            index[ident] = row
    return index


def _scan_results(workdir: Path) -> dict[str, dict]:
    """Read raw_result.json into ``{issue_id: result_record}``."""
    raw = workdir / "raw_result.json"
    if not raw.exists():
        return {}
    try:
        with raw.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except json.JSONDecodeError as exc:
        logger.warning("raw_result.json is malformed: %s", exc)
        return {}
    # raw_result.json is normally ``{ id: {...harness summary...} }``.
    if isinstance(payload, dict):
        return payload
    # Fallback: list-of-records shape.
    if isinstance(payload, list):
        return {row.get("id"): row for row in payload if isinstance(row, dict)}
    return {}


def _did_pass(record: dict) -> bool:
    """A record passes when every harness test returned result == 0.

    The CVDP harness writes ``{ "tests": [{result, log, ...}], "errors": N }``
    per issue. We trust ``errors`` when present and fall back to scanning the
    test list (so we stay forward-compatible with schema renames).
    """
    if isinstance(record.get("errors"), int):
        return record["errors"] == 0
    tests = (
        record.get("tests")
        or record.get("test_results")
        or record.get("results")
        or []
    )
    if not tests and "result" in record:
        return record["result"] == 0
    if not tests:
        return False
    for entry in tests:
        if isinstance(entry, dict) and entry.get("result", 0) != 0:
            return False
    return True


def _gather_log_excerpt(workdir: Path, issue_id: str, max_chars: int = 4000) -> str:
    """Best-effort harness log scrape for a failing issue."""
    candidates: list[Path] = []
    issue_dir = workdir / issue_id
    if issue_dir.exists():
        for pat in ("*.log", "*.txt", "harness_*"):
            candidates.extend(issue_dir.rglob(pat))
    # Some flows write logs flat under the workdir.
    for pat in (f"{issue_id}*.log", f"{issue_id}*.txt"):
        candidates.extend(workdir.glob(pat))

    excerpts: list[str] = []
    for path in sorted(set(candidates))[:6]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Take the tail; that is where simulators print the actual failure.
        tail = text[-max_chars // 2 :]
        excerpts.append(f"--- {path.name} ---\n{tail}")
        if sum(len(x) for x in excerpts) > max_chars:
            break
    if not excerpts:
        return "(no harness log captured)"
    joined = "\n".join(excerpts)
    return joined[-max_chars:]


# ---------------------------------------------------------------------------
# Qwen client
# ---------------------------------------------------------------------------


def _build_qwen_client():
    """Construct a Qwen client without going through the factory plumbing."""
    sys.path.insert(0, str(REPO_ROOT / "evolve_agent"))
    from qwen_factory import Qwen_Instance  # type: ignore  # noqa: WPS433

    return Qwen_Instance(
        context=(
            "You are an expert Verilog / SystemVerilog engineer. "
            "Return only the requested file content, no commentary."
        )
    )


REPAIR_SYSTEM = (
    "You are an expert RTL engineer fixing a buggy candidate solution. "
    "You will receive: the spec, the current candidate, and a harness log "
    "showing how the candidate fails. Produce the corrected full file ONLY, "
    "wrapped in one ```systemverilog code block, with no commentary."
)


def _refine_completion(client, fb: FailureFeedback) -> str:
    user = (
        f"# Spec\n{fb.prompt}\n\n"
        f"# Current candidate\n```systemverilog\n{fb.last_completion}\n```\n\n"
        f"# Harness failure log (truncated)\n{fb.error_excerpt}\n\n"
        "Fix every issue surfaced by the log. Preserve the module name and "
        "port list from the spec. Return only the corrected file."
    )
    try:
        resp = client.chat.chat.completions.create(
            model=client.model,
            messages=[
                {"role": "system", "content": REPAIR_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.15,
            timeout=120,
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.error("Qwen refine call failed for %s: %s", fb.issue_id, exc)
        return fb.last_completion
    return _extract_code(text) or text


def _extract_code(text: str) -> str:
    import re

    fences = re.findall(
        r"```(?:systemverilog|verilog|sv|v)?\s*\n(.*?)```",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return fences[-1].strip() if fences else ""


# ---------------------------------------------------------------------------
# Benchmark runner shims
# ---------------------------------------------------------------------------


def _run_export(dataset: Path, prompts_path: Path, iter_workdir: Path) -> None:
    cmd = [
        sys.executable,
        str(REPO_ROOT / "run_benchmark.py"),
        "-f",
        str(dataset),
        "--llm",
        "--model",
        "local_export",
        "--prompts-responses-file",
        str(prompts_path),
        "--prefix",
        str(iter_workdir),
    ]
    logger.info("export prompts -> %s", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(REPO_ROOT))


def _run_import(dataset: Path, responses_path: Path, iter_workdir: Path) -> None:
    cmd = [
        sys.executable,
        str(REPO_ROOT / "run_benchmark.py"),
        "-f",
        str(dataset),
        "--llm",
        "--model",
        "local_import",
        "--prompts-responses-file",
        str(responses_path),
        "--prefix",
        str(iter_workdir),
    ]
    logger.info("import + evaluate -> %s", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(REPO_ROOT))


def _run_direct(dataset: Path, model_id: str, iter_workdir: Path, factory: Path) -> None:
    cmd = [
        sys.executable,
        str(REPO_ROOT / "run_benchmark.py"),
        "-f",
        str(dataset),
        "--llm",
        "--model",
        model_id,
        "--custom-factory",
        str(factory),
        "--prefix",
        str(iter_workdir),
    ]
    logger.info("direct benchmark -> %s", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Initial completion generation
# ---------------------------------------------------------------------------


def _generate_initial_completions(
    prompts_path: Path,
    responses_path: Path,
    client,
) -> None:
    """Generate completions for each export-mode prompt.

    The prompts.jsonl rows already embed the harness's system block + user
    block as one big text — there is no separate ``category`` field to thread
    through. We bypass the harness-wrapped ``client.prompt()`` (which would
    re-augment and assert) and hit the OpenAI-compatible endpoint directly.
    """
    rows = _read_jsonl(prompts_path)
    out_rows: list[dict] = []
    for idx, row in enumerate(rows, 1):
        issue_id = row["id"]
        try:
            resp = client.chat.chat.completions.create(
                model=client.model,
                messages=[{"role": "user", "content": row["prompt"]}],
                temperature=0.2,
                timeout=120,
            )
            completion = (resp.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.error("initial gen failed for %s: %s", issue_id, exc)
            completion = ""
        out_rows.append({"id": issue_id, "completion": completion})
        if idx % 5 == 0 or idx == len(rows):
            logger.info("initial gen: %d/%d", idx, len(rows))
    _write_jsonl(responses_path, out_rows)


# ---------------------------------------------------------------------------
# Iteration driver
# ---------------------------------------------------------------------------


def _iteration_stats(results: dict[str, dict], iteration: int) -> IterationStats:
    failed = [iid for iid, rec in results.items() if not _did_pass(rec)]
    return IterationStats(
        iteration=iteration,
        total=len(results),
        passed=len(results) - len(failed),
        failed_ids=failed,
    )


def _build_responses_for_failures(
    prompts_index: dict[str, str],
    last_responses: dict[str, str],
    workdir: Path,
    failed_ids: list[str],
    client,
) -> dict[str, str]:
    refined = dict(last_responses)
    for issue_id in failed_ids:
        prompt_text = prompts_index.get(issue_id)
        if not prompt_text:
            logger.warning("no prompt for %s, skipping refine", issue_id)
            continue
        feedback = FailureFeedback(
            issue_id=issue_id,
            prompt=prompt_text,
            last_completion=last_responses.get(issue_id, ""),
            error_excerpt=_gather_log_excerpt(workdir, issue_id),
        )
        new_completion = _refine_completion(client, feedback)
        if new_completion:
            refined[issue_id] = new_completion
    return refined


def evolve(args: argparse.Namespace) -> None:
    dataset = Path(args.dataset).resolve()
    if not dataset.exists():
        raise SystemExit(f"dataset not found: {dataset}")

    base_work = Path(args.workdir).resolve()
    base_work.mkdir(parents=True, exist_ok=True)

    history: list[IterationStats] = []
    client = _build_qwen_client()

    factory_path = REPO_ROOT / "evolve_agent" / "qwen_factory.py"

    # Iter 0 setup: export prompts and generate the first completions.
    iter0 = base_work / "iter_0"
    iter0.mkdir(parents=True, exist_ok=True)
    prompts_path = iter0 / "prompts.jsonl"
    responses_path = iter0 / "responses.jsonl"

    if args.mode == "direct":
        _run_direct(dataset, args.model, iter0, factory_path)
    else:
        _run_export(dataset, prompts_path, iter0)
        _generate_initial_completions(prompts_path, responses_path, client)
        _run_import(dataset, responses_path, iter0)

    results = _scan_results(iter0)
    stats = _iteration_stats(results, 0)
    history.append(stats)
    logger.info(
        "iter 0: pass=%d/%d (%.1f%%)",
        stats.passed,
        stats.total,
        stats.pass_rate * 100,
    )

    if args.mode == "direct":
        # Direct mode only does one shot per iteration; outer loop only useful
        # for the local-import path that can rewrite specific completions.
        _save_history(base_work, history)
        return

    prompts_index = {row["id"]: row["prompt"] for row in _read_jsonl(prompts_path)}
    current_responses = {
        row["id"]: row.get("completion", "")
        for row in _read_jsonl(responses_path)
    }

    for it in range(1, args.iterations + 1):
        if not stats.failed_ids:
            logger.info("no failures left, stopping early")
            break

        iter_dir = base_work / f"iter_{it}"
        iter_dir.mkdir(parents=True, exist_ok=True)
        new_resp_path = iter_dir / "responses.jsonl"

        previous_workdir = base_work / f"iter_{it - 1}"
        refined = _build_responses_for_failures(
            prompts_index=prompts_index,
            last_responses=current_responses,
            workdir=previous_workdir,
            failed_ids=stats.failed_ids,
            client=client,
        )
        _write_jsonl(
            new_resp_path,
            ({"id": iid, "completion": refined[iid]} for iid in prompts_index),
        )
        current_responses = refined

        _run_import(dataset, new_resp_path, iter_dir)
        results = _scan_results(iter_dir)
        stats = _iteration_stats(results, it)
        history.append(stats)
        logger.info(
            "iter %d: pass=%d/%d (%.1f%%) | improved=%d",
            it,
            stats.passed,
            stats.total,
            stats.pass_rate * 100,
            stats.passed - history[-2].passed,
        )

        if stats.pass_rate >= args.target:
            logger.info("hit target pass-rate %.2f, stopping", args.target)
            break

    _save_history(base_work, history)


def _save_history(base_work: Path, history: list[IterationStats]) -> None:
    out = base_work / "evolution_history.json"
    with out.open("w", encoding="utf-8") as fh:
        json.dump(
            [
                {
                    "iteration": s.iteration,
                    "total": s.total,
                    "passed": s.passed,
                    "pass_rate": round(s.pass_rate, 4),
                    "failed_ids": s.failed_ids,
                }
                for s in history
            ],
            fh,
            indent=2,
        )
    logger.info("history saved to %s", out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Evolutionary outer loop for CVDP pass-rate.",
    )
    p.add_argument("-f", "--dataset", required=True, help="path to .jsonl dataset")
    p.add_argument(
        "--workdir",
        default="work_evolve",
        help="output base directory (one subdir per iteration)",
    )
    p.add_argument(
        "--iterations",
        type=int,
        default=3,
        help="max refinement iterations after iter 0",
    )
    p.add_argument(
        "--target",
        type=float,
        default=1.0,
        help="early-stop pass-rate (0.0-1.0)",
    )
    p.add_argument(
        "--mode",
        choices=("local", "direct"),
        default="local",
        help="local = export/import + Qwen refine loop; direct = single Qwen run",
    )
    p.add_argument(
        "--model",
        default="qwen3-evolve",
        help="model id when --mode direct (default qwen3-evolve)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main() -> None:
    args = _parse_args(sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    started = time.time()
    try:
        evolve(args)
    finally:
        logger.info("evolve total runtime: %.1fs", time.time() - started)


if __name__ == "__main__":
    main()
