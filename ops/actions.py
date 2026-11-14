"""Admin actions, shared by `scripts/` and the control panel (ADR-25).

One implementation, two surfaces. Every action here writes a journal entry with
`by = human:<iam identity>`, so the record of what happened to the agent is the
same whether the owner used the CLI or the panel.

Resume is the asymmetric one: pausing is something three different things can
do, and un-pausing is something only a person with the owner's credentials can.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from ulid import ULID

from ops.admin import AdminSession, Stage, StageRefusalError

AGENT_FUNCTIONS = ("agent-conversation", "agent-heartbeat")
GOVERNOR_RULES = ("governor-rate", "governor-weekly")

#: Reserved concurrency when running normally (infra/stacks/common.py).
NORMAL_CONCURRENCY = {"agent-conversation": 2, "agent-heartbeat": 1}


@dataclass
class ActionResult:
    action: str
    details: list[str]
    journal_sk: str = ""

    def render(self) -> str:
        return "\n".join([f"{self.action}:", *(f"  {d}" for d in self.details)])


def pause(session: AdminSession, stage: Stage, *, reason: str) -> ActionResult:
    """Disable the rules, zero the concurrency, set the pause item."""
    details = []
    for rule in GOVERNOR_RULES:
        try:
            session.client("events").disable_rule(Name=stage.resource(rule))
            details.append(f"disabled {stage.resource(rule)}")
        except Exception as exc:
            details.append(f"FAILED to disable {rule}: {exc}")

    for function in AGENT_FUNCTIONS:
        try:
            session.client("lambda").put_function_concurrency(
                FunctionName=stage.resource(function), ReservedConcurrentExecutions=0
            )
            details.append(f"concurrency 0 on {stage.resource(function)}")
        except Exception as exc:
            details.append(f"FAILED to zero {function}: {exc}")

    _put_pause(session, stage, paused=True, by=session.caller, reason=reason)
    details.append("PAUSE#current set")

    sk = journal(
        session,
        stage,
        event_type="human.pause",
        level=2,
        action="pause",
        summary=f"Paused by {session.caller}: {reason}",
    )
    return ActionResult("pause", details, sk)


def resume(session: AdminSession, stage: Stage, *, ack: str | None = None) -> ActionResult:
    """Reverse a pause. Refuses while an unacknowledged Level 2 stands.

    The refusal is the point: an agent paused because something was wrong
    should not be restarted by someone who has not read why.
    """
    unacked = recent_level_two(session, stage, hours=1)
    if unacked and ack not in {entry["sk"] for entry in unacked}:
        raise StageRefusalError(
            "there is a supervisor Level 2 decision in the last hour that has not "
            "been acknowledged. Read it, then pass --ack <journal sk>:\n"
            + "\n".join(f"  {e['sk']}  {e['summary']}" for e in unacked)
        )

    details = []
    for rule in GOVERNOR_RULES:
        try:
            session.client("events").enable_rule(Name=stage.resource(rule))
            details.append(f"enabled {stage.resource(rule)}")
        except Exception as exc:
            details.append(f"FAILED to enable {rule}: {exc}")

    for function, concurrency in NORMAL_CONCURRENCY.items():
        try:
            session.client("lambda").put_function_concurrency(
                FunctionName=stage.resource(function),
                ReservedConcurrentExecutions=concurrency,
            )
            details.append(f"concurrency {concurrency} on {stage.resource(function)}")
        except Exception as exc:
            details.append(f"FAILED to restore {function}: {exc}")

    _put_pause(session, stage, paused=False, by=session.caller, reason="resumed")
    details.append("PAUSE#current cleared")

    sk = journal(
        session,
        stage,
        event_type="human.resume",
        level=1,
        action="notify",
        summary=f"Resumed by {session.caller}" + (f" (ack {ack})" if ack else ""),
    )
    return ActionResult("resume", details, sk)


def stop(session: AdminSession, stage: Stage, *, reason: str, telegram: Any = None) -> ActionResult:
    """Pause, and also remove the webhook so nothing inbound is accepted (ADR-27)."""
    result = pause(session, stage, reason=reason)
    try:
        client = telegram or _telegram(session, stage)
        client.delete_webhook()
        result.details.append("Telegram webhook deleted: no inbound is accepted")
    except Exception as exc:
        result.details.append(f"FAILED to delete the webhook: {exc}")
    return ActionResult("stop", result.details, result.journal_sk)


def start(
    session: AdminSession,
    stage: Stage,
    *,
    ack: str | None = None,
    webhook_url: str = "",
    telegram: Any = None,
) -> ActionResult:
    """Reverse `stop`, webhook included."""
    result = resume(session, stage, ack=ack)
    if webhook_url:
        try:
            client = telegram or _telegram(session, stage)
            secret = session.client("ssm").get_parameter(
                Name=f"{stage.parameter_prefix}webhook-secret", WithDecryption=True
            )["Parameter"]["Value"]
            client.set_webhook(webhook_url, secret)
            result.details.append(f"webhook set to {webhook_url}")
        except Exception as exc:
            result.details.append(f"FAILED to set the webhook: {exc}")
    return ActionResult("start", result.details, result.journal_sk)


def set_flag(session: AdminSession, stage: Stage, name: str, enabled: bool) -> ActionResult:
    from core.contracts import flag_defaults

    if name not in flag_defaults():
        raise StageRefusalError(f"unknown flag {name!r}")
    session.client("dynamodb").put_item(
        TableName=stage.resource("agent_state"),
        Item={
            "PK": {"S": "FLAG"},
            "SK": {"S": name},
            "enabled": {"BOOL": enabled},
            "set_by": {"S": session.caller},
            "at": {"S": _now()},
        },
    )
    sk = journal(
        session,
        stage,
        event_type="human.flag",
        level=1,
        action="notify",
        summary=f"{name} set to {enabled} by {session.caller}",
    )
    return ActionResult("flag", [f"{name} = {enabled}"], sk)


def list_flags(session: AdminSession, stage: Stage) -> dict[str, bool]:
    from core.contracts import flag_defaults

    flags = flag_defaults()
    response = session.client("dynamodb").query(
        TableName=stage.resource("agent_state"),
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": "FLAG"}},
    )
    for item in response.get("Items", []):
        name = item["SK"]["S"]
        if name in flags:
            flags[name] = bool(item.get("enabled", {}).get("BOOL", False))
    return flags


def recent_level_two(
    session: AdminSession, stage: Stage, *, hours: int = 1
) -> list[dict[str, str]]:
    since = (dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    try:
        response = session.client("dynamodb").query(
            TableName=stage.resource("supervisor_journal"),
            KeyConditionExpression="PK = :pk AND SK >= :s",
            ExpressionAttributeValues={":pk": {"S": "JOURNAL"}, ":s": {"S": since}},
        )
    except Exception:
        return []
    return [
        {"sk": i["SK"]["S"], "summary": i.get("summary", {}).get("S", "")}
        for i in response.get("Items", [])
        if i.get("level", {}).get("N") == "2" and i.get("event_type", {}).get("S") != "human.pause"
    ]


def journal(
    session: AdminSession,
    stage: Stage,
    *,
    event_type: str,
    level: int,
    action: str,
    summary: str,
) -> str:
    """Every human action is journaled next to the supervisor's own (docs/14 §1)."""
    sk = f"{_now()}#{ULID()}"
    session.client("dynamodb").put_item(
        TableName=stage.resource("supervisor_journal"),
        Item={
            "PK": {"S": "JOURNAL"},
            "SK": {"S": sk},
            "event_type": {"S": event_type},
            "trace_id": {"S": "-"},
            "level": {"N": str(level)},
            "action": {"S": action},
            "summary": {"S": summary[:2000]},
            "probable_cause": {"S": "-"},
            "by": {"S": session.caller},
            "preclassified": {"BOOL": False},
        },
    )
    return sk


def _put_pause(session: AdminSession, stage: Stage, *, paused: bool, by: str, reason: str) -> None:
    session.client("dynamodb").put_item(
        TableName=stage.resource("agent_state"),
        Item={
            "PK": {"S": "PAUSE"},
            "SK": {"S": "current"},
            "paused": {"BOOL": paused},
            "by": {"S": by},
            "reason": {"S": reason[:500]},
            "at": {"S": _now()},
        },
    )


def _telegram(session: AdminSession, stage: Stage) -> Any:
    from gates.common.telegram import TelegramClient

    token = session.client("ssm").get_parameter(
        Name=f"{stage.parameter_prefix}telegram-agent-bot", WithDecryption=True
    )["Parameter"]["Value"]
    return TelegramClient(token=token)


def _now() -> str:
    return dt.datetime.now(tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
