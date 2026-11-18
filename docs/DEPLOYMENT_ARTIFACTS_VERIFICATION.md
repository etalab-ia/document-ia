# Security Architecture: Verifiable Deployment Artifacts & Model Integrity

**Initiative**: #151 - Security Hardening
**Tracking Plan**: I1 (F6, F8) | **Priority**: P1
**Associated Design Decision**: D3 (Hosting, Operations & Cutover)
**GitHub Reference**: `Closes #166`

---

## 1. Executive Summary

Document IA processes sensitive user documents using Python dependencies, native system libraries (Tesseract OCR, libmagic, OpenCV, Zlib), and pre-trained AI/OCR model weights (`yolov8m-world.pt`, `qrdet-s.pt`, `fra.traineddata`).

Prior to Issue #166:
- Model weights were downloaded during `post_compile` by instantiating `YOLOWorld()` and `QRDetector()` (which immediately executed `torch.load` / `pickle` without cryptographic digest verification) or via unverified `curl` calls against moving branches (`main`).
- Cached weights in `.models/` were never re-verified across builds.
- Runtime model loading (`yoloworld_crop.py`, `extract_barcode_2ddoc_data.py`, `main.py`) did not verify SHA-256 digests before deserialization, allowed implicit runtime network downloads when files were missing, and logged a non-fatal warning in `_prewarm_models()` (*fail-open*).
- Container images and system packages were not locked by a reproducible cryptographic manifest.

