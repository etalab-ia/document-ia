# syntax=docker/dockerfile:1
FROM nixos/nix:2.28.5

WORKDIR /app

COPY flake.nix flake.lock /app/

RUN nix --extra-experimental-features "nix-command flakes" build path:/app#runtime-auto-scaler --out-link /nix-env \
 && ln -sf /nix-env/bin/bash /bin/bash \
 && ln -sf /nix-env/bin/sh /bin/sh \
 && mkdir -p /usr/bin /home/appuser /tmp \
 && ln -sf /nix-env/bin/env /usr/bin/env \
 && echo "appuser:x:10001:10001:appuser:/home/appuser:/nix-env/bin/bash" >> /etc/passwd \
 && echo "appuser:x:10001:" >> /etc/group \
 && chown -R 10001:10001 /home/appuser \
 && chmod 1777 /tmp \
 && nix-collect-garbage -d

ENV PATH="/app/document-ia-auto-scaler/.venv/bin:/nix-env/bin:${PATH}" \
    LD_LIBRARY_PATH="/nix-env/lib" \
    SSL_CERT_FILE="/nix-env/etc/ssl/certs/ca-bundle.crt" \
    NIX_SSL_CERT_FILE="/nix-env/etc/ssl/certs/ca-bundle.crt" \
    UV_PYTHON="/nix-env/bin/python3.14" \
    UV_PYTHON_DOWNLOADS="never" \
    UV_NO_EDITABLE="1" \
    UV_PROJECT_ENVIRONMENT="/app/document-ia-auto-scaler/.venv" \
    PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1" \
    PYTHONPATH="/app/document-ia-auto-scaler/src" \
    HOME="/home/appuser"

COPY pyproject.toml uv.lock /app/
COPY document-ia-api/pyproject.toml document-ia-api/README.md /app/document-ia-api/
COPY document-ia-auto-scaler/pyproject.toml document-ia-auto-scaler/README.md /app/document-ia-auto-scaler/
COPY document-ia-evals/pyproject.toml document-ia-evals/README.md /app/document-ia-evals/
COPY document-ia-infra/pyproject.toml document-ia-infra/README.md /app/document-ia-infra/
COPY document-ia-schemas/pyproject.toml document-ia-schemas/README.md /app/document-ia-schemas/
COPY document-ia-worker/pyproject.toml document-ia-worker/README.md /app/document-ia-worker/
COPY document-ia-schemas /app/document-ia-schemas
COPY document-ia-infra /app/document-ia-infra
COPY document-ia-auto-scaler /app/document-ia-auto-scaler

WORKDIR /app/document-ia-auto-scaler

RUN uv sync --locked --no-dev --no-editable \
 && rm -rf /root/.cache/uv \
 && chown -R 10001:10001 /app/document-ia-auto-scaler

USER 10001:10001

CMD ["python", "-m", "document_ia_auto_scaler.main"]
