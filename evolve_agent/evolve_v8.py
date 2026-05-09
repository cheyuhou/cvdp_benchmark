#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Evolve-Agent contributors
# SPDX-License-Identifier: Apache-2.0

"""evolve_v8: same model as baseline, added modifier inner self-repair loop.

The cid003-20 validation showed v7 lifted +2 over baseline, but every
fix came from iter-0 best-of-N — the repair iterations landed zero new
fixes. Inspecting modifier_scores: the dominant rejection reason was
``parse_failed`` — the same model that diagnosed the bug correctly
couldn't write a syntactically valid SystemVerilog patch on the first
shot.

The fair next move (no model swap, since that would conflate "agent
flow improvement" with "use a smarter model") is to give the modifier a
**self-repair loop**: after each candidate, if iverilog rejects it, feed
the parse error back to the *same* model and ask it to fix only the
syntax. Up to ``--inner-repair`` attempts per candidate. This lets the
agent flow extract more value from the existing model without changing
the comparison anchor.

Concretely, per failure per iter:

    for k in 1..M (best-of-M loop):
        candidate ← modifier(prompt, diagnosis, T=base+0.15·k)
        for attempt in 1..inner_repair:
            if iverilog -tnull(candidate) ok: break
            err ← parse_error
            candidate ← modifier(prompt, diagnosis, prev=candidate, parse_err=err)
        score(candidate)
    submit best-scoring submittable candidate

Otherwise inherits everything from v7: monotonic best-tracking, port-
lock, faithfulness check, length sanity, adaptive best-of-N at iter 0,
multi-hypothesis reviewer with primary+alternative fallback,
test-aware modifier.

Same model used by baseline, reviewer, and modifier — apples-to-apples
comparison preserved.
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

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "evolve_agent"))

from evolve_v3 import (  # type: ignore  # noqa: E402
    _classify_failure,
    _did_pass,
    _extract_code,
    _extract_log_excerpt,
    _extract_module_header,
    _extract_prompt_text,
    _extract_submitted_response,
    _headers_match,
    _iverilog_parse_ok,
    _load_raw_results,
    _normalize_header,
    _read_jsonl,
    _run_local_import,
    _write_jsonl,
)
from evolve_v4 import ProblemState, _build_client, _extract_testbench  # type: ignore  # noqa: E402
from evolve_v5 import _adaptive_best_of_n  # type: ignore  # noqa: E402
from evolve_v6 import CandidateScore, _score_candidate  # type: ignore  # noqa: E402
from evolve_v7 import (  # type: ignore  # noqa: E402
    _MULTI_REVIEWER_SYSTEM,
    _MODIFIER_TEMPLATE,
    _ask_reviewer_multi,
)

logger = logging.getLogger("evolve_v8")


# ---------------------------------------------------------------------------
# iverilog parse with stderr capture (so we can feed errors back to model)
# ---------------------------------------------------------------------------


_SIM_IMAGE = os.environ.get("OSS_SIM_IMAGE", "nvidia/cvdp-sim:v1.0.0")


def _iverilog_parse_with_stderr(rtl: str, timeout: int = 60) -> tuple[bool | None, str]:
    """Like ``_iverilog_parse_ok`` but also returns the compiler stderr.

    Returns (None, "") when iverilog cannot run (no docker etc.) — caller
    should treat that as "parse not checked" and not penalize.
    """
    if not rtl.strip():
        return False, "empty input"
    if not shutil.which("docker"):
        return None, ""
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
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            err = proc.stderr.strip()
            return proc.returncode == 0, err
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("iverilog parse skipped: %s", exc)
        return None, str(exc)


# ---------------------------------------------------------------------------
# Modifier with inner self-repair
# ---------------------------------------------------------------------------


def _ask_modifier_test_aware(
    client,
    *,
    locked_header: str,
    prompt_text: str,
    testbench: str,
    candidate: str,
    diagnosis: dict,
    temperature: float,
    parse_error_feedback: str = "",
    timeout: int = 180,
) -> str:
    """Modifier call. ``parse_error_feedback`` triggers self-repair mode."""
    system = _MODIFIER_TEMPLATE.format(locked_header=locked_header)
    diag_for_prompt = {k: v for k, v in diagnosis.items() if not k.startswith("_")}
    if parse_error_feedback:
        # Self-repair: tighter framing, focus on fixing the compiler error.
        system = (
            "You are fixing a SYNTAX/ELABORATION error in a candidate. "
            "Output a single complete file inside one ```systemverilog "
            "code block. Preserve every working part of the previous "
            "candidate verbatim; only change what's needed to make "
            "iverilog accept the file. Do NOT change the module header.\n\n"
            f"Required module header:\n```\n{locked_header}\n```\n"
        )
        user = (
            f"# iverilog error from previous candidate\n```\n{parse_error_feedback[:3000]}\n```\n\n"
            f"# Previous candidate (broken)\n```systemverilog\n{candidate}\n```\n\n"
            f"# Original diagnosis (for context)\n"
            f"```json\n{json.dumps(diag_for_prompt, indent=2)}\n```\n\n"
            "Return only the corrected file."
        )
    else:
        user = (
            f"# Diagnosis (apply exactly this)\n"
            f"```json\n{json.dumps(diag_for_prompt, indent=2)}\n```\n\n"
            f"# Testbench excerpt (cocotb)\n```python\n{testbench[:6000]}\n```\n\n"
            f"# Previous candidate\n```systemverilog\n{candidate}\n```\n\n"
            f"# Spec (reference)\n{prompt_text[:5000]}\n\n"
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
            timeout=timeout,
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("modifier call failed: %s", exc)
        return ""
    return _extract_code(text) or text


def _modifier_with_self_repair(
    client,
    *,
    locked_header: str,
    prompt_text: str,
    testbench: str,
    candidate_seed: str,
    diagnosis: dict,
    temperature: float,
    inner_repair: int,
    do_parse_check: bool,
) -> tuple[str, bool, int]:
    """Generate a candidate, then self-repair it up to ``inner_repair`` times.

    Returns (text, header_locked_and_parses, attempts_used). The caller
    still applies the static scoring on the final text.
    """
    text = _ask_modifier_test_aware(
        client,
        locked_header=locked_header,
        prompt_text=prompt_text,
        testbench=testbench,
        candidate=candidate_seed,
        diagnosis=diagnosis,
        temperature=temperature,
    )
    if not text.strip():
        return "", False, 0

    if not do_parse_check or inner_repair <= 0:
        return text, True, 1

    attempts = 1
    for r in range(inner_repair):
        ok, err = _iverilog_parse_with_stderr(text)
        if ok or ok is None:
            return text, bool(ok), attempts
        # Header-locked check happens at scoring time, here we just chase
        # syntax; but bail out if the model rewrote the header.
        new_header = _extract_module_header(text)
        if new_header and not _headers_match(new_header, locked_header):
            logger.info("    self-repair: header drift detected, abandoning")
            return text, False, attempts
        logger.info("    self-repair attempt %d: parse failed, feeding error back", r + 1)
        repaired = _ask_modifier_test_aware(
            client,
            locked_header=locked_header,
            prompt_text=prompt_text,
            testbench=testbench,
            candidate=text,
            diagnosis=diagnosis,
            temperature=max(0.1, temperature - 0.1),
            parse_error_feedback=err,
        )
        attempts += 1
        if not repaired.strip():
            break
        text = repaired
    return text, False, attempts


def _modifier_best_of_m_v8(
    client,
    *,
    locked_header: str,
    prompt_text: str,
    testbench: str,
    candidate_seed: str,
    diagnosis: dict,
    base_temp: float,
    m_samples: int,
    inner_repair: int,
    do_parse_check: bool,
) -> tuple[str | None, list[CandidateScore]]:
    affected = diagnosis.get("affected_signals") or []
    if not isinstance(affected, list):
        affected = []
    temps = [round(min(0.85, max(0.1, base_temp + 0.15 * i)), 2)
             for i in range(m_samples)]
    scores: list[CandidateScore] = []
    for k, temp in enumerate(temps):
        text, _, attempts = _modifier_with_self_repair(
            client,
            locked_header=locked_header,
            prompt_text=prompt_text,
            testbench=testbench,
            candidate_seed=candidate_seed,
            diagnosis=diagnosis,
            temperature=temp,
            inner_repair=inner_repair,
            do_parse_check=do_parse_check,
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
        sc.reasons.append(f"inner_repair_attempts={attempts}")
        scores.append(sc)
        logger.info(
            "    modifier sample %d (T=%.2f attempts=%d): score=%.1f reasons=%s",
            k, temp, attempts, sc.total, sc.reasons,
        )
        if sc.total >= 10.0:
            break
    submittable = [sc for sc in scores if sc.is_submittable]
    if not submittable:
        return None, scores
    best = max(submittable, key=lambda sc: sc.total)
    return best.text, scores


# ---------------------------------------------------------------------------
# Driver — same shape as v7
# ---------------------------------------------------------------------------


@dataclass
class V8IterStats:
    iteration: int
    submitted_pass: int
    best_pass: int
    total: int
    fixed_now: list[str] = field(default_factory=list)
    fixed_via_alternative: list[str] = field(default_factory=list)
    rejected_all: list[str] = field(default_factory=list)
    skipped_low_conf: list[str] = field(default_factory=list)


def evolve_v8(args: argparse.Namespace) -> None:
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
    temps = [float(t) for t in args.temperatures.split(",")] if args.temperatures else [0.2, 0.55, 0.85]

    state = _adaptive_best_of_n(
        dataset=dataset,
        factory=factory,
        model=args.model,
        base_work=base_work,
        samples=args.samples_iter0,
        temperatures=temps,
        issue_ids=issue_ids,
    )
    history: list[V8IterStats] = [V8IterStats(
        iteration=0,
        submitted_pass=sum(1 for s in state.values() if s.best_passed),
        best_pass=sum(1 for s in state.values() if s.best_passed),
        total=len(state),
    )]
    logger.info("iter 0 best-of-%d: %d/%d passed", args.samples_iter0,
                history[0].best_pass, history[0].total)

    used_alternative: set[str] = set()

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
        attempted_via_alt: list[str] = []

        for iid, s in state.items():
            if s.best_passed:
                candidates[iid] = s.best_response
                continue
            if not s.locked_header:
                candidates[iid] = s.best_response
                continue

            review_payload = _ask_reviewer_multi(
                client,
                prompt_text=s.prompt_text,
                testbench=s.testbench_text,
                candidate=s.last_attempt or s.best_response,
                log_excerpt=s.last_log_excerpt,
                failure_class=s.failure_class,
            )
            s.last_diagnosis = review_payload
            (diag_dir / f"{iid}_iter{it}.json").write_text(
                json.dumps(review_payload, indent=2), encoding="utf-8",
            )

            primary = review_payload.get("primary", {}) or {}
            alternative = review_payload.get("alternative", {}) or {}
            p_conf = float(primary.get("confidence") or 0.0)
            a_conf = float(alternative.get("confidence") or 0.0)
            logger.info(
                "%s: primary conf=%.2f root='%s' | alt conf=%.2f root='%s'",
                iid, p_conf, str(primary.get("root_cause", ""))[:60],
                a_conf, str(alternative.get("root_cause", ""))[:60],
            )

            if p_conf < args.min_confidence and a_conf < args.min_confidence:
                logger.info("%s: SKIP — both hypotheses < %.2f", iid, args.min_confidence)
                skipped_low_conf.append(iid)
                candidates[iid] = s.best_response
                continue

            best_text: str | None = None
            scores_p: list[CandidateScore] = []
            scores_a: list[CandidateScore] = []

            if p_conf >= args.min_confidence:
                best_text, scores_p = _modifier_best_of_m_v8(
                    client,
                    locked_header=s.locked_header,
                    prompt_text=s.prompt_text,
                    testbench=s.testbench_text,
                    candidate_seed=s.last_attempt or s.best_response,
                    diagnosis=primary,
                    base_temp=args.modifier_temperature,
                    m_samples=args.modifier_samples,
                    inner_repair=args.inner_repair,
                    do_parse_check=args.parse_check,
                )

            if (best_text is None
                    and a_conf >= args.min_confidence
                    and iid not in used_alternative):
                logger.info("%s: primary best-of-M all rejected; trying ALTERNATIVE", iid)
                used_alternative.add(iid)
                attempted_via_alt.append(iid)
                best_text, scores_a = _modifier_best_of_m_v8(
                    client,
                    locked_header=s.locked_header,
                    prompt_text=s.prompt_text,
                    testbench=s.testbench_text,
                    candidate_seed=s.last_attempt or s.best_response,
                    diagnosis=alternative,
                    base_temp=args.modifier_temperature,
                    m_samples=args.modifier_samples,
                    inner_repair=args.inner_repair,
                    do_parse_check=args.parse_check,
                )

            (score_dir / f"{iid}_iter{it}.json").write_text(
                json.dumps({
                    "primary_scores": [sc.__dict__ for sc in scores_p],
                    "alternative_scores": [sc.__dict__ for sc in scores_a],
                }, default=str, indent=2),
                encoding="utf-8",
            )

            if best_text is None:
                logger.info("%s: REJECT — no submittable candidate (both angles)", iid)
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
        fixed_via_alt: list[str] = []
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
                if iid in attempted_via_alt:
                    fixed_via_alt.append(iid)
            elif not passed_now:
                s.last_log_excerpt = _extract_log_excerpt(iter_dir, iid)
                s.failure_class = _classify_failure(s.last_log_excerpt)

        best_pass = sum(1 for s in state.values() if s.best_passed)
        history.append(V8IterStats(
            iteration=it,
            submitted_pass=submitted_pass,
            best_pass=best_pass,
            total=len(state),
            fixed_now=fixed_now,
            fixed_via_alternative=fixed_via_alt,
            rejected_all=rejected_all,
            skipped_low_conf=skipped_low_conf,
        ))
        logger.info(
            "iter %d: best=%d/%d, fixed=%s (alt=%d), rej=%d, low_conf=%d",
            it, best_pass, len(state),
            fixed_now or "[]",
            len(fixed_via_alt), len(rejected_all), len(skipped_low_conf),
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
                    "fixed_via_alternative": h.fixed_via_alternative,
                    "rejected_all": h.rejected_all,
                    "skipped_low_confidence": h.skipped_low_conf,
                }
                for h in history
            ],
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info(
        "FINAL best pass rate: %d/%d (%.1f%%)",
        history[-1].best_pass, history[-1].total,
        100 * history[-1].best_pass / history[-1].total,
    )


def _parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Same model as baseline + modifier inner self-repair")
    p.add_argument("-f", "--dataset", required=True)
    p.add_argument("--workdir", default="work_evolve_v8")
    p.add_argument("--iterations", type=int, default=2)
    p.add_argument("--target", type=float, default=1.0)
    p.add_argument("--model", default="qwen3", help="single model used for executor / reviewer / modifier")
    p.add_argument("--samples-iter0", type=int, default=2)
    p.add_argument("--temperatures", type=str, default="")
    p.add_argument("--modifier-temperature", type=float, default=0.25)
    p.add_argument("--modifier-samples", type=int, default=3)
    p.add_argument("--inner-repair", type=int, default=2,
                   help="number of self-repair turns the modifier can use to fix syntax errors")
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
        evolve_v8(args)
    finally:
        logger.info("evolve_v8 total runtime: %.1fs", time.time() - started)


if __name__ == "__main__":
    main()
