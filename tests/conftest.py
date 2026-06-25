"""Shared pytest configuration and fixtures.

Opt-in E2E tests
----------------
End-to-end tests that drive a *real* ChatGPT account via Chrome CDP are
marked with ``@pytest.mark.e2e``. They mutate the account and need a
logged-in browser, so they must NEVER run in normal/CI test runs.

The mechanism:
  - When ``W2A_E2E_RUN`` is unset (the default), e2e-marked items are
    *deselected* (silently dropped from the session) — not failed — so a
    bare ``pytest`` stays green with no account present.
  - When ``W2A_E2E_RUN=1``, they are collected and run.

The marker is also registered in ``pyproject.toml`` so pytest doesn't warn
about unknown markers, and CI runs ``pytest -m "not e2e"`` as a belt.
"""

from __future__ import annotations

import os

import pytest


def e2e_enabled() -> bool:
    """True iff the operator opted into E2E tests via ``W2A_E2E_RUN=1``."""
    return os.environ.get("W2A_E2E_RUN") == "1"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Deselect e2e-marked items unless ``W2A_E2E_RUN=1``.

    Deselect (not fail) so the default suite is green without a browser or
    account. With the flag set, the items run normally.
    """
    if e2e_enabled():
        return
    e2e_items = [it for it in items if it.get_closest_marker("e2e")]
    if not e2e_items:
        return
    # pytest's config hook expects us to mutate the list in place.
    for it in e2e_items:
        items.remove(it)
    # Report the deselection so it's visible.
    if items is not None and e2e_items:
        print(
            f"\n[e2e] Deselected {len(e2e_items)} end-to-end test(s); "
            "set W2A_E2E_RUN=1 to run them against a real ChatGPT account."
        )


# ═══════════════════════════════════════════════════════════════
# E2E session fixtures
#
# Session-scoped so a single Chrome + driver + login spans the whole e2e
# run. Only instantiated when at least one e2e test is collected (pytest
# does not create session fixtures whose dependents never run).
# ═══════════════════════════════════════════════════════════════

import asyncio  # noqa: E402
import time

import pytest_asyncio  # noqa: E402

from chatgpt_web2api.cdp_driver import CDPDriver  # noqa: E402
from chatgpt_web2api.chrome import ChromeProcess  # noqa: E402
from chatgpt_web2api.config import Config  # noqa: E402

E2E_LOGIN_TIMEOUT = 600  # seconds to wait for interactive login


@pytest.fixture(scope="session")
def event_loop():
    """One event loop for the whole session.

    E2E session fixtures (e2e_driver) open a long-lived websocket that must
    stay alive across tests. pytest-asyncio gives each test its own loop by
    default, which would leave the session-scoped websocket bound to a dead
    loop ("got Future attached to a different loop"). Sharing one loop across
    the session keeps the driver's connection valid for every e2e test.
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def event_loop():
    """One event loop for the whole session.

    E2E session fixtures (e2e_driver) open a long-lived websocket that must
    stay alive across tests. pytest-asyncio gives each test its own loop by
    default, which would leave the session-scoped websocket bound to a dead
    loop ("got Future attached to a different loop"). Sharing one loop across
    the session keeps the driver's connection valid for every e2e test.
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def e2e_config() -> Config:
    """Config for the live run: default profile, headed, CDP on 9222."""
    cfg = Config.load(None)
    cfg.chrome.headless = False  # login + interaction need a visible window
    return cfg


# Minimum seconds to wait before an e2e test that sends a chat message.
# ChatGPT rate-limits rapid consecutive send_and_stream calls ("Too many
# requests"). Pacing each chat-bearing test avoids tripping the limit.
# 20s is a safe default; raise via W2A_E2E_PACE if the account still gets
# throttled, lower for a faster (but riskier) run.
E2E_CHAT_PACE_SECONDS = float(os.environ.get("W2A_E2E_PACE", "20"))


@pytest_asyncio.fixture(autouse=True)
async def e2e_pace_before_chat(request):
    """Sleep before each e2e test so chat operations don't burst-limit.

    Only paces e2e-marked tests; non-e2e tests are unaffected. The delay is
    configurable via W2A_E2E_PACE (seconds). Reads are cheap and could skip
    this, but a uniform small pace keeps the whole suite polite to the host.
    """
    if request.node.get_closest_marker("e2e") is None:
        yield
        return
    await asyncio.sleep(E2E_CHAT_PACE_SECONDS)
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Re-mark RateLimitError e2e failures as skips.

    A rate limit is an environment condition (ChatGPT throttling the account),
    not a code failure. This hook fires for every test; when an e2e test failed
    because of a RateLimitError we convert its report outcome to ``skipped`` so
    the suite reads honestly (green/skipped, never red, on a throttle). Note:
    ``pytest_exception_interact`` does NOT fire for ordinary failures, so it
    can't do this job.
    """
    outcome = yield
    report = outcome.get_result()
    if report.when != "call":
        return
    if item.get_closest_marker("e2e") is None:
        return
    from chatgpt_web2api.cdp_driver import RateLimitError

    # The exception is recorded on the call's excinfo.
    if call.excinfo and isinstance(call.excinfo.value, RateLimitError):
        report.outcome = "skipped"
        # longrepr as a plain string is the standard way pytest records a
        # skip reason. Do NOT set wasxfail — that flips it to xfail handling.
        report.longrepr = f"ChatGPT rate-limited the account: {call.excinfo.value}"


