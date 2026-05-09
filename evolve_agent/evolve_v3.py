#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Evolve-Agent contributors
# SPDX-License-Identifier: Apache-2.0

"""evolve_v3: monotonic + port-preservation + pre-submit validation.

Built on top of v2's monotonic-non-decreasing structure, with three guards
that v2 was missing and the actual bug in cid002 validation hit:

1. **Port-list preservation.**  Before each repair we extract the module
    header (``module <name> (...);``) from the candidate that the harness
    successfully built/ran in iter 0 — that header is, by construction,
    compatible with the cocotb testbench.  The repair prompt embeds the
    header verbatim and the model is told to start the file with it.

2. **Header diff validation.**  After the repair returns, we re-parse the
    new candidate's module header and reject the candidate (keeping the
    previous best) if the port list differs from the locked-in one.  This
    is what kept v2 from ever fixing FIR_0003: the model rewrote the port
    list and broke testbench compatibility on every repair attempt.

3. **iverilog parse gate.**  We optionally run ``iverilog -tnull`` (via
    the cvdp-sim Docker image, which already ships iverilog) on the repair
    candidate.  If parse fails, we skip submission to the harness and keep
    the previous best.  Cheap insurance against syntax-broken outputs.

Other v3 niceties:

- Per-iteration repair temperature climb (0.5 -> 0.7 -> 0.85) with a tiny
  variation in the user-prompt phrasing each call so the model doesn't
  collapse to the same wrong answer.
- Tracks ``best_response`` independently of ``last_submitted_response`` so
  next-iter repairs riff on the previous attempt instead of re-cloning.
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
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("evolve_v3")


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
    rejected_header: list[str] = field(default_factory=list)
    rejected_parse: list[str] = field(default_factory=list)


@dataclass
class ProblemState:
    issue_id: str
    prompt_text: str
    locked_header: str  # canonical "module name (...);" — must be preserved
    best_response: str
    best_passed: bool
    last_attempt: str = ""  # most recent submission, even if it failed
    last_log_excerpt: str = ""
    failure_class: str = "OTHER"


# ---------------------------------------------------------------------------
# IO
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
    except json.JSONDecodeError:
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


def _find_issue_dir(workdir: Path, issue_id: str) -> Path | None:
    """The harness flattens ``cvdp_copilot_X_NNNN`` -> directory ``cvdp_copilot_X``.

    Some issues don't follow the strict NNNN suffix convention, so we also
    fall back to a glob that matches any directory whose name is a prefix
    of the issue id.
    """
    base = workdir / issue_id.rsplit("_", 1)[0]
    if base.exists():
        return base
    # Fallback: longest prefix match.
    candidates = [p for p in workdir.glob("cvdp_*") if issue_id.startswith(p.name)]
    if candidates:
        return max(candidates, key=lambda p: len(p.name))
    return None


def _extract_submitted_response(workdir: Path, issue_id: str) -> str:
    base = _find_issue_dir(workdir, issue_id)
    if base is None:
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
    chunks = []
    for path in rtl_paths:
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n\n".join(chunks)


def _extract_prompt_text(workdir: Path, issue_id: str) -> str:
    base = _find_issue_dir(workdir, issue_id)
    if base is None:
        return ""
    md = sorted((base / "prompts").glob("*.md")) if (base / "prompts").exists() else []
    if not md:
        return ""
    try:
        return md[0].read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _extract_log_excerpt(workdir: Path, issue_id: str, max_chars: int = 6000) -> str:
    base = _find_issue_dir(workdir, issue_id)
    if base is None:
        return ""
    txt = sorted((base / "reports").glob("*.txt")) if (base / "reports").exists() else []
    if not txt:
        return ""
    try:
        return txt[0].read_text(encoding="utf-8", errors="replace")[-max_chars:]
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Module-header extraction
# ---------------------------------------------------------------------------


_MODULE_RE = re.compile(
    r"\bmodule\s+([A-Za-z_]\w*)\s*"   # module <name>
    r"(?:#\s*\([^;]*?\)\s*)?"           # optional parameter list, non-greedy
    r"\((?:[^;]*?)\)\s*;",              # port list ending with );
    re.DOTALL,
)


def _extract_module_header(rtl: str) -> str:
    """Return the verbatim ``module ... ;`` opener of the first module."""
    if not rtl:
        return ""
    match = _MODULE_RE.search(rtl)
    return match.group(0) if match else ""


def _normalize_header(header: str) -> str:
    """Whitespace-insensitive canonical form for header equality."""
    if not header:
        return ""
    # Strip line comments and block comments.
    no_line = re.sub(r"//[^\n]*", "", header)
    no_block = re.sub(r"/\*.*?\*/", "", no_line, flags=re.DOTALL)
    return re.sub(r"\s+", " ", no_block).strip().lower()


def _headers_match(a: str, b: str) -> bool:
    return _normalize_header(a) == _normalize_header(b) and bool(a) and bool(b)


# ---------------------------------------------------------------------------
# Failure classification (carried over from v2 with v3 tweaks)
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
    "elaboration error",
    "i give up.",
    "iverilog: error",
    "error: failed to compile",
    "verilator error",
)
_ASSERT_FAIL_TOKENS = (
    "fail=",
    " failed in ",  # "5 failed in 1.45s"
    "failed 1 of",
    "failed 2 of",
    "failed 3 of",
    "failed 4 of",
    "failed 5 of",
    "failed 6 of",
    "failed 7 of",
    "failed 8 of",
    "failed 9 of",
    "assertionerror",
    "test failed",
    "mismatch:",
)


def _classify_failure(log_excerpt: str) -> str:
    if not log_excerpt:
        return "OTHER"
    needle = log_excerpt.lower()
    if any(t in needle for t in _TIMEOUT_TOKENS):
        return "TIMEOUT"
    if any(t in needle for t in _COMPILE_TOKENS):
        return "COMPILE"
    if any(t in needle for t in _ASSERT_FAIL_TOKENS):
        return "ASSERT_FAIL"
    has_running = "running test" in needle
    has_summary = (
        "tests=" in needle
        or "passed" in needle and "failed" in needle
        or "===== short test summary" in needle
    )
    if has_running and not has_summary:
        return "TIMEOUT"
    return "OTHER"


# ---------------------------------------------------------------------------
# Pre-submit gates
# ---------------------------------------------------------------------------


_SIM_IMAGE = os.environ.get("OSS_SIM_IMAGE", "nvidia/cvdp-sim:v1.0.0")


def _iverilog_parse_ok(rtl: str) -> bool | None:
    """Return True if iverilog accepts the RTL, False if it errors, None if we
    can't run iverilog at all (don't penalize the candidate in that case).
    """
    if not rtl.strip():
        return False
    if not shutil.which("docker"):
        return None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "candidate.sv"
            src.write_text(rtl, encoding="utf-8")
            cmd = [
                "docker", "run", "--rm",
                "-v", f"{tmp}:/work",
                "--network", "none",
                _SIM_IMAGE,
                "iverilog", "-g2012", "-tnull", "-o", "/dev/null",
                "/work/candidate.sv",
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
            )
            return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("iverilog parse skipped: %s", exc)
        return None


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
        "elaboration error. Read the iverilog or yosys error and fix exactly "
        "that. Minimal edits only."
    ),
    "TIMEOUT": (
        "The simulator HANGS — the candidate's FSM or counter logic never "
        "advances, so cocotb sits forever waiting for a signal. Look for "
        "missing state transitions, miswired enables, never-true conditions, "
        "or a counter that does not increment. Rewrite the offending sections."
    ),
    "ASSERT_FAIL": (
        "The candidate compiles and runs but produces WRONG values. The "
        "harness log shows assertion failures or expected-vs-got mismatches. "
        "Trace each mismatch back to the RTL bug and fix; do not change "
        "unrelated logic."
    ),
    "OTHER": (
        "The candidate fails for an unclassified reason. Read the harness "
        "log carefully and propose a focused fix."
    ),
}

_VARIATION_NUDGES = (
    "",
    "Try a noticeably different implementation strategy this time.",
    "Treat the previous attempt as a wrong direction; rebuild the body.",
)


def _ask_repair(
    client,
    *,
    prompt_text: str,
    locked_header: str,
    last_attempt: str,
    log_excerpt: str,
    failure_class: str,
    temperature: float,
    iter_idx: int,
    timeout: int = 180,
) -> str:
    nudge = _VARIATION_NUDGES[min(iter_idx - 1, len(_VARIATION_NUDGES) - 1)]
    system = (
        "You repair RTL designs against a cocotb test harness.\n"
        + _REPAIR_HEADERS.get(failure_class, _REPAIR_HEADERS["OTHER"])
        + "\n\nHARD RULE: your file MUST begin with the EXACT module "
        "declaration shown below — same module name, same port names, "
        "same widths, same direction. Do not rename, add, drop, or reorder "
        "any port. The cocotb testbench depends on this exact interface.\n\n"
        f"REQUIRED MODULE HEADER:\n```\n{locked_header}\n```\n\n"
        "Return the corrected full file inside one ```systemverilog code "
        "block, no commentary."
    )
    user = (
        f"# Spec\n{prompt_text}\n\n"
        f"# Last submitted candidate\n```systemverilog\n{last_attempt}\n```\n\n"
        f"# Harness failure log (truncated tail)\n{log_excerpt}\n\n"
        + (f"# Note\n{nudge}\n" if nudge else "")
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
        "-f", str(dataset),
        "--llm", "--model", model_id,
        "--custom-factory", str(factory),
        "--prefix", str(workdir),
    ]
    logger.info("iter 0 (baseline path): %s", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(REPO_ROOT))


def _run_local_import(dataset: Path, responses: Path, workdir: Path) -> None:
    cmd = [
        sys.executable,
        str(REPO_ROOT / "run_benchmark.py"),
        "-f", str(dataset),
        "--llm", "--model", "local_import",
        "--prompts-responses-file", str(responses),
        "--prefix", str(workdir),
    ]
    logger.info("import-mode iter: %s", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def evolve_v3(args: argparse.Namespace) -> None:
    dataset = Path(args.dataset).resolve()
    base_work = Path(args.workdir).resolve()
    if base_work.exists() and args.fresh:
        shutil.rmtree(base_work)
    base_work.mkdir(parents=True, exist_ok=True)

    factory = REPO_ROOT / "evolve_agent" / "qwen_factory.py"
    client = _build_client()

    iter0 = base_work / "iter_0"
    iter0.mkdir(exist_ok=True)
    _run_baseline(dataset, factory, args.model, iter0)
    iter0_results = _load_raw_results(iter0)

    issue_ids = [row["id"] for row in _read_jsonl(dataset)]
    state: dict[str, ProblemState] = {}
    for iid in issue_ids:
        rec = iter0_results.get(iid, {})
        passed = _did_pass(rec)
        rtl = _extract_submitted_response(iter0, iid)
        header = _extract_module_header(rtl)
        if not header:
            logger.warning(
                "could not extract module header for %s — repair will skip header lock",
                iid,
            )
        log_excerpt = _extract_log_excerpt(iter0, iid)
        state[iid] = ProblemState(
            issue_id=iid,
            prompt_text=_extract_prompt_text(iter0, iid),
            locked_header=header,
            best_response=rtl,
            best_passed=passed,
            last_attempt=rtl,
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
    logger.info("iter 0: %d/%d passed", history[0].best_pass, history[0].total)

    for it in range(1, args.iterations + 1):
        failed_ids = [iid for iid, s in state.items() if not s.best_passed]
        if not failed_ids:
            logger.info("no failures left, stopping early")
            break

        iter_dir = base_work / f"iter_{it}"
        iter_dir.mkdir(exist_ok=True)
        responses_path = iter_dir / "responses.jsonl"

        repair_temp = min(0.9, args.repair_temperature + 0.15 * (it - 1))
        rejected_header: list[str] = []
        rejected_parse: list[str] = []
        candidates: dict[str, str] = {}

        for iid, s in state.items():
            if s.best_passed:
                candidates[iid] = s.best_response
                continue
            if not s.locked_header:
                # No locked header means iter 0 produced unparseable RTL —
                # we won't try to repair without something to anchor on.
                logger.info("%s: no locked header, retaining last attempt", iid)
                candidates[iid] = s.best_response
                continue

            new_resp = _ask_repair(
                client,
                prompt_text=s.prompt_text,
                locked_header=s.locked_header,
                last_attempt=s.last_attempt or s.best_response,
                log_excerpt=s.last_log_excerpt,
                failure_class=s.failure_class,
                temperature=repair_temp,
                iter_idx=it,
            )
            if not new_resp.strip():
                logger.info("%s: empty repair, keeping previous", iid)
                candidates[iid] = s.best_response
                continue

            new_header = _extract_module_header(new_resp)
            if not _headers_match(new_header, s.locked_header):
                logger.info(
                    "%s: REJECT — header changed (locked=%s, got=%s)",
                    iid,
                    _normalize_header(s.locked_header)[:80],
                    _normalize_header(new_header)[:80],
                )
                rejected_header.append(iid)
                candidates[iid] = s.best_response
                continue

            if args.parse_check:
                ok = _iverilog_parse_ok(new_resp)
                if ok is False:
                    logger.info("%s: REJECT — iverilog parse failed", iid)
                    rejected_parse.append(iid)
                    candidates[iid] = s.best_response
                    continue

            candidates[iid] = new_resp
            s.last_attempt = new_resp

        _write_jsonl(
            responses_path,
            ({"id": iid, "completion": candidates[iid]} for iid in issue_ids),
        )
        _run_local_import(dataset, responses_path, iter_dir)
        iter_results = _load_raw_results(iter_dir)

        fixed_now: list[str] = []
        submitted_pass = 0
        for iid in issue_ids:
            rec = iter_results.get(iid, {})
            passed_now = _did_pass(rec)
            if passed_now:
                submitted_pass += 1
            s = state[iid]
            if passed_now and not s.best_passed:
                s.best_response = candidates[iid]
                s.best_passed = True
                fixed_now.append(iid)
            elif not passed_now:
                s.last_log_excerpt = _extract_log_excerpt(iter_dir, iid)
                s.failure_class = _classify_failure(s.last_log_excerpt)
                # Keep last_attempt as the just-submitted candidate so the
                # next repair builds on what was actually tried.

        best_pass = sum(1 for s in state.values() if s.best_passed)
        history.append(
            IterStats(
                iteration=it,
                submitted_pass=submitted_pass,
                best_pass=best_pass,
                total=len(state),
                fixed_now=fixed_now,
                rejected_header=rejected_header,
                rejected_parse=rejected_parse,
            )
        )
        logger.info(
            "iter %d: submitted=%d, best=%d/%d, fixed=%s, rej_header=%d, rej_parse=%d",
            it,
            submitted_pass,
            best_pass,
            len(state),
            fixed_now or "[]",
            len(rejected_header),
            len(rejected_parse),
        )
        if best_pass / len(state) >= args.target:
            logger.info("hit target %.2f, stopping", args.target)
            break

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
                    "rejected_header": h.rejected_header,
                    "rejected_parse": h.rejected_parse,
                }
                for h in history
            ],
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info(
        "FINAL best pass rate: %d/%d (%.1f%%) — history saved to %s",
        history[-1].best_pass,
        history[-1].total,
        100 * history[-1].best_pass / history[-1].total,
        out,
    )


def _parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Monotonic CVDP evolution with port lock")
    p.add_argument("-f", "--dataset", required=True)
    p.add_argument("--workdir", default="work_evolve_v3")
    p.add_argument("--iterations", type=int, default=3)
    p.add_argument("--target", type=float, default=1.0)
    p.add_argument("--model", default="qwen3")
    p.add_argument("--repair-temperature", type=float, default=0.5)
    p.add_argument(
        "--parse-check",
        action="store_true",
        help="iverilog -tnull a candidate before submitting (uses cvdp-sim image)",
    )
    p.add_argument("--fresh", action="store_true")
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
        evolve_v3(args)
    finally:
        logger.info("evolve_v3 total runtime: %.1fs", time.time() - started)


if __name__ == "__main__":
    main()
