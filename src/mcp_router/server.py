"""MCP Router — semantic tool selection proxy.

Путь 1 (notifications/tools/list_changed):
  1. tools/list отдаёт 1 tool: mcp_router_select
  2. LLM вызывает mcp_router_select(task="...") → router ищет top-N tools
  3. Router обновляет current_tools + посылает notifications/tools/list_changed
  4. tools/list отдаёт уже релевантные tools
  5. LLM вызывает tool напрямую → router проксирует на downstream

ponytail: один файл, всё в нём. Refactor когда стабильно.
"""
import asyncio
import contextlib
import json
import logging
from pathlib import Path

import httpx
import numpy as np
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, EmbeddedResource, ImageContent, TextContent, Tool

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("mcp-router")

# --- Config ------------------------------------------------------------------

# src/mcp_router/server.py → project root = parent.parent.parent
CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.yaml"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.warning("config.yaml не найден, использую пустой downstream")
        return {"downstream": [], "embeddings": {}}
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


CFG = load_config()
EMBED_CFG = CFG.get("embeddings", {})
OLLAMA_URL = EMBED_CFG.get("ollama_url", "http://127.0.0.1:11434/api/embeddings")
EMBED_MODEL = EMBED_CFG.get("model", "nomic-embed-text")
TOP_N = EMBED_CFG.get("top_n", 10)
DOWNSTREAM = CFG.get("downstream", [])

# --- State -------------------------------------------------------------------

# tool_name → (downstream_name, Tool)
tool_registry: dict[str, tuple[str, Tool]] = {}
# tool_name → embedding vector (np.ndarray)
embeddings_cache: dict[str, np.ndarray] = {}
# downstream_name → ClientSession
downstream_sessions: dict[str, ClientSession] = {}
# текущий список tools для tools/list
current_tools: list[Tool] = []

# --- Embedding ---------------------------------------------------------------

_embed_client: httpx.AsyncClient | None = None


async def embed(text: str) -> np.ndarray:
    """Получить embedding текста через Ollama. ponytail: кэш в памяти, без БД."""
    global _embed_client
    if _embed_client is None:
        _embed_client = httpx.AsyncClient(timeout=30)
    r = await _embed_client.post(OLLAMA_URL, json={"model": EMBED_MODEL, "prompt": text})
    r.raise_for_status()
    return np.array(r.json()["embedding"], dtype=np.float32)


def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    # ponytail: numpy — до 1000 tools scan норм; больше — FAISS.
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))


# --- Downstream discovery ----------------------------------------------------

async def discover_downstream_tools(exit_stack: contextlib.AsyncExitStack) -> None:
    """Коннектится ко всем downstream MCP-серверам, тянет tools, строит embeddings.

    ponytail: все stdio-соединения держим открытыми через exit_stack на всё время работы.
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

    # Тянем tools и строим embeddings
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
                tool_registry[ns_name] = (name, tool.name, ns_tool)
        except Exception as e:
            log.error("failed to list tools from downstream %s: %s", name, e)

    if not tool_registry:
        log.warning("no downstream tools registered")
        return

    try:
        for ns_name, (ds_name, orig_name, ns_tool) in list(tool_registry.items()):
            text = f"{ns_tool.name}: {ns_tool.description or ''}"
            embeddings_cache[ns_name] = await embed(text)
        log.info("embeddings ready: %d tools", len(embeddings_cache))
    except Exception as e:
        log.warning("embeddings failed (%s), running without semantic search", e)
        embeddings_cache.clear()


# --- MCP Server --------------------------------------------------------------

server: Server = Server("mcp-router")

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


@server.list_tools()
async def list_tools() -> list[Tool]:
    """Отдаём текущий список tools."""
    return current_tools


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[TextContent] | list[ImageContent] | list[EmbeddedResource]:
    """Маршрутизация вызова tool.

    ponytail: Hermes может передать prefixed name (mcp_router_time_get_current_time).
    Стрипаем mcp_router_ если после stripping имя матчится с registry или select.
    """
    args = arguments or {}

    # ponytail: insurance — если клиент передаёт prefixed name, стрипаем
    if name.startswith("mcp_router_"):
        stripped = name[len("mcp_router_"):]
        if stripped == "mcp_router_select" or stripped in tool_registry:
            name = stripped

    if name == "mcp_router_select":
        return await _handle_select(args.get("task", ""))

    if name in tool_registry:
        ds_name, orig_name, ns_tool = tool_registry[name]
        session = downstream_sessions.get(ds_name)
        if session is None:
            return _text(f"downstream session {ds_name} not available")
        try:
            result: CallToolResult = await session.call_tool(orig_name, args)
            return result.content
        except Exception as e:
            log.exception("downstream call failed: %s/%s", ds_name, orig_name)
            return _text(json.dumps({"error": f"downstream call failed: {e}"}, ensure_ascii=False))

    return _text(json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False))


def _text(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text)]


async def _handle_select(task: str) -> list[TextContent]:
    """Ядро: семантический поиск tools по задаче."""
    if not task:
        return _text('{"error": "task is required"}')

    if not tool_registry:
        return _text(json.dumps({
            "info": "no downstream tools registered yet",
            "available_tools": [t.name for t in current_tools],
        }, ensure_ascii=False))

    if not embeddings_cache:
        # нет embeddings — отдаём все tools, не ломая flow
        _set_current_tools([ns_tool for _, _, ns_tool in tool_registry.values()])
        return _text(json.dumps({
            "warning": "embeddings unavailable; returning all tools",
            "tools": [{"name": t.name, "description": t.description} for t in current_tools],
        }, ensure_ascii=False))

    q_vec = await embed(task)
    scored: list[tuple[float, str, Tool]] = []
    for ns_name, vec in embeddings_cache.items():
        ds_name, orig_name, ns_tool = tool_registry[ns_name]
        scored.append((cosine_sim(q_vec, vec), ds_name, ns_tool))
    scored.sort(key=lambda x: x[0], reverse=True)

    top = [t for _, _, t in scored[:TOP_N]]
    _set_current_tools(top)

    # уведомляем клиента (best-effort, работает только внутри request context)
    try:
        ctx = server.request_context
        await ctx.session.send_tool_list_changed()
        log.info("sent tools/list_changed, now exposing %d tools", len(top))
    except (LookupError, AttributeError) as e:
        log.warning("cannot send list_changed outside active request: %s", e)
    except Exception as e:
        log.warning("could not send list_changed: %s", e)

    payload = {
        "selected_tools": [{"name": t.name, "description": t.description} for t in top],
        "hint": "tools/list has been updated — call them directly now.",
    }
    return _text(json.dumps(payload, ensure_ascii=False))


def _set_current_tools(tools: list[Tool]) -> None:
    """Обновить current_tools. Всегда держим mcp_router_select первым."""
    global current_tools
    others = [t for t in tools if t.name != "mcp_router_select"]
    current_tools = [ROUTER_SELECT_TOOL] + others


# --- Entrypoint --------------------------------------------------------------

async def main_async() -> None:
    _set_current_tools([])

    async with contextlib.AsyncExitStack() as exit_stack:
        await discover_downstream_tools(exit_stack)

        log.info("starting mcp-router on stdio, %d downstream connected, %d tools registered",
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
