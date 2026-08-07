from __future__ import annotations

from redis import Redis
from rq import Queue

from app.config import Settings, get_settings


def get_queue(settings: Settings | None = None) -> Queue:
    active_settings = settings or get_settings()
    return Queue(
        active_settings.queue_name,
        connection=Redis.from_url(active_settings.redis_url),
    )


def enqueue_job(job_id: str, *, settings: Settings | None = None) -> str:
    active_settings = settings or get_settings()
    queue = get_queue(active_settings)
    rq_job = queue.enqueue(
        "worker.runner.process_job",
        job_id,
        job_timeout=active_settings.operation_timeout_seconds + 60,
        result_ttl=3600,
        failure_ttl=86400,
    )
    return str(rq_job.id)


def enqueue_webhook_delivery(delivery_id: str, *, settings: Settings | None = None) -> str:
    active_settings = settings or get_settings()
    queue = get_queue(active_settings)
    rq_job = queue.enqueue(
        "worker.runner.process_webhook_delivery",
        delivery_id,
        job_timeout=active_settings.webhook_delivery_timeout_seconds
        * active_settings.webhook_max_attempts
        + 60,
        result_ttl=3600,
        failure_ttl=86400,
    )
    return str(rq_job.id)
