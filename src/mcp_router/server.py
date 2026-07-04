"""MCP Sieve — semantic tool selection proxy.

Path 1 (notifications/tools/list_changed):
  1. tools/list returns 1 tool: mcp_router_select
  2. LLM calls mcp_router_select(task="...") → sieve finds top-N tools
  3. Sieve updates current_tools + sends notifications/tools/list_changed
  4. tools/list now returns the relevant tools
  5. LLM calls a tool directly → sieve proxies to downstream

Path 2 (mcp_router_call):
  For clients with a frozen toolset (e.g. Hermes with prompt caching):
  1. LLM calls mcp_router_select(task="...") → gets a list of suitable tools with their inputSchema.
  2. LLM calls mcp_router_call(tool_name="...", arguments={...}) to execute the chosen tool.

ponytail: one file, everything in it.
"""
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import NamedTuple

import httpx
import numpy as np
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.server import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, EmbeddedResource, ImageContent, TextContent, Tool

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("mcp-sieve")

# --- Config ------------------------------------------------------------------

# ponytail: when launched via uvx --from <project>, the package lives in uv-cache,
# __file__ points there. Look for config.yaml: (1) env MCP_ROUTER_CONFIG,
# (2) next to CWD, (3) fallback to the old path relative to source.
def _find_config() -> Path:
    env = os.environ.get("MCP_ROUTER_CONFIG")
    if env:
        p = Path(env).expanduser()
        if p.exists():
            return p
    cwd_cfg = Path.cwd() / "config.yaml"
    if cwd_cfg.exists():
        return cwd_cfg
    src_cfg = Path(__file__).resolve().parent.parent.parent / "config.yaml"
    return src_cfg

CONFIG_PATH = _find_config()


def _env_downstream() -> list[dict]:
    """Read MCP_SIEVE_DOWNSTREAM_<N>_* env vars into downstream entries.

    ponytail: for Docker/k8s where mounting a file is a pain. N starts at 1,
    stops at the first gap. _ARGS is a JSON array (for quoting/backslashes) or
    plain whitespace-split. _URL/_TRANSPORT enable HTTP/SSE downstream.
    """
    out: list[dict] = []
    n = 1
    while True:
        name = os.environ.get(f"MCP_SIEVE_DOWNSTREAM_{n}_NAME")
        if not name:
            break
        entry: dict = {"name": name}
        for key, field in (("COMMAND", "command"), ("URL", "url"), ("TRANSPORT", "transport")):
            val = os.environ.get(f"MCP_SIEVE_DOWNSTREAM_{n}_{key}")
            if val:
                entry[field] = val
        raw_args = os.environ.get(f"MCP_SIEVE_DOWNSTREAM_{n}_ARGS")
        if raw_args:
            entry["args"] = json.loads(raw_args) if raw_args.lstrip().startswith("[") else raw_args.split()
        out.append(entry)
        n += 1
    return out


def _apply_env(cfg: dict) -> dict:
    """Merge env-var overrides into a loaded config dict."""
    downstream = cfg.get("downstream") or []
    env_ds = _env_downstream()
    if env_ds:
        # name-keyed merge: env entry replaces a same-named yaml entry, else appends
        by_name = {d["name"]: d for d in downstream}
        for d in env_ds:
            by_name[d["name"]] = d
        cfg["downstream"] = list(by_name.values())

    emb = cfg.setdefault("embeddings", {})
    for key, field in (("OLLAMA_URL", "ollama_url"), ("EMBED_MODEL", "model")):
        val = os.environ.get(f"MCP_SIEVE_{key}")
        if val:
            emb[field] = val
    if os.environ.get("MCP_SIEVE_TOP_N"):
        emb["top_n"] = int(os.environ["MCP_SIEVE_TOP_N"])
    return cfg


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.info("config.yaml not found (%s), relying on env vars", CONFIG_PATH)
        return _apply_env({"downstream": [], "embeddings": {}})
    log.info("loading config: %s", CONFIG_PATH)
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return _apply_env(cfg)


