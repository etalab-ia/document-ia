# syntax=docker/dockerfile:1

# ==============================================================================
# Shared scheduler builder & runtime (remove_ppi & replicate_analytics)
# ==============================================================================
FROM nixos/nix:2.28.5@sha256:e2bff159b88c242722022a273d3832a4392f01dba35b796204d852b89042526e AS scheduler_builder

WORKDIR /app

COPY flake.nix flake.lock /app/

RUN nix --extra-experimental-features "nix-command flakes" build path:/app#runtime-scheduler --out-link /nix-env \
 && nix --extra-experimental-features "nix-command flakes" build path:/app#build-tools --out-link /nix-build-tools

ENV PATH="/nix-build-tools/bin:/nix-env/bin:${PATH}" \
    SSL_CERT_FILE="/nix-env/etc/ssl/certs/ca-bundle.crt" \
    NIX_SSL_CERT_FILE="/nix-env/etc/ssl/certs/ca-bundle.crt" \
    GIT_SSL_CAINFO="/nix-build-tools/etc/ssl/certs/ca-bundle.crt" \
    CURL_CA_BUNDLE="/nix-build-tools/etc/ssl/certs/ca-bundle.crt" \
    UV_PYTHON="/nix-env/bin/python3.14" \
    UV_PYTHON_DOWNLOADS="never" \
    UV_NO_EDITABLE="1" \
    UV_PROJECT_ENVIRONMENT="/app/document-ia-worker/.venv"

COPY pyproject.toml uv.lock /app/
COPY document-ia-api/pyproject.toml document-ia-api/README.md /app/document-ia-api/
COPY document-ia-auto-scaler/pyproject.toml document-ia-auto-scaler/README.md /app/document-ia-auto-scaler/
COPY document-ia-evals/pyproject.toml document-ia-evals/README.md /app/document-ia-evals/
COPY document-ia-infra/pyproject.toml document-ia-infra/README.md /app/document-ia-infra/
COPY document-ia-schemas/pyproject.toml document-ia-schemas/README.md /app/document-ia-schemas/
COPY document-ia-worker/pyproject.toml document-ia-worker/README.md /app/document-ia-worker/

WORKDIR /app/document-ia-worker

RUN uv sync --locked --no-dev --no-install-workspace

COPY document-ia-schemas /app/document-ia-schemas
COPY document-ia-infra /app/document-ia-infra
COPY document-ia-worker /app/document-ia-worker

RUN uv sync --locked --no-dev --no-editable \
 && rm -rf /root/.cache/uv /app/flake.nix /app/flake.lock

RUN mkdir -p /runtime-root/nix/store /runtime-root/bin /runtime-root/usr/bin /runtime-root/etc/ssl/certs /runtime-root/tmp /runtime-root/home/appuser \
 && nix-store -qR /nix-env | xargs -r -I{} cp -a {} /runtime-root/nix/store/ \
 && cp -a /nix-env /runtime-root/nix-env \
 && ln -sf /nix-env/bin/sh /runtime-root/bin/sh \
 && ln -sf /nix-env/bin/bash /runtime-root/bin/bash \
 && ln -sf /nix-env/bin/env /runtime-root/usr/bin/env \
 && ln -sf /nix-env/etc/ssl/certs/ca-bundle.crt /runtime-root/etc/ssl/certs/ca-certificates.crt \
 && printf 'root:x:0:0:root:/root:/nix-env/bin/bash\nappuser:x:10001:10001:appuser:/home/appuser:/nix-env/bin/bash\n' > /runtime-root/etc/passwd \
 && printf 'root:x:0:\nappuser:x:10001:\n' > /runtime-root/etc/group \
 && printf 'hosts: files dns\n' > /runtime-root/etc/nsswitch.conf \
 && chown -R 10001:10001 /runtime-root/home/appuser \
 && chmod 1777 /runtime-root/tmp \
 && chown -R 0:0 /app \
 && chmod -R u=rwX,go=rX /app

