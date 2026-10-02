# Running the LLM Gateway in production

This is the operations runbook for a real deployment. The repo-root `docker-compose.yml` is for
the demo and benchmark; for production use `deploy/docker-compose.prod.yml`, which adds restart
policies, resource limits, Redis persistence, secrets from a file, Prometheus alert rules and a
login-protected Grafana.

## 1. Deploy

```bash
git clone https://github.com/umer-78/umer-78-llm-gateway && cd umer-78-llm-gateway
cp deploy/.env.prod.example deploy/.env.prod     # then edit: admin token, Grafana password, provider keys
# edit config.yaml: list your providers, their models and prices, and the request classes
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.prod up -d --build
curl -s localhost:8000/health        # -> {"status":"ok"}
```

The gateway publishes only on `127.0.0.1:8000`. Put your own TLS-terminating reverse proxy
(Caddy, nginx, a cloud load balancer) in front of it, and authenticate callers there.

## 2. Configure providers and secrets

- Providers, models, prices and request classes live in `config.yaml`.
- Secrets (provider API keys, the admin token, the Grafana password) live in `deploy/.env.prod`
  and are injected as environment variables. They are never baked into the image. Keep that file
  out of version control and off shared disks; rotate the admin token and provider keys on a
  schedule.
- Rate limiting is off by default; enable it under `rate_limit:` in `config.yaml` once you know
  your per-tenant budgets.

## 3. Scale

- The gateway is stateless except for Redis, which holds breaker and health state shared across
  replicas. Run several `gateway` replicas behind the proxy and point them all at the same Redis;
  they will agree on breaker state automatically.
- Give Redis persistence (already enabled here with `appendonly`) and back up its volume.
- Watch `llm_gateway_queue_depth`: if the deferred queue does not drain, add gateway capacity or
  raise provider concurrency.

## 4. Monitor and alert

- **Grafana** at `:3000` (login with the password you set) shows the provisioned gateway board:
  success rate, p50/p95 latency, breaker state and spend.
- **Prometheus** at `:9090` loads `deploy/alerts.yml`. The rules that ship (tune the thresholds):

  | Alert | Fires when | Severity |
  |---|---|---|
  | `GatewayDown` | the gateway stops being scraped for 1m | critical |
  | `AllProvidersBreakersOpen` | every provider's breaker is open | critical |
  | `ProviderBreakerOpen` | one provider's breaker is open for 2m | warning |
  | `HighServerErrorRate` | >10% of responses are 5xx over 5m | critical |
  | `RateLimitingHeavy` | >20% of responses are 429 over 10m | warning |
  | `ProviderLatencyP95High` | a provider's p95 is over 5s for 10m | warning |
  | `DeferredQueueBacklog` | the queue is over 100 for 10m | warning |
  | `SpendRateHigh` | attributed spend exceeds $50/hour | warning |

- To page a human, point Prometheus at an Alertmanager and route these by severity (Slack, email,
  PagerDuty). Alertmanager is intentionally not bundled so you can use your existing one.

## 5. Recover

- **A provider is down.** Expected: its breaker opens within ~2s and traffic fails over. No action
  unless `AllProvidersBreakersOpen` fires — then check keys, quotas and provider status pages.
- **A breaker seems stuck open.** It half-opens after the cooldown and closes on three clean
  probes on its own. If a provider is healthy but still open, confirm it is actually reachable from
  the gateway host (network, key, quota); the breaker is reporting a real failure.
- **Redis is unavailable.** Breakers fail open (requests still flow, without shared state) and the
  deferred queue cannot persist. Restore Redis from the volume; state rebuilds from live traffic.
- **Spend spikes.** Use the by-tenant and by-feature cost metrics to find the source, then enable
  or tighten `rate_limit:` for that tenant and redeploy.

## 6. Upgrade

```bash
git pull
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.prod up -d --build
```

Health checks gate the rollout; Redis persistence means breaker state survives the restart.
