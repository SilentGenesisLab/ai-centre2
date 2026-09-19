from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import pytest

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
