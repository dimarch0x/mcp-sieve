# MCP Router

Семантический прокси для MCP-серверов. Решает проблему tool selection degradation — когда у LLM слишком много tools, она хуже выбирает нужный.

## Как работает

Router стоит между клиентом (Hermes, Claude) и downstream MCP-серверами. LLM видит **один** tool: `mcp_router_select`. Перед задачей LLM вызывает его, router через embeddings находит top-N релевантных tools и динамически обновляет список через `notifications/tools/list_changed`.

```
LLM → mcp_router_select(task="...") → embeddings → top-N tools
                                                         ↓
                                    notifications/tools/list_changed
                                                         ↓
                                    LLM вызывает tools напрямую через router
```

## Установка

```bash
cd ~/projects/mcp-router
uv pip install -e .
```

## Запуск (standalone)

```bash
python -m mcp_router.server
```

Сервер слушает stdio (JSON-RPC).

## Подключение к Hermes

```bash
hermes mcp add mcp-router --command python --args "-m mcp_router.server"
hermes mcp test mcp-router
# /reset в чате
```

## Конфиг

`config.yaml`:
```yaml
downstream:
  - name: time
    command: uvx
    args: ["mcp-server-time"]

embeddings:
  ollama_url: "http://127.0.0.1:11434/api/embeddings"
  model: "nomic-embed-text"
  top_n: 10
```

## Стек

- MCP Python SDK (`mcp`)
- Ollama embeddings (локально, бесплатно)
- numpy (cosine similarity)
- stdlib (sqlite3 для кэша — планируется)

## Статус

Этап 0 (скелет) готов. См. `TASKS.md` для roadmap.