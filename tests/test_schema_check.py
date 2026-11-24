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


def test_script_needs_no_packages() -> None:
    """The check only loads queries.py, so it needs nothing installed."""
    source = Path(check.__file__).read_text()
    assert "import aiohttp" not in source
    assert "from custom_components" not in source
    queries = Path(check.__file__).parents[1] / "custom_components/nectr/queries.py"
    # queries.py may only import the standard library.
    imports = [
        line for line in queries.read_text().splitlines() if line.startswith("import ")
    ]
    assert imports == ["import json"]
    assert "from ." not in queries.read_text()


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


def test_refusal_report_shows_status_headers_and_body() -> None:
    """A block page is reported with what identifies the CDN and why."""
    headers = {"Server": "CloudFront", "x-amz-cf-pop": "IAD89-P1", "x-other": "no"}
    lowered = {k.lower(): v for k, v in headers.items()}
    text = check.describe(403, lowered, b"<H1>403 ERROR</H1> block access")
    assert text.startswith("HTTP 403")
    assert "server: CloudFront" in text
    assert "x-amz-cf-pop: IAD89-P1" in text
    assert "x-other" not in text
    assert "block access" in text
