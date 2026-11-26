"""Multi-environment configuration loader with strict secret handling and prod guard."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from qa.models import EnvProfile

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
DEFAULT_ENVIRONMENTS_FILE = PACKAGE_DIR / "environments.yaml"
DEFAULT_ENV_FILE = REPO_ROOT / ".env.qa"
DEFAULT_FIXTURES_DIR = PACKAGE_DIR / "data"

SENSITIVE_HEADER_NAMES = frozenset(
    {
        "x-api-key",
        "authorization",
        "cookie",
        "set-cookie",
        "proxy-authorization",
    }
)


class ConfigError(RuntimeError):
    """Raised when QA configuration is invalid or incomplete."""


class ProdGuardError(ConfigError):
    """Raised when targeting prod without explicit opt-in or disallowed mode."""


class EnvironmentDefinition(BaseModel):
    """Static environment specification loaded from environments.yaml."""

    model_config = ConfigDict(extra="forbid")

    base_url: str
    profile: EnvProfile = "baseline"
    api_key_env: str = "staging"
    aliases: list[str] = Field(default_factory=list)
    allow_load: bool = True
    allow_chaos: bool = True
    requires_explicit_allow: bool = False
    default_timeout_s: float = Field(default=60.0, gt=0)
    default_poll_interval_s: float = Field(default=2.0, gt=0)
    default_poll_timeout_s: float = Field(default=180.0, gt=0)


class EnvironmentsCatalog(BaseModel):
    """Root structure of qa/environments.yaml."""

    model_config = ConfigDict(extra="forbid")

    environments: dict[str, EnvironmentDefinition]


class ResolvedEnvConfig(BaseModel):
    """Runtime environment configuration with masked secret storage."""

    model_config = ConfigDict(extra="forbid")

    name: str
    base_url: str
    profile: EnvProfile
    api_key_env: str
    allow_load: bool
    allow_chaos: bool
    requires_explicit_allow: bool
    default_timeout_s: float
    default_poll_interval_s: float
    default_poll_timeout_s: float
    api_key: SecretStr | None = None
    admin_api_key: SecretStr | None = None
    fixtures_dir: Path = DEFAULT_FIXTURES_DIR
    webhook_url: str | None = None

    def get_api_key(self) -> str | None:
        return self.api_key.get_secret_value() if self.api_key is not None else None

    def get_admin_api_key(self) -> str | None:
        return (
            self.admin_api_key.get_secret_value()
            if self.admin_api_key is not None
            else None
        )

    def masked_summary(self) -> dict[str, str | bool]:
        """Return a logging/display-safe summary with secrets redacted."""
        return {
            "env": self.name,
            "base_url": self.base_url,
            "profile": self.profile,
            "api_key": mask_secret(self.get_api_key()),
            "admin_api_key": mask_secret(self.get_admin_api_key()),
            "fixtures_dir": str(self.fixtures_dir),
            "allow_load": self.allow_load,
            "allow_chaos": self.allow_chaos,
        }


def mask_secret(value: str | None) -> str:
    """Return a non-reversible masked representation of a secret."""
    if not value:
        return "<unset>"
    if len(value) <= 10:
        return "***"
    return f"{value[:4]}***{value[-2:]}"


def sanitize_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Redact sensitive HTTP headers before logging or serializing reports."""
    sanitized: dict[str, str] = {}
    for key, val in headers.items():
        if key.lower() in SENSITIVE_HEADER_NAMES:
            sanitized[key] = "***REDACTED***"
        else:
            sanitized[key] = val
    return sanitized


def parse_dotenv_file(path: Path) -> dict[str, str]:
    """Parse a simple .env file using pure Python stdlib.

    Supports comments (#), blank lines, optional leading `export `, and
    single/double-quoted values. Does not mutate os.environ.
    """
    if not path.is_file():
        return {}

    parsed: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, raw_val = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        val = raw_val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].strip()
        parsed[key] = val
    return parsed


