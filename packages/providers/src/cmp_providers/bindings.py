"""Fill approved connection-reference slots without changing the rest of Helm Values."""

from copy import deepcopy

from cmp_contracts.chart import DynamicBinding, KubernetesName
from pydantic import TypeAdapter, ValidationError

from cmp_providers.artifacts import canonical_json_bytes


def bind_values(template: dict, rules: list[DynamicBinding], resolved: dict[str, str]) -> dict:
    canonical_json_bytes(template)
    allowed = {
        "database.host": "DATABASE_HOST",
        "database.secret_name": "DATABASE_SECRET_NAME",
        "database.url_secret_name": "DATABASE_URL_SECRET_NAME",
    }
    # Optional slots are omitted from serialized Values while unset (ticket 19).
    optional = {"database.url_secret_name"}
    seen = set()
    if set(resolved) != {rule.source for rule in rules}:
        raise ValueError("Resolved binding keys differ from the approved sources")
    result = deepcopy(template)
    for rule in rules:
        if allowed[rule.target] != rule.source or rule.target in seen:
            raise ValueError("Invalid or duplicate binding rule")
        seen.add(rule.target)
        section, key = rule.target.split(".")
        if not isinstance(result.get(section), dict) or (
            key not in result[section] and rule.target not in optional
        ):
            raise ValueError("Approved binding slot is absent")
        if result[section].get(key) is not None:
            raise ValueError("Approved binding slot must be null before resolution")
        value = resolved[rule.source]
        if not isinstance(value, str) or not value or value.strip() != value:
            raise ValueError("Invalid resolved connection reference")
        if rule.source in {"DATABASE_SECRET_NAME", "DATABASE_URL_SECRET_NAME"}:
            try:
                TypeAdapter(KubernetesName).validate_python(value)
            except ValidationError:
                raise ValueError("Invalid resolved Secret name") from None
        elif any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-"
            for char in value
        ):
            raise ValueError("Invalid resolved database host")
        result[section][key] = value
    return result


def verify_bound_values(
    template: dict,
    actual: dict,
    rules: list[DynamicBinding],
    resolved: dict[str, str],
) -> None:
    expected = bind_values(template, rules, resolved)
    if canonical_json_bytes(actual) != canonical_json_bytes(expected):
        raise ValueError("Values differ outside the approved bindings or from resolved references")
