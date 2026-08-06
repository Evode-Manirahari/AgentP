from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import BinaryIO

from app.config import Settings, get_settings


def safe_filename(filename: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip(".-")
    return cleaned or "document"


class StorageService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        import boto3
        from botocore.config import Config

        client_config = Config(signature_version="s3v4")
        self.client = boto3.client(
            "s3",
            endpoint_url=self.settings.s3_endpoint_url,
            aws_access_key_id=self.settings.s3_access_key_id,
            aws_secret_access_key=self.settings.s3_secret_access_key,
            region_name=self.settings.s3_region,
            use_ssl=self.settings.s3_use_ssl,
            config=client_config,
        )
        self.presign_client = boto3.client(
            "s3",
            endpoint_url=self.settings.s3_public_endpoint_url or self.settings.s3_endpoint_url,
            aws_access_key_id=self.settings.s3_access_key_id,
            aws_secret_access_key=self.settings.s3_secret_access_key,
            region_name=self.settings.s3_region,
            use_ssl=self.settings.s3_use_ssl,
            config=client_config,
        )

    def ensure_bucket(self) -> None:
        from botocore.exceptions import ClientError

        try:
            self.client.head_bucket(Bucket=self.settings.s3_bucket)
        except ClientError:
            self.client.create_bucket(Bucket=self.settings.s3_bucket)

    def input_key(self, *, document_id: str, filename: str) -> str:
        return f"inputs/{document_id}/{uuid.uuid4().hex}-{safe_filename(filename)}"

    def output_key(self, *, job_id: str, filename: str) -> str:
        return f"outputs/{job_id}/{uuid.uuid4().hex}-{safe_filename(filename)}"

    def upload_fileobj(self, fileobj: BinaryIO, *, key: str, content_type: str) -> None:
        self.ensure_bucket()
        self.client.upload_fileobj(
            fileobj,
            self.settings.s3_bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )

    def upload_path(self, path: Path, *, key: str, content_type: str) -> None:
        with path.open("rb") as file:
            self.upload_fileobj(file, key=key, content_type=content_type)

    def download_to_path(self, *, key: str, path: Path) -> None:
        self.client.download_file(self.settings.s3_bucket, key, str(path))

    def presigned_download_url(self, *, key: str, filename: str) -> str:
        return self.presign_client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.settings.s3_bucket,
                "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{safe_filename(filename)}"',
            },
            ExpiresIn=self.settings.download_url_expires_seconds,
        )