def load_environments_catalog(
    environments_file: Path = DEFAULT_ENVIRONMENTS_FILE,
) -> EnvironmentsCatalog:
    """Load and validate qa/environments.yaml."""
    if not environments_file.is_file():
        raise ConfigError(f"Environments file not found: {environments_file}")
    try:
        raw = yaml.safe_load(environments_file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ConfigError(f"Invalid YAML in environments file: {environments_file}")
        return EnvironmentsCatalog.model_validate(raw)
    except ConfigError:
        raise
    except (yaml.YAMLError, ValueError, OSError) as exc:
        raise ConfigError(
            f"Failed to load environments file '{environments_file}': {exc}"
        ) from exc


def _lookup_env_var(
    canonical_env: str,
    aliases: list[str],
    suffix: str,
    env_map: Mapping[str, str],
) -> str | None:
    """Resolve QA_<ENV>_<SUFFIX> checking canonical env first, then aliases."""
    candidates = [canonical_env, *aliases]
    for candidate in candidates:
        var_name = f"QA_{candidate.upper()}_{suffix}"
        val = env_map.get(var_name)
        if val is not None and val.strip() != "":
            return val.strip()
    return None


def _validate_base_url(base_url: str) -> str:
    """Validate HTTP(S) URL syntax and return its normalized lowercase hostname."""
    parsed = urlparse(base_url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ConfigError(
            f"Invalid base URL '{base_url}': must use http:// or https:// with a valid hostname."
        )
    return parsed.hostname.lower()


def _enforce_env_guards(
    env_label: str,
    env_def: EnvironmentDefinition,
    *,
    allow_prod: bool,
    for_load: bool,
    for_chaos: bool,
) -> None:
    if env_def.requires_explicit_allow and not allow_prod:
        raise ProdGuardError(
            f"Environment '{env_label}' is protected. Pass --allow-prod to target it explicitly."
        )
    if for_load and not env_def.allow_load:
        raise ProdGuardError(
            f"Load testing is forbidden on environment '{env_label}' (allow_load=false)."
        )
    if for_chaos and not env_def.allow_chaos:
        raise ProdGuardError(
            f"Chaos injection is forbidden on environment '{env_label}' (allow_chaos=false)."
        )


def load_env_config(
    env_name: str,
    *,
    allow_prod: bool = False,
    for_load: bool = False,
    for_chaos: bool = False,
    base_url_override: str | None = None,
    fixtures_dir_override: Path | None = None,
    environments_file: Path = DEFAULT_ENVIRONMENTS_FILE,
    dotenv_file: Path = DEFAULT_ENV_FILE,
    environ: Mapping[str, str] | None = None,
) -> ResolvedEnvConfig:
    """Resolve runtime configuration for a target environment with safety guards."""
    catalog = load_environments_catalog(environments_file)

    requested = env_name.strip().lower()
    canonical_name: str | None = None
    env_def: EnvironmentDefinition | None = None

    if requested in catalog.environments:
        canonical_name = requested
        env_def = catalog.environments[requested]
    else:
        for name, definition in catalog.environments.items():
            if requested in [a.lower() for a in definition.aliases]:
                canonical_name = name
                env_def = definition
                break

    if canonical_name is None or env_def is None:
        valid_names = sorted(catalog.environments.keys())
        raise ConfigError(
            f"Unknown environment '{env_name}'. Available environments: {', '.join(valid_names)}"
        )

    _enforce_env_guards(
        canonical_name,
        env_def,
        allow_prod=allow_prod,
        for_load=for_load,
        for_chaos=for_chaos,
    )

    # Merge .env.qa with os.environ (os.environ takes precedence)
    dotenv_values = parse_dotenv_file(dotenv_file)
    base_environ = os.environ if environ is None else environ
    merged_env: dict[str, str] = {**dotenv_values, **dict(base_environ)}

    env_base_url = _lookup_env_var(
        canonical_name, env_def.aliases, "BASE_URL", merged_env
    )
    resolved_base_url = (base_url_override or env_base_url or env_def.base_url).rstrip(
        "/"
    )
    resolved_host = _validate_base_url(resolved_base_url)

    # Prevent bypassing protected environment guards via --base-url or QA_*_BASE_URL
    effective_allow_load = env_def.allow_load
    effective_allow_chaos = env_def.allow_chaos
    effective_requires_allow = env_def.requires_explicit_allow
    for cat_name, cat_def in catalog.environments.items():
        cat_host = (urlparse(cat_def.base_url).hostname or "").lower()
        if cat_host and cat_host == resolved_host:
            _enforce_env_guards(
                f"{cat_name} ({resolved_host})",
                cat_def,
                allow_prod=allow_prod,
                for_load=for_load,
                for_chaos=for_chaos,
            )
            effective_allow_load = effective_allow_load and cat_def.allow_load
            effective_allow_chaos = effective_allow_chaos and cat_def.allow_chaos
            effective_requires_allow = (
                effective_requires_allow or cat_def.requires_explicit_allow
            )

    api_key_raw = _lookup_env_var(
        canonical_name, env_def.aliases, "API_KEY", merged_env
    )
    admin_key_raw = _lookup_env_var(
        canonical_name, env_def.aliases, "ADMIN_API_KEY", merged_env
    )

    fixtures_env = merged_env.get("QA_FIXTURES_DIR")
    if fixtures_dir_override is not None:
        resolved_fixtures_dir = fixtures_dir_override
    elif fixtures_env:
        resolved_fixtures_dir = Path(fixtures_env)
    else:
        resolved_fixtures_dir = DEFAULT_FIXTURES_DIR

    webhook_url = merged_env.get("QA_WEBHOOK_URL")
    if webhook_url is not None and not webhook_url.strip():
        webhook_url = None

    return ResolvedEnvConfig(
        name=canonical_name,
        base_url=resolved_base_url,
        profile=env_def.profile,
        api_key_env=env_def.api_key_env,
        allow_load=effective_allow_load,
        allow_chaos=effective_allow_chaos,
        requires_explicit_allow=effective_requires_allow,
        default_timeout_s=env_def.default_timeout_s,
        default_poll_interval_s=env_def.default_poll_interval_s,
        default_poll_timeout_s=env_def.default_poll_timeout_s,
        api_key=SecretStr(api_key_raw) if api_key_raw else None,
        admin_api_key=SecretStr(admin_key_raw) if admin_key_raw else None,
        fixtures_dir=resolved_fixtures_dir,
        webhook_url=webhook_url,
    )
