# TASKS — MCP Router

Цели с измеримым результатом. Каждая задача = чекпоинт, который можно проверить.

## Этап 0 — Скелет ✅

- [x] pyproject.toml, config.yaml, структура пакетов
- [x] MCP-сервер на stdio отвечает на initialize/tools/list/tools/call
- [x] `listChanged: true` capability заявлена
- [x] `tools/list` отдаёт ровно 1 tool: `mcp_router_select`
- [x] Fallback при отсутствии Ollama (возвращает все tools, не падает)
- [x] Smoke-test через JSON-RPC проходит

**Результат:** `python -m mcp_router.server` стартует, отвечает на 3 RPC-запроса корректно.

## Этап 1 — Downstream discovery ✅

- [x] `discover_downstream_tools()` спавнит subprocess каждого downstream MCP-сервера из config.yaml
- [x] Через `mcp.Client` подключается, вызывает `list_tools()`
- [x] Заполняет `tool_registry: {tool_name → (downstream_name, Tool)}`
- [x] Сохраняет downstream клиенты в `downstream_procs` для дальнейшей маршрутизации
- [x] Обработка падения одного downstream не роняет остальные

**Результат:** при старте с 1 downstream (mcp-server-time) `tools/list` через `mcp_router_select` возвращает реальные tools (например `time_get_current_time`).

## Этап 2 — Embeddings + семантический поиск ✅

- [x] `ollama pull nomic-embed-text` (или другая embed-модель)
- [x] При discovery: каждый tool description → embedding → `embeddings_cache`
- [x] `mcp_router_select(task)` → embedding(task) → cosine sim по всем tools → top-N
- [x] Отправляется `notifications/tools/list_changed` → список обновляется до top-N
- [x] Скоринг логируется (для дебага)

**Результат:** `mcp_router_select(task="get current time in UTC")` → возвращает `get_current_time` с score > 0.5, не возвращает нерелевантные tools.

## Этап 3 — Маршрутизация вызовов ✅

- [x] `tools/call(name=... )` для реального tool → проксируется на downstream клиент
- [x] Результат downstream возвращается LLM как есть
- [x] Unknown tool → понятная ошибка
- [x] Downstream упал во время вызова → retry / понятная ошибка

**Результат:** LLM вызывает `get_current_time` через router → получает реальное время.

## Этап 4 — Интеграция с Hermes

- [ ] `hermes mcp add mcp-router --command python --args "-m mcp_router.server"`
- [ ] `/reset` → Hermes видит только `mcp_router_mcp_router_select` (1 tool от роутера)
- [ ] Юзер: "сколько времени" → LLM вызывает select → получает time tools → вызывает → получает время
- [ ] Логи Hermes показывают `notifications/tools/list_changed` обработан

**Результат:** end-to-end workflow работает внутри Hermes. Один downstream (time), одна задача.

## Этап 5 — Нагрузка и масштаб

- [ ] Подключить 3+ downstream серверов (filesystem, git, web-search)
- [ ] Проверить что с 30+ tools семантический поиск работает (точность top-10)
- [ ] Замерить latency: select + re-list < 2 секунды
- [ ] Бенчмарк: сравнить с baseline (все tools в system prompt) — LLM точнее выбирает?

**Результат:** с 30+ tools от 3 downstream router отдаёт top-10 релевантных за < 2с, LLM не тонет в описаниях.

## Этап 6 — Релиз (опционально)

- [ ] README с инструкцией установки
- [ ] Поддержка HTTP transport для downstream
- [ ] Конфиг через env vars (не только yaml)
- [ ] Запостить на GitHub + r/mcp или r/ClaudeAI

**Результат:** публичный репозиторий, кто-то другой может установить за `uvx mcp-router`.

---

## Критерии успеха (измеримые)

| Метрика | Цель |
|---------|------|
| Downstream tools, при которых router имеет смысл | ≥ 20 |
| Latency select + re-list | < 2с |
| Точность top-10 (релевантный tool в выдаче) | ≥ 90% |
| Кол-во tools в system prompt LLM | 1 (всегда) до select, ≤ 10 после |
| Память (embeddings in-memory) | < 50MB для 100 tools |