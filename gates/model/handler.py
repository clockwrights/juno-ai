"""`gate-model` — the only place a model call happens (ADR-05, docs/06 §5).

It holds the API key, chooses the model, meters the tokens, and writes a session
record before any response is returned. None of that is reachable from agent
code: this Lambda lives in `gates/`, which no proposal may touch, and the caller
is identified by IAM and by the alias it invoked, never by a field it sent.

Errors are returned, not raised (docs/03 §2.3), so the agent always gets a
structured answer it can reason about instead of a crash it cannot.
"""

from __future__ import annotations

import os
import time
from typing import Any

from core import logging as jlog
from core.clock import SYSTEM_CLOCK
from core.contracts import ContractError, validate
from core.storage.base import CANARY, LIVE, StageResources
from core.storage.sessions import SessionStore
from core.storage.state import BudgetExhaustedError, StateStore
from gates.model.policy import (
    FALLBACK_MODEL,
    FORBIDDEN_REQUEST_FIELDS,
    MAX_TOKENS_CAP,
    alias_from_arn,
    counter_name,
    policy_for,
    role_allowed_on_alias,
)
from gates.model.provider import Provider, TogetherProvider

_log = jlog.get_logger("gate-model")

#: Cached input costs about 20% of fresh input on Together, so it counts as 20%
#: against the budget. Charging it in full would make caching pointless; not
#: charging it at all would let a huge cached prefix run free.
CACHED_TOKEN_WEIGHT = 0.2


def error(kind: str, message: str, *, retry_after: int = 0) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "error": {"type": kind, "message": message, "retry_after_seconds": retry_after},
    }


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return Gateway().handle(event, context)


