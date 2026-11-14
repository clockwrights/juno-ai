"""Writing the stage's SSM parameters (docs/14 §7, ADR-21).

Seven parameters hold everything the system needs that is not in the code: one
provider key, one bot token stored twice, one webhook secret, one GitHub token
and two chat ids. Five are secrets and are written as `SecureString`, so they
never appear in `GetFunctionConfiguration`, the console's parameter list, or a
CloudFormation drift report.

The owner never pastes a secret into a chat with a coding agent (docs/16). This
module is what makes that possible: values come from the environment, from a
local prompt, or from Telegram itself, and go straight to SSM. Nothing here
prints a secret, and `show` prints only lengths and fingerprints.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from ops.admin import AdminSession, Stage

TELEGRAM_API = "https://api.telegram.org"


class ParameterError(RuntimeError):
    """A parameter could not be read or written."""


@dataclass(frozen=True)
class Parameter:
    name: str
    secure: bool
    why: str
    #: Set by `init` without asking, by copying another parameter (ADR-30) or
    #: by generating a value. Everything else needs a human or the environment.
    mirrors: str = ""
    optional: bool = False


#: The order is the order `init` writes them, and the order `show` prints them.
#: `infra/stacks/foundation.py` creates the same seven; `tests/unit/test_params.py`
#: asserts the two lists agree, because a parameter the stack does not create is
#: one `cdk destroy` will not remove.
PARAMETERS: tuple[Parameter, ...] = (
    Parameter("together-api-key", True, "every model and embedding call (ADR-28)"),
    Parameter("telegram-agent-bot", True, "the bot token gate-telegram and gate-notify use"),
    Parameter(
        "telegram-ops-bot",
        True,
        "the same token until the ops bot is split out (ADR-30)",
        mirrors="telegram-agent-bot",
    ),
    Parameter(
        "webhook-secret",
        True,
        "proves an inbound webhook came from Telegram",
        mirrors="@generate",
    ),
    Parameter(
        "github-proposal-token",
        True,
        "gate-proposal's push and PR access (VP8)",
        optional=True,
    ),
    Parameter("owner-chat-id", False, "where Juno talks to the owner"),
    Parameter(
        "ops-chat-id",
        False,
        "the same chat until the ops bot is split out (ADR-30)",
        mirrors="owner-chat-id",
    ),
)

BY_NAME = {p.name: p for p in PARAMETERS}

#: What CDK writes at create time. A parameter still holding this has never been
#: set, which is different from one that is missing.
UNSET = "unset"


@dataclass
class ParamResult:
    action: str
    details: list[str]

    def render(self) -> str:
        return "\n".join([f"{self.action}:", *(f"  {d}" for d in self.details)])


def fingerprint(value: str) -> str:
    """A stable, non-reversible label for a secret.

    Enough to answer "is the deployed key the one in my environment?" without
    ever putting the key on a screen or in a terminal scrollback.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def describe(name: str, value: str) -> str:
    parameter = BY_NAME[name]
    if value == UNSET:
        return "unset"
    if not parameter.secure:
        return value
    return f"set ({len(value)} chars, sha256:{fingerprint(value)})"


def read_all(session: AdminSession, stage: Stage) -> dict[str, str]:
    """Every parameter's current value, or the empty string if it does not exist."""
    ssm = session.client("ssm")
    out: dict[str, str] = {}
    for parameter in PARAMETERS:
        full = f"{stage.parameter_prefix}{parameter.name}"
        try:
            response = ssm.get_parameter(Name=full, WithDecryption=True)
            out[parameter.name] = str(response["Parameter"]["Value"])
        except Exception:
            out[parameter.name] = ""
    return out


def put(session: AdminSession, stage: Stage, name: str, value: str) -> str:
    """Write one parameter with the right type. Returns what happened."""
    if name not in BY_NAME:
        raise ParameterError(f"unknown parameter {name!r}; expected one of {sorted(BY_NAME)}")
    if not value:
        raise ParameterError(f"refusing to write an empty value to {name}")
    parameter = BY_NAME[name]
    ssm = session.client("ssm")
    full = f"{stage.parameter_prefix}{parameter.name}"
    kind = "SecureString" if parameter.secure else "String"
    try:
        ssm.put_parameter(Name=full, Value=value, Type=kind, Overwrite=True)
        return "written"
    except Exception as exc:
        # CDK creates every parameter as `String`, because CloudFormation cannot
        # create a SecureString. SSM refuses to change a parameter's type in
        # place, so the first write of each secret replaces it. The name is what
        # CloudFormation tracks and it survives, so the stack keeps managing the
        # parameter — including deleting it on `cdk destroy`.
        if "type" not in str(exc).lower():
            raise ParameterError(f"could not write {full}: {exc}") from exc
        try:
            ssm.delete_parameter(Name=full)
            ssm.put_parameter(
                Name=full,
                Value=value,
                Type=kind,
                Description=f"{parameter.why} - written by scripts/params.py",
            )
        except Exception as inner:
            raise ParameterError(f"could not convert {full} to {kind}: {inner}") from inner
        return f"converted String -> {kind}"