CFG = load_config()
EMBED_CFG = CFG.get("embeddings", {})
OLLAMA_URL = EMBED_CFG.get("ollama_url", "http://127.0.0.1:11434/api/embeddings")
EMBED_MODEL = EMBED_CFG.get("model", "nomic-embed-text")
TOP_N = EMBED_CFG.get("top_n", 10)
DOWNSTREAM = CFG.get("downstream", [])

# --- State -------------------------------------------------------------------


class RegisteredTool(NamedTuple):
    downstream: str
    orig_name: str
    tool: Tool


# ns_name → RegisteredTool
tool_registry: dict[str, RegisteredTool] = {}
# ns_name → embedding vector (np.ndarray)
embeddings_cache: dict[str, np.ndarray] = {}
# downstream_name → ClientSession
downstream_sessions: dict[str, ClientSession] = {}
# current tool list exposed via tools/list
current_tools: list[Tool] = []

# --- Embedding ---------------------------------------------------------------

_embed_client: httpx.AsyncClient | None = None


async def embed(text: str) -> np.ndarray:
    """Get text embedding via Ollama. ponytail: in-memory cache, no DB."""
    global _embed_client
    if _embed_client is None:
        _embed_client = httpx.AsyncClient(timeout=30)
    r = await _embed_client.post(OLLAMA_URL, json={"model": EMBED_MODEL, "prompt": text})
    r.raise_for_status()
    return np.array(r.json()["embedding"], dtype=np.float32)


def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    # ponytail: numpy — fine up to 1000 tools; beyond that use FAISS.
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))


# --- Helpers -----------------------------------------------------------------


def _text(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text)]


def _tool_dict(t: Tool) -> dict:
    """Single serialization point for Tool → dict in select responses."""
    return {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}


def _resolve(name: str) -> str | None:
    """Resolve a tool name: exact → strip mcp_router_ prefix → unique suffix match.

    ponytail: suffix match with uniqueness check — never silently picks the first.
    'time' would match '..._get_time', but if >1 match → None (ambiguous).
    """
    if name in tool_registry:
        return name
    if name.startswith("mcp_router_"):
        stripped = name[len("mcp_router_"):]
        if stripped in tool_registry:
            return stripped
    matches = [n for n in tool_registry if n.endswith(f"_{name}")]
    return matches[0] if len(matches) == 1 else None


async def _invoke(entry: RegisteredTool, args: dict) -> list[TextContent]:
    """Single point for calling a downstream session."""
    session = downstream_sessions.get(entry.downstream)
    if session is None:
        return _text(f"downstream session {entry.downstream} not available")
    try:
        result: CallToolResult = await session.call_tool(entry.orig_name, args)
        return result.content
    except Exception as e:
        log.exception("downstream call failed: %s/%s", entry.downstream, entry.orig_name)
        return _text(json.dumps({"error": f"downstream call failed: {e}"}, ensure_ascii=False))


# --- Downstream discovery ----------------------------------------------------

async def _open_transport(ds: dict, exit_stack: contextlib.AsyncExitStack):
    """Open the right transport for a downstream and return (read, write).

    ponytail: transport defaults to stdio; a bare `url` implies http.
    """
    name = ds["name"]
    transport = ds.get("transport") or ("http" if ds.get("url") else "stdio")
    if transport == "stdio":
        params = StdioServerParameters(command=ds["command"], args=ds.get("args", []), env=None)
        log.info("connecting downstream %s (stdio): %s %s", name, params.command, params.args)
        streams = await exit_stack.enter_async_context(stdio_client(params))
    elif transport in ("http", "streamable-http"):
        log.info("connecting downstream %s (http): %s", name, ds["url"])
        streams = await exit_stack.enter_async_context(streamablehttp_client(ds["url"]))
    elif transport == "sse":
        log.info("connecting downstream %s (sse): %s", name, ds["url"])
        streams = await exit_stack.enter_async_context(sse_client(ds["url"]))
    else:
        raise ValueError(f"unknown transport '{transport}' for downstream {name}")
    return streams[0], streams[1]  # streamablehttp yields a 3rd session-id getter, ignore it


async def _connect(ds: dict, exit_stack: contextlib.AsyncExitStack) -> ClientSession:
    """Open transport + ClientSession + initialize. Shared by discovery and reconnect."""
    read, write = await _open_transport(ds, exit_stack)
    session = await exit_stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


