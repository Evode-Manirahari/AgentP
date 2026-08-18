# AgentP

AgentP turns a disorganized business-document packet into one searchable, correctly ordered,
validated PDF plus a machine-readable audit report. It is the deterministic document
execution layer behind an AI agent: the agent decides what each document means; AgentP
enforces the packet contract and proves what it produced.

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
9. Isolate every file, job, webhook, and object-storage key by workspace.
10. Enforce workspace storage, document, concurrency, and hourly-job limits.

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
- `GET /ready` checks Postgres, Redis, object storage, and that at least one live RQ worker
  is consuming the configured PDF queue.

Local bootstrap API key:

```text
X-API-Key: local-dev-key
```

On the first successful database startup, AgentP hashes this configured value into a
platform-administrator key in the default workspace. The plaintext is never stored. Once
any key exists in that workspace, changing `AGENTPDF_API_KEY` does not rotate or recreate
it; use the key lifecycle below. Keep the development default out of deployed environments.

## Workspaces and API Keys

Every API key belongs to one workspace. Its authentication context scopes files, jobs,
idempotency keys, webhook endpoints, delivery history, audit events, and object-storage
paths. A caller asking for another workspace's object receives the same not-found response
as it would for an unknown ID.

Only a platform-administrator key can create a workspace. The response contains the new
workspace's first ordinary key, and the token is shown once:

```bash
curl -sS -X POST http://localhost:8000/v1/workspaces \
  -H "X-API-Key: local-dev-key" \
  -H "Content-Type: application/json" \
  -d '{"name": "Acme Lending", "initial_key_name": "production"}'
```

An authenticated caller can inspect its current workspace and manage keys only inside that
workspace:

```bash
curl -sS http://localhost:8000/v1/workspaces/current \
  -H "X-API-Key: $AGENTP_KEY"

curl -sS -X POST http://localhost:8000/v1/api-keys \
  -H "X-API-Key: $AGENTP_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "automation"}'

curl -sS http://localhost:8000/v1/api-keys \
  -H "X-API-Key: $AGENTP_KEY"
```

Rotate before switching clients so there is no credential gap. Rotation atomically creates
a replacement, returns its token once, and revokes the old key:

```bash
curl -sS -X POST http://localhost:8000/v1/api-keys/key_.../rotate \
  -H "X-API-Key: $AGENTP_KEY"
```

Revoke a key after another active key is confirmed working:

```bash
curl -sS -X POST http://localhost:8000/v1/api-keys/key_.../revoke \
  -H "X-API-Key: $AGENTP_KEY"
```

AgentP refuses to revoke a workspace's last active key. Ordinary workspace keys cannot
rotate or revoke a platform-administrator key. Key creation, rotation, and revocation are
serialized per workspace so concurrent requests cannot bypass those safeguards.

For operator recovery with direct database access, use the break-glass CLI:

```bash
python -m app.provision create-workspace "Acme Lending"
python -m app.provision create-key ws_... --name automation
python -m app.provision list-keys ws_...
python -m app.provision rotate-key ws_... key_...
python -m app.provision revoke-key ws_... key_...
```

CLI create and rotate commands also print a token only once. Treat their JSON output as a
secret and move it directly into a secret manager.

## Usage and Admission Limits

Every workspace has transaction-safe admission limits. The defaults are 10 GiB of live
documents, 10,000 live document records, 25 active jobs, and 1,000 new jobs in a rolling
hour. Uploaded inputs and generated outputs both count; deleted document records keep their
provenance but no longer consume document or byte capacity.

```bash
curl -sS http://localhost:8000/v1/usage \
  -H "X-API-Key: $AGENTP_API_KEY"
```

The response reports `used`, `limit`, `remaining`, `utilization`, and `exhausted` for
storage bytes, documents, active jobs, and jobs in the last hour. It also includes job
outcomes and the terminal failure rate for the last 24 hours. The MCP `get_usage()` tool
returns the same workspace-scoped model.

Configure the limits per deployment:

```text
AGENTPDF_WORKSPACE_STORAGE_LIMIT_BYTES=10737418240
AGENTPDF_WORKSPACE_DOCUMENT_LIMIT=10000
AGENTPDF_WORKSPACE_ACTIVE_JOB_LIMIT=25
AGENTPDF_WORKSPACE_JOBS_PER_HOUR_LIMIT=1000
```

Capacity failures use stable codes:

- `WORKSPACE_STORAGE_LIMIT_EXCEEDED` and `WORKSPACE_DOCUMENT_LIMIT_EXCEEDED` require
  deleting stored files or raising capacity.
- `WORKSPACE_ACTIVE_JOB_LIMIT_EXCEEDED` is retryable after a job finishes or a queued job
  is canceled.
- `WORKSPACE_JOB_RATE_LIMIT_EXCEEDED` is retryable after the rolling hour clears.

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

An upstream AI agent can label documents semantically and let AgentP enforce ordering and
completeness without pretending a deterministic PDF engine can classify business meaning:

```bash
curl -sS -X POST http://localhost:8000/v1/jobs \
  -H "X-API-Key: $AGENTP_API_KEY" \
  -H "Idempotency-Key: demo-semantic-packet-001" \
  -H "Content-Type: application/json" \
  -d '{
    "operation": "prepare_packet",
    "inputs": [
      {"file_id": "file_statement_march", "label": "bank_statement"},
      {"file_id": "file_identity", "label": "identity"},
      {"file_id": "file_application", "label": "application"},
      {"file_id": "file_statement_april", "label": "bank_statement"}
    ],
    "parameters": {
      "order": "manifest",
      "manifest": [
        {"label": "application", "min_count": 1, "max_count": 1},
        {"label": "identity", "min_count": 1, "max_count": 2},
        {"label": "bank_statement", "min_count": 1, "max_count": 12}
      ]
    }
  }'
```

