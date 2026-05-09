# Evolve-Agent for CVDP

An agent flow that drives CVDP benchmark **pass rate up over iterations** using
a Qwen3 base model. Two layers of evolution:

- **Inner loop** (per problem): plan → generate → self-critique → repair, all
  inside a single model wrapper (`qwen3-evolve`).
- **Outer loop** (per iteration): run the harness, scrape failures + simulator
  logs, repair only the failing items with grounded error feedback, re-run.

```
                                    ┌───────────────────────────┐
                                    │ Iter N: refined responses │
                                    └────────────┬──────────────┘
   ┌─────────────────┐    fail     ┌────────────▼─────────────┐
   │ run_benchmark   │────────────▶│ scrape harness logs       │
   └─────────┬───────┘             └────────────┬─────────────┘
             │ pass                              │
             ▼                                   ▼
        keep result               ┌──────────────────────────┐
                                  │ Qwen3 repair w/ feedback │
                                  └──────────────┬───────────┘
                                                 │
                                                 ▼
                                       Iter N+1 responses.jsonl
```

## Files

| Path                              | Role                                                          |
| --------------------------------- | ------------------------------------------------------------- |
| `qwen_factory.py`                 | Custom `ModelFactory` that registers `qwen3*` and `qwen3-evolve` |
| `evolve.py`                       | Outer-loop orchestrator (runs harness, refines failures)      |
| `agent/agent.py`                  | Docker agent for agentic-mode datasets, with iverilog feedback |
| `agent/Dockerfile-base/-agent`    | Container build                                               |
| `agent/build_agent.sh`            | One-shot builder                                              |
| `.env.example`                    | All env vars in one place                                     |

## 1. Configure

```bash
cd /path/to/cvdp_benchmark
cp evolve_agent/.env.example .env
# edit .env, at minimum set QWEN_API_KEY
```

`QWEN_BASE_URL` defaults to Alibaba DashScope's OpenAI-compatible endpoint. To
hit a local vLLM/Ollama server instead, point it there — no other change.

## 2. Quick smoke test (non-agentic, single-shot Qwen)

```bash
./run_benchmark.py \
  -f example_dataset/cvdp_v1.1.0_example_nonagentic_code_generation_no_commercial.jsonl \
  --llm \
  --model qwen3 \
  --custom-factory evolve_agent/qwen_factory.py \
  --prefix work_qwen
```

Swap `--model qwen3` → `--model qwen3-evolve` to enable the inner critic+repair
loop on every problem (more tokens, higher pass-rate).

## 3. Full evolutionary run

```bash
python evolve_agent/evolve.py \
  -f example_dataset/cvdp_v1.1.0_example_nonagentic_code_generation_no_commercial.jsonl \
  --workdir work_evolve \
  --iterations 4 \
  --target 0.95
```

Per iteration you get `work_evolve/iter_<N>/` with:

- `responses.jsonl` (the candidate completions for that iteration)
- `raw_result.json` and `report.json` (harness output)
- log scrapes feed the next iteration's repair prompt.

A summary lands at `work_evolve/evolution_history.json`:

```json
[
  {"iteration": 0, "total": 36, "passed": 21, "pass_rate": 0.5833, "failed_ids": [...]},
  {"iteration": 1, "total": 36, "passed": 28, "pass_rate": 0.7778, "failed_ids": [...]},
  {"iteration": 2, "total": 36, "passed": 32, "pass_rate": 0.8889, "failed_ids": [...]}
]
```

The loop early-exits once `--target` is hit or no failures remain.

## 4. Agentic mode (Docker)

For agentic datasets:

```bash
cd evolve_agent/agent
./build_agent.sh                  # builds cvdp-evolve-agent
cd ../..

./run_benchmark.py \
  -f example_dataset/cvdp_v1.1.0_example_agentic_code_generation_no_commercial.jsonl \
  --agent cvdp-evolve-agent \
  --prefix work_evolve_agent
```

Make sure `QWEN_API_KEY`, `QWEN_BASE_URL`, `QWEN_MODEL` are present in your
shell env (they are forwarded into the agent container by the harness via
the standard env-pass mechanism — re-export them in `.env` if needed).

The agent runs plan → generate → iverilog/yosys lint → repair up to
`EVOLVE_MAX_PASSES` times before writing files into `/code/rtl`.

## 5. Where the pass-rate gain comes from

| Source                                 | Gain shape                              |
| -------------------------------------- | --------------------------------------- |
| Inner plan-then-generate               | catches spec misreads early             |
| Inner critic+repair (silent)           | removes obvious lint / FSM bugs         |
| Outer harness-log feedback             | grounds repair in real simulator output |
| Iteration N targets only failing ids   | cost grows only with the long tail      |
| Local lint inside Docker agent         | repairs syntax before it ever runs sim  |

## 6. Cost knobs

- `QWEN_EVOLVE_PASSES` — inner critic rounds (0 disables, 5 max).
- `EVOLVE_MAX_PASSES`  — agent-side lint/repair rounds.
- `--iterations`       — outer rounds.
- `--target`           — pass-rate at which we early-exit.
- Pick `qwen3-coder-plus` or a `qwen3-32b` local model when bulk runs eat into
  budget; reserve `qwen3-max` for the repair phase.

## 7. Caveats

- Outer loop currently re-evaluates the full dataset every iteration (the
  harness does not yet support id-filtered local-import). It is still cheaper
  than re-prompting because we only burn Qwen tokens on the failing items.
- Subjective-scored / commercial datasets need extra setup (Cadence, license
  net) — see upstream `README_FULL.md`. The orchestrator works once that's in
  place; no code changes needed.
- iverilog/yosys aren't always present in the cvdp-sim image — the agent
  silently skips local lint and falls back to "blind repair" if neither is
  installed. Add them in `Dockerfile-base` if you want the gain.
