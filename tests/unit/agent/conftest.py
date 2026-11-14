"""A fully wired agent against fakes.

The point of this fixture is that the code under test is the production loop,
the production tools and the production stores — only the model and AWS are
fake (docs/12 §2).
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.runtime.prompts import load_prompt
from agent.runtime.wiring import Wiring
from agent.tools.implementations import ToolContext, build_handlers
from core.model_client import FakeModelClient
from core.storage.conversations import ConversationStore
from core.storage.diary import DiaryStore
from core.storage.memory import MemoryStore
from core.storage.profile import ProfileStore
from core.storage.proposals import ProposalStore
from core.storage.state import StateStore


class FakeSqs:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send_message(self, **kwargs: Any) -> dict[str, Any]:
        self.sent.append(kwargs)
        return {"MessageId": "m1"}


class FakeLambda:
    """Stands in for the sandbox invoke."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or {
            "stdout": "42\n",
            "stderr": "",
            "exit_code": 0,
            "duration_ms": 5,
            "truncated": False,
            "killed": False,
        }
        self.invocations: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        import io
        import json

        self.invocations.append(kwargs)
        return {"Payload": io.BytesIO(json.dumps(self.payload).encode("utf-8"))}


def make_wiring(
    aws: dict[str, Any],
    resources: Any,
    clock: Any,
    index: Any,
    embed: Any,
    *,
    responses: list[dict[str, Any]] | None = None,
    mode: str = "conversation",
    namespace: str = "live",
    flags: dict[str, bool] | None = None,
) -> Wiring:
    store_kwargs: dict[str, Any] = {
        "namespace": namespace,
        "clock": clock,
        "dynamodb": aws["dynamodb"],
        "s3": aws["s3"],
    }
    memory = MemoryStore(resources, index=index, embed=embed, **store_kwargs)
    state = StateStore(resources, **store_kwargs)

    ctx = ToolContext(
        memory=memory,
        diary=DiaryStore(resources, **store_kwargs),
        state=state,
        profile=ProfileStore(resources, **store_kwargs),
        conversations=ConversationStore(resources, **store_kwargs),
        proposals=ProposalStore(resources, **store_kwargs),
        bucket=resources.bucket,
        s3=aws["s3"],
        sqs=FakeSqs(),
        notifications_queue="https://sqs.example/notifications",
        sandbox=FakeLambda(),
        sandbox_arn="arn:aws:lambda:eu-central-1:0:function:test-sandbox-executor",
        mode=mode,
        namespace=namespace,
        source="conversation" if mode == "conversation" else "heartbeat",
        invocation_id="01J0000000000000000000000A",
        trace_id="trace-1",
    )
    ctx.flags = {**state.flags(), **(flags or {})}

    return Wiring(
        stage=resources.stage,
        namespace=namespace,
        tools=ctx,
        handlers=build_handlers(ctx),
        model=FakeModelClient(responses or []),
        tools_prompt=load_prompt("tools.md"),
        now_iso="2026-09-06T12:00:00.000Z",
    )


@pytest.fixture
def wiring(aws: dict[str, Any], resources: Any, clock: Any, index: Any, embed: Any) -> Any:
    def _make(**kwargs: Any) -> Wiring:
        return make_wiring(aws, resources, clock, index, embed, **kwargs)

    return _make
