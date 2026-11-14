"""The control panel (docs/15, docs/11 VP9 acceptance).

The security tests are the ones that matter: the panel holds admin credentials
and has no authentication of its own, so what protects it is where it listens
and a CSRF token.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from ops.admin import AdminSession, Stage
from panel.app import BindRefusedError, check_bind, create_app
from tests.unit.conftest import BUCKET, STAGE

CSRF = "a-known-csrf-token"


class FakeEvents:
    def disable_rule(self, Name: str) -> dict[str, Any]:  # noqa: N803
        return {}

    def enable_rule(self, Name: str) -> dict[str, Any]:  # noqa: N803
        return {}


class FakeLambda:
    def put_function_concurrency(self, **kwargs: Any) -> dict[str, Any]:
        return {}


class FakeSts:
    @staticmethod
    def get_caller_identity() -> dict[str, str]:
        return {"Arn": "arn:aws:iam::000000000000:user/owner"}


@pytest.fixture
def stage() -> Stage:
    return Stage(name=STAGE, account="000000000000", region="eu-central-1")


@pytest.fixture
def client(aws: dict[str, Any], stage: Stage) -> TestClient:
    session = AdminSession(stage, assume=False)
    session._clients.update(
        {
            "dynamodb": aws["dynamodb"],
            "s3": aws["s3"],
            "events": FakeEvents(),
            "lambda": FakeLambda(),
            "sts": FakeSts(),
        }
    )
    return TestClient(create_app(stage, session, csrf_token=CSRF))


def seed(aws: dict[str, Any]) -> None:
    aws["dynamodb"].put_item(
        TableName=f"{STAGE}-agent_diary",
        Item={
            "PK": {"S": "DIARY"},
            "SK": {"S": "2026-09-06T12:00:00.000Z#inv1"},
            "content": {"S": "A quiet cycle. Nothing needed doing."},
            "mode": {"S": "free"},
        },
    )
    # A real timestamp, not a fixed one: `recent_level_two` asks "in the last
    # hour" against the wall clock, so a hard-coded past time would make this
    # pass or fail depending on when the suite runs.
    from ops.actions import _now

    aws["dynamodb"].put_item(
        TableName=f"{STAGE}-supervisor_journal",
        Item={
            "PK": {"S": "JOURNAL"},
            "SK": {"S": _now()},
            "event_type": {"S": "access.denied"},
            "level": {"N": "2"},
            "action": {"S": "pause"},
            "summary": {"S": "an agent role attempted a denied action"},
        },
    )
    aws["dynamodb"].put_item(
        TableName=f"{STAGE}-agent_memory",
        Item={
            "PK": {"S": "MEM#01J"},
            "SK": {"S": "META"},
            "type": {"S": "user_fact"},
            "content": {"S": "the owner lives in Springfield"},
            "importance": {"N": "4"},
            "created_at": {"S": "2026-09-06T10:00:00.000Z"},
        },
    )


# ------------------------------------------------------------------ security


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_binds_are_allowed(host: str) -> None:
    check_bind(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "10.0.0.5"])
def test_a_non_loopback_bind_is_refused(host: str) -> None:
    """The panel assumes the admin role and authenticates nobody."""
    with pytest.raises(BindRefusedError, match="refusing to bind"):
        check_bind(host)


def test_a_non_loopback_bind_is_allowed_with_i_know() -> None:
    check_bind("0.0.0.0", i_know=True)


def test_a_mutation_without_a_csrf_token_is_refused(client: TestClient) -> None:
    """Any page in the owner's browser can POST to 127.0.0.1."""
    response = client.post("/actions/pause", data={"reason": "x"})
    assert response.status_code == 422


def test_a_mutation_with_the_wrong_csrf_token_is_refused(client: TestClient) -> None:
    response = client.post("/actions/pause", data={"reason": "x", "token": "guessed"})
    assert response.status_code == 403


def test_a_mutation_with_the_right_token_is_accepted(client: TestClient, aws: Any) -> None:
    response = client.post(
        "/actions/pause", data={"reason": "from the panel", "token": CSRF}, follow_redirects=False
    )
    assert response.status_code == 303
    item = aws["dynamodb"].get_item(
        TableName=f"{STAGE}-agent_state", Key={"PK": {"S": "PAUSE"}, "SK": {"S": "current"}}
    )["Item"]
    assert item["paused"]["BOOL"] is True


