# ruff: noqa: F811 - the imported fixtures are used by name as test arguments
"""Ticket 06B: the confirmed analysis scope becomes the analysis input.

Seam: the same FastAPI TestClient + faked Temporal + SQLite as `test_web_start_request`. A scope
is stored exactly as ticket 56 stores it (`ScopeAnalysisRecord`, `ScopeConfirmationRecord`), then a
start request carries only `scope_id`. The started Workflow input is what the analysis receives.
Also the pure rule that turns candidates into `role_arns`.
"""

import pytest
from cmp_api.connections import EnvironmentConnection
from cmp_api.scope_selection import ScopeAnalysisRecord, ScopeConfirmationRecord
from cmp_api.start_request import scope_analysis_fields
from cmp_contracts.analysis import AnalysisInput, AnalysisReport, ConfirmedScope
from test_web_start_request import (  # noqa: F401 - fixtures and helpers shared with ticket 44
    ONPREM,
    agent,
    api,
    catalog,
    db,
    env_ref,
    env_store,
    web_request,
)

ROLE = "arn:aws:iam::123456789012:role/chat-api-irsa"
SOURCE_ROLE = "arn:aws:iam::123456789012:role/cmp-source"
CONNECTION_ID = "conn-" + "5" * 32
PROFILE = "cmp-conn-" + "5" * 32
SELECTION = {
    "connection_id": CONNECTION_ID,
    "region": "ap-northeast-1",
    "cluster": "chat-prod",
    "namespace": "chat",
    "workload": "chat-api",
    "workload_kind": "Deployment",
}
CANDIDATES = [
    {
        "candidate_id": "iam-role-chat-api-irsa",
        "resource_type": "IAM_ROLE",
        "name": "chat-api-irsa",
        "arn": ROLE,
        "evidence": [{"kind": "MANIFEST", "location": "ServiceAccount chat-api"}],
        "suggested": True,
    },
    {
        "candidate_id": "iam-role-other",
        "resource_type": "IAM_ROLE",
        "name": "other",
        "arn": "arn:aws:iam::123456789012:role/other",
        "evidence": [],
        "suggested": True,
    },
    {
        "candidate_id": "s3-chat-attachments",
        "resource_type": "S3",
        "name": "chat-attachments",
        "arn": "arn:aws:s3:::chat-attachments",
        "evidence": [{"kind": "IAM_POLICY", "location": "chat-api-irsa · s3:PutObject"}],
        "suggested": True,
    },
    {
        "candidate_id": "secret-chat-db",
        "resource_type": "SECRET",
        "name": "chat-db",
        "arn": None,
        "evidence": [{"kind": "CONFIG", "location": "Deployment chat-api env"}],
        "suggested": True,
    },
]
INCLUDED = ["iam-role-chat-api-irsa", "s3-chat-attachments", "secret-chat-db"]


def store_scope(db, *, included=None, connection=None, scope_id="scope-" + "a" * 16):
    """Stores a connection, a scope analysis and its confirmation like ticket 56 does."""
    fields = {
        "id": CONNECTION_ID,
        "kind": "AWS_SOURCE",
        "display_name": "chat-production",
        "region": "ap-northeast-2",
        "role_arn": SOURCE_ROLE,
        "account_id": "123456789012",
        "status": "CONNECTED",
        "created_at": 1,
        **(connection or {}),
    }
    chosen = INCLUDED if included is None else included
    with db() as session:
        session.add(EnvironmentConnection(**fields))
        session.add(
            ScopeAnalysisRecord(
                id="sa-" + "b" * 16,
                connection_id=CONNECTION_ID,
                selection=SELECTION,
                candidates=CANDIDATES,
                created_at=1,
            )
        )
        session.add(
            ScopeConfirmationRecord(
                id=scope_id,
                analysis_id="sa-" + "b" * 16,
                included=chosen,
                excluded=[c["candidate_id"] for c in CANDIDATES if c["candidate_id"] not in chosen],
                confirmed_at=2,
            )
        )
        session.commit()
    return scope_id


def start(client, env_ref, scope_id, **extra):
    return client.post(
        "/migrations", json=web_request(ONPREM, env_ref.id, scope_id=scope_id, **extra)
    )


# --- the confirmed scope fills the analysis input -------------------------------------------


def test_scope_id_fills_the_analysis_input_from_stored_records(api, db, env_ref, agent):
    client, temporal = api
    scope_id = store_scope(db)
    response = start(client, env_ref, scope_id)
    assert response.status_code == 202, response.text
    analysis = temporal.started[0]["data"]["analysis"]
    assert analysis["credential_ref"] == PROFILE
    assert analysis["cluster_name"] == "chat-prod"
    assert analysis["region"] == "ap-northeast-1"
    assert analysis["role_arns"] == [ROLE]
    assert analysis["scope"]["scope_id"] == scope_id
    assert analysis["scope"]["selection"]["namespace"] == "chat"
    # 06C: every candidate is passed in full, told apart by `selected`.
    by_id = {c["candidate_id"]: c for c in analysis["scope"]["candidates"]}
    assert list(by_id) == [c["candidate_id"] for c in CANDIDATES]
    assert [i for i, c in by_id.items() if c["selected"]] == INCLUDED
    unselected = by_id["iam-role-other"]
    assert unselected["selected"] is False
    assert unselected["arn"] == "arn:aws:iam::123456789012:role/other"
    # What the worker receives is a valid analysis input.
    assert AnalysisInput.model_validate(analysis).scope is not None


