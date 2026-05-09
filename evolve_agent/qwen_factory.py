# SPDX-FileCopyrightText: Copyright (c) 2026 Evolve-Agent contributors
# SPDX-License-Identifier: Apache-2.0

"""
Qwen3 model factory for the CVDP benchmark.

Registers two model families:

- ``qwen3-*`` / ``qwen-*``: vanilla single-shot Qwen3 model that talks to any
  OpenAI-compatible endpoint (Alibaba DashScope, vLLM, Ollama, SGLang, ...).

- ``qwen3-evolve``: same backing model but each ``prompt()`` call internally
  runs a plan -> generate -> self-critique -> repair pipeline before returning
  the final completion.  Drop-in replacement for non-agentic runs that lifts
  pass-rate without changing the harness.

Endpoint and model id are fully driven by environment variables so you can
swap between cloud DashScope and a local vLLM server with no code change.

Required env (one of):
    QWEN_API_KEY        DashScope / OpenAI-compatible API key
    DASHSCOPE_API_KEY   alias accepted by the Alibaba SDK

Optional env:
    QWEN_BASE_URL       default https://dashscope-intl.aliyuncs.com/compatible-mode/v1
    QWEN_MODEL          default qwen3-max
    QWEN_TEMPERATURE    default 0.2
    QWEN_MAX_TOKENS     default 4096
    QWEN_EVOLVE_PASSES  default 2  (extra critic+repair rounds in -evolve mode)
"""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any, Optional

# Make sibling files importable regardless of cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.append(_HERE)
# CVDP repo root, so ``from src.*`` resolves when ``-c`` loads this file.
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)

import openai  # noqa: E402

from src.config_manager import config  # noqa: E402
from src.llm_lib.model_factory import ModelFactory  # noqa: E402
from src.llm_lib.openai_llm import OpenAI_Instance  # noqa: E402

logger = logging.getLogger(__name__)


DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen3-max"


def _resolve_api_key(explicit_key: Optional[str]) -> str:
    if explicit_key:
        return explicit_key
    for var in ("QWEN_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_USER_KEY"):
        val = config.get(var) or os.environ.get(var)
        if val:
            return val
    raise ValueError(
        "Qwen factory: no API key found. Set QWEN_API_KEY (or DASHSCOPE_API_KEY)."
    )


def _resolve_base_url() -> str:
    return (
        config.get("QWEN_BASE_URL")
        or os.environ.get("QWEN_BASE_URL")
        or DEFAULT_BASE_URL
    )


def _resolve_model_id(model_name: Optional[str]) -> str:
    """Resolve the real upstream Qwen model id.

    ``QWEN_MODEL`` env var, when set, overrides every alias so that one
    knob in ``.env`` retargets all registered qwen ids. When unset, the
    factory key is used as-is (so ``-m qwen3-max`` works literally).
    """
    env_override = config.get("QWEN_MODEL") or os.environ.get("QWEN_MODEL")
    if env_override:
        return env_override
    if not model_name:
        return DEFAULT_MODEL
    return model_name


