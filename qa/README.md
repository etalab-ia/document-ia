# 🧪 Document IA — Outil de QA, Non-Régression, Sécurité & Résilience (`qa/`)

Cet outil Python permet de jouer des campagnes de QA reproductibles **par environnement** (`local`, `sandbox`, `staging`/`develop`, `prod`) afin de :

1. **Vérifier l'absence de régression** entre la version en production (`sandbox`) et la nouvelle version (`staging` / `develop`) grâce à un comparateur automatique de rapports JSON (`qa compare`).
2. **Déclarer facilement des scénarios métier (1 fichier $\rightarrow$ 1 résultat attendu)** en YAML — soit avec des **fichiers réels locaux** (stockés hors Git pour protéger les PII), soit avec des **fixtures synthétiques générées à la volée** (PDF/PNG pure-Python, utilisables immédiatement ou en CI).
3. **Valider tout le durcissement sécurité & résilience** mergé sur `develop` (Anti-SSRF `#152`, Safe Logging `#153`, `purged` `#156`, API Key Binding `#159`, Isolation Webhooks `#160`, Rate Limit `#250`, Idempotency-Key `#148`, Anomalies de dates `#170`, Garde OOM 2D-Doc `#258`).
4. **Tester la montée en charge et la tolérance aux pannes (Chaos)** : concurrence configurable, retries idempotents sur `503`/`429`/`502`/`504`, injection de pannes (manuelle Scalingo, CLI Scalingo ou Docker) et vérification stricte **0 drop / 0 doublon / 0 HTTP 500**.

> 📘 Pour la documentation d'architecture en anglais, voir [docs/QA_TOOL.md](../docs/QA_TOOL.md).

---

## 📋 Sommaire

