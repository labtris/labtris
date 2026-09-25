"""jmespath filter — Phase K2 `--query` shape from aws-cli.

`aws ec2 describe-instances --query 'Reservations[].Instances[].InstanceId'`
is the pattern. Same idea here: apply a JMESPath expression to the raw
data (whatever the API returned) before it hits the formatter, so users
can pick fields, filter, and reshape without piping to jq.

The dependency is optional at call time — if jmespath is not installed,
we fail with a clear message rather than blowing up in an import error.
"""

from __future__ import annotations

import os
from typing import Any


def apply_query(data: Any, expression: str | None = None) -> Any:
    """Return the filtered payload, or the original if there's no query."""
    expr = expression or os.environ.get("LABTRIS_QUERY")
    if not expr:
        return data
    try:
        import jmespath  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "the --query flag needs the jmespath package: pip install jmespath"
        ) from exc
    try:
        return jmespath.search(expr, data)
    except jmespath.exceptions.JMESPathError as exc:
        raise RuntimeError(f"invalid --query: {exc}") from exc
