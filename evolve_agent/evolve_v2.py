#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Evolve-Agent contributors
# SPDX-License-Identifier: Apache-2.0

"""Monotonic-non-decreasing evolutionary loop for CVDP pass rate.

Three invariants that v1 violated and v2 enforces:

1. **iter 0 == baseline.** Run ``run_benchmark.py`` with the configured Qwen
   model exactly the way a vanilla evaluation does, so iter 0 inherits every
   harness-side prompt advantage (category-aware system block, etc.).

2. **Never submit a known regression.** Track the best response seen per
   problem across all iterations. When a repair candidate fails on a
   previously-passing problem, we keep the previously-passing response.

3. **Targeted repair.** Each failure is classified (``COMPILE``,
   ``ASSERT_FAIL``, ``TIMEOUT``, ``OTHER``) and a type-specific repair prompt
   is used; repair calls run at higher temperature (default 0.7) so
   successive attempts diverge instead of cloning.

CLI:

    python evolve_agent/evolve_v2.py \
        -f cid002_subset.jsonl \
        --workdir work_evolve_v2 \
        --iterations 3
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("evolve_v2")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class IterStats:
    iteration: int
    submitted_pass: int
    best_pass: int
    total: int
    fixed_now: list[str] = field(default_factory=list)
    regressed_blocked: list[str] = field(default_factory=list)


@dataclass
class ProblemState:
    issue_id: str
    prompt_text: str
    best_response: str
    best_passed: bool
    last_log_excerpt: str = ""
    failure_class: str = "OTHER"


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _load_raw_results(workdir: Path) -> dict[str, dict]:
    raw = workdir / "raw_result.json"
    if not raw.exists():
        return {}
    try:
        return json.loads(raw.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("raw_result.json malformed at %s: %s", raw, exc)
        return {}


def _did_pass(record: dict) -> bool:
    if isinstance(record.get("errors"), int):
        return record["errors"] == 0
    tests = record.get("tests") or record.get("test_results") or []
    if not tests:
        return False
    return all(isinstance(t, dict) and t.get("result", 1) == 0 for t in tests)


# ---------------------------------------------------------------------------
# Harness artifact extraction
# ---------------------------------------------------------------------------


_RTL_EXTS = (".sv", ".v", ".svh", ".vh")


def _extract_submitted_response(workdir: Path, issue_id: str) -> str:
    """Reconstruct the response we sent to the harness for one issue.

    The harness lays out files at ``<workdir>/<issue>/harness/<n>/rtl/*.sv``.
    For non-agentic single-file problems we concat all RTL files (prefixed
    with their relative path comment) so the repair model sees what the
    simulator actually saw.
    """
    base = workdir / issue_id.rsplit("_", 1)[0]
    if not base.exists():
        return ""
    rtl_paths: list[Path] = []
    for harness_dir in sorted(base.glob("harness/*/rtl")):
        for path in sorted(harness_dir.rglob("*")):
            if path.is_file() and path.suffix in _RTL_EXTS:
                rtl_paths.append(path)
        if rtl_paths:
            break
    if not rtl_paths:
        return ""
    chunks: list[str] = []
    for path in rtl_paths:
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n\n".join(chunks)


def _extract_prompt_text(workdir: Path, issue_id: str) -> str:
    base = workdir / issue_id.rsplit("_", 1)[0] / "prompts"
    if not base.exists():
        return ""
    md = sorted(base.glob("*.md"))
    if not md:
        return ""
    try:
        return md[0].read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _extract_log_excerpt(workdir: Path, issue_id: str, max_chars: int = 6000) -> str:
    base = workdir / issue_id.rsplit("_", 1)[0] / "reports"
    if not base.exists():
        return ""
    txt = sorted(base.glob("*.txt"))
    if not txt:
        return ""
    try:
        text = txt[0].read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:]


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


