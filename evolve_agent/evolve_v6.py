#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Evolve-Agent contributors
# SPDX-License-Identifier: Apache-2.0

"""evolve_v6: v5 + modifier best-of-M with cheap candidate scoring.

Why: in v4 / v5 logs we saw the modifier produce ONE candidate per failure
per iteration, and the port-lock / parse / harness guards then accept-or-
reject it. When the single candidate failed a guard, the iteration burned
without making progress, even though the reviewer's diagnosis was useful.

v6 closes that loop. Per failure, per iteration:

    1. Reviewer runs once (cold, JSON diagnosis) — same as v4/v5.
    2. Modifier runs M times at climbing temperatures (e.g. 0.2 / 0.45 / 0.7).
    3. Each output is *scored* without invoking the harness:
         - header_locked       (must pass)
         - iverilog_parse      (must pass when --parse-check)
         - touches_affected    (diff vs prev hits >=1 of the diagnosis's
                                affected_signals — catches "modifier
                                returned a near-clone" failure mode)
         - length_ratio        (0.4 .. 2.5 of prev — catches truncations
                                and rewrites-from-scratch)
    4. The best-scoring candidate is submitted to the harness. If none
       score above threshold we keep the previous best (no regression).

Other v6 niceties:

- We log every candidate's score breakdown so post-hoc you can tell whether
  the model is converging on the diagnosis or thrashing.
- The faithfulness check is whitespace-tolerant (line-by-line, comments
  stripped) so trivial reformat-only edits still register as "no real
  change" and get penalized.
"""

from __future__ import annotations

import argparse
import difflib
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
from evolve_v5 import _adaptive_best_of_n  # type: ignore  # noqa: E402

logger = logging.getLogger("evolve_v6")


# ---------------------------------------------------------------------------
# Candidate scoring
# ---------------------------------------------------------------------------


def _strip_for_diff(text: str) -> list[str]:
    """Comment-stripped, whitespace-collapsed lines for faithfulness check."""
    no_block = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    lines = []
    for raw in no_block.splitlines():
        stripped = re.sub(r"//.*$", "", raw)
        stripped = re.sub(r"\s+", " ", stripped).strip()
        if stripped:
            lines.append(stripped)
    return lines


def _changed_text(prev: str, new: str) -> str:
    """Concatenate added + removed lines as one blob for keyword search."""
    a = _strip_for_diff(prev)
    b = _strip_for_diff(new)
    if a == b:
        return ""
    diff = difflib.ndiff(a, b)
    return "\n".join(line[2:] for line in diff if line.startswith(("+ ", "- ")))


def _touches_signals(prev: str, new: str, signals: list[str]) -> bool:
    if not signals:
        return True  # no signals to check, treat as pass
    diff_blob = _changed_text(prev, new)
    if not diff_blob:
        return False
    needle = diff_blob.lower()
    return any(s.lower() in needle for s in signals if s)


@dataclass
class CandidateScore:
    text: str
    header_locked: bool
    parse_ok: bool | None  # None = not checked
    touches_affected: bool
    length_ratio: float
    total: float
    reasons: list[str] = field(default_factory=list)

    @property
    def is_submittable(self) -> bool:
        if not self.header_locked:
            return False
        if self.parse_ok is False:
            return False
        return True


