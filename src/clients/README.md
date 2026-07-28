# Benchmark clients

Protocol clients that measure the weather agents. Each client speaks one
transport; the runner drives them and reports a comparison table.

## Protocols

| name             | transport            | endpoint                       | streams (TTFB) |
|------------------|----------------------|--------------------------------|----------------|
| `responses`      | HTTP + SSE           | `POST /responses`              | yes            |
| `invocations`    | HTTP                 | `POST /invocations`            | no             |
| `invocations_ws` | WebSocket            | `/invocations_ws`              | yes            |
| `a2a`            | HTTP (JSON-RPC)      | `POST /a2a`                    | no             |
| `activity`       | HTTP                 | `POST /activity/messages`      | no             |

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
# Custom agent (Container App) — base URL + auth derived from .env
python -m src.clients.run_benchmark --agent custom --protocols all

# A subset, more iterations, save raw results
python -m src.clients.run_benchmark \
  --base-url http://127.0.0.1:8088 \
  --protocols responses,invocations_ws \
  --iterations 20 \
  --out results.json
```

`--agent {prompt,hosted-responses,hosted-invocations,custom}` derives the base
URL(s) and the right `--auth` mode from `.env` (loaded automatically) — see the
main [README](../../README.md#run-the-benchmark) for the full set of commands
per variation. Use `--base-url` directly to bypass `.env` (e.g. a local
process). Install deps with `pip install -r src/clients/requirements.txt`.
