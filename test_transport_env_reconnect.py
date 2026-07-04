"""ponytail self-checks for env config (#2) and reconnect supervisor (#3).

Run: python test_transport_env_reconnect.py
No framework — plain asserts. Fails loudly if the logic breaks.
"""
import asyncio
import os
import sys

sys.path.insert(0, "src")
import mcp_router.server as s


def test_env_downstream():
    env = {
        "MCP_SIEVE_DOWNSTREAM_1_NAME": "time",
        "MCP_SIEVE_DOWNSTREAM_1_COMMAND": "uvx",
        "MCP_SIEVE_DOWNSTREAM_1_ARGS": "mcp-server-time --local",  # whitespace split
        "MCP_SIEVE_DOWNSTREAM_2_NAME": "remote",
        "MCP_SIEVE_DOWNSTREAM_2_URL": "https://example.com/mcp",
        "MCP_SIEVE_DOWNSTREAM_2_ARGS": '["--flag", "with space"]',  # JSON keeps the quoted arg
        # gap at 3 stops the scan; a stray 4 must be ignored
        "MCP_SIEVE_DOWNSTREAM_4_NAME": "ignored",
    }
    old = dict(os.environ)
    os.environ.update(env)
    try:
        ds = s._env_downstream()
    finally:
        os.environ.clear()
        os.environ.update(old)

    assert [d["name"] for d in ds] == ["time", "remote"], ds
    assert ds[0]["args"] == ["mcp-server-time", "--local"], ds[0]
    assert ds[1]["url"] == "https://example.com/mcp"
    assert ds[1]["args"] == ["--flag", "with space"], ds[1]  # JSON preserved the space
    print("ok: _env_downstream")


def test_apply_env_merge():
    cfg = {"downstream": [{"name": "time", "command": "old"}, {"name": "git", "command": "git"}]}
    old = dict(os.environ)
    os.environ.update({
        "MCP_SIEVE_DOWNSTREAM_1_NAME": "time",       # same name → replaces
        "MCP_SIEVE_DOWNSTREAM_1_COMMAND": "new",
        "MCP_SIEVE_DOWNSTREAM_2_NAME": "arxiv",       # new name → appends
        "MCP_SIEVE_DOWNSTREAM_2_COMMAND": "uvx",
        "MCP_SIEVE_TOP_N": "5",
    })
    try:
        out = s._apply_env(cfg)
    finally:
        os.environ.clear()
        os.environ.update(old)

    by_name = {d["name"]: d for d in out["downstream"]}
    assert by_name["time"]["command"] == "new", by_name["time"]
    assert by_name["git"]["command"] == "git"          # untouched yaml entry survives
    assert by_name["arxiv"]["command"] == "uvx"        # env-only entry added
    assert out["embeddings"]["top_n"] == 5
    print("ok: _apply_env merge")


def test_reconnect_supervisor():
    """_supervise must survive a failed first connect and re-heal after the session dies."""
    attempts = {"n": 0}

    class FakeSession:
        def __init__(self):
            self.pinged = False

        async def send_ping(self):
            if self.pinged:            # dies on the 2nd ping
                raise ConnectionError("transport gone")
            self.pinged = True

    async def fake_connect(ds, stack):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("first attempt fails")
        return FakeSession()

    async def noop_register(name, session):
        return None

    async def run():
        s._connect = fake_connect
        s._register_tools = noop_register
        s.RECONNECT_BASE = s.RECONNECT_CAP = s.HEALTH_INTERVAL = 0.01
        ready = asyncio.Event()
        task = asyncio.create_task(s._supervise({"name": "x"}, ready))
        await asyncio.sleep(0.3)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert ready.is_set()                       # startup unblocked despite first failure
        assert attempts["n"] >= 3, attempts         # failed once, connected+died, reconnected
        assert "x" in s.downstream_sessions          # healed: a live session is registered

    asyncio.run(run())
    print("ok: _supervise reconnect")


if __name__ == "__main__":
    test_env_downstream()
    test_apply_env_merge()
    test_reconnect_supervisor()
    print("all passed")
