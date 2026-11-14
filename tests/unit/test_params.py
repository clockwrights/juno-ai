"""`scripts/params.py` and `ops/params.py` (docs/14 §7, ADR-21, ADR-30).

This is the one module whose job is to handle the owner's secrets, so the tests
are mostly about what must *not* happen: a secret in output, a parameter written
as a plain String, or a half-written stage that cannot be re-run.
"""

from __future__ import annotations

import io
import json
from typing import Any

import boto3
import pytest
from moto import mock_aws

from infra.stacks.foundation import PLAIN_PARAMETERS, SECURE_PARAMETERS
from ops import params
from ops.admin import AdminSession, Stage

REGION = "eu-central-1"
KEY = "together-key-0123456789abcdef"
TOKEN = "8000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
CHAT = "-1002233445566"


@pytest.fixture
def stage() -> Stage:
    return Stage(name="test", account="000000000000", region=REGION)


@pytest.fixture
def session(stage: Stage) -> Any:
    with mock_aws():
        admin = AdminSession(stage, assume=False)
        ssm = boto3.client("ssm", region_name=REGION)
        # CDK creates every parameter as a plain String holding "unset"
        # (CloudFormation cannot create a SecureString), which is the state
        # `init` actually meets on a freshly deployed stage.
        for name in (*SECURE_PARAMETERS, *PLAIN_PARAMETERS):
            ssm.put_parameter(Name=f"/agent/test/{name}", Value="unset", Type="String")
        yield admin


def telegram(updates: list[dict[str, Any]] | None = None, username: str = "juno_test_bot") -> Any:
    """A stand-in for `urlopen` that answers getMe and getUpdates."""

    def opener(request: Any, timeout: int = 0) -> Any:
        method = request.full_url.rsplit("/", 1)[-1]
        body = {"ok": True, "result": {"username": username}}
        if method == "getUpdates":
            body = {"ok": True, "result": updates if updates is not None else []}
        return io.BytesIO(json.dumps(body).encode("utf-8"))

    return opener


def message(chat_id: str) -> dict[str, Any]:
    return {"message": {"chat": {"id": int(chat_id)}, "text": "/start"}}


def run_init(session: Any, stage: Stage, **overrides: Any) -> params.ParamResult:
    """`init` with the network stubbed out. Nothing here may reach Telegram."""
    kwargs: dict[str, Any] = {
        "together_key": KEY,
        "bot_token": TOKEN,
        "chat_id": CHAT,
        "opener": telegram([message(CHAT)]),
    }
    kwargs.update(overrides)
    return params.init(session, stage, **kwargs)


# ------------------------------------------------------------------ the list


def test_the_parameter_list_matches_the_stack() -> None:
    """A parameter the stack does not create is one `cdk destroy` leaves behind."""
    assert {p.name for p in params.PARAMETERS} == set(SECURE_PARAMETERS) | set(PLAIN_PARAMETERS)


def test_exactly_the_stack_secure_ones_are_written_encrypted() -> None:
    assert {p.name for p in params.PARAMETERS if p.secure} == set(SECURE_PARAMETERS)


# ----------------------------------------------------------------- discovery


def test_the_chat_id_comes_from_the_last_message() -> None:
    assert params.discover_chat_id(TOKEN, telegram([message("111"), message(CHAT)])) == CHAT


def test_no_updates_says_what_to_do() -> None:
    with pytest.raises(params.ParameterError, match="/start"):
        params.discover_chat_id(TOKEN, telegram([]))


def test_a_bad_token_is_named_as_such() -> None:
    def opener(request: Any, timeout: int = 0) -> Any:
        return io.BytesIO(json.dumps({"ok": False, "description": "Unauthorized"}).encode())

    with pytest.raises(params.ParameterError, match="Unauthorized"):
        params.bot_username("wrong", opener)


# ---------------------------------------------------------------------- init


def test_init_writes_every_parameter(session: Any, stage: Stage) -> None:
    run_init(session, stage, chat_id="")
    values = params.read_all(session, stage)
    assert values["together-api-key"] == KEY
    assert values["telegram-agent-bot"] == TOKEN
    assert values["owner-chat-id"] == CHAT
    assert len(values["webhook-secret"]) >= 32


def test_the_secrets_are_encrypted(session: Any, stage: Stage) -> None:
    """ADR-21: a secret in a plain String is readable from the console."""
    run_init(session, stage)
    ssm = boto3.client("ssm", region_name=REGION)
    for name in SECURE_PARAMETERS:
        got = ssm.get_parameter(Name=f"/agent/test/{name}")["Parameter"]
        if got["Value"] == "unset":
            continue  # optional, never written
        assert got["Type"] == "SecureString", name


