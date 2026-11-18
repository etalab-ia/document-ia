#!/usr/bin/env bash
# Vérifie l'opposabilité du contrôle Gitleaks (Issue #167) :
# 1. Intégrité stricte de `.gitleaks-baseline.json` (exactement les 3 empreintes historiques connues).
# 2. Contrôle négatif : un commit contenant un placeholder autorisé (`YOUR_API_KEY`) passe (exit 0).
# 3. Sonde synthétique 1 (`document-ia-token`) : un token `dia_...` construit dynamiquement est détecté et bloqué (exit != 0).
# 4. Sonde synthétique 2 (`generic-api-key`) : une clé API générique haute entropie construite dynamiquement est détectée et bloquée (exit != 0).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="${REPO_ROOT}/.gitleaks.toml"
BASELINE_PATH="${REPO_ROOT}/.gitleaks-baseline.json"

if [ ! -f "${CONFIG_PATH}" ]; then
  echo "erreur : fichier de configuration introuvable : ${CONFIG_PATH}" >&2
  exit 1
fi

if [ ! -f "${BASELINE_PATH}" ]; then
  echo "erreur : fichier baseline introuvable : ${BASELINE_PATH}" >&2
  exit 1
fi

echo "==> [1/4] Vérification de l'intégrité de .gitleaks-baseline.json..."
python3 - "${BASELINE_PATH}" <<'PYEOF'
import json
from pathlib import Path
import sys

baseline_path = Path(sys.argv[1])
data = json.loads(baseline_path.read_text(encoding="utf-8"))
if not isinstance(data, list):
    print("erreur: .gitleaks-baseline.json doit contenir une liste JSON.", file=sys.stderr)
    sys.exit(1)

expected_fingerprints = [
    "e97f180fef487112272dfd3df4adf4a13de2b3e3:.github/workflows/document-ia-ci.yml:generic-api-key:55",
    "35f258277c8fc310938aa24f7d30e9c487a7ba1c:.github/workflows/document-ia-ci.yml:document-ia-token:75",
    "c2ecb3214a4196f8405c42ca5cdd8d2e8c962b9c:document-ia-worker/tests/unit/workflow/step/test_download_file_failure_class.py:generic-api-key:20",
]
actual_fingerprints = [
    str(entry.get("Fingerprint", "")) for entry in data if isinstance(entry, dict)
]

if len(actual_fingerprints) != len(data) or sorted(actual_fingerprints) != sorted(expected_fingerprints):
    print(
        "erreur: .gitleaks-baseline.json a été modifié sans autorisation.\n"
        f"Attendu ({len(expected_fingerprints)}) : {sorted(expected_fingerprints)}\n"
        f"Obtenu  ({len(actual_fingerprints)}) : {sorted(actual_fingerprints)}",
        file=sys.stderr,
    )
    sys.exit(1)

print(f"OK : .gitleaks-baseline.json contient exactement les {len(expected_fingerprints)} empreintes historiques figées.")
PYEOF

if ! command -v gitleaks >/dev/null 2>&1; then
  echo "erreur : binaire 'gitleaks' introuvable dans le PATH." >&2
  exit 1
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

git -C "${TMP_DIR}" init -q
git -C "${TMP_DIR}" config user.name "Security Probe"
git -C "${TMP_DIR}" config user.email "security-probe@document-ia.local"
git -C "${TMP_DIR}" config commit.gpgsign false
cp "${CONFIG_PATH}" "${TMP_DIR}/.gitleaks.toml"

echo "==> [2/4] Contrôle négatif (placeholder autorisé dans .gitleaks.toml)..."
PlaceholderA="YOUR_API"
PlaceholderB="_KEY"
cat > "${TMP_DIR}/sample.py" <<EOF
# Exemple avec placeholder autorisé par la liste blanche
HEADER_EXAMPLE = "Bearer ${PlaceholderA}${PlaceholderB}"
EOF
git -C "${TMP_DIR}" add .gitleaks.toml sample.py
git -C "${TMP_DIR}" commit -q -m "chore: add clean file with allowed placeholder"
CLEAN_SHA="$(git -C "${TMP_DIR}" rev-parse HEAD)"