def test_pausing_from_the_panel_is_journaled_the_same_way(client: TestClient, aws: Any) -> None:
    """One implementation, two surfaces: the record must be identical (ADR-25)."""
    client.post("/actions/pause", data={"reason": "x", "token": CSRF})
    entries = aws["dynamodb"].scan(TableName=f"{STAGE}-supervisor_journal")["Items"]
    assert entries[0]["event_type"]["S"] == "human.pause"
    assert entries[0]["by"]["S"] == "human:owner"


def test_resume_returns_a_conflict_while_a_level_two_stands(client: TestClient, aws: Any) -> None:
    seed(aws)
    response = client.post("/actions/resume", data={"token": CSRF})
    assert response.status_code == 409
    assert "acknowledged" in response.json()["detail"]


def test_an_unknown_flag_is_refused(client: TestClient) -> None:
    response = client.post(
        "/actions/flag", data={"name": "do_anything", "enabled": "true", "token": CSRF}
    )
    assert response.status_code == 400


def test_every_mutating_route_is_a_post(client: TestClient) -> None:
    for path in ("/actions/pause", "/actions/resume", "/actions/flag"):
        assert client.get(path).status_code == 405


# --------------------------------------------------------------------- pages


def test_the_overview_renders(client: TestClient, aws: Any) -> None:
    seed(aws)
    response = client.get("/")
    assert response.status_code == 200
    assert "Running." in response.text
    assert "quiet cycle" in response.text


def test_the_overview_shows_a_pause(client: TestClient, aws: Any) -> None:
    client.post("/actions/pause", data={"reason": "investigating", "token": CSRF})
    assert "Paused by human:owner: investigating" in client.get("/").text


def test_the_diary_page_renders(client: TestClient, aws: Any) -> None:
    seed(aws)
    assert "Nothing needed doing" in client.get("/diary").text


def test_the_journal_page_marks_level_two(client: TestClient, aws: Any) -> None:
    seed(aws)
    text = client.get("/journal").text
    assert "level-2" in text
    assert "denied action" in text


def test_the_memory_page_renders(client: TestClient, aws: Any) -> None:
    seed(aws)
    assert "lives in Springfield" in client.get("/memory").text


@pytest.mark.parametrize(
    "path", ["/", "/diary", "/journal", "/sessions", "/memory", "/proposals", "/storage"]
)
def test_every_page_returns_200_on_an_empty_stage(client: TestClient, path: str) -> None:
    """A fresh stage has no data; every page must still render."""
    assert client.get(path).status_code == 200


def test_a_missing_session_is_a_404(client: TestClient) -> None:
    assert client.get("/sessions/nope").status_code == 404


def test_agent_output_is_escaped_not_rendered(client: TestClient, aws: Any) -> None:
    """The agent writes the diary; the panel must never execute what it wrote."""
    aws["dynamodb"].put_item(
        TableName=f"{STAGE}-agent_diary",
        Item={
            "PK": {"S": "DIARY"},
            "SK": {"S": "2026-09-06T13:00:00.000Z#inv2"},
            "content": {"S": "<script>alert('xss')</script>"},
            "mode": {"S": "free"},
        },
    )
    text = client.get("/diary").text
    assert "<script>alert" not in text
    assert "&lt;script&gt;" in text


def test_the_session_page_shows_raw_reasoning_collapsed(client: TestClient, aws: Any) -> None:
    """ADR-28 stores raw reasoning; it is shown, but not in the reader's face."""
    aws["dynamodb"].put_item(
        TableName=f"{STAGE}-agent_sessions",
        Item={
            "PK": {
                "S": "SESSION#"
                + __import__("datetime")
                .datetime.now(tz=__import__("datetime").UTC)
                .strftime("%Y-%m-%d")
            },
            "SK": {"S": "2026-09-06T12:00:00.000Z#t1"},
            "kind": {"S": "conversation"},
            "role": {"S": "conversation"},
            "started_at": {"S": "2026-09-06T12:00:00.000Z"},
            "calls": {"N": "1"},
            "s3_prefix": {"S": "sessions/2026-09-06/t1"},
        },
    )
    import json

    aws["s3"].put_object(
        Bucket=BUCKET,
        Key="sessions/2026-09-06/t1/0000.json",
        Body=json.dumps(
            {
                "seq": 0,
                "model": "zai-org/GLM-5.3",
                "response": {
                    "content": "Springfield.",
                    "reasoning_content": "the owner seems tired today",
                },
            }
        ).encode("utf-8"),
    )
    text = client.get("/sessions/t1").text
    assert "<details>" in text
    assert "seems tired" in text
