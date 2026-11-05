"""元测试：新加一个会花钱的端点却忘了挂计费钩子时，这里会先红。"""

import os
import re

os.environ.setdefault("SERVICE_TOKEN", "test")

from fastapi.routing import APIRoute

from control_plane import api

# 在 GenerationJobClient.submit() 里自己预占的端点，不能重复挂 billable。
JOB_CLIENT_ENDPOINTS = re.compile(r"^/v1/(video|image|audio|decision|text)-generations/jobs$")
# 不算生成：对象存储、来源校验、算力调度。
NOT_BILLABLE = ("/v1/uploads", "/v1/provenance/inspect", "/v1/admin/", "/internal/")


def _dependency_names(dependant) -> list[str]:
    names: list[str] = []
    for dependency in dependant.dependencies:
        names.append(getattr(dependency.call, "__qualname__", str(dependency.call)))
        names.extend(_dependency_names(dependency))
    return names


def _creating_routes() -> list[APIRoute]:
    routes = []
    for route in api.app.routes:
        if not isinstance(route, APIRoute):
            continue
        if "POST" not in route.methods and "PUT" not in route.methods:
            continue
        path = route.path
        if not path.startswith("/v1/"):
            continue
        if path.startswith(NOT_BILLABLE) or path.endswith("/cancel"):
            continue
        routes.append(route)
    return routes


def test_every_creating_endpoint_is_billed() -> None:
    missing = [
        route.path
        for route in _creating_routes()
        if not JOB_CLIENT_ENDPOINTS.match(route.path)
        and not any("billable" in name for name in _dependency_names(route.dependant))
    ]
    assert missing == [], f"endpoints that cost money but lack billable(): {missing}"


def test_job_client_endpoints_are_not_double_billed() -> None:
    doubled = [
        route.path
        for route in _creating_routes()
        if JOB_CLIENT_ENDPOINTS.match(route.path)
        and any("billable" in name for name in _dependency_names(route.dependant))
    ]
    assert doubled == [], f"held inside submit() already, must not also mount billable(): {doubled}"


def test_color_grade_shows_up_in_job_logs() -> None:
    from control_plane.observability import route_info

    info = route_info("POST", "/v1/color-grade/jobs")
    assert info is not None and info.service == "color_grade"