_TIMEOUT_TOKENS = (
    "timed out after",
    "docker_timeout",
    "container exited with code 137",
    "container exited with code 124",
    "killed by signal 9",
)
_COMPILE_TOKENS = (
    "syntax error",
    "syntax in assignment",
    "elaboration error",
    "i give up.",
    "iverilog: error",
    "iverilog: command not found",
    "error: failed to compile",
    "verilator error",
    "no syntax for",
    "unrecognized include",
)
_ASSERT_FAIL_TOKENS = (
    "fail=",
    "failed 1 of",
    "failed 2 of",
    "failed 3 of",
    "failed 4 of",
    "failed 5 of",
    "failed 6 of",
    "failed 7 of",
    "failed 8 of",
    "failed 9 of",
    "x failed",  # pytest "5 failed in"
    "assertionerror",
    "test failed",
    "mismatch:",
    "expected ",
)


def _classify_failure(log_excerpt: str) -> str:
    """Pick a failure class from log keywords.

    Priority: explicit timeout > compile/elab error > assert/test-runtime
    fail > truncated-mid-test (deadlock) > OTHER.
    """
    if not log_excerpt:
        return "OTHER"
    needle = log_excerpt.lower()
    if any(t in needle for t in _TIMEOUT_TOKENS):
        return "TIMEOUT"
    # Compile errors: only trust very specific tokens, since INFO-level log
    # lines often contain the literal substring "error" without meaning a
    # compile failure.
    if any(t in needle for t in _COMPILE_TOKENS):
        return "COMPILE"
    if any(t in needle for t in _ASSERT_FAIL_TOKENS):
        return "ASSERT_FAIL"
    # Truncated-mid-test detection: log shows a test starting but no
    # summary line at the tail.
    has_running = "running test" in needle
    has_summary = (
        "tests=" in needle
        or "passed" in needle and "failed" in needle  # cocotb summary line
        or "===== short test summary" in needle
        or "1 passed" in needle
        or "0 failed" in needle
    )
    if has_running and not has_summary:
        return "TIMEOUT"
    return "OTHER"


# ---------------------------------------------------------------------------
# Qwen client
# ---------------------------------------------------------------------------


def _build_client():
    sys.path.insert(0, str(REPO_ROOT / "evolve_agent"))
    from qwen_factory import Qwen_Instance  # type: ignore  # noqa: WPS433

    return Qwen_Instance(
        context=(
            "You are an expert Verilog / SystemVerilog engineer. "
            "Return only the requested file content; never wrap commentary."
        )
    )


_REPAIR_HEADERS = {
    "COMPILE": (
        "The candidate fails to COMPILE. There is a syntax / typing / "
        "elaboration error in the RTL. Read the iverilog or yosys error "
        "lines and fix exactly that problem. Do not change the architecture; "
        "minimal edits only."
    ),
    "TIMEOUT": (
        "The simulator HANGS — the candidate's FSM or counter logic never "
        "advances, so cocotb sits forever waiting for a signal. Look for "
        "missing state transitions, miswired enables, never-true conditions, "
        "or a counter that does not increment. Rewrite the offending sections; "
        "preserve the module name and port list."
    ),
    "ASSERT_FAIL": (
        "The candidate compiles and runs but produces WRONG values. The "
        "harness log shows assertion failures or expected-vs-got mismatches. "
        "Trace each mismatch back to the RTL bug that caused it and fix; "
        "do not change unrelated logic."
    ),
    "OTHER": (
        "The candidate fails for an unclassified reason. Read the harness "
        "log carefully and propose a focused fix."
    ),
}


