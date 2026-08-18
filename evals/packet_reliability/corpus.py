"""Case definitions and the synthetic document generator.

The generator exists so the harness has something to measure on a laptop with no customer
data. Synthetic pages are a floor, not a reliability claim: they are cleaner than real
scans, and a number produced here is only evidence that the pipeline is not broken. The
customer-facing figure has to come from real documents, which is why `load_manifest_cases`
lets a directory of real PDFs be scored by exactly the same scorers.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

PAGE_SIZES = {
    "letter": (612.0, 792.0),
    "a4": (595.0, 842.0),
    "legal": (612.0, 1008.0),
}


@dataclass(frozen=True)
class InputSpec:
    """One input document in a packet, and how ugly it should be."""

    filename: str
    pages: int = 1
    scanned: bool = False
    rotation: int = 0
    skew_degrees: float = 0.0
    noise: float = 0.0
    dpi: int = 150
    font_size: float = 14.0
    page_size: str = "letter"
    tokens: tuple[str, ...] = ()
    blank: bool = False

    @property
    def expects_ocr(self) -> bool:
        """A rasterized page carries no text layer, so the pipeline should OCR it."""
        return self.scanned


@dataclass(frozen=True)
class PacketCase:
    case_id: str
    description: str
    inputs: tuple[InputSpec, ...]
    order: str = "as_provided"
    input_labels: tuple[str, ...] = ()
    manifest: tuple[dict[str, Any], ...] = ()
    allow_unlisted: bool = False
    expected_sequence: tuple[int, ...] = ()
    expected_warning_codes: tuple[str, ...] = ()
    # Some inputs are beyond what OCR can read. Refusing them with a known code is the
    # correct outcome, so the case passes when that code is what comes back.
    expected_error_code: str | None = None
    # Degraded scans lose text no matter what. What matters is that the loss is reported,
    # so recall below this is tolerated as long as a warning was raised.
    tolerate_text_loss: bool = False
    tags: tuple[str, ...] = ()
    real_paths: tuple[Path, ...] = field(default=())

    @property
    def expected_total_pages(self) -> int:
        return sum(item.pages for item in self.inputs)

    @property
    def expected_scanned_positions(self) -> set[int]:
        return {
            position
            for position, item in enumerate(self.inputs, start=1)
            if item.scanned or item.blank
        }

    @property
    def requires_ocr(self) -> bool:
        return any(item.expects_ocr or item.blank for item in self.inputs)

    def resolved_sequence(self) -> tuple[int, ...]:
        """What the pipeline should emit, derived from the ordering rule under test."""
        if self.expected_sequence:
            return self.expected_sequence
        if self.order == "filename":
            positions = sorted(
                range(1, len(self.inputs) + 1),
                key=lambda position: (self.inputs[position - 1].filename.lower(), position),
            )
            return tuple(positions)
        if self.order == "manifest":
            rank = {
                section["label"]: index for index, section in enumerate(self.manifest)
            }
            fallback_rank = len(rank)
            return tuple(
                sorted(
                    range(1, len(self.inputs) + 1),
                    key=lambda position: (
                        rank.get(self.input_labels[position - 1], fallback_rank),
                        position,
                    ),
                )
            )
        return tuple(range(1, len(self.inputs) + 1))

    def expected_tokens(self) -> dict[int, tuple[str, ...]]:
        return {
            position: item.tokens
            for position, item in enumerate(self.inputs, start=1)
            if item.tokens
        }


# --- Generation --------------------------------------------------------------------


def _page_text(spec: InputSpec, page_number: int) -> list[str]:
    lines = [
        f"{spec.filename.upper()} PAGE {page_number} OF {spec.pages}",
        "ONBOARDING PACKET DOCUMENT",
    ]
    lines.extend(spec.tokens)
    return lines


def _build_text_pdf(spec: InputSpec, destination: Path) -> None:
    import fitz

    width, height = PAGE_SIZES[spec.page_size]
    document = fitz.open()
    try:
        for page_number in range(1, spec.pages + 1):
            page = document.new_page(width=width, height=height)
            if spec.blank:
                continue
            y = 90.0
            for line in _page_text(spec, page_number):
                page.insert_text(
                    (72, y),
                    line,
                    fontsize=spec.font_size,
                    fontname="helv",
                )
                y += spec.font_size * 2.0
        # PyMuPDF normally adds a fresh trailer ID on every save. Suppressing it makes
        # generated fixtures byte-for-byte reproducible, including scanner noise seeded
        # below, so an eval result can be reproduced on another run.
        document.save(destination, no_new_id=True)
    finally:
        document.close()


def _degrade(png_bytes: bytes, spec: InputSpec, rng: random.Random) -> bytes:
    """Apply the scanner artefacts that make OCR hard: skew and speckle."""
    if spec.skew_degrees == 0.0 and spec.noise == 0.0:
        return png_bytes

    from PIL import Image

    image = Image.open(BytesIO(png_bytes)).convert("RGB")
    if spec.skew_degrees:
        image = image.rotate(
            spec.skew_degrees,
            resample=Image.BICUBIC,
            expand=True,
            fillcolor=(255, 255, 255),
        )
    if spec.noise:
        pixels = image.load()
        speckles = int(image.width * image.height * spec.noise)
        for _ in range(speckles):
            x = rng.randrange(image.width)
            y = rng.randrange(image.height)
            pixels[x, y] = (0, 0, 0)

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _build_scanned_pdf(spec: InputSpec, destination: Path, rng: random.Random) -> None:
    """Render a text PDF to images and rebuild it, leaving no text layer behind."""
    import fitz

    source_path = destination.with_name(f"{destination.stem}-source.pdf")
    _build_text_pdf(spec, source_path)

    source = fitz.open(source_path)
    scanned = fitz.open()
    try:
        for page in source:
            pixmap = page.get_pixmap(dpi=spec.dpi)
            png_bytes = _degrade(pixmap.tobytes("png"), spec, rng)
            new_page = scanned.new_page(width=page.rect.width, height=page.rect.height)
            new_page.insert_image(new_page.rect, stream=png_bytes)
            if spec.rotation:
                new_page.set_rotation(spec.rotation)
        scanned.save(destination, no_new_id=True)
    finally:
        source.close()
        scanned.close()
        source_path.unlink(missing_ok=True)


def materialize(case: PacketCase, directory: Path) -> list[Path]:
    """Write the case's input documents to disk and return them in submission order."""
    if case.real_paths:
        return list(case.real_paths)

    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, spec in enumerate(case.inputs):
        rng = random.Random(f"{case.case_id}:{spec.filename}:{index}")
        destination = directory / spec.filename
        if spec.scanned:
            _build_scanned_pdf(spec, destination, rng)
        else:
            _build_text_pdf(spec, destination)
            if spec.rotation:
                _apply_rotation(destination, spec.rotation)
        paths.append(destination)
    return paths


