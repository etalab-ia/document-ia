import logging
from typing import Optional, Dict, Any, cast

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from mypy_boto3_s3.client import S3Client
from mypy_boto3_s3.type_defs import ListBucketsOutputTypeDef, BucketTypeDef

from document_ia_infra.exception.s3_authentification_exception import (
    S3AuthentificationException,
)
from document_ia_infra.s3.s3_metadata_util import sanitize_metadata
from document_ia_infra.s3.s3_settings import s3_settings

logger = logging.getLogger(__name__)


class S3Manager:
    def __init__(self):
        # Disable checksum because outscale do not support them
        config = Config(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )
        self.s3_client: S3Client = cast(
            S3Client,
            boto3.client(  # pyright: ignore [reportUnknownMemberType]
                "s3",
                endpoint_url=s3_settings.S3_ENDPOINT_URL,
                aws_access_key_id=s3_settings.S3_ACCESS_KEY_ID.get_secret_value(),
                aws_secret_access_key=s3_settings.S3_SECRET_ACCESS_KEY.get_secret_value(),
                region_name=s3_settings.S3_REGION_NAME,
                use_ssl=s3_settings.S3_USE_SSL,
                config=config,
            ),
        )
        self.bucket_name = s3_settings.S3_BUCKET_NAME

    # The Exception need to be handled by the caller
    def upload_file(
        self,
        file_key: str,
        file_data: bytes,
        content_type: str,
        metadata: Optional[dict[str, str]] = None,
    ):
        if metadata is None:
            metadata = {}
        return self.s3_client.put_object(
            Bucket=self.bucket_name,
            Key=file_key,
            Body=file_data,
            ContentType=content_type,
            Metadata=sanitize_metadata(metadata),
        )

    # The Exception need to be handled by the caller
    def get_presigned_url(
        self,
        file_key: str,
        expiration: int = 3600,  # 1 hour
    ) -> str:
        return self.s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket_name, "Key": file_key},
            ExpiresIn=expiration,
        )

    def delete_file(self, s3_key: str) -> bool:
        try:
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=s3_key)
            logger.info("File deleted successfully from S3")
            return True
        except ClientError as e:
            error_code = self.client_error_code(e)
            if error_code in ("404", "NoSuchKey"):
                logger.info("S3 file already absent (404/NoSuchKey)")
                return True
            logger.error(
                "Failed to delete file from S3: %s",
                error_code or type(e).__name__,
            )
            return False
        except Exception as e:
            logger.error(
                "Unexpected error during file deletion from S3: %s",
                type(e).__name__,
            )
            return False

    @staticmethod
    def client_error_code(error: ClientError) -> str | None:
        response = cast(dict[str, Any], error.response)
        error_info = response.get("Error")
        if not isinstance(error_info, dict):
            return None
        error_info_map = cast(dict[str, Any], error_info)
        code = error_info_map.get("Code")
        return code if isinstance(code, str) else None

    def download_file(self, s3_key: str, output_path: str):
        try:
            self.s3_client.download_file(
                Bucket=self.bucket_name, Key=s3_key, Filename=output_path
            )
            logger.info("File downloaded successfully from S3")
        except ClientError as e:
            error_code = self.client_error_code(e)
            logger.error(
                "Failed to download file from S3: %s",
                error_code or type(e).__name__,
            )
            if error_code in ("404", "NoSuchKey"):
                raise FileNotFoundError("S3 object not found or expired") from None
            raise S3AuthentificationException() from None
        except Exception as e:
            logger.error(
                "Unexpected error during file download: %s",
                type(e).__name__,
            )
            raise RuntimeError(
                f"Unexpected error during S3 file download ({type(e).__name__})"
            ) from None

    def get_file_info(self, s3_key: str) -> Optional[Dict[str, Any]]:
        try:
            response = self.s3_client.head_object(Bucket=self.bucket_name, Key=s3_key)

            return {
                "s3_key": s3_key,
                "size": response["ContentLength"],
                "content_type": response["ContentType"],
                "last_modified": response["LastModified"].isoformat(),
                "metadata": response.get("Metadata", {}),
            }

        except ClientError as e:
            error_code = self.client_error_code(e)
            if error_code in ("404", "NoSuchKey"):
                return None
            logger.error(
                "Failed to get file info from S3: %s",
                error_code or type(e).__name__,
            )
            return None
        except Exception as e:
            logger.error(
                "Unexpected error getting file info: %s",
                type(e).__name__,
            )
            return None

    def check_bucket_exists(self) -> bool:
        """
        Check if the configured S3 bucket exists.

        Returns:
            True if bucket exists, False otherwise
        """
        try:
            self.s3_client.head_bucket(Bucket=self.bucket_name)
            return True
        except ClientError as e:
            error_code = self.client_error_code(e)
            if error_code == "404":
                logger.warning(f"S3 bucket '{self.bucket_name}' does not exist")
                return False
            logger.error("S3 bucket check failed: %s", error_code or type(e).__name__)
            return False
        except Exception as e:
            logger.error("Unexpected error checking bucket: %s", type(e).__name__)
            return False

    # The Exception need to be handled by the caller
    def check_connectivity(self):
        self.s3_client.list_buckets()

    def list_buckets(self) -> list[str]:
        try:
            response: ListBucketsOutputTypeDef = self.s3_client.list_buckets()
            buckets: list[BucketTypeDef] = response.get("Buckets", [])
            list_bucket_names: list[str] = []
            for bucket in buckets:
                if "Name" in bucket:
                    list_bucket_names.append(bucket["Name"])
            return list_bucket_names
        except ClientError as e:
            error_code = self.client_error_code(e)
            if error_code == "404":
                return list()
            logger.error(
                "Failed to list buckets: %s",
                error_code or type(e).__name__,
            )
            return list()
        except Exception as e:
            logger.error("Unexpected error listing buckets: %s", type(e).__name__)
            return list()
