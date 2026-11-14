"""Full offline export and restore of a stage (ADR-27, docs/14 §11.1).

The format is deliberately boring: JSONL per table, a file tree for S3, one
manifest. It is readable without this repository, which matters because the
whole point of a backup is that it survives whatever happened to the thing that
made it.

The manifest carries `schema_version`, so `restore` is a contract like any
other: it accepts the current version and the previous one.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ops.admin import AdminSession, Stage, utc_stamp

BACKUP_SCHEMA_VERSION = "1"
SUPPORTED_VERSIONS = frozenset({"1"})

TABLES = (
    "agent_memory",
    "agent_memory_tags",
    "agent_diary",
    "agent_state",
    "agent_conversations",
    "agent_proposals",
    "agent_sessions",
    "supervisor_journal",
)

#: Durable prefixes. `exports/` is transient and `canary/` is purged after every
#: run, so neither belongs in a backup.
PREFIXES = (
    "profile/",
    "memories/",
    "artifacts/",
    "drafts/",
    "conversations/",
    "proposals/",
    "prompts/",
)
OPTIONAL_PREFIXES = ("sessions/",)

#: Cloud-sync folders. Writing a backup into one silently publishes the owner's
#: memory to a third party, so it is refused unless explicitly overridden.
SYNCED_FOLDER_HINTS = ("dropbox", "onedrive", "google drive", "icloud", "nextcloud")


@dataclass
class BackupResult:
    directory: Path
    items: dict[str, int] = field(default_factory=dict)
    objects: int = 0
    bytes_written: int = 0
    vectors: int = 0

    @property
    def total_items(self) -> int:
        return sum(self.items.values())


class BackupRefusedError(RuntimeError):
    """A guard refused to write the backup where it was asked to."""


def check_destination(directory: Path, *, i_know: bool = False) -> None:
    """Refuse to write personal data into a folder that syncs to the cloud."""
    lowered = str(directory.resolve()).lower()
    for hint in SYNCED_FOLDER_HINTS:
        if hint in lowered and not i_know:
            raise BackupRefusedError(
                f"{directory} looks like a synced cloud folder, and a backup holds "
                "the owner's memory in the clear. Pass --i-know to override."
            )


def backup(
    session: AdminSession,
    stage: Stage,
    *,
    out: Path | None = None,
    include_sessions: bool = False,
    include_secrets: bool = False,
    i_know: bool = False,
) -> BackupResult:
    directory = out or Path("backups") / stage.name / utc_stamp()
    check_destination(directory, i_know=i_know)
    directory.mkdir(parents=True, exist_ok=True)
    _lock_down(directory)

    result = BackupResult(directory=directory)

    # ------------------------------------------------------------- DynamoDB
    ddb = session.client("dynamodb")
    tables_dir = directory / "dynamodb"
    tables_dir.mkdir(exist_ok=True)
    for table in TABLES:
        full = stage.resource(table)
        count = 0
        path = tables_dir / f"{table}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            try:
                paginator = ddb.get_paginator("scan")
                for page in paginator.paginate(TableName=full):
                    for item in page.get("Items", []):
                        handle.write(json.dumps(item, default=str) + "\n")
                        count += 1
            except Exception as exc:
                handle.write("")
                result.items[table] = -1
                _note(directory, f"table {full} could not be read: {exc}")
                continue
        result.items[table] = count

    # -------------------------------------------------------------------- S3
    s3 = session.client("s3")
    prefixes = PREFIXES + (OPTIONAL_PREFIXES if include_sessions else ())
    s3_dir = directory / "s3"
    for prefix in prefixes:
        objects, written = _download_prefix(s3, stage.bucket, prefix, s3_dir)
        result.objects += objects
        result.bytes_written += written

    # Every stored version of profile.md, not just the current one: the history
    # of how the agent saw the owner is the part worth keeping.
    result.objects += _download_profile_versions(s3, stage.bucket, s3_dir)

    # --------------------------------------------------------------- vectors
    result.vectors = _dump_vectors(session, stage, directory)

    # ------------------------------------------------------------ parameters
    _dump_parameters(session, stage, directory, include_secrets=include_secrets)

    # ----------------------------------------------------------------- state
    _dump_state(session, stage, directory)

    manifest = {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "stage": stage.name,
        "account": stage.account,
        "region": stage.region,
        "created_at": utc_stamp(),
        "tables": result.items,
        "objects": result.objects,
        "bytes": result.bytes_written,
        "vectors": result.vectors,
        "includes_sessions": include_sessions,
        "includes_secrets": include_secrets,
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return result


def verify(directory: Path) -> list[str]:
    """Re-read a backup and report anything inconsistent with its manifest."""
    problems: list[str] = []
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        return [f"{directory} has no manifest.json"]

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") not in SUPPORTED_VERSIONS:
        problems.append(f"unsupported backup schema_version {manifest.get('schema_version')!r}")

    for table, expected in manifest.get("tables", {}).items():
        path = directory / "dynamodb" / f"{table}.jsonl"
        if not path.is_file():
            problems.append(f"{table}.jsonl is missing")
            continue
        if expected < 0:
            problems.append(f"{table} was not readable when the backup ran")
            continue
        actual = 0
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    # A truncated write must fail verification rather than
                    # restore partially and look successful.
                    problems.append(f"{table}.jsonl line {line_number} is not valid JSON")
                    break
                actual += 1
        if actual != expected:
            problems.append(f"{table}: manifest says {expected} items, file has {actual}")

    return problems


def restore(
    session: AdminSession,
    stage: Stage,
    directory: Path,
    *,
    overwrite: bool = False,
    only: str | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Restore into a *fresh* stage. Deploy it first with `make stage-up`."""
    problems = verify(directory)
    if problems:
        raise BackupRefusedError("; ".join(problems))

    report: list[str] = []
    ddb = session.client("dynamodb")

    if only in (None, "dynamodb"):
        for table in TABLES:
            path = directory / "dynamodb" / f"{table}.jsonl"
            if not path.is_file():
                continue
            full = stage.resource(table)
            if not overwrite and _has_items(ddb, full):
                raise BackupRefusedError(
                    f"{full} is not empty; pass --overwrite to restore into it anyway"
                )
            count = _restore_table(ddb, full, path, dry_run=dry_run)
            report.append(f"{'would restore' if dry_run else 'restored'} {count} items -> {full}")

    if only in (None, "s3"):
        count = _restore_objects(session, stage, directory / "s3", dry_run=dry_run)
        report.append(f"{'would upload' if dry_run else 'uploaded'} {count} objects")

    if only in (None, "vectors"):
        count = _restore_vectors(session, stage, directory, dry_run=dry_run)
        report.append(f"{'would put' if dry_run else 'put'} {count} vectors")

    # A restored stage always comes up paused with the heartbeat off. Waking a
    # restored agent is the operator's decision, not a side effect of restoring.
    if not dry_run:
        _restore_safe_state(session, stage)
        report.append("restored stage is paused with heartbeat_enabled = false")
    return report


