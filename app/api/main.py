"""ASGI entrypoint: ``uvicorn app.api.main:app --workers 1``.

Production refuses to start (lifespan error) without a ready model, the ML service
module and at least one verified rule. Tests inject a RuntimeSpec via create_app.
"""
from __future__ import annotations

import hmac
import logging
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from anyio import fail_after, to_thread
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.upload import UploadRejected, receive_mov
from app.contracts import AnalyzeAccepted
from app.jobs.media import InvalidVideo, probe_mov
from app.jobs.runtime import REPO_ROOT, production_spec
from app.jobs.store import JobStore
from app.jobs.supervisor import InstanceLock, RuntimeSpec, State, Supervisor, Ticket
from app.settings import Settings

log = logging.getLogger("assar.api")
PROBE_TIMEOUT_SEC = 30.0
WEB_DIR = REPO_ROOT / "app" / "web"


@dataclass
class _Service:
    settings: Settings
    store: JobStore
    supervisor: Supervisor


def create_app(settings: Settings | None = None,
               spec_factory: Callable[[Settings], RuntimeSpec] | None = None,
               web_dir: Path | None = WEB_DIR) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s = settings if settings is not None else Settings.from_env()
        spec = (spec_factory or production_spec)(s)  # ServiceNotReady stops startup.
        lock = InstanceLock(s.outputs_dir / ".assar.lock")
        store = JobStore(s.outputs_dir / "jobs.sqlite3")
        for job_id in store.fail_unfinished("ServiceRestart"):
            _remove_job_files(s, job_id)
        supervisor = Supervisor(s, spec, store)
        try:
            await to_thread.run_sync(supervisor.start)
            app.state.service = _Service(s, store, supervisor)
            yield
        finally:
            await to_thread.run_sync(supervisor.close)
            store.close()
            lock.release()

    app = FastAPI(title="assar", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def service(request: Request) -> _Service:
        return request.app.state.service

    def require_key(request: Request, svc: _Service) -> None:
        given = request.headers.get("x-api-key", "")
        expected = svc.settings.api_key.get_secret_value()
        if not hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(401, "Invalid or missing API key")

    @app.post("/api/v1/analyze", status_code=202)
    async def analyze(request: Request) -> JSONResponse:
        accepted = time.monotonic()  # Before reading the upload (contract).
        svc = service(request)
        require_key(request, svc)
        s = svc.settings
        request_deadline = accepted + s.request_timeout_sec
        if not svc.supervisor.try_reserve():
            raise HTTPException(503, "Service is busy or not ready; retry later")
        job_id = uuid.uuid4().hex
        upload_dir, job_dir = _job_paths(s, job_id)
        submitted = False
        try:
            upload_dir.mkdir(parents=True)
            video = upload_dir / "input.mov"  # Client filename is never used on disk.
            # A slow client must not hold the only slot past the request budget.
            with fail_after(max(0.0, request_deadline - s.response_reserve_sec - time.monotonic())):
                await receive_mov(request, video, s.max_upload_bytes)
            budget = min(PROBE_TIMEOUT_SEC, request_deadline - s.response_reserve_sec - time.monotonic())
            duration = await to_thread.run_sync(probe_mov, video, budget)
            job_dir.mkdir(parents=True)
            svc.store.create(job_id)
            svc.supervisor.submit(Ticket(job_id, video, job_dir, duration, request_deadline))
            submitted = True
        except UploadRejected as exc:
            raise HTTPException(exc.status_code, exc.detail) from None
        except TimeoutError:
            raise HTTPException(408, "Upload took too long") from None
        except InvalidVideo:
            raise HTTPException(422, "File is not a decodable .mov video") from None
        except RuntimeError:  # e.g. ffprobe missing: a service fault, not a bad upload.
            log.error("upload probe unavailable for job %s", job_id)
            raise HTTPException(503, "Service is not ready; retry later") from None
        finally:
            if not submitted:
                shutil.rmtree(upload_dir, ignore_errors=True)
                shutil.rmtree(job_dir, ignore_errors=True)
                svc.supervisor.release()
        return JSONResponse(AnalyzeAccepted(job_id=job_id).model_dump(), status_code=202)

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(job_id: str, request: Request) -> Response:
        svc = service(request)
        require_key(request, svc)
        body = svc.store.public_json(job_id)
        if body is None:
            raise HTTPException(404, "Unknown job_id")
        return Response(body, media_type="application/json")

    @app.get("/api/internal/status")
    async def status(request: Request) -> JSONResponse:
        """For the team UI only; not part of the jury schema."""
        svc = service(request)
        require_key(request, svc)
        return JSONResponse({"accepting": svc.supervisor.state is State.READY})

    if web_dir is not None and web_dir.is_dir():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app


def _job_paths(s: Settings, job_id: str) -> tuple[Path, Path]:
    return s.uploads_dir.resolve() / job_id, s.outputs_dir.resolve() / "jobs" / job_id


def _remove_job_files(s: Settings, job_id: str) -> None:
    for path in _job_paths(s, job_id):
        shutil.rmtree(path, ignore_errors=True)


app = create_app()
