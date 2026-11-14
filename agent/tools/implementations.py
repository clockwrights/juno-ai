"""Tool implementations, bound to the stores for one invocation.

Every tool here is a thin translation between the frozen contract shape and a
store method. The interesting decisions — what may be written where, what a
canary run may do, what a flag turns off — are made in `core/tools/registry.py`
and in the stores, not here, so this file stays boring on purpose.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from core.storage.conversations import ConversationStore
from core.storage.diary import DiaryStore
from core.storage.memory import MemoryStore
from core.storage.profile import ProfileConflict, ProfileStore
from core.storage.proposals import ProposalStore
from core.storage.state import StateStore
from core.tools.registry import ToolError

#: Where the agent may write. `artifacts/` is added only for a conversation turn
#: in which the owner asked (docs/09 §2, the staging-rule invariant).
DRAFT_PREFIX = "drafts/"
ARTIFACT_PREFIX = "artifacts/"


@dataclass
class ToolContext:
    """Everything the tools need for one invocation."""

    memory: MemoryStore
    diary: DiaryStore
    state: StateStore
    profile: ProfileStore
    conversations: ConversationStore
    proposals: ProposalStore
    bucket: str
    s3: Any
    sqs: Any = None
    notifications_queue: str = ""
    sandbox: Any = None
    sandbox_arn: str = ""
    mode: str = "conversation"
    namespace: str = "live"
    source: str = "conversation"
    invocation_id: str = ""
    trace_id: str = ""
    #: Set for a conversation turn in which the owner asked for canonical work.
    may_write_artifacts: bool = False
    #: Read once per invocation (docs/03 §6).
    flags: dict[str, bool] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.flags is None:
            self.flags = {}


def build_handlers(ctx: ToolContext) -> dict[str, Any]:
    """Bind every tool to this invocation's context."""

    def memory_save(*, content: str, type: str, tags: list[str], importance: int) -> dict[str, Any]:
        saved = ctx.memory.save(
            content=content, type=type, tags=tags, importance=importance, source=ctx.source
        )
        return {"id": saved.id}

    def memory_search(
        *,
        query: str,
        type: str | None = None,
        tags: list[str] | None = None,
        top_k: int | None = None,
    ) -> dict[str, Any]:
        results = ctx.memory.search(query, type=type, tags=tags, top_k=top_k or 8)
        return {
            "results": [
                {
                    "id": m.id,
                    "type": m.type,
                    "content": m.content,
                    "tags": m.tags,
                    "importance": m.importance,
                    "created_at": m.created_at,
                    "last_accessed": m.last_accessed,
                    "score": round(m.relevance(ctx.memory.clock.now()), 4),
                }
                for m in results
            ]
        }

    def memory_update(*, id: str, content: str) -> dict[str, Any]:
        updated = ctx.memory.update(id, content)
        return {"id": updated.id, "version": updated.version}

    def memory_forget(*, id: str, reason: str) -> dict[str, Any]:
        ctx.memory.forget(id, reason)
        return {"ok": True}

    def profile_read() -> dict[str, Any]:
        read = ctx.profile.read()
        return {"content": read.content, "etag": read.etag}

    def profile_write(*, content: str, etag: str) -> dict[str, Any]:
        result = ctx.profile.write(content, etag)
        if isinstance(result, ProfileConflict):
            # Returned, not raised: the model is shown what is there now and
            # decides how to merge (ADR-17).
            return {
                "error": "conflict",
                "current_etag": result.current_etag,
                "content": result.content,
            }
        return {"etag": result.etag}

    def diary_read(*, last_n: int | None = None) -> dict[str, Any]:
        entries = ctx.diary.last(last_n or 5)
        return {"entries": [{"sk": e["sk"], "content": e["content"]} for e in entries]}

    def diary_write(*, content: str) -> dict[str, Any]:
        sk = ctx.diary.append(
            content,
            invocation_id=ctx.invocation_id or ctx.trace_id or "manual",
            mode=ctx.mode,
            trace_id=ctx.trace_id,
        )
        return {"sk": sk}

    def leave_note(*, text: str) -> dict[str, Any]:
        return {"note_id": ctx.state.leave_note(text, source=ctx.source)}

    def notify_user(*, text: str, priority: str) -> dict[str, Any]:
        # The agent only ever enqueues; gate-notify decides whether it is sent
        # and enforces the daily budget atomically (ADR-03).
        if ctx.sqs is None or not ctx.notifications_queue:
            raise ToolError("notifications are not wired in this invocation")
        from core.contracts import envelope

        body = {
            **envelope(ctx.memory.resources.stage, ctx.trace_id),
            "kind": "notify",
            "priority": priority,
            "text": text,
            "source": ctx.source if ctx.source in ("conversation", "heartbeat") else "heartbeat",
        }
        ctx.sqs.send_message(
            QueueUrl=ctx.notifications_queue, MessageBody=json.dumps(body, default=str)
        )
        return {
            "queued": True,
            "notifications_remaining_hint": ctx.state.remaining("notifications", 2),
        }

    def run_code(
        *, code: str, timeout_s: int | None = None, stdin: str | None = None
    ) -> dict[str, Any]:
        if ctx.sandbox is None or not ctx.sandbox_arn:
            raise ToolError("the sandbox is not wired in this invocation")
        from core.contracts import envelope

        payload = {
            **envelope(ctx.memory.resources.stage, ctx.trace_id),
            "language": "python",
            "code": code,
            "timeout_s": timeout_s or 30,
            "stdin": stdin,
        }
        response = ctx.sandbox.invoke(
            FunctionName=ctx.sandbox_arn,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8"),
        )
        result = json.loads(response["Payload"].read().decode("utf-8"))
        return {
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
            "exit_code": int(result.get("exit_code", 1)),
            "duration_ms": int(result.get("duration_ms", 0)),
            "truncated": bool(result.get("truncated", False)),
            "killed": bool(result.get("killed", False)),
        }

    def artifact_read(*, path: str) -> dict[str, Any]:
        key = _readable_key(path, ctx)
        from botocore.exceptions import ClientError

        try:
            obj = ctx.s3.get_object(Bucket=ctx.bucket, Key=key)
        except ClientError as exc:
            raise ToolError(f"{path} does not exist", kind="not_found") from exc
        return {
            "content": obj["Body"].read().decode("utf-8"),
            "etag": str(obj["ETag"]).strip('"'),
        }

    def artifact_write(*, path: str, content: str, etag: str | None = None) -> dict[str, Any]:
        key = _writable_key(path, ctx)
        if key is None:
            # The staging-rule invariant, enforced in code rather than by the
            # prompt alone: idle work stages to drafts/ (docs/08 §5).
            return {"error": "path_not_allowed"}
        params: dict[str, Any] = {
            "Bucket": ctx.bucket,
            "Key": key,
            "Body": content.encode("utf-8"),
            "ContentType": "text/markdown",
        }
        if etag:
            params["IfMatch"] = etag
        from botocore.exceptions import ClientError

        try:
            response = ctx.s3.put_object(**params)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("PreconditionFailed", "412"):
                return {"error": "conflict"}
            raise
        return {"etag": str(response["ETag"]).strip('"')}

    def artifact_list(*, prefix: str | None = None) -> dict[str, Any]:
        wanted = prefix or DRAFT_PREFIX
        if not wanted.startswith((DRAFT_PREFIX, ARTIFACT_PREFIX)):
            wanted = DRAFT_PREFIX + wanted.lstrip("/")
        listing = ctx.s3.list_objects_v2(Bucket=ctx.bucket, Prefix=_scoped(wanted, ctx))
        return {
            "paths": [obj["Key"].removeprefix("canary/") for obj in listing.get("Contents", [])]
        }

    def schedule_next_wake(*, minutes: int, reason: str) -> dict[str, Any]:
        ctx.state.set_wake_wish(minutes, reason)
        return {"recorded_minutes": minutes}

    def propose_change(*, title: str, rationale: str, diff: str) -> dict[str, Any]:
        if not ctx.flags.get("proposals_enabled", False):
            # The tool exists from VP4 and is disabled by flag, never stubbed
            # (docs/11 §1.3), so enabling it later changes no code.
            return {"error": "proposals_disabled"}
        paths = _paths_in_diff(diff)
        proposal_id = ctx.proposals.submit(
            title=title, rationale=rationale, diff=diff, target_paths=paths
        )
        return {"proposal_id": proposal_id}

    def list_proposals(*, status: str | None = None) -> dict[str, Any]:
        return {
            "proposals": [
                {
                    "proposal_id": p.proposal_id,
                    "title": p.title,
                    "status": p.status,
                    "created_at": p.created_at,
                }
                for p in ctx.proposals.list(status=status)
            ]
        }

    def proposal_status(*, proposal_id: str) -> dict[str, Any]:
        proposal = ctx.proposals.get(proposal_id)
        if proposal is None:
            raise ToolError(f"no proposal {proposal_id}", kind="not_found")
        return {
            "proposal_id": proposal.proposal_id,
            "title": proposal.title,
            "status": proposal.status,
            "created_at": proposal.created_at,
            "updated_at": proposal.updated_at,
            "pr_url": proposal.pr_url,
            "rejection_reason": proposal.rejection_reason,
        }

    return {
        "memory_save": memory_save,
        "memory_search": memory_search,
        "memory_update": memory_update,
        "memory_forget": memory_forget,
        "profile_read": profile_read,
        "profile_write": profile_write,
        "diary_read": diary_read,
        "diary_write": diary_write,
        "leave_note": leave_note,
        "notify_user": notify_user,
        "run_code": run_code,
        "artifact_read": artifact_read,
        "artifact_write": artifact_write,
        "artifact_list": artifact_list,
        "schedule_next_wake": schedule_next_wake,
        "propose_change": propose_change,
        "list_proposals": list_proposals,
        "proposal_status": proposal_status,
    }