class Gateway:
    def __init__(
        self,
        *,
        resources: StageResources | None = None,
        provider: Provider | None = None,
        state: StateStore | None = None,
        sessions: SessionStore | None = None,
        clock: Any = SYSTEM_CLOCK,
    ) -> None:
        self._resources = resources
        self._provider = provider
        self._state = state
        self._sessions = sessions
        self.clock = clock

    # ------------------------------------------------------------------ deps
    @property
    def resources(self) -> StageResources:
        if self._resources is None:
            self._resources = StageResources.from_env()
        return self._resources

    @property
    def provider(self) -> Provider:
        if self._provider is None:
            self._provider = TogetherProvider(
                api_key_parameter=os.environ.get(
                    "API_KEY_PARAMETER", f"/agent/{self.resources.stage}/together-api-key"
                )
            )
        return self._provider

    def state(self, namespace: str) -> StateStore:
        if self._state is not None:
            return self._state
        return StateStore(self.resources, namespace=namespace, clock=self.clock)

    def sessions(self, namespace: str) -> SessionStore:
        if self._sessions is not None:
            return self._sessions
        return SessionStore(self.resources, namespace=namespace, clock=self.clock)

    # ---------------------------------------------------------------- handle
    def handle(self, event: dict[str, Any], context: Any) -> dict[str, Any]:
        trace_id = str(event.get("trace_id") or "")
        jlog.set_trace_id(trace_id)

        # 1. Shape. A caller that sends `model` is trying to choose one.
        for field in FORBIDDEN_REQUEST_FIELDS:
            if field in event:
                return error("invalid_request", f"callers may not send {field!r}")

        role = str(event.get("role") or "")
        try:
            policy = policy_for(role)
        except ValueError as exc:
            return error("invalid_request", str(exc))

        # Embeddings share the gateway because it holds the only copy of the
        # provider key (ADR-03, ADR-29), but they are not a chat call: no system
        # prompt, no messages, no tools, and a different response shape — hence
        # a contract of their own, chosen here before validation.
        contract = "embed-request" if role == "embed" else "model-request"
        try:
            validate(contract, event)
        except ContractError as exc:
            return error("invalid_request", str(exc))

        # 2. Alias. IAM already refused the wrong caller; this refuses a caller
        # that reached the right function through the wrong door.
        alias = alias_from_arn(getattr(context, "invoked_function_arn", "") or "")
        if not role_allowed_on_alias(role, alias):
            jlog.warning(_log, "role_not_allowed", role=role, alias=alias)
            return error("role_not_allowed", f"role {role!r} may not be called on alias {alias!r}")

        if role == "embed":
            return self._embed(event, policy, trace_id)

        namespace = CANARY if role == "canary" else LIVE
        state = self.state(namespace)
        sessions = self.sessions(namespace)

        max_tokens = min(int(event.get("max_tokens", 8000)), MAX_TOKENS_CAP)
        system_blocks = list(event["system"])
        messages = list(event["messages"])

        # 3. Budget, before the call. The estimate deliberately leads the true
        # count (ADR-28): a budget may be reached early, never overrun silently.
        estimate = estimate_tokens(system_blocks, messages) + max_tokens
        counter = counter_name(role)
        try:
            state.spend(counter, estimate, policy.daily_token_budget)
        except BudgetExhaustedError:
            jlog.warning(_log, "budget_exhausted", role=role, budget=policy.daily_token_budget)
            self._emit("model.budget_exhausted", {"role": role})
            return error(
                "budget_exhausted",
                f"the daily token budget for {role!r} is spent",
                retry_after=seconds_until_utc_midnight(self.clock),
            )

        # 4. Call.
        started = time.monotonic()
        request = self.provider.build_request(
            model=policy.model,
            system=system_blocks,
            messages=messages,
            tools=event.get("tools"),
            max_tokens=max_tokens,
            response_format=event.get("response_format"),
            reasoning=policy.reasoning,
        )
        try:
            payload = self.provider.complete(request)
            # One retry against a sibling model on a refusal; there is no
            # server-side fallback on this provider (ADR-28).
            if payload.get("finish_reason") == "content_filter":
                jlog.warning(_log, "content_filter_retry", role=role, model=policy.model)
                payload = self.provider.complete({**request, "model": FALLBACK_MODEL})
        except Exception as exc:
            latency = int((time.monotonic() - started) * 1000)
            jlog.error(_log, "upstream_error", role=role, error=str(exc))
            self._emit("model.upstream_error", {"role": role, "message": str(exc)[:200]})
            self._record(
                sessions,
                trace_id,
                role,
                policy.model,
                request,
                None,
                latency,
                {"type": "upstream_error", "message": str(exc)[:500]},
            )
            return error("upstream_error", str(exc)[:500])

        latency_ms = int((time.monotonic() - started) * 1000)

        # 5. Reconcile the estimate against what was actually used.
        usage = payload.get("usage") or {}
        actual = weighted_tokens(usage)
        drift = actual - estimate
        if drift > 0:
            try:
                state.spend(counter, drift, policy.daily_token_budget)
            except BudgetExhaustedError:
                # The call already happened; the budget is simply now spent, and
                # the next call is the one that gets refused.
                jlog.warning(_log, "budget_overshoot", role=role, drift=drift)

        # 6. Log, without content.
        jlog.info(
            _log,
            "model_call",
            role=role,
            model=payload.get("model") or policy.model,
            finish_reason=payload.get("finish_reason"),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            latency_ms=latency_ms,
            tools=[c.get("function", {}).get("name") for c in payload.get("tool_calls") or []],
        )

        # 7. Record, before returning. A failure here fails the call: an
        # unrecorded model call is worse than no model call at all.
        self._record(sessions, trace_id, role, policy.model, request, payload, latency_ms, None)

        remaining = state.remaining(counter, policy.daily_token_budget)
        return {
            "schema_version": "1",
            "trace_id": trace_id,
            **payload,
            "gateway": {
                "model_used": payload.get("model") or policy.model,
                "tokens_remaining_today": remaining,
                "latency_ms": latency_ms,
            },
        }

    # ---------------------------------------------------------------- embed
    def _embed(self, event: dict[str, Any], policy: Any, trace_id: str) -> dict[str, Any]:
        """One embedding call. Metered, but not recorded as a session.

        Session records exist so the owner can read what the agent thought
        (ADR-24). An embedding has no thinking in it — recording one per memory
        save would bury the readable sessions in noise for no benefit.
        """
        texts: list[str] = list(event["texts"])
        state = self.state(LIVE)
        counter = counter_name("embed")

        # Embeddings are charged per input token, so the same conservative
        # local estimate applies (ADR-28); there is no output.
        estimate = max(1, sum(len(t) for t in texts) // 3)
        try:
            state.spend(counter, estimate, policy.daily_token_budget)
        except BudgetExhaustedError:
            jlog.warning(_log, "embed_budget_exhausted", texts=len(texts))
            self._emit("model.budget_exhausted", {"role": "embed"})
            return error(
                "budget_exhausted",
                "the daily embedding budget is spent",
                retry_after=seconds_until_utc_midnight(self.clock),
            )

        started = time.monotonic()
        try:
            vectors = self.provider.embed(texts)
        except Exception as exc:  # upstream errors are returned, not raised
            jlog.error(_log, "embed_upstream_error", error=str(exc))
            self._emit("model.upstream_error", {"role": "embed", "message": str(exc)[:200]})
            return error("upstream_error", str(exc)[:500])

        latency_ms = int((time.monotonic() - started) * 1000)
        jlog.info(
            _log,
            "embed_call",
            model=policy.model,
            texts=len(texts),
            dimension=len(vectors[0]) if vectors else 0,
            latency_ms=latency_ms,
        )
        return {
            "schema_version": "1",
            "trace_id": trace_id,
            "embeddings": vectors,
            "model": policy.model,
            "dimension": len(vectors[0]) if vectors else 0,
            "gateway": {
                "model_used": policy.model,
                "tokens_remaining_today": state.remaining(counter, policy.daily_token_budget),
                "latency_ms": latency_ms,
            },
        }

    # --------------------------------------------------------------- helpers
    def _record(
        self,
        sessions: SessionStore,
        trace_id: str,
        role: str,
        model: str,
        request: dict[str, Any],
        payload: dict[str, Any] | None,
        latency_ms: int,
        err: dict[str, Any] | None,
    ) -> None:
        sessions.write_record(
            trace_id=trace_id,
            seq=next_seq(sessions, trace_id),
            kind=role,
            role=role,
            model=model,
            request=request,
            response=payload,
            latency_ms=latency_ms,
            error=err,
        )

    def _emit(self, detail_type: str, detail: dict[str, Any]) -> None:
        from core.events import put_event

        try:
            put_event(detail_type, "agent.gate", detail, stage=self.resources.stage)
        except Exception:
            pass


#: Session sequence numbers are per trace. Counting existing objects would cost
#: a list call per model call, so the gateway keeps an in-process counter and
#: falls back to zero on a cold start; collisions only cost an overwritten
#: record of the same call.
_SEQ: dict[str, int] = {}


def next_seq(sessions: SessionStore, trace_id: str) -> int:
    seq = _SEQ.get(trace_id, 0)
    _SEQ[trace_id] = seq + 1
    if len(_SEQ) > 512:
        _SEQ.clear()
    return seq


def estimate_tokens(system: list[dict[str, Any]], messages: list[dict[str, Any]]) -> int:
    """A deliberately pessimistic local estimate.

    Together exposes no token-counting endpoint (ADR-28), and importing a
    tokeniser would tie the gate to a vocabulary it would have to keep in sync
    with the provider. Three characters per token over-counts for English prose,
    which is the safe direction for a budget.
    """
    characters = sum(len(str(block.get("text", ""))) for block in system)
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            characters += len(content)
        else:
            characters += len(str(content))
    return max(1, characters // 3)


def weighted_tokens(usage: dict[str, Any]) -> int:
    """What a call costs the budget, discounting cached input."""
    prompt = int(usage.get("prompt_tokens", 0))
    completion = int(usage.get("completion_tokens", 0))
    cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
    fresh = max(0, prompt - cached)
    return int(fresh + cached * CACHED_TOKEN_WEIGHT + completion)


def seconds_until_utc_midnight(clock: Any) -> int:
    import datetime as dt

    now = clock.now()
    midnight = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((midnight - now).total_seconds()))
