"""Durable Research lifecycle coordination, separate from round execution."""
from __future__ import annotations

import secrets
from threading import RLock
from typing import Callable

from src.application.commands import ResearchCommand, ResearchFeedbackCommand
from src.application.ports.research_store import ResearchStore
from src.application.ports.runtime import Clock
from src.application.ports.search_seed import SearchSeedStore, StoredSearchSeed
from src.application.research_dispatcher import ResearchDispatcher, ResearchQueueFull
from src.application.research_errors import ResearchRequestError
from src.application.research_planner import ResearchPlanner
from src.domain.research import (
    ResearchLinks,
    ResearchStop,
    ResearchTaskEnvelope,
    ResolvedResearch,
)


class ResearchCoordinator:
    """Own admission, public lifecycle transitions, feedback and recovery."""

    def __init__(
        self,
        *,
        seed_store: SearchSeedStore,
        task_store: ResearchStore,
        clock: Clock,
        planner: ResearchPlanner,
        request_hash: Callable[[ResearchCommand], str],
        resolve: Callable[[ResearchCommand, StoredSearchSeed], ResolvedResearch],
        links: Callable[[str], ResearchLinks],
    ) -> None:
        self._seed_store = seed_store
        self._task_store = task_store
        self._clock = clock
        self._planner = planner
        self._request_hash = request_hash
        self._resolve = resolve
        self._links = links
        self._dispatcher: ResearchDispatcher | None = None
        self._recovery_lock = RLock()

    @property
    def dispatcher(self) -> ResearchDispatcher | None:
        return self._dispatcher

    def attach_dispatcher(self, dispatcher: ResearchDispatcher) -> None:
        self._dispatcher = dispatcher

    def recover_pending(self) -> None:
        dispatcher = self._dispatcher
        if dispatcher is None:
            return
        with self._recovery_lock:
            for research_id in self._task_store.runnable():
                if dispatcher.contains(research_id):
                    continue
                try:
                    dispatcher.submit(research_id)
                except ResearchQueueFull:
                    break
                except RuntimeError:
                    break

    def start(
        self,
        command: ResearchCommand,
        *,
        idempotency_key: str,
    ) -> ResearchTaskEnvelope:
        dispatcher = self._dispatcher
        if dispatcher is None:
            raise RuntimeError("Research dispatcher 尚未装配")
        idempotency_key = idempotency_key.strip()
        if not idempotency_key or len(idempotency_key) > 200:
            raise ResearchRequestError(
                "Idempotency-Key 必须是 1 到 200 个字符的非空值"
            )
        request_hash = self._request_hash(command)
        existing = self._task_store.find_by_idempotency(
            idempotency_key,
            request_hash,
        )
        if existing is not None:
            return existing
        seed = self._seed_store.get(command.search_id)
        now = self._clock.now()
        research_id = "rsch_" + secrets.token_urlsafe(18)
        task = ResearchTaskEnvelope(
            research_id=research_id,
            state="queued",
            phase="planning",
            seed_search_id=seed.seed.search_id,
            seed_snapshot_hash=seed.seed.seed_snapshot_hash,
            created_at=now,
            updated_at=now,
            resolved=self._resolve(command, seed),
            links=self._links(research_id),
            retry_after_ms=500,
        )
        reservation = dispatcher.reserve()
        try:
            stored, created = self._task_store.create(
                task,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                seed_snapshot=seed.snapshot,
            )
            if created:
                reservation.submit(stored.research_id)
            else:
                reservation.release()
        except BaseException:
            reservation.release()
            raise
        return stored

    def get(
        self,
        research_id: str,
        *,
        detail: str = "standard",
    ) -> ResearchTaskEnvelope:
        task = self._task_store.get(research_id)
        if detail == "full" or task.dossier is None:
            return task
        refs = {
            ref
            for finding in task.dossier.findings
            for ref in (
                finding.assessment.support_refs
                + finding.assessment.conflict_refs
                + finding.assessment.mention_refs
            )
        }
        dossier = task.dossier.model_copy(update={
            "evidence_index": {
                key: item
                for key, item in task.dossier.evidence_index.items()
                if key in refs
            },
            "query_trace": [],
            "rounds": [],
        })
        return task.model_copy(update={"dossier": dossier})

    def feedback(
        self,
        research_id: str,
        command: ResearchFeedbackCommand,
    ) -> ResearchTaskEnvelope:
        dispatcher = self._dispatcher
        if dispatcher is None:
            raise RuntimeError("Research dispatcher 尚未装配")
        current = self._task_store.get(research_id)
        if current.task_revision != command.task_revision:
            raise ValueError("task_revision 已过期")
        if current.state != "needs_input":
            raise ValueError("只有 needs_input 状态可以提交 feedback")
        latest = self._task_store.latest_plan(research_id)
        if latest is None or current.resolved is None:
            raise ValueError("Research 计划不存在，无法应用 feedback")
        attempt, plan = latest
        resolution = self._planner.apply_feedback(
            current.resolved,
            plan,
            command.answers,
        )
        resolved = resolution.resolved
        if command.note:
            resolved = resolved.model_copy(update={
                "adjustments": [
                    *resolved.adjustments,
                    f"用户反馈: {command.note}",
                ]
            })
        next_attempt = attempt + 1
        self._task_store.save_plan(
            research_id,
            attempt=next_attempt,
            plan=resolution.plan,
        )
        if resolution.plan.ambiguities:
            updated = current.model_copy(update={
                "state": "needs_input",
                "phase": None,
                "task_revision": current.task_revision + 1,
                "updated_at": self._clock.now(),
                "resolved": resolved,
                "input_request": self._planner.input_request(resolution.plan),
                "retry_after_ms": None,
            })
            saved = self._task_store.save(
                updated,
                expected_revision=current.task_revision,
            )
            self._task_store.append_event(
                research_id,
                attempt=next_attempt,
                kind="feedback_applied",
                payload={
                    "plan_revision": resolution.plan.revision,
                    "answered_fields": sorted(command.answers),
                    "remaining_questions": len(resolution.plan.ambiguities),
                },
            )
            return saved
        reservation = dispatcher.reserve()
        updated = current.model_copy(update={
            "state": "queued",
            "phase": "planning",
            "task_revision": current.task_revision + 1,
            "updated_at": self._clock.now(),
            "resolved": resolved,
            "input_request": None,
            "stop": None,
            "retry_after_ms": 500,
        })
        try:
            saved = self._task_store.save(
                updated,
                expected_revision=current.task_revision,
            )
            self._task_store.append_event(
                research_id,
                attempt=next_attempt,
                kind="feedback_applied",
                payload={
                    "plan_revision": resolution.plan.revision,
                    "answered_fields": sorted(command.answers),
                    "remaining_questions": 0,
                },
            )
            reservation.submit(research_id)
        except BaseException:
            reservation.release()
            raise
        return saved

    def cancel(
        self,
        research_id: str,
        *,
        task_revision: int | None = None,
    ) -> ResearchTaskEnvelope:
        current = self._task_store.get(research_id)
        if task_revision is not None and current.task_revision != task_revision:
            raise ValueError("task_revision 已过期")
        if current.state in {"completed", "partial", "failed", "cancelled"}:
            return current
        updated = current.model_copy(update={
            "state": "cancelled",
            "phase": None,
            "task_revision": current.task_revision + 1,
            "updated_at": self._clock.now(),
            "stop": ResearchStop(
                reason="cancelled_by_user",
                message="研究任务已由调用方取消。",
            ),
            "retry_after_ms": None,
        })
        saved = self._task_store.cancel(
            updated,
            expected_revision=current.task_revision,
        )
        if self._dispatcher is not None:
            self._dispatcher.cancel(research_id)
        return saved
