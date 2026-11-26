# QA Local Data Directory (`qa/data/`)

This directory is **ignored by Git** (except `.gitkeep` and this `README.md`) so you can safely store real documents (which may contain PII) and custom local YAML scenarios without ever committing them.

## Structure

```text
qa/data/
├── cni/
│   └── cni_sample.pdf
├── passeport/
│   └── passeport_sample.pdf
├── avis_imposition/
│   └── avis_sample.pdf
├── bulletin_salaire/
│   └── bulletin_sample.pdf
├── 2ddoc/
│   └── doc_2ddoc.pdf
└── scenarios/
    └── real_documents.yaml   # Auto-discovered by `uv run python -m qa run`
```

## Quick Start

1. Copy the template scenario file into `qa/data/scenarios/`:
   ```bash
   mkdir -p qa/data/scenarios
   cp qa/scenarios/real_documents.yaml.example qa/data/scenarios/real_documents.yaml
   ```
2. Place your test files in `qa/data/` and update paths/expectations in `qa/data/scenarios/real_documents.yaml`.
3. Run your real-document scenarios:
   ```bash
   uv run python -m qa run --env staging --tags real
   ```
4. You can also store fixtures outside the repo and point to them with `--fixtures-dir /path/to/docs` or `QA_FIXTURES_DIR=/path/to/docs`.
