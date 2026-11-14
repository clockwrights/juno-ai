"""`profile.md`: the agent's hot memory, always in context.

Writes are ETag-conditional (ADR-17). A conversation and a heartbeat can run
concurrently, and without the condition the later write would silently discard
the earlier one — losing exactly the curated summary the agent cares most about.
On conflict the current content is returned to the model, which decides how to
merge rather than being told it failed.

Every version is kept forever (docs/09 §2), so the owner can read how the
agent's picture of them changed over time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError

from core.storage.base import StoreBase

PROFILE_KEY = "profile/profile.md"
MAX_PROFILE_BYTES = 32768


@dataclass(frozen=True)
class ProfileRead:
    content: str
    etag: str


@dataclass(frozen=True)
class ProfileConflict:
    """The write lost a race; here is what is there now."""

    current_etag: str
    content: str
    error: str = "conflict"


class ProfileStore(StoreBase):
    @property
    def key_name(self) -> str:
        return f"canary/{PROFILE_KEY}" if self.is_canary else PROFILE_KEY

    def read(self) -> ProfileRead:
        try:
            response = self.s3.get_object(Bucket=self.resources.bucket, Key=self.key_name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return ProfileRead(content="", etag="")
            raise
        return ProfileRead(
            content=response["Body"].read().decode("utf-8"),
            etag=response["ETag"].strip('"'),
        )

    def write(self, content: str, etag: str) -> ProfileRead | ProfileConflict:
        """Conditional PUT. An empty `etag` means "only if it does not exist"."""
        if len(content.encode("utf-8")) > MAX_PROFILE_BYTES:
            raise ValueError("profile.md is at most 32 KiB")

        params: dict[str, Any] = {
            "Bucket": self.resources.bucket,
            "Key": self.key_name,
            "Body": content.encode("utf-8"),
            "ContentType": "text/markdown",
        }
        if etag:
            params["IfMatch"] = etag
        else:
            params["IfNoneMatch"] = "*"

        try:
            response = self.s3.put_object(**params)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("PreconditionFailed", "ConditionalRequestConflict", "412"):
                current = self.read()
                return ProfileConflict(current_etag=current.etag, content=current.content)
            raise
        return ProfileRead(content=content, etag=str(response["ETag"]).strip('"'))

    def versions(self, limit: int = 20) -> list[dict[str, str]]:
        """Every stored version, newest first (docs/15: the Profile page)."""
        response = self.s3.list_object_versions(
            Bucket=self.resources.bucket, Prefix=self.key_name, MaxKeys=limit
        )
        return [
            {
                "version_id": v["VersionId"],
                "last_modified": v["LastModified"].isoformat(),
                "is_latest": str(v["IsLatest"]),
                "size": str(v["Size"]),
            }
            for v in response.get("Versions", [])
        ]

    def read_version(self, version_id: str) -> str:
        response = self.s3.get_object(
            Bucket=self.resources.bucket, Key=self.key_name, VersionId=version_id
        )
        return str(response["Body"].read().decode("utf-8"))