def _apply_rotation(path: Path, rotation: int) -> None:
    import fitz

    document = fitz.open(path)
    try:
        for page in document:
            page.set_rotation(rotation)
        document.saveIncr()
    finally:
        document.close()


# --- The corpus --------------------------------------------------------------------


def _digital(filename: str, **kwargs: object) -> InputSpec:
    return InputSpec(filename=filename, **kwargs)  # type: ignore[arg-type]


def _scan(filename: str, **kwargs: object) -> InputSpec:
    return InputSpec(filename=filename, scanned=True, **kwargs)  # type: ignore[arg-type]


def default_cases() -> list[PacketCase]:
    """The synthetic corpus. Each case isolates one way real packets are ugly."""
    return [
        PacketCase(
            case_id="clean_digital_pair",
            description="Two clean digital PDFs, submitted in the intended order.",
            inputs=(
                _digital("application.pdf", pages=2, tokens=("ZEPHYR", "POL-48213")),
                _digital("statement.pdf", pages=3, tokens=("QUANTUM", "ACCT-99120")),
            ),
            tags=("baseline", "digital"),
        ),
        PacketCase(
            case_id="digital_many_inputs",
            description="Five digital documents of varying length in one packet.",
            inputs=(
                _digital("a.pdf", pages=1, tokens=("ALPHA",)),
                _digital("b.pdf", pages=4, tokens=("BRAVO",)),
                _digital("c.pdf", pages=2, tokens=("CHARLIE",)),
                _digital("d.pdf", pages=6, tokens=("DELTA",)),
                _digital("e.pdf", pages=3, tokens=("ECHO",)),
            ),
            tags=("digital", "multi-input"),
        ),
        PacketCase(
            case_id="filename_reorder",
            description="Documents submitted out of order, reordered by filename.",
            inputs=(
                _digital("03-tax-return.pdf", pages=2, tokens=("GAMMA",)),
                _digital("01-application.pdf", pages=1, tokens=("ALPHA",)),
                _digital("02-id-card.pdf", pages=1, tokens=("BETA",)),
            ),
            order="filename",
            expected_sequence=(2, 3, 1),
            tags=("ordering",),
        ),
        PacketCase(
            case_id="filename_reorder_mixed_case",
            description="Filename ordering must be case-insensitive and stable.",
            inputs=(
                _digital("Bravo.pdf", pages=1, tokens=("BRAVO",)),
                _digital("alpha.pdf", pages=1, tokens=("ALPHA",)),
                _digital("CHARLIE.pdf", pages=1, tokens=("CHARLIE",)),
            ),
            order="filename",
            expected_sequence=(2, 1, 3),
            tags=("ordering",),
        ),
        PacketCase(
            case_id="semantic_manifest_reorder",
            description=(
                "Documents arrive in arbitrary order and are organized by caller-supplied "
                "semantic labels, with repeatable sections kept stable."
            ),
            inputs=(
                _digital("statement-march.pdf", pages=2, tokens=("MARCH",)),
                _digital("identity.pdf", pages=1, tokens=("IDENTITY",)),
                _digital("application.pdf", pages=2, tokens=("APPLICATION",)),
                _digital("statement-april.pdf", pages=3, tokens=("APRIL",)),
            ),
            order="manifest",
            input_labels=("statement", "identity", "application", "statement"),
            manifest=(
                {"label": "application", "min_count": 1, "max_count": 1},
                {"label": "identity", "min_count": 1, "max_count": 2},
                {"label": "statement", "min_count": 1},
            ),
            expected_sequence=(3, 2, 1, 4),
            tags=("ordering", "manifest"),
        ),
        PacketCase(
            case_id="mixed_page_sizes",
            description="Letter, A4, and legal pages merged into one packet.",
            inputs=(
                _digital("letter.pdf", pages=2, page_size="letter", tokens=("LETTERDOC",)),
                _digital("a4.pdf", pages=2, page_size="a4", tokens=("A4DOC",)),
                _digital("legal.pdf", pages=1, page_size="legal", tokens=("LEGALDOC",)),
            ),
            tags=("geometry",),
        ),
        PacketCase(
            case_id="rotated_digital_pages",
            description="Digital pages rotated 90 and 270 degrees.",
            inputs=(
                _digital("upright.pdf", pages=1, tokens=("UPRIGHT",)),
                _digital("sideways.pdf", pages=2, rotation=90, tokens=("SIDEWAYS",)),
                _digital("other-way.pdf", pages=1, rotation=270, tokens=("OTHERWAY",)),
            ),
            tags=("geometry", "rotation"),
        ),
        PacketCase(
            case_id="large_packet",
            description="A 40-page packet, to stress page conservation through the merge.",
            inputs=(
                _digital("part-one.pdf", pages=18, tokens=("PARTONE",)),
                _digital("part-two.pdf", pages=22, tokens=("PARTTWO",)),
            ),
            tags=("scale",),
        ),
        PacketCase(
            case_id="borderline_text_density",
            description=(
                "Sparse but genuinely digital text, near the scanned-detection threshold "
                "of 40 characters per page."
            ),
            inputs=(
                _digital("dense.pdf", pages=1, tokens=("DENSE", "PARAGRAPH", "CONTENT")),
                InputSpec(filename="sparse.pdf", pages=3, font_size=9.0, tokens=("X",)),
            ),
            tags=("detection", "threshold"),
        ),
        PacketCase(
            case_id="single_scanned_input",
            description="One scanned document alongside a digital one; OCR should hit only it.",
            inputs=(
                _digital("digital-form.pdf", pages=1, tokens=("DIGITAL", "FORM")),
                _scan("scanned-id.pdf", pages=1, tokens=("SCANNED", "IDENTITY")),
            ),
            tags=("ocr",),
        ),
        PacketCase(
            case_id="all_scanned_inputs",
            description="An entirely scanned packet, the common paper-office case.",
            inputs=(
                _scan("scan-one.pdf", pages=2, tokens=("MERIDIAN", "POLICY")),
                _scan("scan-two.pdf", pages=1, tokens=("HARBOUR", "INVOICE")),
            ),
            tags=("ocr",),
        ),
        PacketCase(
            case_id="skewed_scan",
            description="A scan fed through the feeder crooked; deskew should recover it.",
            inputs=(
                _digital("cover.pdf", pages=1, tokens=("COVER",)),
                _scan("crooked.pdf", pages=1, skew_degrees=3.5, tokens=("CROOKED", "LEDGER")),
            ),
            tags=("ocr", "skew"),
        ),
        PacketCase(
            case_id="noisy_scan",
            description="A speckled photocopy, the usual cause of OCR garbage.",
            inputs=(
                _digital("cover.pdf", pages=1, tokens=("COVER",)),
                _scan("speckled.pdf", pages=1, noise=0.004, tokens=("SPECKLED", "RECEIPT")),
            ),
            tags=("ocr", "noise"),
        ),
        PacketCase(
            case_id="low_resolution_scan",
            description="A 72 dpi scan, below what OCR reliably reads.",
            inputs=(
                _digital("cover.pdf", pages=1, tokens=("COVER",)),
                _scan("lowres.pdf", pages=1, dpi=72, font_size=9.0, tokens=("LOWRES",)),
            ),
            tags=("ocr", "resolution"),
        ),
        PacketCase(
            case_id="rotated_scan",
            description="A scan fed in sideways, then OCRed.",
            inputs=(
                _digital("cover.pdf", pages=1, tokens=("COVER",)),
                _scan("sideways-scan.pdf", pages=1, rotation=90, tokens=("SIDEWAYS", "DEED")),
            ),
            tags=("ocr", "rotation"),
        ),
        PacketCase(
            case_id="blank_pages",
            description=(
                "Separator sheets with no content. OCR cannot add text, so the packet should "
                "still be produced and the emptiness reported rather than silently passed."
            ),
            inputs=(
                _digital("cover.pdf", pages=1, tokens=("COVER",)),
                InputSpec(filename="separator.pdf", pages=2, blank=True),
            ),
            expected_warning_codes=("LOW_TEXT_AFTER_OCR",),
            tags=("ocr", "warnings", "edge"),
        ),
        # The cases below sit past the point where OCR stops recovering text, measured
        # rather than guessed. They are not asking OCR to succeed: they ask whether the
        # pipeline notices that it failed, which is the promise the product actually makes.
        PacketCase(
            case_id="heavy_speckle_photocopy",
            description=(
                "A heavily speckled photocopy at 5% noise. OCR cannot read it; the packet "
                "must come back flagged rather than quietly unsearchable."
            ),
            inputs=(
                _digital("cover.pdf", pages=1, tokens=("COVER",)),
                _scan("photocopy.pdf", pages=1, noise=0.05, tokens=("MERIDIAN", "POL-48213")),
            ),
            expected_warning_codes=("LOW_TEXT_AFTER_OCR",),
            tolerate_text_loss=True,
            tags=("ocr", "noise", "degraded"),
        ),
        PacketCase(
            case_id="severe_skew",
            description="A page fed in at 25 degrees, well past what deskew recovers.",
            inputs=(
                _digital("cover.pdf", pages=1, tokens=("COVER",)),
                _scan("askew.pdf", pages=1, skew_degrees=25.0, tokens=("HARBOUR", "ACCT-99120")),
            ),
            expected_warning_codes=("LOW_TEXT_AFTER_OCR",),
            tolerate_text_loss=True,
            tags=("ocr", "skew", "degraded"),
        ),
        PacketCase(
            case_id="fax_resolution_small_print",
            description="6pt print at fax resolution, below what OCR resolves.",
            inputs=(
                _digital("cover.pdf", pages=1, tokens=("COVER",)),
                _scan("faxed.pdf", pages=1, dpi=72, font_size=6.0, tokens=("ZEPHYR",)),
            ),
            expected_warning_codes=("LOW_TEXT_AFTER_OCR",),
            tolerate_text_loss=True,
            tags=("ocr", "resolution", "degraded"),
        ),
        PacketCase(
            case_id="unreadable_resolution",
            description=(
                "A 50 dpi scan, which the OCR stage rejects outright. Refusing the job is "
                "the correct answer; returning a packet that claims to be searchable is not."
            ),
            inputs=(
                _digital("cover.pdf", pages=1, tokens=("COVER",)),
                _scan("unreadable.pdf", pages=1, dpi=50, font_size=8.0, tokens=("VOID",)),
            ),
            expected_error_code="OCR_FAILED",
            tags=("ocr", "resolution", "degraded", "error-path"),
        ),
    ]


