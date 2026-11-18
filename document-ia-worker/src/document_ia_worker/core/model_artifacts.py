from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)


class ModelArtifactIntegrityError(RuntimeError):
    """Raised when a model or OCR artifact is missing or fails SHA-256 verification."""


@dataclass(frozen=True)
class ModelArtifactSpec:
    key: str
    name: str
    url: str
    relative_path: str
    sha256: str
    size_bytes: int


QRDET_RELEASE_URL: Final[str] = (
    "https://github.com/Eric-Canas/qrdet/releases/download/v2.0_release"
)

YOLOWORLD_ARTIFACT: Final[ModelArtifactSpec] = ModelArtifactSpec(
    key="yoloworld",
    name="yolov8m-world.pt",
    url="https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8m-world.pt",
    relative_path="yolov8m-world.pt",
    sha256="8ede3cfa80c243608a9ef14d54f55030efc9d7a8635af245fc6d67efdf294aaa",
    size_bytes=58_610_242,
)

QRDET_ARTIFACT: Final[ModelArtifactSpec] = ModelArtifactSpec(
    key="qrdet",
    name="qrdet-s.pt",
    url="https://github.com/Eric-Canas/qrdet/releases/download/v2.0_release/qrdet-s.pt",
    relative_path="qrdet/qrdet-s.pt",
    sha256="50a9113970a878a51206d5caabcc2c758856f751944199a2c371d872fd768af1",
    size_bytes=23_970_659,
)

TESSERACT_FRA_ARTIFACT: Final[ModelArtifactSpec] = ModelArtifactSpec(
    key="tesseract_fra",
    name="fra.traineddata",
    url="https://github.com/tesseract-ocr/tessdata_fast/raw/4.1.0/fra.traineddata",
    relative_path="tessdata/fra.traineddata",
    sha256="ced037562e8c80c13122dece28dd477d399af80911a28791a66a63ac1e3445ca",
    size_bytes=1_130_365,
)

MODEL_ARTIFACTS: Final[tuple[ModelArtifactSpec, ...]] = (
    YOLOWORLD_ARTIFACT,
    QRDET_ARTIFACT,
    TESSERACT_FRA_ARTIFACT,
)

WORKER_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
DEFAULT_MODELS_DIR: Final[Path] = WORKER_ROOT / ".models"

_SYSTEM_TESSDATA_DIRS: Final[tuple[Path, ...]] = (
    Path("/usr/share/tesseract-ocr/5/tessdata"),
    Path("/usr/share/tesseract-ocr/4.00/tessdata"),
    Path("/usr/share/tessdata"),
    Path("/opt/homebrew/share/tessdata"),
    Path("/usr/local/share/tessdata"),
)

_CHUNK_SIZE: Final[int] = 1024 * 1024
_DOWNLOAD_TIMEOUT_S: Final[int] = 120

# Cache of verified files keyed by (resolved_path_str, st_mtime_ns, st_size, expected_sha256)
_VERIFIED_FILES_CACHE: set[tuple[str, int, int, str]] = set()


def clear_verification_cache() -> None:
    """Clear the in-memory verification cache (primarily used in tests)."""
    _VERIFIED_FILES_CACHE.clear()


def compute_file_sha256(path: Path) -> str:
    """Compute the lowercase hexadecimal SHA-256 digest of a file."""
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest().lower()


