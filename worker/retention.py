"""Run one document retention sweep.

Retention is caller-scheduled rather than self-scheduling: run this from cron, a
Kubernetes CronJob, or `make retention`. It is safe to run concurrently with the API and
worker, and safe to run when retention is disabled, in which case it does nothing.
"""

from __future__ import annotations

import json

from app.config import get_settings
from app.db import SessionLocal
from app.services.documents import purge_expired_documents


def run_sweep() -> dict:
    settings = get_settings()
    with SessionLocal() as session:
        sweep = purge_expired_documents(session, settings=settings)
    return sweep.as_dict()


def main() -> int:
    print(json.dumps(run_sweep()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
