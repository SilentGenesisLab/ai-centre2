from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import os

os.environ.setdefault("SERVICE_TOKEN", "test")

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from control_plane import api
from control_plane.api_keys import ApiKeyStore
from control_plane.generation_jobs import current_api_key_id


def test_api_key_id_reaches_the_endpoint(monkeypatch) -> None:
    """同步依赖会被丢进线程池，set 的 ContextVar 传不回端点；这条用例钉住这个前提。"""
    with TemporaryDirectory() as directory:
        store = ApiKeyStore(Path(directory) / "keys.db")
        created = store.create("视频", None, None, daily_credit_quota=1000)
        monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(service_token="service-secret"))
        monkeypatch.setattr(api, "get_api_key_store", lambda: store)

        app = FastAPI()

        @app.get("/probe", dependencies=[Depends(api.require_service_token)])
        async def probe() -> dict[str, str | None]:
            return {"key_id": current_api_key_id.get()}

        client = TestClient(app)
        response = client.get("/probe", headers={"Authorization": f"Bearer {created['api_key']}"})
        assert response.status_code == 200
        assert response.json()["key_id"] == created["id"]

        service = client.get("/probe", headers={"Authorization": "Bearer service-secret"})
        assert service.status_code == 200
        assert service.json()["key_id"] is None