class Qwen_Instance(OpenAI_Instance):
    """OpenAI-compatible Qwen client.

    Inherits the full prompt() / debug() / schema-handling pipeline from
    OpenAI_Instance and only overrides the constructor so we point the SDK at
    the Qwen endpoint with the right key.
    """

    def __init__(
        self,
        context: str = "You are an expert RTL design and verification engineer.",
        key: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        api_key = _resolve_api_key(key)
        base_url = _resolve_base_url()
        resolved_model = _resolve_model_id(model or DEFAULT_MODEL)

        # Skip OpenAI_Instance.__init__ entirely so we can set base_url cleanly.
        self.context = context
        self.model = resolved_model
        self.debug = False
        self.chat = openai.OpenAI(api_key=api_key, base_url=base_url)
        logger.info(
            "Qwen_Instance ready: model=%s base_url=%s", resolved_model, base_url
        )


# Verilog/SystemVerilog code-fence pattern for extracting clean RTL from
# free-form model output.
_CODE_FENCE = re.compile(
    r"```(?:systemverilog|verilog|sv|v)?\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)


def _strip_fences(text: str) -> str:
    matches = _CODE_FENCE.findall(text)
    if matches:
        return matches[-1].strip()
    return text.strip()


class Qwen_Evolutionary_Instance(Qwen_Instance):
    """Qwen client that self-iterates plan -> generate -> critique -> repair.

    Same external API (``prompt()``) as a vanilla model, so it slots into the
    benchmark harness with no other change.  Internally it issues additional
    chat completions to push pass-rate up.
    """

    PLAN_SYS = (
        "You are a senior Verilog/SystemVerilog architect. "
        "Given an RTL design or verification problem, produce a short, "
        "numbered implementation plan. Keep it under 12 bullets. "
        "Highlight: I/O contract, clocking/reset assumptions, FSM states, "
        "edge cases (overflow, X-propagation), and lint pitfalls."
    )

    CRITIC_SYS = (
        "You are a strict RTL code reviewer. Read the candidate solution and "
        "list every concrete defect: syntax errors, missing signals, wrong "
        "widths, latches, sensitivity-list issues, reset polarity, off-by-one, "
        "unhandled edge cases, and any deviation from the spec. "
        "If the code looks correct, reply with the single token: OK."
    )

    REPAIR_SYS = (
        "You are an expert RTL engineer fixing a candidate solution. "
        "Apply every issue from the review. Return the FULL corrected file "
        "only, inside one ```systemverilog code block, with no commentary."
    )

    def __init__(
        self,
        context: str = "You are an expert RTL design and verification engineer.",
        key: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        super().__init__(context=context, key=key, model=model)
        try:
            self.passes = int(
                config.get("QWEN_EVOLVE_PASSES")
                or os.environ.get("QWEN_EVOLVE_PASSES")
                or 2
            )
        except (TypeError, ValueError):
            self.passes = 2
        self.passes = max(0, min(self.passes, 5))
        logger.info("Qwen_Evolutionary_Instance: %d critic/repair passes", self.passes)

    def _chat(self, system: str, user: str, timeout: int = 60) -> str:
        try:
            resp = self.chat.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=float(
                    config.get("QWEN_TEMPERATURE")
                    or os.environ.get("QWEN_TEMPERATURE")
                    or 0.2
                ),
                timeout=timeout,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001 -- best-effort path, fall back below
            logger.warning("Qwen evolutionary sub-call failed: %s", exc)
            return ""

    def prompt(  # type: ignore[override]
        self,
        prompt: str,
        schema: Optional[str] = None,
        prompt_log: str = "",
        files: Optional[list] = None,
        timeout: int = 60,
        category: Optional[int] = None,
    ) -> Any:
        # If the harness expects a JSON schema or multi-file output, defer to
        # the parent path so we do not break structured response parsing.
        if schema is not None or (files and len(files) != 1):
            return super().prompt(
                prompt=prompt,
                schema=schema,
                prompt_log=prompt_log,
                files=files,
                timeout=timeout,
                category=category,
            )

        plan = self._chat(self.PLAN_SYS, prompt, timeout=timeout) or "(no plan)"

        draft_user = (
            f"Implementation plan:\n{plan}\n\n"
            f"Problem:\n{prompt}\n\n"
            "Now write the complete solution. Return ONLY the file content "
            "inside one ```systemverilog code block."
        )
        draft = super().prompt(
            prompt=draft_user,
            schema=None,
            prompt_log=prompt_log,
            files=files,
            timeout=timeout,
            category=category,
        )
        if not isinstance(draft, str):
            return draft  # parent returned a structured response, respect it
        candidate = _strip_fences(draft)

        for round_idx in range(self.passes):
            review = self._chat(
                self.CRITIC_SYS,
                f"Spec:\n{prompt}\n\nCandidate:\n```systemverilog\n{candidate}\n```",
                timeout=timeout,
            )
            if not review or review.strip().upper().startswith("OK"):
                break
            repair_user = (
                f"Spec:\n{prompt}\n\n"
                f"Issues found in review:\n{review}\n\n"
                f"Current candidate:\n```systemverilog\n{candidate}\n```\n\n"
                "Return the corrected full file only."
            )
            fixed = self._chat(self.REPAIR_SYS, repair_user, timeout=timeout)
            if fixed:
                candidate = _strip_fences(fixed)
            logger.info("evolve pass %d/%d applied", round_idx + 1, self.passes)

        # Honour single-file direct-text mode the harness uses when len(files)==1.
        return candidate


class CustomModelFactory(ModelFactory):
    """Registers Qwen models on top of the stock factory."""

    def __init__(self) -> None:
        super().__init__()

        qwen_ids = (
            "qwen",
            "qwen3",
            "qwen3-max",
            "qwen3-coder-plus",
            "qwen3-coder",
            "qwen3-235b-a22b",
            "qwen3-32b",
            "qwen-max",
            "qwen-plus",
            "qwen-turbo",
        )
        for key in qwen_ids:
            self.model_types[key] = self._create_qwen_instance

        self.model_types["qwen3-evolve"] = self._create_qwen_evolve_instance
        self.model_types["qwen-evolve"] = self._create_qwen_evolve_instance

        logger.info(
            "CustomModelFactory: registered %d Qwen ids + qwen3-evolve",
            len(qwen_ids),
        )

    def _create_qwen_instance(
        self,
        model_name: str,
        context: Any,
        key: Optional[str],
        **_: Any,
    ) -> Qwen_Instance:
        return Qwen_Instance(context=context, key=key, model=model_name)

    def _create_qwen_evolve_instance(
        self,
        model_name: str,
        context: Any,
        key: Optional[str],
        **_: Any,
    ) -> Qwen_Evolutionary_Instance:
        return Qwen_Evolutionary_Instance(context=context, key=key, model=model_name)


if __name__ == "__main__":
    # Smoke test: only runs if QWEN_API_KEY is set in env.
    logging.basicConfig(level=logging.INFO)
    factory = CustomModelFactory()
    model = factory.create_model(model_name="qwen3", context="You are a helpful assistant.")
    print("model:", model.model, "base_url:", model.chat.base_url)
