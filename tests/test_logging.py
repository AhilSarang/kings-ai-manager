import json
import logging

from app.logging_setup import JsonFormatter


def _format(**extra) -> dict:
    record = logging.makeLogRecord({"name": "t", "levelname": "INFO", "msg": "hello", **extra})
    return json.loads(JsonFormatter().format(record))


def test_json_line_has_core_fields():
    out = _format()
    assert out["msg"] == "hello"
    assert out["level"] == "INFO"
    assert "ts" in out


def test_extra_fields_included_and_secrets_masked():
    out = _format(task_ref="T-1", access_token="abc123")
    assert out["task_ref"] == "T-1"
    assert out["access_token"] == "***"
