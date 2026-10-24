"""Known pre-existing failures, registered as strict xfail.

The extraction baseline started with 22 cases that failed identically in the
source project (``D:\\Projects\\Fxi-novel``). Twenty were repaired or turned out
to assert an outdated contract; two only exercised upgrades from v81/v82
databases and were deleted because this checkout does not support old
databases. The registry is therefore empty, but the mechanism stays: any case
confirmed to fail identically in the source project can be listed here with a
short reason.

Maintenance rules:

* A listed case that starts passing must be removed (strict xfail reports it
  as ``XPASS`` otherwise) and README's baseline updated.
* A listed case that is renamed or deleted must have its entry updated too; a
  stale entry simply stops matching and the new test fails normally.
"""

from __future__ import annotations

import pytest


_KNOWN_FAILURES: dict[str, str] = {}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        reason = _KNOWN_FAILURES.get(item.nodeid)
        if reason is not None:
            item.add_marker(pytest.mark.xfail(strict=True, reason=reason))
