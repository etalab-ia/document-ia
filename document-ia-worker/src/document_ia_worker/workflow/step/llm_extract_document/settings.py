from pydantic import Field
from pydantic_settings import BaseSettings


class LLMExtractDocumentSettings(BaseSettings):
    OPENAI_REPLAY_PAYLOAD_SAVE_ENABLED: bool = False
    OPENAI_REPLAY_PAYLOAD_SAVE_PATH: str = "/tmp/document-ia-openai-replay"
    OPENAI_REPLAY_RETENTION_HOURS: float = Field(default=24.0, ge=0.0)


settings = LLMExtractDocumentSettings()
