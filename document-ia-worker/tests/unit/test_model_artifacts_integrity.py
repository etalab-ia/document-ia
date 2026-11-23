from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from document_ia_worker.core import model_artifacts
from document_ia_worker.core.model_artifacts import (
    CLIP_ARTIFACT,
    QRDET_ARTIFACT,
    QRDET_RELEASE_URL,
    TESSERACT_FRA_ARTIFACT,
    YOLOWORLD_ARTIFACT,
    ModelArtifactIntegrityError,
    ModelArtifactSpec,
    clear_verification_cache,
    compute_file_sha256,
    download_and_verify_all,
    download_and_verify_artifact,
    resolve_and_verify_clip_path,
    resolve_and_verify_qrdet_dir,
    verify_all_artifacts,
    verify_file_sha256,
)
from document_ia_worker.core.preprocessing import yoloworld_crop
from document_ia_worker.workflow.step.extract_barcode_data import (
    extract_barcode_2ddoc_data,
)
from document_ia_worker.workflow.step.extract_content_ocr.extract_content_ocr import (
    ExtractContentOcrStep,
)
from document_ia_worker.workflow.step.step_result.preprocess_file_result import (
    PreprocessFileResult,
)


@pytest.fixture(autouse=True)
def _reset_artifact_cache() -> None:
    clear_verification_cache()


