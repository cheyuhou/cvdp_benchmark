#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Evolve-Agent contributors
# SPDX-License-Identifier: Apache-2.0

"""evolve_v4: three-agent flow (Executor + Reviewer + Modifier).

Layered on top of evolve_v3's monotonic + port-lock + parse-check infra,
v4 splits the repair step into two cooperating agents:

    Reviewer   — cold (temperature 0), structured JSON diagnosis. Reads
                 the spec, the failing candidate, the harness log tail,
                 AND the cocotb testbench source. Outputs:
                     { root_cause, evidence, affected_signals,
                       proposed_fix, confidence }
                 Confidence < 0.4 short-circuits the iteration: we keep
                 the previous best instead of asking the modifier to
                 hallucinate a fix it doesn't understand.

    Modifier   — focused (temperature ~0.3), gets the diagnosis JSON +
                 the previous candidate, and returns the patched file
                 inside one ```systemverilog block. Subject to the same
                 port-lock and iverilog-parse guards as v3, so a bad
                 modifier can never regress the best-so-far.

The Executor is just whatever path runs the harness — iter 0 uses
``run_benchmark.py``; iter N>0 uses ``--model local_import`` over the
modifier's output.

Why three agents and not just one stronger prompt? In practice the
single-prompt v3 repair tends to drift into "rewrite the whole module"
which then fails the port-lock guard. Splitting forces:

  * The reviewer to commit to a *specific* diagnosis (auditable).
  * The modifier to apply ONLY that diagnosis (tight context).

CLI:

    python evolve_agent/evolve_v4.py \
        -f cid002_mixed.jsonl \
        --workdir work_evolve_v4 \
        --iterations 3 --parse-check
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

# Reuse all the harness-shape helpers from v3 — keep the moving parts in
# one place rather than copy-pasting four files of regexes and parsers.
sys.path.insert(0, str(REPO_ROOT / "evolve_agent"))
from evolve_v3 import (  # type: ignore  # noqa: E402
    IterStats,
    _classify_failure,
    _did_pass,
    _extract_code,
    _extract_log_excerpt,
    _extract_module_header,
    _extract_prompt_text,
    _extract_submitted_response,
    _find_issue_dir,
    _headers_match,
    _iverilog_parse_ok,
    _load_raw_results,
    _normalize_header,
    _read_jsonl,
    _run_baseline,
    _run_local_import,
    _write_jsonl,
)

logger = logging.getLogger("evolve_v4")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class ProblemState:
    issue_id: str
    prompt_text: str
    testbench_text: str
    locked_header: str
    best_response: str
    best_passed: bool
    last_attempt: str = ""
    last_log_excerpt: str = ""
    last_diagnosis: dict | None = None
    failure_class: str = "OTHER"


@dataclass
class V4IterStats:
    iteration: int
    submitted_pass: int
    best_pass: int
    total: int
    fixed_now: list[str] = field(default_factory=list)
    rejected_header: list[str] = field(default_factory=list)
    rejected_parse: list[str] = field(default_factory=list)
    skipped_low_confidence: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Testbench extraction
# ---------------------------------------------------------------------------


def _extract_testbench(workdir: Path, issue_id: str, max_chars: int = 12_000) -> str:
    """Read the cocotb test python file(s) for this issue.

    The harness lays them out at ``<issue_base>/harness/<N>/src/test_*.py``,
    excluding ``test_runner.py`` which is just the cocotb wrapper. We
    concatenate up to ``max_chars`` of these so the reviewer knows exactly
    which signals get poked and what asserts fire.
    """
    base = _find_issue_dir(workdir, issue_id)
    if base is None:
        return ""
    chunks: list[str] = []
    used = 0
    for src_dir in sorted(base.glob("harness/*/src")):
        for path in sorted(src_dir.glob("test_*.py")):
            if path.name == "test_runner.py":
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            chunks.append(f"--- {path.name} ---\n{text}")
            used += len(text)
            if used >= max_chars:
                break
        if used >= max_chars:
            break
    if not chunks:
        return ""
    blob = "\n\n".join(chunks)
    return blob[: max_chars] + ("\n…[truncated]" if len(blob) > max_chars else "")


# ---------------------------------------------------------------------------
# Qwen client
# ---------------------------------------------------------------------------


def _build_client():
    from qwen_factory import Qwen_Instance  # type: ignore  # noqa: WPS433

    return Qwen_Instance(
        context=(
            "You are an expert Verilog / SystemVerilog engineer. "
            "Return only the requested content, no commentary."
        )
    )


# ---------------------------------------------------------------------------
# Reviewer agent
# ---------------------------------------------------------------------------


_REVIEWER_SYSTEM = """\
You are a cold, analytical RTL reviewer. You are given:
  - the design SPEC,
  - the cocotb TESTBENCH (the actual test source code),
  - the failing CANDIDATE RTL,
  - the truncated HARNESS LOG.

