#!/usr/bin/env bash
# The ninety-second demo: healthy traffic, a provider breaks, the breaker trips,
# traffic reroutes, the provider heals and the breaker closes on its own.
# Start the stack first:  docker compose --profile demo up -d   (Grafana on :3000)
set -euo pipefail
GW=${GW:-http://localhost:8000}
AUTH="Authorization: Bearer ${GATEWAY_ADMIN_TOKEN:-change-me}"
say() { printf '\n== %s\n' "$*"; }

say "healthy for 20 s"; sleep 20
say "alpha starts returning 503s for 40 s"
curl -s -X POST "$GW/admin/chaos/alpha" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"error_rate": 1.0, "error_kind": "server_error", "duration_s": 40}'; echo
sleep 45
say "alpha is back; watch it go half open, then closed"; sleep 25
say "alpha turns slow (+6 s) for 40 s: hedging answers first, then the breaker trips on p95"
curl -s -X POST "$GW/admin/chaos/alpha" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"extra_latency_ms": 6000, "duration_s": 40}'; echo
sleep 45
say "breaker events"; curl -s "$GW/admin/events" -H "$AUTH"; echo
