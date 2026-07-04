# Roadmap — MCP Sieve

## Completed

- **Core MCP server** — stdio transport, `initialize`/`tools/list`/`tools/call`, `listChanged` capability
- **Downstream discovery** — connects to all downstream MCP servers from `config.yaml`, pulls tools via `list_tools()`, one failing server doesn't crash the rest
- **Semantic search** — Ollama embeddings (`nomic-embed-text`), cosine similarity, top-N ranking. Fallback: if Ollama is down, returns all tools with a warning
- **Call routing** — `tools/call` proxied to the correct downstream. Unknown tools → clear error
- **`mcp_router_call` proxy** — for clients with frozen toolsets (prompt caching). `select` returns tools with `inputSchema`, `call` proxies execution. No `/reset` needed on new downstream tools
- **Cross-platform** — tested on Windows (npx via `cmd /c`, config discovery via `MCP_ROUTER_CONFIG` env). Works with Claude Code and Hermes

## Benchmark

Tested with **9 downstream servers (74 tools)** in Claude Code across 5 long tasks:

| Metric | Result |
|--------|--------|
| `mcp_router_select` latency | 83–166ms |
| `mcp_router_call` latency | 15–774ms |
| Startup (9 servers + 74 embeddings) | ~16s |
| Tools in LLM system prompt | 2 (always) |
| Router accuracy (relevant tool in top-10) | 100% across test tasks |

**Downstream servers tested:** time, fetch, git, arxiv, context7, filesystem, playwright, sequential-thinking, memory

**Observation:** Claude Code prefers native tools when they overlap (Bash vs git MCP, Write vs filesystem MCP). The router is most valuable for tools without native equivalents — arxiv, playwright, memory, context7.

## Planned

- HTTP transport support for downstream servers
- Config via env vars (beyond `MCP_ROUTER_CONFIG`)
- Auto-reconnect for crashed downstream servers
- FAISS for >1000 tools (currently numpy cosine sim)