def _score_candidate(
    text: str,
    *,
    locked_header: str,
    prev_response: str,
    affected_signals: list[str],
    do_parse_check: bool,
) -> CandidateScore:
    score = 0.0
    reasons: list[str] = []
    new_header = _extract_module_header(text)
    header_ok = _headers_match(new_header, locked_header)
    if header_ok:
        score += 4.0
    else:
        reasons.append("header_changed")

    parse_ok: bool | None = None
    if do_parse_check:
        parse_ok = _iverilog_parse_ok(text)
        if parse_ok:
            score += 3.0
        elif parse_ok is False:
            reasons.append("parse_failed")
        # parse_ok None (no docker etc.) -> neutral

    touches = _touches_signals(prev_response, text, affected_signals)
    if touches:
        score += 2.0
    elif affected_signals:
        reasons.append("did_not_touch_diagnosis_signals")

    prev_len = max(1, len(prev_response.splitlines()))
    new_len = max(1, len(text.splitlines()))
    ratio = new_len / prev_len
    if 0.4 <= ratio <= 2.5:
        score += 1.0
    else:
        reasons.append(f"length_ratio={ratio:.2f}")

    return CandidateScore(
        text=text,
        header_locked=header_ok,
        parse_ok=parse_ok,
        touches_affected=touches,
        length_ratio=ratio,
        total=score,
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# Modifier best-of-M
# ---------------------------------------------------------------------------


def _modifier_best_of_m(
    client,
    *,
    locked_header: str,
    prompt_text: str,
    candidate_seed: str,
    diagnosis: dict,
    base_temp: float,
    m_samples: int,
    do_parse_check: bool,
) -> tuple[str | None, list[CandidateScore]]:
    """Generate M modifier candidates, return (best_submittable, all_scores)."""
    affected = diagnosis.get("affected_signals") or []
    if not isinstance(affected, list):
        affected = []
    temps = [
        round(base_temp + 0.15 * i, 2) for i in range(m_samples)
    ]
    temps = [min(0.85, max(0.1, t)) for t in temps]
    scores: list[CandidateScore] = []
    for k, temp in enumerate(temps):
        text = _ask_modifier(
            client,
            locked_header=locked_header,
            prompt_text=prompt_text,
            candidate=candidate_seed,
            diagnosis=diagnosis,
            temperature=temp,
        )
        if not text.strip():
            scores.append(CandidateScore(
                text="", header_locked=False, parse_ok=False,
                touches_affected=False, length_ratio=0.0, total=-1.0,
                reasons=["empty_response"],
            ))
            continue
        sc = _score_candidate(
            text,
            locked_header=locked_header,
            prev_response=candidate_seed,
            affected_signals=affected,
            do_parse_check=do_parse_check,
        )
        scores.append(sc)
        logger.info(
            "  modifier sample %d (T=%.2f): score=%.1f reasons=%s",
            k, temp, sc.total, sc.reasons or [],
        )
        # Early exit if we already have a perfect candidate.
        if sc.total >= 10.0:
            break

    submittable = [sc for sc in scores if sc.is_submittable]
    if not submittable:
        return None, scores
    best = max(submittable, key=lambda sc: sc.total)
    return best.text, scores


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


@dataclass
class V6IterStats:
    iteration: int
    submitted_pass: int
    best_pass: int
    total: int
    fixed_now: list[str] = field(default_factory=list)
    rejected_all_candidates: list[str] = field(default_factory=list)
    skipped_low_confidence: list[str] = field(default_factory=list)


def evolve_v6(args: argparse.Namespace) -> None:
    dataset = Path(args.dataset).resolve()
    base_work = Path(args.workdir).resolve()
    if base_work.exists() and args.fresh:
        shutil.rmtree(base_work)
    base_work.mkdir(parents=True, exist_ok=True)

    factory = REPO_ROOT / "evolve_agent" / "qwen_factory.py"
    client = _build_client()
    diag_dir = base_work / "diagnoses"
    diag_dir.mkdir(exist_ok=True)
    score_dir = base_work / "modifier_scores"
    score_dir.mkdir(exist_ok=True)

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
    history: list[V6IterStats] = [
        V6IterStats(
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

    for it in range(1, args.iterations + 1):
        failed_ids = [iid for iid, s in state.items() if not s.best_passed]
        if not failed_ids:
            logger.info("no failures left, stopping early")
            break

        iter_dir = base_work / f"iter_{it}"
        iter_dir.mkdir(exist_ok=True)
        responses_path = iter_dir / "responses.jsonl"

        candidates: dict[str, str] = {}
        rejected_all: list[str] = []
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
                iid, confidence,
                str(diagnosis.get("root_cause", ""))[:80],
            )
            if confidence < args.min_confidence:
                logger.info("%s: SKIP (low conf)", iid)
                skipped_low_conf.append(iid)
                candidates[iid] = s.best_response
                continue

            best_text, scores = _modifier_best_of_m(
                client,
                locked_header=s.locked_header,
                prompt_text=s.prompt_text,
                candidate_seed=s.last_attempt or s.best_response,
                diagnosis=diagnosis,
                base_temp=args.modifier_temperature,
                m_samples=args.modifier_samples,
                do_parse_check=args.parse_check,
            )
            (score_dir / f"{iid}_iter{it}.json").write_text(
                json.dumps(
                    [
                        {
                            "score": sc.total,
                            "header_locked": sc.header_locked,
                            "parse_ok": sc.parse_ok,
                            "touches_affected": sc.touches_affected,
                            "length_ratio": sc.length_ratio,
                            "reasons": sc.reasons,
                        }
                        for sc in scores
                    ],
                    indent=2,
                ),
                encoding="utf-8",
            )
            if best_text is None:
                logger.info("%s: REJECT — no submittable candidate among %d", iid, len(scores))
                rejected_all.append(iid)
                candidates[iid] = s.best_response
                continue
            candidates[iid] = best_text
            s.last_attempt = best_text

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
            V6IterStats(
                iteration=it,
                submitted_pass=submitted_pass,
                best_pass=best_pass,
                total=len(state),
                fixed_now=fixed_now,
                rejected_all_candidates=rejected_all,
                skipped_low_confidence=skipped_low_conf,
            )
        )
        logger.info(
            "iter %d: best=%d/%d, fixed=%s, rej_all=%d, low_conf=%d",
            it, best_pass, len(state),
            fixed_now or "[]",
            len(rejected_all), len(skipped_low_conf),
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
                    "rejected_all_candidates": h.rejected_all_candidates,
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
    p = argparse.ArgumentParser(description="Best-of-N + three-agent CVDP evolution + modifier best-of-M")
    p.add_argument("-f", "--dataset", required=True)
    p.add_argument("--workdir", default="work_evolve_v6")
    p.add_argument("--iterations", type=int, default=2)
    p.add_argument("--target", type=float, default=1.0)
    p.add_argument("--model", default="qwen3")
    p.add_argument("--samples-iter0", type=int, default=3)
    p.add_argument("--temperatures", type=str, default="")
    p.add_argument("--modifier-temperature", type=float, default=0.25,
                   help="Lowest temperature in the modifier best-of-M sweep")
    p.add_argument("--modifier-samples", type=int, default=3,
                   help="Number of modifier candidates to generate per failure per iter")
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
        evolve_v6(args)
    finally:
        logger.info("evolve_v6 total runtime: %.1fs", time.time() - started)


if __name__ == "__main__":
    main()
