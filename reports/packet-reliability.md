# prepare_packet reliability

Generated 2026-08-18T21:35:01.775771+00:00.

## Headline

| Measure | Value |
| --- | --- |
| Cases attempted | 20 of 20 |
| Packet produced | 100.0% |
| Output validated | 100.0% |
| Ordering correct | 100.0% |
| Pages conserved | 100.0% |
| All checks passed | 100.0% |
| Text loss reported, never silent | 100.0% |
| Text recall (mean) | 0.90 |
| Text recall (worst case) | 0.33 |

## Scanned-page detection

The decision that gates OCR. A miss produces an unsearchable packet that still
reports success, so recall matters more than accuracy here.

| Measure | Value |
| --- | --- |
| Documents labelled | 47 |
| Accuracy | 100.0% |
| Precision | 100.0% |
| Recall | 100.0% |
| Missed scans (false negative) | 0 |
| Over-eager OCR (false positive) | 0 |

## Failure taxonomy

No failures.

## Cases

| Case | Status | Recall | Notes |
| --- | --- | --- | --- |
| `clean_digital_pair` | passed | 1.00 |  |
| `digital_many_inputs` | passed | 1.00 |  |
| `filename_reorder` | passed | 1.00 |  |
| `filename_reorder_mixed_case` | passed | 1.00 |  |
| `semantic_manifest_reorder` | passed | 1.00 |  |
| `mixed_page_sizes` | passed | 1.00 |  |
| `rotated_digital_pages` | passed | 1.00 |  |
| `large_packet` | passed | 1.00 |  |
| `borderline_text_density` | passed | 1.00 |  |
| `single_scanned_input` | passed | 1.00 |  |
| `all_scanned_inputs` | passed | 1.00 |  |
| `skewed_scan` | passed | 1.00 |  |
| `noisy_scan` | passed | 1.00 |  |
| `low_resolution_scan` | passed | 1.00 |  |
| `rotated_scan` | passed | 1.00 |  |
| `blank_pages` | passed | 1.00 |  |
| `heavy_speckle_photocopy` | passed | 0.33 |  |
| `severe_skew` | passed | 0.33 |  |
| `fax_resolution_small_print` | passed | 0.50 |  |
| `unreadable_resolution` | passed |  |  |
