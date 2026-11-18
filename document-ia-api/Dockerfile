# syntax=docker/dockerfile:1
FROM nixos/nix:2.28.5

WORKDIR /app

COPY flake.nix flake.lock /app/

RUN nix --extra-experimental-features "nix-command flakes" build path:/app#runtime-api --out-link /nix-env \
 && ln -sf /nix-env/bin/bash /bin/bash \
 && mkdir -p /usr/bin /home/appuser /tmp \
 && ln -sf /nix-env/bin/env /usr/bin/env \
 && echo "appuser:x:10001:10001:appuser:/home/appuser:/nix-env/bin/bash" >> /etc/passwd \
 && echo "appuser:x:10001:" >> /etc/group \
 && chown -R 10001:10001 /home/appuser \
 && chmod 1777 /tmp \
 && nix-collect-garbage -d

ENV PATH="/app/document-ia-api/.venv/bin:/nix-env/bin:${PATH}" \
    LD_LIBRARY_PATH="/nix-env/lib" \
    MAGIC="/nix-env/share/misc/magic.mgc" \
    SSL_CERT_FILE="/nix-env/etc/ssl/certs/ca-bundle.crt" \
    NIX_SSL_CERT_FILE="/nix-env/etc/ssl/certs/ca-bundle.crt" \
    UV_PYTHON="/nix-env/bin/python3.14" \
    UV_PYTHON_DOWNLOADS="never" \
    UV_NO_EDITABLE="1" \
    PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1" \
    PYTHONPATH="/app/document-ia-api/src" \
    HOME="/home/appuser" \
    PORT="8000"

COPY pyproject.toml uv.lock /app/
COPY document-ia-schemas /app/document-ia-schemas
COPY document-ia-infra /app/document-ia-infra
COPY document-ia-api /app/document-ia-api

WORKDIR /app/document-ia-api

RUN uv sync --locked --no-dev --no-editable \
 && rm -rf /root/.cache/uv \
 && chown -R 10001:10001 /app/document-ia-api

USER 10001:10001
EXPOSE 8000

CMD ["sh", "-c", "exec gunicorn -w ${WEB_CONCURRENCY:-4} -k uvicorn.workers.UvicornWorker -b 0.0.0.0:${PORT:-8000} main:app --chdir /app/document-ia-api/src/document_ia_api"]
