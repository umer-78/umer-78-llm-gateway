"""Steady traffic against a running gateway, for the live dashboard demo.

    python -m bench.traffic --url http://localhost:8000 --rps 6
"""
import argparse
import asyncio
import collections
import itertools
import random
import time

import httpx


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--rps", type=float, default=6)
    ap.add_argument("--seconds", type=float, default=0, help="0 runs until stopped")
    args = ap.parse_args()
    rng, counts, n = random.Random(), collections.Counter(), itertools.count()
    mix = [("interactive", "chat"), ("interactive", "search"), ("classify", "tagging"), ("batch", "summarize")]
    async with httpx.AsyncClient(base_url=args.url, timeout=60) as client:
        async def one():
            cls, feature = rng.choices(mix, weights=[5, 3, 2, 1])[0]
            i = next(n)
            headers = {"X-Tenant": rng.choices(["acme", "globex"], weights=[7, 3])[0], "X-Feature": feature,
                       "X-Request-Id": f"demo-{time.time_ns()}-{i}", "X-Request-Class": cls}
            try:
                r = await client.post("/v1/chat/completions", headers=headers,
                                      json={"messages": [{"role": "user", "content": f"{feature} request {i}"}]})
                counts[f"{r.status_code} {r.headers.get('X-Gateway-Provider', '')}".strip()] += 1
            except httpx.HTTPError as e:
                counts[type(e).__name__] += 1
        start, last = time.monotonic(), time.monotonic()
        while not args.seconds or time.monotonic() - start < args.seconds:
            asyncio.create_task(one())
            await asyncio.sleep(rng.expovariate(args.rps))
            if time.monotonic() - last >= 5:
                print(dict(counts), flush=True)
                counts.clear()
                last = time.monotonic()


if __name__ == "__main__":
    asyncio.run(main())
