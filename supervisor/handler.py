"""Layer 2 — the supervisor (docs/07).

It observes everything and can change exactly one thing: it can stop the agent.
It cannot start it, cannot edit anything the agent owns, and never speaks to the
agent — the agent has no idea it exists.

The order here matters. Evidence is gathered deterministically, the event is
pre-classified by rule, the pause happens *before* the model is consulted, and
only then is the model asked to describe what happened for the human. A
supervisor whose model is down still pauses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ulid import ULID

from core import logging as jlog
from core.clock import SYSTEM_CLOCK, Clock, utc_now_iso
from core.events import put_event
from core.storage.base import StageResources
from core.storage.state import StateStore
from supervisor.rubric import METRIC_ONLY, PreClassification, pre_classify, reconcile

_log = jlog.get_logger("supervisor")

#: An evidence pack larger than this is truncated: the model gets less useful
#: past a point, and the cost is real.
MAX_EVIDENCE_BYTES = 16 * 1024


@dataclass
class Journal:
    """One decision, as written to `supervisor_journal`."""

    sk: str
    event_type: str
    trace_id: str
    level: int
    action: str
    summary: str
    probable_cause: str
    evidence: dict[str, Any] = field(default_factory=dict)
    preclassified: bool = False
    raw_model_output: str | None = None


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return Supervisor().handle(event)


class Supervisor:
    def __init__(
        self,
        *,
        resources: StageResources | None = None,
        state: StateStore | None = None,
        model: Any = None,
        telegram: Any = None,
        lambda_client: Any = None,
        events_client: Any = None,
        dynamodb: Any = None,
        clock: Clock = SYSTEM_CLOCK,
        ops_chat_id: str = "",
    ) -> None:
        self._resources = resources
        self._state = state
        self._model = model
        self._telegram = telegram
        self._lambda = lambda_client
        self._events = events_client
        self._dynamodb = dynamodb
        self.clock = clock
        self._ops_chat_id = ops_chat_id

    # ------------------------------------------------------------------ deps
    @property
    def resources(self) -> StageResources:
        if self._resources is None:
            self._resources = StageResources.from_env()
        return self._resources

    @property
    def state(self) -> StateStore:
        if self._state is None:
            self._state = StateStore(self.resources, clock=self.clock)
        return self._state

    @property
    def dynamodb(self) -> Any:
        if self._dynamodb is None:
            import boto3

            self._dynamodb = boto3.client("dynamodb")
        return self._dynamodb

    # ---------------------------------------------------------------- handle
    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        decisions = []
        for record in event.get("Records", [{"body": json.dumps(event)}]):
            decisions.append(self._one(json.loads(record["body"])))
        return {"decisions": decisions}

    def _one(self, envelope: dict[str, Any]) -> dict[str, Any]:
        event_type = str(envelope.get("detail-type") or envelope.get("detail_type") or "")
        detail = envelope.get("detail") or envelope
        trace_id = str(detail.get("trace_id", ""))
        jlog.set_trace_id(trace_id)

        if event_type in METRIC_ONLY and not _is_anomalous(event_type, detail):
            return {"event_type": event_type, "level": 0, "action": "skipped"}

        same_type = self.count_same_type_24h(event_type)
        pre = pre_classify(event_type, detail, same_type)

        # The pause happens here, before the model. This is what makes ADR-13
        # true rather than aspirational.
        if pre.level == 2 and pre.decided:
            self.pause(reason=f"{event_type}: {pre.reason}")

        evidence = self.gather_evidence(event_type, detail, same_type)
        model_level, model_action, summary, cause, raw = self.ask_model(
            event_type, detail, evidence, pre
        )
        level, action, note = reconcile(pre, model_level, model_action)

        if action == "pause" and not (pre.level == 2 and pre.decided):
            self.pause(reason=f"{event_type}: {summary}")

        journal = Journal(
            sk=f"{utc_now_iso(self.clock)}#{ULID()}",
            event_type=event_type,
            trace_id=trace_id,
            level=level,
            action=action,
            summary=summary or pre.reason or event_type,
            probable_cause=" ".join(x for x in (cause, note) if x).strip(),
            evidence=evidence,
            preclassified=pre.decided,
            raw_model_output=raw,
        )
        self.write_journal(journal)

        if level >= 1:
            self.notify_ops(journal)

        self.emit_decision(journal)
        jlog.info(
            _log,
            "decision",
            event_type=event_type,
            level=level,
            action=action,
            preclassified=pre.decided,
        )
        return {"event_type": event_type, "level": level, "action": action, "sk": journal.sk}

    # -------------------------------------------------------------- evidence
    def count_same_type_24h(self, event_type: str) -> int:
        """From the journal's GSI: how often this has already happened today."""
        import datetime as dt

        since = (self.clock.now() - dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        try:
            response = self.dynamodb.query(
                TableName=self.resources.table("supervisor_journal"),
                IndexName="event_type-sk-index",
                KeyConditionExpression="event_type = :t AND SK >= :s",
                ExpressionAttributeValues={":t": {"S": event_type}, ":s": {"S": since}},
                Select="COUNT",
            )
            return int(response.get("Count", 0))
        except Exception as exc:
            jlog.warning(_log, "count_failed", error=str(exc))
            return 0

    def gather_evidence(
        self, event_type: str, detail: dict[str, Any], same_type_24h: int
    ) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "event": _truncate(detail),
            "same_type_24h": same_type_24h,
            "paused": self.state.pause_state().paused,
            "counters": self.state.counters(),
        }
        if event_type.startswith(("heartbeat.", "conversation.")):
            try:
                from core.storage.diary import DiaryStore

                diary = DiaryStore(self.resources, clock=self.clock, dynamodb=self.dynamodb)
                evidence["last_diary"] = diary.last(1)
            except Exception as exc:
                evidence["last_diary_error"] = str(exc)
        return evidence

    # ----------------------------------------------------------------- model
    def ask_model(
        self,
        event_type: str,
        detail: dict[str, Any],
        evidence: dict[str, Any],
        pre: PreClassification,
    ) -> tuple[int | None, str | None, str, str, str | None]:
        """Ask for a structured decision. Returns (level, action, summary, cause, raw)."""
        if self._model is None:
            return None, None, pre.reason or event_type, "", None

        from core.contracts import load_schema
        from core.model_client import SystemBlock
        from supervisor.rubric import rubric_text

        prompt = _load_prompt()
        instruction = (
            "This event was already classified as Level 2 by rule. Describe only "
            "what happened and its probable cause."
            if pre.decided and pre.level == 2
            else "Classify this event using the rubric."
        )
        response = self._model.invoke(
            role="supervisor",
            system=[
                SystemBlock(text=f"{prompt}\n\n{rubric_text()}", stable=True),
                SystemBlock(text=instruction, stable=False),
            ],
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(
                        {"event_type": event_type, "evidence": evidence}, default=str
                    ),
                }
            ],
            max_tokens=1200,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "decision",
                    "strict": True,
                    "schema": load_schema("supervisor-decision"),
                },
            },
        )
        raw = response.content or ""
        if response.error or response.finish_reason != "stop":
            return None, None, pre.reason or event_type, "", raw or None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None, None, pre.reason or event_type, "", raw
        return (
            parsed.get("level"),
            parsed.get("action"),
            str(parsed.get("summary", ""))[:2000],
            str(parsed.get("probable_cause", ""))[:2000],
            raw,
        )

    # ---------------------------------------------------------------- action
    def pause(self, *, reason: str) -> None:
        """Disable the governor rules, zero the agent concurrency, set PAUSE.

        All three, and each independently: any one of them alone stops the
        agent, and doing all three means a single AWS API failure does not
        leave it half-paused.
        """
        self.state.set_pause(paused=True, by="supervisor", reason=reason[:500])
        stage = self.resources.stage
        for rule in (f"{stage}-governor-rate", f"{stage}-governor-weekly"):
            try:
                self._events_client().disable_rule(Name=rule)
            except Exception as exc:
                jlog.error(_log, "disable_rule_failed", rule=rule, error=str(exc))
        for function in (f"{stage}-agent-conversation", f"{stage}-agent-heartbeat"):
            try:
                self._lambda_client().put_function_concurrency(
                    FunctionName=function, ReservedConcurrentExecutions=0
                )
            except Exception as exc:
                jlog.error(_log, "concurrency_failed", function=function, error=str(exc))
        jlog.warning(_log, "agent_paused", reason=reason[:200])

    def write_journal(self, journal: Journal) -> None:
        item: dict[str, Any] = {
            "PK": {"S": "JOURNAL"},
            "SK": {"S": journal.sk},
            "event_type": {"S": journal.event_type or "unknown"},
            "trace_id": {"S": journal.trace_id or "-"},
            "level": {"N": str(journal.level)},
            "action": {"S": journal.action},
            "summary": {"S": journal.summary[:2000] or "-"},
            "probable_cause": {"S": journal.probable_cause[:2000] or "-"},
            "evidence": {"S": json.dumps(journal.evidence, default=str)[:20000]},
            "preclassified": {"BOOL": journal.preclassified},
        }
        if journal.raw_model_output:
            item["raw_model_output"] = {"S": journal.raw_model_output[:8000]}
        self.dynamodb.put_item(TableName=self.resources.table("supervisor_journal"), Item=item)

    def notify_ops(self, journal: Journal) -> None:
        if self._telegram is None:
            return
        from gates.common.telegram import ops_message

        title = {1: "Something needs your attention", 2: "The agent has been paused"}[journal.level]
        body = (
            f"{journal.event_type}\n\n{journal.summary}\n\n"
            f"Probable cause: {journal.probable_cause or 'unclear'}\n"
            f"Journal: {journal.sk}"
        )
        if journal.level == 2:
            body += "\n\nResume with: uv run scripts/resume.py --stage " + self.resources.stage
        try:
            self._telegram.send_message(
                self.ops_chat_id,
                ops_message(self.resources.stage, title, body),
                ops=True,
                stage=self.resources.stage,
            )
        except Exception as exc:
            jlog.error(_log, "ops_notify_failed", error=str(exc))

    def emit_decision(self, journal: Journal) -> None:
        try:
            put_event(
                "supervisor.decision",
                "agent.supervisor",
                {
                    "level": journal.level,
                    "action": journal.action,
                    "summary": journal.summary[:500],
                    "evidence": journal.sk,
                },
                stage=self.resources.stage,
                trace_id=journal.trace_id or None,
            )
        except Exception as exc:
            jlog.warning(_log, "emit_failed", error=str(exc))

    @property
    def ops_chat_id(self) -> str:
        if not self._ops_chat_id:
            from gates.common.parameters import get_parameter

            self._ops_chat_id = get_parameter(f"/agent/{self.resources.stage}/ops-chat-id")
        return self._ops_chat_id

    def _events_client(self) -> Any:
        if self._events is None:
            import boto3

            self._events = boto3.client("events")
        return self._events

    def _lambda_client(self) -> Any:
        if self._lambda is None:
            import boto3

            self._lambda = boto3.client("lambda")
        return self._lambda


def _is_anomalous(event_type: str, detail: dict[str, Any]) -> bool:
    """A metric-only event still matters when its numbers are alarming."""
    ops = detail.get("memory_ops") or {}
    total = sum(int(v) for v in ops.values() if isinstance(v, int))
    return total > 50


def _truncate(payload: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(payload, default=str)
    if len(encoded) <= MAX_EVIDENCE_BYTES:
        return payload
    return {"truncated": True, "head": encoded[:MAX_EVIDENCE_BYTES]}


def _load_prompt() -> str:
    from pathlib import Path

    path = Path(__file__).resolve().parent / "prompt.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return "You are the supervisor of an autonomous agent."
