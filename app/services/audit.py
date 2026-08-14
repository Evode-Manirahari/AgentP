from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEvent


def add_audit_event(
    session: Session,
    *,
    workspace_id: str,
    job_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        workspace_id=workspace_id,
        job_id=job_id,
        event_type=event_type,
        payload=payload or {},
    )
    session.add(event)
    session.flush()
    return event
