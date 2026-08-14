from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

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
from app.operations.base import KnownOperationError
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
BLOCKED_TARGET_MESSAGE = (
    "Webhook URLs must resolve to a public address. Set "
    "AGENTPDF_WEBHOOK_ALLOW_PRIVATE_URLS=true to allow private, loopback, or link-local "
    "targets in development."
)
logger = logging.getLogger(__name__)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _resolve_addresses(host: str, port: int) -> list[IPAddress]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError):
        # Unresolvable now does not mean unresolvable at delivery time, and a host we
        # cannot resolve is also a host we cannot connect to. Let the delivery attempt fail.
        return []

    addresses: list[IPAddress] = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return addresses


def is_blocked_address(address: IPAddress) -> bool:
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def ensure_public_webhook_target(url: str, *, settings: Settings) -> None:
    """Reject webhook targets that resolve inside the deployment's own network.

    Without this, any holder of the API key can point deliveries at cloud metadata
    (169.254.169.254) or internal services and read the response status back out of the
    delivery log. Checked at registration and again at delivery, because DNS can change
    in between.
    """
    if settings.webhook_allow_private_urls:
        return

    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise KnownOperationError(
            "INVALID_WEBHOOK_URL",
            "Webhook URLs must include a host.",
            details={"url": url[:200]},
        )

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise KnownOperationError(
            "INVALID_WEBHOOK_URL",
            "Webhook URLs must include a valid port.",
            details={"url": url[:200]},
        ) from exc

    blocked = [
        str(address)
        for address in _resolve_addresses(host, port)
        if is_blocked_address(address)
    ]
    if blocked:
        raise KnownOperationError(
            "WEBHOOK_TARGET_NOT_ALLOWED",
            BLOCKED_TARGET_MESSAGE,
            details={"host": host, "blocked_addresses": sorted(blocked)},
        )


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
    workspace_id: str,
    request: WebhookCreate,
    settings: Settings | None = None,
) -> WebhookCreateResponse:
    ensure_public_webhook_target(request.url, settings=settings or get_settings())
    secret = secrets.token_urlsafe(32)
    endpoint = WebhookEndpoint(
        workspace_id=workspace_id,
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
    workspace_id: str,
    limit: int = 50,
    offset: int = 0,
) -> WebhookEndpointListResponse:
    statement = (
        select(WebhookEndpoint)
        .where(WebhookEndpoint.workspace_id == workspace_id)
        .order_by(WebhookEndpoint.created_at.desc(), WebhookEndpoint.id.desc())
        .limit(limit)
        .offset(offset)
    )
    endpoints = list(session.scalars(statement))
    return WebhookEndpointListResponse(
        webhooks=[_endpoint_response(endpoint) for endpoint in endpoints],
        count=len(endpoints),
        limit=limit,
        offset=offset,
    )


def get_webhook_endpoint(
    session: Session,
    webhook_id: str,
    *,
    workspace_id: str,
) -> WebhookEndpointResponse | None:
    endpoint = session.scalar(
        select(WebhookEndpoint)
        .where(WebhookEndpoint.id == webhook_id)
        .where(WebhookEndpoint.workspace_id == workspace_id)
    )
    if endpoint is None:
        return None
    return _endpoint_response(endpoint)


def disable_webhook_endpoint(
    session: Session,
    webhook_id: str,
    *,
    workspace_id: str,
) -> WebhookEndpointResponse | None:
    endpoint = session.scalar(
        select(WebhookEndpoint)
        .where(WebhookEndpoint.id == webhook_id)
        .where(WebhookEndpoint.workspace_id == workspace_id)
    )
    if endpoint is None:
        return None
    endpoint.active = False
    session.add(endpoint)
    session.commit()
    return _endpoint_response(endpoint)


