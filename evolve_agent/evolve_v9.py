#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Evolve-Agent contributors
# SPDX-License-Identifier: Apache-2.0

"""evolve_v9: v8 + header normalizer + bug-class classifier agent.

Two additions over v8, observed from the cid003-20 v8 run on 2026-05-08:

1. **Header normalizer** (deterministic, no LLM)

   v8 log showed roughly half of modifier samples rejected with
   ``header_changed``. The model was making correct body-level fixes but
   also mutating the locked module header (port types, signedness,
   ``output reg`` ↔ ``output logic``). Since the locked header comes from
   the spec and the testbench expects exactly that interface, any
   header rewrite is wrong by definition. Instead of relying on prompt
   compliance, v9 splices the locked header back into every modifier
   output before parse-check.

2. **Bug-class classifier agent** + class-specific modifier prompts

   v8 used one modifier system prompt for every bug, but the failure
   classes are very different: Icarus -g2012 syntax limits, FSM
   transitions, and bit-width/signed arithmetic each have their own
   common pitfalls. v9 inserts a cheap classifier call (T=0, ~80
   tokens) right after the reviewer, then routes to one of four
   modifier system prompts (syntax / fsm / width / other) with the
   class-specific reminders inlined.

Same model used for executor / reviewer / classifier / modifier — the
"apples-to-apples" anchor is preserved.
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
    _MODULE_RE,
    _classify_failure,
    _did_pass,
    _extract_code,
    _extract_log_excerpt,
    _extract_module_header,
    _extract_prompt_text,
    _extract_submitted_response,
    _headers_match,
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
    _MODIFIER_TEMPLATE,
    _MULTI_REVIEWER_SYSTEM,
    _ask_reviewer_multi,
)
from evolve_v8 import _iverilog_parse_with_stderr  # type: ignore  # noqa: E402

logger = logging.getLogger("evolve_v9")


# ---------------------------------------------------------------------------
# Component 1: Header normalizer (deterministic)
# ---------------------------------------------------------------------------


def _force_locked_header(text: str, locked_header: str) -> str:
    """Splice ``locked_header`` over the candidate's module header.

    Returns text unchanged when either operand is empty or no ``module ... ;``
    opener is detected. The candidate's body (everything after the matched
    ``);``) is preserved verbatim — only the opener is rewritten.
    """
    if not text or not locked_header:
        return text
    match = _MODULE_RE.search(text)
    if not match:
        return text
    if _headers_match(match.group(0), locked_header):
        return text  # already correct
    return text[: match.start()] + locked_header + text[match.end():]


# ---------------------------------------------------------------------------
# Component 2: Bug-class classifier agent
# ---------------------------------------------------------------------------


_CLASSIFIER_SYSTEM = (
    "You classify a hardware-RTL bug into ONE of these classes:\n\n"
    "  syntax  - language errors, Icarus Verilog -g2012 unsupported constructs,\n"
    "            compile/elaboration failures, type/declaration mismatches,\n"
    "            undeclared identifiers, mixing output reg vs output logic\n"
    "  fsm     - state transition / handshake / sequencing / control-flow bugs,\n"
    "            ready-valid backpressure, missing reset, combinational loops\n"
    "  width   - bit-width truncation, signed/unsigned mismatches, off-by-one\n"
    "            arithmetic, missing sign-extension, wrong shift type\n"
    "  other   - anything that doesn't fit the above (off-spec mapping, missing\n"
    "            features, incorrect formula, etc.)\n\n"
    "Respond with ONLY a JSON object, no fences, no prose:\n"
    '{"class": "syntax|fsm|width|other", "reason": "<one short sentence>"}'
)


_CLASS_VALUES = ("syntax", "fsm", "width", "other")


def _classify_bug(
    client,
    *,
    diagnosis: dict,
    log_excerpt: str,
    timeout: int = 60,
) -> tuple[str, str]:
    """Run the classifier agent. Returns (class, reason).

    Falls back to ('other', '<error>') on any LLM/parse failure — the modifier
    just gets the generic prompt in that case.
    """
    diag_for_prompt = {k: v for k, v in (diagnosis or {}).items() if not k.startswith("_")}
    user = (
        "# Reviewer diagnosis\n"
        f"```json\n{json.dumps(diag_for_prompt, indent=2)}\n```\n\n"
        "# Harness log tail\n"
        f"```\n{(log_excerpt or '')[:1500]}\n```\n\n"
        "Classify."
    )
    try:
        resp = client.chat.chat.completions.create(
            model=client.model,
            messages=[
                {"role": "system", "content": _CLASSIFIER_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            timeout=timeout,
            max_tokens=120,
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("classifier call failed: %s", exc)
        return "other", f"classifier_error: {exc!s:.80}"

    obj_match = re.search(r"\{.*?\}", raw, re.DOTALL)
    if not obj_match:
        return "other", "classifier_no_json"
    try:
        obj = json.loads(obj_match.group(0))
    except json.JSONDecodeError:
        return "other", "classifier_bad_json"
    cls = str(obj.get("class", "other")).strip().lower()
    if cls not in _CLASS_VALUES:
        cls = "other"
    reason = str(obj.get("reason", ""))[:200]
    return cls, reason


# ---------------------------------------------------------------------------
# Component 3: Class-specific modifier prompts
# ---------------------------------------------------------------------------


_CLASS_GUIDANCE = {
    "syntax": (
        "\n\n# Icarus Verilog -g2012 reminders (this benchmark uses iverilog):\n"
        "- Dynamic part-selects on the LHS (e.g. `arr[i +: W] = ...`) compile\n"
        "  in some simulators but FAIL on iverilog. Use a `for` loop with a\n"
        "  fixed-width assignment per iteration instead.\n"
        "- `$clog2(PARAM)` is NOT allowed inside port width declarations on\n"
        "  iverilog. Compute the width into a `localparam` first, then use it.\n"
        "- Don't mix `output reg` and `output logic` for the same port; pick\n"
        "  one consistently with the locked header.\n"
        "- Every variable assigned in `always_comb` must be assigned on every\n"
        "  path (or have a default at the top of the block).\n"
        "- Avoid `var` with unpacked array types — unsupported on iverilog.\n"
    ),
    "fsm": (
        "\n\n# FSM / handshake checklist:\n"
        "- Make `case` statements exhaustive — add `default:` even if you\n"
        "  think it's unreachable.\n"
        "- For ready/valid handshakes, advance state ONLY when both sides\n"
        "  are asserted in the same cycle.\n"
        "- Reset must drive every flop to a defined value.\n"
        "- Don't read combinational outputs into next-state logic that feeds\n"
        "  back to the same combinational output (loop).\n"
        "- For backpressure (`temp`/`buf` flags), gate the consumer-side\n"
        "  write, not the producer-side state transition.\n"
    ),
    "width": (
        "\n\n# Bit-width / signedness checklist:\n"
        "- Sum of two W-bit signed values needs (W+1) bits to avoid overflow.\n"
        "- Average / interpolation: widen first, divide later.\n"
        "- Shifts on signed types: use `>>>` (arithmetic) not `>>` (logical)\n"
        "  when sign-preservation matters.\n"
        "- Mixing signed and unsigned in arithmetic implicitly converts to\n"
        "  unsigned — wrap with `$signed(...)` to keep the operation signed.\n"
        "- Truncation: an explicit cast (`OUT_WIDTH'(value)`) is safer than\n"
        "  relying on implicit narrowing.\n"
    ),
    "other": "",
}


def _modifier_system_for_class(bug_class: str, locked_header: str) -> str:
    base = _MODIFIER_TEMPLATE.format(locked_header=locked_header)
    return base + _CLASS_GUIDANCE.get(bug_class, "")


# ---------------------------------------------------------------------------
# Modifier (v9): same shape as v8 but threads bug_class through and applies
# header normalization before parse check.
# ---------------------------------------------------------------------------


def _ask_modifier_v9(
    client,
    *,
    locked_header: str,
    prompt_text: str,
    testbench: str,
    candidate: str,
    diagnosis: dict,
    bug_class: str,
    temperature: float,
    parse_error_feedback: str = "",
    timeout: int = 180,
) -> str:
    diag_for_prompt = {k: v for k, v in diagnosis.items() if not k.startswith("_")}
    if parse_error_feedback:
        system = (
            "You are fixing a SYNTAX/ELABORATION error in a candidate. "
            "Output a single complete file inside one ```systemverilog "
            "code block. Preserve every working part of the previous "
            "candidate verbatim; only change what's needed to make "
            "iverilog accept the file. Do NOT change the module header.\n\n"
            f"Required module header:\n```\n{locked_header}\n```\n"
            + _CLASS_GUIDANCE.get(bug_class, "")
        )
        user = (
            f"# iverilog error from previous candidate\n```\n{parse_error_feedback[:3000]}\n```\n\n"
            f"# Previous candidate (broken)\n```systemverilog\n{candidate}\n```\n\n"
            f"# Original diagnosis (for context)\n"
            f"```json\n{json.dumps(diag_for_prompt, indent=2)}\n```\n\n"
            "Return only the corrected file."
        )
    else:
        system = _modifier_system_for_class(bug_class, locked_header)
        user = (
            f"# Diagnosis (apply exactly this)\n"
            f"```json\n{json.dumps(diag_for_prompt, indent=2)}\n```\n\n"
            f"# Bug class: {bug_class}\n\n"
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


def _modifier_with_self_repair_v9(
    client,
    *,
    locked_header: str,
    prompt_text: str,
    testbench: str,
    candidate_seed: str,
    diagnosis: dict,
    bug_class: str,
    temperature: float,
    inner_repair: int,
    do_parse_check: bool,
) -> tuple[str, bool, int, bool]:
    """Returns (text, parse_ok, attempts_used, header_was_normalized)."""
    text = _ask_modifier_v9(
        client,
        locked_header=locked_header,
        prompt_text=prompt_text,
        testbench=testbench,
        candidate=candidate_seed,
        diagnosis=diagnosis,
        bug_class=bug_class,
        temperature=temperature,
    )
    if not text.strip():
        return "", False, 0, False

    # Header normalization happens BEFORE parse check / scoring. If the
    # candidate had a wrong header, splicing the locked one back in often
    # fixes parse and always fixes the scoring penalty.
    pre_norm_header = _extract_module_header(text)
    text = _force_locked_header(text, locked_header)
    header_was_normalized = bool(pre_norm_header) and not _headers_match(
        pre_norm_header, locked_header
    )

    if not do_parse_check or inner_repair <= 0:
        return text, True, 1, header_was_normalized

    attempts = 1
    for r in range(inner_repair):
        ok, err = _iverilog_parse_with_stderr(text)
        if ok or ok is None:
            return text, bool(ok), attempts, header_was_normalized
        # Header drift after normalization is impossible by construction,
        # so the remaining failure mode here is genuine body syntax.
        logger.info("    self-repair attempt %d: parse failed, feeding error back", r + 1)
        repaired = _ask_modifier_v9(
            client,
            locked_header=locked_header,
            prompt_text=prompt_text,
            testbench=testbench,
            candidate=text,
            diagnosis=diagnosis,
            bug_class=bug_class,
            temperature=max(0.1, temperature - 0.1),
            parse_error_feedback=err,
        )
        attempts += 1
        if not repaired.strip():
            break
        # Re-normalize after each repair attempt too — the model can drift
        # the header again while fixing syntax.
        text = _force_locked_header(repaired, locked_header)
    return text, False, attempts, header_was_normalized


def _modifier_best_of_m_v9(
    client,
    *,
    locked_header: str,
    prompt_text: str,
    testbench: str,
    candidate_seed: str,
    diagnosis: dict,
    bug_class: str,
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
        text, _, attempts, normed = _modifier_with_self_repair_v9(
            client,
            locked_header=locked_header,
            prompt_text=prompt_text,
            testbench=testbench,
            candidate_seed=candidate_seed,
            diagnosis=diagnosis,
            bug_class=bug_class,
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
        sc.reasons.append(f"bug_class={bug_class}")
        if normed:
            sc.reasons.append("header_normalized")
        scores.append(sc)
        logger.info(
            "    modifier sample %d (T=%.2f attempts=%d class=%s%s): score=%.1f reasons=%s",
            k, temp, attempts, bug_class, " norm" if normed else "",
            sc.total, sc.reasons,
        )
        if sc.total >= 10.0:
            break
    submittable = [sc for sc in scores if sc.is_submittable]
    if not submittable:
        return None, scores
    best = max(submittable, key=lambda sc: sc.total)
    return best.text, scores


# ---------------------------------------------------------------------------
# Driver — same shape as v8, with classifier insertion and bug_class threading
# ---------------------------------------------------------------------------


@dataclass
class V9IterStats:
    iteration: int
    submitted_pass: int
    best_pass: int
    total: int
    fixed_now: list[str] = field(default_factory=list)
    fixed_via_alternative: list[str] = field(default_factory=list)
    rejected_all: list[str] = field(default_factory=list)
    skipped_low_conf: list[str] = field(default_factory=list)
    bug_classes: dict[str, str] = field(default_factory=dict)


def _write_history(base_work: Path, history: list[V9IterStats]) -> None:
    """Atomic incremental write of evolution_history.json."""
    out = base_work / "evolution_history.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(
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
                    "bug_classes": h.bug_classes,
                }
                for h in history
            ],
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(tmp, out)


def evolve_v9(args: argparse.Namespace) -> None:
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
    classify_dir = base_work / "bug_classes"
    classify_dir.mkdir(exist_ok=True)

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
    history: list[V9IterStats] = [V9IterStats(
        iteration=0,
        submitted_pass=sum(1 for s in state.values() if s.best_passed),
        best_pass=sum(1 for s in state.values() if s.best_passed),
        total=len(state),
    )]
    logger.info("iter 0 best-of-%d: %d/%d passed", args.samples_iter0,
                history[0].best_pass, history[0].total)
    _write_history(base_work, history)

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
        bug_classes_iter: dict[str, str] = {}

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

            # NEW: classify bug class from the higher-confidence side. The
            # classifier only fires when at least one hypothesis is above
            # min_confidence (i.e. when modifier will actually run).
            bug_class, class_reason = _classify_bug(
                client,
                diagnosis=primary if p_conf >= a_conf else alternative,
                log_excerpt=s.last_log_excerpt,
            )
            (classify_dir / f"{iid}_iter{it}.json").write_text(
                json.dumps({"class": bug_class, "reason": class_reason},
                           indent=2),
                encoding="utf-8",
            )
            bug_classes_iter[iid] = bug_class
            logger.info("%s: classifier=%s (%s)", iid, bug_class, class_reason[:80])

            best_text: str | None = None
            scores_p: list[CandidateScore] = []
            scores_a: list[CandidateScore] = []

            if p_conf >= args.min_confidence:
                best_text, scores_p = _modifier_best_of_m_v9(
                    client,
                    locked_header=s.locked_header,
                    prompt_text=s.prompt_text,
                    testbench=s.testbench_text,
                    candidate_seed=s.last_attempt or s.best_response,
                    diagnosis=primary,
                    bug_class=bug_class,
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
                # Re-classify on the alternative diagnosis since the
                # signals/root_cause may differ enough to warrant a
                # different prompt variant.
                alt_class, alt_reason = _classify_bug(
                    client,
                    diagnosis=alternative,
                    log_excerpt=s.last_log_excerpt,
                )
                logger.info("%s: classifier(alt)=%s (%s)", iid, alt_class, alt_reason[:80])
                bug_classes_iter[iid] = f"{bug_class}->{alt_class}"
                best_text, scores_a = _modifier_best_of_m_v9(
                    client,
                    locked_header=s.locked_header,
                    prompt_text=s.prompt_text,
                    testbench=s.testbench_text,
                    candidate_seed=s.last_attempt or s.best_response,
                    diagnosis=alternative,
                    bug_class=alt_class,
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
        history.append(V9IterStats(
            iteration=it,
            submitted_pass=submitted_pass,
            best_pass=best_pass,
            total=len(state),
            fixed_now=fixed_now,
            fixed_via_alternative=fixed_via_alt,
            rejected_all=rejected_all,
            skipped_low_conf=skipped_low_conf,
            bug_classes=bug_classes_iter,
        ))
        logger.info(
            "iter %d: best=%d/%d, fixed=%s (alt=%d), rej=%d, low_conf=%d",
            it, best_pass, len(state),
            fixed_now or "[]",
            len(fixed_via_alt), len(rejected_all), len(skipped_low_conf),
        )
        _write_history(base_work, history)
        if best_pass / len(state) >= args.target:
            logger.info("hit target %.2f, stopping", args.target)
            break

    _write_jsonl(
        base_work / "best_responses.jsonl",
        ({"id": iid, "completion": state[iid].best_response} for iid in issue_ids),
    )
    _write_history(base_work, history)
    logger.info(
        "FINAL best pass rate: %d/%d (%.1f%%)",
        history[-1].best_pass, history[-1].total,
        100 * history[-1].best_pass / history[-1].total,
    )


def _parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="v8 + header normalizer + bug-class classifier")
    p.add_argument("-f", "--dataset", required=True)
    p.add_argument("--workdir", default="work_evolve_v9")
    p.add_argument("--iterations", type=int, default=2)
    p.add_argument("--target", type=float, default=1.0)
    p.add_argument("--model", default="qwen3", help="single model used for executor / reviewer / classifier / modifier")
    p.add_argument("--samples-iter0", type=int, default=2)
    p.add_argument("--temperatures", type=str, default="")
    p.add_argument("--modifier-temperature", type=float, default=0.25)
    p.add_argument("--modifier-samples", type=int, default=3)
    p.add_argument("--inner-repair", type=int, default=2)
    p.add_argument("--min-confidence", type=float, default=0.4)
    p.add_argument("--parse-check", action="store_true")
    p.add_argument("--fresh", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    evolve_v9(args)


if __name__ == "__main__":
    main()
