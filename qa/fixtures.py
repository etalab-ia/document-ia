"""Fixture resolution (local files + pure-Python synthetic generators) and scenario discovery."""

from __future__ import annotations

import math
import mimetypes
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_SCENARIOS_DIR = PACKAGE_DIR / "scenarios"

SYNTHETIC_PREFIX = "synthetic:"


class FixtureNotFoundError(FileNotFoundError):
    """Raised when a referenced local fixture file does not exist."""


class FixturePathTraversalError(ValueError):
    """Raised when a fixture path escapes the configured fixtures directory."""


@dataclass(frozen=True, slots=True)
class ResolvedFixture:
    """In-memory representation of a resolved upload fixture."""

    name: str
    content: bytes
    content_type: str
    source: str


def _escape_pdf_text(text: str) -> str:
    """Escape characters for PDF literal strings using WinAnsi/Latin-1 safe ASCII."""
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return safe


def _build_pdf_document(pages_lines: list[list[str]]) -> bytes:
    """Build a valid multi-page PDF 1.4 document with an exact xref table.

    Uses standard Type1 Helvetica font so no external font embedding is required.
    """
    if not pages_lines:
        pages_lines = [["Empty QA document"]]

    # Object numbering:
    # 1: Catalog
    # 2: Pages
    # 3: Font (/F1 Helvetica)
    # Then for each page i: (4 + 2*i) = Page object, (5 + 2*i) = Content stream
    objects: dict[int, bytes] = {}
    page_ids: list[int] = []

    for idx, lines in enumerate(pages_lines):
        page_obj_id = 4 + 2 * idx
        content_obj_id = 5 + 2 * idx
        page_ids.append(page_obj_id)

        stream_cmds: list[str] = ["BT", "/F1 12 Tf", "50 760 Td", "18 TL"]
        for line_idx, line in enumerate(lines):
            escaped = _escape_pdf_text(line)
            if line_idx == 0:
                stream_cmds.append(f"({escaped}) Tj")
            else:
                stream_cmds.append(f"T* ({escaped}) Tj")
        stream_cmds.append("ET")
        stream_bytes = ("\n".join(stream_cmds) + "\n").encode(
            "latin-1", errors="replace"
        )

        objects[content_obj_id] = (
            f"<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii")
            + stream_bytes
            + b"endstream"
        )
        objects[page_obj_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {content_obj_id} 0 R >>"
        ).encode("ascii")

    kids_str = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = f"<< /Type /Pages /Kids [{kids_str}] /Count {len(page_ids)} >>".encode(
        "ascii"
    )
    objects[3] = (
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )

    out = bytearray()
    out.extend(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

    max_obj_id = max(objects.keys())
    offsets: dict[int, int] = {}

    for obj_id in range(1, max_obj_id + 1):
        offsets[obj_id] = len(out)
        out.extend(f"{obj_id} 0 obj\n".encode("ascii"))
        out.extend(objects[obj_id])
        out.extend(b"\nendobj\n")

    xref_offset = len(out)
    out.extend(f"xref\n0 {max_obj_id + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for obj_id in range(1, max_obj_id + 1):
        out.extend(f"{offsets[obj_id]:010d} 00000 n \n".encode("ascii"))

    out.extend(
        (
            f"trailer\n<< /Size {max_obj_id + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(out)


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    """Encode a single PNG chunk with length, type, data, and CRC32."""
    crc = zlib.crc32(chunk_type)
    crc = zlib.crc32(data, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def _build_rgb_png(width: int = 64, height: int = 64) -> bytes:
    """Generate a valid RGB PNG image using pure Python stdlib."""
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw_rows = bytearray()
    for y in range(height):
        raw_rows.append(0)  # filter type 0 (None)
        for x in range(width):
            raw_rows.extend(((x * 4) % 256, (y * 4) % 256, 180))
    compressed = zlib.compress(bytes(raw_rows), level=9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr_data)
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )


def _build_decompression_bomb_png(width: int = 11_000, height: int = 10_000) -> bytes:
    """Generate a tiny-on-disk, valid PNG whose IHDR declares >100 MP (110 MP).

    Uses 1-bit grayscale (color_type=0, bit_depth=1) streamed into zlib at level 9
    so client-side RAM stays under a few KB and the resulting PNG is < 50 KB.
    """
    ihdr_data = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)
    row_bytes_len = 1 + math.ceil(width / 8)
    zero_row = b"\x00" * row_bytes_len

    compressor = zlib.compressobj(level=9)
    idat_parts: list[bytes] = []
    # Compress in small batches to keep peak memory tiny and speed fast
    batch_rows = zero_row * 100
    full_batches, remainder = divmod(height, 100)
    for _ in range(full_batches):
        chunk = compressor.compress(batch_rows)
        if chunk:
            idat_parts.append(chunk)
    if remainder:
        chunk = compressor.compress(zero_row * remainder)
        if chunk:
            idat_parts.append(chunk)
    flushed = compressor.flush()
    if flushed:
        idat_parts.append(flushed)

    idat_data = b"".join(idat_parts)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr_data)
        + _png_chunk(b"IDAT", idat_data)
        + _png_chunk(b"IEND", b"")
    )


def _gen_minimal_pdf() -> ResolvedFixture:
    content = _build_pdf_document(
        [
            [
                "REPUBLIQUE FRANCAISE",
                "ATTESTATION ADMINISTRATIVE DE TEST QA",
                "Document synthetique genere pour verification de la chaine Document IA.",
                "Reference: QA-MINIMAL-001",
                "Date: 15/01/2026",
            ]
        ]
    )
    return ResolvedFixture(
        name="minimal_qa.pdf",
        content=content,
        content_type="application/pdf",
        source="synthetic:minimal_pdf",
    )


def _gen_cni_pdf() -> ResolvedFixture:
    content = _build_pdf_document(
        [
            [
                "REPUBLIQUE FRANCAISE",
                "CARTE NATIONALE D'IDENTITE No: 123456789012",
                "Nationalite Francaise",
                "NOM: DUPONT",
                "Prenom(s): JEAN",
                "Sexe: M",
                "Ne(e) le: 14/05/1990",
                "a: PARIS 15E",
                "Delivree le: 10/01/2020",
                "Valable jusqu'au: 10/01/2030",
                "Autorite: PREFECTURE DE POLICE DE PARIS",
                "IDFRADUPONT<<<<<<<<<<<<<<<<<<<750001",
                "1234567890123JEAN<<<<<<<<<<<9005141M8",
            ]
        ]
    )
    return ResolvedFixture(
        name="synthetic_cni.pdf",
        content=content,
        content_type="application/pdf",
        source="synthetic:cni_pdf",
    )


def _gen_invalid_date_pdf() -> ResolvedFixture:
    content = _build_pdf_document(
        [
            [
                "REPUBLIQUE FRANCAISE",
                "CARTE NATIONALE D'IDENTITE No: 987654321098",
                "Nationalite Francaise",
                "NOM: MARTIN",
                "Prenom(s): CLAIRE",
                "Sexe: F",
                "Ne(e) le: 12/03/1988",
                "a: LYON",
                "Delivree le: 15/05/2016",
                "Valable jusqu'au: 31/04/2026",
                "Date d'expiration: 31/04/2026",
                "Autorite: PREFECTURE DU RHONE",
            ]
        ]
    )
    return ResolvedFixture(
        name="synthetic_invalid_date.pdf",
        content=content,
        content_type="application/pdf",
        source="synthetic:invalid_date_pdf",
    )


def _gen_multipage_pdf() -> ResolvedFixture:
    content = _build_pdf_document(
        [
            [
                "DOCUMENT MULTIPAGE DE TEST QA - PAGE 1 / 3",
                "Titre: Dossier Administratif Synthetique",
                "Titulaire: DUPONT JEAN",
                "Numero de dossier: QA-MULTI-2026-001",
            ],
            [
                "DOCUMENT MULTIPAGE DE TEST QA - PAGE 2 / 3",
                "Section: Justificatif complementaire",
                "Montant declare: 32 500 EUR",
                "Date d'emission: 01/02/2026",
            ],
            [
                "DOCUMENT MULTIPAGE DE TEST QA - PAGE 3 / 3",
                "Section: Validation et signature",
                "Statut: Conforme",
                "Fin du document synthetique.",
            ],
        ]
    )
    return ResolvedFixture(
        name="synthetic_multipage.pdf",
        content=content,
        content_type="application/pdf",
        source="synthetic:multipage_pdf",
    )


def _gen_valid_png() -> ResolvedFixture:
    return ResolvedFixture(
        name="synthetic_valid.png",
        content=_build_rgb_png(64, 64),
        content_type="image/png",
        source="synthetic:valid_png",
    )


def _gen_fake_pdf_shell() -> ResolvedFixture:
    return ResolvedFixture(
        name="fake.pdf",
        content=b"#!/bin/sh\necho not-a-pdf\n",
        content_type="application/pdf",
        source="synthetic:fake_pdf_shell",
    )


def _gen_decompression_bomb_png() -> ResolvedFixture:
    return ResolvedFixture(
        name="decompression_bomb.png",
        content=_build_decompression_bomb_png(11_000, 10_000),
        content_type="image/png",
        source="synthetic:decompression_bomb_png",
    )


SYNTHETIC_GENERATORS: dict[str, Callable[[], ResolvedFixture]] = {
    "minimal_pdf": _gen_minimal_pdf,
    "cni_pdf": _gen_cni_pdf,
    "invalid_date_pdf": _gen_invalid_date_pdf,
    "multipage_pdf": _gen_multipage_pdf,
    "valid_png": _gen_valid_png,
    "fake_pdf_shell": _gen_fake_pdf_shell,
    "decompression_bomb_png": _gen_decompression_bomb_png,
}


def list_synthetic_fixtures() -> list[str]:
    """Return all available synthetic fixture identifiers."""
    return sorted(f"{SYNTHETIC_PREFIX}{name}" for name in SYNTHETIC_GENERATORS)


def _guess_content_type(path: Path) -> str:
    ext = path.suffix.lower()
    explicit = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }
    if ext in explicit:
        return explicit[ext]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def resolve_fixture(spec: str, fixtures_dir: Path) -> ResolvedFixture:
    """Resolve a synthetic fixture (`synthetic:<name>`) or a confined local file."""
    cleaned = spec.strip()
    if cleaned.startswith(SYNTHETIC_PREFIX):
        key = cleaned[len(SYNTHETIC_PREFIX) :]
        generator = SYNTHETIC_GENERATORS.get(key)
        if generator is None:
            available = ", ".join(sorted(SYNTHETIC_GENERATORS))
            raise FixtureNotFoundError(
                f"Unknown synthetic fixture '{cleaned}'. Available: {available}"
            )
        return generator()

    candidate = Path(cleaned)
    if candidate.is_absolute():
        raise FixturePathTraversalError(
            f"Absolute fixture paths are forbidden for safety: '{cleaned}'. "
            f"Place files inside --fixtures-dir ({fixtures_dir}) and use a relative path."
        )

    base_resolved = fixtures_dir.resolve()
    target_resolved = (base_resolved / candidate).resolve()
    if not target_resolved.is_relative_to(base_resolved):
        raise FixturePathTraversalError(
            f"Fixture path '{cleaned}' escapes fixtures directory '{fixtures_dir}'."
        )

    if not target_resolved.is_file():
        raise FixtureNotFoundError(
            f"Local fixture file not found: '{target_resolved}' (from '{cleaned}')."
        )

    return ResolvedFixture(
        name=target_resolved.name,
        content=target_resolved.read_bytes(),
        content_type=_guess_content_type(target_resolved),
        source=str(target_resolved),
    )