FROM scratch AS scheduler_base

COPY --from=scheduler_builder /runtime-root/ /
COPY --from=scheduler_builder /app /app

ENV PATH="/app/document-ia-worker/.venv/bin:/nix-env/bin" \
    LD_LIBRARY_PATH="/nix-env/lib" \
    MAGIC="/nix-env/share/misc/magic.mgc" \
    SSL_CERT_FILE="/nix-env/etc/ssl/certs/ca-bundle.crt" \
    NIX_SSL_CERT_FILE="/nix-env/etc/ssl/certs/ca-bundle.crt" \
    PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1" \
    PYTHONPATH="/app/document-ia-worker/src" \
    HOME="/home/appuser"

WORKDIR /app/document-ia-worker

# ==============================================================================
# Target 1: remove_ppi (cron.json: 0 3 * * * python -u src/document_ia_task_scheduler/task/remove_ppi/main.py)
# ==============================================================================
FROM scheduler_base AS remove_ppi

USER 10001:10001
CMD ["python", "-u", "src/document_ia_task_scheduler/task/remove_ppi/main.py"]

# ==============================================================================
# Target 2: replicate_analytics (cron.json: 0 5 * * * python -u src/document_ia_task_scheduler/task/replicate_analytics/main.py)
# ==============================================================================
FROM scheduler_base AS replicate_analytics

USER 10001:10001
CMD ["python", "-u", "src/document_ia_task_scheduler/task/replicate_analytics/main.py"]

# ==============================================================================
# Target 3 (default): worker (with Tesseract, OpenCV/GL & SHA-256 verified models)
# ==============================================================================
FROM nixos/nix:2.28.5@sha256:e2bff159b88c242722022a273d3832a4392f01dba35b796204d852b89042526e AS worker_builder

WORKDIR /app

COPY flake.nix flake.lock /app/

RUN nix --extra-experimental-features "nix-command flakes" build path:/app#runtime-worker --out-link /nix-env \
 && nix --extra-experimental-features "nix-command flakes" build path:/app#build-tools --out-link /nix-build-tools \
 && nix --extra-experimental-features "nix-command flakes" build path:/app#model-artifacts --out-link /nix-models

ENV PATH="/nix-build-tools/bin:/nix-env/bin:${PATH}" \
    SSL_CERT_FILE="/nix-env/etc/ssl/certs/ca-bundle.crt" \
    NIX_SSL_CERT_FILE="/nix-env/etc/ssl/certs/ca-bundle.crt" \
    GIT_SSL_CAINFO="/nix-build-tools/etc/ssl/certs/ca-bundle.crt" \
    CURL_CA_BUNDLE="/nix-build-tools/etc/ssl/certs/ca-bundle.crt" \
    UV_PYTHON="/nix-env/bin/python3.14" \
    UV_PYTHON_DOWNLOADS="never" \
    UV_NO_EDITABLE="1" \
    UV_PROJECT_ENVIRONMENT="/app/document-ia-worker/.venv"

COPY pyproject.toml uv.lock /app/
COPY document-ia-api/pyproject.toml document-ia-api/README.md /app/document-ia-api/
COPY document-ia-auto-scaler/pyproject.toml document-ia-auto-scaler/README.md /app/document-ia-auto-scaler/
COPY document-ia-evals/pyproject.toml document-ia-evals/README.md /app/document-ia-evals/
COPY document-ia-infra/pyproject.toml document-ia-infra/README.md /app/document-ia-infra/
COPY document-ia-schemas/pyproject.toml document-ia-schemas/README.md /app/document-ia-schemas/
COPY document-ia-worker/pyproject.toml document-ia-worker/README.md /app/document-ia-worker/

WORKDIR /app/document-ia-worker

RUN uv sync --locked --no-dev --no-install-workspace