def test_excluded_roles_never_reach_role_arns(api, db, env_ref, agent):
    client, temporal = api
    scope_id = store_scope(db, included=["s3-chat-attachments", "secret-chat-db"])
    assert start(client, env_ref, scope_id).status_code == 202
    assert temporal.started[0]["data"]["analysis"]["role_arns"] == []


def test_the_web_cannot_override_what_the_api_fills(api, db, env_ref, agent):
    client, _ = api
    scope_id = store_scope(db)
    response = start(client, env_ref, scope_id, credential_ref="cmp-conn-" + "9" * 32)
    assert response.status_code == 422  # unknown top-level fields are still refused


# --- refusals: nothing starts, codes only -----------------------------------------------------


@pytest.mark.parametrize(
    ("connection", "code"),
    [
        ({"kind": "AWS_TARGET"}, "SCOPE_CONNECTION_KIND_MISMATCH"),
        ({"status": "PENDING"}, "SCOPE_CONNECTION_NOT_READY"),
        ({"role_arn": None}, "SCOPE_CONNECTION_NOT_READY"),
    ],
)
def test_a_connection_that_cannot_scan_refuses_the_start(api, db, env_ref, agent, connection, code):
    client, temporal = api
    scope_id = store_scope(db, connection=connection)
    response = start(client, env_ref, scope_id)
    assert response.status_code == 422
    assert response.json()["detail"] == {"code": code}
    assert temporal.started == []


def test_an_unknown_scope_refuses_the_start(api, env_ref, agent):
    client, temporal = api
    response = start(client, env_ref, "scope-" + "f" * 16)
    assert response.status_code == 422
    assert response.json()["detail"] == {"code": "SCOPE_NOT_FOUND"}
    assert temporal.started == []


def test_a_malformed_scope_id_is_refused_by_the_contract(api, env_ref, agent):
    client, temporal = api
    assert start(client, env_ref, "not-a-scope").status_code == 422
    assert temporal.started == []


def test_a_scope_without_a_target_is_refused(api, db):
    client, temporal = api
    scope_id = store_scope(db)
    response = client.post("/migrations", json={"project_id": "demo", "scope_id": scope_id})
    assert response.status_code == 422
    assert response.json()["detail"] == {"code": "SCOPE_NOT_APPLICABLE"}
    assert temporal.started == []


# --- backward compatible ----------------------------------------------------------------------


def test_without_scope_the_start_input_is_unchanged(api, env_ref, agent):
    client, temporal = api
    assert client.post("/migrations", json=web_request(ONPREM, env_ref.id)).status_code == 202
    analysis = temporal.started[0]["data"]["analysis"]
    assert analysis["scope"] is None
    assert analysis["region"] is None
    assert analysis["cluster_name"] is None
    assert analysis["credential_ref"] is None
    assert analysis["role_arns"] == []


# --- the pure rule and the contract -----------------------------------------------------------


def scope(selected):
    return ConfirmedScope.model_validate(
        {
            "scope_id": "scope-" + "a" * 16,
            "selection": SELECTION,
            "candidates": [{**c, "selected": c["candidate_id"] in selected} for c in CANDIDATES],
        }
    )


def test_only_selected_iam_roles_with_an_arn_become_role_arns():
    fields = scope_analysis_fields(scope(INCLUDED), PROFILE)
    assert fields == {
        "credential_ref": PROFILE,
        "cluster_name": "chat-prod",
        "region": "ap-northeast-1",
        "role_arns": [ROLE],
        "scope": scope(INCLUDED).model_dump(mode="json"),
    }


def test_role_arns_are_unique_and_keep_order():
    both = scope(["iam-role-chat-api-irsa", "iam-role-other"])
    first = both.candidates[0]
    again = both.model_copy(update={"candidates": [*both.candidates, first]})
    assert scope_analysis_fields(again, PROFILE)["role_arns"] == [
        ROLE,
        "arn:aws:iam::123456789012:role/other",
    ]


def test_an_iam_role_candidate_without_an_arn_is_ignored():
    bare = scope(["iam-role-chat-api-irsa"])
    first = bare.candidates[0].model_copy(update={"arn": None})
    bare = bare.model_copy(update={"candidates": [first, *bare.candidates[1:]]})
    assert scope_analysis_fields(bare, PROFILE)["role_arns"] == []


def test_a_report_without_scope_still_reads_and_leaves_no_trace_when_dumped_without_it():
    # Reports stored before 06B have no `scope` key; they must still validate.
    assert AnalysisReport.model_fields["scope"].default is None
    assert AnalysisInput.model_fields["scope"].default is None
    assert AnalysisInput.model_fields["region"].default is None


# --- ticket 31: the target provider reaches the analysis (for the Fixer's skills) --------------


def test_the_start_input_carries_the_target_provider_of_the_request(api, env_ref, agent):
    client, temporal = api
    assert client.post("/migrations", json=web_request(ONPREM, env_ref.id)).status_code == 202
    assert temporal.started[0]["data"]["analysis"]["target_provider"] == "ON_PREM"