def _scoped(key: str, ctx: ToolContext) -> str:
    """Canary artifacts live under `canary/` so a probe cannot touch real work."""
    return f"canary/{key}" if ctx.namespace == "canary" else key


def _readable_key(path: str, ctx: ToolContext) -> str:
    clean = path.lstrip("/")
    if not clean.startswith((DRAFT_PREFIX, ARTIFACT_PREFIX)):
        clean = DRAFT_PREFIX + clean
    return _scoped(clean, ctx)


def _writable_key(path: str, ctx: ToolContext) -> str | None:
    clean = path.lstrip("/")
    if clean.startswith(ARTIFACT_PREFIX):
        if not ctx.may_write_artifacts:
            return None
        return _scoped(clean, ctx)
    if not clean.startswith(DRAFT_PREFIX):
        clean = DRAFT_PREFIX + clean
    return _scoped(clean, ctx)


def _paths_in_diff(diff: str) -> list[str]:
    """Every file a unified diff touches, for the proposal record.

    `gate-proposal` parses the diff again with `unidiff` and enforces the
    allowlist; this is only for the record the agent writes.
    """
    paths: set[str] = set()
    for line in diff.splitlines():
        if line.startswith(("+++ ", "--- ")):
            target = line[4:].strip()
            if target in ("/dev/null", ""):
                continue
            paths.add(target.removeprefix("a/").removeprefix("b/"))
    return sorted(paths)
