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
from mcp.client.stdio import stdio_client
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


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.warning("config.yaml not found (%s), using empty downstream", CONFIG_PATH)
        return {"downstream": [], "embeddings": {}}
    log.info("loading config: %s", CONFIG_PATH)
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


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

async def discover_downstream_tools(exit_stack: contextlib.AsyncExitStack) -> None:
    """Connect to all downstream MCP servers, pull tools, build embeddings.

    ponytail: all stdio connections stay open via exit_stack for the whole lifetime.
    """
    for ds in DOWNSTREAM:
        name = ds["name"]
        try:
            params = StdioServerParameters(
                command=ds["command"],
                args=ds.get("args", []),
                env=None,
            )
            log.info("connecting downstream %s: %s %s", name, params.command, params.args)
            read, write = await exit_stack.enter_async_context(stdio_client(params))
            session = await exit_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            downstream_sessions[name] = session
            log.info("downstream %s connected", name)
        except Exception as e:
            log.error("failed to connect downstream %s: %s", name, e)
            continue

    # Pull tools and build embeddings
    for name, session in downstream_sessions.items():
        try:
            result = await session.list_tools()
            for tool in result.tools:
                ns_name = f"{name}_{tool.name}"
                ns_tool = Tool(
                    name=ns_name,
                    description=tool.description,
                    inputSchema=tool.inputSchema,
                )
                tool_registry[ns_name] = RegisteredTool(name, tool.name, ns_tool)
        except Exception as e:
            log.error("failed to list tools from downstream %s: %s", name, e)

    if not tool_registry:
        log.warning("no downstream tools registered")
        return

    try:
        for ns_name, entry in list(tool_registry.items()):
            text = f"{entry.tool.name}: {entry.tool.description or ''}"
            embeddings_cache[ns_name] = await embed(text)
        log.info("embeddings ready: %d tools", len(embeddings_cache))
    except Exception as e:
        log.warning("embeddings failed (%s), running without semantic search", e)
        embeddings_cache.clear()


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

    async with contextlib.AsyncExitStack() as exit_stack:
        await discover_downstream_tools(exit_stack)

        log.info("starting mcp-sieve on stdio, %d downstream connected, %d tools registered",
                 len(downstream_sessions), len(tool_registry))
        async with stdio_server() as (read, write):
            await server.run(
                read,
                write,
                server.create_initialization_options(NotificationOptions(tools_changed=True)),
            )


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()