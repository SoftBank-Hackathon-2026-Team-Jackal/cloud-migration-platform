import pytest
from cmp_worker.review_client import parse_approvals, parse_required


def test_required_pairs_become_a_boolean_map():
    assert parse_required(["a=true", "b=false"]) == {"a": True, "b": False}


@pytest.mark.parametrize("bad", ["a", "a=maybe", "=true", "a=true=x"])
def test_bad_required_pairs_are_refused(bad):
    with pytest.raises(ValueError):
        parse_required([bad])


def test_approval_pairs_accept_only_the_two_decisions():
    assert parse_approvals(["b=approved", "c=REJECTED"]) == {"b": "APPROVED", "c": "REJECTED"}
    with pytest.raises(ValueError):
        parse_approvals(["b=maybe"])