Your job is to identify the single most likely root cause of the failure
and propose a focused fix. You DO NOT write code.

Respond with ONLY a JSON object, no surrounding prose, no code fences.
Schema (all fields required):

{
  "root_cause": "<one sentence>",
  "evidence": ["<short quote from log or testbench or RTL>", "..."],
  "affected_signals": ["<signal name>", "..."],
  "proposed_fix": "<two-sentence description of the change to make, no RTL code>",
  "confidence": <number between 0.0 and 1.0>,
  "fallback_strategy": "<what to try if the proposed fix doesn't work>"
}

Set confidence < 0.4 if you cannot identify a clear cause.
Be terse. Quote evidence verbatim and keep it short.
"""


def _ask_reviewer(client, *, prompt_text: str, testbench: str, candidate: str,
                  log_excerpt: str, failure_class: str) -> dict:
    user = (
        f"# Spec\n{prompt_text}\n\n"
        f"# Testbench (cocotb)\n```python\n{testbench[:10_000]}\n```\n\n"
        f"# Failing candidate RTL\n```systemverilog\n{candidate[:10_000]}\n```\n\n"
        f"# Harness log tail (failure class hint: {failure_class})\n"
        f"```\n{log_excerpt[:5000]}\n```\n\n"
        "Diagnose now. JSON only."
    )
    try:
        resp = client.chat.chat.completions.create(
            model=client.model,
            messages=[
                {"role": "system", "content": _REVIEWER_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            timeout=180,
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("reviewer call failed: %s", exc)
        return {"confidence": 0.0, "_error": str(exc)}
    return _parse_reviewer_json(text)


def _parse_reviewer_json(text: str) -> dict:
    """Extract the JSON diagnosis even if the model wraps it in fences."""
    if not text:
        return {"confidence": 0.0}
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    raw = fence.group(1) if fence else text
    start = raw.find("{")
    if start == -1:
        return {"confidence": 0.0, "_raw": text[:400]}
    depth = 0
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                blob = raw[start : i + 1]
                try:
                    payload = json.loads(blob)
                except json.JSONDecodeError:
                    return {"confidence": 0.0, "_raw": text[:400]}
                if isinstance(payload, dict):
                    payload.setdefault("confidence", 0.0)
                    return payload
                return {"confidence": 0.0}
    return {"confidence": 0.0, "_raw": text[:400]}


# ---------------------------------------------------------------------------
# Modifier agent
# ---------------------------------------------------------------------------


_MODIFIER_SYSTEM_TEMPLATE = """\
You modify a SystemVerilog file to apply a SPECIFIC diagnosis from a reviewer.
Constraints:

