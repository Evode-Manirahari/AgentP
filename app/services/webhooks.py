from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import (
    Job,
    JobOutput,
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookEndpoint,
)
from app.schemas import (
    DEFAULT_WEBHOOK_EVENTS,
    WebhookCreate,
    WebhookCreateResponse,
    WebhookDeliveryListResponse,
    WebhookDeliveryResponse,
    WebhookEndpointListResponse,
    WebhookEndpointResponse,
)
from worker.queue import enqueue_webhook_delivery

WEBHOOK_BACKOFF_SECONDS = (1.0, 3.0, 10.0)
logger = logging.getLogger(__name__)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def serialize_webhook_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")


def sign_webhook_payload(*, secret: str, timestamp: int, body: bytes) -> str:
    signed = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _normalize_events(events: list[str]) -> list[str]:
    normalized = sorted(set(events))
    if not normalized:
        return list(DEFAULT_WEBHOOK_EVENTS)
    return normalized


def _endpoint_response(endpoint: WebhookEndpoint) -> WebhookEndpointResponse:
    return WebhookEndpointResponse(
        webhook_id=endpoint.id,
        url=endpoint.url,
        events=endpoint.events,
        active=endpoint.active,
        created_at=endpoint.created_at,
    )


def _delivery_response(delivery: WebhookDelivery) -> WebhookDeliveryResponse:
    return WebhookDeliveryResponse(
        delivery_id=delivery.id,
        webhook_id=delivery.endpoint_id,
        job_id=delivery.job_id,
        event_type=delivery.event_type,
        status=delivery.status,
        attempts=delivery.attempts,
        last_status_code=delivery.last_status_code,
        last_error=delivery.last_error,
        created_at=delivery.created_at,
        delivered_at=delivery.delivered_at,
    )


def create_webhook_endpoint(
    session: Session,
    *,
    request: WebhookCreate,
) -> WebhookCreateResponse:
    secret = secrets.token_urlsafe(32)
    endpoint = WebhookEndpoint(
        url=request.url,
        secret=secret,
        events=_normalize_events(list(request.events)),
        active=request.active,
    )
    session.add(endpoint)
    session.flush()
    session.commit()

    return WebhookCreateResponse(
        **_endpoint_response(endpoint).model_dump(),
        signing_secret=secret,
    )


def list_webhook_endpoints(
    session: Session,
    *,
    limit: int = 50,
    offset: int = 0,
) -> WebhookEndpointListResponse:
    statement = select(WebhookEndpoint).order_by(WebhookEndpoint.created_at.desc()).limit(
        limit
    ).offset(offset)
    endpoints = list(session.scalars(statement))
    return WebhookEndpointListResponse(
        webhooks=[_endpoint_response(endpoint) for endpoint in endpoints],
        count=len(endpoints),
        limit=limit,
        offset=offset,
    )


def get_webhook_endpoint(session: Session, webhook_id: str) -> WebhookEndpointResponse | None:
    endpoint = session.get(WebhookEndpoint, webhook_id)
    if endpoint is None:
        return None
    return _endpoint_response(endpoint)


def disable_webhook_endpoint(session: Session, webhook_id: str) -> WebhookEndpointResponse | None:
    endpoint = session.get(WebhookEndpoint, webhook_id)
    if endpoint is None:
        return None
    endpoint.active = False
    session.add(endpoint)
    session.commit()
    return _endpoint_response(endpoint)