async def _register_tools(name: str, session: ClientSession) -> None:
    """Pull tools + build embeddings for one downstream. Idempotent across reconnects.

    ponytail: a server's tool set is stable — register once, skip on reconnect.
    """
    if any(e.downstream == name for e in tool_registry.values()):
        return  # already registered on a previous connect
    try:
        result = await session.list_tools()
    except Exception as e:
        log.error("failed to list tools from downstream %s: %s", name, e)
        return
    for tool in result.tools:
        ns_name = f"{name}_{tool.name}"
        ns_tool = Tool(name=ns_name, description=tool.description, inputSchema=tool.inputSchema)
        tool_registry[ns_name] = RegisteredTool(name, tool.name, ns_tool)
    try:
        for ns_name, entry in list(tool_registry.items()):
            if entry.downstream == name and ns_name not in embeddings_cache:
                embeddings_cache[ns_name] = await embed(f"{entry.tool.name}: {entry.tool.description or ''}")
        log.info("embeddings ready for %s (%d total)", name, len(embeddings_cache))
    except Exception as e:
        log.warning("embeddings failed for %s (%s), semantic search degraded", name, e)


# ponytail: fixed backoff cap; make configurable if flapping servers appear.
RECONNECT_BASE, RECONNECT_CAP, HEALTH_INTERVAL, STARTUP_TIMEOUT = 1.0, 30.0, 15.0, 60.0


async def _supervise(ds: dict, ready: asyncio.Event) -> None:
    """Keep one downstream connected; reconnect with exponential backoff on failure.

    ponytail: each supervisor owns its exit_stack so connect/teardown run in the
    same task — sidesteps anyio 'cancel scope in a different task'. Liveness via
    periodic send_ping; a raised ping means the transport died.
    """
    name = ds["name"]
    backoff = RECONNECT_BASE
    while True:
        try:
            async with contextlib.AsyncExitStack() as stack:
                session = await _connect(ds, stack)
                downstream_sessions[name] = session
                backoff = RECONNECT_BASE
                log.info("downstream %s connected", name)
                await _register_tools(name, session)
                ready.set()
                while True:
                    await asyncio.sleep(HEALTH_INTERVAL)
                    await session.send_ping()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            downstream_sessions.pop(name, None)
            ready.set()  # don't hold up startup on a server that won't connect
            log.warning("downstream %s down (%s); reconnecting in %.0fs", name, e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_CAP)


# --- MCP Server --------------------------------------------------------------

server: Server = Server("mcp-sieve")

ROUTER_SELECT_TOOL = Tool(
    name="mcp_router_select",
    description=(
        "REQUIRED first step before any task involving external tools. "
        "Pass your current task description; returns the relevant tools "
        "available for that task. Always call this first."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Natural language description of what you want to do.",
            }
        },
        "required": ["task"],
    },
)

ROUTER_CALL_TOOL = Tool(
    name="mcp_router_call",
    description=(
        "Execute a downstream tool by name. Use after mcp_router_select "
        "to find the right tool, then call this with the tool_name and "
        "arguments from the select response."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "tool_name": {
                "type": "string",
                "description": "Tool name from mcp_router_select response (e.g. 'time_get_current_time').",
            },
            "arguments": {
                "type": "object",
                "description": "Arguments object matching the tool's inputSchema.",
            },
        },
        "required": ["tool_name", "arguments"],
    },
)


