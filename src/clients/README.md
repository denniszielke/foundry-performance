# Benchmark clients

Protocol clients that measure the weather agents. Each client speaks one
transport; the runner drives them and reports a comparison table.

## Protocols

| name             | transport            | endpoint                       | streams (TTFB) |
|------------------|----------------------|--------------------------------|----------------|
| `responses`      | HTTP + SSE           | `POST /responses`              | yes            |
| `invocations`    | HTTP                 | `POST /invocations`            | no             |
| `invocations_ws` | WebSocket            | `/invocations_ws`              | yes            |
| `a2a`            | HTTP (JSON-RPC)      | `POST /a2a` or `/a2a/{assistant_id}` | no          |

## Metrics

Every run reports, per protocol and per phase:

- **mean / p50 / p95 latency** — total round-trip time.
- **TTFB** — time to first streamed token (streaming protocols only).

Phases answer the questions from the scenario:

- **cold** — the first request against a freshly started agent (pays process +
  MCP tool startup — i.e. "start up time for new agents").
- **warm** — steady-state requests, each on a brand-new session ("first request").
- **followup** — a second turn that reuses a primed session ("follow up request
  on existing sessions").

## Usage

```bash
# Custom MAF agent (Container App) — base URL + auth derived from .env
python -m src.clients.run_benchmark \
  --agent custom-maf \
  --protocols all \
  --model-hosting openai

# A subset, more iterations, save raw results
python -m src.clients.run_benchmark \
  --base-url http://127.0.0.1:8088 \
  --protocols responses,invocations_ws \
  --model-hosting foundry \
  --iterations 20 \
  --out results.json
```

`--model-hosting {foundry,openai}` is mandatory. Every run writes a timestamped
JSON artifact under `results/` unless `--out` overrides the path. Its `results`
array provides comparison-ready rows by protocol and phase, while `turns`
retains the raw measurements and errors.

Generate a self-contained interactive dashboard from every benchmark artifact:

```bash
python -m scripts.generate_benchmark_dashboard
```

The default output is `results/benchmark-dashboard.html`. Use `--results-dir`,
`--pattern`, or `--out` to select another source folder, filename glob, or
destination. The generated page embeds the current results and can also import
additional benchmark JSON files through its file picker or drag and drop.

`--agent {prompt,hosted-responses,hosted-invocations,custom-maf,custom-langchain}` derives the base
URL(s) and the right `--auth` mode from `.env` (loaded automatically) — see the
main [README](../../README.md#run-the-benchmark) for the full set of commands
per variation. Use `--base-url` directly to bypass `.env` (e.g. a local
process). Install deps with `pip install -r src/clients/requirements.txt`.
