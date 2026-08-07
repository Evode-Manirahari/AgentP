#!/usr/bin/env bash
# Flagship workflow demo: an unorganized onboarding packet in, one validated
# searchable PDF plus an audit report out.
set -euo pipefail

API_URL="${API_URL:-http://localhost:8000}"
API_KEY="${API_KEY:-local-dev-key}"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

json_field() {
  python3 -c 'import json,sys; print(json.load(sys.stdin)["'"$1"'"])'
}

wait_for_api() {
  for _ in $(seq 1 60); do
    if curl -fsS --max-time 2 "${API_URL}/ready" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done

  docker compose logs --no-color --tail=80 api >&2 || true
  echo "Timed out waiting for API readiness at ${API_URL}/ready" >&2
  return 1
}

docker compose up -d --build
wait_for_api

# Build a packet that looks like a real one: two digital documents and one scan
# with no text layer, uploaded out of order.
docker compose run --rm -v "${TMP_DIR}:/out" api python - <<'PY'
from pathlib import Path

import fitz

BODY = (
    "This document is part of a customer onboarding packet and carries enough "
    "machine readable text to be treated as a digital original."
)


def write_digital(path: Path, title: str, pages: int) -> None:
    document = fitz.open()
    for index in range(pages):
        page = document.new_page()
        page.insert_text((72, 72), f"{title} - page {index + 1}")
        page.insert_text((72, 100), BODY)
    document.save(path)
    document.close()


def write_scan(path: Path, title: str) -> None:
    """Render a page to an image so it carries no extractable text layer."""
    source = fitz.open()
    page = source.new_page()
    page.insert_text((72, 72), f"{title} - page 1")
    page.insert_text((72, 100), BODY)
    pixmap = page.get_pixmap(dpi=200)
    source.close()

    scan = fitz.open()
    scan_page = scan.new_page(width=pixmap.width * 72 / 200, height=pixmap.height * 72 / 200)
    scan_page.insert_image(scan_page.rect, pixmap=pixmap)
    scan.save(path)
    scan.close()


out = Path("/out")
write_digital(out / "03-bank-statement.pdf", "Bank statement", 1)
write_digital(out / "01-application.pdf", "Application form", 2)
write_scan(out / "02-id-card.pdf", "Identity card")
PY

upload() {
  curl -sS -X POST "${API_URL}/v1/files" \
    -H "X-API-Key: ${API_KEY}" \
    -F "file=@$1"
}

echo "==> Uploading three documents out of order"
statement_id="$(upload "${TMP_DIR}/03-bank-statement.pdf" | json_field file_id)"
application_id="$(upload "${TMP_DIR}/01-application.pdf" | json_field file_id)"
id_card_id="$(upload "${TMP_DIR}/02-id-card.pdf" | json_field file_id)"

echo "==> Requesting prepare_packet"
job_id="$(
  curl -sS -X POST "${API_URL}/v1/jobs" \
    -H "X-API-Key: ${API_KEY}" \
    -H "Idempotency-Key: demo-packet-$(date +%s)" \
    -H "Content-Type: application/json" \
    -d "{
          \"operation\":\"prepare_packet\",
          \"inputs\":[
            {\"file_id\":\"${statement_id}\"},
            {\"file_id\":\"${application_id}\"},
            {\"file_id\":\"${id_card_id}\"}
          ],
          \"parameters\":{\"order\":\"filename\",\"language\":\"eng\"}
        }" | json_field job_id
)"

echo "==> Polling ${job_id}"
for _ in $(seq 1 120); do
  response="$(curl -sS "${API_URL}/v1/jobs/${job_id}" -H "X-API-Key: ${API_KEY}")"
  status="$(json_field status <<<"${response}")"

  if [[ "${status}" == "succeeded" || "${status}" == "completed_with_warnings" ]]; then
    echo "==> Job finished with status: ${status}"
    packet_id="$(
      python3 -c 'import json,sys
job = json.load(sys.stdin)
print(next(o["file_id"] for o in job["outputs"] if o["filename"] == "packet.pdf"))' \
        <<<"${response}"
    )"
    report_id="$(
      python3 -c 'import json,sys
job = json.load(sys.stdin)
print(next(o["file_id"] for o in job["outputs"] if o["mime_type"] == "application/json"))' \
        <<<"${response}"
    )"

    curl -fsS -L "${API_URL}/v1/files/${packet_id}/content" \
      -H "X-API-Key: ${API_KEY}" -o "${TMP_DIR}/packet.pdf"
    curl -fsS -L "${API_URL}/v1/files/${report_id}/content" \
      -H "X-API-Key: ${API_KEY}" -o "${TMP_DIR}/packet-audit-report.json"

    docker compose run --rm -v "${TMP_DIR}:/out" api python - <<'PY'
from pathlib import Path

import fitz

packet = Path("/out/packet.pdf")
assert packet.read_bytes().startswith(b"%PDF"), "packet is not a PDF"
with fitz.open(packet) as document:
    pages = document.page_count
    text = "\n".join(page.get_text("text") for page in document)

assert pages == 4, f"expected 4 pages, found {pages}"
assert "Identity card" in text, "the scanned page did not become searchable"
print(f"Packet verified: {pages} pages, scanned page is searchable after OCR.")
PY

    echo
    echo "==> Validation and assertions"
    python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["validation"]["assertions"], indent=2))' \
      <<<"${response}"

    echo
    echo "==> Audit report"
    python3 -m json.tool "${TMP_DIR}/packet-audit-report.json"

    echo
    echo "==> Job audit trail"
    python3 -c 'import json,sys; print("\n".join(e["event_type"] for e in json.load(sys.stdin)["audit"]))' \
      <<<"${response}"
    exit 0
  fi

  if [[ "${status}" == "failed" ]]; then
    echo "${response}" | python3 -m json.tool
    exit 1
  fi
  sleep 1
done

echo "Timed out waiting for ${job_id}" >&2
exit 1
