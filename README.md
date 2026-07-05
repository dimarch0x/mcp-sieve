# MCP Sieve

A semantic proxy for MCP servers. Solves **tool selection degradation** — when an LLM has too many tools, it picks the wrong ones.

The sieve sits between the client (Claude Code, Hermes, any MCP client) and downstream MCP servers. The client sees **2 tools** instead of dozens: `mcp_router_select` + `mcp_router_call`. The first finds relevant tools via embeddings, the second proxies the call.

## How it works

```
Client (Claude Code / Hermes)
  ↓ sees only 2 tools
mcp_router_select(task="...")  →  embeddings → top-N relevant tools
mcp_router_call(tool_name, arguments)  →  proxies to downstream
  ↓
downstream MCP servers (time, fetch, git, arxiv, playwright, ...)
```

**Two call paths:**

1. **Path 1 (notifications/tools/list_changed):** `select` finds tools → sieve updates `tools/list` → client calls tools directly. Works with clients that support dynamic toolset updates.

2. **Path 2 (mcp_router_call proxy):** for clients with a **frozen toolset** (Hermes, Claude Code with prompt caching). `select` returns tools with `inputSchema` → `call` proxies execution. No `/reset` needed when new downstream tools are discovered.

## Install

```bash
pip install mcp-sieve
# or
uvx mcp-sieve          # run without installing
```

Requires [Ollama](https://ollama.com) with an embed model:
```bash
ollama pull nomic-embed-text
```

## Quick start

Copy the example config and edit it:
```bash
cp config.example.yaml config.yaml
# edit config.yaml — add your downstream servers and paths
```

Run standalone:
```bash
python -m mcp_router.server
```
Server listens on stdio (JSON-RPC).

## Connect to Claude Code

In `~/.claude.json` → `projects["<path>"].mcpServers`:

```json
"sieve": {
  "type": "stdio",
  "command": "uvx",
  "args": ["mcp-sieve"],
  "env": {
    "MCP_ROUTER_CONFIG": "/path/to/config.yaml"
  }
}
```

Or via CLI:
```bash
claude mcp add sieve -- uvx mcp-sieve
```

> **Windows:** `MCP_ROUTER_CONFIG` is required — `uvx` installs the package into an isolated venv, `__file__` points into uv-cache. See [Windows notes](#windows-notes) below.

## Connect to Hermes

```bash
hermes mcp add sieve --command uvx --args "mcp-sieve"
hermes mcp test sieve
# /reset in chat
```

## Config

`config.yaml` (see `config.example.yaml` for a full template):

```yaml
downstream:
  - name: time
    command: uvx
    args: ["mcp-server-time"]

  - name: fetch
    command: uvx
    args: ["mcp-server-fetch"]

  - name: git
    command: uvx
    args: ["mcp-server-git", "--repository", "/path/to/your/repo"]

  # Windows: npx is a .cmd file — needs cmd /c
  - name: context7
    command: cmd
    args: ["/c", "npx", "-y", "@upstash/context7-mcp@latest"]

  - name: filesystem
    command: cmd
    args: ["/c", "npx", "-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"]

  # Remote MCP over HTTP (streamable) or SSE — no local process.
  # transport defaults to stdio; a bare url implies http.
  - name: gitmcp
    transport: http
    url: "https://gitmcp.io/docs"

embeddings:
  ollama_url: "http://127.0.0.1:11434/api/embeddings"
  model: "nomic-embed-text"
  top_n: 10
```

Env variables:
- `MCP_ROUTER_CONFIG` — path to `config.yaml` (otherwise looks in CWD or next to source)
- `MCP_SIEVE_DOWNSTREAM_<N>_NAME` / `_COMMAND` / `_ARGS` / `_URL` / `_TRANSPORT` — define downstream servers without a file (Docker/k8s). `N` starts at 1, stops at the first gap. `_ARGS` is a JSON array or whitespace-split. A same-named entry overrides the yaml one.
- `MCP_SIEVE_OLLAMA_URL` / `MCP_SIEVE_EMBED_MODEL` / `MCP_SIEVE_TOP_N` — embeddings overrides

Crashed downstream servers (Ollama, npx) auto-reconnect with exponential backoff — no restart needed.

## Windows notes

1. **npx → `cmd /c npx`:** `npx` is a `.cmd` file, Python subprocess (MCP SDK) can't find it without a shell. `uvx` is a real binary, works directly.

2. **uvx isolated venv:** `uvx mcp-sieve` installs into an isolated uv-cache venv. All imports must be in `pyproject.toml` `[project.dependencies]` — implicit deps from the dev env won't be picked up.

3. **uv cache clean:** if the cache is locked (`os error 32`), kill MCP server processes first:
   ```bash
   powershell -Command "Get-Process | Where-Object { $_.ProcessName -match 'mcp|uv' } | Stop-Process -Force"
   uv cache clean --force
   ```

4. **Debug connection failures:** `claude --debug` writes to `~/.claude/debug/<session>.txt`. Grep `Server stderr:` for real server tracebacks.

## Stack

- **MCP Python SDK** (`mcp`) — stdio + HTTP/SSE transports, `notifications/tools/list_changed`
- **Ollama** — local embeddings (`nomic-embed-text`), free
- **numpy** — cosine similarity
- **httpx** — HTTP client for Ollama API

## Fallback

If Ollama is unavailable — the sieve doesn't crash. `mcp_router_select` returns **all** downstream tools without ranking, with a warning in the response.

## Performance

Tested with 9 downstream servers (74 tools):
- `mcp_router_select`: 83–166ms
- `mcp_router_call`: 15–774ms (longest: playwright browser navigation)
- Startup: ~16s (all 9 downstream connect + 74 embeddings built)

## Status

Working end-to-end in Claude Code and Hermes. See `TASKS.md` for the roadmap and benchmark results.