@pytest_asyncio.fixture(scope="session")
async def e2e_chrome(e2e_config: Config):
    """Ensure Chrome with CDP is running for the whole e2e session.

    Attach semantics: if Chrome is already on the CDP port it is reused and
    teardown leaves it running. If the fixture launched it, teardown stops it.
    """
    chrome = ChromeProcess(e2e_config)
    launched = not await chrome._cdp_alive()
    await chrome.ensure_running()
    try:
        await chrome.start_monitor()
    except Exception:
        pass  # monitor is optional; the driver is what we need
    yield chrome
    if launched:
        await chrome.stop()
    else:
        # Attached to an external Chrome: don't kill it, just drop the monitor.
        if chrome._monitor_task:
            chrome._monitor_task.cancel()


@pytest_asyncio.fixture(scope="session")
async def e2e_login_ready(e2e_chrome, e2e_config: Config) -> bool:
    """Ensure the account is logged in once for the whole session.

    Uses a throwaway driver to detect/wait for login. The expensive part
    (launching Chrome + interactive login) happens exactly once here; each
    test then opens its own short-lived driver via ``e2e_driver``.
    """
    probe = CDPDriver(cdp_port=e2e_config.chrome.cdp_port)
    try:
        await probe.connect()
    except Exception:
        await _wait_for_login(probe, timeout=E2E_LOGIN_TIMEOUT)
        await probe.connect()
    await probe.close()
    return True


@pytest_asyncio.fixture
async def e2e_driver(e2e_login_ready, e2e_config: Config) -> CDPDriver:
    """A fresh connected CDPDriver per test.

    Function-scoped on purpose: the driver's websocket is bound to the loop it
    was created on, and pytest-asyncio runs each test on its own loop. Opening
    a fresh driver per test avoids the "Future attached to a different loop"
    cross-loop error while still reusing the session-scoped Chrome + login.
    ``connect()`` is cheap (a localhost page lookup + websocket + token fetch).
    """
    driver = CDPDriver(cdp_port=e2e_config.chrome.cdp_port)
    await driver.connect()
    yield driver
    await driver.close()


@pytest_asyncio.fixture
async def e2e_sse_server(e2e_login_ready, e2e_config: Config) -> str:
    """Start a real uvicorn SSE MCP server for e2e tests, return its URL.

    Function-scoped (NOT session-scoped) on purpose: pytest-asyncio with
    ``asyncio_mode="auto"`` runs each async test on its own loop, so a
    session-scoped server would live on a different loop than the test's
    ``sse_client`` — the HTTP request would be serviced on the server's loop
    but the response could never reach the test's loop, and the call hangs.
    Running the server per test keeps it on the same loop as the client.
    Startup is ~4s, acceptable for opt-in e2e.

    Runs on a non-8090 port so it never contends with an operational SSE
    server. Wired to a dedicated driver via the module globals (the same
    wiring ``_live_server`` in test_e2e_mcp uses), independent of the
    per-test ``e2e_driver``.

    Readiness is polled via a TCP socket connect — NOT a ``GET /sse``, which
    would hold the connection open and look like a hang. Teardown cancels the
    server task so no task is left dangling and the port is free for the next
    test.
    """
    import socket as _socket

    import chatgpt_web2api.mcp_server as mod

    port = int(os.environ.get("W2A_SSE_PORT", "18090"))

    # Fail clearly if the chosen port is already occupied (avoids a confusing
    # "address in use" from uvicorn mid-test).
    with _socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as e:
            raise RuntimeError(
                f"e2e_sse_server: port {port} is occupied. Set W2A_SSE_PORT to a free port. ({e})"
            )

    # Dedicated driver for the SSE server this test, separate from the
    # per-test e2e_driver. Wire the module globals so create_server()/_run_sse
    # see it.
    sse_driver = CDPDriver(cdp_port=e2e_config.chrome.cdp_port)
    await sse_driver.connect()
    mod._driver = sse_driver
    mod._config = e2e_config
    mod._lock = asyncio.Lock()

    server = mod.create_server()
    init_options = server.create_initialization_options()

    # Run _run_sse as a background task; it serves until cancelled at teardown.
    sse_task = asyncio.create_task(mod._run_sse(server, init_options, e2e_config, port))

    url = f"http://127.0.0.1:{port}/sse"

    # Wait for the TCP port to accept connections (server readiness). Poll
    # rather than GET /sse: an SSE endpoint holds the connection open, so an
    # HTTP probe can look like a hang even when the server is correct.
    deadline = time.monotonic() + 15.0
    ready = False
    while time.monotonic() < deadline:
        try:
            with _socket.socket() as s:
                s.settimeout(0.5)
                s.connect(("127.0.0.1", port))
                ready = True
                break
        except OSError:
            await asyncio.sleep(0.2)
    if not ready:
        sse_task.cancel()
        await asyncio.gather(sse_task, return_exceptions=True)
        raise TimeoutError(f"SSE server did not become ready on port {port}")

    yield url

    # Clean teardown: cancel the server task, then close the driver. Cancelling
    # (and awaiting the gather) avoids a dangling task / occupied port on the
    # next test.
    try:
        sse_task.cancel()
        await asyncio.gather(sse_task, return_exceptions=True)
    finally:
        await sse_driver.close()