def write_synthetic_fixtures(output_dir: Path) -> list[Path]:
    """Materialize all synthetic fixtures onto disk for manual inspection or testing."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in sorted(SYNTHETIC_GENERATORS):
        fixture = SYNTHETIC_GENERATORS[name]()
        target = output_dir / fixture.name
        if target.is_symlink():
            raise FixturePathTraversalError(
                f"Refusing to write synthetic fixture through symlink: '{target}'."
            )
        target.write_bytes(fixture.content)
        written.append(target)
    return written


def discover_scenario_files(
    *,
    fixtures_dir: Path,
    extra_paths: list[Path] | None = None,
    default_scenarios_dir: Path = DEFAULT_SCENARIOS_DIR,
) -> list[Path]:
    """Discover YAML scenario files across built-in, local fixtures, and CLI paths.

    Auto-discovers `*.yaml` and `*.yml` in:
      1. `qa/scenarios/`
      2. `<fixtures_dir>/scenarios/` (if present)
      3. Any explicit files or directories passed via `--scenarios`
    """
    discovered: list[Path] = []
    seen: set[Path] = set()

    def _add_path(p: Path) -> None:
        resolved = p.resolve()
        if resolved not in seen and resolved.is_file():
            seen.add(resolved)
            discovered.append(resolved)

    def _scan_dir(directory: Path) -> None:
        if not directory.is_dir():
            return
        candidates = sorted(
            [
                item
                for item in directory.iterdir()
                if item.is_file() and item.suffix.lower() in (".yaml", ".yml")
            ],
            key=lambda item: item.name,
        )
        for item in candidates:
            _add_path(item)

    if extra_paths:
        for path in extra_paths:
            if path.is_dir():
                _scan_dir(path)
            elif path.is_file():
                _add_path(path)
            else:
                raise FileNotFoundError(f"Scenario path not found: {path}")
        return discovered

    _scan_dir(default_scenarios_dir)
    _scan_dir(fixtures_dir / "scenarios")
    return discovered
