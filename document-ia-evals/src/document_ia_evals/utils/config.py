"""Configuration utilities for the application."""

from pathlib import Path
from pydantic import Field
from pydantic_settings import SettingsConfigDict, BaseSettings


def resolve_root_dir(
    current_file: Path | None = None,
    cwd: Path | None = None,
) -> Path:
    """Resolve the document-ia-evals project root directory.

    Works in both local editable installs and non-editable Scalingo/Docker
    deployments where the package is installed inside site-packages.
    """
    base_cwd = (cwd if cwd is not None else Path.cwd()).resolve()
    base_file = (current_file if current_file is not None else Path(__file__)).resolve()

    def _is_evals_root(path: Path) -> bool:
        return (path / "pyproject.toml").is_file() and (
            path / "src" / "document_ia_evals"
        ).is_dir()

    # 1. Check CWD and its parents (Scalingo / Docker / running inside document-ia-evals)
    for candidate in (base_cwd, *base_cwd.parents):
        if _is_evals_root(candidate):
            return candidate

    # 2. Check source file parents (local editable install, even from monorepo root)
    for candidate in base_file.parents:
        if _is_evals_root(candidate):
            return candidate

    # 3. Standard Scalingo / Docker container root fallback
    app_dir = Path("/app")
    if (app_dir / "src" / "document_ia_evals").is_dir() or (
        app_dir / ".streamlit"
    ).is_dir():
        return app_dir

    # 4. Legacy fallback
    return base_file.parents[3]


class Config(BaseSettings):
    """Application configuration using Pydantic BaseSettings."""

    @staticmethod
    def _find_env_file(filename: str = ".env") -> str | None:
        # Start from this file's directory and walk up to filesystem root looking for filename
        current = Path(__file__).resolve().parent
        root = current.anchor
        while True:
            candidate = current / filename
            if candidate.exists():
                return str(candidate)
            if str(current) == root:
                break
            current = current.parent
        return None

    _env_path = _find_env_file()
    # Extra env vars should be ignored so adding unrelated keys in the .env won't raise errors.
    model_config = SettingsConfigDict(
        env_file=_env_path or "", env_prefix="", extra="ignore"
    )

    # Project paths
    ROOT_DIR: Path = Field(default_factory=resolve_root_dir)

    @property
    def SRC_DIR(self) -> Path:
        return self.ROOT_DIR / "src"

    @property
    def DATA_DIR(self) -> Path:
        return self.ROOT_DIR / "data"

    # App settings
    APP_TITLE: str = Field(default="Streamlit App")
    PAGE_ICON: str = Field(default="📊")
    LAYOUT: str = Field(default="wide")

    # Configuration related to the Document IA API
    DOCUMENT_IA_BASE_URL: str = Field(
        default="https://api.staging.document-ia.beta.gouv.fr/"
    )
    DOCUMENT_IA_API_KEY: str | None = Field(default=None)

    # S3 Configuration
    S3_ENDPOINT: str | None = Field(default=None)
    S3_ACCESS_KEY: str | None = Field(default=None)
    S3_SECRET_KEY: str | None = Field(default=None)
    S3_BUCKET_NAME: str | None = Field(default=None)
    S3_REGION: str | None = Field(default=None)

    # Label Studio Configuration
    LABEL_STUDIO_URL: str | None = Field(default=None)
    LABEL_STUDIO_API_KEY: str | None = Field(default=None)

    ALLOW_INSECURE_REQUESTS: bool = Field(default=False)

    # Default Workflow IDs for Ground Truth V2
    DEFAULT_EXTRACTION_WORKFLOW_ID: str = Field(default="document-extraction-v2")
    DEFAULT_CLASSIFICATION_WORKFLOW_ID: str = Field(
        default="document-classification-v2"
    )

    # ProConnect / OpenID Connect Authentication
    AUTH_BYPASS_LOCAL: bool = Field(default=False)
    PROCONNECT_CLIENT_ID: str | None = Field(default=None)
    PROCONNECT_CLIENT_SECRET: str | None = Field(default=None)
    PROCONNECT_METADATA_URL: str = Field(
        default="https://fca.integ01.dev-agentconnect.fr/api/v2/.well-known/openid-configuration"
    )
    PROCONNECT_REDIRECT_URI: str = Field(default="http://localhost:8501/oauth2callback")
    PROCONNECT_COOKIE_SECRET: str | None = Field(default=None)
    PROCONNECT_SCOPES: str = Field(default="openid email profile")
    # Optional override. When unset, federated logout keeps the redirect Streamlit
    # derives from PROCONNECT_REDIRECT_URI, so hosted deployments are not sent to
    # a localhost address that ProConnect has not registered.
    PROCONNECT_POST_LOGOUT_REDIRECT_URI: str | None = Field(default=None)

    def ensure_directories(self):
        """Ensure necessary directories exist."""
        self.DATA_DIR.mkdir(exist_ok=True)


# Create singleton instance
config = Config()