def _ask_repair(
    client,
    prompt_text: str,
    submitted: str,
    log_excerpt: str,
    failure_class: str,
    *,
    temperature: float,
    timeout: int = 180,
) -> str:
    system = (
        "You repair RTL designs against a cocotb test harness. "
        + _REPAIR_HEADERS.get(failure_class, _REPAIR_HEADERS["OTHER"])
        + " Return the corrected full file inside one ```systemverilog "
        "code block, with no commentary, no explanations. Preserve module "
        "and port names exactly."
    )
    user = (
        f"# Spec / prompt\n{prompt_text}\n\n"
        f"# Last submitted candidate\n```systemverilog\n{submitted}\n```\n\n"
        f"# Harness failure log (truncated tail)\n{log_excerpt}\n"
    )
    try:
        resp = client.chat.chat.completions.create(
            model=client.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            timeout=timeout,
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("repair call failed: %s", exc)
        return ""
    return _extract_code(text) or text


def _extract_code(text: str) -> str:
    if not text:
        return ""
    matches = re.findall(
        r"```(?:systemverilog|verilog|sv|v)?\s*\n(.*?)```",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return matches[-1].strip() if matches else ""


# ---------------------------------------------------------------------------
# Harness invocations
# ---------------------------------------------------------------------------


def _run_baseline(dataset: Path, factory: Path, model_id: str, workdir: Path) -> None:
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
        str(workdir),
    ]
    logger.info("iter 0 (baseline path): %s", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(REPO_ROOT))


def _run_local_import(dataset: Path, responses: Path, workdir: Path) -> None:
    cmd = [
        sys.executable,
        str(REPO_ROOT / "run_benchmark.py"),
        "-f",
        str(dataset),
        "--llm",
        "--model",
        "local_import",
        "--prompts-responses-file",
        str(responses),
        "--prefix",
        str(workdir),
    ]
    logger.info("import-mode iter: %s", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def evolve_v2(args: argparse.Namespace) -> None:
    dataset = Path(args.dataset).resolve()
    if not dataset.exists():
        raise SystemExit(f"dataset missing: {dataset}")
    base_work = Path(args.workdir).resolve()
    if base_work.exists() and args.fresh:
        shutil.rmtree(base_work)
    base_work.mkdir(parents=True, exist_ok=True)

    factory = REPO_ROOT / "evolve_agent" / "qwen_factory.py"
    client = _build_client()

    # ---- iter 0: full baseline run ----
    iter0 = base_work / "iter_0"
    iter0.mkdir(exist_ok=True)
    _run_baseline(dataset, factory, args.model, iter0)
    iter0_results = _load_raw_results(iter0)

    state: dict[str, ProblemState] = {}
    issue_ids = [row["id"] for row in _read_jsonl(dataset)]
    for iid in issue_ids:
        rec = iter0_results.get(iid, {})
        passed = _did_pass(rec)
        log_excerpt = _extract_log_excerpt(iter0, iid)
        state[iid] = ProblemState(
            issue_id=iid,
            prompt_text=_extract_prompt_text(iter0, iid),
            best_response=_extract_submitted_response(iter0, iid),
            best_passed=passed,
            last_log_excerpt=log_excerpt,
            failure_class=_classify_failure(log_excerpt),
        )

    history = [
        IterStats(
            iteration=0,
            submitted_pass=sum(1 for s in state.values() if s.best_passed),
            best_pass=sum(1 for s in state.values() if s.best_passed),
            total=len(state),
        )
    ]
    logger.info(
        "iter 0: %d/%d passed",
        history[0].best_pass,
        history[0].total,
    )

    # ---- repair iterations ----
    for it in range(1, args.iterations + 1):
        failed_ids = [iid for iid, s in state.items() if not s.best_passed]
        if not failed_ids:
            logger.info("no failures left, stopping early")
            break

        iter_dir = base_work / f"iter_{it}"
        iter_dir.mkdir(exist_ok=True)
        responses_path = iter_dir / "responses.jsonl"

        # Build response file: passing items unchanged (preserves invariant 2),
        # failed items get a fresh repair candidate.
        # Repair temp climbs slightly with iteration to escape clones.
        repair_temp = min(0.9, args.repair_temperature + 0.1 * (it - 1))
        candidates: dict[str, str] = {}
        for iid, s in state.items():
            if s.best_passed:
                candidates[iid] = s.best_response
                continue
            new_resp = _ask_repair(
                client,
                prompt_text=s.prompt_text,
                submitted=s.best_response,
                log_excerpt=s.last_log_excerpt,
                failure_class=s.failure_class,
                temperature=repair_temp,
            )
            if not new_resp.strip():
                logger.info("repair returned empty for %s, retaining previous", iid)
                new_resp = s.best_response
            candidates[iid] = new_resp

        _write_jsonl(
            responses_path,
            ({"id": iid, "completion": candidates[iid]} for iid in issue_ids),
        )

        _run_local_import(dataset, responses_path, iter_dir)
        iter_results = _load_raw_results(iter_dir)

        fixed_now: list[str] = []
        regressed_blocked: list[str] = []
        submitted_pass = 0
        for iid in issue_ids:
            rec = iter_results.get(iid, {})
            passed_now = _did_pass(rec)
            if passed_now:
                submitted_pass += 1
            s = state[iid]
            if passed_now and not s.best_passed:
                # Genuine improvement — promote candidate.
                s.best_response = candidates[iid]
                s.best_passed = True
                fixed_now.append(iid)
            elif not passed_now and s.best_passed:
                # Should never happen because we resubmit best_response for
                # passing items, but guard anyway.
                regressed_blocked.append(iid)
            elif not passed_now:
                # Still failing. Refresh log + class for next attempt.
                s.last_log_excerpt = _extract_log_excerpt(iter_dir, iid)
                s.failure_class = _classify_failure(s.last_log_excerpt)
                # Carry the new attempt forward as the next "last submitted"
                # so repairs don't keep regenerating identical drafts.
                s.best_response = candidates[iid]

        best_pass = sum(1 for s in state.values() if s.best_passed)
        history.append(
            IterStats(
                iteration=it,
                submitted_pass=submitted_pass,
                best_pass=best_pass,
                total=len(state),
                fixed_now=fixed_now,
                regressed_blocked=regressed_blocked,
            )
        )
        logger.info(
            "iter %d: submitted_pass=%d, best_pass=%d/%d, fixed=%s",
            it,
            submitted_pass,
            best_pass,
            len(state),
            fixed_now or "[]",
        )
        if best_pass / len(state) >= args.target:
            logger.info("hit target %.2f, stopping", args.target)
            break

    # ---- final responses + history ----
    final_path = base_work / "best_responses.jsonl"
    _write_jsonl(
        final_path,
        ({"id": iid, "completion": state[iid].best_response} for iid in issue_ids),
    )
    out = base_work / "evolution_history.json"
    out.write_text(
        json.dumps(
            [
                {
                    "iteration": h.iteration,
                    "submitted_pass": h.submitted_pass,
                    "best_pass": h.best_pass,
                    "total": h.total,
                    "best_pass_rate": round(h.best_pass / h.total, 4) if h.total else 0,
                    "fixed_this_iter": h.fixed_now,
                    "regressed_blocked": h.regressed_blocked,
                }
                for h in history
            ],
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("history saved to %s", out)
    logger.info(
        "FINAL best pass rate: %d/%d (%.1f%%)",
        history[-1].best_pass,
        history[-1].total,
        100 * history[-1].best_pass / history[-1].total,
    )


def _parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Monotonic CVDP evolutionary loop.")
    p.add_argument("-f", "--dataset", required=True)
    p.add_argument("--workdir", default="work_evolve_v2")
    p.add_argument("--iterations", type=int, default=3)
    p.add_argument("--target", type=float, default=1.0)
    p.add_argument(
        "--model",
        default="qwen3",
        help="Model id passed through to the factory for iter 0 (default: qwen3).",
    )
    p.add_argument(
        "--repair-temperature",
        type=float,
        default=0.6,
        help="Base temperature for repair calls (climbs by 0.1/iter, capped 0.9).",
    )
    p.add_argument("--fresh", action="store_true", help="Wipe workdir before run")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main() -> None:
    args = _parse(sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    started = time.time()
    try:
        evolve_v2(args)
    finally:
        logger.info("evolve_v2 total runtime: %.1fs", time.time() - started)


if __name__ == "__main__":
    main()
