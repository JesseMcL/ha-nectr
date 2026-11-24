"""Tests for the schema drift check script (no network)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "check_api_schema",
    Path(__file__).parents[1] / "scripts" / "check_api_schema.py",
)
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)


def test_all_documents_found() -> None:
    """Every operation the client sends is covered."""
    assert set(check.documents()) == {
        "accounts_query",
        "account_info_query",
        "usage_query",
        "request_otp_mutation",
        "verify_otp_mutation",
        "resend_otp_mutation",
        "refresh_mutation",
    }


@pytest.mark.parametrize("name", sorted(check.documents()))
def test_probe_is_first_root_field(name: str) -> None:
    """The probe is injected right inside the operation's body, once."""
    document = check.with_probe(check.documents()[name])
    assert document.count(check.PROBE) == 1
    before, after = document.split(check.PROBE)
    assert before.count("(") == before.count(")")
    assert before.count("{") - before.count("}") == 1
    assert before.rstrip().endswith("{")
    assert after.count("}") == after.count("{") + 1
