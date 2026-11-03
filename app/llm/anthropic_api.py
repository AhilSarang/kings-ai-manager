"""Backend that calls the Anthropic Messages API with ANTHROPIC_API_KEY."""

from __future__ import annotations

import json

import anthropic

from app.config import get_settings
from app.llm.base import LLMError, LLMProvider, LLMRequest, LLMResponse, LLMUnavailableError

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicAPIProvider(LLMProvider):
    backend = "anthropic_api"

    def __init__(self, client: anthropic.Anthropic | None = None, model: str | None = None):
        s = get_settings()
        key = s.anthropic_api_key.get_secret_value()
        if client is None and not key:
            raise LLMError("LLM_BACKEND=anthropic_api requires ANTHROPIC_API_KEY")
        self.client = client or anthropic.Anthropic(api_key=key, timeout=120.0, max_retries=2)
        self.model = model or s.anthropic_model
        self.effort = s.anthropic_effort

    def complete(self, request: LLMRequest) -> LLMResponse:
        params: dict = dict(
            model=self.model,
            max_tokens=request.max_tokens,
            system=request.system,
            messages=[{"role": "user", "content": request.prompt}],
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        output_config: dict = {}
        if request.json_schema:
            output_config["format"] = {"type": "json_schema", "schema": request.json_schema}
        if self.effort:
            output_config["effort"] = self.effort
        if output_config:
            params["output_config"] = output_config

        try:
            response = self.client.beta.messages.create(**params)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise LLMUnavailableError(f"Anthropic API auth failed: {exc.message}", "auth") from exc
        except anthropic.RateLimitError as exc:
            retry_after = _retry_after(exc)
            raise LLMUnavailableError(f"Anthropic API rate limited: {exc.message}", "usage_limit", retry_after) from exc
        except anthropic.BadRequestError as exc:
            raise LLMError(f"Anthropic API rejected the request: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise LLMUnavailableError(f"Anthropic API unavailable ({exc.status_code})", "overloaded",
                                          _retry_after(exc)) from exc
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailableError("Cannot reach the Anthropic API", "network") from exc

        if response.stop_reason == "refusal":
            raise LLMError("The model declined this request")
        if response.stop_reason == "max_tokens":
            raise LLMError("The model reply was cut off (max_tokens)")
        text = "".join(b.text for b in response.content if b.type == "text")
        structured = None
        if request.json_schema:
            try:
                structured = json.loads(text)
            except json.JSONDecodeError:
                structured = None
        return LLMResponse(
            text=text,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            structured=structured,
            meta={"request_id": getattr(response, "_request_id", None)},
        )


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    try:
        return float(exc.response.headers.get("retry-after"))
    except (TypeError, ValueError, AttributeError):
        return None
