"""Check that the configured LLM backend works (real call, tiny prompt).

Usage (server): docker compose exec worker python scripts/llm_smoke.py
Usage (local):  uv run python scripts/llm_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import BaseModel  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.llm.base import LLMRequest  # noqa: E402
from app.llm.schema import strict_schema  # noqa: E402
from app.llm.service import extract_json, get_provider  # noqa: E402


class Pong(BaseModel):
    reply: str


def main() -> None:
    provider = get_provider()
    print(f"Backend: {get_settings().llm_backend} ({provider.model or 'default model'})")
    resp = provider.complete(LLMRequest(
        system="You are a health check. Reply with JSON only.",
        prompt='Return {"reply": "pong"}.', purpose="smoke", json_schema=strict_schema(Pong), max_tokens=200,
    ))
    data = resp.structured if resp.structured is not None else extract_json(resp.text)
    print("Parsed:", Pong.model_validate(data))
    print(f"Tokens in/out: {resp.input_tokens}/{resp.output_tokens}")


if __name__ == "__main__":
    main()
