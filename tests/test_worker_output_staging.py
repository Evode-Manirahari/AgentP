from __future__ import annotations

from pathlib import Path

import pytest

for _m in ["pydantic_settings", "sqlalchemy", "redis", "rq"]:
    pytest.importorskip(_m)

from app.operations.base import OperationOutput  # noqa: E402
from worker.runner import _discard_staged_outputs, _stage_outputs  # noqa: E402


class RecordingStorage:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str]] = []

    def output_key(self, *, workspace_id: str, job_id: str, filename: str) -> str:
        return f"workspaces/{workspace_id}/jobs/{job_id}/{filename}"

    def upload_path(self, path: Path, *, key: str, content_type: str) -> None:
        self.uploads.append((str(path), key))


def test_staging_writes_every_output_and_describes_it(tmp_path: Path) -> None:
    packet = tmp_path / "packet.pdf"
    packet.write_bytes(b"%PDF-1.7\npacket")
    report = tmp_path / "packet-audit-report.json"
    report.write_text('{"workflow": "prepare_packet"}', encoding="utf-8")

    storage = RecordingStorage()
    staged = _stage_outputs(
        storage,
        workspace_id="ws_acme",
        job_id="job_1",
        outputs=[
            OperationOutput(path=packet, filename="packet.pdf", page_count=3),
            OperationOutput(
                path=report,
                filename="packet-audit-report.json",
                mime_type="application/json",
            ),
        ],
    )

    assert [key for _, key in storage.uploads] == [
        "workspaces/ws_acme/jobs/job_1/packet.pdf",
        "workspaces/ws_acme/jobs/job_1/packet-audit-report.json",
    ]
    assert [item["position"] for item in staged] == [0, 1]
    assert [item["storage_key"] for item in staged] == [k for _, k in storage.uploads]
    assert staged[0]["size_bytes"] == packet.stat().st_size
    assert staged[1]["size_bytes"] == report.stat().st_size
    assert len({item["sha256"] for item in staged}) == 2
    assert all(len(item["sha256"]) == 64 for item in staged)


class FailingStorage(RecordingStorage):
    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[str] = []

    def delete_object(self, *, key: str) -> None:
        raise RuntimeError("object storage is unreachable")


class DeletingStorage(RecordingStorage):
    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[str] = []

    def delete_object(self, *, key: str) -> None:
        self.deleted.append(key)


def test_registered_outputs_are_never_discarded() -> None:
    storage = DeletingStorage()

    _discard_staged_outputs(
        storage,
        [{"storage_key": "k1"}, {"storage_key": "k2"}],
        registered=True,
    )

    assert storage.deleted == [], "committed outputs must survive"


def test_unregistered_outputs_are_discarded() -> None:
    storage = DeletingStorage()

    _discard_staged_outputs(
        storage,
        [{"storage_key": "k1"}, {"storage_key": "k2"}],
        registered=False,
    )

    assert storage.deleted == ["k1", "k2"]


def test_a_cleanup_failure_never_replaces_the_real_error() -> None:
    _discard_staged_outputs(
        FailingStorage(),
        [{"storage_key": "k1"}],
        registered=False,
    )
