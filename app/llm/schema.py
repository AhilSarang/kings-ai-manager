"""Turn a Pydantic model into a strict JSON schema for structured output.

Structured-output backends want every object closed (additionalProperties
false) with all properties listed as required. Optional fields stay
nullable, so Pydantic still validates the result normally.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

# Keywords structured-output backends may reject. Pydantic still enforces them on the reply.
_DROP = {"default", "title", "examples", "discriminator", "minLength", "maxLength", "minItems",
         "maxItems", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "pattern"}
# Keywords whose keys are field or definition names, not keywords (e.g. a field called "title").
_NAME_MAPS = {"properties", "$defs"}


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema(mode="validation")
    return _walk(schema)


def _walk(node: Any) -> Any:
    if isinstance(node, list):
        return [_walk(n) for n in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for k, v in node.items():
        if k in _NAME_MAPS and isinstance(v, dict):
            out[k] = {name: _walk(sub) for name, sub in v.items()}
        elif k not in _DROP:
            out[k] = _walk(v)
    if out.get("type") == "object" and "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"].keys())
    return out
