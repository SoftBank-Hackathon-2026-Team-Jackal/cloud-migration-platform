"""Prepare and bind app Values without reading configuration or credential contents."""

from cmp_contracts.application import ApplicationPlan, ApplicationValues
from cmp_contracts.chart import DynamicBinding
from cmp_contracts.provisioning import ProvisioningInputError, ValidationIssue
from pydantic import ValidationError

from cmp_providers.bindings import bind_values, verify_bound_values


def _invalid():
    # Neither attacker-controlled field names nor Pydantic input/context escape here.
    return ProvisioningInputError(
        [
            ValidationIssue(
                code="INVALID_APP_CONFIGURATION",
                path="application",
                message="Invalid application configuration",
            )
        ]
    )


def validate_application(payload: dict) -> ApplicationValues:
    try:
        return ApplicationValues.model_validate(payload)
    except (ValidationError, ValueError, TypeError):
        raise _invalid() from None


def prepare_application(payload: dict) -> ApplicationPlan:
    values = validate_application(payload)
    database = values.database
    if any(
        slot is not None for slot in (database.host, database.secret_name, database.url_secret_name)
    ):
        raise _invalid()
    fields = {item.source.field for item in values.env if item.source.kind == "DATABASE"}
    rules = []
    if "host" in fields:
        rules.append(DynamicBinding(target="database.host", source="DATABASE_HOST"))
    if "secret_ref" in fields:
        rules.append(DynamicBinding(target="database.secret_name", source="DATABASE_SECRET_NAME"))
    if "connection_url_secret_ref" in fields:
        rules.append(
            DynamicBinding(target="database.url_secret_name", source="DATABASE_URL_SECRET_NAME")
        )
    return ApplicationPlan(values=values, bindings=rules)


def resolve_application(template: dict, resolved: dict[str, str]) -> dict:
    plan = prepare_application(template)
    try:
        result = bind_values(plan.values.model_dump(mode="json"), plan.bindings, resolved)
        validate_application(result)
        return result
    except (ValueError, TypeError):
        raise _invalid() from None


def verify_application(template: dict, actual: dict, resolved: dict[str, str]) -> None:
    plan = prepare_application(template)
    try:
        verify_bound_values(plan.values.model_dump(mode="json"), actual, plan.bindings, resolved)
        validate_application(actual)
    except (ValueError, TypeError):
        raise _invalid() from None