# --- Real documents ----------------------------------------------------------------


MANIFEST_NAME = "manifest.json"


def load_manifest_cases(corpus_dir: Path) -> list[PacketCase]:
    """Load real documents described by a manifest, scored by the same scorers.

    manifest.json:
        [
          {
            "case_id": "acme-onboarding-01",
            "description": "Real packet from Acme, received 2026-08-01",
            "order": "as_provided",
            "inputs": [
              {"path": "acme/01-application.pdf", "pages": 3, "scanned": false,
               "tokens": ["POL-48213"]}
            ]
          }
        ]

    `pages` and `scanned` are the human-labelled ground truth for that document, which is
    what makes page conservation and scanned-detection scoreable on real files.
    """
    manifest_path = corpus_dir / MANIFEST_NAME
    if not manifest_path.exists():
        return []

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases: list[PacketCase] = []
    for entry in raw:
        specs: list[InputSpec] = []
        paths: list[Path] = []
        for item in entry["inputs"]:
            path = (corpus_dir / item["path"]).resolve()
            if not path.exists():
                raise FileNotFoundError(f"Manifest references a missing document: {path}")
            paths.append(path)
            specs.append(
                InputSpec(
                    filename=path.name,
                    pages=int(item["pages"]),
                    scanned=bool(item.get("scanned", False)),
                    tokens=tuple(item.get("tokens", ())),
                )
            )
        cases.append(
            PacketCase(
                case_id=entry["case_id"],
                description=entry.get("description", ""),
                inputs=tuple(specs),
                order=entry.get("order", "as_provided"),
                input_labels=tuple(item.get("label", "") for item in entry["inputs"]),
                manifest=tuple(entry.get("manifest", ())),
                allow_unlisted=bool(entry.get("allow_unlisted", False)),
                expected_sequence=tuple(entry.get("expected_sequence", ())),
                expected_warning_codes=tuple(entry.get("expected_warning_codes", ())),
                tags=tuple(entry.get("tags", ())) + ("real",),
                real_paths=tuple(paths),
            )
        )
    return cases
