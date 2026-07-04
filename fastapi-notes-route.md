# FastAPI Lifespan Handlers

Источник: Context7 MCP (`/fastapi/fastapi`) — docs/en/docs + applications.py (актуально на 2026-07-05)
`lifespan` появился в FastAPI 0.93.0 (2023-03-07).

## Суть

`lifespan` — async-контекст-менеджер, переданный в `FastAPI(lifespan=...)`.
Код **до `yield`** выполняется при старте, код **после `yield`** — при остановке.
Заменяет устаревшие `on_event`, а также параметры `on_startup` / `on_shutdown`.

Из исходников FastAPI:
- `lifespan` — «A `Lifespan` context manager handler. This replaces `startup` and `shutdown` functions with a single context manager.»
- `on_startup` / `on_shutdown` помечены deprecated → «You should instead use the `lifespan` handlers.»
- `@app.on_event(...)` помечен `@deprecated`: «on_event is deprecated, use lifespan event handlers instead.»

## Минимальный пример

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

def fake_answer_to_everything_ml_model(x: float):
    return x * 42

ml_models = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    ml_models["answer_to_everything"] = fake_answer_to_everything_ml_model  # startup
    yield
    ml_models.clear()                                                       # shutdown

app = FastAPI(lifespan=lifespan)

@app.get("/predict")
async def predict(x: float):
    return {"result": ml_models["answer_to_everything"](x)}
```

## Заметки

- Startup и shutdown в одной функции → общие переменные без глобалей.
- Если задан `lifespan`, обработчики `on_event` не вызываются — либо одно, либо другое.
- Под капотом — ASGI Lifespan Protocol.
