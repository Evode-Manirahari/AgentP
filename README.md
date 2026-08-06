# AgentP

AgentP is a typed, asynchronous, verifiable PDF execution API for AI agents.

Positioning: the document execution layer for AI agents. Upload business documents, run allowlisted operations, verify the output, and retrieve an immutable artifact with an audit trail.

## What v0 Builds

The first product path is intentionally narrow:

1. Upload PDFs.
2. Validate structure with `qpdf`, page counting, and a render probe.
3. Create an async job for `merge`, `split`, `ocr`, `compress`, or `extract_text`.
4. Run the job in an RQ worker using deterministic PDF tools.
5. Validate the output before marking the job complete.
6. Return short-lived download URLs, validation details, and audit events.

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

## Local Start

```bash
docker compose up --build
```

Services:

- API: `http://localhost:8000`
- Swagger UI: `http://localhost:8000/docs`
- MCP Streamable HTTP endpoint: `http://localhost:8000/mcp`
- MinIO console: `http://localhost:9001`

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

Poll the job:

```bash
curl -sS http://localhost:8000/v1/jobs/job_... \
  -H "X-API-Key: local-dev-key"
```

The response includes:

- `outputs[].download_url`
- `validation`
- `audit`
- structured `error` if the job failed

Idempotency keys are request-scoped. Reusing the same `Idempotency-Key` with the same
operation, inputs, and parameters returns the existing job. Reusing the key for a different
request returns an `IDEMPOTENCY_KEY_CONFLICT` error.

In Docker Compose, signed download URLs use `AGENTPDF_S3_PUBLIC_ENDPOINT_URL=http://localhost:9000` so links returned to host clients are reachable outside the Docker network.

## MCP Tools

The MCP server exposes strongly typed tools:

- `merge_pdfs(file_ids, ocr_if_needed, language, deskew, idempotency_key)`
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
- Subprocess calls use argument arrays, not shell strings.
- Structural validation after PDF operations.
- Audit events for job creation, enqueue, validation, success, and failure.

Not in v0:

- User accounts and organizations.
- Billing.
- Webhooks.
- True redaction.
- Electronic signatures.
- Natural-language workflow planning.

## Development

Run syntax checks without external services:

```bash
make test
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
