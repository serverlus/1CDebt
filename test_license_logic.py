"""
Тесты логики лицензирования (без FastAPI, без aiosqlite).
Проверяем: генерацию ключей, HMAC, валидацию подписи, срок действия.
Запуск: python3 test_license_logic.py
"""

import sys
import hmac
import hashlib
import secrets
import unittest
from datetime import datetime, timedelta, timezone


# ─────────────────────────────────────────────────────────────────────────────
# Переносим чистую логику из серверного кода — без зависимостей
# ─────────────────────────────────────────────────────────────────────────────

def generate_key() -> str:
    """Генерация лицензионного ключа в формате DSCR-XXXX-XXXX-XXXX-XXXX."""
    def block(): return secrets.token_hex(2).upper()
    return f"DSCR-{block()}-{block()}-{block()}-{block()}"


def generate_device_token(master_secret: str, inn: str, mac: str) -> str:
    """HMAC(master_secret, inn:mac) — привязка к устройству."""
    return hmac.new(
        key       = master_secret.encode(),
        msg       = f"{inn}:{mac}".encode(),
        digestmod = hashlib.sha256,
    ).hexdigest()


def generate_daily_checksum(device_token: str, inn: str, mac: str,
                             date_str: str = None) -> str:
    """HMAC(device_token, inn:mac:today)[:16] — защита от replay-атак."""
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return hmac.new(
        key       = device_token.encode(),
        msg       = f"{inn}:{mac}:{date_str}".encode(),
        digestmod = hashlib.sha256,
    ).hexdigest()[:16]


def validate_checksum(device_token: str, inn: str, mac: str,
                       checksum: str) -> bool:
    """Сервер проверяет подпись от клиента."""
    expected = generate_daily_checksum(device_token, inn, mac)
    return hmac.compare_digest(checksum, expected)


def validate_expiry(expires_at: str) -> tuple[bool, int]:
    """Возвращает (действительна, дней_осталось)."""
    try:
        expires = datetime.fromisoformat(expires_at)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        now       = datetime.now(timezone.utc)
        days_left = (expires - now).days
        return days_left > 0, days_left
    except Exception:
        return False, 0


def validate_key_format(key: str) -> bool:
    """Проверяет формат ключа DSCR-XXXX-XXXX-XXXX-XXXX."""
    parts = key.upper().strip().split("-")
    return len(parts) == 5 and parts[0] == "DSCR" and all(len(p) == 4 for p in parts[1:])


# ─────────────────────────────────────────────────────────────────────────────
# ТЕСТЫ
# ─────────────────────────────────────────────────────────────────────────────

class TestKeyGeneration(unittest.TestCase):

    def test_key_format_valid(self):
        key = generate_key()
        self.assertTrue(validate_key_format(key), f"Неверный формат: {key}")

    def test_keys_are_unique(self):
        keys = {generate_key() for _ in range(1000)}
        self.assertEqual(len(keys), 1000)

    def test_key_starts_with_dscr(self):
        key = generate_key()
        self.assertTrue(key.startswith("DSCR-"))

    def test_key_has_5_parts(self):
        key = generate_key()
        self.assertEqual(len(key.split("-")), 5)

    def test_invalid_key_format(self):
        self.assertFalse(validate_key_format("WRONG-KEY"))
        self.assertFalse(validate_key_format("DSCR-123-456-789"))
        self.assertFalse(validate_key_format(""))
        self.assertTrue(validate_key_format("DSCR-A1B2-C3D4-E5F6-G7H8"))


class TestDeviceToken(unittest.TestCase):

    INN     = "7712345678"
    MAC     = "00-11-22-33-44-55"
    SECRET  = secrets.token_hex(32)

    def test_token_generated(self):
        token = generate_device_token(self.SECRET, self.INN, self.MAC)
        self.assertIsNotNone(token)
        self.assertEqual(len(token), 64)  # SHA256 hex = 64 символа

    def test_token_deterministic(self):
        """Один и тот же секрет + устройство → всегда одинаковый токен."""
        t1 = generate_device_token(self.SECRET, self.INN, self.MAC)
        t2 = generate_device_token(self.SECRET, self.INN, self.MAC)
        self.assertEqual(t1, t2)

    def test_token_different_for_different_inn(self):
        t1 = generate_device_token(self.SECRET, "1111111111", self.MAC)
        t2 = generate_device_token(self.SECRET, "2222222222", self.MAC)
        self.assertNotEqual(t1, t2)

    def test_token_different_for_different_mac(self):
        t1 = generate_device_token(self.SECRET, self.INN, "AA-BB-CC-DD-EE-FF")
        t2 = generate_device_token(self.SECRET, self.INN, "11-22-33-44-55-66")
        self.assertNotEqual(t1, t2)

    def test_token_different_for_different_secret(self):
        t1 = generate_device_token("secret_one", self.INN, self.MAC)
        t2 = generate_device_token("secret_two", self.INN, self.MAC)
        self.assertNotEqual(t1, t2)


