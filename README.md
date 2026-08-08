# AgentP

AgentP is a typed, asynchronous, verifiable PDF execution API for AI agents.

Positioning: the document execution layer for AI agents. Upload business documents, run allowlisted operations, verify the output, and retrieve an immutable artifact with an audit trail.

## What v0 Builds

The first product path is intentionally narrow:

1. Upload PDFs.
2. Validate structure with `qpdf`, page counting, and a render probe.
3. Create an async job for `merge`, `split`, `ocr`, `compress`, or `extract_text`.
4. Run the `prepare_packet` workflow, which chains those steps into one verified artifact.
5. Run the job in an RQ worker using deterministic PDF tools.
6. Validate the output before marking the job complete.
7. Return short-lived download URLs, validation details, and audit events.
8. Deliver signed completion webhooks with durable delivery history.

MCP tools expose the same job service as the REST API. There is no separate AI chat layer.

## Architecture

```text
REST client / AI agent
        |
  FastAPI + MCP
        |
 Postgres + Redis
        |
      Worker
        |
 pikepdf / PyMuPDF / OCRmyPDF / qpdf / Ghostscript
        |
      MinIO
```

The API is the control plane. The worker is the data plane. Large document operations never run in the FastAPI request process.

Database schema changes are versioned with Alembic migrations. The API applies migrations
on startup when `alembic.ini` is present, and `make db-upgrade` can be used for manual
schema upgrades.

## Local Start

```bash
docker compose up --build
```

Services:

- API: `http://localhost:8000`
- Swagger UI: `http://localhost:8000/docs`
- MCP Streamable HTTP endpoint: `http://localhost:8000/mcp`
- MinIO console: `http://localhost:9001`

System endpoints:

- `GET /health` reports that the API process is alive.
- `GET /ready` checks Postgres, Redis, and object storage connectivity.

Local API key:

```text
X-API-Key: local-dev-key
```

## REST Demo

Upload two PDFs:

```bash
curl -sS -X POST http://localhost:8000/v1/files \
  -H "X-API-Key: local-dev-key" \
  -F "file=@packet-a.pdf"
```

```bash
curl -sS -X POST http://localhost:8000/v1/files \
  -H "X-API-Key: local-dev-key" \
  -F "file=@packet-b.pdf"
```

Create an OCR-if-needed merge job:

```bash
curl -sS -X POST http://localhost:8000/v1/jobs \
  -H "X-API-Key: local-dev-key" \
  -H "Idempotency-Key: demo-merge-001" \
  -H "Content-Type: application/json" \
  -d '{
    "operation": "merge",
    "inputs": [{"file_id": "file_..."}, {"file_id": "file_..."}],
    "parameters": {"ocr_if_needed": true, "language": "eng", "deskew": true}
  }'
```

## Packet Workflow

`prepare_packet` is the flagship workflow: hand it an unorganized set of documents and it
returns one validated PDF plus an audit report.

```bash
curl -sS -X POST http://localhost:8000/v1/jobs \
  -H "X-API-Key: local-dev-key" \
  -H "Idempotency-Key: demo-packet-001" \
  -H "Content-Type: application/json" \
  -d '{
    "operation": "prepare_packet",
    "inputs": [{"file_id": "file_..."}, {"file_id": "file_..."}],
    "parameters": {"order": "filename", "language": "eng", "deskew": true}
  }'
```

It runs four recorded steps:

1. `inspect` — page count, SHA-256, and scan detection for every input.
2. `ocr` — OCR only the inputs that have no usable text layer.
3. `organize` — order documents by `as_provided` (default) or `filename`.
4. `merge` — combine them into `packet.pdf`.

Two outputs come back. `packet.pdf` is the merged, searchable result.
`packet-audit-report.json` records each input's checksum, whether it was OCRed, the step
timeline, the final sequence, and any warnings.

Warnings describe an input without failing the packet. `LOW_TEXT_AFTER_OCR` means a
document was OCRed but still holds almost no text — usually an unreadable scan.

A job that produced warnings finishes as `completed_with_warnings` rather than `succeeded`.
Both are success: every assertion passed and the outputs are valid. The distinct status
exists so a caller polling `status` learns that something needs a human look without
having to parse the audit report. Warnings are listed on the job as `warnings`, counted as
`warning_count` in job listings, and included in the webhook payload.

**Treat `succeeded` and `completed_with_warnings` as the same outcome unless you act on
warnings.** Code that checks `status == "succeeded"` will silently skip warned jobs.

The job only reaches `succeeded` after two assertions hold: the packet contains every
input page, and the audit report describes every input document. Either failing produces
`PACKET_PAGE_COUNT_MISMATCH` or `AUDIT_REPORT_INCOMPLETE` instead of a silent result.

Run it end to end against real OCR:

```bash
make demo-packet
```

Discover supported operations and parameter schemas:

```bash
curl -sS http://localhost:8000/v1/operations \
  -H "X-API-Key: local-dev-key"
```

Poll the job:

```bash
curl -sS http://localhost:8000/v1/jobs/job_... \
  -H "X-API-Key: local-dev-key"
```

List recent jobs, optionally filtering by status:

