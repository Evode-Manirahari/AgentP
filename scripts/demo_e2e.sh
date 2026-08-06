#!/usr/bin/env bash
set -euo pipefail

API_URL="${API_URL:-http://localhost:8000}"
API_KEY="${API_KEY:-local-dev-key}"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

docker compose up -d --build

docker compose run --rm -v "${TMP_DIR}:/out" api python - <<'PY'
from pathlib import Path

import fitz

for index, text in enumerate(["AgentP demo packet A", "AgentP demo packet B"], start=1):
    path = Path("/out") / f"agentp-demo-{index}.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()
PY

upload() {
  local file_path="$1"
  curl -sS -X POST "${API_URL}/v1/files" \
    -H "X-API-Key: ${API_KEY}" \
    -F "file=@${file_path}"
}

file_1_json="$(upload "${TMP_DIR}/agentp-demo-1.pdf")"
file_2_json="$(upload "${TMP_DIR}/agentp-demo-2.pdf")"

file_1_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["file_id"])' <<<"${file_1_json}")"
file_2_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["file_id"])' <<<"${file_2_json}")"

job_json="$(
  curl -sS -X POST "${API_URL}/v1/jobs" \
    -H "X-API-Key: ${API_KEY}" \
    -H "Idempotency-Key: demo-merge-$(date +%s)" \
    -H "Content-Type: application/json" \
    -d "{\"operation\":\"merge\",\"inputs\":[{\"file_id\":\"${file_1_id}\"},{\"file_id\":\"${file_2_id}\"}],\"parameters\":{\"ocr_if_needed\":false}}"
)"
job_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])' <<<"${job_json}")"

for _ in $(seq 1 30); do
  response="$(
    curl -sS "${API_URL}/v1/jobs/${job_id}" \
      -H "X-API-Key: ${API_KEY}"
  )"
  status="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' <<<"${response}")"
  if [[ "${status}" == "succeeded" || "${status}" == "failed" ]]; then
    echo "${response}" | python3 -m json.tool
    exit 0
  fi
  sleep 1
done

echo "Timed out waiting for ${job_id}" >&2
exit 1

