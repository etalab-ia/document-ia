# syntax=docker/dockerfile:1
FROM nixos/nix:2.28.5@sha256:e2bff159b88c242722022a273d3832a4392f01dba35b796204d852b89042526e AS builder

WORKDIR /app

COPY flake.nix flake.lock /app/

RUN nix --extra-experimental-features "nix-command flakes" build path:/app#runtime-evals --out-link /nix-env \
 && nix --extra-experimental-features "nix-command flakes" build path:/app#build-tools --out-link /nix-build-tools

ENV PATH="/nix-build-tools/bin:/nix-env/bin:${PATH}" \
    SSL_CERT_FILE="/nix-env/etc/ssl/certs/ca-bundle.crt" \
    NIX_SSL_CERT_FILE="/nix-env/etc/ssl/certs/ca-bundle.crt" \
    GIT_SSL_CAINFO="/nix-build-tools/etc/ssl/certs/ca-bundle.crt" \
    CURL_CA_BUNDLE="/nix-build-tools/etc/ssl/certs/ca-bundle.crt" \
    UV_PYTHON="/nix-env/bin/python3.14" \
    UV_PYTHON_DOWNLOADS="never" \
    UV_NO_EDITABLE="1" \
    UV_PROJECT_ENVIRONMENT="/app/document-ia-evals/.venv"

COPY pyproject.toml uv.lock /app/
COPY document-ia-api/pyproject.toml document-ia-api/README.md /app/document-ia-api/
COPY document-ia-auto-scaler/pyproject.toml document-ia-auto-scaler/README.md /app/document-ia-auto-scaler/
COPY document-ia-evals/pyproject.toml document-ia-evals/README.md /app/document-ia-evals/
COPY document-ia-infra/pyproject.toml document-ia-infra/README.md /app/document-ia-infra/
COPY document-ia-schemas/pyproject.toml document-ia-schemas/README.md /app/document-ia-schemas/
COPY document-ia-worker/pyproject.toml document-ia-worker/README.md /app/document-ia-worker/

WORKDIR /app/document-ia-evals

RUN uv sync --locked --no-dev --no-install-workspace

COPY document-ia-schemas /app/document-ia-schemas
COPY document-ia-infra /app/document-ia-infra
COPY document-ia-api /app/document-ia-api
COPY document-ia-evals /app/document-ia-evals

RUN uv sync --locked --no-dev --no-editable \
 && rm -rf /root/.cache/uv /app/flake.nix /app/flake.lock \
 && chmod +x /app/document-ia-evals/start_evals.sh

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
 && mkdir -p /app/document-ia-evals/.streamlit /app/document-ia-evals/src/document_ia_evals/static/review-pdfs \
 && chown 10001:10001 /app/document-ia-evals/.streamlit /app/document-ia-evals/src/document_ia_evals/static/review-pdfs \
 && chmod 0755 /app/document-ia-evals/.streamlit /app/document-ia-evals/src/document_ia_evals/static/review-pdfs

FROM scratch

COPY --from=builder /runtime-root/ /
COPY --from=builder /app /app

ENV PATH="/app/document-ia-evals/.venv/bin:/nix-env/bin" \
    LD_LIBRARY_PATH="/nix-env/lib" \
    MAGIC="/nix-env/share/misc/magic.mgc" \
    SSL_CERT_FILE="/nix-env/etc/ssl/certs/ca-bundle.crt" \
    NIX_SSL_CERT_FILE="/nix-env/etc/ssl/certs/ca-bundle.crt" \
    PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1" \
    PYTHONPATH="/app/document-ia-evals/src" \
    HOME="/home/appuser" \
    PORT="8501"

WORKDIR /app/document-ia-evals
USER 10001:10001
EXPOSE 8501

CMD ["./start_evals.sh"]
