"""Admin-side session handling for `scripts/` and the control panel.

Everything a human runs against a stage goes through here, so there is one place
that decides which account, which region and which role — and one place that
refuses to touch `prod` by accident.

The admin role (ADR-25) is assumed rather than used directly, so the owner's own
credentials are never what talks to the tables; the role is also the only
principal allowed to delete live data.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

from ops import localenv

DEFAULT_REGION = localenv.DEFAULT_REGION
ADMIN_ROLE_TEMPLATE = "arn:aws:iam::{account}:role/{stage}-agent-admin"
SESSION_NAME = "agent-ops"


class StageRefusalError(RuntimeError):
    """A guard refused an operation on this stage."""


@dataclass(frozen=True)
class Stage:
    """Everything a script needs to address one deployment."""

    name: str
    account: str
    region: str = DEFAULT_REGION

    @property
    def is_prod(self) -> bool:
        return self.name == "prod"

    def resource(self, suffix: str) -> str:
        return f"{self.name}-{suffix}"

    @property
    def bucket(self) -> str:
        return f"{self.name}-agent-{self.account}"

    @property
    def vector_bucket(self) -> str:
        return f"{self.name}-agent-vectors"

    @property
    def parameter_prefix(self) -> str:
        return f"/agent/{self.name}/"

    @property
    def stack_names(self) -> tuple[str, ...]:
        """Stacks in dependency order. Destroy walks this backwards."""
        return (
            f"{self.name}-foundation",
            f"{self.name}-storage",
            f"{self.name}-sandbox",
            f"{self.name}-gates",
            f"{self.name}-agent",
            f"{self.name}-supervisor",
        )


def _role_exists(stage: Stage, arn: str) -> bool:
    """Whether the admin role is there at all. Unknown counts as there."""
    try:
        boto3.client("iam", region_name=stage.region).get_role(RoleName=arn.rsplit("/", 1)[-1])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchEntity":
            return False
    except Exception:
        # Cannot tell (no iam:GetRole, say). Treating it as present keeps the
        # strict refusal, which is the safe direction.
        return True
    return True


class AdminSession:
    """A boto3 session holding the assumed admin role, with client caching."""

    def __init__(self, stage: Stage, *, assume: bool = True) -> None:
        self.stage = stage
        self._clients: dict[str, Any] = {}
        if assume:
            self._session = self._assume(stage)
        else:
            # Used by tests under moto, and as a fallback when the role does not
            # exist yet — which is the case before VP0 has ever been deployed.
            self._session = boto3.Session(region_name=stage.region)

    @staticmethod
    def _assume(stage: Stage) -> boto3.Session:
        sts = boto3.client("sts", region_name=stage.region)
        arn = ADMIN_ROLE_TEMPLATE.format(account=stage.account, stage=stage.name)
        try:
            creds = sts.assume_role(RoleArn=arn, RoleSessionName=SESSION_NAME)["Credentials"]
        except ClientError as exc:
            # A role that does not exist and a role that will not have you both
            # come back as AccessDenied, and they need opposite responses: the
            # first means the stage is not deployed — which is exactly the state
            # `destroy.py` resumes from, after `cdk destroy` has removed the role
            # it would otherwise assume — and the second is a trust-policy
            # problem worth stopping on. Ask IAM which one it is.
            if _role_exists(stage, arn):
                raise StageRefusalError(
                    f"cannot assume {arn}: {exc}. Does your identity trust the role?"
                ) from exc
            print(
                f"note: {arn} does not exist, so this runs as your own identity. "
                "That is expected when the stage is not deployed."
            )
            return boto3.Session(region_name=stage.region)
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=stage.region,
        )

    def client(self, service: str) -> Any:
        # boto3-stubs overloads client() on literal service names so it can
        # return a precise type. These accessors are deliberately generic —
        # `scripts/` chooses the service at runtime — so the narrow typing is
        # traded away here and nowhere else.
        session: Any = self._session
        if service not in self._clients:
            self._clients[service] = session.client(service)
        return self._clients[service]

    def resource(self, service: str) -> Any:
        session: Any = self._session
        return session.resource(service)

    @property
    def caller(self) -> str:
        """`human:<iam identity>`, written into every journal entry (docs/14 §1)."""
        identity = self.client("sts").get_caller_identity()
        return f"human:{identity['Arn'].rsplit('/', 1)[-1]}"


def resolve_stage(name: str, account: str | None = None, region: str | None = None) -> Stage:
    """`--region`, else `JUNO_AWS_REGION` from the environment or `.env`.

    The account is whatever the caller's credentials belong to: a script acts
    where it is authenticated, and the CDK app refuses a mismatch on deploy.
    """
    region = region or localenv.region()
    acct = account or boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    return Stage(name=name, account=acct, region=region)


def guard_prod(stage: Stage, *, i_know: bool, action: str) -> None:
    """Refuse a destructive action on prod unless the caller passed --i-know."""
    if stage.is_prod and not i_know:
        raise StageRefusalError(
            f"{action} on prod needs --i-know. "
            "This is the stage with the owner's real memory in it."
        )


def confirm_stage_name(stage: Stage, typed: str | None, *, prompt: str) -> None:
    """Require the stage name typed back, the way `resume.py` and `destroy.py` do."""
    if typed == stage.name:
        return
    if typed is not None:
        raise StageRefusalError(f"typed {typed!r}, expected {stage.name!r}")
    answer = input(f"{prompt}\nType the stage name ({stage.name}) to continue: ").strip()
    if answer != stage.name:
        raise StageRefusalError("cancelled")


def utc_stamp() -> str:
    """Filesystem-safe UTC timestamp for backup directories."""
    return dt.datetime.now(tz=dt.UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
