#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Evolve-Agent contributors
# SPDX-License-Identifier: Apache-2.0

"""evolve_v7: v6 + multi-hypothesis reviewer + test-aware modifier.

What v6 left on the table: when the modifier's best-of-M failed for a
problem, the iteration ended with that problem unfixed even though the
real bug might be a different angle than the reviewer's primary
hypothesis. v6 only knew one hypothesis per iter.

v7 closes that loop with two changes:

  1. Multi-hypothesis reviewer. The reviewer now emits two ranked
     hypotheses ``primary`` + ``alternative`` (must be a *different*
     angle, not a rephrased one). Per failure, we try modifier best-of-M
     against the primary. If all M candidates fail the static guards, we
     retry best-of-M against the alternative before giving up.

  2. Test-aware modifier. The modifier prompt now includes the cocotb
     testbench excerpt the reviewer was already seeing. This lets the
     modifier pattern-match what the testbench expects (e.g. polarity,
     ready/valid handshake, register widths) rather than reasoning from
     the spec alone. Cost: bigger modifier prompt; benefit: fewer
     candidates rejected for "didn't actually fix the issue".

Otherwise inherits everything from v6: monotonic best-tracking, port-
lock, iverilog parse gate, faithfulness check, length sanity, adaptive
best-of-N at iter 0.
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import re
import shutil
import sys
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
    _read_jsonl,
    _run_local_import,
    _write_jsonl,
)
from evolve_v4 import (  # type: ignore  # noqa: E402
    ProblemState,
    _build_client,
    _extract_testbench,
)
from evolve_v5 import _adaptive_best_of_n  # type: ignore  # noqa: E402
from evolve_v6 import (  # type: ignore  # noqa: E402
    CandidateScore,
    _score_candidate,
)

logger = logging.getLogger("evolve_v7")


# ---------------------------------------------------------------------------
# Multi-hypothesis reviewer
# ---------------------------------------------------------------------------


_MULTI_REVIEWER_SYSTEM = """\
You are a cold, analytical RTL reviewer. You are given:
  - the design SPEC,
  - the cocotb TESTBENCH source,
  - the failing CANDIDATE RTL,
  - the truncated HARNESS LOG.

Produce TWO ranked hypotheses for the failure, each from a *different
angle* (different bug class, different signal cluster, or different
abstraction level). The alternative must not be a rephrased primary.

Respond with ONLY a JSON object, no surrounding prose, no fences:

{
  "primary": {
    "root_cause": "<one sentence>",
    "evidence": ["<short verbatim quote>", "..."],
    "affected_signals": ["<signal>", "..."],
    "proposed_fix": "<two-sentence description, no RTL code>",
    "confidence": <0.0-1.0>
  },
  "alternative": {
    "root_cause": "<one sentence — different angle>",
    "evidence": ["..."],
    "affected_signals": ["..."],
    "proposed_fix": "...",
    "confidence": <0.0-1.0>
  }
}

If you cannot find a credible alternative, set alternative.confidence < 0.2.
Be terse. Quote evidence verbatim.
"""


def _ask_reviewer_multi(client, *, prompt_text: str, testbench: str,
                        candidate: str, log_excerpt: str,
                        failure_class: str) -> dict:
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
                {"role": "system", "content": _MULTI_REVIEWER_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            timeout=180,
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("reviewer call failed: %s", exc)
        return {"primary": {"confidence": 0.0}, "alternative": {"confidence": 0.0},
                "_error": str(exc)}
    return _parse_multi_reviewer_json(text)


def _parse_multi_reviewer_json(text: str) -> dict:
    if not text:
        return {"primary": {"confidence": 0.0}, "alternative": {"confidence": 0.0}}
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    raw = fence.group(1) if fence else text
    start = raw.find("{")
    if start == -1:
        return {"primary": {"confidence": 0.0}, "alternative": {"confidence": 0.0},
                "_raw": text[:400]}
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
                    return {"primary": {"confidence": 0.0},
                            "alternative": {"confidence": 0.0},
                            "_raw": text[:400]}
                if not isinstance(payload, dict):
                    return {"primary": {"confidence": 0.0},
                            "alternative": {"confidence": 0.0}}
                # Backwards-compat: accept v4-style flat schema, promote
                # to ``primary`` and synthesize an empty alternative.
                if "primary" not in payload and "root_cause" in payload:
                    return {
                        "primary": payload,
                        "alternative": {"confidence": 0.0},
                    }
                payload.setdefault("primary", {"confidence": 0.0})
                payload.setdefault("alternative", {"confidence": 0.0})
                payload["primary"].setdefault("confidence", 0.0)
                payload["alternative"].setdefault("confidence", 0.0)
                return payload
    return {"primary": {"confidence": 0.0}, "alternative": {"confidence": 0.0},
            "_raw": text[:400]}


# ---------------------------------------------------------------------------
# Test-aware modifier
# ---------------------------------------------------------------------------


_MODIFIER_TEMPLATE = """\
You modify a SystemVerilog file to apply a SPECIFIC diagnosis.
Constraints:

