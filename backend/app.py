"""Synthetic-data HTTP adapter for local SQLite and authenticated Azure PostgreSQL."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend.cloud_auth import CloudAccessMiddleware, CoordinatorCredentials
from backend.models import (
    Assignment,
    Candidate,
    ConfirmRequest,
    OptimizationResult,
    OptimizeRequest,
    Order,
    Proposal,
    RespondRequest,
    RunAccepted,
    RunView,
    Snapshot,
    Staff,
)
from backend.persistence import MatchingStore
from backend.store import Store, StoreError

if TYPE_CHECKING:
    from backend.identity import IdentityStore
    from backend.saas_store import SaasStore

logger = logging.getLogger(__name__)


def create_app(
    *,
    database_path: str | Path | None = None,
    tenant_id: str | None = None,
    mode: str | None = None,
    optimizer: Callable[..., OptimizationResult] | None = None,
    store: MatchingStore | None = None,
    extension_report: Callable[[str], dict[str, object]] | None = None,
    identity_store: IdentityStore | None = None,
    saas_store: SaasStore | None = None,
) -> FastAPI:
    configured_mode = mode if mode is not None else os.getenv("APP_MODE", "local-mock")
    auth_mode = os.getenv("POC_AUTH_MODE", "basic")
    if auth_mode not in ("basic", "accounts"):
        raise RuntimeError("POC_AUTH_MODE must be basic or accounts.")
    accounts_enabled = configured_mode == "azure-poc" and auth_mode == "accounts"
    for name in ("SAAS_ENABLED", "SAAS_DEMO_SIMULATION"):
        if os.getenv(name, "false") not in ("true", "false"):
            raise RuntimeError(f"{name} must be true or false.")
    saas_enabled = os.getenv("SAAS_ENABLED", "false") == "true"
    allow_simulation = os.getenv("SAAS_DEMO_SIMULATION", "false") == "true"
    if saas_enabled and not accounts_enabled:
        raise RuntimeError("SaaS requires Azure PostgreSQL and account authentication.")
    if allow_simulation and not saas_enabled:
        raise RuntimeError("Demo actor simulation requires the explicit SaaS mode.")
    static_dir = Path(
        os.getenv("STATIC_DIR", str(Path(__file__).resolve().parent.parent / "frontend" / "dist"))
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        diagnostics = extension_report
        active_store: MatchingStore
        if configured_mode == "local-mock":
            tenant = tenant_id if tenant_id is not None else os.getenv("LOCAL_TENANT_ID", "demo")
            active_store = store if store is not None else Store(
                database_path or os.getenv("STAFF_MATCHING_DB", "data/staff-matching.sqlite3")
            )
        elif configured_mode == "azure-poc":
            if accounts_enabled:
                tenant = None
                if tenant_id is not None:
                    raise RuntimeError("Account authentication requires request-scoped tenancy.")
            else:
                application.state.coordinator_credentials = (
                    CoordinatorCredentials.from_environment()
                )
                tenant = os.getenv("STAFF_MATCHING_TENANT", "")
                if not tenant.strip():
                    raise RuntimeError("Azure PoC requires STAFF_MATCHING_TENANT.")
                if tenant_id is not None and tenant_id != tenant:
                    raise RuntimeError("Azure PoC tenant must match STAFF_MATCHING_TENANT.")
            missing = [
                name for name in ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD")
                if not os.getenv(name, "").strip()
            ]
            if missing:
                raise RuntimeError(
                    "Azure PoC requires PostgreSQL configuration: " + ", ".join(missing)
                )
            if os.getenv("PGSSLMODE") != "verify-full":
                raise RuntimeError("Azure PoC requires PGSSLMODE=verify-full.")
            if not (static_dir / "index.html").is_file() or not (static_dir / "assets").is_dir():
                raise RuntimeError(
                    "Azure PoC requires the built frontend in STATIC_DIR or frontend/dist."
                )
            if database_path is not None:
                raise RuntimeError("Azure PoC does not support a SQLite database_path.")
            if store is not None:
                # Tests must explicitly inject their store; DB failures never select SQLite.
                active_store = store
            else:
                from backend.postgres_store import PostgresStore

                postgres_store = PostgresStore()
                active_store = postgres_store
                diagnostics = postgres_store.extension_report
        else:
            raise RuntimeError(
                "APP_MODE must be local-mock or azure-poc; production is unavailable."
            )
        with active_store.server_lock():
            active_store.initialize()
            if accounts_enabled:
                from backend.identity import IdentityStore

                identities = identity_store if identity_store is not None else IdentityStore()
                identities.initialize()
                application.state.identity_store = identities
            else:
                active_store.recover_interrupted_runs(tenant)
                application.state.tenant_id = tenant
            if saas_enabled:
                from backend.saas_store import SaasStore

                active_saas = saas_store if saas_store is not None else SaasStore()
                active_saas.initialize()
                application.state.saas_store = active_saas
            application.state.store = active_store
            application.state.extension_report = diagnostics
            application.state.executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=f"{configured_mode}-solver"
            )
            application.state.tasks = set()
            try:
                yield
            finally:
                # Drain bounded solver work before releasing the backend's server lease.
                try:
                    if application.state.tasks:
                        await asyncio.gather(*application.state.tasks)
                finally:
                    application.state.executor.shutdown(wait=True, cancel_futures=True)

    application = FastAPI(
        title="Staff matching — synthetic-data PoC", lifespan=lifespan,
        docs_url=None if accounts_enabled else "/docs",
        redoc_url=None if accounts_enabled else "/redoc",
    )
    application.state.coordinator_credentials = None
    application.state.identity_store = None
    application.state.saas_store = None
    if configured_mode == "local-mock":
        application.add_middleware(
            CORSMiddleware,
            allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "X-Requested-With"],
        )
    elif accounts_enabled:
        from backend.identity import (
            AccountAuthMiddleware,
            AuthSettings,
            create_identity_router,
        )

        settings = AuthSettings.from_environment()
        application.add_middleware(
            AccountAuthMiddleware,
            store=lambda: application.state.identity_store,
            settings=settings,
        )
        application.include_router(create_identity_router(
            store=lambda: application.state.identity_store,
            settings=settings,
        ))
    else:
        application.add_middleware(
            CloudAccessMiddleware,
            credentials=lambda: application.state.coordinator_credentials,
        )
    api = APIRouter()
    if saas_enabled:
        from backend.saas_api import create_saas_router

        application.include_router(create_saas_router(
            store=lambda: application.state.saas_store,
            allow_simulation=allow_simulation,
        ))
        geographic_files = {
            "network.json.gz": "application/gzip",
            "basemap.geojson": "application/geo+json",
            "manifest.json": "application/json",
            "LICENSE.txt": "text/plain",
        }
        geographic_directory = Path(__file__).resolve().parent.parent / "data" / "geo"

        def geographic_file(name: str) -> FileResponse:
            media_type = geographic_files.get(name)
            if media_type is None:
                raise StoreError(404, "reference_not_found", "Geographic reference not found.")
            path = geographic_directory / name
            if not path.is_file():
                raise StoreError(503, "reference_unavailable", "Geographic reference unavailable.")
            return FileResponse(path, media_type=media_type)

        @application.get("/assets/geography/{name}", include_in_schema=False)
        def public_geographic_reference(name: str) -> FileResponse:
            return geographic_file(name)

        @application.get("/api/saas/geo/basemap", include_in_schema=False)
        def geographic_basemap() -> FileResponse:
            return geographic_file("basemap.geojson")

        @application.get("/api/saas/geo/manifest", include_in_schema=False)
        def geographic_manifest() -> FileResponse:
            return geographic_file("manifest.json")

    @application.exception_handler(StoreError)
    async def store_error_handler(_request: Request, error: StoreError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status,
            content={"detail": {"code": error.code, "message": error.message}},
        )

    if accounts_enabled:
        @application.get("/docs", include_in_schema=False)
        @application.get("/redoc", include_in_schema=False)
        def account_api_definition() -> RedirectResponse:
            return RedirectResponse("/openapi.json")

        @application.exception_handler(RequestValidationError)
        async def account_validation_error(
            _request: Request, _error: RequestValidationError
        ) -> JSONResponse:
            return JSONResponse(
                status_code=422,
                content={"detail": {
                    "code": "invalid_request",
                    "message": "入力内容と文字数を確認してください。",
                }},
            )

    @application.exception_handler(ValueError)
    async def value_error_handler(_request: Request, _error: ValueError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "code": "invalid_input",
                    "message": "The current synthetic input cannot be evaluated.",
                }
            },
        )

    def current_store() -> MatchingStore:
        return application.state.store

    def current_tenant(request: Request) -> str:
        if accounts_enabled:
            identity = getattr(request.state, "identity", None)
            if identity is None:
                raise StoreError(401, "authentication_required", "Account login required.")
            return identity.tenant_id
        return application.state.tenant_id

    def current_snapshot(request: Request) -> Snapshot:
        return current_store().snapshot(current_tenant(request))

    def execute_run(run_id: str, tenant: str) -> None:
        active_store = current_store()
        try:
            solver = optimizer
            if solver is None:
                from backend.engine import optimize

                solver = optimize
            snapshot, config = active_store.start_run(tenant, run_id)
            result = solver(snapshot, time_limit_seconds=config.time_limit_seconds)
            active_store.publish_run(tenant, run_id, result)
        except Exception as error:
            # Persist a safe error category without exposing input records or exception contents.
            logger.error("Matching run failed: %s", type(error).__name__)
            active_store.fail_run(
                tenant,
                run_id,
                f"Solver failed ({type(error).__name__}); retry with a new run.",
            )

    def finished(task: asyncio.Future) -> None:
        application.state.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "Run failure could not be persisted: %s", type(task.exception()).__name__
            )

    @application.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
    def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/health")
    def health(request: Request) -> dict[str, str]:
        return {
            "status": "ok",
            "mode": configured_mode,
            "tenant_id": current_tenant(request),
            "persistence": "postgresql" if configured_mode == "azure-poc" else "sqlite-mock",
        }

    @api.get("/workspace")
    def workspace() -> dict[str, str]:
        return {"kind": "saas" if saas_enabled else "legacy"}

    if not accounts_enabled:
        @api.get("/auth/session")
        def legacy_session() -> dict[str, bool]:
            return {"enabled": False, "authenticated": False}

    @api.get("/extensions")
    def extensions(request: Request) -> dict[str, object]:
        if configured_mode != "azure-poc":
            return {"persistence": "sqlite-mock", "extensions": []}
        report = application.state.extension_report
        if report is None:
            raise StoreError(
                503, "diagnostics_unavailable", "Database diagnostics are unavailable."
            )
        return report(current_tenant(request))

    @api.get("/staff", response_model=list[Staff])
    def staff(request: Request) -> list[Staff]:
        return current_snapshot(request).staff

    @api.get("/orders", response_model=list[Order])
    def orders(request: Request) -> list[Order]:
        return current_snapshot(request).orders

    @api.get("/assignments", response_model=list[Assignment])
    def assignments(request: Request) -> list[Assignment]:
        return current_snapshot(request).assignments

    @api.get("/orders/{order_id}/candidates", response_model=list[Candidate])
    def candidates(order_id: str, request: Request) -> list[Candidate]:
        from backend.engine import generate_candidates

        snapshot = current_snapshot(request)
        if not any(order.id == order_id for order in snapshot.orders):
            raise StoreError(404, "order_not_found", "Order not found.")
        return [
            candidate
            for candidate in generate_candidates(snapshot)
            if candidate.order_id == order_id
        ]

    @api.post("/matching/optimize", status_code=202, response_model=RunAccepted)
    async def submit(request: OptimizeRequest, http_request: Request) -> RunAccepted:
        tenant = current_tenant(http_request)
        accepted, created = await asyncio.to_thread(
            current_store().create_run, tenant, request
        )
        if created:
            task = asyncio.get_running_loop().run_in_executor(
                application.state.executor, execute_run, accepted.run_id, tenant
            )
            application.state.tasks.add(task)
            task.add_done_callback(finished)
        return accepted

    @api.get("/matching/runs", response_model=list[RunView])
    def runs(request: Request) -> list[RunView]:
        return current_store().list_runs(current_tenant(request))

    @api.get("/matching/runs/{run_id}", response_model=RunView)
    def run(run_id: str, request: Request) -> RunView:
        return current_store().get_run(current_tenant(request), run_id)

    @api.get(
        "/matching/runs/{run_id}/explanations/{order_id}/{staff_id}", response_model=Candidate
    )
    def explanation(
        run_id: str, order_id: str, staff_id: str, request: Request
    ) -> Candidate:
        return current_store().explanation(current_tenant(request), run_id, order_id, staff_id)

    @api.post("/proposals/{proposal_id}/respond", response_model=Proposal)
    def respond(
        proposal_id: str, request: RespondRequest, http_request: Request
    ) -> Proposal:
        return current_store().respond(current_tenant(http_request), proposal_id, request)

    @api.post("/proposals/{proposal_id}/confirm", response_model=Assignment)
    def confirm(
        proposal_id: str, request: ConfirmRequest, http_request: Request
    ) -> Assignment:
        return current_store().confirm(current_tenant(http_request), proposal_id, request)

    application.include_router(api, prefix="/api")
    application.include_router(api, include_in_schema=False)

    @application.get("/", include_in_schema=False)
    def frontend() -> FileResponse:
        index = static_dir / "index.html"
        if not index.is_file():
            raise StoreError(
                404, "frontend_unavailable", "Build the frontend or use the local Vite server."
            )
        return FileResponse(index)

    application.mount(
        "/assets", StaticFiles(directory=static_dir / "assets", check_dir=False), name="assets"
    )
    return application


app = create_app()
