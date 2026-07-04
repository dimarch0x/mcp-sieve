# MCP Router

Семантический прокси для MCP-серверов. Решает проблему **tool selection degradation** — когда у LLM слишком много инструментов, она хуже выбирает нужный.

Роутер стоит между клиентом (Claude Code, Hermes, любой MCP-клиент) и downstream MCP-серверами. Клиент видит **2 инструмента** вместо десятков: `mcp_router_select` + `mcp_router_call`. Первый находит релевантные tools через embeddings, второй проксирует вызов.

## Как работает

```
Клиент (Claude Code / Hermes)
  ↓ видит только 2 инструмента
mcp_router_select(task="...")  →  embeddings → top-N релевантных tools
mcp_router_call(tool_name, arguments)  →  проксирует на downstream
  ↓
downstream MCP-серверы (time, fetch, context7, filesystem, ...)
```

**Два пути вызова:**

1. **Путь 1 (notifications/tools/list_changed):** `select` находит tools → роутер обновляет `tools/list` → клиент вызывает tools напрямую. Работает с клиентами, поддерживающими динамическое обновление toolset.

2. **Путь 2 (mcp_router_call proxy):** для клиентов с **замороженным toolset** (Hermes, Claude Code с prompt caching). `select` отдаёт tools с `inputSchema` → `call` проксирует выполнение. Не требует `/reset` при обнаружении новых downstream-тулов.

## Установка

```bash
git clone <repo> ~/Projects/mcp-router
cd ~/Projects/mcp-router
uv pip install -e .
```

Нужен [Ollama](https://ollama.com) с embed-моделью:
```bash
ollama pull nomic-embed-text
```

## Запуск (standalone)

```bash
python -m mcp_router.server
```

Сервер слушает stdio (JSON-RPC).

## Подключение к Claude Code

В `~/.claude.json` → `projects["<path>"].mcpServers`:

```json
"router": {
  "type": "stdio",
  "command": "uvx",
  "args": ["--from", "C:\\Users\\<user>\\Projects\\mcp-router", "mcp-router"],
  "env": {
    "MCP_ROUTER_CONFIG": "C:\\Users\\<user>\\Projects\\mcp-router\\config.yaml"
  }
}
```

Или через CLI:
```bash
claude mcp add router -- uvx --from /path/to/mcp-router mcp-router
```

> **Windows:** `MCP_ROUTER_CONFIG` обязателен — `uvx` ставит пакет в изолированный venv, `__file__` указывает в uv-cache. См. [Windows-нюансы](#windows-нюансы).

## Подключение к Hermes

```bash
hermes mcp add router --command uvx --args "--from" --args "/path/to/mcp-router" --args "mcp-router"
hermes mcp test router
# /reset в чате
```

## Конфиг

`config.yaml`:

```yaml
downstream:
  - name: time
    command: uvx
    args: ["mcp-server-time"]

  - name: fetch
    command: uvx
    args: ["mcp-server-fetch"]

  # Windows: npx это .cmd — нужен cmd /c
  - name: context7
    command: cmd
    args: ["/c", "npx", "-y", "@upstash/context7-mcp@latest"]

  - name: filesystem
    command: cmd
    args: ["/c", "npx", "-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"]

embeddings:
  ollama_url: "http://127.0.0.1:11434/api/embeddings"
  model: "nomic-embed-text"
  top_n: 10
```

Env-переменные:
- `MCP_ROUTER_CONFIG` — путь к `config.yaml` (иначе ищет в CWD или рядом с исходником)

## Windows-нюансы

1. **npx → `cmd /c npx`:** `npx` это `.cmd`-файл, Python subprocess (MCP SDK) не находит его без shell. `uvx` — настоящий бинарник, работает напрямую.

2. **uvx --from и dependencies:** `uvx --from <project>` ставит пакет в изолированный uv-cache venv. Все импорты должны быть в `pyproject.toml` `[project.dependencies]` — неявные deps из dev-окружения не подхватятся.

3. **uv cache clean:** если cache занят (`os error 32`), убить процессы MCP-серверов:
   ```bash
   powershell -Command "Get-Process | Where-Object { $_.ProcessName -match 'mcp|uv' } | Stop-Process -Force"
   uv cache clean --force
   ```

4. **Дебаг подключения:** `claude --debug` пишет в `~/.claude/debug/<session>.txt`. Grep `Server stderr:` — реальные traceback'и сервера.

## Стек

- **MCP Python SDK** (`mcp`) — stdio transport, `notifications/tools/list_changed`
- **Ollama** — локальные embeddings (`nomic-embed-text`), бесплатно
- **numpy** — cosine similarity
- **httpx** — HTTP-клиент для Ollama API

## Fallback

Если Ollama недоступен — роутер не падает. `mcp_router_select` отдаёт **все** downstream tools без ранжирования, с warning в ответе.

## Статус

Этапы 0–4 завершены. Роутер работает end-to-end в Claude Code и Hermes: 4 downstream-сервера, 19 инструментов, семантический роутинг, `mcp_router_call` proxy.

См. `TASKS.md` для roadmap (нагрузка, бенчмарк, релиз).