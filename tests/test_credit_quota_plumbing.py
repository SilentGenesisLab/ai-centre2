from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import os

os.environ.setdefault("SERVICE_TOKEN", "test")

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from control_plane import api
from control_plane.api_keys import ApiKeyStore
from control_plane.billing_hook import BillingRoute, billable
from control_plane.generation_jobs import current_api_key_id


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


def _app(store: ApiKeyStore) -> FastAPI:
    app = FastAPI()
    app.router.route_class = BillingRoute

    @app.get("/probe", dependencies=[Depends(api.require_service_token)])
    async def probe() -> dict[str, str | None]:
        return {"key_id": current_api_key_id.get()}

    @app.post("/bill", dependencies=[Depends(api.require_service_token), Depends(billable("tts"))])
    async def billed() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post("/strict", dependencies=[Depends(api.require_service_token), Depends(billable("tts"))])
    async def strict(body: StrictBody) -> dict[str, str]:
        return {"ok": body.text}

    @app.post("/boom", dependencies=[Depends(api.require_service_token), Depends(billable("tts"))])
    async def boom() -> dict[str, str]:
        raise RuntimeError("upstream exploded")

    return app


def test_api_key_id_reaches_the_endpoint(monkeypatch) -> None:
    """同步依赖会被丢进线程池，set 的 ContextVar 传不回端点；这条用例钉住这个前提。"""
    with TemporaryDirectory() as directory:
        store = ApiKeyStore(Path(directory) / "keys.db")
        created = store.create("视频", None, None)
        monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(service_token="service-secret"))
        monkeypatch.setattr(api, "get_api_key_store", lambda: store)

        client = TestClient(_app(store))
        response = client.get("/probe", headers={"Authorization": f"Bearer {created['api_key']}"})
        assert response.status_code == 200
        assert response.json()["key_id"] == created["id"]

        service = client.get("/probe", headers={"Authorization": "Bearer service-secret"})
        assert service.status_code == 200
        assert service.json()["key_id"] is None


def test_billable_writes_a_settled_charge(monkeypatch) -> None:
    with TemporaryDirectory() as directory:
        store = ApiKeyStore(Path(directory) / "keys.db")
        created = store.create("语音", None, None)
        monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(service_token="service-secret"))
        monkeypatch.setattr(api, "get_api_key_store", lambda: store)

        client = TestClient(_app(store))
        response = client.post(
            "/bill",
            json={"text": "一" * 2000, "model": "some-cloud-tts"},
            headers={"Authorization": f"Bearer {created['api_key']}"},
        )
        assert response.status_code == 200
        charges = store.charges()
        assert charges["total"] == 1
        row = charges["items"][0]
        assert row["service"] == "tts"
        assert row["state"] == "spent"
        assert row["key_id"] == created["id"]
        assert row["amount_micro"] == 200_000  # 0.10 元 / 千字 × 2 千字


def test_billable_refunds_when_the_endpoint_raises(monkeypatch) -> None:
    with TemporaryDirectory() as directory:
        store = ApiKeyStore(Path(directory) / "keys.db")
        created = store.create("语音", None, None)
        monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(service_token="service-secret"))
        monkeypatch.setattr(api, "get_api_key_store", lambda: store)

        client = TestClient(_app(store), raise_server_exceptions=False)
        response = client.post(
            "/boom",
            json={"text": "一" * 2000, "model": "some-cloud-tts"},
            headers={"Authorization": f"Bearer {created['api_key']}"},
        )
        assert response.status_code == 500
        assert store.charges()["items"][0]["state"] == "refunded"


def test_billable_refunds_when_the_body_is_rejected(monkeypatch) -> None:
    """422 的请求没做任何事，不能扣钱——FastAPI 不会把校验失败抛进 yield 依赖。"""
    with TemporaryDirectory() as directory:
        store = ApiKeyStore(Path(directory) / "keys.db")
        created = store.create("语音", None, None)
        monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(service_token="service-secret"))
        monkeypatch.setattr(api, "get_api_key_store", lambda: store)

        client = TestClient(_app(store))
        response = client.post(
            "/strict",
            json={"text": "一" * 2000, "unexpected": 1},
            headers={"Authorization": f"Bearer {created['api_key']}"},
        )
        assert response.status_code == 422
        assert store.charges()["items"][0]["state"] == "refunded"


def test_billable_rejects_over_limit_with_429(monkeypatch) -> None:
    with TemporaryDirectory() as directory:
        store = ApiKeyStore(Path(directory) / "keys.db")
        created = store.create("语音", None, None, limit_5h_micro=100_000)
        monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(service_token="service-secret"))
        monkeypatch.setattr(api, "get_api_key_store", lambda: store)

        client = TestClient(_app(store))
        response = client.post(
            "/bill",
            json={"text": "一" * 2000, "model": "some-cloud-tts"},
            headers={"Authorization": f"Bearer {created['api_key']}"},
        )
        assert response.status_code == 429
        assert "key_5h" in response.json()["detail"]
        assert store.charges()["total"] == 0