@server.list_tools()
async def list_tools() -> list[Tool]:
    """Return the current tool list."""
    return current_tools


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[TextContent] | list[ImageContent] | list[EmbeddedResource]:
    """Tool call routing.

    ponytail: Hermes may pass a prefixed name (mcp_router_time_get_current_time).
    Strip mcp_router_ if the stripped name matches the registry or select.
    """
    args = arguments or {}

    # ponytail: insurance — if the client passes a prefixed name, strip it
    if name.startswith("mcp_router_"):
        stripped = name[len("mcp_router_"):]
        if stripped in ("mcp_router_select", "mcp_router_call") or stripped in tool_registry:
            name = stripped

    if name == "mcp_router_select":
        return await _handle_select(args.get("task", ""))

    if name == "mcp_router_call":
        tool_name = args.get("tool_name", "")
        tool_args = args.get("arguments", {})
        if isinstance(tool_args, str):
            try:
                tool_args = json.loads(tool_args)
            except Exception as e:
                return _text(json.dumps({"error": f"failed to parse arguments JSON: {e}"}, ensure_ascii=False))

        if not tool_name:
            return _text(json.dumps({"error": "tool_name is required"}, ensure_ascii=False))

        resolved = _resolve(tool_name)
        if resolved is None:
            return _text(json.dumps({"error": f"unknown tool: {tool_name}"}, ensure_ascii=False))
        return await _invoke(tool_registry[resolved], tool_args)

    # Direct downstream call by registry name
    resolved = _resolve(name)
    if resolved is not None:
        return await _invoke(tool_registry[resolved], args)

    return _text(json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False))


async def _handle_select(task: str) -> list[TextContent]:
    """Core: semantic search for tools matching the task."""
    if not task:
        return _text('{"error": "task is required"}')

    if not tool_registry:
        return _text(json.dumps({
            "info": "no downstream tools registered yet",
            "available_tools": [t.name for t in current_tools],
        }, ensure_ascii=False))

    if not embeddings_cache:
        # no embeddings — return all tools, don't break the flow
        all_ns_tools = [entry.tool for entry in tool_registry.values()]
        _set_current_tools(all_ns_tools)
        return _text(json.dumps({
            "warning": "embeddings unavailable; returning all tools",
            "tools": [_tool_dict(t) for t in all_ns_tools],
            "hint": "Use mcp_router_call(tool_name=<name>, arguments=<args>) to execute any of these tools.",
        }, ensure_ascii=False))

    q_vec = await embed(task)
    scored: list[tuple[float, Tool]] = []
    for ns_name, vec in embeddings_cache.items():
        entry = tool_registry[ns_name]
        scored.append((cosine_sim(q_vec, vec), entry.tool))
    scored.sort(key=lambda x: x[0], reverse=True)

    top = [t for _, t in scored[:TOP_N]]
    _set_current_tools(top)

    # notify the client (best-effort, only works inside a request context)
    try:
        ctx = server.request_context
        await ctx.session.send_tool_list_changed()
        log.info("sent tools/list_changed, now exposing %d tools", len(top))
    except (LookupError, AttributeError) as e:
        log.warning("cannot send list_changed outside active request: %s", e)
    except Exception as e:
        log.warning("could not send list_changed: %s", e)

    payload = {
        "selected_tools": [_tool_dict(t) for t in top],
        "hint": "Use mcp_router_call(tool_name=<name>, arguments=<args>) to execute any of these tools.",
    }
    return _text(json.dumps(payload, ensure_ascii=False))


def _set_current_tools(tools: list[Tool]) -> None:
    """Update current_tools. Always keep mcp_router_select and mcp_router_call first."""
    global current_tools
    others = [t for t in tools if t.name not in ("mcp_router_select", "mcp_router_call")]
    current_tools = [ROUTER_SELECT_TOOL, ROUTER_CALL_TOOL] + others


# --- Entrypoint --------------------------------------------------------------

async def main_async() -> None:
    _set_current_tools([])

    ready = [asyncio.Event() for _ in DOWNSTREAM]
    supervisors = [
        asyncio.create_task(_supervise(ds, ev), name=f"supervise-{ds['name']}")
        for ds, ev in zip(DOWNSTREAM, ready)
    ]
    # Wait for the first connect attempt of each downstream so tools/list isn't
    # empty on the first select — bounded so one slow server can't stall startup.
    if ready:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*(e.wait() for e in ready)), timeout=STARTUP_TIMEOUT
            )

    log.info("starting mcp-sieve on stdio, %d downstream connected, %d tools registered",
             len(downstream_sessions), len(tool_registry))
    try:
        async with stdio_server() as (read, write):
            await server.run(
                read,
                write,
                server.create_initialization_options(NotificationOptions(tools_changed=True)),
            )
    finally:
        for t in supervisors:
            t.cancel()
        await asyncio.gather(*supervisors, return_exceptions=True)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()