def verify_file_sha256(
    path: Path,
    expected_sha256: str,
    artifact_name: str,
) -> Path:
    """Verify that `path` exists, is a regular file, and matches `expected_sha256`.

    Raises `ModelArtifactIntegrityError` if the file is missing or its digest does not match.
    """
    normalized_expected = expected_sha256.strip().lower()
    if not path.exists() or not path.is_file():
        raise ModelArtifactIntegrityError(
            f"Missing model artifact '{artifact_name}' at '{path}'. "
            "Implicit runtime downloads are disabled. "
            "Run 'python -m document_ia_worker.core.model_artifacts --download-and-verify' "
            "to provision verified model artifacts."
        )

    try:
        resolved = path.resolve()
        stat_res = path.stat()
    except OSError as exc:
        raise ModelArtifactIntegrityError(
            f"Unable to stat model artifact '{artifact_name}' at '{path}': {exc}"
        ) from exc

    cache_key = (
        str(resolved),
        stat_res.st_mtime_ns,
        stat_res.st_size,
        normalized_expected,
    )
    if cache_key in _VERIFIED_FILES_CACHE:
        return path

    actual_sha256 = compute_file_sha256(path)
    if actual_sha256 != normalized_expected:
        raise ModelArtifactIntegrityError(
            f"SHA-256 integrity check failed for model artifact '{artifact_name}' at '{path}': "
            f"expected {normalized_expected}, got {actual_sha256}. "
            "Run 'python -m document_ia_worker.core.model_artifacts --download-and-verify' "
            "to replace the corrupted file."
        )

    _VERIFIED_FILES_CACHE.add(cache_key)
    return path


def _ensure_qrdet_release_marker(qrdet_dir: Path, *, allow_write: bool) -> Path:
    marker_path = qrdet_dir / "current_release.txt"
    if marker_path.is_file():
        try:
            content = marker_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ModelArtifactIntegrityError(
                f"Cannot read QRDet release marker at '{marker_path}': {exc}"
            ) from exc
        if content == QRDET_RELEASE_URL:
            return marker_path

    if allow_write:
        try:
            qrdet_dir.mkdir(parents=True, exist_ok=True)
            marker_path.write_text(QRDET_RELEASE_URL, encoding="utf-8")
            return marker_path
        except OSError as exc:
            raise ModelArtifactIntegrityError(
                f"QRDet release marker at '{marker_path}' is missing or invalid and "
                f"cannot be written: {exc}"
            ) from exc

    raise ModelArtifactIntegrityError(
        f"Invalid or missing QRDet release marker at '{marker_path}': "
        f"expected exact content '{QRDET_RELEASE_URL}'."
    )


def resolve_and_verify_yoloworld_path(models_dir: Path | None = None) -> Path:
    """Resolve the YOLO-World weights file path and verify its SHA-256 before loading."""
    if models_dir is not None:
        candidate = models_dir / YOLOWORLD_ARTIFACT.relative_path
    else:
        env_path = os.environ.get("YOLOWORLD_PATH", "").strip()
        if env_path:
            candidate = Path(env_path)
        else:
            candidate = DEFAULT_MODELS_DIR / YOLOWORLD_ARTIFACT.relative_path

    return verify_file_sha256(
        candidate,
        YOLOWORLD_ARTIFACT.sha256,
        YOLOWORLD_ARTIFACT.name,
    )


def resolve_and_verify_qrdet_dir(
    model_size: str = "s",
    models_dir: Path | None = None,
) -> Path:
    """Resolve the QRDet weights directory, verify `qrdet-s.pt` SHA-256 and `current_release.txt`."""
    if model_size != "s":
        raise ModelArtifactIntegrityError(
            f"Unsupported QRDet model_size '{model_size}': only 's' is pinned with a verified SHA-256."
        )

    if models_dir is not None:
        qrdet_dir = models_dir / "qrdet"
    else:
        env_dir = os.environ.get("QRDET_WEIGHTS_DIR", "").strip()
        if not env_dir:
            try:
                from document_ia_worker.core.barcode_settings import barcode_settings

                if barcode_settings.QRDET_WEIGHTS_DIR:
                    env_dir = barcode_settings.QRDET_WEIGHTS_DIR.strip()
            except Exception:
                env_dir = ""

        if env_dir:
            qrdet_dir = Path(env_dir)
        else:
            qrdet_dir = DEFAULT_MODELS_DIR / "qrdet"

    weights_path = qrdet_dir / QRDET_ARTIFACT.name
    verify_file_sha256(
        weights_path,
        QRDET_ARTIFACT.sha256,
        QRDET_ARTIFACT.name,
    )
    _ensure_qrdet_release_marker(qrdet_dir, allow_write=False)
    return qrdet_dir


