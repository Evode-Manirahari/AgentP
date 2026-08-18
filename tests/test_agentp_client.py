from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentp_client import AgentPClient, AgentPClientError
from agentp_client import cli as client_cli


def _documents(tmp_path: Path) -> list[Path]:
    first = tmp_path / "application.pdf"
    second = tmp_path / "identity.pdf"
    first.write_bytes(b"%PDF-1.7\napplication")
    second.write_bytes(b"%PDF-1.7\nidentity")
    return [first, second]


def test_one_command_packet_flow_uploads_waits_and_downloads(tmp_path: Path) -> None:
    upload_count = 0
    job_polls = 0
    submitted: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upload_count, job_polls
        assert request.headers["X-API-Key"] == "test-key"
        if request.method == "POST" and request.url.path == "/v1/files":
            upload_count += 1
            return httpx.Response(
                201,
                json={
                    "file_id": f"file_{upload_count}",
                    "filename": f"input-{upload_count}.pdf",
                    "sha256": "0" * 64,
                    "page_count": 1,
                    "status": "validated",
                },
            )
        if request.method == "POST" and request.url.path == "/v1/jobs":
            submitted.update(json.loads(request.content))
            return httpx.Response(202, json={"job_id": "job_123", "status": "queued"})
        if request.method == "GET" and request.url.path == "/v1/jobs/job_123":
            job_polls += 1
            if job_polls == 1:
                return httpx.Response(200, json={"job_id": "job_123", "status": "running"})
            return httpx.Response(
                200,
                json={
                    "job_id": "job_123",
                    "status": "succeeded",
                    "outputs": [
                        {"file_id": "file_packet", "filename": "packet.pdf"},
                        {
                            "file_id": "file_audit",
                            "filename": "packet-audit-report.json",
                        },
                    ],
                    "warnings": [],
                    "validation": {"assertions": {"packet_manifest": {"passed": True}}},
                },
            )
        if request.method == "GET" and request.url.path == "/v1/files/file_packet/content":
            return httpx.Response(200, content=b"packet bytes")
        if request.method == "GET" and request.url.path == "/v1/files/file_audit/content":
            return httpx.Response(200, content=b'{"workflow":"prepare_packet"}')
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    output_dir = tmp_path / "result"
    with AgentPClient(
        api_url="https://agentp.example",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.run_packet(
            _documents(tmp_path),
            output_dir=output_dir,
            order="manifest",
            labels=["application", "identity"],
            manifest=[{"label": "application"}, {"label": "identity"}],
            poll_interval_seconds=0,
        )

    assert submitted == {
        "operation": "prepare_packet",
        "inputs": [
            {"file_id": "file_1", "label": "application"},
            {"file_id": "file_2", "label": "identity"},
        ],
        "parameters": {
            "order": "manifest",
            "language": "eng",
            "deskew": True,
            "manifest": [{"label": "application"}, {"label": "identity"}],
        },
    }
    assert result.job_id == "job_123"
    assert result.status == "succeeded"
    assert (output_dir / "packet.pdf").read_bytes() == b"packet bytes"
    assert json.loads((output_dir / "packet-audit-report.json").read_text()) == {
        "workflow": "prepare_packet"
    }


def test_completed_with_warnings_is_a_successful_client_result(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/jobs/job_warned":
            return httpx.Response(
                200,
                json={
                    "job_id": "job_warned",
                    "status": "completed_with_warnings",
                    "warnings": [{"code": "LOW_TEXT_AFTER_OCR"}],
                },
            )
        raise AssertionError(request.url)

    with AgentPClient(
        api_url="https://agentp.example/v1",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    ) as client:
        job = client.wait_for_job("job_warned", timeout_seconds=1, poll_interval_seconds=0)

    assert job["status"] == "completed_with_warnings"


def test_failed_job_surfaces_the_worker_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "job_id": "job_failed",
                "status": "failed",
                "error": {
                    "code": "PACKET_PAGE_COUNT_MISMATCH",
                    "message": "A page went missing.",
                    "details": {"expected_pages": 3, "actual_pages": 2},
                    "retryable": False,
                },
            },
        )

    with AgentPClient(
        api_url="https://agentp.example",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(AgentPClientError) as exc:
            client.wait_for_job("job_failed", timeout_seconds=1, poll_interval_seconds=0)

    assert exc.value.code == "PACKET_PAGE_COUNT_MISMATCH"
    assert exc.value.details == {"expected_pages": 3, "actual_pages": 2}


def test_api_errors_keep_the_structured_code_and_retryability() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "error": {
                    "code": "QUEUE_UNAVAILABLE",
                    "message": "Redis is unavailable.",
                    "details": {"queue": "pdf-jobs"},
                    "retryable": True,
                }
            },
        )

    with AgentPClient(
        api_url="https://agentp.example",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(AgentPClientError) as exc:
            client.get_job("job_123")

    assert exc.value.code == "QUEUE_UNAVAILABLE"
    assert exc.value.retryable is True


def test_packet_flow_rejects_label_count_before_uploading(tmp_path: Path) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    with AgentPClient(
        api_url="https://agentp.example",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(AgentPClientError) as exc:
            client.run_packet(
                _documents(tmp_path),
                output_dir=tmp_path / "output",
                order="manifest",
                labels=["application"],
                manifest=[{"label": "application"}],
            )

    assert exc.value.code == "INVALID_PACKET_LABELS"
    assert requests == 0


def test_packet_flow_refuses_output_collisions_before_uploading(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "packet.pdf").write_bytes(b"keep me")
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    with AgentPClient(
        api_url="https://agentp.example",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(AgentPClientError) as exc:
            client.run_packet(_documents(tmp_path), output_dir=output_dir)

    assert exc.value.code == "OUTPUT_EXISTS"
    assert (output_dir / "packet.pdf").read_bytes() == b"keep me"
    assert requests == 0


def test_manifest_reader_rejects_non_array_json(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"label":"application"}', encoding="utf-8")

    with pytest.raises(AgentPClientError) as exc:
        client_cli._read_manifest(manifest)

    assert exc.value.code == "MANIFEST_SCHEMA_INVALID"


def test_cli_reports_a_missing_key_as_actionable_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("AGENTP_API_KEY", raising=False)
    monkeypatch.delenv("AGENTP_KEY", raising=False)
    documents = _documents(tmp_path)

    exit_code = client_cli.main(
        ["packet", *(str(path) for path in documents), "--json"]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    payload = json.loads(captured.err)
    assert payload["error"]["code"] == "API_KEY_MISSING"
    assert "AGENTP_API_KEY" in payload["error"]["fix"]
