"""Check that the GraphQL documents the integration sends still match Nectr's schema.

Nectr's API has introspection disabled, but it validates every request before
it checks authentication. So each document in ``custom_components/nectr/
queries.py`` is sent with one extra, deliberately invalid field. The server
then validates the whole document and rejects it without running anything (no
login, no email, no data), and the only validation errors it returns are the
probe field plus anything that has drifted: a renamed or removed field, a
changed argument or variable type, or a changed enum.

Needs nothing installed, only Python 3.8 or newer: ``queries.py`` is loaded by
path, so neither Home Assistant nor the rest of the integration is imported.

Run ``python scripts/check_api_schema.py``. Exits 1 on drift, 2 if Nectr could
not be reached or answered unexpectedly. When the answer isn't a GraphQL
validation response (for example a block page), the HTTP status, the response
headers that identify a CDN or firewall, and the start of the body are printed.
That shows whether the network is being refused, for example by a geographic
restriction, which CloudFront words as blocking access "from your country".
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

_QUERIES = Path(__file__).resolve().parents[1] / "custom_components/nectr/queries.py"
_spec = importlib.util.spec_from_file_location("nectr_queries", _QUERIES)
queries = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(queries)

PROBE = "zzNectrSchemaProbe"
USER_AGENT = "ha-nectr-schema-check (+https://github.com/JesseMcL/ha-nectr)"
# Response headers worth showing when a request is refused.
DIAGNOSTIC_HEADERS = (
    "server",
    "via",
    "date",
    "content-type",
    "x-cache",
    "x-amz-cf-pop",
    "x-amz-cf-id",
    "cf-ray",
    "cf-cache-status",
    "x-amzn-requestid",
    "x-amzn-errortype",
    "retry-after",
)


def documents() -> dict[str, str]:
    """Return every GraphQL document the client sends, by name."""
    docs = {
        name.lower(): value
        for name, value in vars(queries).items()
        if name.endswith(("_QUERY", "_MUTATION"))
    }
    docs["refresh_mutation"] = queries.refresh_mutation("x")
    return docs


def with_probe(document: str) -> str:
    """Add an invalid root field to a document so it is never executed."""
    # Variable definitions never contain braces, so the first one opens the body.
    header_end = document.index("{")
    return f"{document[: header_end + 1]} {PROBE} {document[header_end + 1 :]}"


def describe(status: int, headers, body: bytes) -> str:
    """Describe a response that wasn't a GraphQL validation answer."""
    shown = [f"{h}: {headers[h]}" for h in DIAGNOSTIC_HEADERS if headers.get(h)]
    text = body.decode("utf-8", "replace").strip()
    return "\n".join(
        [f"HTTP {status}, not a GraphQL validation response", *shown, text[:2000]]
    )


def validate(document: str) -> list[str]:
    """Return the validation errors other than the probe field's own."""
    request = urllib.request.Request(
        queries.API_URL,
        data=json.dumps({"query": with_probe(document)}).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Client-Type": "web",
            "App-Version": queries.APP_VERSION,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status, headers, body = response.status, response.headers, response.read()
    except urllib.error.HTTPError as err:
        status, headers, body = err.code, err.headers, err.read()
    try:
        errors = json.loads(body).get("errors") or []
    except ValueError:
        errors = []
    if not errors or not all(
        e.get("extensions", {}).get("code") == "GRAPHQL_VALIDATION_FAILED"
        for e in errors
    ):
        raise RuntimeError(describe(status, headers, body))
    return [e["message"] for e in errors if PROBE not in e["message"]]


def main() -> int:
    """Check every document and report drift."""
    drift = False
    for name, document in documents().items():
        try:
            problems = validate(document)
        except (OSError, RuntimeError, ValueError) as err:
            print(f"ERROR {name}: {err}")
            return 2
        if problems:
            drift = True
            for problem in problems:
                print(f"DRIFT {name}: {problem}")
        else:
            print(f"ok    {name}")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
