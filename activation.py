"""
routers/activation.py — Активация лицензии.
Вызывается из C# компоненты при первом запуске у клиента.
"""

import hmac
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, validator

from core import database as db
from core.config import settings

router = APIRouter(tags=["Client"])


class ActivationRequest(BaseModel):
    license_key: str
    inn:         str
    mac_address: str
    hostname:    str = ""

    @validator("license_key")
    def key_format(cls, v):
        v = v.strip().upper()
        if not v.startswith("DSCR-") or len(v) < 24:
            raise ValueError("Неверный формат ключа. Ожидается: DSCR-XXXX-XXXX-XXXX-XXXX")
        return v

    @validator("inn")
    def inn_not_empty(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("ИНН не может быть пустым")
        return v


@router.post("/activate")
async def activate(req: ActivationRequest, request: Request):
    """
    Активация лицензии.
    Привязывает ключ к ИНН + MAC-адресу, возвращает device_token.
    """
    ip = request.client.host if request.client else "unknown"

    # Получаем лицензию из базы
    license = await db.get_license(req.license_key)

    if not license:
        await db.log_request(req.license_key, "activate", ip, req.inn,
                              req.mac_address, False, "KEY_NOT_FOUND")
        raise HTTPException(404, detail={
            "error":   "KEY_NOT_FOUND",
            "message": "Лицензионный ключ не найден. Проверьте правильность ввода."
        })

    # Ключ отозван
    if license["status"] == "REVOKED":
        await db.log_request(req.license_key, "activate", ip, req.inn,
                              req.mac_address, False, "KEY_REVOKED")
        raise HTTPException(403, detail={
            "error":   "KEY_REVOKED",
            "message": "Лицензионный ключ отозван. Обратитесь в поддержку."
        })

    # Ключ уже активирован — проверяем то ли устройство
    if license["status"] == "ACTIVE":
        same_inn = license["inn"]         == req.inn
        same_mac = license["mac_address"] == req.mac_address

        if same_inn and same_mac:
            # Повторная активация на том же устройстве — окей
            await db.log_request(req.license_key, "activate", ip, req.inn,
                                  req.mac_address, True, "REACTIVATION")
            return _build_activation_response(license)

        # Другое устройство — отказываем
        await db.log_request(req.license_key, "activate", ip, req.inn,
                              req.mac_address, False, "ALREADY_ACTIVATED_OTHER_DEVICE")
        raise HTTPException(409, detail={
            "error":   "ALREADY_ACTIVATED",
            "message": (
                "Ключ уже активирован на другом устройстве. "
                "Для переноса лицензии обратитесь в поддержку."
            )
        })

    # Новая активация
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=license["months"] * 30)
    ).isoformat()

    # Генерируем device_token = HMAC-SHA256(master_secret, inn:mac)
    device_token = hmac.new(
        key     = license["master_secret"].encode(),
        msg     = f"{req.inn}:{req.mac_address}".encode(),
        digestmod = hashlib.sha256,
    ).hexdigest()

    # Сохраняем активацию
    updated = await db.activate_license(
        key          = req.license_key,
        inn          = req.inn,
        mac          = req.mac_address,
        hostname     = req.hostname,
        device_token = device_token,
        expires_at   = expires_at,
    )

    await db.log_request(req.license_key, "activate", ip, req.inn,
                          req.mac_address, True, f"plan={license['plan']}")

    return _build_activation_response(updated)


def _build_activation_response(license: dict) -> dict:
    plan_info = settings.PLANS.get(license["plan"], {})
    return {
        "status":        "activated",
        "device_secret": license["device_token"],
        "plan":          license["plan"],
        "plan_name":     plan_info.get("name", license["plan"]),
        "expires_at":    license["expires_at"],
        "features":      plan_info.get("features", []),
        "Success":       True,
        "Info":          f"Активировано. Тариф: {plan_info.get('name')}. "
                         f"Действует до: {license['expires_at'][:10]}",
    }