def _write_bytes_with_hash(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def test_verify_file_sha256_nominal_and_cache_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"verified-model-weights-payload"
    artifact_file = tmp_path / "model.pt"
    expected_hash = _write_bytes_with_hash(artifact_file, payload)

    hash_calls = 0
    original_compute = compute_file_sha256

    def CountingCompute(p: Path) -> str:
        nonlocal hash_calls
        hash_calls += 1
        return original_compute(p)

    monkeypatch.setattr(model_artifacts, "compute_file_sha256", CountingCompute)

    # First call computes SHA-256 from disk
    verified_1 = verify_file_sha256(
        artifact_file,
        expected_hash,
        "model.pt",
        expected_size_bytes=len(payload),
    )
    assert verified_1 == artifact_file
    assert hash_calls == 1

    # Second call with unchanged mtime and size hits the in-memory cache
    verified_2 = verify_file_sha256(
        artifact_file,
        expected_hash,
        "model.pt",
        expected_size_bytes=len(payload),
    )
    assert verified_2 == artifact_file
    assert hash_calls == 1


def test_verify_file_sha256_rejects_missing_or_corrupted_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.pt"
    with pytest.raises(ModelArtifactIntegrityError, match="Missing model artifact"):
        verify_file_sha256(missing, YOLOWORLD_ARTIFACT.sha256, "yolov8m-world.pt")

    corrupted = tmp_path / "corrupted.pt"
    corrupted.write_bytes(b"corrupted-bytes")
    with pytest.raises(
        ModelArtifactIntegrityError, match="SHA-256 integrity check failed"
    ):
        verify_file_sha256(corrupted, YOLOWORLD_ARTIFACT.sha256, "yolov8m-world.pt")


def test_verify_file_sha256_wraps_oserror_and_validates_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"abc123"
    artifact_file = tmp_path / "model.pt"
    expected_hash = _write_bytes_with_hash(artifact_file, payload)

    with pytest.raises(ModelArtifactIntegrityError, match="Size check failed"):
        verify_file_sha256(
            artifact_file,
            expected_hash,
            "model.pt",
            expected_size_bytes=len(payload) + 10,
        )

    def raise_permission_error(self: Path, *_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError("read denied")

    monkeypatch.setattr(Path, "open", raise_permission_error)
    with pytest.raises(
        ModelArtifactIntegrityError, match="Unable to read model artifact"
    ):
        verify_file_sha256(
            artifact_file,
            expected_hash,
            "model.pt",
            expected_size_bytes=len(payload),
        )


def test_yoloworld_loader_rejects_tampered_or_missing_weights_before_ultralytics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tampered_file = tmp_path / "yolov8m-world.pt"
    tampered_file.write_bytes(b"\x00" * 128)

    monkeypatch.setenv("YOLOWORLD_PATH", str(tampered_file))
    monkeypatch.setattr(yoloworld_crop, "_MODEL_CACHE", None)

    ultralytics_called = False

    class ForbiddenYOLOWorld:
        def __init__(self, _path: str) -> None:
            nonlocal ultralytics_called
            ultralytics_called = True

    monkeypatch.setattr("ultralytics.YOLOWorld", ForbiddenYOLOWorld)

    with pytest.raises(ModelArtifactIntegrityError, match="Size check failed"):
        yoloworld_crop.get_yoloworld_model()

    assert ultralytics_called is False

    # Missing file also fails closed without calling YOLOWorld
    monkeypatch.setenv("YOLOWORLD_PATH", str(tmp_path / "does-not-exist.pt"))
    with pytest.raises(ModelArtifactIntegrityError, match="Missing model artifact"):
        yoloworld_crop.get_yoloworld_model()

    assert ultralytics_called is False


def test_clip_resolver_respects_env_and_rejects_tampered_weights(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_clip_bytes = b"valid-clip-weights"
    fake_clip_spec = ModelArtifactSpec(
        key="clip",
        name="ViT-B-32.pt",
        url=CLIP_ARTIFACT.url,
        relative_path="clip/ViT-B-32.pt",
        sha256=hashlib.sha256(fake_clip_bytes).hexdigest(),
        size_bytes=len(fake_clip_bytes),
    )
    monkeypatch.setattr(model_artifacts, "CLIP_ARTIFACT", fake_clip_spec)

    clip_path = tmp_path / "custom" / "ViT-B-32.pt"
    clip_path.parent.mkdir(parents=True)
    clip_path.write_bytes(fake_clip_bytes)
    monkeypatch.setenv("CLIP_WEIGHTS_PATH", str(clip_path))
    assert resolve_and_verify_clip_path() == clip_path

    # Tampered byte with same size fails SHA-256 check
    clip_path.write_bytes(b"X" + fake_clip_bytes[1:])
    clear_verification_cache()
    with pytest.raises(
        ModelArtifactIntegrityError, match="SHA-256 integrity check failed"
    ):
        resolve_and_verify_clip_path()


def test_qrdet_loader_rejects_tampered_or_missing_weights_before_qrdetector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qrdet_dir = tmp_path / "qrdet"
    qrdet_dir.mkdir(parents=True)
    tampered_weights = qrdet_dir / "qrdet-s.pt"
    tampered_weights.write_bytes(b"tampered-qrdet-weights")

    monkeypatch.setenv("QRDET_WEIGHTS_DIR", str(qrdet_dir))
    monkeypatch.setattr(extract_barcode_2ddoc_data, "_qrdet_detector", None)

    qrdetector_called = False

    def forbidden_qrdetector(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal qrdetector_called
        qrdetector_called = True
        raise AssertionError(
            "QRDetector should not be instantiated on tampered weights"
        )

    monkeypatch.setattr(extract_barcode_2ddoc_data, "QRDetector", forbidden_qrdetector)

    with pytest.raises(ModelArtifactIntegrityError, match="Size check failed"):
        extract_barcode_2ddoc_data._get_qrdet_detector()

    assert qrdetector_called is False

    # Unsupported model size is also rejected
    with pytest.raises(
        ModelArtifactIntegrityError, match="Unsupported QRDet model_size"
    ):
        resolve_and_verify_qrdet_dir(model_size="l", models_dir=tmp_path)


@pytest.mark.asyncio
async def test_ocr_step_rejects_tampered_tesseract_fra_traineddata_before_ocr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    main_workflow_context: Any,
) -> None:
    tessdata_dir = tmp_path / "tessdata"
    tessdata_dir.mkdir(parents=True)
    (tessdata_dir / "fra.traineddata").write_bytes(b"corrupted-traineddata")

    monkeypatch.setenv("TESSDATA_PREFIX", str(tessdata_dir))

    ocr_called = False

    def forbidden_image_to_string(*_args: Any, **_kwargs: Any) -> str:
        nonlocal ocr_called
        ocr_called = True
        return "should not run"

    monkeypatch.setattr(
        "document_ia_worker.workflow.step.extract_content_ocr.extract_content_ocr.image_to_string",
        forbidden_image_to_string,
    )

    step = ExtractContentOcrStep(main_workflow_context=main_workflow_context)
    step.inject_workflow_context(
        {
            PreprocessFileResult.__name__: PreprocessFileResult(
                output_files_path=[str(tmp_path / "page1.png")]
            )
        }
    )

    with pytest.raises(ModelArtifactIntegrityError, match="Size check failed"):
        await step.execute()

    assert ocr_called is False


def test_download_and_verify_replaces_corrupted_cache_and_writes_release_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_yolo_bytes = b"valid-yolo-bytes"
    fake_clip_bytes = b"valid-clip-bytes"
    fake_qrdet_bytes = b"valid-qrdet-bytes"
    fake_tess_bytes = b"valid-tess-bytes"

    fake_specs = (
        ModelArtifactSpec(
            key="yoloworld",
            name="yolov8m-world.pt",
            url="https://example.invalid/yolov8m-world.pt",
            relative_path="yolov8m-world.pt",
            sha256=hashlib.sha256(fake_yolo_bytes).hexdigest(),
            size_bytes=len(fake_yolo_bytes),
        ),
        ModelArtifactSpec(
            key="clip",
            name="ViT-B-32.pt",
            url="https://example.invalid/ViT-B-32.pt",
            relative_path="clip/ViT-B-32.pt",
            sha256=hashlib.sha256(fake_clip_bytes).hexdigest(),
            size_bytes=len(fake_clip_bytes),
        ),
        ModelArtifactSpec(
            key="qrdet",
            name="qrdet-s.pt",
            url="https://example.invalid/qrdet-s.pt",
            relative_path="qrdet/qrdet-s.pt",
            sha256=hashlib.sha256(fake_qrdet_bytes).hexdigest(),
            size_bytes=len(fake_qrdet_bytes),
        ),
        ModelArtifactSpec(
            key="tesseract_fra",
            name="fra.traineddata",
            url="https://example.invalid/fra.traineddata",
            relative_path="tessdata/fra.traineddata",
            sha256=hashlib.sha256(fake_tess_bytes).hexdigest(),
            size_bytes=len(fake_tess_bytes),
        ),
    )
    monkeypatch.setattr(model_artifacts, "YOLOWORLD_ARTIFACT", fake_specs[0])
    monkeypatch.setattr(model_artifacts, "CLIP_ARTIFACT", fake_specs[1])
    monkeypatch.setattr(model_artifacts, "QRDET_ARTIFACT", fake_specs[2])
    monkeypatch.setattr(model_artifacts, "TESSERACT_FRA_ARTIFACT", fake_specs[3])
    monkeypatch.setattr(model_artifacts, "MODEL_ARTIFACTS", fake_specs)

    # Pre-populate a corrupted cached file for yolov8m-world.pt
    corrupted_yolo = tmp_path / "yolov8m-world.pt"
    corrupted_yolo.write_bytes(b"corrupted-cache-content")

    url_payloads = {
        fake_specs[0].url: fake_yolo_bytes,
        fake_specs[1].url: fake_clip_bytes,
        fake_specs[2].url: fake_qrdet_bytes,
        fake_specs[3].url: fake_tess_bytes,
    }

    def fake_download(
        url: str,
        destination: Path,
        *,
        max_bytes: int | None = None,  # noqa: ARG001
    ) -> None:
        destination.write_bytes(url_payloads[url])

    monkeypatch.setattr(model_artifacts, "_download_file", fake_download)

    verified = download_and_verify_all(models_dir=tmp_path)
    assert verified["yoloworld"].read_bytes() == fake_yolo_bytes
    assert verified["clip"].read_bytes() == fake_clip_bytes
    assert verified["qrdet"].read_bytes() == fake_qrdet_bytes
    assert verified["tesseract_fra"].read_bytes() == fake_tess_bytes
    assert (tmp_path / "qrdet" / "current_release.txt").read_text(
        encoding="utf-8"
    ) == QRDET_RELEASE_URL

    # MODELS_DIR environment variable is respected by verify_all_artifacts()
    monkeypatch.setenv("MODELS_DIR", str(tmp_path))
    assert set(verify_all_artifacts().keys()) == {
        "yoloworld",
        "clip",
        "qrdet",
        "tesseract_fra",
    }

    # CLI --verify and --print-inventory succeed with exit code 0
    assert model_artifacts.main(["--verify", "--models-dir", str(tmp_path)]) == 0
    assert (
        model_artifacts.main(["--print-inventory", "--models-dir", str(tmp_path)]) == 0
    )

    # Tamper 1 byte in qrdet-s.pt -> CLI --verify must return exit code 1
    tampered_bytes = bytearray(fake_qrdet_bytes)
    tampered_bytes[0] ^= 0xFF
    verified["qrdet"].write_bytes(bytes(tampered_bytes))
    clear_verification_cache()
    assert model_artifacts.main(["--verify", "--models-dir", str(tmp_path)]) == 1


def test_download_and_verify_cleans_up_temp_file_on_bad_downloaded_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = ModelArtifactSpec(
        key="yoloworld",
        name="yolov8m-world.pt",
        url="https://example.invalid/tampered-upstream.pt",
        relative_path="yolov8m-world.pt",
        sha256=YOLOWORLD_ARTIFACT.sha256,
        size_bytes=YOLOWORLD_ARTIFACT.size_bytes,
    )

    def fake_bad_download(
        _url: str,
        destination: Path,
        *,
        max_bytes: int | None = None,  # noqa: ARG001
    ) -> None:
        destination.write_bytes(b"upstream-tampered-bytes")

    monkeypatch.setattr(model_artifacts, "_download_file", fake_bad_download)

    with pytest.raises(
        ModelArtifactIntegrityError, match="failed SHA-256 verification"
    ):
        download_and_verify_artifact(spec, models_dir=tmp_path)

    assert not (tmp_path / "yolov8m-world.pt").exists()
    assert list(tmp_path.glob("*.part")) == []


def test_prewarm_models_and_trust_store_is_fail_closed_on_integrity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from document_ia_worker import main as worker_main

    monkeypatch.setattr(
        "document_ia_worker.core.model_artifacts.resolve_and_verify_tesseract_lang_data",
        MagicMock(side_effect=ModelArtifactIntegrityError("tampered fra.traineddata")),
    )

    with pytest.raises(ModelArtifactIntegrityError, match="tampered fra.traineddata"):
        worker_main._prewarm_models_and_trust_store()


def test_prewarm_models_and_trust_store_preloads_2ddoc_trust_store(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from fr_2ddoc_parser.model.models import TrustListFreshness

    from document_ia_worker import main as worker_main

    monkeypatch.setattr(
        "document_ia_worker.core.model_artifacts.resolve_and_verify_tesseract_lang_data",
        MagicMock(return_value=Path("/fake/fra.traineddata")),
    )
    monkeypatch.setattr(
        "document_ia_worker.core.model_artifacts.resolve_and_verify_clip_path",
        MagicMock(return_value=Path("/fake/ViT-B-32.pt")),
    )
    monkeypatch.setattr(
        "document_ia_worker.core.preprocessing.yoloworld_crop.get_yoloworld_model",
        MagicMock(return_value=object()),
    )
    monkeypatch.setattr(
        "document_ia_worker.workflow.step.extract_barcode_data.extract_barcode_2ddoc_data._get_qrdet_detector",
        MagicMock(return_value=object()),
    )

    with caplog.at_level(logging.INFO, logger=worker_main.__name__):
        worker_main._prewarm_models_and_trust_store()

    assert "2D-Doc PKI trust store verified and pre-loaded into RAM" in caplog.text
    assert "tsl_sequence_number=" in caplog.text
    assert "tsl_next_update=" in caplog.text
    assert "tsl_freshness=" in caplog.text

    # Non-fresh trust list emits a warning without blocking boot.
    stale_resolver = MagicMock()
    stale_resolver.tsl_sequence_number = 23
    stale_resolver.tsl_next_update = "2020-01-01T00:00:00+00:00"
    stale_resolver.get_tsl_freshness.return_value = TrustListFreshness.STALE
    monkeypatch.setattr(
        "fr_2ddoc_parser.crypto.key_resolver.get_default_key_resolver",
        MagicMock(return_value=stale_resolver),
    )

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=worker_main.__name__):
        worker_main._prewarm_models_and_trust_store()

    assert "2D-Doc PKI trust list is not fresh" in caplog.text
    assert "tsl_freshness=stale" in caplog.text


def test_prewarm_models_and_trust_store_is_fail_closed_on_trust_store_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fr_2ddoc_parser.exception.exceptions import ResolverUnavailableError

    from document_ia_worker import main as worker_main

    monkeypatch.setattr(
        "document_ia_worker.core.model_artifacts.resolve_and_verify_tesseract_lang_data",
        MagicMock(return_value=Path("/fake/fra.traineddata")),
    )
    monkeypatch.setattr(
        "document_ia_worker.core.model_artifacts.resolve_and_verify_clip_path",
        MagicMock(return_value=Path("/fake/ViT-B-32.pt")),
    )
    monkeypatch.setattr(
        "document_ia_worker.core.preprocessing.yoloworld_crop.get_yoloworld_model",
        MagicMock(return_value=object()),
    )
    monkeypatch.setattr(
        "document_ia_worker.workflow.step.extract_barcode_data.extract_barcode_2ddoc_data._get_qrdet_detector",
        MagicMock(return_value=object()),
    )
    monkeypatch.setattr(
        "fr_2ddoc_parser.crypto.key_resolver.get_default_key_resolver",
        MagicMock(side_effect=ResolverUnavailableError("corrupt TSL signature")),
    )

    with pytest.raises(ResolverUnavailableError, match="corrupt TSL signature"):
        worker_main._prewarm_models_and_trust_store()


def test_qrdet_loader_rejects_missing_or_tampered_release_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_qrdet_bytes = b"valid-qrdet-weights"
    fake_spec = ModelArtifactSpec(
        key="qrdet",
        name="qrdet-s.pt",
        url="https://example.invalid/qrdet-s.pt",
        relative_path="qrdet/qrdet-s.pt",
        sha256=hashlib.sha256(fake_qrdet_bytes).hexdigest(),
        size_bytes=len(fake_qrdet_bytes),
    )
    monkeypatch.setattr(model_artifacts, "QRDET_ARTIFACT", fake_spec)

    qrdet_dir = tmp_path / "qrdet"
    qrdet_dir.mkdir(parents=True)
    (qrdet_dir / "qrdet-s.pt").write_bytes(fake_qrdet_bytes)

    # Missing current_release.txt must fail closed without writing to disk
    marker_path = qrdet_dir / "current_release.txt"
    with pytest.raises(
        ModelArtifactIntegrityError, match="Invalid or missing QRDet release marker"
    ):
        resolve_and_verify_qrdet_dir(models_dir=tmp_path)
    assert not marker_path.exists()

    # Tampered current_release.txt must also fail closed without overwriting
    tampered_url = "https://attacker.invalid/qrdet/releases/download/v2.0_release"
    marker_path.write_text(tampered_url, encoding="utf-8")
    with pytest.raises(
        ModelArtifactIntegrityError, match="Invalid or missing QRDet release marker"
    ):
        resolve_and_verify_qrdet_dir(models_dir=tmp_path)
    assert marker_path.read_text(encoding="utf-8") == tampered_url

    # Exact pinned marker passes
    marker_path.write_text(QRDET_RELEASE_URL, encoding="utf-8")
    assert resolve_and_verify_qrdet_dir(models_dir=tmp_path) == qrdet_dir


def test_download_file_rejects_non_https_scheme(tmp_path: Path) -> None:
    destination = tmp_path / "model.pt"

    with pytest.raises(ModelArtifactIntegrityError, match="non-HTTPS URL"):
        model_artifacts._download_file("file:///etc/passwd", destination)

    with pytest.raises(ModelArtifactIntegrityError, match="non-HTTPS URL"):
        model_artifacts._download_file("http://example.invalid/model.pt", destination)

    assert not destination.exists()


def test_download_file_rejects_untrusted_host_and_oversized_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "model.pt"

    with pytest.raises(ModelArtifactIntegrityError, match="untrusted host"):
        model_artifacts._download_file(
            "https://untrusted.example.invalid/model.pt",
            destination,
        )
    assert not destination.exists()

    class FakeResponse(io.BytesIO):
        def __init__(self, data: bytes, content_length: str | None = None) -> None:
            super().__init__(data)
            self.headers: dict[str, str] = {}
            if content_length is not None:
                self.headers["Content-Length"] = content_length

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: Any) -> None:
            self.close()

    # Rejects when Content-Length exceeds max_bytes
    monkeypatch.setattr(
        model_artifacts.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(b"hello", content_length="100"),
    )
    with pytest.raises(ModelArtifactIntegrityError, match="Content-Length"):
        model_artifacts._download_file(
            YOLOWORLD_ARTIFACT.url,
            destination,
            max_bytes=10,
        )

    # Rejects when streamed body exceeds max_bytes without Content-Length
    monkeypatch.setattr(
        model_artifacts.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(b"0123456789ABCDEF"),
    )
    with pytest.raises(ModelArtifactIntegrityError, match="exceeded expected size"):
        model_artifacts._download_file(
            YOLOWORLD_ARTIFACT.url,
            destination,
            max_bytes=8,
        )


def test_flake_nix_artifacts_match_python_manifest() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    flake_path = repo_root / "flake.nix"
    flake_text = flake_path.read_text(encoding="utf-8")

    def _extract_fetchurl(binding_name: str) -> tuple[str, str]:
        pattern = re.compile(
            rf"{re.escape(binding_name)}\s*=\s*pkgs\.fetchurl\s*\{{(.*?)\}};",
            re.DOTALL,
        )
        match = pattern.search(flake_text)
        assert match is not None, (
            f"Missing pkgs.fetchurl block '{binding_name}' in flake.nix"
        )
        block = match.group(1)
        url_match = re.search(r'url\s*=\s*"([^"]+)";', block)
        sha_match = re.search(r'sha256\s*=\s*"([^"]+)";', block)
        assert url_match is not None and sha_match is not None, (
            f"Incomplete fetchurl block '{binding_name}' in flake.nix"
        )
        return url_match.group(1), sha_match.group(1)

    expected_bindings = (
        ("yoloworldWeights", YOLOWORLD_ARTIFACT),
        ("clipWeights", CLIP_ARTIFACT),
        ("qrdetWeights", QRDET_ARTIFACT),
        ("tesseractFraTraineddata", TESSERACT_FRA_ARTIFACT),
    )
    for binding_name, spec in expected_bindings:
        nix_url, nix_sha256 = _extract_fetchurl(binding_name)
        assert nix_url == spec.url
        assert nix_sha256 == spec.sha256

    assert QRDET_RELEASE_URL in flake_text
