from pathlib import Path
from tempfile import TemporaryDirectory

from control_plane.api_keys import ApiKeyStore


def test_api_key_edit_preserves_identity_and_usage() -> None:
    with TemporaryDirectory() as directory:
        store = ApiKeyStore(Path(directory) / "keys.db")
        created = store.create("原名称", 100, "2099-01-01T00:00:00.000+00:00")
        ok, _ = store.authenticate_and_consume(created["api_key"])
        assert ok
        updated = store.update(created["id"], name="  新名称  ", quota=200, expires_at=None)
        assert updated is not None
        assert updated["id"] == created["id"]
        assert updated["prefix"] == created["prefix"]
        assert updated["name"] == "新名称"
        assert updated["quota"] == 200
        assert updated["used"] == 1
        assert updated["expires_at"] is None
