# prepare_packet reliability

Measures the flagship workflow — messy documents in, one validated searchable packet plus
an audit report out — and produces the numbers you would put in front of a design partner.

```bash
python -m evals.packet_reliability
python -m evals.packet_reliability --markdown-out reports/packet-reliability.md
```

## What it measures

| Measure | Question it answers |
| --- | --- |
| Packet produced | Did the workflow return a packet at all, or raise? |
| Output validated | Did the packet pass the same validator the production worker runs? |
| Pages conserved | Did any page get dropped or duplicated in the merge? |
| Ordering correct | Did the documents come out in the order the caller asked for? |
| Manifest contract | Were semantic labels recorded and all required sections satisfied? |
| Scanned detection | Did it recognise which inputs had no text layer? |
| OCR application | Did OCR run on exactly those inputs, and no others? |
| Text recall | How much known content survived into the searchable packet? |
| Warnings | Did it report the problems it is supposed to report? |

Scanned detection is reported as a confusion matrix, not a single accuracy figure. The two
errors are not equally bad: a **false negative** ships an unsearchable packet that still
reports success, which is the failure a customer discovers weeks later. A false positive
only wastes OCR time.

Text recall is reported as a distribution rather than pass/fail. Partial recall is the
normal outcome on real scans, and the useful artefact is the spread — particularly the
worst case, which is what a customer will hit eventually.

## The synthetic corpus is a floor, not a reliability claim

`corpus.py` generates documents with known ground truth: digital text, rasterized
"scans" with no text layer, rotation, skew, speckle, low resolution, mixed page sizes,
blank separator sheets, and text density near the detection threshold.

**These are cleaner than real scans.** A good number here means the pipeline is not broken.
It does not mean the pipeline works on a lender's fax archive. Do not publish a synthetic
number as a reliability figure — publish it as a regression baseline, which is what it is.

## Scoring real documents

Point the harness at a directory of real PDFs with a `manifest.json`. Real cases run
through exactly the same scorers as the synthetic ones.

```json
[
  {
    "case_id": "acme-onboarding-01",
    "description": "Real packet from Acme, received 2026-08-01",
    "order": "manifest",
    "manifest": [
      {"label": "application", "min_count": 1, "max_count": 1},
      {"label": "identity", "min_count": 1, "max_count": 2}
    ],
    "inputs": [
      {"path": "acme/01-application.pdf", "label": "application",
       "pages": 3, "scanned": false,
       "tokens": ["POL-48213", "ZEPHYR"]},
      {"path": "acme/02-id.pdf", "label": "identity",
       "pages": 1, "scanned": true,
       "tokens": ["MERIDIAN"]}
    ]
  }
]
```

```bash
python -m evals.packet_reliability --corpus ./corpus --real-only \
  --markdown-out reports/real-packet-reliability.md
```

Three fields carry the ground truth you have to supply by hand:

- `pages` — the true page count, which makes page conservation checkable.
- `scanned` — whether the document truly lacks a text layer, which makes detection
  scoreable. Label what the document *is*, not what AgentP says it is.
- `tokens` — distinctive strings you know appear in the document (a policy number, a
  surname). These drive text recall. Pick strings that are unambiguous when found.
- `label` plus the top-level `manifest` — optional semantic ordering and completeness
  ground truth. Use them together with `"order": "manifest"`.

Keep real customer documents out of the repository. Point `--corpus` at a directory
outside the working tree.

## OCR toolchain

OCR cases need `tesseract` and `ghostscript` on the PATH, which `ocrmypdf` shells out to.
When they are missing, those cases are reported as **skipped** rather than failed, and the
report says so — a skipped OCR case must never be mistaken for a passing one.

```bash
brew install tesseract ghostscript        # macOS
apt-get install tesseract-ocr ghostscript # Debian/Ubuntu
```

## In CI

`--fail-under` turns the suite into a regression gate once you have a baseline worth
defending:

```bash
python -m evals.packet_reliability --fail-under 0.95
```

It exits non-zero if any case raised an unexpected error, or if the all-checks-passed rate
drops below the threshold. Set the threshold from a measured baseline, not from an
aspiration.

## Inspecting failures

`--keep-artifacts DIR` preserves the generated inputs and the produced packet per case, so
a failure can be opened and looked at rather than inferred from a number.

```bash
python -m evals.packet_reliability --case noisy_scan --keep-artifacts ./artifacts
```
