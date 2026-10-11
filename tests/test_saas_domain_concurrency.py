import asyncio
import inspect
from threading import Event, Lock, get_ident
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from test_saas_domain import fixture_dataset

from backend.saas_api import create_saas_router
from backend.saas_models import Job, Page, PersonSummary


def test_all_saas_store_endpoints_are_threadpool_dispatched_sync_functions():
    router = create_saas_router(lambda: None)
    assert len(router.routes) == 24
    assert all(not inspect.iscoroutinefunction(route.endpoint) for route in router.routes)


def test_ten_blocking_list_calls_overlap_without_blocking_health():
    fixture = fixture_dataset()
    job_page = Page[Job](items=[fixture.jobs[0]], next_cursor=None, total=1)
    summary = PersonSummary.model_validate(
        fixture.people[0].public().model_dump(exclude={"employment_history"}),
    )
    people_page = Page[PersonSummary](items=[summary], next_cursor=None, total=1)
    release, all_entered, lock = Event(), Event(), Lock()
    worker_threads = set()
    active = peak = 0

    class BlockingStore:
        @staticmethod
        def block():
            nonlocal active, peak
            with lock:
                worker_threads.add(get_ident())
                active += 1
                peak = max(peak, active)
                if active == 10:
                    all_entered.set()
            try:
                if not release.wait(timeout=2):
                    raise RuntimeError("Store call was not released; possible event-loop blocking.")
            finally:
                with lock:
                    active -= 1

        def list_jobs(self, _tenant, **_kwargs):
            self.block()
            return job_page

        def list_people(self, _tenant, **_kwargs):
            self.block()
            return people_page

    store = BlockingStore()
    app = FastAPI()

    @app.middleware("http")
    async def authenticated(request, call_next):
        request.state.identity = SimpleNamespace(
            tenant_id="authorized-tenant",
            user_id="test-actor",
            role="coordinator",
        )
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    app.include_router(create_saas_router(lambda: store))

    async def exercise():
        loop_thread = get_ident()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            requests = [
                asyncio.create_task(client.get("/api/saas/jobs" if i % 2 else "/api/saas/people"))
                for i in range(10)
            ]
            try:
                concurrent = await asyncio.wait_for(
                    asyncio.to_thread(all_entered.wait, 1),
                    timeout=1.5,
                )
                assert concurrent, "Ten synchronous list calls must overlap in worker threads."
                response = await asyncio.wait_for(client.get("/health"), timeout=0.5)
                assert response.status_code == 200 and not release.is_set()
                assert active == 10
            finally:
                release.set()
                responses = await asyncio.wait_for(
                    asyncio.gather(*requests, return_exceptions=True),
                    timeout=3,
                )
            assert all(
                isinstance(response, httpx.Response) and response.status_code == 200
                for response in responses
            )
            assert loop_thread not in worker_threads
            assert peak == 10 and active == 0

    asyncio.run(exercise())
