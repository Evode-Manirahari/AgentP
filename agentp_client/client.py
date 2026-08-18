from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

SUCCESS_STATUSES = {"succeeded", "completed_with_warnings"}
TERMINAL_STATUSES = SUCCESS_STATUSES | {"failed", "canceled"}
EXPECTED_PACKET_FILENAMES = ("packet.pdf", "packet-audit-report.json")


class AgentPClientError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = retryable


@dataclass(frozen=True)
class PacketResult:
    job_id: str
    status: str
    outputs: dict[str, Path]
    warnings: list[dict[str, Any]]
    validation: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "outputs": {name: str(path) for name, path in self.outputs.items()},
            "warnings": self.warnings,
            "validation": self.validation,
        }


def _extract_error(response: httpx.Response) -> AgentPClientError:
    payload: Any = None
    try:
        payload = response.json()
    except ValueError:
        pass

    error: Any = payload
    if isinstance(payload, dict):
        error = payload.get("error", payload)
        if isinstance(error, dict) and isinstance(error.get("detail"), dict):
            error = error["detail"].get("error", error["detail"])

    if isinstance(error, dict):
        code = str(error.get("code") or f"HTTP_{response.status_code}")
        message = str(error.get("message") or "AgentP rejected the request.")
        details = error.get("details")
        retryable = bool(error.get("retryable", response.status_code >= 500))
        return AgentPClientError(
            code,
            message,
            details=details if isinstance(details, dict) else {},
            retryable=retryable,
        )

    return AgentPClientError(
        f"HTTP_{response.status_code}",
        "AgentP returned an unreadable error response.",
        details={"response_text": response.text[-2000:]},
        retryable=response.status_code >= 500,
    )