def list_webhook_deliveries(
    session: Session,
    *,
    workspace_id: str,
    endpoint_id: str | None = None,
    job_id: str | None = None,
    status_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> WebhookDeliveryListResponse:
    statement = select(WebhookDelivery).order_by(
        WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc()
    )
    statement = statement.where(WebhookDelivery.workspace_id == workspace_id)
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
        "workspace_id": job.workspace_id,
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


def _matching_endpoints(
    session: Session,
    *,
    workspace_id: str,
    event_type: str,
) -> list[WebhookEndpoint]:
    endpoints = list(
        session.scalars(
            select(WebhookEndpoint)
            .where(WebhookEndpoint.workspace_id == workspace_id)
            .where(WebhookEndpoint.active.is_(True))
        )
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
        for endpoint in _matching_endpoints(
            session,
            workspace_id=job.workspace_id,
            event_type=event_type,
        ):
            delivery = WebhookDelivery(
                workspace_id=job.workspace_id,
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


@dataclass(frozen=True)
class DeliveryPlan:
    delivery_id: str
    event_type: str
    url: str
    secret: str
    body: bytes
    attempts: int


def _start_delivery(delivery_id: str, *, settings: Settings) -> DeliveryPlan | None:
    """Load everything an attempt needs, apply the guards, and release the session."""
    with SessionLocal() as session:
        delivery = session.get(WebhookDelivery, delivery_id)
        if delivery is None:
            return None

        endpoint = delivery.endpoint
        refusal: str | None = None
        if not endpoint.active:
            refusal = "Webhook is inactive."
        else:
            # Re-checked here because the endpoint may have been registered before this
            # guard existed, or its DNS record may since have been pointed inward.
            try:
                ensure_public_webhook_target(endpoint.url, settings=settings)
            except KnownOperationError as exc:
                refusal = exc.message

        if refusal is not None:
            _mark_delivery_failure(delivery, attempts=delivery.attempts, error=refusal)
            session.add(delivery)
            session.commit()
            return None

        return DeliveryPlan(
            delivery_id=delivery.id,
            event_type=delivery.event_type,
            url=endpoint.url,
            secret=endpoint.secret,
            body=serialize_webhook_payload(delivery.payload),
            attempts=delivery.attempts,
        )


def _record_delivery_state(
    delivery_id: str,
    *,
    attempts: int,
    status: str,
    status_code: int | None = None,
    error: str | None = None,
    delivered: bool = False,
) -> None:
    with SessionLocal() as session:
        delivery = session.get(WebhookDelivery, delivery_id)
        if delivery is None:
            return
        delivery.attempts = attempts
        delivery.status = status
        delivery.last_status_code = status_code
        delivery.last_error = error[:2000] if error else None
        if delivered:
            delivery.delivered_at = datetime.now(UTC)
        session.add(delivery)
        session.commit()


def deliver_webhook_delivery(
    delivery_id: str,
    *,
    settings: Settings | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    client_factory: Callable[..., httpx.Client] = httpx.Client,
) -> None:
    active_settings = settings or get_settings()
    plan = _start_delivery(delivery_id, settings=active_settings)
    if plan is None:
        return

    max_attempts = active_settings.webhook_max_attempts

    # Each state change opens its own short session. A receiver that is slow, or a backoff
    # between attempts, must never hold a database connection for its duration.
    for attempt in range(plan.attempts + 1, max_attempts + 1):
        _record_delivery_state(
            plan.delivery_id,
            attempts=attempt,
            status=WebhookDeliveryStatus.PENDING.value,
        )

        timestamp = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "AgentP-Webhooks/0.1",
            "X-AgentP-Delivery": plan.delivery_id,
            "X-AgentP-Event": plan.event_type,
            "X-AgentP-Timestamp": str(timestamp),
            "X-AgentP-Signature": sign_webhook_payload(
                secret=plan.secret,
                timestamp=timestamp,
                body=plan.body,
            ),
        }

        status_code: int | None = None
        error: str | None = None
        try:
            with client_factory(
                timeout=active_settings.webhook_delivery_timeout_seconds
            ) as client:
                response = client.post(plan.url, content=plan.body, headers=headers)
            status_code = response.status_code
            if 200 <= status_code < 300:
                _record_delivery_state(
                    plan.delivery_id,
                    attempts=attempt,
                    status=WebhookDeliveryStatus.SUCCEEDED.value,
                    status_code=status_code,
                    delivered=True,
                )
                return
            error = f"HTTP {status_code}"
        except Exception as exc:
            error = str(exc)

        exhausted = attempt >= max_attempts
        _record_delivery_state(
            plan.delivery_id,
            attempts=attempt,
            status=(
                WebhookDeliveryStatus.FAILED.value
                if exhausted
                else WebhookDeliveryStatus.PENDING.value
            ),
            status_code=status_code,
            error=error,
        )
        if exhausted:
            return

        backoff_index = min(attempt - 1, len(WEBHOOK_BACKOFF_SECONDS) - 1)
        sleep_fn(WEBHOOK_BACKOFF_SECONDS[backoff_index])
