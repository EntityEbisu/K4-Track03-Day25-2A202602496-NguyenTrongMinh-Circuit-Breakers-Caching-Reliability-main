"""Part D — Exactly-once *effects* for an agent workflow.

Two ingredients, and you need both:

1. **Idempotency keys** — a retry (or a re-run after a crash) of a side effect
   must reuse the *same* key, so the payment provider de-duplicates it.
   The key must be derived from *what* you are doing (session + step), never
   random per attempt.
2. **Checkpoints** — after each step succeeds, persist its result in the
   DurableStore. A fresh worker resumes from the last checkpoint instead of
   redoing (and re-paying for) everything.

Think hard about the ORDER of "execute step" and "save checkpoint".
Never catch ``WorkerCrash`` — it means this process is dead.

Tests: tests/test_executor.py, then `make workflows`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from gameday_lab.workflows.runner import STEPS, RefundRequest
from gameday_lab.workflows.services import DurableStore, ServiceError, Services, ServiceTimeout

T = TypeVar("T")


def idempotency_key(req: RefundRequest, step: str) -> str:
    """TODO(student): a key that is identical for every retry / restart of the same
    side effect of the same session, and different across sessions and steps."""
    # Derived from *what* we are doing, never from a random source: every retry
    # and every restarted worker must present the same key, or the payment
    # provider cannot recognise the duplicate.
    return f"{req.session_id}:{step}"


def checkpoint_key(req: RefundRequest, step: str) -> str:
    """TODO(student): DurableStore key under which the result of ``step`` is saved."""
    return f"checkpoint:{req.session_id}:{step}"


class WorkflowExecutor:
    def __init__(self, services: Services, store: DurableStore, max_attempts: int = 5) -> None:
        self.s = services
        self.store = store
        self.max_attempts = max_attempts

    def _retry(self, fn: Callable[[], T]) -> T:
        """TODO(student): call fn(); retry on ServiceError / ServiceTimeout up to
        ``max_attempts`` times (re-raise the last one). Never catch WorkerCrash."""
        last: BaseException | None = None
        for _ in range(max(1, self.max_attempts)):
            try:
                return fn()
            except (ServiceError, ServiceTimeout) as exc:
                # Retryable: the call failed before or after the effect, but the
                # idempotency key makes a repeat safe. WorkerCrash is a
                # BaseException and must keep propagating — this process is dead.
                last = exc
        assert last is not None
        raise last

    def _step(self, req: RefundRequest, state: dict[str, Any], step: str) -> Any:
        """PROVIDED: execute one step. ``state`` holds results of earlier steps."""
        if step == "classify_request":
            return self._retry(lambda: self.s.llm.classify(req.order_id, req.amount_cents))
        if step == "lookup_order":
            return self._retry(lambda: self.s.orders.get(state["classify_request"]["order_id"]))
        if step == "issue_refund":
            key = idempotency_key(req, step)
            return self._retry(lambda: self.s.payments.refund(req.order_id, req.amount_cents, idempotency_key=key))
        if step == "send_email":
            key = idempotency_key(req, step)
            data = {"refund_id": state["issue_refund"]["refund_id"]}
            return self._retry(lambda: self.s.email.send(req.customer_email, "refund_done", data, idempotency_key=key))
        if step == "write_summary":
            return self._retry(lambda: self.s.llm.summarize(state["issue_refund"]["refund_id"],
                                                            state["send_email"]["message_id"]))
        raise ValueError(step)

    def run(self, req: RefundRequest) -> dict[str, Any]:
        """TODO(student): run every step in STEPS for ``req``.

        * a step whose checkpoint already exists in ``self.store`` was done by a
          previous (crashed) worker: load its result into ``state`` and skip it
        * otherwise execute it with ``self._step`` and checkpoint the result
        * return {"status": "done", **state}
        """
        state: dict[str, Any] = {}
        for step in STEPS:
            key = checkpoint_key(req, step)
            done = self.store.get(key)
            if done is not None:
                # A previous worker already did this step (and may have died
                # before finishing the run). Reuse its result, do not redo it.
                state[step] = done
                continue
            result = self._step(req, state, step)
            # Checkpoint *after* the step returns: the other order would record
            # work that never happened and lose the refund on a crash.
            self.store.put(key, result)
            state[step] = result
        return {"status": "done", **state}
