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

| Client | Path | Notes |
|--------|------|-------|
| Hermes | Path 2 (proxy) | Hermes loads a frozen toolset at startup — use `mcp_router_select` + `mcp_router_call`. |
| Claude Code | Path 1 or 2 | Without prompt caching: supports `tools/list_changed` (Path 1). With caching: behaves like Path 2. |
| Custom MCP client | Path 1 | If your client handles `notifications/tools/list_changed`, the relevant tools appear in `tools/list` after `select`. |

- **Path 1 (notifications/tools/list_changed):** `select` finds tools → sieve updates `tools/list` → client calls tools directly.
- **Path 2 (mcp_router_call proxy):** for clients with a **frozen toolset**. `select` returns tools with `inputSchema` → `call` proxies execution. No `/reset` needed when new downstream tools are discovered.

## Install

```bash
pip install mcp-sieve
# or
uvx mcp-sieve          # run without installing
```

### Ollama setup

Sieve needs a **running** Ollama instance at startup. Model download alone is not enough.

```bash
ollama pull nomic-embed-text
ollama serve            # start the daemon (or enable the systemd service)
```

If Ollama is unreachable when sieve starts, it does **not** crash — it falls back to returning all downstream tools unordered (see [Fallback](#fallback)). For the semantic router to actually rank tools, Ollama must be alive before the first `mcp_router_select` call.

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

## Connect to Hermes

```bash
hermes mcp add sieve --command uvx --args "mcp-sieve"
hermes mcp test sieve
# /reset in chat
```

> **Always set `MCP_ROUTER_CONFIG`** when running via `uvx`. Because `uvx` installs the package into an isolated cache, sieve cannot find your `config.yaml` next to the source without this variable. This applies to **all OSes**, not just Windows.

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

**Security:** avoid committing tokens. `config.yaml` is gitignored by default. For secrets (GitHub tokens, API keys), pass them as environment variables to the sieve process itself — your MCP client (`claude_desktop_config.json`, `~/.claude.json`, or `hermes mcp add ... --env`) propagates them to downstream servers.

## Using sieve

1. Call `mcp_router_select` with a natural language task:
   ```json
   {"task": "list open issues in facebook/react"}
   ```
   It returns up to `top_n` relevant tools with their `inputSchema`.

2. Pick the tool you need and call `mcp_router_call`:
   ```json
   {
     "tool_name": "github_list_issues",
     "arguments": {
       "owner": "facebook",
       "repo": "react",
       "state": "open",
       "per_page": 5
     }
   }
   ```

Important:
- `tool_name` must be exactly the name from `selected_tools` (e.g. `github_list_issues`, not `mcp_router_github_list_issues`).
- `arguments` must match the `inputSchema` of that tool. If validation fails, the downstream server's error is forwarded unchanged.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `mcp_router_select` returns empty list or all tools unordered | Ollama is down or unreachable | Start `ollama serve` and restart sieve/gateway |
| `unknown tool: <name>` | Wrong `tool_name` in `mcp_router_call` | Copy the exact name from `selected_tools` |
| `downstream call failed: Invalid input...` | Arguments don't match `inputSchema` | Check required fields in the schema returned by `select` |
| Client sees only `mcp_router_select` / `mcp_router_call` | Client needs a tool-list refresh | In Hermes: `/reset` or restart gateway; in Claude Code: restart |
| Some downstream tools are missing | That downstream failed to connect | Check `mcp-stderr.log` for npm/uvx errors; sieve keeps retrying |

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

If Ollama is unreachable when sieve builds embeddings at **startup**, `mcp_router_select` returns **all** downstream tools without ranking, plus a `warning` field. If Ollama becomes reachable later, restart sieve to build embeddings; the in-memory cache is not backfilled on the fly.

## Performance

Tested with 10 downstream servers (79 tools):
- `mcp_router_select`: 83–166ms
- `mcp_router_call`: 15–774ms (longest: playwright browser navigation)
- Startup: ~16–18s (all 10 downstream connect + 79 embeddings built)

## Status

Working end-to-end in Claude Code and Hermes. See `TASKS.md` for the roadmap and benchmark results.