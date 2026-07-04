# GOAL — MCP Router

## Одной строкой

Собрать рабочий MCP Router — семантический прокси для MCP-инструментов, который прячет все downstream tools за одним meta-tool `mcp_router_select` и динамически подсовывает LLM ≤10 релевантных tools через `notifications/tools/list_changed`.

## Scope этой сессии

Довести проект до **Этапа 4 (end-to-end в Hermes)**:
1. Downstream discovery — реальное подключение mcp-server-time
2. Embeddings — семантический поиск через Ollama/nomic-embed-text
3. Маршрутизация — `tools/call` проксируется на downstream
4. Интеграция — router работает внутри Hermes, юзер получает время через select

Этапы 5 (нагрузка/бенчмарк) и 6 (релиз) — не в этой сессии.

## Измеримые критерии успеха

| Что проверяем | Цель |
|---------------|------|
| `hermes mcp test mcp-router` | подключается без ошибок |
| Hermes видит tools | только 1 tool: `mcp_router_mcp_router_select` |
| Юзер: "сколько времени?" | LLM вызывает select → получает time tools → вызывает → получает реальное время |
| Latency select + re-list | < 2 секунд |
| Точность top-N | `get_current_time` в выдаче при запросе времени |
| Fallback | если Ollama упал, router не падает, возвращает все tools |

## Что делаем

- stdio-коннект к downstream MCP-серверам через `mcp.Client`
- Кэш embeddings в памяти, cosine similarity, top-N
- Динамическая подмена `tools/list` через `notifications/tools/list_changed`
- Проксирование `tools/call` на правильный downstream сервер

## Что НЕ делаем

- Web UI / админка
- HTTP transport для downstream
- Авто-реконнект упавших серверов
- Публичный релиз / упаковку в PyPI
- Бенчмарки на 30+ tools

## Кому пригодится

Любому, кто использует Hermes/Claude с 3+ MCP-серверами и замечает, что LLM начинает тупить в выборе tools.