COPY document-ia-schemas /app/document-ia-schemas
COPY document-ia-infra /app/document-ia-infra
COPY document-ia-worker /app/document-ia-worker

RUN mkdir -p /app/document-ia-worker/.models \
 && cp -RL /nix-models/. /app/document-ia-worker/.models/ \
 && uv sync --locked --no-dev --no-editable \
 && rm -rf /root/.cache/uv /app/flake.nix /app/flake.lock \
 && PYTHONPATH="/app/document-ia-worker/src" /app/document-ia-worker/.venv/bin/python -m document_ia_worker.core.model_artifacts --verify --models-dir /app/document-ia-worker/.models \
 && chmod +x /app/document-ia-worker/bin/start_worker.sh /app/document-ia-worker/bin/post_compile

RUN mkdir -p /runtime-root/nix/store /runtime-root/bin /runtime-root/usr/bin /runtime-root/etc/ssl/certs /runtime-root/tmp /runtime-root/home/appuser \
 && nix-store -qR /nix-env | xargs -r -I{} cp -a {} /runtime-root/nix/store/ \
 && cp -a /nix-env /runtime-root/nix-env \
 && ln -sf /nix-env/bin/sh /runtime-root/bin/sh \
 && ln -sf /nix-env/bin/bash /runtime-root/bin/bash \
 && ln -sf /nix-env/bin/env /runtime-root/usr/bin/env \
 && ln -sf /nix-env/etc/ssl/certs/ca-bundle.crt /runtime-root/etc/ssl/certs/ca-certificates.crt \
 && printf 'root:x:0:0:root:/root:/nix-env/bin/bash\nappuser:x:10001:10001:appuser:/home/appuser:/nix-env/bin/bash\n' > /runtime-root/etc/passwd \
 && printf 'root:x:0:\nappuser:x:10001:\n' > /runtime-root/etc/group \
 && printf 'hosts: files dns\n' > /runtime-root/etc/nsswitch.conf \
 && chown -R 10001:10001 /runtime-root/home/appuser \
 && chmod 1777 /runtime-root/tmp \
 && chown -R 0:0 /app \
 && chmod -R u=rwX,go=rX /app \
 && chmod -R a-w,a+rX /app/document-ia-worker/.models

FROM scratch AS worker

COPY --from=worker_builder /runtime-root/ /
COPY --from=worker_builder /app /app

ENV PATH="/app/document-ia-worker/.venv/bin:/nix-env/bin" \
    LD_LIBRARY_PATH="/nix-env/lib" \
    MAGIC="/nix-env/share/misc/magic.mgc" \
    SSL_CERT_FILE="/nix-env/etc/ssl/certs/ca-bundle.crt" \
    NIX_SSL_CERT_FILE="/nix-env/etc/ssl/certs/ca-bundle.crt" \
    PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1" \
    PYTHONPATH="/app/document-ia-worker/src" \
    MODELS_DIR="/app/document-ia-worker/.models" \
    YOLOWORLD_PATH="/app/document-ia-worker/.models/yolov8m-world.pt" \
    CLIP_WEIGHTS_PATH="/app/document-ia-worker/.models/clip/ViT-B-32.pt" \
    QRDET_WEIGHTS_DIR="/app/document-ia-worker/.models/qrdet" \
    TESSDATA_PREFIX="/app/document-ia-worker/.models/tessdata" \
    YOLO_OFFLINE="true" \
    YOLO_CONFIG_DIR="/tmp/Ultralytics" \
    MALLOC_ARENA_MAX="2" \
    OMP_NUM_THREADS="1" \
    MKL_NUM_THREADS="1" \
    OPENBLAS_NUM_THREADS="1" \
    OPENCV_FOR_THREADS_NUM="1" \
    HOME="/home/appuser"

WORKDIR /app/document-ia-worker
USER 10001:10001
CMD ["./bin/start_worker.sh"]
