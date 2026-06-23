"""`doctor --verify <function>`: re-run a function live to confirm a fix.

After an AI repair agent proposes a corrected payload/selector/parse (from the
evidence `doctor` printed), verify runs the patched function against the live
account and reports PASS/FAIL using the same classifier as detection. For
mutating tools, verify has no safe runner (it would have to create then clean up
state) and points the operator to the E2E suite instead.
"""

from __future__ import annotations

import asyncio
import sys

from .cdp_driver import CDPDriver
from .diagnostics import classify_result


async def _connect_driver() -> CDPDriver:
    """Connect a driver to a running Chrome on CDP 9222."""
    driver = CDPDriver(cdp_port=9222)
    await driver.connect()
    return driver


# Safe (read-only) invocations per function. Mutating tools are deliberately
# excluded — verifying them needs the create-then-cleanup safety pattern, which
# lives in the E2E suite (tests/test_e2e_*.py).
_VERIFY_SAFE = {
    "get_models": lambda d: d.get_models(),
    "get_projects": lambda d: d.get_projects(),
    "get_conversations": lambda d: d.get_conversations(limit=1),
    "get_memories": lambda d: d.get_memories(),
    "list_gpts": lambda d: d.list_gpts(),
}


async def verify_function(function: str) -> int:
    """Run the function live, classify, return exit code (0=pass, 1=fail, 2=no-runner).

    Async so it can be awaited in tests; the CLI wrapper (run_doctor) runs it
    via asyncio.run. Driver connection is injected via _connect_driver so tests
    can monkeypatch it without touching the network.
    """
    driver = await _connect_driver()
    try:
        runner = _VERIFY_SAFE.get(function)
        if runner is None:
            print(f"'{function}' has no safe verify runner. For mutating tools, "
                  "use the E2E suite (tests/test_e2e_*.py) to verify a fix.")
            return 2
        result = await runner(driver)
        healthy, mismatch = classify_result(function, result)
        if healthy:
            print(f"PASS: {function} returned a healthy shape.")
            return 0
        print(f"FAIL: {function} still broken — {mismatch}")
        print(f"Actual: {result!r:.500}")
        return 1
    finally:
        await driver.close()


def run_verify_cli(function: str) -> None:
    """CLI entry: run verify_function and exit with its code."""
    code = asyncio.run(verify_function(function))
    sys.exit(code)
