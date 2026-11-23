# Security Architecture: 2D-Doc Cryptographic Verification & `VerificationStatus` Exposure

**Initiative**: #151 - Security Hardening
**Upstream Dependency**: `betagouv/2ddoc-parser` **v1.0.7** ([PR #13](https://github.com/betagouv/2ddoc-parser/pull/13), resolving `etalab-ia/document-ia#161` / T1)
**Scope**: Security integration milestone preparatory to `#162` (T2) — exposes 2D-Doc signature/PKI verification status across `document-ia-infra`, `document-ia-worker`, `document-ia-api` (v1/v2 + OpenAPI/ReDoc), and webhooks. Functional OCR vs. 2D-Doc field reconciliation is intentionally out of scope of this milestone.

---

## 1. Executive Summary

Prior to integrating `2ddoc-parser v1.0.7`, `Ants2DDoc` exposed only a coarse boolean `is_valid: bool`, making it impossible for downstream consumers to distinguish between a cryptographically invalid signature, a missing signature, an unknown or revoked certificate, an expired Trust Status List (TSL), or an unavailable resolver. Furthermore, the worker did not pre-verify the 2D-Doc offline PKI trust store at startup.

This integration enforces:
1. **Single Source of Truth for `VerificationStatus`**: `Ants2DDoc.verification_status` directly reuses `fr_2ddoc_parser.model.models.VerificationStatus` in `document-ia-infra` (zero enum duplication). FastAPI / OpenAPI / ReDoc automatically documents all 10 enum values.
2. **Strict Non-Exposure of Internal Diagnostics (OWASP A05 / A09)**: Only the closed `VerificationStatus` enum is persisted and returned. Internal diagnostic fields from `VerificationResult` (notably `detail`, which may include raw exception strings or local filesystem paths) are never exposed in persisted events, API responses, or webhooks.
3. **Fail-Closed Worker Mapping & `is_valid` Invariant**: `BaseExtractBarcode2DDoc._resolve_verification_status()` guarantees that `is_valid` can only be `True` when `result.is_valid is True` **and** `verification_status == VerificationStatus.VERIFIED`. Missing or unrecognized verification statuses fail closed to `(VerificationStatus.RESOLVER_UNAVAILABLE, False)`.
4. **Offline Boot Pre-Warming**: `_prewarm_models_and_trust_store()` in `document-ia-worker/src/document_ia_worker/main.py` loads and verifies the bundled offline ANTS PKI trust store at worker startup (~95 ms, zero network I/O) and fails fast if the store is corrupted or unreadable.
5. **Backward Compatibility**: `verification_status` defaults to `None` so historical `WorkflowExecutionCompletedEvent` records stored in PostgreSQL decode without error across REST v1/v2 endpoints and webhooks.

---

## 2. End-to-End Verification & Exposure Flow

```mermaid
sequenceDiagram
    autonumber
    participant Boot as Worker Boot (main.py)
    participant Step as BaseExtractBarcode2DDoc
    participant Lib as fr_2ddoc_parser v1.0.7 (Offline PKI)
    participant PG as PostgreSQL Event Store
    participant API as ExecutionService (API v1/v2 & Webhooks)

    Boot->>Lib: _prewarm_models_and_trust_store() -> get_default_key_resolver()
    Note over Boot,Lib: Verifies bundled XML-DSig TSL, 230 leaf certs & 7 CRLs (0 network calls, fail-fast)

    Step->>Lib: decode_2d_doc(barcode.text)
    Lib-->>Step: Decoded2DDoc(is_valid, verification=VerificationResult)
    Step->>Step: _resolve_verification_status(result) [Fail-Closed + Invariant]
    Step->>PG: Persist Ants2DDoc(is_valid, verification_status, ...)
    API->>PG: Fetch latest EventDTO
    API->>API: decode_event_dto() -> ExecutionSuccessModel
    API-->>API: Serialize JSON (REST v1/v2 + Webhook POST)
```

---

## 3. Contract & `VerificationStatus` Semantics

`Ants2DDoc` ([`document-ia-infra/src/document_ia_infra/data/event/schema/barcode.py`](../document-ia-infra/src/document_ia_infra/data/event/schema/barcode.py)) exposes:

| Field | Type | Default | Privacy (`x-mask`) | Notes |
| :--- | :--- | :--- | :--- | :--- |
| `is_valid` | `bool` | *(required)* | No | `True` **if and only if** `verification_status == "verified"` for newly processed 2D-Docs. |
| `verification_status` | `VerificationStatus \| None` | `None` | No | `None` only for historical persisted events predating this release. |
| `raw_data` | `Any \| None` | `None` | **Yes** (`x-mask: True`) | Raw decoded fields. |
| `typed_data` | `list[GenericProperty]` | `[]` | **Yes** (`x-mask: True`) | Typed decoded fields. |

### `VerificationStatus` Enum Values (`fr_2ddoc_parser.model.models.VerificationStatus`)

| Value | `is_valid` | Meaning |
| :--- | :---: | :--- |
| `verified` | `true` | Cryptographic signature verified against a valid, non-revoked leaf certificate chaining up to a fresh ANTS TSL. |
| `signature_missing` | `false` | 2D-Doc payload does not contain a signature block. |
| `signature_invalid` | `false` | Cryptographic signature verification failed against the resolved certificate(s). |
| `unknown_ca` | `false` | Issuing CA identifier (`ca_id`, e.g., excluded test CA `FR00`) is not present in the trust store. |
| `unknown_cert` | `false` | Signing certificate identifier (`cert_id`) is not found under the declared CA. |
| `untrusted_certificate` | `false` | Certificate failed X.509 chain, constraints, key usage, or trust-anchor validation. |
| `cert_revoked` | `false` | Signing certificate is listed as revoked in an applicable signed CRL. |
| `cert_not_valid_at_signing` | `false` | Signing certificate was not valid at the document reference date (`signature_date` or `issue_date`). |
| `trust_list_expired` | `false` | Bundled or overridden TSL `NextUpdate` has elapsed under strict freshness policy (fail-closed). |
| `resolver_unavailable` | `false` | Trust store unavailable, required CRL missing/expired/unapplicable, or worker fail-closed fallback triggered. |

> [!WARNING]
> **Security Interpretation**:
> - The absence of a 2D-Doc barcode on a document does **not** prove fraud.
> - A `verified` signature proves the cryptographic integrity and ANTS PKI provenance of the **barcode payload only** — it does **not** prove the authenticity of the surrounding image, the consistency with OCR-extracted text, or the identity of the document holder.

---

## 4. Security Controls & Threat Matrix

| Threat / Failure Mode | OWASP | Control Implemented |
| :--- | :--- | :--- |
| **Internal Error / Path Disclosure** | A05 / A09 | `VerificationResult.detail` and internal PKI metadata (`ca_id`, `cert_id`, `tsl_*`) are excluded from `Ants2DDoc`. Worker logs emit only non-PII status/mismatch warnings. |
| **Status / Boolean Desynchronization** | A04 / A08 | `_resolve_verification_status()` enforces `is_valid = bool(result.is_valid and status == VerificationStatus.VERIFIED)`. Any `is_valid=True` paired with a non-`verified` status is forced to `False` with a `WARNING` log. |
| **Upstream Library Drift / Missing Verification Object** | A04 | If `result.verification is None` or `result.verification.status` is not a valid `VerificationStatus`, the worker fails closed to `(VerificationStatus.RESOLVER_UNAVAILABLE, False)` rather than crashing or marking the barcode valid. |
| **Runtime Network Dependency / SSRF** | A10 | `2ddoc-parser v1.0.7` performs 100% offline PKI validation against the bundled snapshot (verified in offline `--network none` worker smoke tests). |
| **Corrupted Trust Store at Runtime** | A08 | `_prewarm_models_and_trust_store()` invokes `get_default_key_resolver()` at worker startup and aborts boot on load/integrity errors. |
| **Historical Event Deserialization Regression** | A04 | `Ants2DDoc.verification_status` defaults to `None`, preserving compatibility with historical PostgreSQL events, REST API v1/v2, and webhooks. |

---

## 5. Operations, Trust Store Lifecycle & Known Limitations

### Bundled Trust Store (`2ddoc-parser v1.0.7`)
- **TSL Sequence Number**: `23`
- **TSL `NextUpdate`**: `2027-05-11T14:00:00Z`
- **Boot Load Time**: ~95 ms (0 network calls)
- **Boot Freshness Logging**: `_prewarm_models_and_trust_store()` logs `tsl_sequence_number`, `tsl_next_update`, and `tsl_freshness` at `INFO` level, and emits a `WARNING` if `tsl_freshness != TrustListFreshness.FRESH` (without crashing the worker, since runtime verification enforces fail-closed `trust_list_expired`).

### Operational Override (`FR_2DDOC_TRUST_STORE_DIR`)
To refresh the ANTS trust store before `2027-05-11` without rebuilding container images, operators can mount an updated offline trust store directory (generated via `python -m fr_2ddoc_parser.crypto.sync_trust_store`) and set:

```bash
FR_2DDOC_TRUST_STORE_DIR=/path/to/verified-trust-store
```

### Known Snapshot Limitation (`FR01`)
In the `2ddoc-parser v1.0.7` bundled snapshot, the CRL for legacy authority `FR01` expired in 2022. By design (fail-closed revocation checking), 2D-Docs signed by `FR01` return `is_valid: false` and `verification_status: "resolver_unavailable"`. All other issuing authorities remain unaffected.

---

## 6. Example API Response (`Ants2DDoc`)

```json
{
  "type": "2D_DOC",
  "page_number": 1,
  "position": {
    "top_left": [100, 100],
    "top_right": [250, 100],
    "bottom_right": [250, 250],
    "bottom_left": [100, 250]
  },
  "is_valid": true,
  "verification_status": "verified",
  "raw_data": {"01": "EXAMPLE"},
  "typed_data": [
    {"name": "first_name", "value": "Alice", "type": "str"}
  ],
  "issue_date": "2026-01-01",
  "ants_type": "01",
  "doc_type": "01"
}
```
