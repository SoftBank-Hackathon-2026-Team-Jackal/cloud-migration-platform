import pytest
from cmp_contracts.chart import DynamicBinding
from cmp_providers.bindings import bind_values, verify_bound_values


def template():
    return {
        "image": "registry/app@sha256:approved",
        "database": {"host": None, "secret_name": None},
        "replicas": 2,
    }


def rules():
    return [
        DynamicBinding(target="database.host", source="DATABASE_HOST"),
        DynamicBinding(target="database.secret_name", source="DATABASE_SECRET_NAME"),
    ]


def test_only_approved_dynamic_slots_are_filled_without_mutating_template():
    approved = template()
    resolved = {"DATABASE_HOST": "db.demo.svc", "DATABASE_SECRET_NAME": "generated-db-auth"}
    result = bind_values(approved, rules(), resolved)
    assert result == {
        "image": "registry/app@sha256:approved",
        "database": {"host": "db.demo.svc", "secret_name": "generated-db-auth"},
        "replicas": 2,
    }
    assert approved == template()
    verify_bound_values(approved, result, rules(), resolved)


def test_modifying_non_dynamic_values_is_rejected():
    resolved = {"DATABASE_HOST": "db.demo.svc", "DATABASE_SECRET_NAME": "generated-db-auth"}
    result = bind_values(template(), rules(), resolved)
    result["replicas"] = 10
    with pytest.raises(ValueError, match="Values differ outside the approved bindings"):
        verify_bound_values(template(), result, rules(), resolved)


@pytest.mark.parametrize(
    "change", ["extra", "missing", "duplicate", "occupied", "wrong_source", "absent", "bad_secret"]
)
def test_binding_errors_fail_closed(change):
    approved = template()
    bindings = rules()
    resolved = {"DATABASE_HOST": "db.demo.svc", "DATABASE_SECRET_NAME": "generated-db-auth"}
    if change == "extra":
        resolved["PASSWORD"] = "never-expose-this"
    elif change == "missing":
        del resolved["DATABASE_HOST"]
    elif change == "duplicate":
        bindings.append(bindings[0])
    elif change == "occupied":
        approved["database"]["host"] = "old-db"
    elif change == "wrong_source":
        bindings[0] = DynamicBinding(target="database.host", source="DATABASE_SECRET_NAME")
        del resolved["DATABASE_HOST"]
    elif change == "absent":
        del approved["database"]["host"]
    else:
        resolved["DATABASE_SECRET_NAME"] = "never-expose-this / invalid"
    with pytest.raises(ValueError) as caught:
        bind_values(approved, bindings, resolved)
    assert "never-expose-this" not in str(caught.value)


def test_dynamic_paths_cannot_include_image_or_password():
    from pydantic import ValidationError

    for target in ("image", "database.password"):
        with pytest.raises(ValidationError):
            DynamicBinding(target=target, source="DATABASE_HOST")
