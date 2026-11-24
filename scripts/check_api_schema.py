"""Check that the GraphQL documents the integration sends still match Nectr's schema.

Nectr's API has introspection disabled, but it validates every request before
it checks authentication. So each document used by ``custom_components/nectr/
api.py`` is sent with one extra, deliberately invalid field. The server then
validates the whole document and rejects it without running anything (no
login, no email, no data), and the only validation errors it returns are the
probe field plus anything that has drifted: a renamed or removed field, a
changed argument or variable type, or a changed enum.

Run ``python scripts/check_api_schema.py``. Exits 1 on drift, 2 if Nectr could
not be reached or answered unexpectedly.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.nectr import api

PROBE = "zzNectrSchemaProbe"
USER_AGENT = "ha-nectr-schema-check (+https://github.com/JesseMcL/ha-nectr)"

# The refresh mutation is built inline in the client, so mirror its shape here.
REFRESH_DOCUMENT = (
    'mutation refreshToken { refreshTokens(refreshToken: "x") '
    "{ accessToken refreshToken } }"
)


def documents() -> dict[str, str]:
    """Return every GraphQL document the client sends, by name."""
    docs = {
        name.strip("_").lower(): value
        for name, value in vars(api).items()
        if re.fullmatch(r"_[A-Z_]+_(QUERY|MUTATION)", name)
    }
    docs["refresh_mutation"] = REFRESH_DOCUMENT
    return docs


def with_probe(document: str) -> str:
    """Add an invalid root field to a document so it is never executed."""
    # Variable definitions never contain braces, so the first one opens the body.
    header_end = document.index("{")
    return f"{document[: header_end + 1]} {PROBE} {document[header_end + 1 :]}"


def validate(document: str) -> list[str]:
    """Return the validation errors other than the probe field's own."""
    request = urllib.request.Request(
        api.API_URL,
        data=json.dumps({"query": with_probe(document)}).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Client-Type": "web",
            "App-Version": api.APP_VERSION,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as err:
        body = err.read()
    errors = json.loads(body).get("errors") or []
    if not errors or not all(
        e.get("extensions", {}).get("code") == "GRAPHQL_VALIDATION_FAILED"
        for e in errors
    ):
        raise RuntimeError(f"Unexpected response: {body[:300]!r}")
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