# ------------------------------------------------------------------ telegram


def _telegram(token: str, method: str, opener: Any = None) -> dict[str, Any]:
    request = urllib.request.Request(f"{TELEGRAM_API}/bot{token}/{method}")
    try:
        with (opener or urllib.request.urlopen)(request, timeout=15) as response:
            body: dict[str, Any] = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 401 here means the pasted token is wrong, which is worth saying plainly
        # rather than letting it surface later as a silent delivery failure.
        raise ParameterError(f"Telegram rejected {method}: HTTP {exc.code}") from exc
    except Exception as exc:
        raise ParameterError(f"Telegram {method} failed: {exc}") from exc
    if not body.get("ok"):
        raise ParameterError(f"Telegram {method} failed: {body.get('description')}")
    return body


def bot_username(token: str, opener: Any = None) -> str:
    """Confirms the token works, and that it is the bot the owner expects."""
    return str(_telegram(token, "getMe", opener).get("result", {}).get("username", ""))


def discover_chat_id(token: str, opener: Any = None) -> str:
    """The chat id of whoever last messaged the bot.

    `getUpdates` only returns anything while no webhook is set, which is exactly
    the state of a stage that has not been deployed yet. After a deploy, use
    `scripts/params.py set owner-chat-id --value ...`, or run `stop.py` first.
    """
    updates = _telegram(token, "getUpdates", opener).get("result", [])
    chats = []
    for update in updates:
        chat = (update.get("message") or update.get("edited_message") or {}).get("chat") or {}
        if chat.get("id") is not None:
            chats.append(str(chat["id"]))
    if not chats:
        raise ParameterError(
            "no updates from Telegram. Send /start to the bot from the owner's "
            "chat, then run this again. (If the stage is already deployed the "
            "webhook is consuming updates: run scripts/stop.py first, or pass "
            "--chat-id.)"
        )
    return chats[-1]


# ---------------------------------------------------------------------- init


def init(
    session: AdminSession,
    stage: Stage,
    *,
    together_key: str,
    bot_token: str,
    chat_id: str = "",
    github_token: str = "",
    overwrite: bool = False,
    opener: Any = None,
) -> ParamResult:
    """Write every parameter that has a value, skipping the ones already set.

    Idempotent: re-running after a failure writes only what is missing. That
    matters because the interesting failure here is halfway through, with the
    key written and the chat id not.
    """
    existing = read_all(session, stage)
    details: list[str] = []

    if bot_token:
        details.append(f"token belongs to @{bot_username(bot_token, opener)}")
    if not chat_id and bot_token:
        chat_id = discover_chat_id(bot_token, opener)
        details.append(f"owner chat id {chat_id} (from the last message to the bot)")

    values: dict[str, str] = {
        "together-api-key": together_key,
        "telegram-agent-bot": bot_token,
        "webhook-secret": secrets.token_urlsafe(32),
        "github-proposal-token": github_token,
        "owner-chat-id": chat_id,
    }
    values["telegram-ops-bot"] = values["telegram-agent-bot"]
    values["ops-chat-id"] = values["owner-chat-id"]

    for parameter in PARAMETERS:
        value = values.get(parameter.name, "")
        current = existing.get(parameter.name, "")
        if current not in ("", UNSET) and not overwrite:
            details.append(f"{parameter.name}: kept ({describe(parameter.name, current)})")
            continue
        if not value:
            note = "optional, skipped" if parameter.optional else "NO VALUE - still unset"
            details.append(f"{parameter.name}: {note}")
            continue
        outcome = put(session, stage, parameter.name, value)
        details.append(f"{parameter.name}: {outcome} ({describe(parameter.name, value)})")

    return ParamResult(f"params init {stage.name}", details)


def show(session: AdminSession, stage: Stage) -> ParamResult:
    existing = read_all(session, stage)
    details = []
    for parameter in PARAMETERS:
        value = existing.get(parameter.name, "")
        state = "MISSING (stage not deployed?)" if value == "" else describe(parameter.name, value)
        details.append(f"{parameter.name}: {state}  - {parameter.why}")
    return ParamResult(f"params {stage.name}", details)