def test_one_bot_fills_both_pairs(session: Any, stage: Stage) -> None:
    """ADR-30: splitting later is a `set` plus a bounce, with no deploy."""
    run_init(session, stage)
    values = params.read_all(session, stage)
    assert values["telegram-ops-bot"] == values["telegram-agent-bot"]
    assert values["ops-chat-id"] == values["owner-chat-id"]


def test_the_github_token_is_optional(session: Any, stage: Stage) -> None:
    """VP8 is not built, and its token should not block the first deploy."""
    result = run_init(session, stage)
    assert "github-proposal-token: optional, skipped" in result.render()


def test_a_missing_chat_id_is_reported_not_silently_skipped(session: Any, stage: Stage) -> None:
    result = run_init(session, stage, bot_token="", chat_id="")
    assert "owner-chat-id: NO VALUE" in result.render()


def test_init_keeps_what_is_already_set(session: Any, stage: Stage) -> None:
    """Re-running after a failure must not rotate the webhook secret."""
    run_init(session, stage)
    first = params.read_all(session, stage)["webhook-secret"]
    result = run_init(session, stage)
    assert params.read_all(session, stage)["webhook-secret"] == first
    assert "kept" in result.render()


def test_overwrite_replaces(session: Any, stage: Stage) -> None:
    run_init(session, stage)
    run_init(session, stage, together_key="new-key", bot_token=TOKEN, chat_id=CHAT, overwrite=True)
    assert params.read_all(session, stage)["together-api-key"] == "new-key"


# ------------------------------------------------------------------- output


def test_no_output_contains_a_secret(session: Any, stage: Stage) -> None:
    """The owner runs this in a terminal whose scrollback outlives the session."""
    written = run_init(session, stage).render()
    listed = params.show(session, stage).render()
    secret = params.read_all(session, stage)["webhook-secret"]
    for text in (written, listed):
        assert KEY not in text
        assert TOKEN not in text
        assert secret not in text


def test_show_says_which_parameters_are_still_unset(session: Any, stage: Stage) -> None:
    assert params.show(session, stage).render().count("unset") == 7


def test_the_chat_id_is_shown_because_it_is_not_a_secret(session: Any, stage: Stage) -> None:
    """Being able to see it is what lets the owner check the right chat."""
    run_init(session, stage)
    assert CHAT in params.show(session, stage).render()


# -------------------------------------------------------------------- writes


def test_an_empty_value_is_refused(session: Any, stage: Stage) -> None:
    with pytest.raises(params.ParameterError, match="empty"):
        params.put(session, stage, "owner-chat-id", "")


def test_an_unknown_parameter_is_refused(session: Any, stage: Stage) -> None:
    with pytest.raises(params.ParameterError, match="unknown"):
        params.put(session, stage, "anthropic-api-key", "x")


def test_a_type_change_falls_back_to_replacing_the_parameter(stage: Stage) -> None:
    """SSM refuses to convert a String to a SecureString in place."""
    calls: list[str] = []

    class RefusesTypeChange:
        def put_parameter(self, **kwargs: Any) -> dict[str, Any]:
            calls.append("put" + ("+overwrite" if kwargs.get("Overwrite") else ""))
            if kwargs.get("Overwrite"):
                raise RuntimeError("ValidationException: The parameter type cannot be changed")
            return {}

        def delete_parameter(self, **kwargs: Any) -> dict[str, Any]:
            calls.append("delete")
            return {}

    class FakeSession:
        def client(self, service: str) -> Any:
            return RefusesTypeChange()

    outcome = params.put(FakeSession(), stage, "together-api-key", KEY)  # type: ignore[arg-type]
    assert outcome == "converted String -> SecureString"
    assert calls == ["put+overwrite", "delete", "put"]


def test_another_write_failure_is_not_mistaken_for_a_type_change(stage: Stage) -> None:
    class Denied:
        def put_parameter(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("AccessDeniedException: not authorized")

    class FakeSession:
        def client(self, service: str) -> Any:
            return Denied()

    with pytest.raises(params.ParameterError, match="AccessDenied"):
        params.put(FakeSession(), stage, "together-api-key", KEY)  # type: ignore[arg-type]


def test_the_fingerprint_does_not_reveal_the_secret() -> None:
    assert KEY not in params.describe("together-api-key", KEY)
    assert params.fingerprint(KEY) == params.fingerprint(KEY)
    assert params.fingerprint(KEY) != params.fingerprint(KEY + "x")
