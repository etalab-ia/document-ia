import logging
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from document_ia_infra.s3.s3_manager import S3Manager


@pytest.fixture
def mock_s3_manager():
    with patch("document_ia_infra.s3.s3_manager.boto3.client") as mock_boto:
        mock_client = MagicMock()
        mock_boto.return_value = mock_client
        manager = S3Manager()
        yield manager, mock_client


def test_delete_file_success(mock_s3_manager, caplog):
    manager, mock_client = mock_s3_manager
    s3_key = "sensitive/customer_123/identity_document.pdf"

    with caplog.at_level(logging.INFO):
        result = manager.delete_file(s3_key)

    assert result is True
    mock_client.delete_object.assert_called_once_with(
        Bucket=manager.bucket_name, Key=s3_key
    )
    # Verification: raw s3_key must NOT appear in log messages (security requirement)
    assert s3_key not in caplog.text
    assert "File deleted successfully from S3" in caplog.text


def test_delete_file_client_error(mock_s3_manager, caplog):
    manager, mock_client = mock_s3_manager
    s3_key = "sensitive/customer_123/identity_document.pdf"

    mock_client.delete_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
        "DeleteObject",
    )

    with caplog.at_level(logging.INFO):
        result = manager.delete_file(s3_key)

    assert result is False
    assert s3_key not in caplog.text
    assert "Failed to delete file from S3" in caplog.text


def test_delete_file_unexpected_exception(mock_s3_manager, caplog):
    manager, mock_client = mock_s3_manager
    s3_key = "sensitive/customer_123/identity_document.pdf"

    mock_client.delete_object.side_effect = RuntimeError("Network timeout")

    with caplog.at_level(logging.INFO):
        result = manager.delete_file(s3_key)

    assert result is False
    assert s3_key not in caplog.text
    assert "Unexpected error during file deletion from S3" in caplog.text


@pytest.mark.parametrize("error_code", ["NoSuchKey", "404"])
def test_delete_file_404_or_no_such_key_returns_true(
    mock_s3_manager, caplog, error_code: str
):
    manager, mock_client = mock_s3_manager
    s3_key = "sensitive/customer_123/identity_document.pdf"

    mock_client.delete_object.side_effect = ClientError(
        {"Error": {"Code": error_code, "Message": "Not Found"}},
        "DeleteObject",
    )

    with caplog.at_level(logging.INFO):
        result = manager.delete_file(s3_key)

    assert result is True
    assert s3_key not in caplog.text
    assert "S3 file already absent (404/NoSuchKey)" in caplog.text
    assert not any(rec.levelno >= logging.ERROR for rec in caplog.records)


@pytest.mark.parametrize("error_code", ["404", "NoSuchKey"])
def test_download_file_404_raises_file_not_found_without_s3_key(
    mock_s3_manager, caplog, tmp_path, error_code: str
):
    manager, mock_client = mock_s3_manager
    s3_key = "sensitive/customer_123/identity_document.pdf"
    output_path = str(tmp_path / "downloaded.pdf")

    mock_client.download_file.side_effect = ClientError(
        {"Error": {"Code": error_code, "Message": f"Not Found: {s3_key}"}},
        "GetObject",
    )

    with caplog.at_level(logging.INFO):
        with pytest.raises(FileNotFoundError) as exc_info:
            manager.download_file(s3_key, output_path)

    assert s3_key not in str(exc_info.value)
    assert s3_key not in caplog.text


def test_download_file_unexpected_error_redacts_s3_key(
    mock_s3_manager, caplog, tmp_path
):
    manager, mock_client = mock_s3_manager
    s3_key = "sensitive/customer_123/identity_document.pdf"
    output_path = str(tmp_path / "downloaded.pdf")

    mock_client.download_file.side_effect = RuntimeError(
        f"Failed to download {s3_key} to {output_path}"
    )

    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError) as exc_info:
            manager.download_file(s3_key, output_path)

    assert s3_key not in str(exc_info.value)
    assert output_path not in str(exc_info.value)
    assert "RuntimeError" in str(exc_info.value)
    assert s3_key not in caplog.text