def list_webhook_deliveries(
    session: Session,
    *,
    endpoint_id: str | None = None,
    job_id: str | None = None,
    status_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> WebhookDeliveryListResponse:
    statement = select(WebhookDelivery).order_by(WebhookDelivery.created_at.desc())
    if endpoint_id is not None:
        statement = statement.where(WebhookDelivery.endpoint_id == endpoint_id)
    if job_id is not None:
        statement = statement.where(WebhookDelivery.job_id == job_id)
    if status_filter is not None:
        statement = statement.where(WebhookDelivery.status == status_filter)
    statement = statement.limit(limit).offset(offset)
    deliveries = list(session.scalars(statement))
    return WebhookDeliveryListResponse(
        deliveries=[_delivery_response(delivery) for delivery in deliveries],
        count=len(deliveries),
        limit=limit,
        offset=offset,
    )


def _job_error(job: Job) -> dict[str, Any] | None:
    if not job.error_code and not job.error_message:
        return None
    return {
        "code": job.error_code or "JOB_FAILED",
        "message": job.error_message or "The job failed.",
        "details": {},
        "retryable": False,
    }


def _isoformat_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def build_job_webhook_payload(*, event_type: str, job: Job) -> dict[str, Any]:
    outputs = [
        {
            "file_id": output.document.id,
            "filename": output.document.original_filename,
            "mime_type": output.document.mime_type,
            "page_count": output.document.page_count,
        }
        for output in job.outputs
    ]
    warnings = (job.validation or {}).get("warnings") or []
    return {
        "event": event_type,
        "job": {
            "job_id": job.id,
            "operation": job.operation,
            "status": job.status,
            "parameters": job.parameters,
            "outputs": outputs,
            "validation": job.validation,
            "warnings": warnings,
            "error": _job_error(job),
            "created_at": _isoformat_datetime(job.created_at),
            "started_at": _isoformat_datetime(job.started_at),
            "finished_at": _isoformat_datetime(job.finished_at),
        },
    }


def _matching_endpoints(session: Session, event_type: str) -> list[WebhookEndpoint]:
    endpoints = list(
        session.scalars(select(WebhookEndpoint).where(WebhookEndpoint.active.is_(True)))
    )
    return [endpoint for endpoint in endpoints if event_type in endpoint.events]


def queue_terminal_job_webhooks(
    *,
    job_id: str,
    event_type: str,
    settings: Settings | None = None,
    enqueue_fn: Callable[..., str] = enqueue_webhook_delivery,
) -> list[str]:
    active_settings = settings or get_settings()
    with SessionLocal() as session:
        job = session.scalar(
            select(Job)
            .options(selectinload(Job.outputs).selectinload(JobOutput.document))
            .where(Job.id == job_id)
        )
        if job is None:
            return []

        payload = build_job_webhook_payload(event_type=event_type, job=job)
        delivery_ids: list[str] = []
        for endpoint in _matching_endpoints(session, event_type):
            delivery = WebhookDelivery(
                endpoint_id=endpoint.id,
                job_id=job.id,
                event_type=event_type,
                payload=payload,
                status=WebhookDeliveryStatus.PENDING.value,
            )
            session.add(delivery)
            session.flush()
            delivery_ids.append(delivery.id)
        session.commit()

    enqueued_delivery_ids: list[str] = []
    for delivery_id in delivery_ids:
        try:
            enqueue_fn(delivery_id, settings=active_settings)
        except Exception as exc:
            logger.exception("Could not enqueue webhook delivery %s.", delivery_id)
            _record_enqueue_failure(delivery_id, exc)
        else:
            enqueued_delivery_ids.append(delivery_id)
    return enqueued_delivery_ids


def safe_queue_terminal_job_webhooks(
    *,
    job_id: str,
    event_type: str,
    settings: Settings | None = None,
) -> list[str]:
    try:
        return queue_terminal_job_webhooks(
            job_id=job_id,
            event_type=event_type,
            settings=settings,
        )
    except Exception:
        logger.exception("Could not create webhook deliveries for job %s.", job_id)
        return []


def _mark_delivery_failure(
    delivery: WebhookDelivery,
    *,
    attempts: int,
    error: str,
    status_code: int | None = None,
) -> None:
    delivery.attempts = attempts
    delivery.last_status_code = status_code
    delivery.last_error = error[:2000]
    delivery.status = WebhookDeliveryStatus.FAILED.value


def _record_enqueue_failure(delivery_id: str, error: Exception) -> None:
    try:
        with SessionLocal() as session:
            delivery = session.get(WebhookDelivery, delivery_id)
            if delivery is None:
                return
            _mark_delivery_failure(
                delivery,
                attempts=delivery.attempts,
                error=f"Webhook could not be enqueued: {error}",
            )
            session.add(delivery)
            session.commit()
    except Exception:
        logger.exception("Could not record enqueue failure for webhook delivery %s.", delivery_id)


def deliver_webhook_delivery(
    delivery_id: str,
    *,
    settings: Settings | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    client_factory: Callable[..., httpx.Client] = httpx.Client,
) -> None:
    active_settings = settings or get_settings()
    with SessionLocal() as session:
        delivery = session.get(WebhookDelivery, delivery_id)
        if delivery is None:
            return
        endpoint = delivery.endpoint
        if not endpoint.active:
            _mark_delivery_failure(
                delivery,
                attempts=delivery.attempts,
                error="Webhook is inactive.",
            )
            session.add(delivery)
            session.commit()
            return

        body = serialize_webhook_payload(delivery.payload)
        max_attempts = active_settings.webhook_max_attempts

        for attempt in range(delivery.attempts + 1, max_attempts + 1):
            timestamp = int(time.time())
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "AgentP-Webhooks/0.1",
                "X-AgentP-Delivery": delivery.id,
                "X-AgentP-Event": delivery.event_type,
                "X-AgentP-Timestamp": str(timestamp),
                "X-AgentP-Signature": sign_webhook_payload(
                    secret=endpoint.secret,
                    timestamp=timestamp,
                    body=body,
                ),
            }

            delivery.attempts = attempt
            delivery.status = WebhookDeliveryStatus.PENDING.value
            session.add(delivery)
            session.commit()

            try:
                with client_factory(
                    timeout=active_settings.webhook_delivery_timeout_seconds
                ) as client:
                    response = client.post(endpoint.url, content=body, headers=headers)
                delivery.last_status_code = response.status_code
                delivery.last_error = None
                if 200 <= response.status_code < 300:
                    delivery.status = WebhookDeliveryStatus.SUCCEEDED.value
                    delivery.delivered_at = datetime.now(UTC)
                    session.add(delivery)
                    session.commit()
                    return
                delivery.last_error = f"HTTP {response.status_code}"
            except Exception as exc:
                delivery.last_status_code = None
                delivery.last_error = str(exc)[:2000]

            if attempt >= max_attempts:
                delivery.status = WebhookDeliveryStatus.FAILED.value
                session.add(delivery)
                session.commit()
                return

            session.add(delivery)
            session.commit()
            backoff_index = min(attempt - 1, len(WEBHOOK_BACKOFF_SECONDS) - 1)
            sleep_fn(WEBHOOK_BACKOFF_SECONDS[backoff_index])
