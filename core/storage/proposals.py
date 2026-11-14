"""`agent_proposals`: the agent's requests to change itself.

The agent may only ever create a record in `submitted` status; every later
transition belongs to `gate-proposal` and the pipeline (docs/03 §7). That is
enforced twice — by a condition expression here and by IAM on the agent role —
because a proposal that could mark itself `approved` would defeat the entire
review mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError
from ulid import ULID

from core.clock import utc_now_iso
from core.storage.base import ConflictError, StoreBase

STATUSES = (
    "submitted",
    "rejected_path",
    "pr_open",
    "gate_failed",
    "approved",
    "closed",
    "deploying",
    "deployed",
    "rolled_back",
)

#: The only transitions the pipeline may make (docs/03 §7).
ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "submitted": ("rejected_path", "pr_open"),
    "pr_open": ("gate_failed", "approved", "closed"),
    "gate_failed": ("closed", "pr_open"),
    "approved": ("deploying",),
    "deploying": ("deployed", "rolled_back"),
}


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    title: str
    rationale: str
    status: str
    created_at: str
    updated_at: str = ""
    target_paths: tuple[str, ...] = ()
    diff_s3_key: str | None = None
    pr_url: str | None = None
    rejection_reason: str | None = None


class ProposalStore(StoreBase):
    @property
    def table(self) -> str:
        return self.resources.table("agent_proposals")

    def _pk(self, proposal_id: str) -> str:
        pid = proposal_id if proposal_id.startswith("PROPOSAL#") else f"PROPOSAL#{proposal_id}"
        return self.key(pid)

    def submit(self, *, title: str, rationale: str, diff: str, target_paths: list[str]) -> str:
        """Create-only. The diff goes to S3; the item carries the pointer."""
        proposal_id = f"PROPOSAL#{ULID()}"
        pk = self._pk(proposal_id)
        now = utc_now_iso(self.clock)
        diff_key = f"proposals/{proposal_id.replace('#', '_')}/diff.patch"

        self.s3.put_object(
            Bucket=self.resources.bucket,
            Key=diff_key,
            Body=diff.encode("utf-8"),
            ContentType="text/x-patch",
        )
        paths = sorted(set(target_paths))
        try:
            self.dynamodb.put_item(
                TableName=self.table,
                Item={
                    "PK": {"S": pk},
                    "SK": {"S": "META"},
                    "title": {"S": title[:200]},
                    "rationale": {"S": rationale},
                    "diff_s3_key": {"S": diff_key},
                    "target_paths": {"SS": paths} if paths else {"NULL": True},
                    "status": {"S": "submitted"},
                    "created_at": {"S": now},
                    "updated_at": {"S": now},
                    "namespace": {"S": self.namespace},
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ConflictError(f"{pk} already exists") from exc
            raise
        return pk

    def get(self, proposal_id: str) -> Proposal | None:
        response = self.dynamodb.get_item(
            TableName=self.table,
            Key={"PK": {"S": self._pk(proposal_id)}, "SK": {"S": "META"}},
        )
        item = response.get("Item")
        if not item:
            return None
        paths = item.get("target_paths", {})
        return Proposal(
            proposal_id=item["PK"]["S"],
            title=item["title"]["S"],
            rationale=item["rationale"]["S"],
            status=item["status"]["S"],
            created_at=item["created_at"]["S"],
            updated_at=item.get("updated_at", {}).get("S", ""),
            target_paths=tuple(paths.get("SS", [])) if "SS" in paths else (),
            diff_s3_key=item.get("diff_s3_key", {}).get("S"),
            pr_url=item.get("pr_url", {}).get("S"),
            rejection_reason=item.get("rejection_reason", {}).get("S"),
        )

    def diff(self, proposal_id: str) -> str:
        proposal = self.get(proposal_id)
        if proposal is None or not proposal.diff_s3_key:
            return ""
        obj = self.s3.get_object(Bucket=self.resources.bucket, Key=proposal.diff_s3_key)
        return str(obj["Body"].read().decode("utf-8"))

    def transition(
        self, proposal_id: str, *, to: str, by: str, detail: str = "", **fields: str
    ) -> None:
        """Move status, refusing a transition the state machine does not allow."""
        if to not in STATUSES:
            raise ValueError(f"unknown status {to!r}")
        current = self.get(proposal_id)
        if current is None:
            raise ConflictError(f"{proposal_id} does not exist")
        allowed = ALLOWED_TRANSITIONS.get(current.status, ())
        if to not in allowed:
            raise ConflictError(
                f"{current.status} -> {to} is not a legal transition (allowed: {allowed})"
            )

        pk = self._pk(proposal_id)
        now = utc_now_iso(self.clock)
        expression = "SET #s = :to, updated_at = :n"
        names = {"#s": "status"}
        values: dict[str, Any] = {":to": {"S": to}, ":n": {"S": now}}
        for i, (key, value) in enumerate(fields.items()):
            expression += f", #f{i} = :f{i}"
            names[f"#f{i}"] = key
            values[f":f{i}"] = {"S": value}

        self.dynamodb.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": self.table,
                        "Key": {"PK": {"S": pk}, "SK": {"S": "META"}},
                        "UpdateExpression": expression,
                        "ExpressionAttributeNames": names,
                        "ExpressionAttributeValues": values,
                    }
                },
                # Append-only history, so a proposal's path is auditable even if
                # the final status says little about how it got there.
                {
                    "Put": {
                        "TableName": self.table,
                        "Item": {
                            "PK": {"S": pk},
                            "SK": {"S": f"EVENT#{now}"},
                            "status_from": {"S": current.status},
                            "status_to": {"S": to},
                            "by": {"S": by},
                            "detail": {"S": detail},
                        },
                    }
                },
            ]
        )

    def list(self, status: str | None = None, limit: int = 50) -> list[Proposal]:
        response = self.dynamodb.scan(TableName=self.table, Limit=limit * 4)
        found = []
        for item in response.get("Items", []):
            if item["SK"]["S"] != "META":
                continue
            proposal = self.get(item["PK"]["S"])
            if proposal and (status is None or proposal.status == status):
                found.append(proposal)
        return sorted(found, key=lambda p: p.created_at, reverse=True)[:limit]