Manifest sections define output order. Repeated labels keep their original relative order.
`min_count` defaults to 1; `max_count` defaults to unlimited. Unknown labels fail closed
with `PACKET_LABEL_NOT_IN_MANIFEST`, or can be appended in original order by setting
`allow_unlisted: true`. Missing or excessive sections fail before queueing with
`PACKET_MANIFEST_COUNT_MISMATCH`.

It runs four recorded steps:

1. `inspect` — page count, SHA-256, and scan detection for every input.
2. `ocr` — OCR only the inputs that have no usable text layer.
3. `organize` — order documents by `as_provided` (default), `filename`, or semantic
   `manifest`.
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

The job only reaches a success state after the packet contains every input page, the audit
report describes every document, and ordering evidence agrees across the result metadata,
PDF output, and report. Manifest jobs additionally prove every declared section is
satisfied. A mismatch fails with a stable validation code instead of returning a silent
partial result.

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

List uploaded and produced files, newest first, optionally filtered by status:

```bash
curl -sS 'http://localhost:8000/v1/files?status=validated&limit=20' \
  -H "X-API-Key: local-dev-key"
```

Each entry carries the checksum, page count, status, and `source_job_id` for files a job
produced, so a caller that lost a `file_id` can find it again.

## Deleting Documents

```bash
curl -sS -X DELETE http://localhost:8000/v1/files/file_... \
  -H "X-API-Key: local-dev-key"
```

Deletion purges the stored bytes and keeps the record. The document row survives with
`status: "deleted"` and a `deleted_at` timestamp, so the checksum, filename, page count,
and the job trail that produced it stay intact — the bytes go, the provenance does not.
Removing an output also records an `output.deleted` event on the job that produced it.

- Reading a deleted file returns `410 FILE_DELETED` rather than `404`, so a caller can
  tell "purged" apart from "never existed".
- A job listing a deleted file as an output still lists it, with `status: "deleted"` and a
  null `download_url`.
- Deleting is idempotent. Deleting an already-deleted file returns `200`.
- A file that a queued, running, or validating job needs as an input is refused with
  `409 FILE_IN_USE` and the blocking job IDs. Finish or cancel those jobs first.
- A deleted file cannot be an input to a new job (`FILE_NOT_VALIDATED`).

### Retention

Retention is off by default: documents are kept until deleted explicitly. Set a window to
purge older ones:

```bash
AGENTPDF_DOCUMENT_RETENTION_DAYS=30
```

The sweep is caller-scheduled rather than self-scheduling, so it fits cron, a Kubernetes
CronJob, or a one-off run without adding a scheduler to the stack:

```bash
make retention
# or, against any environment:
python -m worker.retention
```

It purges through the same path as an explicit delete — bytes gone, row retired, job trail
intact — and tags the audit event with `"reason": "retention"` so a scheduled purge is
distinguishable from one a caller asked for. Inputs belonging to a queued, running, or
validating job are left alone no matter how old they are. Each run prints a summary:

```json
{"cutoff": "2026-07-09T12:00:00+00:00", "examined": 12, "purged": 11,
 "skipped_in_use": 1, "purged_file_ids": ["file_..."]}
```

Running it with retention unset is a no-op, so it is safe to schedule before deciding on a
window.

Idempotency keys are workspace-scoped. Reusing the same `Idempotency-Key` with the same
operation, inputs, and parameters returns the existing job. Reusing the key for a different
request in that workspace returns an `IDEMPOTENCY_KEY_CONFLICT` error. Two workspaces can
use the same key independently. Requests that race on the same key inside one workspace
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
- `list_files(status, limit, offset)`
- `list_jobs(status, limit, offset)`
- `get_usage()`
- `cancel_job(job_id)`
- `merge_pdfs(file_ids, ocr_if_needed, language, deskew, idempotency_key)`
- `prepare_packet(file_ids, order, language, deskew, input_labels, manifest,
  allow_unlisted, idempotency_key)`
- `split_pdf(file_id, page_ranges, idempotency_key)`
- `ocr_pdf(file_id, language, deskew, idempotency_key)`
- `compress_pdf(file_id, preset, idempotency_key)`
- `extract_text(file_id, include_coordinates, idempotency_key)`
- `get_job(job_id)`

MCP HTTP requests use the same `X-API-Key` header and workspace isolation as REST requests.

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

- Hashed, revocable API keys required for REST and MCP endpoints.
- Workspace isolation for database reads, writes, idempotency, webhooks, and storage paths.
- Platform-administrator capability required to create workspaces.
- Upload size limit.
- Page count limit.
- Transaction-safe workspace storage, document, active-job, and hourly-job limits.
- Immutable input and output storage keys.
- Temporary per-job workspace cleanup.
- Allowlisted operations and parameters.
- Webhook targets restricted to public addresses.
- Caller-driven document deletion that purges stored bytes.
- Subprocess calls use argument arrays, not shell strings.
- Structural validation after PDF operations.
- Audit events for job creation, enqueue, validation, success, and failure.

Not in v0:

- User accounts and interactive login.
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
