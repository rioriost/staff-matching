"""Shared persistence contract; backend selection is explicit, never a fallback."""

from contextlib import AbstractContextManager
from typing import Protocol

from backend.models import (
    Assignment,
    Candidate,
    ConfirmRequest,
    OptimizationResult,
    OptimizeRequest,
    Proposal,
    RespondRequest,
    RunAccepted,
    RunView,
    Snapshot,
)


class MatchingStore(Protocol):
    def initialize(self) -> None: ...

    def server_lock(self) -> AbstractContextManager[None]: ...

    def recover_interrupted_runs(self, tenant_id: str) -> None: ...

    def snapshot(self, tenant_id: str) -> Snapshot: ...

    def create_run(self, tenant_id: str, request: OptimizeRequest) -> tuple[RunAccepted, bool]: ...

    def start_run(self, tenant_id: str, run_id: str) -> tuple[Snapshot, OptimizeRequest]: ...

    def publish_run(self, tenant_id: str, run_id: str, result: OptimizationResult) -> bool: ...

    def fail_run(self, tenant_id: str, run_id: str, error: str) -> None: ...

    def list_runs(self, tenant_id: str) -> list[RunView]: ...

    def get_run(self, tenant_id: str, run_id: str) -> RunView: ...

    def explanation(
        self, tenant_id: str, run_id: str, order_id: str, staff_id: str
    ) -> Candidate: ...

    def respond(self, tenant_id: str, proposal_id: str, request: RespondRequest) -> Proposal: ...

    def confirm(self, tenant_id: str, proposal_id: str, request: ConfirmRequest) -> Assignment: ...