```bash
curl -sS 'http://localhost:8000/v1/jobs?status=queued&limit=20' \
  -H "X-API-Key: local-dev-key"
```

Cancel a queued job before the worker starts processing it:

```bash
curl -sS -X POST http://localhost:8000/v1/jobs/job_.../cancel \
  -H "X-API-Key: local-dev-key"
```

The response includes:

- `outputs[].download_url`
- `validation`
- `audit`
- structured `error` if the job failed

Files can also be downloaded through the API when direct object-store URLs are not
reachable from the client:

```bash
curl -L http://localhost:8000/v1/files/file_.../content \
  -H "X-API-Key: local-dev-key" \
  -o output.pdf
```

Idempotency keys are request-scoped. Reusing the same `Idempotency-Key` with the same
operation, inputs, and parameters returns the existing job. Reusing the key for a different
request returns an `IDEMPOTENCY_KEY_CONFLICT` error. Requests that race on the same key
collapse onto a single job rather than failing.

A `QUEUE_UNAVAILABLE` error is retryable: the job record exists but never reached the
worker queue. Retrying with the same `Idempotency-Key` re-enqueues that job and records a
`job.enqueue_retried` audit event. Jobs that already reached the queue are never enqueued
twice, so retrying is safe for every other outcome as well.

In Docker Compose, signed download URLs use `AGENTPDF_S3_PUBLIC_ENDPOINT_URL=http://localhost:9000` so links returned to host clients are reachable outside the Docker network.

## Webhooks

Register an endpoint for terminal job events:

```bash
curl -sS -X POST http://localhost:8000/v1/webhooks \
  -H "X-API-Key: local-dev-key" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com/agentp/events",
    "events": [
      "job.succeeded",
      "job.completed_with_warnings",
      "job.failed",
      "job.canceled"
    ]
  }'
```

Omitting `events` registers all four. A packet that finished with warnings emits
`job.completed_with_warnings`, not `job.succeeded`, so an endpoint that subscribes only to
`job.succeeded` will never hear about it.

The response returns a `signing_secret` once. Each delivery includes
`X-AgentP-Delivery`, `X-AgentP-Event`, `X-AgentP-Timestamp`, and
`X-AgentP-Signature` headers. Verify the signature as an HMAC-SHA256 over
`<timestamp>.<raw-request-body>` and compare it using a constant-time function.

Inspect delivery attempts or disable an endpoint:

```bash
curl -sS 'http://localhost:8000/v1/webhooks/deliveries?status=failed' \
  -H "X-API-Key: local-dev-key"
```

```bash
curl -sS -X POST http://localhost:8000/v1/webhooks/wh_.../disable \
  -H "X-API-Key: local-dev-key"
```

Deliveries are retried with backoff. A webhook failure is recorded but does not
change the completed job's status.

Webhook targets must resolve to a public address. Private, loopback, link-local,
reserved, multicast, and unspecified addresses are refused with
`WEBHOOK_TARGET_NOT_ALLOWED` — otherwise any holder of the API key could aim deliveries at
cloud instance metadata or internal services and read the result back out of the delivery
log. The check runs at registration and again at delivery, so a hostname that is later
repointed inward stops being delivered to.

To receive webhooks on `localhost` during development:

```bash
AGENTPDF_WEBHOOK_ALLOW_PRIVATE_URLS=true
```

A hostname that does not resolve is accepted at registration; the delivery attempt fails
on its own if it stays unreachable.

## MCP Tools

The MCP server exposes strongly typed tools:

- `list_operations()`
- `list_jobs(status, limit, offset)`
- `cancel_job(job_id)`
- `merge_pdfs(file_ids, ocr_if_needed, language, deskew, idempotency_key)`
- `prepare_packet(file_ids, order, language, deskew, idempotency_key)`
- `split_pdf(file_id, page_ranges, idempotency_key)`
- `ocr_pdf(file_id, language, deskew, idempotency_key)`
- `compress_pdf(file_id, preset, idempotency_key)`
- `extract_text(file_id, include_coordinates, idempotency_key)`
- `get_job(job_id)`

Every operation tool returns:

```json
{
  "job_id": "job_...",
  "status": "queued",
  "next_action": {
    "tool": "get_job",
    "arguments": {"job_id": "job_..."}
  }
}
```

## Security Baseline

Implemented in v0:

- API key required for REST endpoints.
- Upload size limit.
- Page count limit.
- Immutable input and output storage keys.
- Temporary per-job workspace cleanup.
- Allowlisted operations and parameters.
- Webhook targets restricted to public addresses.
- Subprocess calls use argument arrays, not shell strings.
- Structural validation after PDF operations.
- Audit events for job creation, enqueue, validation, success, and failure.

Not in v0:

- User accounts and organizations.
- Billing.
- True redaction.
- Electronic signatures.
- Natural-language workflow planning.

## Development

Run syntax checks without external services:

```bash
make test
```

Apply database migrations manually:

```bash
make db-upgrade
```

Run the full local Python checks after installing dependencies:

```bash
python -m ruff check app tests worker
python -m pytest
```

Run tests inside the application container, including native PDF operation tests:

```bash
make container-test
```

Run the API directly, assuming Postgres, Redis, and MinIO are reachable:

```bash
uvicorn app.main:app --reload
```