1. Your output MUST be a single complete file inside one ```systemverilog
   code block, no commentary.
2. The file MUST begin with this EXACT module header — same name, same
   ports, same widths, same direction. Do not rename, add, drop, or
   reorder any port:

```
{locked_header}
```

3. Apply ONLY the fix described in the diagnosis. Preserve all unrelated
   logic verbatim from the previous candidate.
4. Do not introduce new modules or files.
"""


def _ask_modifier(client, *, locked_header: str, prompt_text: str,
                  candidate: str, diagnosis: dict, temperature: float) -> str:
    system = _MODIFIER_SYSTEM_TEMPLATE.format(locked_header=locked_header)
    diag_for_prompt = {
        k: v for k, v in diagnosis.items() if not k.startswith("_")
    }
    user = (
        f"# Diagnosis (apply exactly this)\n"
        f"```json\n{json.dumps(diag_for_prompt, indent=2)}\n```\n\n"
        f"# Previous candidate\n```systemverilog\n{candidate}\n```\n\n"
        f"# Spec (for reference only)\n{prompt_text[:6000]}\n\n"
        "Produce the corrected full file now."
    )
    try:
        resp = client.chat.chat.completions.create(
            model=client.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            timeout=180,
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("modifier call failed: %s", exc)
        return ""
    return _extract_code(text) or text


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def evolve_v4(args: argparse.Namespace) -> None:
    dataset = Path(args.dataset).resolve()
    base_work = Path(args.workdir).resolve()
    if base_work.exists() and args.fresh:
        shutil.rmtree(base_work)
    base_work.mkdir(parents=True, exist_ok=True)

    factory = REPO_ROOT / "evolve_agent" / "qwen_factory.py"
    client = _build_client()
    diag_dir = base_work / "diagnoses"
    diag_dir.mkdir(exist_ok=True)

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
        log_excerpt = _extract_log_excerpt(iter0, iid)
        state[iid] = ProblemState(
            issue_id=iid,
            prompt_text=_extract_prompt_text(iter0, iid),
            testbench_text=_extract_testbench(iter0, iid),
            locked_header=header,
            best_response=rtl,
            best_passed=passed,
            last_attempt=rtl,
            last_log_excerpt=log_excerpt,
            failure_class=_classify_failure(log_excerpt),
        )
        if not header:
            logger.warning("%s: no module header extracted", iid)

    history = [
        V4IterStats(
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

        modifier_temp = min(0.7, args.modifier_temperature + 0.1 * (it - 1))
        candidates: dict[str, str] = {}
        rejected_header: list[str] = []
        rejected_parse: list[str] = []
        skipped_low_conf: list[str] = []

        for iid, s in state.items():
            if s.best_passed:
                candidates[iid] = s.best_response
                continue
            if not s.locked_header:
                logger.info("%s: no locked header, retaining last attempt", iid)
                candidates[iid] = s.best_response
                continue

            # ---- REVIEWER ----
            diagnosis = _ask_reviewer(
                client,
                prompt_text=s.prompt_text,
                testbench=s.testbench_text,
                candidate=s.last_attempt or s.best_response,
                log_excerpt=s.last_log_excerpt,
                failure_class=s.failure_class,
            )
            s.last_diagnosis = diagnosis
            (diag_dir / f"{iid}_iter{it}.json").write_text(
                json.dumps(diagnosis, indent=2), encoding="utf-8"
            )
            confidence = float(diagnosis.get("confidence") or 0.0)
            logger.info(
                "%s: review confidence=%.2f root='%s'",
                iid,
                confidence,
                str(diagnosis.get("root_cause", ""))[:80],
            )
            if confidence < args.min_confidence:
                logger.info(
                    "%s: SKIP — reviewer confidence %.2f < %.2f",
                    iid, confidence, args.min_confidence,
                )
                skipped_low_conf.append(iid)
                candidates[iid] = s.best_response
                continue

            # ---- MODIFIER ----
            new_resp = _ask_modifier(
                client,
                locked_header=s.locked_header,
                prompt_text=s.prompt_text,
                candidate=s.last_attempt or s.best_response,
                diagnosis=diagnosis,
                temperature=modifier_temp,
            )
            if not new_resp.strip():
                logger.info("%s: empty modifier output", iid)
                candidates[iid] = s.best_response
                continue

            new_header = _extract_module_header(new_resp)
            if not _headers_match(new_header, s.locked_header):
                logger.info("%s: REJECT — header changed", iid)
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

        best_pass = sum(1 for s in state.values() if s.best_passed)
        history.append(
            V4IterStats(
                iteration=it,
                submitted_pass=submitted_pass,
                best_pass=best_pass,
                total=len(state),
                fixed_now=fixed_now,
                rejected_header=rejected_header,
                rejected_parse=rejected_parse,
                skipped_low_confidence=skipped_low_conf,
            )
        )
        logger.info(
            "iter %d: best=%d/%d, fixed=%s, rej_hdr=%d, rej_parse=%d, low_conf=%d",
            it,
            best_pass,
            len(state),
            fixed_now or "[]",
            len(rejected_header),
            len(rejected_parse),
            len(skipped_low_conf),
        )
        if best_pass / len(state) >= args.target:
            logger.info("hit target %.2f, stopping", args.target)
            break

    _write_jsonl(
        base_work / "best_responses.jsonl",
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
                    "skipped_low_confidence": h.skipped_low_confidence,
                }
                for h in history
            ],
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info(
        "FINAL best pass rate: %d/%d (%.1f%%)",
        history[-1].best_pass,
        history[-1].total,
        100 * history[-1].best_pass / history[-1].total,
    )


def _parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Three-agent CVDP evolution")
    p.add_argument("-f", "--dataset", required=True)
    p.add_argument("--workdir", default="work_evolve_v4")
    p.add_argument("--iterations", type=int, default=3)
    p.add_argument("--target", type=float, default=1.0)
    p.add_argument("--model", default="qwen3")
    p.add_argument("--modifier-temperature", type=float, default=0.3)
    p.add_argument(
        "--min-confidence", type=float, default=0.4,
        help="Skip modifier when reviewer confidence is below this",
    )
    p.add_argument("--parse-check", action="store_true")
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
        evolve_v4(args)
    finally:
        logger.info("evolve_v4 total runtime: %.1fs", time.time() - started)


if __name__ == "__main__":
    main()
