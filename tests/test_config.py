import pytest
from pydantic import ValidationError

from app.config import Settings


def test_defaults_load():
    s = Settings(_env_file=None, owner_whatsapp_number="+18045550100")
    assert s.llm_backend in {"anthropic_api", "fake"}
    assert s.daily_summary_time == "07:30"
    assert s.tz.key == "America/New_York"
    assert s.max_upload_bytes == 25 * 1024 * 1024


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "anthropic_api")
    monkeypatch.setenv("DAILY_SUMMARY_TIME", "06:45")
    s = Settings(_env_file=None)
    assert s.llm_backend == "anthropic_api"
    assert s.daily_summary_time == "06:45"


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_whatsapp_number", "8045550100"),
        ("daily_summary_time", "7:30pm"),
        ("daily_summary_time", "24:00"),
        ("llm_backend", "openai"),
        ("timezone", "Mars/Olympus"),
    ],
)
def test_invalid_values_rejected(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_secrets_not_in_repr():
    s = Settings(_env_file=None, anthropic_api_key="sk-secret-value")
    assert "sk-secret-value" not in repr(s)
