#!/usr/bin/env bash
# Smoke test: отправляем JSON-RPC запросы в mcp-router через stdio.
# Проверяем: initialize → tools/list → tools/call(mcp_router_select)

set -e
cd "$(dirname "$0")/.."

# JSON-RPC initialize
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}'
# initialized notification
INIT_DONE='{"jsonrpc":"2.0","method":"notifications/initialized"}'
# tools/list
LIST='{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
# tools/call mcp_router_select
CALL='{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"mcp_router_select","arguments":{"task":"get current time in UTC"}}}'

# Пайпим все запросы, читаем ответы
printf '%s\n%s\n%s\n%s\n' "$INIT" "$INIT_DONE" "$LIST" "$CALL" | \
  timeout 10 python -m mcp_router.server 2>/dev/null