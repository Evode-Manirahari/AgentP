from __future__ import annotations

from copy import deepcopy
from typing import Any

OPERATION_CATALOG: list[dict[str, Any]] = [
    {
        "name": "merge",
        "description": "Merge two or more PDFs into one output PDF.",
        "input_count_min": 2,
        "input_count_max": None,
        "parameters": [
            {
                "name": "ocr_if_needed",
                "type": "boolean",
                "required": False,
                "default": False,
                "description": "Run OCR on scanned inputs before merging.",
            },
            {
                "name": "language",
                "type": "string",
                "required": False,
                "default": "eng",
                "description": "OCR language code passed to OCRmyPDF.",
            },
            {
                "name": "deskew",
                "type": "boolean",
                "required": False,
                "default": True,
                "description": "Deskew pages when OCR is applied.",
            },
        ],
    },
    {
        "name": "prepare_packet",
        "description": (
            "Prepare a document packet: inspect inputs, OCR scanned documents, organize them, "
            "merge them into one validated PDF, and return an audit report."
        ),
        "input_count_min": 2,
        "input_count_max": None,
        "parameters": [
            {
                "name": "order",
                "type": "string",
                "required": False,
                "default": "as_provided",
                "allowed_values": ["as_provided", "filename"],
                "description": "How to order documents before merging.",
            },
            {
                "name": "language",
                "type": "string",
                "required": False,
                "default": "eng",
                "description": "OCR language code passed to OCRmyPDF.",
            },
            {
                "name": "deskew",
                "type": "boolean",
                "required": False,
                "default": True,
                "description": "Deskew pages when OCR is applied.",
            },
        ],
    },
    {
        "name": "split",
        "description": "Split one PDF into one output PDF per requested page range.",
        "input_count_min": 1,
        "input_count_max": 1,
        "parameters": [
            {
                "name": "page_ranges",
                "type": "array<string>",
                "required": True,
                "default": None,
                "description": "Page ranges such as '1', '2-4', '-3', or '5-'.",
            }
        ],
    },
    {
        "name": "ocr",
        "description": "Create a searchable OCR PDF from one input PDF.",
        "input_count_min": 1,
        "input_count_max": 1,
        "parameters": [
            {
                "name": "language",
                "type": "string",
                "required": False,
                "default": "eng",
                "description": "OCR language code passed to OCRmyPDF.",
            },
            {
                "name": "deskew",
                "type": "boolean",
                "required": False,
                "default": True,
                "description": "Deskew pages during OCR.",
            },
        ],
    },
    {
        "name": "compress",
        "description": "Compress one PDF using a Ghostscript preset.",
        "input_count_min": 1,
        "input_count_max": 1,
        "parameters": [
            {
                "name": "preset",
                "type": "string",
                "required": False,
                "default": "ebook",
                "allowed_values": ["screen", "ebook", "print"],
                "description": "Compression target quality.",
            }
        ],
    },
    {
        "name": "extract_text",
        "description": "Extract page-level PDF text into a JSON artifact.",
        "input_count_min": 1,
        "input_count_max": 1,
        "parameters": [
            {
                "name": "include_coordinates",
                "type": "boolean",
                "required": False,
                "default": False,
                "description": "Include text block bounding boxes in the JSON output.",
            }
        ],
    },
]


def list_operation_specs() -> list[dict[str, Any]]:
    return deepcopy(OPERATION_CATALOG)
