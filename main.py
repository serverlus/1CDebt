"""
DebtScoring License Server
==========================
FastAPI сервер лицензирования.
Разворачивается на твоём VPS (Ubuntu, 1 ядро, 1GB RAM — достаточно).

Установка на сервере:
    pip install fastapi uvicorn python-jose[cryptography] passlib[bcrypt] aiosqlite

Запуск:
    uvicorn main:app --host 0.0.0.0 --port 8000
    # Или через systemd (см. docs/server_deploy.md)

Endpoints:
    POST /api/v1/activate        — активация ключа клиентом
    POST /api/v1/validate        — ежедневная валидация
    POST /api/v1/admin/generate  — генерация ключей (только для тебя)
    GET  /api/v1/admin/licenses  — список всех лицензий
    POST /api/v1/admin/revoke    — отзыв лицензии
    GET  /api/v1/health          — healthcheck
"""

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import time

from routers import activation, admin, validation
from core.database import init_db
from core.config import settings

# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title        = "DebtScoring License Server",
    description  = "Сервер лицензирования для расширения 1С DebtScoring",
    version      = "1.0.0",
    # Отключаем публичную документацию на продакшене
    docs_url     = "/docs" if settings.DEBUG else None,
    redoc_url    = None,
)

# CORS — разрешаем только запросы от компоненты (не от браузеров)
app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],   # компонента не браузер, origin не важен
    allow_credentials = False,
    allow_methods     = ["POST", "GET"],
    allow_headers     = ["Content-Type", "Authorization"],
)

# ── Роутеры ──────────────────────────────────────────────────────────────────
app.include_router(activation.router, prefix="/api/v1")
app.include_router(validation.router, prefix="/api/v1")
app.include_router(admin.router,      prefix="/api/v1/admin")

# ── Startup ──────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    await init_db()
    print("✓ База данных инициализирована")
    print(f"✓ Сервер запущен. Debug={settings.DEBUG}")

# ── Healthcheck ──────────────────────────────────────────────────────────────
@app.get("/api/v1/health")
async def health():
    return {
        "status":  "ok",
        "service": "DebtScoring License Server",
        "version": "1.0.0",
        "time":    int(time.time()),
    }

# ── Rate limiting middleware (простой, без Redis) ────────────────────────────
# Защита от брутфорса ключей: не более 10 запросов/мин с одного IP
_request_counts: dict = {}

@app.middleware("http")
async def rate_limit(request: Request, call_next):
    # Не применяем к healthcheck
    if request.url.path == "/api/v1/health":
        return await call_next(request)

    ip      = request.client.host if request.client else "unknown"
    now     = int(time.time())
    window  = now // 60  # минута

    key     = f"{ip}:{window}"
    count   = _request_counts.get(key, 0) + 1
    _request_counts[key] = count

    # Чистим старые записи
    old_key = f"{ip}:{window - 1}"
    _request_counts.pop(old_key, None)

    if count > 20:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code = 429,
            content     = {"error": "TOO_MANY_REQUESTS",
                           "message": "Слишком много запросов. Попробуйте через минуту."}
        )

    return await call_next(request)
