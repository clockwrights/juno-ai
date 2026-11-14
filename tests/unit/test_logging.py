"""Logging never carries content to CloudWatch (docs/06 §5.2 step 6)."""

from __future__ import annotations

import json
import logging

import pytest

from core import logging as jlog


@pytest.fixture(autouse=True)
def _clear_trace() -> None:
    jlog.set_trace_id(None)


def _emit(capsys: pytest.CaptureFixture[str], **fields: object) -> dict[str, object]:
    logger = jlog.get_logger("test.logging")
    jlog.info(logger, "event", **fields)
    parsed: dict[str, object] = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    return parsed


def test_output_is_one_json_object_per_line(capsys: pytest.CaptureFixture[str]) -> None:
    record = _emit(capsys, role="conversation")
    assert record["msg"] == "event"
    assert record["level"] == "INFO"
    assert record["role"] == "conversation"


def test_trace_id_is_attached_when_bound(capsys: pytest.CaptureFixture[str]) -> None:
    jlog.set_trace_id("01J-trace")
    assert _emit(capsys)["trace_id"] == "01J-trace"


def test_trace_id_is_absent_when_unbound(capsys: pytest.CaptureFixture[str]) -> None:
    assert "trace_id" not in _emit(capsys)


@pytest.mark.parametrize(
    "field", ["text", "content", "messages", "reply", "code", "stdout", "diff", "reasoning_content"]
)
def test_content_fields_are_redacted(field: str, capsys: pytest.CaptureFixture[str]) -> None:
    """A careless log call must not be able to leak the owner's data."""
    record = _emit(capsys, **{field: "the owner's private text"})
    assert record[field] == "<redacted>"


def test_non_content_fields_survive(capsys: pytest.CaptureFixture[str]) -> None:
    record = _emit(capsys, tokens=1234, tool="memory_search")
    assert record["tokens"] == 1234
    assert record["tool"] == "memory_search"


def test_configuring_twice_does_not_duplicate_output(capsys: pytest.CaptureFixture[str]) -> None:
    jlog.get_logger("test.dup")
    logger = jlog.get_logger("test.dup")
    jlog.info(logger, "once")
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_exception_logging_omits_the_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    """Tracebacks can carry argument values, so only the message is kept."""
    logger = jlog.get_logger("test.exc")
    try:
        raise ValueError("secret-bearing detail")
    except ValueError:
        logger.exception("failed", extra={"fields": {}})
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["exc_type"] == "ValueError"
    assert "Traceback" not in json.dumps(record)


def test_logger_level_defaults_to_info() -> None:
    assert jlog.get_logger("test.level").level == logging.INFO