def resolve_and_verify_tesseract_lang_data(
    lang: str = "fra",
    models_dir: Path | None = None,
) -> Path | None:
    """Locate and verify `fra.traineddata` SHA-256 when `lang == 'fra'`."""
    if lang != "fra":
        return None

    if models_dir is not None:
        candidate = models_dir / TESSERACT_FRA_ARTIFACT.relative_path
        return verify_file_sha256(
            candidate,
            TESSERACT_FRA_ARTIFACT.sha256,
            TESSERACT_FRA_ARTIFACT.name,
        )

    env_prefix = os.environ.get("TESSDATA_PREFIX", "").strip()
    if env_prefix:
        prefix_path = Path(env_prefix)
        candidate = prefix_path / TESSERACT_FRA_ARTIFACT.name
        if (
            not candidate.exists()
            and (prefix_path / "tessdata" / TESSERACT_FRA_ARTIFACT.name).exists()
        ):
            candidate = prefix_path / "tessdata" / TESSERACT_FRA_ARTIFACT.name
        return verify_file_sha256(
            candidate,
            TESSERACT_FRA_ARTIFACT.sha256,
            TESSERACT_FRA_ARTIFACT.name,
        )

    bundled_tessdata_dir = DEFAULT_MODELS_DIR / "tessdata"
    search_dirs: tuple[Path, ...] = (bundled_tessdata_dir, *_SYSTEM_TESSDATA_DIRS)
    for tessdata_dir in search_dirs:
        candidate = tessdata_dir / TESSERACT_FRA_ARTIFACT.name
        if candidate.exists():
            return verify_file_sha256(
                candidate,
                TESSERACT_FRA_ARTIFACT.sha256,
                TESSERACT_FRA_ARTIFACT.name,
            )

    # None of the candidate directories contained fra.traineddata -> fail closed with clear message
    return verify_file_sha256(
        bundled_tessdata_dir / TESSERACT_FRA_ARTIFACT.name,
        TESSERACT_FRA_ARTIFACT.sha256,
        TESSERACT_FRA_ARTIFACT.name,
    )


def verify_all_artifacts(models_dir: Path = DEFAULT_MODELS_DIR) -> dict[str, Path]:
    """Verify all 3 pinned artifacts inside `models_dir` (plus QRDet release marker)."""
    yolo_path = verify_file_sha256(
        models_dir / YOLOWORLD_ARTIFACT.relative_path,
        YOLOWORLD_ARTIFACT.sha256,
        YOLOWORLD_ARTIFACT.name,
    )
    qrdet_path = verify_file_sha256(
        models_dir / QRDET_ARTIFACT.relative_path,
        QRDET_ARTIFACT.sha256,
        QRDET_ARTIFACT.name,
    )
    _ensure_qrdet_release_marker(qrdet_path.parent, allow_write=False)
    tess_path = verify_file_sha256(
        models_dir / TESSERACT_FRA_ARTIFACT.relative_path,
        TESSERACT_FRA_ARTIFACT.sha256,
        TESSERACT_FRA_ARTIFACT.name,
    )
    return {
        YOLOWORLD_ARTIFACT.key: yolo_path,
        QRDET_ARTIFACT.key: qrdet_path,
        TESSERACT_FRA_ARTIFACT.key: tess_path,
    }


def _download_file(url: str, destination: Path) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ModelArtifactIntegrityError(
            f"Refusing to download model artifact from non-HTTPS URL: '{url}'"
        )
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "document-ia-worker-artifact-verifier/1.0"},
    )
    with (
        urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_S) as response,
        destination.open("wb") as out_file,
    ):
        shutil.copyfileobj(response, out_file, length=_CHUNK_SIZE)


