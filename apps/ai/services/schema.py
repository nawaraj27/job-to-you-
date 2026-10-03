"""
Output validation (SRS §5.11).

A model returns text. A feature needs a shape. Between the two sits this
module, and its job is to be strict in one direction only: it will fish JSON
out of a fenced code block, because models wrap output in prose no matter how
firmly they are asked not to — but it will not accept a document missing a
required field, because a caller reading `output["findings"]` on a dict that
has no `findings` is the failure this exists to prevent.

Deliberately a small subset of JSON Schema — objects, arrays, scalars,
`required`, `type`, `enum`, `minItems` — rather than a dependency. The prompts
in this repository need exactly this much, and a validator nobody can read is a
validator nobody checks.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: Models wrap JSON in ```json fences roughly half the time. Stripping them is
#: not leniency about the shape, only about the packaging.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def validate(text: str, output_schema: dict[str, Any] | None) -> tuple[bool, dict, str]:
    """Parse and check one model output.

    Returns `(ok, parsed, problem)`. A schema of `None` or `{}` means the task
    wants prose, and the text is wrapped as `{"text": ...}` so every caller
    reads `run.output` the same way.
    """
    if not output_schema:
        return True, {"text": text.strip()}, ""

    parsed, problem = _parse(text)
    if problem:
        return False, {}, problem

    problem = _check(parsed, output_schema, path="")
    if problem:
        return False, {}, problem
    return True, parsed, ""


def _parse(text: str) -> tuple[Any, str]:
    candidate = (text or "").strip()
    if not candidate:
        return None, "The model returned nothing."

    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    else:
        # Some models prefix a sentence before the object. Take from the first
        # brace, which is wrong for prose and right for everything this is used
        # for — and the schema check below catches it when it is wrong.
        start = candidate.find("{")
        if start > 0:
            candidate = candidate[start:]

    try:
        return json.loads(candidate), ""
    except json.JSONDecodeError as exc:
        return None, f"Not valid JSON: {exc.msg} at position {exc.pos}."


def _check(value: Any, schema: dict[str, Any], *, path: str) -> str:
    """Return a problem description, or an empty string.

    Messages name the path, because "expected an array at findings[2].evidence"
    is actionable in a prompt review and "invalid output" is not.
    """
    expected = schema.get("type")
    where = path or "the output"

    if expected == "object":
        if not isinstance(value, dict):
            return f"Expected an object at {where}, got {_name(value)}."
        for key in schema.get("required", []):
            if key not in value:
                return f"Missing required field {key!r} at {where}."
        for key, subschema in (schema.get("properties") or {}).items():
            if key in value:
                problem = _check(value[key], subschema, path=f"{path}.{key}" if path else key)
                if problem:
                    return problem
        return ""

    if expected == "array":
        if not isinstance(value, list):
            return f"Expected an array at {where}, got {_name(value)}."
        minimum = schema.get("minItems")
        if minimum is not None and len(value) < minimum:
            return f"Expected at least {minimum} item(s) at {where}, got {len(value)}."
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                problem = _check(item, item_schema, path=f"{path}[{index}]")
                if problem:
                    return problem
        return ""

    if expected == "string" and not isinstance(value, str):
        return f"Expected a string at {where}, got {_name(value)}."
    if expected == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        return f"Expected an integer at {where}, got {_name(value)}."
    if expected == "number" and (isinstance(value, bool) or not isinstance(value, int | float)):
        return f"Expected a number at {where}, got {_name(value)}."
    if expected == "boolean" and not isinstance(value, bool):
        return f"Expected true or false at {where}, got {_name(value)}."

    allowed = schema.get("enum")
    if allowed is not None and value not in allowed:
        return f"{value!r} at {where} is not one of {allowed}."

    minimum, maximum = schema.get("minimum"), schema.get("maximum")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if minimum is not None and value < minimum:
            return f"{value} at {where} is below the minimum of {minimum}."
        if maximum is not None and value > maximum:
            return f"{value} at {where} is above the maximum of {maximum}."

    return ""


def _name(value: Any) -> str:
    return {
        dict: "an object",
        list: "an array",
        str: "a string",
        bool: "a boolean",
        int: "an integer",
        float: "a number",
        type(None): "null",
    }.get(type(value), type(value).__name__)
