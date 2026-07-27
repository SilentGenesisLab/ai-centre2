from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .config import GatewayConfig, WorkerEndpoint, load_gateway_config
from .schemas import BatchRequest


class WorkerPool:
    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self._cursor = 0
        self._lock = asyncio.Lock()
        self.inflight = {worker.name: 0 for worker in config.workers}
        self.requests_total = 0
        self.failures_total = 0
        self.worker_success = {worker.name: 0 for worker in config.workers}

    async def ordered_workers(self) -> list[WorkerEndpoint]:
        async with self._lock:
            workers = list(self.config.workers)
            if not workers:
                return []
            offset = self._cursor % len(workers)
            self._cursor += 1
            rotated = workers[offset:] + workers[:offset]
            return sorted(rotated, key=lambda worker: self.inflight[worker.name])

    async def change_inflight(self, worker: WorkerEndpoint, delta: int) -> None:
        async with self._lock:
            self.inflight[worker.name] += delta


config = load_gateway_config()
pool = WorkerPool(config)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(timeout=config.request_timeout_seconds)
    yield
    await app.state.http.aclose()


app = FastAPI(
    title="Video Translate OCR Gateway",
    version="1.0.0",
    lifespan=lifespan,
)


async def _worker_health(client: httpx.AsyncClient, worker: WorkerEndpoint) -> dict[str, Any]:
    try:
        response = await client.get(f"{worker.url}/health", timeout=3.0)
        payload = response.json()
        return {
            "name": worker.name,
            "url": worker.url,
            "healthy": response.status_code == 200 and payload.get("status") == "ok",
            "status_code": response.status_code,
            "detail": payload,
        }
    except Exception as exc:
        return {
            "name": worker.name,
            "url": worker.url,
            "healthy": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    workers = await asyncio.gather(
        *(_worker_health(request.app.state.http, worker) for worker in config.workers)
    )
    healthy_count = sum(bool(worker["healthy"]) for worker in workers)
    status = "ok" if healthy_count == len(workers) else "degraded"
    return {
        "status": status,
        "healthy_workers": healthy_count,
        "worker_count": len(workers),
        "workers": workers,
        "inflight": dict(pool.inflight),
    }


@app.get("/metrics")
async def metrics() -> Response:
    lines = [
        "# TYPE ocr_gateway_requests_total counter",
        f"ocr_gateway_requests_total {pool.requests_total}",
        "# TYPE ocr_gateway_failures_total counter",
        f"ocr_gateway_failures_total {pool.failures_total}",
    ]
    for worker in config.workers:
        lines.append(
            f'ocr_gateway_worker_inflight{{worker="{worker.name}"}} '
            f"{pool.inflight[worker.name]}"
        )
        lines.append(
            f'ocr_gateway_worker_success_total{{worker="{worker.name}"}} '
            f"{pool.worker_success[worker.name]}"
        )
    return Response("\n".join(lines) + "\n", media_type="text/plain")


@app.post("/v1/ocr/batch")
async def ocr_batch(request: BatchRequest, raw_request: Request) -> JSONResponse:
    workers = await pool.ordered_workers()
    if not workers:
        raise HTTPException(status_code=503, detail="no OCR workers configured")

    pool.requests_total += 1
    errors: list[str] = []
    payload = request.model_dump(mode="json")
    client: httpx.AsyncClient = raw_request.app.state.http
    for worker in workers:
        await pool.change_inflight(worker, 1)
        try:
            response = await client.post(f"{worker.url}/v1/ocr/batch", json=payload)
            if response.status_code < 500:
                if response.status_code < 400:
                    pool.worker_success[worker.name] += 1
                return JSONResponse(
                    status_code=response.status_code,
                    content=response.json(),
                    headers={"X-OCR-Worker": worker.name},
                )
            errors.append(f"{worker.name}: HTTP {response.status_code}")
        except Exception as exc:
            errors.append(f"{worker.name}: {type(exc).__name__}: {exc}")
        finally:
            await pool.change_inflight(worker, -1)

    pool.failures_total += 1
    raise HTTPException(status_code=503, detail={"workers": errors})