This document specifies the architecture implemented for **I1 (#166)**:
1. **Cryptographic Pinning & Fail-Closed Runtime Verification (`model_artifacts.py`)**: All three AI/OCR artifacts are pinned to immutable upstream release tags and verified against SHA-256 digests at build time, container startup, and before any `torch.load` or Tesseract execution.
2. **Unified Python Workspace Lock (`uv.lock`)**: All direct and transitive Python dependencies across the monorepo are locked in the root `uv.lock`, with `pyright` isolated to `dev` dependency groups and `uv run --no-project` enforced in `post_compile`.
3. **Reproducible Nix-Based Container Artifacts (`flake.nix` + `flake.lock` + `Dockerfile`)**: System packages (`python314`, `uv`, `tesseract`, `file`/`libmagic`, `glib`, `libGL`, `zlib`, `openssl`, `cacert`) and model derivations (`pkgs.fetchurl`) are locked in `flake.lock` and built into 6 role-specific container images published to **GitHub Container Registry (`ghcr.io/etalab-ia/*`)**.

---

## 2. Pinned AI & OCR Model Artifacts

All model artifacts are defined in [`document-ia-worker/src/document_ia_worker/core/model_artifacts.py`](file:///Users/nsagon/Projects/beta/documentAI/document-ia/document-ia-worker/src/document_ia_worker/core/model_artifacts.py) and mirrored in [`flake.nix`](file:///Users/nsagon/Projects/beta/documentAI/document-ia/flake.nix):

| Artifact Key | File Name | Relative Path in `.models/` | Immutable Source URL | SHA-256 Digest | Size (Bytes) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `yoloworld` | `yolov8m-world.pt` | `yolov8m-world.pt` | `https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8m-world.pt` | `8ede3cfa80c243608a9ef14d54f55030efc9d7a8635af245fc6d67efdf294aaa` | `58,610,242` |
| `qrdet` | `qrdet-s.pt` | `qrdet/qrdet-s.pt` | `https://github.com/Eric-Canas/qrdet/releases/download/v2.0_release/qrdet-s.pt` | `50a9113970a878a51206d5caabcc2c758856f751944199a2c371d872fd768af1` | `23,970,659` |
| `tesseract_fra` | `fra.traineddata` | `tessdata/fra.traineddata` | `https://github.com/tesseract-ocr/tessdata_fast/raw/4.1.0/fra.traineddata` | `ced037562e8c80c13122dece28dd477d399af80911a28791a66a63ac1e3445ca` | `1,130,365` |

Additionally, `qrdet/current_release.txt` is written and verified with exact content `https://github.com/Eric-Canas/qrdet/releases/download/v2.0_release` (without trailing newline) so `QRDetector` never attempts an outbound HTTP check at runtime.

---

## 3. Verification Lifecycle (Buildpack & Nix Container -> Runtime)

```mermaid
sequenceDiagram
    autonumber
    participant Build as Build (Scalingo post_compile / Nix Docker build)
    participant Manifest as SHA-256 Manifest (model_artifacts.py & flake.nix)
    participant Store as Local Storage (.models/)
    participant Boot as Worker Startup (start_worker.sh & main.py)
    participant Loader as Runtime Loaders (YOLOWorld / QRDetector / Tesseract)

    Build->>Manifest: Read pinned URLs & expected SHA-256 digests
    alt Cached artifact exists in .models/
        Build->>Store: Verify SHA-256 of cached file
        alt Digest mismatch
            Build->>Store: Remove corrupted file & re-download to .part temp file
        end
    else Artifact missing
        Build->>Store: Download raw bytes to .part temp file (no torch.load)
    end
    Build->>Store: Verify SHA-256 of .part before atomic rename + write current_release.txt
    Note over Build,Store: Any digest mismatch aborts the build immediately (exit 1)

    Boot->>Manifest: model_artifacts --verify on .models/
    Note over Boot: Missing or altered artifact -> exit 1 (Fail-Closed)
    Boot->>Loader: _prewarm_models() at worker startup
    Loader->>Manifest: verify_file_sha256() before any torch.load() or OCR call
    Note over Loader: Implicit runtime downloads are strictly forbidden
```

### Runtime Verification API

- `verify_file_sha256(path: Path, expected_sha256: str, artifact_name: str) -> Path`:
  Validates file existence, regular file status, and SHA-256 digest. Caches verified results in memory keyed by `(resolved_path, st_mtime_ns, st_size, expected_sha256)` so repeated document workflows do not re-hash unchanged files on disk. Raises `ModelArtifactIntegrityError` on any mismatch.
- `resolve_and_verify_yoloworld_path(models_dir: Path | None = None) -> Path`:
  Called by `get_yoloworld_model()` in `yoloworld_crop.py` prior to `YOLOWorld(str(verified_path))`.
- `resolve_and_verify_qrdet_dir(model_size: str = "s", models_dir: Path | None = None) -> Path`:
  Called by `_get_qrdet_detector()` in `extract_barcode_2ddoc_data.py` prior to `QRDetector(...)`. Rejects any unpinned `model_size`.
- `resolve_and_verify_tesseract_lang_data(lang: str = "fra", models_dir: Path | None = None) -> Path | None`:
  Called by `_prewarm_models()` in `main.py` and `ExtractContentOcrStep._prepare_step()` in `extract_content_ocr.py`.

### CLI Usage

```bash
# Download missing or repair corrupted artifacts in .models/ and verify SHA-256 digests
PYTHONPATH=document-ia-worker/src uv run --no-project python -m document_ia_worker.core.model_artifacts \
  --download-and-verify \
  --models-dir document-ia-worker/.models

# Strictly verify existing artifacts without network access (returns 0 if valid, 1 otherwise)
PYTHONPATH=document-ia-worker/src uv run --no-project python -m document_ia_worker.core.model_artifacts \
  --verify \
  --models-dir document-ia-worker/.models

# Print JSON inventory of verified model artifacts
PYTHONPATH=document-ia-worker/src uv run --no-project python -m document_ia_worker.core.model_artifacts \
  --print-inventory \
  --models-dir document-ia-worker/.models
```

---

## 4. Reproducible Nix-Based Container Artifacts & GHCR CD Pipeline

To eliminate unpinned OS package installation (`apt-get`) and satisfy target hosting requirements (**Decision D3**), all 6 deployment images are built using **Nix** (`flake.nix` + `flake.lock`) and `uv sync --locked --no-dev --no-editable`:

| GHCR Image Repository | Dockerfile | Target | Role / Entrypoint |
| :--- | :--- | :--- | :--- |
| `ghcr.io/etalab-ia/document-ia-api` | `document-ia-api/Dockerfile` | *(default)* | FastAPI / Gunicorn + Uvicorn workers (`main:app`) |
| `ghcr.io/etalab-ia/document-ia-worker` | `document-ia-worker/Dockerfile` | `worker` | Redis Stream worker + Tesseract + OpenCV + 3 verified models (`./bin/start_worker.sh`) |
| `ghcr.io/etalab-ia/document-ia-remove-ppi` | `document-ia-worker/Dockerfile` | `remove_ppi` | Cron job `0 3 * * *` (`src/document_ia_task_scheduler/task/remove_ppi/main.py`) |
| `ghcr.io/etalab-ia/document-ia-replicate-analytics` | `document-ia-worker/Dockerfile` | `replicate_analytics` | Cron job `0 5 * * *` (`src/document_ia_task_scheduler/task/replicate_analytics/main.py`) |
| `ghcr.io/etalab-ia/document-ia-evals` | `document-ia-evals/Dockerfile` | *(default)* | Streamlit evaluation dashboard (`./start_evals.sh`) |
| `ghcr.io/etalab-ia/document-ia-auto-scaler` | `document-ia-auto-scaler/Dockerfile` | *(default)* | Queue-lag auto-scaler daemon (`python -m document_ia_auto_scaler.main`) |

### Local Build & Verification Commands

```bash
# Build all 6 images from the repository root
docker build -f document-ia-api/Dockerfile -t document-ia-api:local .
docker build -f document-ia-worker/Dockerfile --target worker -t document-ia-worker:local .
docker build -f document-ia-worker/Dockerfile --target remove_ppi -t document-ia-remove-ppi:local .
docker build -f document-ia-worker/Dockerfile --target replicate_analytics -t document-ia-replicate-analytics:local .
docker build -f document-ia-evals/Dockerfile -t document-ia-evals:local .
docker build -f document-ia-auto-scaler/Dockerfile -t document-ia-auto-scaler:local .
```

### Continuous Delivery Workflow (`.github/workflows/document-ia-cd.yml`)

On every pull request (verification mode) and every published release / `v*` tag (publish mode):
1. Builds all 6 Nix-backed container images.
2. Runs a **negative integrity test** on `document-ia-worker` proving that flipping 1 byte in a bundled model causes `model_artifacts --verify` and `get_yoloworld_model()` to fail closed before `torch.load`.
3. Runs **smoke tests** on all 6 images (including `_prewarm_models()` in the worker image).
4. Generates a **verifiable JSON & Markdown component inventory** recording the image SHA-256 digest, Nix store runtime path, `flake.lock` SHA-256, `uv.lock` SHA-256, and verified model hashes.
5. Pushes verified images to `ghcr.io/etalab-ia/*` on release/tag events.

---

## 5. Procedure for Updating a Pinned Model Artifact

To update any of the three pinned model weights (`yolov8m-world.pt`, `qrdet-s.pt`, or `fra.traineddata`):

1. **Select an Immutable Upstream Release**:
   - Only immutable version tags or commit SHAs from the authorized upstream repositories (`ultralytics/assets`, `Eric-Canas/qrdet`, `tesseract-ocr/tessdata_fast`) are permitted. Moving branch references (such as `main` or `latest`) are prohibited.
2. **Compute and Verify the SHA-256 Digest**:
   ```bash
   curl -fSL "<IMMUTABLE_RELEASE_URL>" -o /tmp/candidate_artifact
   shasum -a 256 /tmp/candidate_artifact
   wc -c /tmp/candidate_artifact
   ```
3. **Update Both Manifests Atomically**:
   - Update `url`, `sha256`, and `size_bytes` in [`document-ia-worker/src/document_ia_worker/core/model_artifacts.py`](../document-ia-worker/src/document_ia_worker/core/model_artifacts.py).
   - Update the matching `pkgs.fetchurl` block (`url` and `sha256`) in [`flake.nix`](../flake.nix).
4. **Refresh Local Cache & Run Parity Tests**:
   ```bash
   PYTHONPATH=document-ia-worker/src uv run --no-project python -m document_ia_worker.core.model_artifacts \
     --download-and-verify --models-dir document-ia-worker/.models
   cd document-ia-worker && uv run pytest tests/unit/test_model_artifacts_integrity.py tests/unit/test_extract_content_ocr_step.py tests/unit/test_extract_barcode_data.py tests/unit/test_yoloworld_crop.py
   ```

---

## 6. Dual Delivery Path (Scalingo Buildpack vs. Nix Containers) & Reproducibility Scope

### 6.1. Current Scalingo Hosting (Buildpack Path)
On Scalingo, deployments run through the Python buildpack:
- Python dependencies are installed from the locked root [`uv.lock`](../uv.lock).
- [`document-ia-worker/bin/post_compile`](../document-ia-worker/bin/post_compile) invokes `python -m document_ia_worker.core.model_artifacts --download-and-verify` using `uv run --no-project` (preventing any secondary dependency resolution) and verifies SHA-256 digests even when `.models/` is restored from the Scalingo build cache.
- [`document-ia-worker/bin/start_worker.sh`](../document-ia-worker/bin/start_worker.sh) and `_prewarm_models()` re-verify all three SHA-256 digests at boot before loading weights into memory.

### 6.2. Verifiability vs. Bit-for-Bit Reproducibility
- **Guaranteed Verifiability**: Every input to the build is cryptographically pinned (`flake.lock` NAR hashes for Nix system packages, `uv.lock` wheel/sdist SHA-256 hashes for Python packages, and SHA-256 digests for the three AI/OCR models). Each CD run publishes the resulting container image digest (`sha256:...`) and component inventory JSON.
- **Explicit Scope Limit on Bit-for-Bit Reproducibility**: While input derivations are locked via Nix and `uv.lock`, Docker/BuildKit layer assembly (`RUN uv sync` filesystem timestamps and OCI layer metadata) is **not** claimed to be bit-for-bit identical across independent builds unless explicitly measured. Consumers and operators must verify deployments against the published GHCR image digest and component inventory artifact.
