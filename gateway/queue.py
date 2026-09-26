"""Deferrable requests that survive an outage.

A job is a JSON record plus an entry in a sorted set scored by when it is next
due. Workers pop the earliest entry with ZPOPMIN, which is atomic, so two
replicas never take the same job. A failed attempt goes back with exponential
backoff and jitter. The job id is also its idempotency key: enqueueing the same
id twice queues it once, and a job that is already done never runs again.
"""
import asyncio
import json
import logging
import random

from .clock import Clock
from .config import QueueConfig
from .errors import KINDS, worth_failover

log = logging.getLogger("gateway.queue")


def retryable(kind: str | None) -> bool:
    return kind not in KINDS or worth_failover(kind)  # "unavailable" (every breaker open) is retryable


class JobQueue:
    def __init__(self, redis, clock: Clock, router, cfg: QueueConfig, prefix: str = "gw",
                 rng: random.Random | None = None, on_done=None):
        self.redis, self.clock, self.router, self.cfg, self.prefix = redis, clock, router, cfg, prefix
        self.rng = rng or random.Random()
        self.on_done = on_done  # callback(job, outcome) once a job succeeds
        self.qkey = f"{prefix}:queue"

    def _job(self, job_id: str) -> str:
        return f"{self.prefix}:job:{job_id}"

    async def enqueue(self, job_id: str, request: dict, class_name: str, meta: dict) -> bool:
        now = self.clock.now()
        record = {"id": job_id, "status": "queued", "attempts": 0, "class": class_name,
                  "request": request, "meta": meta, "created_at": now}
        created = await self.redis.set(self._job(job_id), json.dumps(record), nx=True, ex=7 * 86400)
        if created:
            await self.redis.zadd(self.qkey, {job_id: now})
        return bool(created)

    async def get(self, job_id: str) -> dict | None:
        raw = await self.redis.get(self._job(job_id))
        return json.loads(raw) if raw else None

    async def _save(self, job: dict) -> None:
        await self.redis.set(self._job(job["id"]), json.dumps(job), ex=7 * 86400)

    async def depth(self) -> int:
        return await self.redis.zcard(self.qkey)

    async def work_once(self) -> bool:
        """Run the earliest due job, if there is one. True when it did any work."""
        popped = await self.redis.zpopmin(self.qkey, 1)
        if not popped:
            return False
        job_id, due = popped[0]
        job_id = job_id.decode() if isinstance(job_id, bytes) else job_id
        now = self.clock.now()
        if due > now:
            await self.redis.zadd(self.qkey, {job_id: due})
            return False
        job = await self.get(job_id)
        if not job or job["status"] in ("done", "failed"):
            return True
        outcome = await self.router.route(job["request"], job["class"])
        job["attempts"] += 1
        job["cost_usd"] = round(job.get("cost_usd", 0.0) + outcome.cost_usd, 8)
        if outcome.completion:
            c = outcome.completion
            job.update(status="done", provider=outcome.provider, finished_at=self.clock.now(),
                       result={"text": c.text, "model": c.model, "input_tokens": c.input_tokens, "output_tokens": c.output_tokens})
            await self._save(job)
            if self.on_done:
                self.on_done(job, outcome)
        elif job["attempts"] >= self.cfg.max_attempts or not retryable(outcome.error_kind):
            job.update(status="failed", last_error=outcome.error_kind, finished_at=self.clock.now())
            await self._save(job)
        else:
            delay = min(self.cfg.max_delay_s, self.cfg.base_delay_s * 2 ** (job["attempts"] - 1))
            delay = self.rng.uniform(delay / 2, delay)  # jitter, so a recovery is not met by a stampede
            job.update(status="retrying", last_error=outcome.error_kind, next_try_at=now + delay)
            await self._save(job)
            await self.redis.zadd(self.qkey, {job_id: now + delay})
        return True

    async def run(self, stop: asyncio.Event, idle_s: float = 0.5) -> None:
        while not stop.is_set():
            try:
                did = await self.work_once()
            except Exception:  # keep the worker alive through a Redis blip
                log.exception("queue worker error")
                did = False
            if not did:
                await self.clock.sleep(idle_s)
