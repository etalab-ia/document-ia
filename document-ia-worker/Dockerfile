# syntax=docker/dockerfile:1

# ==============================================================================
# Shared scheduler base (used by cron.json targets: remove_ppi & replicate_analytics)
# ==============================================================================
FROM nixos/nix:2.28.5 AS scheduler_base

WORKDIR /app

COPY flake.nix flake.lock /app/

RUN nix --extra-experimental-features "nix-command flakes" build path:/app#runtime-scheduler --out-link /nix-env \
 && ln -sf /nix-env/bin/bash /bin/bash \
 && mkdir -p /usr/bin /home/appuser /tmp \
 && ln -sf /nix-env/bin/env /usr/bin/env \
 && echo "appuser:x:10001:10001:appuser:/home/appuser:/nix-env/bin/bash" >> /etc/passwd \
 && echo "appuser:x:10001:" >> /etc/group \
 && chown -R 10001:10001 /home/appuser \
 && chmod 1777 /tmp \
 && nix-collect-garbage -d

ENV PATH="/app/document-ia-worker/.venv/bin:/nix-env/bin:${PATH}" \
    LD_LIBRARY_PATH="/nix-env/lib" \
    MAGIC="/nix-env/share/misc/magic.mgc" \
    SSL_CERT_FILE="/nix-env/etc/ssl/certs/ca-bundle.crt" \
    NIX_SSL_CERT_FILE="/nix-env/etc/ssl/certs/ca-bundle.crt" \
    UV_PYTHON="/nix-env/bin/python3.14" \
    UV_PYTHON_DOWNLOADS="never" \
    UV_NO_EDITABLE="1" \
    PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1" \
    PYTHONPATH="/app/document-ia-worker/src" \
    HOME="/home/appuser"

COPY pyproject.toml uv.lock /app/
COPY document-ia-schemas /app/document-ia-schemas
COPY document-ia-infra /app/document-ia-infra
COPY document-ia-worker /app/document-ia-worker

WORKDIR /app/document-ia-worker

RUN uv sync --locked --no-dev --no-editable \
 && rm -rf /root/.cache/uv \
 && chown -R 10001:10001 /app/document-ia-worker

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
FROM nixos/nix:2.28.5 AS worker

WORKDIR /app

COPY flake.nix flake.lock /app/

RUN nix --extra-experimental-features "nix-command flakes" build path:/app#runtime-worker --out-link /nix-env \
 && nix --extra-experimental-features "nix-command flakes" build path:/app#model-artifacts --out-link /nix-models \
 && ln -sf /nix-env/bin/bash /bin/bash \
 && mkdir -p /usr/bin /home/appuser /tmp \
 && ln -sf /nix-env/bin/env /usr/bin/env \
 && echo "appuser:x:10001:10001:appuser:/home/appuser:/nix-env/bin/bash" >> /etc/passwd \
 && echo "appuser:x:10001:" >> /etc/group \
 && chown -R 10001:10001 /home/appuser \
 && chmod 1777 /tmp \
 && nix-collect-garbage -d

ENV PATH="/app/document-ia-worker/.venv/bin:/nix-env/bin:${PATH}" \
    LD_LIBRARY_PATH="/nix-env/lib" \
    MAGIC="/nix-env/share/misc/magic.mgc" \
    SSL_CERT_FILE="/nix-env/etc/ssl/certs/ca-bundle.crt" \
    NIX_SSL_CERT_FILE="/nix-env/etc/ssl/certs/ca-bundle.crt" \
    UV_PYTHON="/nix-env/bin/python3.14" \
    UV_PYTHON_DOWNLOADS="never" \
    UV_NO_EDITABLE="1" \
    PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1" \
    PYTHONPATH="/app/document-ia-worker/src" \
    MODELS_DIR="/app/document-ia-worker/.models" \
    YOLOWORLD_PATH="/app/document-ia-worker/.models/yolov8m-world.pt" \
    QRDET_WEIGHTS_DIR="/app/document-ia-worker/.models/qrdet" \
    TESSDATA_PREFIX="/app/document-ia-worker/.models/tessdata" \
    MALLOC_ARENA_MAX="2" \
    OMP_NUM_THREADS="1" \
    MKL_NUM_THREADS="1" \
    OPENBLAS_NUM_THREADS="1" \
    OPENCV_FOR_THREADS_NUM="1" \
    HOME="/home/appuser"

COPY pyproject.toml uv.lock /app/
COPY document-ia-schemas /app/document-ia-schemas
COPY document-ia-infra /app/document-ia-infra
COPY document-ia-worker /app/document-ia-worker

WORKDIR /app/document-ia-worker

RUN mkdir -p /app/document-ia-worker/.models \
 && cp -RL /nix-models/. /app/document-ia-worker/.models/ \
 && chmod -R u+rwX,go+rX /app/document-ia-worker/.models \
 && uv sync --locked --no-dev --no-editable \
 && rm -rf /root/.cache/uv \
 && python -m document_ia_worker.core.model_artifacts --verify --models-dir /app/document-ia-worker/.models \
 && chmod +x /app/document-ia-worker/bin/start_worker.sh /app/document-ia-worker/bin/post_compile \
 && chown -R 10001:10001 /app/document-ia-worker

USER 10001:10001
CMD ["./bin/start_worker.sh"]
