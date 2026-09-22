from __future__ import annotations

import asyncio
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import pytest
from fastapi import HTTPException, UploadFile

from control_plane.api import create_upload
from control_plane.oss_storage import OssStorage


def test_prefix_validation_and_public_url() -> None:
    bucket = Mock()
    bucket.put_object_from_file.return_value.status = 200
    storage = OssStorage(bucket, "https://media.example.com/", "ai-centre/uploads", "bucket", "endpoint")
    assert storage.normalize_prefix(None) == "ai-centre/uploads"
    with pytest.raises(ValueError):
        storage.normalize_prefix("../secret")
    with TemporaryDirectory() as directory:
        source = Path(directory) / "sample.PNG"
        source.write_bytes(b"image")
        result = storage.upload(source, "sample.PNG", "image/png", "projects/demo")
    assert result["url"].startswith("https://media.example.com/projects/demo/")
    assert result["url"].endswith(".png")
    assert result["bytes"] == 5


def _wire_storage(monkeypatch) -> Mock:
    """让 /v1/uploads 用真实的 OssStorage 但换掉网络桶，只断言键与 URL 的构造。"""
    bucket = Mock()
    bucket.put_object_from_file.return_value.status = 200
    storage = OssStorage(bucket, "https://media.example.com", "ai-centre/uploads", "bucket", "endpoint")
    monkeypatch.setattr("control_plane.api.OssStorage.from_env", classmethod(lambda cls: storage))
    return bucket


def _upload(body: bytes, filename: str) -> UploadFile:
    return UploadFile(file=io.BytesIO(body), filename=filename)


def test_public_upload_uses_server_side_prefix(monkeypatch) -> None:
    bucket = _wire_storage(monkeypatch)

    result = asyncio.run(create_upload(_upload(b"hello", "clip.MP4")))

    key = bucket.put_object_from_file.call_args[0][0]
    # 公网端点固定落在服务端默认前缀，调用方没有字段可以指定别处
    assert key.startswith("ai-centre/uploads/")
    assert key.endswith(".mp4")
    assert result["url"].startswith("https://media.example.com/ai-centre/uploads/")
    assert result["bytes"] == 5
    assert result["filename"] == "clip.MP4"


def test_public_upload_rejects_empty_body(monkeypatch) -> None:
    _wire_storage(monkeypatch)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(create_upload(_upload(b"", "empty.mp4")))

    assert caught.value.status_code == 422


def test_public_upload_reports_unconfigured_oss(monkeypatch) -> None:
    def missing(cls):
        raise RuntimeError("missing OSS configuration: OSS_BUCKET")

    monkeypatch.setattr("control_plane.api.OssStorage.from_env", classmethod(missing))

    with pytest.raises(HTTPException) as caught:
        asyncio.run(create_upload(_upload(b"x", "clip.mp4")))

    assert caught.value.status_code == 503
