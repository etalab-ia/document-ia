"""Unit tests for OpenAI replay payload saving in LLMExtractDocumentStep (Issue #158)."""

import json
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import BaseModel

from document_ia_infra.data.document.schema.document_extraction import (
    DocumentExtraction,
)
from document_ia_schemas import SupportedDocumentType
from document_ia_worker.workflow.step.llm_extract_document.llm_extract_document import (
    LLMExtractDocumentStep,
)
from document_ia_worker.workflow.step.llm_extract_document.settings import (
    LLMExtractDocumentSettings,
)


class _DummyExtractedModel(BaseModel):
    value: str = "ok"


@pytest.mark.asyncio
async def test_save_openai_replay_payload_permissions_and_default_disabled(
    tmp_path: Path,
):
    """Replay payload saving must be disabled by default and enforce 0o700/0o600 when enabled."""
    default_settings = LLMExtractDocumentSettings()
    assert default_settings.OPENAI_REPLAY_PAYLOAD_SAVE_ENABLED is False
    assert default_settings.OPENAI_REPLAY_RETENTION_HOURS == 24.0

    execution_id = str(uuid4())
    replay_dir = tmp_path / "openai-replay"

    step = LLMExtractDocumentStep.__new__(LLMExtractDocumentStep)
    step.execution_id = execution_id
    step.model = "gpt-4o"
    step.temperature = 0.0
    step.document_type_override = SupportedDocumentType.CNI
    step.extraction_parameters = None
    step.llm_classification_result = None
    step.ocr_result = SimpleNamespace(pages=[SimpleNamespace(text="page text")])

    mock_prompt_service = MagicMock()
    mock_prompt_service.get_extraction_prompt.return_value = (
        "sys prompt",
        _DummyExtractedModel,
    )
    step.prompt_service = mock_prompt_service

    mock_openai = MagicMock()
    mock_openai.get_extraction_response = AsyncMock(
        return_value=(
            DocumentExtraction[BaseModel](
                type=SupportedDocumentType.CNI,
                properties=_DummyExtractedModel(),
            ),
            10,
            5,
        )
    )
    step.openai_manager = mock_openai

    # 1. Disabled by default: _execute_internal must not write any replay file
    with patch(
        "document_ia_worker.workflow.step.llm_extract_document.llm_extract_document.settings",
        LLMExtractDocumentSettings(
            OPENAI_REPLAY_PAYLOAD_SAVE_ENABLED=False,
            OPENAI_REPLAY_PAYLOAD_SAVE_PATH=str(replay_dir),
        ),
    ):
        await step._execute_internal()
    assert not replay_dir.exists()

    # 2. Enabled: _save_openai_replay_payload writes confined 0o700 dir + 0o600 JSON file
    with patch(
        "document_ia_worker.workflow.step.llm_extract_document.llm_extract_document.settings",
        LLMExtractDocumentSettings(
            OPENAI_REPLAY_PAYLOAD_SAVE_ENABLED=True,
            OPENAI_REPLAY_PAYLOAD_SAVE_PATH=str(replay_dir),
        ),
    ):
        step._save_openai_replay_payload(
            system_prompt="sys prompt",
            user_prompt="page text\n\n",
            model="gpt-4o",
            document_type=SupportedDocumentType.CNI,
        )

    assert replay_dir.exists()
    assert stat.S_IMODE(replay_dir.stat().st_mode) == 0o700

    payload_file = replay_dir / f"{execution_id}.json"
    assert payload_file.exists()
    assert stat.S_IMODE(payload_file.stat().st_mode) == 0o600

    saved = json.loads(payload_file.read_text(encoding="utf-8"))
    assert saved["execution_id"] == execution_id
    assert saved["model"] == "gpt-4o"
    assert saved["system_prompt"] == "sys prompt"
    assert saved["user_prompt"] == "page text\n\n"


def test_save_openai_replay_payload_supports_relative_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """[QUAL-02] Relative OPENAI_REPLAY_PAYLOAD_SAVE_PATH must work safely and still reject symlinks."""
    from document_ia_infra.exception.security_exception import PathTraversalException

    monkeypatch.chdir(tmp_path)

    execution_id = str(uuid4())
    step = LLMExtractDocumentStep.__new__(LLMExtractDocumentStep)
    step.execution_id = execution_id

    with patch(
        "document_ia_worker.workflow.step.llm_extract_document.llm_extract_document.settings",
        LLMExtractDocumentSettings(
            OPENAI_REPLAY_PAYLOAD_SAVE_ENABLED=True,
            OPENAI_REPLAY_PAYLOAD_SAVE_PATH="replays",
        ),
    ):
        step._save_openai_replay_payload(
            system_prompt="sys prompt",
            user_prompt="user prompt",
            model="gpt-4o",
            document_type=SupportedDocumentType.CNI,
        )

    replay_dir = tmp_path / "replays"
    assert replay_dir.exists()
    assert stat.S_IMODE(replay_dir.stat().st_mode) == 0o700

    payload_file = replay_dir / f"{execution_id}.json"
    assert payload_file.exists()
    assert stat.S_IMODE(payload_file.stat().st_mode) == 0o600
    saved = json.loads(payload_file.read_text(encoding="utf-8"))
    assert saved["execution_id"] == execution_id

    # Relative symlink must still be rejected
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    sym_dir = tmp_path / "sym_replays"
    sym_dir.symlink_to(outside_dir, target_is_directory=True)

    with patch(
        "document_ia_worker.workflow.step.llm_extract_document.llm_extract_document.settings",
        LLMExtractDocumentSettings(
            OPENAI_REPLAY_PAYLOAD_SAVE_ENABLED=True,
            OPENAI_REPLAY_PAYLOAD_SAVE_PATH="sym_replays",
        ),
    ):
        with pytest.raises(PathTraversalException):
            step._save_openai_replay_payload(
                system_prompt="sys prompt",
                user_prompt="user prompt",
                model="gpt-4o",
                document_type=SupportedDocumentType.CNI,
            )

    assert not (outside_dir / f"{execution_id}.json").exists()
