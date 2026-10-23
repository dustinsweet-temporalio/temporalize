"""
Convert Pydantic response models into JSON schemas the Messages API accepts for
structured output (`output_config.format`).

The API needs `additionalProperties: false` on every object and does not accept
length or numeric constraints, so those are stripped here (Pydantic still
validates them on the response). Fields the indexer fills in itself, such as a
folder's `subtree`, are removed so the model never writes them.
"""
from typing import Any

from pydantic import BaseModel

UNSUPPORTED_KEYS = {
    "default", "title",
    "minLength", "maxLength", "pattern",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minItems", "maxItems", "uniqueItems",
}

# Computed by the indexer after generation; never part of an LLM response.
COMPUTED_FIELDS = {"subtree"}


def _clean(node: Any) -> Any:
    if isinstance(node, list):
        return [_clean(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {k: _clean_names(v) if k in ("properties", "$defs") else _clean(v)
           for k, v in node.items() if k not in UNSUPPORTED_KEYS}
    if out.get("type") == "object" or "properties" in out:
        props = {k: v for k, v in out.get("properties", {}).items() if k not in COMPUTED_FIELDS}
        out["properties"] = props
        out["required"] = list(props)
        out["additionalProperties"] = False
    return out


def _clean_names(mapping: Any) -> Any:
    """A properties or $defs mapping: its keys are names (a field may be called `title`), not keywords."""
    if not isinstance(mapping, dict):
        return _clean(mapping)
    return {name: _clean(schema) for name, schema in mapping.items()}


def _refs(node: Any) -> set[str]:
    if isinstance(node, dict):
        found = {node["$ref"].rsplit("/", 1)[-1]} if isinstance(node.get("$ref"), str) else set()
        for value in node.values():
            found |= _refs(value)
        return found
    if isinstance(node, list):
        return set().union(*(_refs(v) for v in node)) if node else set()
    return set()


def _prune_defs(schema: dict[str, Any]) -> dict[str, Any]:
    """Drop $defs no longer referenced (e.g. the type of a removed computed field)."""
    defs = schema.get("$defs")
    if not defs:
        return schema
    body = {k: v for k, v in schema.items() if k != "$defs"}
    keep: set[str] = set()
    pending = _refs(body)
    while pending:
        name = pending.pop()
        if name in keep or name not in defs:
            continue
        keep.add(name)
        pending |= _refs(defs[name])
    if keep:
        body["$defs"] = {k: v for k, v in defs.items() if k in keep}
    return body


def to_llm_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema for `model`, cleaned for structured output."""
    return _prune_defs(_clean(model.model_json_schema()))
