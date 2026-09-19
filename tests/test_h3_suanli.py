from __future__ import annotations

from unittest.mock import MagicMock

from control_plane.h3_suanli import H3_IMAGE, SuanliClient, SuanliResource


def _client() -> SuanliClient:
    client = SuanliClient("test-token")
    client.resource = MagicMock(return_value=SuanliResource(
        mark="signed-mark",
        resource={"device_name": "5090", "gpu_count": 1},
        region_name="test-region",
        inventory=1,
        price=1,
    ))
    client._call = MagicMock(return_value={"task_id": 123})
    return client


def test_spot_worker_uses_job_api_and_spot_contract() -> None:
    client = _client()
    try:
        assert client.create_h3_worker(
            "h3-spot", "5090_32g", provider_mode="spot", spot_estimated_exec_seconds=82800
        ) == 123
        client.resource.assert_called_once_with("5090_32g", "spot")
        method, path = client._call.call_args.args
        body = client._call.call_args.kwargs["json"]
        assert (method, path) == ("POST", "/api/task/job/create")
        assert body["sub_type"] == "Spot"
        assert body["job_support"]["estimated_exec_sec"] == 82800
        assert body["job_support"]["timeout_sec"] == 86400
        assert body["services"][0]["service_image"] == H3_IMAGE
        assert body["services"][0]["remote_ports"] == [
            {"service_port": 8188}, {"service_port": 3000}
        ]
    finally:
        client.close()


def test_spot_resource_search_uses_job_inventory() -> None:
    client = SuanliClient("test-token")
    client._call = MagicMock(return_value={
        "results": [{
            "device_name": "5090",
            "gpu_count": 1,
            "gpu_memory": 32768,
            "regions": [{
                "inventory": 2,
                "mark": {"mark": "signed-mark", "resource": {"gpu_name": "5090"}},
                "region_name": "test-region",
                "price": 10,
            }],
        }],
    })
    try:
        selected = client.resource("5090_32g", "spot")
        assert selected.inventory == 2
        client._call.assert_called_once_with(
            "GET",
            "/api/deployment/resource/search",
            params={"task_type": "Job", "device_type": "GpuDevice"},
        )
    finally:
        client.close()


def test_spot_detail_and_stop_use_job_endpoints() -> None:
    client = _client()
    try:
        client._call.return_value = {"status": "Running"}
        assert client.detail(123, provider_mode="spot")["status"] == "Running"
        client._call.assert_called_with("GET", "/api/task/job/detail", params={"task_id": 123})
        client.stop(123, provider_mode="spot")
        client._call.assert_called_with("POST", "/api/task/job/stop", json={"task_id": 123})
    finally:
        client.close()