# --------------------------------------------------------------------- detail


def _lock_down(directory: Path) -> None:
    if os.name != "nt":
        directory.chmod(stat.S_IRWXU)


def _note(directory: Path, message: str) -> None:
    with (directory / "NOTES.txt").open("a", encoding="utf-8") as handle:
        handle.write(message + "\n")


def _download_prefix(s3: Any, bucket: str, prefix: str, target: Path) -> tuple[int, int]:
    count = 0
    written = 0
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                path = target / key
                path.parent.mkdir(parents=True, exist_ok=True)
                body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                path.write_bytes(body)
                count += 1
                written += len(body)
    except Exception:
        return count, written
    return count, written


def _download_profile_versions(s3: Any, bucket: str, target: Path) -> int:
    count = 0
    try:
        versions = s3.list_object_versions(Bucket=bucket, Prefix="profile/profile.md")
    except Exception:
        return 0
    history = target / "profile" / "history"
    for version in versions.get("Versions", []):
        if version.get("IsLatest"):
            continue
        history.mkdir(parents=True, exist_ok=True)
        body = s3.get_object(Bucket=bucket, Key=version["Key"], VersionId=version["VersionId"])[
            "Body"
        ].read()
        (history / f"{version['VersionId']}.md").write_bytes(body)
        count += 1
    return count


def _dump_vectors(session: AdminSession, stage: Stage, directory: Path) -> int:
    path = directory / "vectors"
    path.mkdir(exist_ok=True)
    out = path / "memory.jsonl"
    count = 0
    try:
        client = session.client("s3vectors")
        token = None
        with out.open("w", encoding="utf-8") as handle:
            while True:
                request: dict[str, Any] = {
                    "vectorBucketName": stage.vector_bucket,
                    "indexName": "memory",
                    "returnData": True,
                    "returnMetadata": True,
                    "maxResults": 500,
                }
                if token:
                    request["nextToken"] = token
                response = client.list_vectors(**request)
                for vector in response.get("vectors", []):
                    handle.write(json.dumps(vector, default=str) + "\n")
                    count += 1
                token = response.get("nextToken")
                if not token:
                    break
    except Exception as exc:
        _note(directory, f"vectors could not be dumped: {exc}")
    return count