class AgentPClient:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        request_timeout_seconds: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        base = api_url.rstrip("/")
        self.api_root = base if base.endswith("/v1") else f"{base}/v1"
        self._client = httpx.Client(
            headers={
                "X-API-Key": api_key,
                "User-Agent": "agentp-python-client/0.1",
            },
            timeout=request_timeout_seconds,
            transport=transport,
        )

    def __enter__(self) -> AgentPClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, f"{self.api_root}{path}", **kwargs)
        except httpx.TransportError as exc:
            raise AgentPClientError(
                "NETWORK_ERROR",
                "Could not reach the AgentP API.",
                details={"api_url": self.api_root, "reason": str(exc)},
                retryable=True,
            ) from exc
        if response.is_error:
            raise _extract_error(response)
        return response

    def upload_file(self, path: Path) -> dict[str, Any]:
        try:
            with path.open("rb") as handle:
                response = self._request(
                    "POST",
                    "/files",
                    files={"file": (path.name, handle, "application/pdf")},
                )
        except OSError as exc:
            raise AgentPClientError(
                "FILE_READ_FAILED",
                "Could not read an input document.",
                details={"path": str(path), "reason": str(exc)},
            ) from exc
        return response.json()

    def create_packet_job(
        self,
        *,
        uploads: list[dict[str, Any]],
        order: str,
        labels: list[str] | None,
        manifest: list[dict[str, Any]] | None,
        allow_unlisted: bool,
        language: str,
        deskew: bool,
        idempotency_key: str | None,
    ) -> str:
        inputs = []
        for index, upload in enumerate(uploads):
            item = {"file_id": upload["file_id"]}
            if labels is not None:
                item["label"] = labels[index]
            inputs.append(item)

        parameters: dict[str, Any] = {
            "order": order,
            "language": language,
            "deskew": deskew,
        }
        if manifest is not None:
            parameters["manifest"] = manifest
        if allow_unlisted:
            parameters["allow_unlisted"] = True

        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        response = self._request(
            "POST",
            "/jobs",
            json={"operation": "prepare_packet", "inputs": inputs, "parameters": parameters},
            headers=headers,
        )
        payload = response.json()
        job_id = payload.get("job_id")
        if not isinstance(job_id, str):
            raise AgentPClientError(
                "MALFORMED_API_RESPONSE",
                "AgentP accepted the job but did not return a job ID.",
                details={"response": payload},
                retryable=True,
            )
        return job_id

    def get_job(self, job_id: str) -> dict[str, Any]:
        payload = self._request("GET", f"/jobs/{job_id}").json()
        if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
            raise AgentPClientError(
                "MALFORMED_API_RESPONSE",
                "AgentP returned a job response without a status.",
                details={"job_id": job_id},
                retryable=True,
            )
        return payload

    def wait_for_job(
        self,
        job_id: str,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float = 1.0,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        interval = max(0.0, poll_interval_seconds)
        last_status: str | None = None

        while True:
            try:
                job = self.get_job(job_id)
            except AgentPClientError as exc:
                if not exc.retryable or time.monotonic() >= deadline:
                    raise
                if progress is not None:
                    progress(f"Temporary API error ({exc.code}); retrying job {job_id}.")
                time.sleep(interval)
                interval = min(max(interval * 1.5, 0.1), 5.0)
                continue

            status = job["status"]
            if status != last_status and progress is not None:
                progress(f"Job {job_id}: {status}")
                last_status = status
            if status in SUCCESS_STATUSES:
                return job
            if status in {"failed", "canceled"}:
                error = job.get("error")
                if isinstance(error, dict):
                    raise AgentPClientError(
                        str(error.get("code") or "JOB_FAILED"),
                        str(error.get("message") or f"Packet job {status}."),
                        details=(
                            error.get("details")
                            if isinstance(error.get("details"), dict)
                            else {}
                        ),
                        retryable=bool(error.get("retryable", False)),
                    )
                raise AgentPClientError(
                    "JOB_CANCELED" if status == "canceled" else "JOB_FAILED",
                    f"Packet job {status}.",
                    details={"job_id": job_id},
                )
            if status not in TERMINAL_STATUSES | {"queued", "running", "validating"}:
                raise AgentPClientError(
                    "UNKNOWN_JOB_STATUS",
                    "AgentP returned an unknown job status.",
                    details={"job_id": job_id, "status": status},
                    retryable=True,
                )
            if time.monotonic() >= deadline:
                raise AgentPClientError(
                    "JOB_TIMEOUT",
                    "Timed out while waiting for the packet job.",
                    details={"job_id": job_id, "last_status": status},
                    retryable=True,
                )
            time.sleep(interval)
            interval = min(max(interval * 1.25, 0.1), 5.0)

    def download_output(self, file_id: str, destination: Path, *, overwrite: bool) -> None:
        if destination.exists() and not overwrite:
            raise AgentPClientError(
                "OUTPUT_EXISTS",
                "Refusing to overwrite an existing output file.",
                details={"path": str(destination)},
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        response = self._request("GET", f"/files/{file_id}/content")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{destination.name}.",
                suffix=".part",
                dir=destination.parent,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                for chunk in response.iter_bytes():
                    temporary.write(chunk)
            os.replace(temporary_path, destination)
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise AgentPClientError(
                "OUTPUT_WRITE_FAILED",
                "Could not save a packet output.",
                details={"path": str(destination), "reason": str(exc)},
            ) from exc

    def run_packet(
        self,
        documents: list[Path],
        *,
        output_dir: Path,
        order: str = "as_provided",
        labels: list[str] | None = None,
        manifest: list[dict[str, Any]] | None = None,
        allow_unlisted: bool = False,
        language: str = "eng",
        deskew: bool = True,
        idempotency_key: str | None = None,
        timeout_seconds: float = 600.0,
        poll_interval_seconds: float = 1.0,
        overwrite: bool = False,
        progress: Callable[[str], None] | None = None,
    ) -> PacketResult:
        if len(documents) < 2:
            raise AgentPClientError(
                "NOT_ENOUGH_INPUTS",
                "A packet requires at least two input PDFs.",
                details={"input_count": len(documents)},
            )
        missing = [str(path) for path in documents if not path.is_file()]
        if missing:
            raise AgentPClientError(
                "INPUT_FILE_NOT_FOUND",
                "One or more input documents do not exist.",
                details={"paths": missing},
            )
        if labels is not None and len(labels) != len(documents):
            raise AgentPClientError(
                "INVALID_PACKET_LABELS",
                "Provide exactly one --label for every input document.",
                details={"input_count": len(documents), "label_count": len(labels)},
            )
        if order == "manifest" and (labels is None or manifest is None):
            raise AgentPClientError(
                "INVALID_PACKET_MANIFEST",
                "Manifest ordering requires --manifest and one --label per document.",
            )
        if order != "manifest" and (labels is not None or manifest is not None or allow_unlisted):
            raise AgentPClientError(
                "PACKET_MANIFEST_NOT_ENABLED",
                "Labels and manifest settings require manifest ordering.",
                details={"order": order},
            )

        if not overwrite:
            collisions = [
                str(output_dir / filename)
                for filename in EXPECTED_PACKET_FILENAMES
                if (output_dir / filename).exists()
            ]
            if collisions:
                raise AgentPClientError(
                    "OUTPUT_EXISTS",
                    "Refusing to overwrite existing packet outputs.",
                    details={"paths": collisions},
                )

        uploads: list[dict[str, Any]] = []
        for index, path in enumerate(documents, start=1):
            if progress is not None:
                progress(f"Uploading {index}/{len(documents)}: {path.name}")
            uploads.append(self.upload_file(path))

        job_id = self.create_packet_job(
            uploads=uploads,
            order=order,
            labels=labels,
            manifest=manifest,
            allow_unlisted=allow_unlisted,
            language=language,
            deskew=deskew,
            idempotency_key=idempotency_key,
        )
        if progress is not None:
            progress(f"Submitted packet job {job_id}.")
        job = self.wait_for_job(
            job_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            progress=progress,
        )

        output_entries = job.get("outputs")
        if not isinstance(output_entries, list) or not output_entries:
            raise AgentPClientError(
                "PACKET_OUTPUTS_MISSING",
                "The packet job succeeded without downloadable outputs.",
                details={"job_id": job_id},
            )

        saved: dict[str, Path] = {}
        for output in output_entries:
            if not isinstance(output, dict):
                continue
            file_id = output.get("file_id")
            filename = output.get("filename")
            if not isinstance(file_id, str) or not isinstance(filename, str):
                continue
            safe_name = Path(filename).name
            destination = output_dir / safe_name
            if progress is not None:
                progress(f"Downloading {safe_name}")
            self.download_output(file_id, destination, overwrite=overwrite)
            saved[safe_name] = destination

        if set(EXPECTED_PACKET_FILENAMES) - set(saved):
            raise AgentPClientError(
                "PACKET_OUTPUTS_INCOMPLETE",
                "The packet job did not return both expected artifacts.",
                details={"job_id": job_id, "downloaded": sorted(saved)},
            )

        warnings = job.get("warnings")
        return PacketResult(
            job_id=job_id,
            status=job["status"],
            outputs=saved,
            warnings=warnings if isinstance(warnings, list) else [],
            validation=job.get("validation") if isinstance(job.get("validation"), dict) else None,
        )
