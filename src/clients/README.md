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
# All protocols against the custom agent (Container App)
python -m src.clients.run_benchmark \
  --base-url https://weather-custom-agent.<region>.azurecontainerapps.io

# A subset, more iterations, save raw results
python -m src.clients.run_benchmark \
  --base-url http://127.0.0.1:8088 \
  --protocols responses,invocations_ws \
  --iterations 20 \
  --out results.json
```

Point `--base-url` at any of the three agent variations to compare hosting
formats. Install deps with `pip install -r src/clients/requirements.txt`.
