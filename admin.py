"""
routers/admin.py — Административные операции.
Все эндпоинты требуют Bearer-токен (DS_ADMIN_TOKEN из .env).
Только ты имеешь доступ.
"""

import secrets
import hashlib
from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Optional

from core import database as db
from core.config import settings

router   = APIRouter(tags=["Admin"])
security = HTTPBearer()


def require_admin(creds: HTTPAuthorizationCredentials = Depends(security)):
    """Проверяет Bearer-токен администратора."""
    if creds.credentials != settings.ADMIN_TOKEN:
        raise HTTPException(
            status_code = 403,
            detail      = {"error": "FORBIDDEN", "message": "Неверный токен администратора"}
        )
    return True


# ─── Генерация ключей ─────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    plan:    str = "PRO"     # STARTER / PRO / CORP
    count:   int = 1         # сколько ключей создать
    months:  int = 12        # срок действия в месяцах
    comment: str = ""        # заметка (имя клиента, сделка)
    invoice: str = ""        # номер счёта


@router.post("/generate")
async def generate_keys(req: GenerateRequest, _=Depends(require_admin)):
    """Генерация новых лицензионных ключей."""

    if req.plan.upper() not in settings.PLANS:
        raise HTTPException(400, detail={
            "error":   "INVALID_PLAN",
            "message": f"Неверный тариф. Доступны: {list(settings.PLANS.keys())}"
        })

    if not 1 <= req.count <= 100:
        raise HTTPException(400, detail={"error": "Invalid count (1-100)"})

    if not 1 <= req.months <= 120:
        raise HTTPException(400, detail={"error": "Invalid months (1-120)"})

    generated = []
    for _ in range(req.count):
        # Формат: DSCR-XXXX-XXXX-XXXX-XXXX (Base32-подобный, только A-Z0-9)
        def rand_block():
            return secrets.token_hex(2).upper()

        key    = f"DSCR-{rand_block()}-{rand_block()}-{rand_block()}-{rand_block()}"
        secret = secrets.token_hex(32)

        await db.create_license(
            key           = key,
            plan          = req.plan.upper(),
            months        = req.months,
            master_secret = secret,
            comment       = req.comment,
            invoice       = req.invoice,
        )
        generated.append(key)

    return {
        "success":    True,
        "keys":       generated,
        "plan":       req.plan.upper(),
        "months":     req.months,
        "count":      len(generated),
        "created_at": datetime.utcnow().isoformat(),
    }


# ─── Список лицензий ─────────────────────────────────────────────────────────

@router.get("/licenses")
async def list_licenses(status: Optional[str] = None,
                         limit:  int = 50,
                         offset: int = 0,
                         _=Depends(require_admin)):
    """Список всех лицензий с фильтрацией по статусу."""
    licenses = await db.get_all_licenses(status=status, limit=limit, offset=offset)

    # Скрываем секреты из ответа
    for lic in licenses:
        lic.pop("master_secret",  None)
        lic.pop("device_token",   None)

    return {
        "licenses": licenses,
        "count":    len(licenses),
        "offset":   offset,
    }


# ─── Статистика ───────────────────────────────────────────────────────────────

@router.get("/stats")
async def get_stats(_=Depends(require_admin)):
    """Сводная статистика по лицензиям и выручке."""
    stats = await db.get_stats()

    # Считаем потенциальную выручку
    monthly_revenue = 0
    for plan, count in stats.get("by_plan", {}).items():
        plan_info        = settings.PLANS.get(plan, {})
        monthly_revenue += plan_info.get("price_month", 0) * count

    stats["monthly_revenue_rub"] = monthly_revenue
    stats["currency"]            = "RUB"

    return stats


# ─── Отзыв лицензии ──────────────────────────────────────────────────────────

class RevokeRequest(BaseModel):
    license_key: str
    reason:      str = ""


@router.post("/revoke")
async def revoke(req: RevokeRequest, _=Depends(require_admin)):
    """Отзыв лицензии. Клиент потеряет доступ в течение 72 часов."""
    success = await db.revoke_license(req.license_key)

    if not success:
        raise HTTPException(404, detail={"error": "KEY_NOT_FOUND"})

    return {
        "success":     True,
        "license_key": req.license_key.upper(),
        "status":      "REVOKED",
        "message":     "Лицензия отозвана. Клиент потеряет доступ через 72 часа.",
    }


# ─── Информация о конкретном ключе ───────────────────────────────────────────

@router.get("/license/{key}")
async def get_license_info(key: str, _=Depends(require_admin)):
    """Детальная информация о лицензии."""
    license = await db.get_license(key)
    if not license:
        raise HTTPException(404, detail={"error": "NOT_FOUND"})

    license.pop("master_secret", None)
    license.pop("device_token",  None)
    return license