def download_and_verify_artifact(
    spec: ModelArtifactSpec,
    models_dir: Path = DEFAULT_MODELS_DIR,
) -> Path:
    """Ensure `spec` is present in `models_dir` and matches its pinned SHA-256.

    If already present in cache with a valid SHA-256, reuses it.
    If missing or corrupted, downloads to a temporary file, verifies the SHA-256
    BEFORE moving into place, and never executes `torch.load`.
    """
    target_path = models_dir / spec.relative_path
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if target_path.is_file():
        try:
            verify_file_sha256(target_path, spec.sha256, spec.name)
            logger.info(
                "[model_artifacts] Cached artifact '%s' verified (%s)",
                spec.name,
                spec.sha256,
            )
            return target_path
        except ModelArtifactIntegrityError as exc:
            logger.warning(
                "[model_artifacts] Cached artifact '%s' at '%s' is invalid (%s); re-downloading.",
                spec.name,
                target_path,
                exc,
            )
            target_path.unlink(missing_ok=True)
            clear_verification_cache()

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{spec.name}.",
        suffix=".part",
        dir=str(target_path.parent),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)

    try:
        logger.info(
            "[model_artifacts] Downloading '%s' from %s ...",
            spec.name,
            spec.url,
        )
        _download_file(spec.url, tmp_path)
        actual_sha256 = compute_file_sha256(tmp_path)
        expected_sha256 = spec.sha256.lower()
        if actual_sha256 != expected_sha256:
            raise ModelArtifactIntegrityError(
                f"Downloaded artifact '{spec.name}' from '{spec.url}' failed SHA-256 verification: "
                f"expected {expected_sha256}, got {actual_sha256}."
            )
        tmp_path.chmod(0o644)
        tmp_path.replace(target_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    clear_verification_cache()
    return verify_file_sha256(target_path, spec.sha256, spec.name)


def download_and_verify_all(models_dir: Path = DEFAULT_MODELS_DIR) -> dict[str, Path]:
    """Download (if missing or invalid) and verify all 3 pinned model artifacts."""
    verified: dict[str, Path] = {}
    for spec in MODEL_ARTIFACTS:
        verified[spec.key] = download_and_verify_artifact(spec, models_dir=models_dir)
    _ensure_qrdet_release_marker(models_dir / "qrdet", allow_write=True)
    return verified


def build_artifacts_inventory(
    models_dir: Path = DEFAULT_MODELS_DIR,
) -> dict[str, object]:
    """Verify all artifacts in `models_dir` and return a structured inventory dictionary."""
    paths = verify_all_artifacts(models_dir=models_dir)
    items: list[dict[str, object]] = []
    for spec in MODEL_ARTIFACTS:
        artifact_path = paths[spec.key]
        stat_res = artifact_path.stat()
        items.append(
            {
                "key": spec.key,
                "name": spec.name,
                "url": spec.url,
                "path": str(artifact_path),
                "relative_path": spec.relative_path,
                "sha256": spec.sha256,
                "size_bytes": stat_res.st_size,
                "verified": True,
            }
        )
    return {
        "models_dir": str(models_dir),
        "qrdet_release_url": QRDET_RELEASE_URL,
        "artifacts": items,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m document_ia_worker.core.model_artifacts",
        description="Verify or download-and-verify pinned AI and OCR model artifacts.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--verify",
        action="store_true",
        help="Verify that all pinned artifacts exist and match their SHA-256 digests.",
    )
    group.add_argument(
        "--download-and-verify",
        action="store_true",
        help="Download missing or corrupted artifacts and verify their SHA-256 digests.",
    )
    group.add_argument(
        "--print-inventory",
        action="store_true",
        help="Verify all artifacts and print their inventory metadata as JSON.",
    )
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=DEFAULT_MODELS_DIR,
        help=f"Root directory for bundled models (default: {DEFAULT_MODELS_DIR}).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    models_dir: Path = args.models_dir.resolve()

    try:
        if args.download_and_verify:
            verified = download_and_verify_all(models_dir=models_dir)
            for key, path in verified.items():
                print(f"[model_artifacts] OK {key}: {path}", file=sys.stderr)
            return 0

        if args.verify:
            verified = verify_all_artifacts(models_dir=models_dir)
            for key, path in verified.items():
                print(f"[model_artifacts] VERIFIED {key}: {path}", file=sys.stderr)
            return 0

        if args.print_inventory:
            inventory = build_artifacts_inventory(models_dir=models_dir)
            print(json.dumps(inventory, indent=2, sort_keys=True))
            return 0
    except ModelArtifactIntegrityError as exc:
        print(f"[model_artifacts] INTEGRITY ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[model_artifacts] UNEXPECTED ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
