from __future__ import annotations

import mimetypes
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import oss2


SAFE_PREFIX = re.compile(r"^[A-Za-z0-9/_-]{1,128}$")


class OssStorage:
    def __init__(self, bucket: Any, public_base_url: str, default_prefix: str, bucket_name: str, endpoint: str) -> None:
        self.bucket = bucket
        self.public_base_url = public_base_url.rstrip("/")
        self.default_prefix = default_prefix.strip("/")
        self.bucket_name = bucket_name
        self.endpoint = endpoint

    @classmethod
    def from_env(cls) -> "OssStorage":
        values = {name: os.environ.get(name, "").strip() for name in (
            "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET", "OSS_ENDPOINT", "OSS_BUCKET"
        )}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise RuntimeError(f"missing OSS configuration: {', '.join(missing)}")
        base = os.environ.get("OSS_PUBLIC_BASE_URL", "").strip() or f"https://{values['OSS_BUCKET']}.{values['OSS_ENDPOINT']}"
        prefix = os.environ.get("OSS_ADMIN_UPLOAD_PREFIX", "ai-centre/uploads")
        auth = oss2.Auth(values["OSS_ACCESS_KEY_ID"], values["OSS_ACCESS_KEY_SECRET"])
        bucket = oss2.Bucket(auth, values["OSS_ENDPOINT"], values["OSS_BUCKET"])
        return cls(bucket, base, prefix, values["OSS_BUCKET"], values["OSS_ENDPOINT"])

    def normalize_prefix(self, prefix: str | None) -> str:
        value = (prefix or self.default_prefix).strip().strip("/")
        if not SAFE_PREFIX.fullmatch(value) or ".." in value.split("/"):
            raise ValueError("invalid OSS prefix")
        return value

    def upload(self, path: Path, original_name: str, content_type: str | None, prefix: str | None) -> dict[str, Any]:
        safe_prefix = self.normalize_prefix(prefix)
        suffix = Path(original_name).suffix.lower()[:16]
        stamp = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        key = f"{safe_prefix}/{stamp}/{uuid4().hex}{suffix}"
        media_type = content_type or mimetypes.guess_type(original_name)[0] or "application/octet-stream"
        result = self.bucket.put_object_from_file(key, str(path), headers={"Content-Type": media_type})
        if not 200 <= result.status < 300:
            raise RuntimeError(f"OSS upload returned HTTP {result.status}")
        return {
            "key": key,
            "url": f"{self.public_base_url}/{quote(key, safe='/')}",
            "content_type": media_type,
            "bytes": path.stat().st_size,
            "filename": original_name,
        }

    def status(self) -> dict[str, Any]:
        return {
            "configured": True,
            "provider": "Aliyun OSS",
            "bucket": self.bucket_name,
            "endpoint": self.endpoint,
            "public_base_url": self.public_base_url,
            "default_prefix": self.default_prefix,
        }
