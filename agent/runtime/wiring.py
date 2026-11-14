"""Assembling the stores, tools and model client for one invocation.

Both handlers need the same set of objects built the same way, and tests need
to substitute any of them. Keeping the construction in one place means a test
that swaps the model client does not also have to know how a store is built.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from agent.runtime.prompts import load_prompt
from agent.tools.implementations import ToolContext, build_handlers
from core.clock import SYSTEM_CLOCK, Clock, utc_now_iso
from core.model_client import ModelClient, ModelClientProtocol
from core.storage.base import CANARY, LIVE, StageResources
from core.storage.conversations import ConversationStore
from core.storage.diary import DiaryStore
from core.storage.embeddings import GatewayEmbedder
from core.storage.memory import MemoryStore
from core.storage.profile import ProfileStore
from core.storage.proposals import ProposalStore
from core.storage.state import StateStore
from core.storage.vectors import S3VectorsIndex, VectorIndex


@dataclass
class Wiring:
    stage: str
    namespace: str
    tools: ToolContext
    handlers: dict[str, Any]
    model: ModelClientProtocol
    tools_prompt: str
    now_iso: str


def build_wiring(
    event: dict[str, Any],
    *,
    resources: StageResources | None = None,
    clock: Clock = SYSTEM_CLOCK,
    model: ModelClientProtocol | None = None,
    index: VectorIndex | None = None,
    embed: Any | None = None,
    dynamodb: Any = None,
    s3: Any = None,
    sqs: Any = None,
    lambda_client: Any = None,
) -> Wiring:
    resources = resources or StageResources.from_env()
    namespace = CANARY if event.get("mode") == "canary" else LIVE
    stage = resources.stage

    store_kwargs: dict[str, Any] = {
        "namespace": namespace,
        "clock": clock,
        "dynamodb": dynamodb,
        "s3": s3,
    }
    index = index or S3VectorsIndex(resources)
    embed = embed or _gateway_embed(stage, lambda_client, str(event.get("trace_id", "")))

    memory = MemoryStore(resources, index=index, embed=embed, **store_kwargs)
    tool_ctx = ToolContext(
        memory=memory,
        diary=DiaryStore(resources, **store_kwargs),
        state=StateStore(resources, **store_kwargs),
        profile=ProfileStore(resources, **store_kwargs),
        conversations=ConversationStore(resources, **store_kwargs),
        proposals=ProposalStore(resources, **store_kwargs),
        bucket=resources.bucket,
        s3=memory.s3,
        sqs=sqs,
        notifications_queue=os.environ.get("NOTIFICATIONS_QUEUE_URL", ""),
        sandbox=lambda_client,
        sandbox_arn=os.environ.get("SANDBOX_ARN", ""),
        mode=str(event.get("mode", "free")),
        namespace=namespace,
        source=_source(event),
        invocation_id=str(event.get("invocation_id", "")),
        trace_id=str(event.get("trace_id", "")),
        may_write_artifacts=False,
    )
    tool_ctx.flags = tool_ctx.state.flags()

    return Wiring(
        stage=stage,
        namespace=namespace,
        tools=tool_ctx,
        handlers=build_handlers(tool_ctx),
        model=model
        or ModelClient(
            stage=stage,
            gate_arn=os.environ.get("GATE_MODEL_ARN", ""),
            lambda_client=lambda_client,
        ),
        tools_prompt=load_prompt("tools.md"),
        now_iso=utc_now_iso(clock),
    )


def _source(event: dict[str, Any]) -> str:
    mode = event.get("mode")
    if mode == "canary":
        return "canary"
    if mode == "maintenance":
        return "maintenance"
    if mode in ("free",):
        return "heartbeat"
    return "conversation"


def _gateway_embed(stage: str, lambda_client: Any = None, trace_id: str = "") -> Any:
    """Embeddings go through the gateway, which holds the key (ADR-29)."""
    return GatewayEmbedder(
        stage=stage,
        gate_arn=os.environ.get("GATE_MODEL_ARN", ""),
        lambda_client=lambda_client,
        trace_id=trace_id,
    )