async def _wait_for_login(driver: CDPDriver, timeout: int) -> None:
    """Navigate to chatgpt.com and poll for an auth token (mirrors Service)."""
    print("\n" + "=" * 52 + "\n  NOT LOGGED IN — log into ChatGPT in the window\n" + "=" * 52)
    try:
        await driver._cdp("Page.navigate", {"url": "https://chatgpt.com/"})
    except Exception:
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw = await driver._js(
                "(async () => { try { const r = await fetch('/api/auth/session',"
                " {credentials:'include'}); const d = await r.json(); return d.accessToken || ''; }"
                " catch(e) { return ''; } })()"
            )
            if raw and len(raw) > 100:
                print("  Login detected.\n")
                return
        except Exception:
            pass
        await asyncio.sleep(2)
    raise TimeoutError(f"Login not completed within {timeout}s")


@pytest.fixture(scope="session")
def e2e_created() -> dict:
    """Registry of account state created by the e2e session, for guaranteed cleanup.

    Tests register ids here as they create conversations/memories/projects.
    The ``e2e_cleanup`` finalizer removes them even if a test crashed.
    """
    return {"conversations": set(), "memories": set(), "projects": set()}


@pytest.fixture
def e2e_app_config() -> Config:
    """A Config for do_chat_completion (server config, not the chrome cfg)."""
    return Config.load(None)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def e2e_cleanup(e2e_created, e2e_config: Config, request):
    """Safety-net finalizer: remove any state left in the registry.

    Autouse + session: runs once after all e2e tests. Opens its own throwaway
    driver (on the session loop) so cleanup works regardless of per-test loop
    lifecycle. Conversations/memories/projects registered here are deleted;
    only ever ids the tests themselves created. No-op if no e2e tests ran.
    """
    yield
    if (
        not e2e_created["conversations"]
        and not e2e_created["memories"]
        and not e2e_created["projects"]
    ):
        return
    driver = CDPDriver(cdp_port=e2e_config.chrome.cdp_port)
    try:
        await driver.connect()
    except Exception as e:
        print(f"[e2e cleanup] could not connect for cleanup: {e}")
        return
    try:
        for cid in list(e2e_created["conversations"]):
            try:
                await driver.delete_conversation(cid)
            except Exception as e:  # cleanup must be best-effort
                print(f"[e2e cleanup] could not delete conversation {cid}: {e}")
        for mid in list(e2e_created["memories"]):
            try:
                await driver.delete_memory(mid)
            except Exception as e:
                print(f"[e2e cleanup] could not delete memory {mid}: {e}")
        for pid in list(e2e_created["projects"]):
            # delete_project is not yet implemented (Step 0 found the endpoint
            # works but create_project's payload is broken — tracked as xfail).
            deleter = getattr(driver, "delete_project", None)
            if deleter is None:
                print(
                    f"[e2e cleanup] project {pid} has no delete method — "
                    "remove manually in the ChatGPT UI."
                )
                continue
            try:
                await deleter(pid)
            except Exception as e:
                print(f"[e2e cleanup] could not delete project {pid}: {e}")
    finally:
        await driver.close()
