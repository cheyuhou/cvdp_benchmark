#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Evolve-Agent contributors
# SPDX-License-Identifier: Apache-2.0

"""evolve_v5: v4's three-agent repair on top of an adaptive best-of-N iter 0.

The validation runs of v3 and v4 made one thing clear: with a stochastic
model like Qwen3.6-35B-A3B, a single iter 0 sample is the dominant source
of variance in final pass rate. v4 does demonstrably lift +1 via repair,
but its iter-0 floor was 2/8 (vs baseline 4/8) and the loop could not
recover the full gap.

v5 fixes this by sampling K candidates at iter 0 with varied temperatures
and keeping the best per problem. Naive best-of-N is K× cost; we make it
adaptive by only running sample k+1 on the problems that failed sample k:

    sample 0 (T=0.2)  -> full dataset
    sample 1 (T=0.5)  -> still-failing-after-sample-0
    sample 2 (T=0.8)  -> still-failing-after-sample-1
    ...

So the cost grows with the difficulty of the dataset, not its size.

After best-of-N seeds the per-problem ``best_response``, the repair loop is
identical to v4: Reviewer (cold, JSON) -> Modifier (focused, port-locked,
parse-checked) -> harness via ``--model local_import``.
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
sys.path.insert(0, str(REPO_ROOT / "evolve_agent"))

from evolve_v3 import (  # type: ignore  # noqa: E402
    _classify_failure,
    _did_pass,
    _extract_log_excerpt,
    _extract_module_header,
    _extract_prompt_text,
    _extract_submitted_response,
    _headers_match,
    _iverilog_parse_ok,
    _load_raw_results,
    _read_jsonl,
    _run_local_import,
    _write_jsonl,
)
from evolve_v4 import (  # type: ignore  # noqa: E402
    ProblemState,
    V4IterStats,
    _ask_modifier,
    _ask_reviewer,
    _build_client,
    _extract_testbench,
)

logger = logging.getLogger("evolve_v5")


# ---------------------------------------------------------------------------
# Best-of-N at iter 0
# ---------------------------------------------------------------------------


def _run_baseline_with_temp(
    dataset: Path,
    factory: Path,
    model: str,
    workdir: Path,
    *,
    temperature: float,
) -> None:
    """Run ``run_benchmark.py`` with QWEN_TEMPERATURE forced via env."""
    env = os.environ.copy()
    env["QWEN_TEMPERATURE"] = str(temperature)
    cmd = [
        sys.executable,
        str(REPO_ROOT / "run_benchmark.py"),
        "-f", str(dataset),
        "--llm", "--model", model,
        "--custom-factory", str(factory),
        "--prefix", str(workdir),
    ]
    logger.info("baseline T=%.2f -> %s", temperature, workdir.name)
    subprocess.run(cmd, check=True, cwd=str(REPO_ROOT), env=env)


def _build_subset_jsonl(full_dataset: Path, ids: list[str], out_path: Path) -> None:
    keep = set(ids)
    with full_dataset.open("r", encoding="utf-8") as src, out_path.open("w", encoding="utf-8") as dst:
        for line in src:
            if not line.strip():
                continue
            obj = json.loads(line)
            iid = obj.get("id") or obj.get("issue_id") or (next(iter(obj)) if len(obj) == 1 else None)
            if iid in keep:
                dst.write(line)


def _adaptive_best_of_n(
    dataset: Path,
    factory: Path,
    model: str,
    base_work: Path,
    samples: int,
    temperatures: list[float],
    issue_ids: list[str],
) -> dict[str, ProblemState]:
    """Run K samples at iter 0, only re-running on still-failing problems."""
    if samples < 1:
        raise ValueError("samples must be >= 1")

    # Per-problem best-so-far, keyed by issue id.
    best_passed: dict[str, bool] = {iid: False for iid in issue_ids}
    best_workdir: dict[str, Path] = {}

    pending = list(issue_ids)
    for k in range(samples):
        if not pending:
            logger.info("all issues passed by sample %d, stopping early", k)
            break
        sample_dir = base_work / f"iter_0_sample_{k}"
        sample_dir.mkdir(exist_ok=True)
        if k == 0:
            sample_dataset = dataset
        else:
            sample_dataset = sample_dir / "subset.jsonl"
            _build_subset_jsonl(dataset, pending, sample_dataset)
        temp = temperatures[k] if k < len(temperatures) else temperatures[-1]
        _run_baseline_with_temp(sample_dataset, factory, model, sample_dir, temperature=temp)
        results = _load_raw_results(sample_dir)
        new_pending: list[str] = []
        for iid in pending:
            rec = results.get(iid)
            if rec is None:
                # Issue not in this sample's run (because we filtered).
                new_pending.append(iid)
                continue
            if _did_pass(rec):
                best_passed[iid] = True
                best_workdir[iid] = sample_dir
            else:
                # Even if it didn't pass, take this as the best-known so far
                # iff we don't already have one.
                if iid not in best_workdir:
                    best_workdir[iid] = sample_dir
                new_pending.append(iid)
        passed_so_far = sum(1 for v in best_passed.values() if v)
        logger.info(
            "iter 0 sample %d: T=%.2f, pass=%d/%d, still-failing=%d",
            k, temp, passed_so_far, len(issue_ids), len(new_pending),
        )
        pending = new_pending

    # Build state from per-problem best workdir.
    state: dict[str, ProblemState] = {}
    for iid in issue_ids:
        wd = best_workdir.get(iid, base_work / "iter_0_sample_0")
        rtl = _extract_submitted_response(wd, iid)
        header = _extract_module_header(rtl)
        log_excerpt = _extract_log_excerpt(wd, iid)
        state[iid] = ProblemState(
            issue_id=iid,
            prompt_text=_extract_prompt_text(wd, iid),
            testbench_text=_extract_testbench(wd, iid),
            locked_header=header,
            best_response=rtl,
            best_passed=best_passed[iid],
            last_attempt=rtl,
            last_log_excerpt=log_excerpt,
            failure_class=_classify_failure(log_excerpt),
        )
    return state


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def evolve_v5(args: argparse.Namespace) -> None:
    dataset = Path(args.dataset).resolve()
    base_work = Path(args.workdir).resolve()
    if base_work.exists() and args.fresh:
        shutil.rmtree(base_work)
    base_work.mkdir(parents=True, exist_ok=True)

    factory = REPO_ROOT / "evolve_agent" / "qwen_factory.py"
    client = _build_client()
    diag_dir = base_work / "diagnoses"
    diag_dir.mkdir(exist_ok=True)

    issue_ids = [row["id"] for row in _read_jsonl(dataset)]
    temps = [float(t) for t in args.temperatures.split(",")] if args.temperatures else [0.2, 0.5, 0.8]

    state = _adaptive_best_of_n(
        dataset=dataset,
        factory=factory,
        model=args.model,
        base_work=base_work,
        samples=args.samples_iter0,
        temperatures=temps,
        issue_ids=issue_ids,
    )

    history: list[V4IterStats] = [
        V4IterStats(
            iteration=0,
            submitted_pass=sum(1 for s in state.values() if s.best_passed),
            best_pass=sum(1 for s in state.values() if s.best_passed),
            total=len(state),
        )
    ]
    logger.info(
        "iter 0 best-of-%d: %d/%d passed",
        args.samples_iter0,
        history[0].best_pass,
        history[0].total,
    )

    # ---- Repair iterations: identical to v4 ----
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
                candidates[iid] = s.best_response
                continue

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
                json.dumps(diagnosis, indent=2), encoding="utf-8",
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

            new_resp = _ask_modifier(
                client,
                locked_header=s.locked_header,
                prompt_text=s.prompt_text,
                candidate=s.last_attempt or s.best_response,
                diagnosis=diagnosis,
                temperature=modifier_temp,
            )
            if not new_resp.strip():
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
            it, best_pass, len(state),
            fixed_now or "[]",
            len(rejected_header), len(rejected_parse), len(skipped_low_conf),
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
        "FINAL best pass rate: %d/%d (%.1f%%) — history saved to %s",
        history[-1].best_pass,
        history[-1].total,
        100 * history[-1].best_pass / history[-1].total,
        out,
    )


def _parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Best-of-N + three-agent CVDP evolution")
    p.add_argument("-f", "--dataset", required=True)
    p.add_argument("--workdir", default="work_evolve_v5")
    p.add_argument("--iterations", type=int, default=2)
    p.add_argument("--target", type=float, default=1.0)
    p.add_argument("--model", default="qwen3")
    p.add_argument("--samples-iter0", type=int, default=3,
                   help="K samples per problem at iter 0 (adaptive: only re-runs failing items)")
    p.add_argument("--temperatures", type=str, default="",
                   help="comma-separated temps for iter 0 samples (default 0.2,0.5,0.8)")
    p.add_argument("--modifier-temperature", type=float, default=0.3)
    p.add_argument("--min-confidence", type=float, default=0.4)
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
        evolve_v5(args)
    finally:
        logger.info("evolve_v5 total runtime: %.1fs", time.time() - started)


if __name__ == "__main__":
    main()