def _dump_parameters(
    session: AdminSession, stage: Stage, directory: Path, *, include_secrets: bool
) -> None:
    entries: list[dict[str, Any]] = []
    secrets: dict[str, str] = {}
    try:
        ssm = session.client("ssm")
        paginator = ssm.get_paginator("describe_parameters")
        for page in paginator.paginate(
            ParameterFilters=[
                {"Key": "Name", "Option": "BeginsWith", "Values": [stage.parameter_prefix]}
            ]
        ):
            for param in page.get("Parameters", []):
                secure = param.get("Type") == "SecureString"
                value = None
                if not secure:
                    value = ssm.get_parameter(Name=param["Name"])["Parameter"]["Value"]
                elif include_secrets:
                    secrets[param["Name"]] = ssm.get_parameter(
                        Name=param["Name"], WithDecryption=True
                    )["Parameter"]["Value"]
                entries.append(
                    {
                        "name": param["Name"],
                        "type": param.get("Type"),
                        "last_modified": str(param.get("LastModifiedDate", "")),
                        "value": value,
                    }
                )
    except Exception as exc:
        _note(directory, f"parameters could not be listed: {exc}")

    (directory / "params.json").write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    if secrets:
        path = directory / "secrets.json"
        path.write_text(json.dumps(secrets, indent=2) + "\n", encoding="utf-8")
        if os.name != "nt":
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        _note(directory, "secrets.json contains plaintext credentials. Handle accordingly.")


def _dump_state(session: AdminSession, stage: Stage, directory: Path) -> None:
    """Flags, pause and counters — what a restore must *not* carry over blindly."""
    state: dict[str, Any] = {"flags": {}, "pause": None, "counters": {}}
    try:
        ddb = session.client("dynamodb")
        table = stage.resource("agent_state")
        for page in ddb.get_paginator("scan").paginate(TableName=table):
            for item in page.get("Items", []):
                pk = item["PK"]["S"]
                sk = item["SK"]["S"]
                if pk == "FLAG":
                    state["flags"][sk] = item.get("enabled", {}).get("BOOL", False)
                elif pk == "PAUSE":
                    state["pause"] = {
                        "paused": item.get("paused", {}).get("BOOL", False),
                        "reason": item.get("reason", {}).get("S", ""),
                    }
                elif pk == "STATE" and sk.startswith("counters#"):
                    state["counters"][sk] = {
                        k: v["N"] for k, v in item.items() if isinstance(v, dict) and "N" in v
                    }
    except Exception as exc:
        _note(directory, f"state could not be dumped: {exc}")
    (directory / "state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _has_items(ddb: Any, table: str) -> bool:
    try:
        return bool(ddb.scan(TableName=table, Limit=1).get("Items"))
    except Exception:
        return False


def _restore_table(ddb: Any, table: str, path: Path, *, dry_run: bool) -> int:
    batch: list[dict[str, Any]] = []
    count = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            batch.append({"PutRequest": {"Item": json.loads(line)}})
            count += 1
            if len(batch) == 25 and not dry_run:
                ddb.batch_write_item(RequestItems={table: batch})
                batch = []
    if batch and not dry_run:
        ddb.batch_write_item(RequestItems={table: batch})
    return count


def _restore_objects(session: AdminSession, stage: Stage, root: Path, *, dry_run: bool) -> int:
    if not root.is_dir():
        return 0
    s3 = session.client("s3")
    count = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        key = path.relative_to(root).as_posix()
        if key.startswith("profile/history/"):
            continue  # history is informational; only the current version restores
        if not dry_run:
            s3.put_object(Bucket=stage.bucket, Key=key, Body=path.read_bytes())
        count += 1
    return count


def _restore_vectors(session: AdminSession, stage: Stage, directory: Path, *, dry_run: bool) -> int:
    path = directory / "vectors" / "memory.jsonl"
    if not path.is_file():
        return 0
    count = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            vector = json.loads(line)
            count += 1
            if dry_run:
                continue
            session.client("s3vectors").put_vectors(
                vectorBucketName=stage.vector_bucket,
                indexName="memory",
                vectors=[
                    {
                        "key": vector["key"],
                        "data": vector["data"],
                        "metadata": vector.get("metadata", {}),
                    }
                ],
            )
    return count


def _restore_safe_state(session: AdminSession, stage: Stage) -> None:
    """Come up paused, heartbeat off. Waking is a decision, not a side effect."""
    ddb = session.client("dynamodb")
    table = stage.resource("agent_state")
    ddb.put_item(
        TableName=table,
        Item={
            "PK": {"S": "PAUSE"},
            "SK": {"S": "current"},
            "paused": {"BOOL": True},
            "by": {"S": "restore"},
            "reason": {"S": "restored from a backup; resume deliberately"},
            "at": {"S": utc_stamp()},
        },
    )
    ddb.put_item(
        TableName=table,
        Item={
            "PK": {"S": "FLAG"},
            "SK": {"S": "heartbeat_enabled"},
            "enabled": {"BOOL": False},
            "set_by": {"S": "restore"},
            "at": {"S": utc_stamp()},
        },
    )