1. Output a single complete file inside one ```systemverilog code block.
   No commentary, no preamble.
2. The file MUST begin with this EXACT module header — same name, same
   ports, same widths, same direction. Do not rename, add, drop, or
   reorder any port:

```
{locked_header}
```

3. Apply ONLY the fix described in the diagnosis. Preserve all unrelated
   logic verbatim from the previous candidate.
4. Do not introduce new modules or files.

You also have access to the cocotb testbench so you know the expected
signal behavior. Use it to verify your fix matches what the test pokes
and asserts on, but do not copy testbench code.
"""


def _ask_modifier_test_aware(
    client,
    *,
    locked_header: str,
    prompt_text: str,
    testbench: str,
    candidate: str,
    diagnosis: dict,
    temperature: float,
    timeout: int = 180,
) -> str:
    system = _MODIFIER_TEMPLATE.format(locked_header=locked_header)
    diag_for_prompt = {k: v for k, v in diagnosis.items() if not k.startswith("_")}
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


# ---------------------------------------------------------------------------
# Per-hypothesis modifier best-of-M
# ---------------------------------------------------------------------------


def _modifier_best_of_m_test_aware(
    client,
    *,
    locked_header: str,
    prompt_text: str,
    testbench: str,
    candidate_seed: str,
    diagnosis: dict,
    base_temp: float,
    m_samples: int,
    do_parse_check: bool,
) -> tuple[str | None, list[CandidateScore]]:
    affected = diagnosis.get("affected_signals") or []
    if not isinstance(affected, list):
        affected = []
    temps = [round(min(0.85, max(0.1, base_temp + 0.15 * i)), 2)
             for i in range(m_samples)]
    scores: list[CandidateScore] = []
    for k, temp in enumerate(temps):
        text = _ask_modifier_test_aware(
            client,
            locked_header=locked_header,
            prompt_text=prompt_text,
            testbench=testbench,
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
            "    modifier sample %d (T=%.2f): score=%.1f reasons=%s",
            k, temp, sc.total, sc.reasons or [],
        )
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
class V7IterStats:
    iteration: int
    submitted_pass: int
    best_pass: int
    total: int
    fixed_now: list[str] = field(default_factory=list)
    fixed_via_alternative: list[str] = field(default_factory=list)
    rejected_all: list[str] = field(default_factory=list)
    skipped_low_conf: list[str] = field(default_factory=list)


def evolve_v7(args: argparse.Namespace) -> None:
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
    history: list[V7IterStats] = [V7IterStats(
        iteration=0,
        submitted_pass=sum(1 for s in state.values() if s.best_passed),
        best_pass=sum(1 for s in state.values() if s.best_passed),
        total=len(state),
    )]
    logger.info("iter 0 best-of-%d: %d/%d passed", args.samples_iter0,
                history[0].best_pass, history[0].total)

    # Track which issues have been fed which alternative angle so we
    # don't re-try the same one.
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
                logger.info("%s: SKIP — both hypotheses below %.2f",
                            iid, args.min_confidence)
                skipped_low_conf.append(iid)
                candidates[iid] = s.best_response
                continue

            best_text: str | None = None
            scores_p: list[CandidateScore] = []
            scores_a: list[CandidateScore] = []

            # ---- Try primary first ----
            if p_conf >= args.min_confidence:
                best_text, scores_p = _modifier_best_of_m_test_aware(
                    client,
                    locked_header=s.locked_header,
                    prompt_text=s.prompt_text,
                    testbench=s.testbench_text,
                    candidate_seed=s.last_attempt or s.best_response,
                    diagnosis=primary,
                    base_temp=args.modifier_temperature,
                    m_samples=args.modifier_samples,
                    do_parse_check=args.parse_check,
                )

            # ---- Fallback to alternative ----
            if (best_text is None
                    and a_conf >= args.min_confidence
                    and iid not in used_alternative):
                logger.info("%s: primary best-of-M all rejected; trying ALTERNATIVE",
                            iid)
                used_alternative.add(iid)
                attempted_via_alt.append(iid)
                best_text, scores_a = _modifier_best_of_m_test_aware(
                    client,
                    locked_header=s.locked_header,
                    prompt_text=s.prompt_text,
                    testbench=s.testbench_text,
                    candidate_seed=s.last_attempt or s.best_response,
                    diagnosis=alternative,
                    base_temp=args.modifier_temperature,
                    m_samples=args.modifier_samples,
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
        history.append(V7IterStats(
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
    p = argparse.ArgumentParser(description="Multi-hypothesis + test-aware CVDP evolution")
    p.add_argument("-f", "--dataset", required=True)
    p.add_argument("--workdir", default="work_evolve_v7")
    p.add_argument("--iterations", type=int, default=2)
    p.add_argument("--target", type=float, default=1.0)
    p.add_argument("--model", default="qwen3")
    p.add_argument("--samples-iter0", type=int, default=2)
    p.add_argument("--temperatures", type=str, default="")
    p.add_argument("--modifier-temperature", type=float, default=0.25)
    p.add_argument("--modifier-samples", type=int, default=3)
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
        evolve_v7(args)
    finally:
        logger.info("evolve_v7 total runtime: %.1fs", time.time() - started)


if __name__ == "__main__":
    main()