class TestDailyChecksum(unittest.TestCase):

    INN     = "7712345678"
    MAC     = "00-11-22-33-44-55"
    TOKEN   = generate_device_token(secrets.token_hex(32), "7712345678", "00-11-22-33-44-55")

    def test_checksum_length(self):
        cs = generate_daily_checksum(self.TOKEN, self.INN, self.MAC)
        self.assertEqual(len(cs), 16)

    def test_checksum_valid_today(self):
        cs = generate_daily_checksum(self.TOKEN, self.INN, self.MAC)
        self.assertTrue(validate_checksum(self.TOKEN, self.INN, self.MAC, cs))

    def test_checksum_invalid_yesterday(self):
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        cs = generate_daily_checksum(self.TOKEN, self.INN, self.MAC, yesterday)
        # Вчерашняя подпись не проходит сегодня
        self.assertFalse(validate_checksum(self.TOKEN, self.INN, self.MAC, cs))

    def test_checksum_invalid_wrong_inn(self):
        cs = generate_daily_checksum(self.TOKEN, self.INN, self.MAC)
        # Подпись не проходит для другого ИНН
        self.assertFalse(validate_checksum(self.TOKEN, "9999999999", self.MAC, cs))

    def test_checksum_invalid_wrong_mac(self):
        cs = generate_daily_checksum(self.TOKEN, self.INN, self.MAC)
        self.assertFalse(validate_checksum(self.TOKEN, self.INN, "FF-FF-FF-FF-FF-FF", cs))

    def test_checksum_different_each_day(self):
        today     = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tomorrow  = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        cs_today  = generate_daily_checksum(self.TOKEN, self.INN, self.MAC, today)
        cs_tmrw   = generate_daily_checksum(self.TOKEN, self.INN, self.MAC, tomorrow)
        self.assertNotEqual(cs_today, cs_tmrw)


class TestExpiryValidation(unittest.TestCase):

    def test_active_license(self):
        future     = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        valid, rem = validate_expiry(future)
        self.assertTrue(valid)
        self.assertGreater(rem, 25)

    def test_expired_license(self):
        past       = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        valid, rem = validate_expiry(past)
        self.assertFalse(valid)
        self.assertLessEqual(rem, 0)

    def test_expiring_soon_warning(self):
        soon       = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        valid, rem = validate_expiry(soon)
        self.assertTrue(valid)
        self.assertLessEqual(rem, 7)

    def test_invalid_date(self):
        valid, rem = validate_expiry("not-a-date")
        self.assertFalse(valid)
        self.assertEqual(rem, 0)


class TestFullActivationFlow(unittest.TestCase):
    """Симуляция полного флоу активации и валидации."""

    def setUp(self):
        self.key           = generate_key()
        self.master_secret = secrets.token_hex(32)
        self.inn           = "7712345678"
        self.mac           = "AA-BB-CC-DD-EE-FF"
        self.expires_at    = (
            datetime.now(timezone.utc) + timedelta(days=365)
        ).isoformat()

    def test_full_flow(self):
        # 1. Клиент активирует — сервер генерирует device_token
        device_token = generate_device_token(
            self.master_secret, self.inn, self.mac)
        self.assertIsNotNone(device_token)

        # 2. Клиент сохраняет device_token локально

        # 3. Через 24 часа клиент валидирует — генерирует checksum
        checksum = generate_daily_checksum(device_token, self.inn, self.mac)

        # 4. Сервер проверяет checksum
        checksum_valid = validate_checksum(device_token, self.inn, self.mac, checksum)
        self.assertTrue(checksum_valid)

        # 5. Сервер проверяет срок
        expiry_valid, days = validate_expiry(self.expires_at)
        self.assertTrue(expiry_valid)
        self.assertGreater(days, 300)

        print(f"\n  Full flow OK: token={device_token[:16]}... days_left={days}")

    def test_stolen_token_different_device(self):
        """Украденный токен не работает на другом устройстве."""
        device_token  = generate_device_token(
            self.master_secret, self.inn, self.mac)

        # Злоумышленник с другим MAC пытается использовать токен
        attacker_mac  = "00-00-00-00-00-00"
        checksum      = generate_daily_checksum(
            device_token, self.inn, attacker_mac)  # checksum для чужого MAC

        # Сервер проверяет с оригинальным MAC — не совпадает
        valid = validate_checksum(device_token, self.inn, self.mac, checksum)
        self.assertFalse(valid, "Токен не должен работать на другом устройстве")

    def test_key_uniqueness_under_load(self):
        """1000 ключей — все уникальные."""
        keys = [generate_key() for _ in range(1000)]
        self.assertEqual(len(set(keys)), 1000)


# ─────────────────────────────────────────────────────────────────────────────

def demo():
    print("\n" + "=" * 60)
    print("DEMO: Полный цикл лицензирования")
    print("=" * 60)

    # Генерируем ключ для клиента
    key    = generate_key()
    secret = secrets.token_hex(32)
    print(f"\n1. Создали ключ для продажи: {key}")
    print(f"   Тариф: PRO, срок: 12 месяцев")

    # Клиент активирует
    inn   = "7712345678"
    mac   = "AA-BB-CC-DD-EE-FF"
    token = generate_device_token(secret, inn, mac)
    expires = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
    print(f"\n2. Клиент активировал:")
    print(f"   ИНН:         {inn}")
    print(f"   MAC:         {mac}")
    print(f"   device_token:{token[:24]}...")
    print(f"   Действует до:{expires[:10]}")

    # Ежедневная валидация
    checksum = generate_daily_checksum(token, inn, mac)
    valid    = validate_checksum(token, inn, mac, checksum)
    ok, days = validate_expiry(expires)
    print(f"\n3. Ежедневная валидация:")
    print(f"   Подпись:  {'✅ OK' if valid else '❌ FAIL'}")
    print(f"   Срок:     {'✅ OK' if ok else '❌ ИСТЁК'} ({days} дней)")

    # Попытка с чужого устройства
    fake_mac  = "00-00-00-00-00-00"
    fake_cs   = generate_daily_checksum(token, inn, fake_mac)
    fake_ok   = validate_checksum(token, inn, mac, fake_cs)
    print(f"\n4. Попытка с другого MAC: {'❌ ЗАБЛОКИРОВАНО' if not fake_ok else '⚠️ ПРОШЛО'}")


if __name__ == "__main__":
    demo()
    print("\n" + "=" * 60)
    print("UNIT TESTS")
    print("=" * 60)
    unittest.main(verbosity=2, exit=False)
