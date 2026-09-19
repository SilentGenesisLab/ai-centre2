import json

from ocr_service.config import load_gateway_config


def test_load_gateway_config_builds_worker_endpoints(tmp_path) -> None:
    path = tmp_path / "gateway.json"
    path.write_text(
        json.dumps(
            {
                "host": "127.0.0.1",
                "port": 9000,
                "request_timeout_seconds": 30,
                "workers": [{"name": "gpu1", "url": "http://127.0.0.1:9001/"}],
            }
        ),
        encoding="utf-8",
    )

    config = load_gateway_config(path)

    assert config.port == 9000
    assert config.request_timeout_seconds == 30
    assert config.workers[0].name == "gpu1"
    assert config.workers[0].url == "http://127.0.0.1:9001"