- [1. Prérequis & Configuration (`.env.qa`)](#1-prérequis--configuration-envqa)
- [2. Environnements & Garde-fous](#2-environnements--garde-fous)
- [3. Démarrage rapide : Campagne `sandbox` vs `staging`](#3-démarrage-rapide--campagne-sandbox-vs-staging)
- [4. Ajouter des scénarios avec tes vrais documents (1 fichier → 1 résultat)](#4-ajouter-des-scénarios-avec-tes-vrais-documents-1-fichier--1-résultat)
- [5. Référence du format YAML & Opérateurs d'assertions](#5-référence-du-format-yaml--opérateurs-dassertions)
- [6. Guide complet des commandes CLI](#6-guide-complet-des-commandes-cli)
- [7. Catalogue des tests intégrés & Fixtures synthétiques](#7-catalogue-des-tests-intégrés--fixtures-synthétiques)
- [8. Montée en charge & Simulation de pannes (Chaos)](#8-montée-en-charge--simulation-de-pannes-chaos)
- [9. Codes de sortie, Sécurité PII & Dépannage](#9-codes-de-sortie-sécurité-pii--dépannage)

---

## 1. Prérequis & Configuration (`.env.qa`)

L'outil n'ajoute **aucune dépendance externe** : il s'exécute directement dans le `.venv` du monorepo (`uv run python -m qa ...`).

### Étape 1 : Créer ton fichier de secrets local `.env.qa`

Copie le template à la racine du dépôt (le fichier `.env.qa` est **gitignoré**) :

```bash
cp .env.qa.example .env.qa
```

Renseigne les clés API des environnements que tu veux tester dans `.env.qa` :

```dotenv
# Clés API standards (obligatoires pour les envs ciblés)
QA_LOCAL_API_KEY=dia_dev_2_...
QA_SANDBOX_API_KEY=dia_sandbox_1_...
QA_STAGING_API_KEY=dia_staging_2_...

# Clés Admin PlatformAdmin (OPTIONNEL : uniquement pour --tags webhook)
QA_STAGING_ADMIN_API_KEY=dia_staging_2_...
QA_WEBHOOK_URL=https://example.com/qa-webhook

# Surcharge optionnelle d'URL ou de dossier de documents réels
# QA_STAGING_BASE_URL=https://api.staging.document-ia.beta.gouv.fr
# QA_FIXTURES_DIR=/chemin/vers/mes-documents-qa
```

### Ordre de priorité de la configuration
1. Options CLI (`--base-url`, `--fixtures-dir`)
2. Variables d'environnement déjà exportées dans ton shell (`export QA_STAGING_API_KEY=...`)
3. Fichier `.env.qa` à la racine du projet
4. Valeurs par défaut définies dans [`qa/environments.yaml`](./environments.yaml)

---

## 2. Environnements & Garde-fous

Les environnements sont définis dans [`qa/environments.yaml`](./environments.yaml) (sans aucun secret) :

| `--env` | Alias | URL par défaut | Profil | Charge / Chaos autorisés | Sécurité supplémentaire |
|---|---|---|---|---|---|
| `local` | — | `http://localhost:8000` | `develop` | ✅ Oui / ✅ Oui | — |
| `sandbox` | — | `https://api.sandbox.document-ia.beta.gouv.fr` | `baseline` | ✅ Oui / ✅ Oui | — |
| `staging` | `develop` | `https://api.staging.document-ia.beta.gouv.fr` | `develop` | ✅ Oui / ✅ Oui | — |
| `prod` | — | `https://api.document-ia.beta.gouv.fr` | `baseline` | ❌ Non / ❌ Non | Exige `--allow-prod` |

### Profils `baseline` vs `develop`
- **`baseline`** (`sandbox`, `prod`) : exécute par défaut uniquement les tests de non-régression compatibles avec la version actuellement en prod (`baseline`, `smoke`, `functional`, `negative`). Les tests propres aux nouvelles fonctionnalités de `develop` (Idempotency-Key, Anti-SSRF, anomalies `#170`, `purged`) sont automatiquement ignorés.
- **`develop`** (`staging`, `local`) : exécute par défaut la **baseline + toutes les nouvelles vérifications sécurité/résilience** (`security`, `idempotency`, `resilience`, `develop`).

### Garde-fou Production
- Cibler `prod` sans `--allow-prod` lève une erreur immédiate (`exit code 2`).
- `qa load` et `--chaos-mode != none` sont **strictement bloqués sur `prod`**, même avec `--allow-prod`.
- Le garde-fou vérifie aussi le **hostname résolu** (`--base-url` ou `QA_*_BASE_URL`) ainsi que le nom de l'application `--scalingo-app` (tout nom contenant `prod`/`production` est rejeté).

---

## 3. Démarrage rapide : Campagne `sandbox` vs `staging`

Voici le workflow type pour valider que ta branche `develop` (déployée sur `staging`) n'introduit **aucune régression** par rapport à `sandbox` (proche prod) :

```bash
# 1. Voir tous les scénarios et fixtures disponibles
uv run python -m qa list --env staging

# 2. Jouer la baseline sur sandbox et sauvegarder le rapport JSON
uv run python -m qa run --env sandbox --tags baseline --report qa/reports/sandbox.json

# 3. Jouer la suite complète (baseline + sécurité + idempotence + résilience) sur staging
uv run python -m qa run --env staging --report qa/reports/staging.json

# 4. Comparer automatiquement sandbox vs staging (code retour 1 si régression)
uv run python -m qa compare qa/reports/sandbox.json qa/reports/staging.json
```

> 💡 Tu peux aussi lancer un seul scénario pour déboguer rapidement :
> ```bash
> uv run python -m qa run --env staging --id smoke-health,proto-idempotency -v
> ```

---

## 4. Ajouter des scénarios avec tes vrais documents (1 fichier → 1 résultat)

Pour ne jamais risquer de commiter des documents personnels (PII) dans Git, le dossier **`qa/data/` est entièrement gitignoré** (seuls `.gitkeep` et `README.md` sont suivis). De plus, tout fichier YAML placé dans **`qa/data/scenarios/` est découvert automatiquement** par `qa run` et `qa list` !

### Pas à pas

1. **Copie le modèle de scénarios réels dans le dossier local gitignoré :**
   ```bash
   mkdir -p qa/data/scenarios
   cp qa/scenarios/real_documents.yaml.example qa/data/scenarios/real_documents.yaml
   ```

2. **Dépose tes fichiers PDF / JPG / PNG dans `qa/data/` :**
   ```text
   qa/data/
   ├── cni/
   │   └── cni_jean_dupont.pdf
   ├── passeport/
   │   └── passeport_test.jpg
   ├── avis_imposition/
   │   └── avis_2024.pdf
   ├── bulletin_salaire/
   │   └── bulletin_mars.pdf
   ├── 2ddoc/
   │   └── justificatif_2ddoc.pdf
   └── scenarios/
       └── real_documents.yaml   # Auto-découvert !
   ```
   *(Alternative : stocke tes documents n'importe où sur ta machine et passe `--fixtures-dir /mon/dossier` ou `QA_FIXTURES_DIR=/mon/dossier`.)*

3. **Édite `qa/data/scenarios/real_documents.yaml` pour associer chaque fichier à son résultat attendu :**
   ```yaml
   scenarios:
     - id: real-cni-dupont
       description: "CNI réelle recto/verso : classification + champs identité"
       tags: [baseline, functional, real, v2]
       request:
         api_version: v2
         mode: async
         workflow_id: document-classification-extraction-v2
         file: cni/cni_jean_dupont.pdf
         override:
           extract_content_ocr:
             - param: model
               value: mistral
       expect:
         http_status: 200
         execution_status: SUCCESS
         purged: false
         classification:
           document_type: cni
           confidence: { gte: 0.80 }
         extraction:
           type: cni
           properties:
             nom: { iequals: "DUPONT" }
             prenom: { contains: "Jean" }
             date_naissance: { equals: "1990-05-14" }
         barcodes:
           min_count: 0
   ```

4. **Joue tes scénarios réels sur `sandbox`, puis sur `staging`, et compare :**
   ```bash
   uv run python -m qa run --env sandbox --tags real --strict-fixtures --report qa/reports/sandbox_real.json
   uv run python -m qa run --env staging --tags real --strict-fixtures --report qa/reports/staging_real.json
   uv run python -m qa compare qa/reports/sandbox_real.json qa/reports/staging_real.json --fail-on-functional-diff
   ```

> ℹ️ **Comportement si un fichier local est absent :** par défaut, le scénario passe en `SKIPPED` (pratique si tu veux exécuter uniquement les tests synthétiques en CI). Ajoute `--strict-fixtures` pour transformer un fichier manquant en `FAIL`.

---

## 5. Référence du format YAML & Opérateurs d'assertions

### Structure d'un scénario (`request` + `expect`)

```yaml
scenarios:
  - id: mon-scenario-unique             # Obligatoire, unique dans tous les fichiers
    description: "Description lisible"
    tags: [baseline, functional, v2]    # Utilisés pour filtrer avec --tags / --exclude-tags
    min_profile: baseline               # Optionnel : baseline (défaut) | develop
    request:
      api_version: v2                   # v1 | v2 (défaut: v2)
      mode: async                       # async (execute + polling) | sync (execute-sync) | http
      workflow_id: document-classification-extraction-v2
      file: mon_fichier.pdf             # Chemin relatif à --fixtures-dir OU synthetic:<nom>
      # file_url: "https://..."         # Mutuellement exclusif avec file
      # override: { ... }               # Surcharges v2 par step
      # raw_override: "{malformed"      # Chaîne brute (pour tester les 400/422)
      # metadata: { source: "qa" }      # Métadonnées JSON optionnelles
      # classification_parameters:      # Uniquement v1 (attention aux alias à tirets !)
      #   llm-model: openweight-medium
      # extraction_parameters:          # Uniquement v1 (alias exact : document-type)
      #   document-type: cni
      # auth: default                   # default | none | invalid (pour mode: http ou tests 401)
      # method: GET                     # Uniquement pour mode: http
      # path: /api/v1/health            # Uniquement pour mode: http
      # headers: { ... }                # Headers HTTP additionnels
      # poll_interval_s: 2.0            # Surcharge optionnelle de l'intervalle de polling
      # poll_timeout_s: 180.0           # Surcharge optionnelle du timeout d'attente
    expect:
      http_status: 200                  # int ou liste d'int (ex: [400, 422])
      execution_status: SUCCESS         # SUCCESS | FAILED
      purged: false                     # Vérifié sur profil develop ou si présent dans la réponse
      classification:
        document_type: cni              # Chaîne exacte OU règle { one_of: [cni, passeport] }
        confidence: { gte: 0.75 }
        explanation: { not_empty: true }
      extraction:
        type: cni
        properties:
          nom: { iequals: "DUPONT" }
          date_expiration:
            value: null
            anomaly_code: "invalid_date"
            anomaly_raw_value: "31/04/2026"
      barcodes:
        min_count: 1
        max_count: 3
        contains_type: "2D_DOC"
      headers:
        exists: [x-ratelimit-remaining-minute]
        not_exists: [idempotent-replay]
        equals:
          content-type: "application/json"
      json_path:
        status: { equals: "success" }
        data.$length: { gte: 4 }
```

### Tous les opérateurs d'assertions supportés (`FieldRule`)

Le moteur d'assertions est pensé pour la nature partiellement non-déterministe de l'OCR/LLM :

| Opérateur | Effet | Exemple |
|---|---|---|
| `equals` | Égalité stricte | `{ equals: "1990-05-14" }` |
| `not_equals` | Différence stricte | `{ not_equals: "autre" }` |
| `iequals` | Égalité normalisée (insensible à la casse, aux accents et aux espaces multiples) | `{ iequals: "Élodie DUPONT" }` |
| `contains` | Sous-chaîne présente | `{ contains: "Ministère" }` |
| `regex` | Match expression régulière (`re.search`) | `{ regex: "^[0-9]{14}$" }` |
| `one_of` | Valeur parmi une liste autorisée | `{ one_of: ["cni", "passeport"] }` |
| `gte` / `lte` | Comparaison numérique ($\ge$ / $\le$) | `{ gte: 0.75 }` |
| `between` | Intervalle numérique inclusif `[min, max]` | `{ between: [0.5, 1.0] }` |
| `is_null` | Vérifie `val is None` (`true`) ou `val is not None` (`false`) | `{ is_null: true }` |
| `not_empty` | Chaîne/liste/dict non vide | `{ not_empty: true }` |
| `exists` | Clé présente (`true`) ou absente (`false`) | `{ exists: true }` |
| `value` | Shorthand pour vérifier la valeur exacte d'une propriété (y compris `null`) | `{ value: null, anomaly_code: "invalid_date" }` |
| `anomaly_code` | Vérifie `property.anomaly.code` (anomalies de dates `#170`) | `{ anomaly_code: "invalid_date" }` |
| `anomaly_raw_value` | Vérifie `property.anomaly.raw_value` (`#170`) | `{ anomaly_raw_value: "31/04/2026" }` |
| `$length` (dans `json_path`) | Longueur d'une liste, d'un dict ou d'une chaîne (racine `$length` ou imbriqué `data.$length`) | `$length: { gte: 1 }` |

---

## 6. Guide complet des commandes CLI

### `list` — Lister les scénarios et fixtures découverts

```bash
uv run python -m qa list --env staging
uv run python -m qa list --env staging --tags security
uv run python -m qa list --env staging --fixtures-dir /chemin/docs
```

### `run` — Exécuter les scénarios YAML et protocolaires

```bash
uv run python -m qa run --env <local|sandbox|staging|prod> [options]
```

| Option | Défaut | Description |
|---|---|---|
| `--env` | `local` | Environnement cible (`local`, `sandbox`, `staging` / `develop`, `prod`). |
| `--allow-prod` | `false` | Requis explicitement pour cibler `prod`. |
| `--base-url` | *(env)* | Surcharge ponctuelle de l'URL de l'API (soumise au garde-fou `prod`). |
| `--fixtures-dir` | `qa/data` | Dossier des documents réels et des scénarios locaux (`<dir>/scenarios/`). |
| `--scenarios` | *(auto)* | Fichiers ou dossiers YAML explicites à charger (désactive la découverte par défaut). |
| `--tags` | *(profil)* | Liste de tags séparés par des virgules (ex: `baseline`, `real`, `security,idempotency`, `webhook`, `ratelimit-burst`). |
| `--exclude-tags` | *(opt-in)* | Tags à exclure (ex: `v1`). |
| `--id` | — | Exécute uniquement les scénarios/protocoles listés par ID exact (ex: `smoke-health,proto-idempotency`). |
| `--strict-fixtures` | `false` | Échoue (`FAIL`) au lieu de `SKIPPED` si un fichier local référencé est introuvable. |
| `-c, --concurrency` | `4` | Nombre max de scénarios déclaratifs exécutés en parallèle. |
| `--report` | — | Chemin d'export du rapport JSON (ex: `qa/reports/staging.json`). |
| `-v, --verbose` | `false` | Affiche le détail de toutes les assertions (y compris celles réussies). |

**Tags exécutés par défaut selon le profil :**
- Sur `sandbox` (`baseline`) : `baseline, smoke, functional, negative`
- Sur `staging` / `local` (`develop`) : `baseline, smoke, functional, negative, security, idempotency, resilience, develop`
- Toujours **exclus par défaut** (opt-in explicite via `--tags`) : `ratelimit-burst`, `webhook`, `load`.

### `compare` — Comparer deux campagnes (`sandbox` vs `staging`)

```bash
uv run python -m qa compare qa/reports/sandbox.json qa/reports/staging.json [options]
```

| Option | Description |
|---|---|
| `--report <path>` | Exporte le diff en JSON (`ComparisonReport`). |
| `--fail-on-functional-diff` | Retourne aussi le code `1` si les sorties métier diffèrent (classification, propriétés extraites ou nombre de codes-barres) même si les deux runs sont `PASS`. |

Verdicts affichés par scénario :
- `REGRESSION` (`PASS` $\rightarrow$ `FAIL`/`ERROR`) $\Rightarrow$ **exit code 1**
- `IMPROVEMENT` (`FAIL` $\rightarrow$ `PASS`)
- `UNCHANGED` (+ indication d'éventuels diffs fonctionnels et delta de latence en ms / %)
- `ADDED` / `REMOVED` (scénarios présents uniquement dans un des deux rapports)

### `gen-fixtures` — Matérialiser les fixtures synthétiques sur disque

```bash
uv run python -m qa gen-fixtures --output-dir qa/data/synthetic
```

Génère les 7 fichiers synthétiques dans le dossier choisi (utile pour les inspecter, les tester manuellement dans Bruno/Swagger, ou les passer à d'autres scripts).

### `load` — Montée en charge & Chaos

Voir la [Section 8](#8-montée-en-charge--simulation-de-pannes-chaos) ci-dessous.

---

## 7. Catalogue des tests intégrés & Fixtures synthétiques

### 7.1 Scénarios déclaratifs YAML (`qa/scenarios/`)

| Fichier | Scénarios inclus | Profil / Tags |
|---|---|---|
| [`00_smoke.yaml`](./scenarios/00_smoke.yaml) | `smoke-health`, `smoke-workflows-v2`, `smoke-extraction-schemas` | `baseline`, `smoke`, `synthetic` |
| [`01_synthetic_v2.yaml`](./scenarios/01_synthetic_v2.yaml) | `func-v2-barcode-async`, `func-v2-sync-png`, `func-v2-cni-classification-extraction`, `func-v2-extraction-forced-cni`, `func-v2-classification-only`, `func-v2-invalid-date-anomaly` (`#170`) | `baseline`/`develop`, `functional`, `v2` |
| [`02_synthetic_v1.yaml`](./scenarios/02_synthetic_v1.yaml) | `func-v1-2ddoc-compat`, `func-v1-extraction-mistral-compat` | `baseline`, `functional`, `v1` |
| [`03_negative_and_security.yaml`](./scenarios/03_negative_and_security.yaml) | `neg-auth-missing`, `neg-auth-invalid`, `neg-mutually-exclusive-file-and-url`, `neg-invalid-override-unknown-param`, `neg-malformed-override-json`, `neg-invalid-mime-fake-pdf`, `sec-ssrf-http-cleartext`, `sec-ssrf-loopback-ip`, `sec-ssrf-loopback-localhost`, `sec-ssrf-cloud-metadata`, `sec-ssrf-rfc1918` | `baseline`/`develop`, `negative`, `security`, `ssrf` |

### 7.2 Scénarios protocolaires multi-étapes (`qa/runners/protocol_runner.py`)

| ID | Exécuté par défaut (`staging`) | Issue(s) | Ce qui est vérifié |
|---|---|---|---|
| `proto-auth-binding` | ✅ Oui | `#159`, `#250` | Rejet `401` sur clé absente, aléatoire, mauvais `<env>`, préfixe muté et préfixe `v2`, **sans décrémenter** le quota `X-RateLimit-Remaining-Minute` de la clé valide. |
| `proto-idempotency` | ✅ Oui | `#148` | Premier envoi (`200`), replay identique (`200` + `Idempotent-Replay: true` + même `execution_id`), conflit payload (`409 idempotency_conflict`), clé invalide accents/>256 (`400 invalid_idempotency_key`), 2 appels concurrents (1 seul `execution_id`). |
| `proto-ratelimit-headers` | ✅ Oui | `#250` | Présence et décrémentation cohérente des headers `X-RateLimit-Remaining-Minute`, `Daily`, `Reset-Minute`, `Reset-Daily`. |
| `proto-oom-decompression-bomb` | ✅ Oui | `#258` | Envoie un PNG valide de 110 MP (< 50 Ko sur disque), vérifie gestion sûre (`400`/`413`/`422`, `FAILED` ou `SUCCESS` avec `barcodes: []`), puis envoie aussitôt un PDF nominal et vérifie `SUCCESS` (worker vivant sans OOM kill). |
| `proto-ratelimit-burst` | ⚙️ Opt-in (`--tags ratelimit-burst`) | `#250` | Rafale jusqu'à `429 Too Many Requests`, vérifie `Retry-After` et que le quota journalier n'est pas consommé sur les appels `429`. *(Attention : consomme le quota minute de la clé de test !)* |
| `proto-webhook-isolation` | ⚙️ Opt-in (`--tags webhook`) | `#160` | Nécessite `QA_<ENV>_ADMIN_API_KEY` (+ au moins 2 organisations). Crée un webhook temporaire sur org A, vérifie que `DELETE` via org B renvoie `404`, puis nettoie systématiquement en `finally`. |

### 7.3 Fixtures synthétiques pure-Python (`synthetic:*`)

| Identifiant | Format | Usage |
|---|---|---|
| `synthetic:minimal_pdf` | PDF 1 page | Smoke, v1/v2 rapide, charge |
| `synthetic:cni_pdf` | PDF 1 page | Texte structuré type CNI (`DUPONT`, `JEAN`, `14/05/1990`) |
| `synthetic:invalid_date_pdf` | PDF 1 page | Contient une date impossible `31/04/2026` (`#170`) |
| `synthetic:multipage_pdf` | PDF 3 pages | Charge / OCR multi-pages sans PII |
| `synthetic:valid_png` | PNG RGB | Ingestion image & `execute-sync` |
| `synthetic:fake_pdf_shell` | Script shell `.pdf` | Test rejet MIME spoofing (`#152`) |
| `synthetic:decompression_bomb_png` | PNG 110 MP (< 50 Ko) | Test garde OOM `MAX_BARCODE_IMAGE_PIXELS` (`#258`) |

---

## 8. Montée en charge & Simulation de pannes (Chaos)

La commande `qa load` soumet `N` workflows avec une concurrence `C`, gère proprement le backpressure (`503` / `429` avec `Retry-After`, `502`/`504` avec `Idempotency-Key`, `409 idempotency_in_progress`), peut injecter une panne en plein tir, puis vérifie tous les invariants de fiabilité :

- **`dropped_count == 0`** : 100 % des `execution_id` acceptés atteignent un état terminal (aucune exécution bloquée en `STARTED` / `TIMEOUT`).
- **`zero_idempotency_duplicates == True`** : sonde active de rejeu idempotent + unicité stricte des `execution_id`.
- **`http_500_count == 0`** : aucune erreur 500 (ni à la soumission, ni au polling).
- **`failed_count == 0`** : toutes les exécutions terminent en `SUCCESS`.
- **Statistiques de latence** : `min`, `avg`, `p50`, `p95`, `p99`, `max` (soumission, traitement serveur, bout-en-bout).

### Options de `qa load`

| Option | Défaut | Description |
|---|---|---|
| `-n, --requests` | `10` | Nombre total d'exécutions à soumettre. |
| `-c, --concurrency` | `5` | Concurrence maximale en soumission. |
| `--workflow-id` | `document-classification-extraction-v2` | Workflow v2 ciblé (astuce : `document-barcode-extraction-v2` ne consomme aucun crédit LLM). |
| `--files` | `synthetic:minimal_pdf` | Liste de fichiers réels ou `synthetic:*` séparés par virgules (rotation round-robin). |
| `--override` | — | JSON d'override v2 optionnel (ex: `'{"extract_content_ocr":[{"param":"model","value":"tesseract"}]}'`). |
| `--use-idempotency-key` / `--no-use-idempotency-key` | `true` | Attache une `Idempotency-Key` unique par requête logique et la réutilise sur retry. |
| `--poll-interval` | `2.0` | Intervalle de polling en secondes. |
| `--max-timeout` | `180` (`360` avec chaos) | Timeout max de polling par exécution en secondes. |
| `--chaos-mode` | `none` | Mode de panne : `none`, `manual`, `docker`, `scalingo`. |
| `--chaos-after` | `3` | Nombre d'exécutions acceptées avant de déclencher la panne. |
| `--chaos-target` | — | Nom du conteneur Docker ou type Scalingo (ex: `worker`, `web`). |
| `--scalingo-app` | — | Nom de l'application Scalingo (pour `--chaos-mode scalingo`). |
| `--chaos-downtime-s` | `5.0` | Durée de coupure Docker (`stop` $\rightarrow$ sleep $\rightarrow$ `start`). |
| `--report` | — | Export JSON (`LoadReport`). |

### Recettes prêtes à copier-coller

#### A. Montée en charge pure sur `staging` (avec tes vrais PDF lourds / 2D-Doc)
```bash
uv run python -m qa load \
  --env staging \
  -n 30 -c 10 \
  --workflow-id document-barcode-extraction-v2 \
  --files qa/data/cni/cni_sample.pdf,synthetic:multipage_pdf \
  --report qa/reports/staging_load.json
```

#### B. Simulation de crash Scalingo en mode interactif (`--chaos-mode manual`) — Recommandé
1. Lance la commande :
   ```bash
   uv run python -m qa load \
     --env staging \
     -n 15 -c 5 \
     --workflow-id document-classification-extraction-v2 \
     --files synthetic:cni_pdf \
     --chaos-mode manual \
     --chaos-after 5 \
     --max-timeout 420 \
     --report qa/reports/staging_chaos_manual.json
   ```
2. Après 5 exécutions acceptées, le script se met en pause et t'invite à intervenir.
3. Redémarre ou tue ton container worker/API sur le dashboard Scalingo (ou `scalingo --app <app> restart worker`).
4. Appuie sur **Entrée** : le script termine les soumissions, attend la reprise PEL (`XAUTOCLAIM`, `reclaim_idle_ms = 300s` d'où `--max-timeout 420`) et valide **0 drop**.

#### C. Redémarrage Scalingo automatisé (`--chaos-mode scalingo`)
```bash
uv run python -m qa load \
  --env staging \
  -n 15 -c 5 \
  --chaos-mode scalingo \
  --scalingo-app document-ia-worker-staging \
  --chaos-target worker \
  --chaos-after 4 \
  --max-timeout 420
```

#### D. Test local de coupure Redis (`--chaos-mode docker`, vérifie `#149`)
Coupe `document-ia-redis` pendant 5 s, vérifie automatiquement pendant la panne que `GET /api/v1/executions/{id}` reste lisible (`200`) alors que `POST /execute` échoue en fail-closed (`503`), relance Redis et confirme **0 drop** :
```bash
uv run python -m qa load \
  --env local \
  -n 10 -c 4 \
  --chaos-mode docker \
  --chaos-target document-ia-redis \
  --chaos-downtime-s 5 \
  --chaos-after 3
```

---

## 9. Codes de sortie, Sécurité PII & Dépannage

### Codes de sortie CLI
- **`0`** : Tous les scénarios / invariants de charge ont réussi (ou aucune régression dans `compare`).
- **`1`** : Au moins un scénario en échec (`FAIL`/`ERROR`), un drop/doublon/500 en test de charge, ou une régression détectée par `compare`.
- **`2`** : Erreur de configuration (`.env.qa`, YAML invalide, garde-fou `prod`, argument CLI invalide, erreur Docker/Scalingo).

### Hygiène sécurité & PII
- **Ne jamais commiter** `.env.qa`, `qa/data/*` (hors `.gitkeep`/`README.md`) ni `qa/reports/*` (les rapports issus de vrais documents contiennent les champs extraits). Tous ces chemins sont déjà dans `.gitignore`.
- Les clés API sont encapsulées dans `SecretStr`, jamais affichées ni écrites dans les rapports JSON ; les headers sensibles dans les assertions sont masqués en `***REDACTED***`.

### Dépannage rapide

| Symptôme | Cause probable & Solution |
|---|---|
| `ERROR: API key missing for environment 'staging'` | Renseigne `QA_STAGING_API_KEY` dans `.env.qa` ou exporte la variable dans ton shell. |
| Scénario marqué `SKIPPED (Fixture not found)` | Le fichier local n'existe pas dans `qa/data/` (ou `--fixtures-dir`). Ajoute le fichier ou utilise `--strict-fixtures` pour forcer un échec. |
| `smoke-health` échoue en `local` | Vérifie que `docker compose up -d` + `document-ia-api` (port 8000) + `document-ia-worker` tournent localement. |
| `TIMEOUT` en mode chaos Scalingo | Le worker reclaim la PEL après `reclaim_idle_ms` (300 s par défaut) : passe `--max-timeout 420` (ou `480`). |
| `ProdGuardError` | Protection volontaire : `--allow-prod` requis pour `run` sur `prod`, et `load`/`chaos` toujours interdits sur `prod`. |

### Lancer les tests unitaires de l'outil QA lui-même (hors-ligne)

```bash
uv run pytest qa/tests -v
uv run ruff check qa
uv run ruff format --check qa
uv run pyright qa
```