if ! gitleaks git --config "${TMP_DIR}/.gitleaks.toml" --redact "${TMP_DIR}"; then
  echo "erreur : Gitleaks a échoué sur un commit propre contenant uniquement un placeholder autorisé." >&2
  exit 1
fi
echo "OK : Le contrôle négatif passe (exit 0)."

echo "==> [3/4] Sonde synthétique #1 (règle personnalisée 'document-ia-token')..."
# Construction dynamique par fragments pour qu'aucun secret littéral ne figure dans ce script
P1="dia_"
P2="prod_v1_"
P3="SYNTH999_"
P4="K8M2P9Q4R7T1V5W3X6Y8Z2B4N6M8Q1W3E5R7_"
P5="Z9X8"
SYNTH_DIA_TOKEN="${P1}${P2}${P3}${P4}${P5}"

printf 'SYNTHETIC_TOKEN = "%s"\n' "${SYNTH_DIA_TOKEN}" > "${TMP_DIR}/leaked_token.py"
git -C "${TMP_DIR}" add leaked_token.py
git -C "${TMP_DIR}" commit -q -m "test: inject synthetic document-ia token"

REPORT_1="${TMP_DIR}/report_dia_token.json"
if gitleaks git \
  --config "${TMP_DIR}/.gitleaks.toml" \
  --report-format json \
  --report-path "${REPORT_1}" \
  --redact \
  "${TMP_DIR}" >/dev/null 2>&1; then
  echo "erreur : Gitleaks n'a PAS bloqué le token synthétique Document IA !" >&2
  exit 1
fi

python3 - "${REPORT_1}" "document-ia-token" <<'PYEOF'
import json
from pathlib import Path
import sys

report_path = Path(sys.argv[1])
expected_rule = sys.argv[2]
findings = json.loads(report_path.read_text(encoding="utf-8"))
rules = {item.get("RuleID") for item in findings if isinstance(item, dict)}
if expected_rule not in rules:
    print(
        f"erreur: la règle attendue '{expected_rule}' n'apparaît pas dans le rapport Gitleaks (règles: {sorted(rules)}).",
        file=sys.stderr,
    )
    sys.exit(1)
print(f"OK : Sonde synthétique bloquée avec succès par la règle '{expected_rule}'.")
PYEOF

echo "==> [4/4] Sonde synthétique #2 (règle par défaut 'generic-api-key')..."
git -C "${TMP_DIR}" reset -q --hard "${CLEAN_SHA}"

K_LEFT="api"
K_RIGHT="_key"
S1="7f9a2b4c"
S2="8e1d3f5a"
S3="6c0b9e2d"
S4="4a7f1c8b"
S5="3e6d9a2f"
printf '%s%s = "%s"\n' "${K_LEFT}" "${K_RIGHT}" "${S1}${S2}${S3}${S4}${S5}" > "${TMP_DIR}/leaked_generic.py"
git -C "${TMP_DIR}" add leaked_generic.py
git -C "${TMP_DIR}" commit -q -m "test: inject synthetic generic high-entropy secret"

REPORT_2="${TMP_DIR}/report_generic.json"
if gitleaks git \
  --config "${TMP_DIR}/.gitleaks.toml" \
  --report-format json \
  --report-path "${REPORT_2}" \
  --redact \
  "${TMP_DIR}" >/dev/null 2>&1; then
  echo "erreur : Gitleaks n'a PAS bloqué le secret générique haute entropie !" >&2
  exit 1
fi

python3 - "${REPORT_2}" "generic-api-key" <<'PYEOF'
import json
from pathlib import Path
import sys

report_path = Path(sys.argv[1])
expected_rule = sys.argv[2]
findings = json.loads(report_path.read_text(encoding="utf-8"))
rules = {item.get("RuleID") for item in findings if isinstance(item, dict)}
if expected_rule not in rules:
    print(
        f"erreur: la règle attendue '{expected_rule}' n'apparaît pas dans le rapport Gitleaks (règles: {sorted(rules)}).",
        file=sys.stderr,
    )
    sys.exit(1)
print(f"OK : Sonde synthétique bloquée avec succès par la règle '{expected_rule}'.")
PYEOF

echo "✅ Vérification Gitleaks (baseline + sondes synthétiques) terminée avec succès."
