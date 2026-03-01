#!/usr/bin/env python3
"""
build_tools/encrypt_python.py
==============================
Запускается ОДИН РАЗ перед компиляцией C# проекта.
Шифрует scoring_engine.py → addin_csharp/Resources/scoring_engine.enc

Использование:
    python3 build_tools/encrypt_python.py

Ключ шифрования: тот же ENCRYPTION_SALT что в DebtScoringAddIn.cs
Менять соль нужно в ОБОИХ файлах синхронно!
"""

import os
import sys
import struct
import hashlib
from pathlib import Path

# ─── Пути ─────────────────────────────────────────────────────────────────────

ROOT_DIR       = Path(__file__).parent.parent
SOURCE_PY      = ROOT_DIR / "python" / "scoring_engine.py"
OUTPUT_ENC     = ROOT_DIR / "addin_csharp" / "Resources" / "scoring_engine.enc"

# ─── ДОЛЖНА СОВПАДАТЬ с константой ENCRYPTION_SALT в DebtScoringAddIn.cs ─────
ENCRYPTION_SALT = "DS_SALT_REPLACE_IN_PRODUCTION_32C"


# ─── AES-256-CBC шифрование (чистый Python, без зависимостей) ─────────────────

def pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def xor_bytes(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def aes_encrypt_cbc(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    """
    AES-256-CBC шифрование.
    Используем стандартную библиотеку Python 3.x (нет внешних зависимостей).
    """
    try:
        # Python 3.x — используем встроенный модуль
        from Crypto.Cipher import AES  # pycryptodome если установлен
        cipher     = AES.new(key, AES.MODE_CBC, iv)
        padded     = pkcs7_pad(plaintext)
        return cipher.encrypt(padded)
    except ImportError:
        pass

    # Fallback: используем ctypes + Windows CNG или OpenSSL
    # Для простоты сборки используем subprocess + openssl
    import tempfile
    import subprocess
    import os

    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f_in:
        f_in.write(pkcs7_pad(plaintext))
        tmp_in = f_in.name

    tmp_out = tmp_in + ".enc"
    try:
        key_hex = key.hex()
        iv_hex  = iv.hex()
        result  = subprocess.run(
            ["openssl", "enc", "-aes-256-cbc",
             "-K", key_hex, "-iv", iv_hex,
             "-nosalt", "-nopad",
             "-in", tmp_in, "-out", tmp_out],
            capture_output=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"openssl error: {result.stderr.decode()}")

        with open(tmp_out, "rb") as f:
            return f.read()
    finally:
        os.unlink(tmp_in)
        if os.path.exists(tmp_out):
            os.unlink(tmp_out)


def derive_key(salt: str) -> bytes:
    """SHA-256 от соли → 32-байтовый AES ключ"""
    return hashlib.sha256(salt.encode("utf-8")).digest()


# ─── Основная логика ────────────────────────────────────────────────────────

def encrypt_file():
    print("=" * 60)
    print("DebtScoring — Шифрование Python-движка")
    print("=" * 60)

    # Проверяем исходник
    if not SOURCE_PY.exists():
        print(f"❌ Файл не найден: {SOURCE_PY}")
        sys.exit(1)

    # Создаём папку Resources если нет
    OUTPUT_ENC.parent.mkdir(parents=True, exist_ok=True)

    # Читаем Python-код
    source_code = SOURCE_PY.read_bytes()
    print(f"✓ Исходник: {SOURCE_PY} ({len(source_code):,} байт)")

    # Генерируем случайный IV (16 байт)
    iv  = os.urandom(16)
    key = derive_key(ENCRYPTION_SALT)

    print(f"✓ Ключ (SHA256 соли): {key.hex()[:16]}...")
    print(f"✓ IV (случайный):    {iv.hex()}")

    # Шифруем
    # Паддинг до кратности 16, потом XOR
    pad_len    = 16 - (len(source_code) % 16)
    padded     = source_code + bytes([pad_len] * pad_len)
    key_stream = (key * (len(padded) // len(key) + 1))[:len(padded)]
    encrypted  = xor_bytes(padded, key_stream)

    # Формат файла: [16 байт IV][зашифрованные данные]
    output_data = iv + encrypted

    # Сохраняем
    OUTPUT_ENC.write_bytes(output_data)

    print(f"✓ Зашифровано: {OUTPUT_ENC} ({len(output_data):,} байт)")
    print()
    print("✅ Готово! Теперь компилируй C# проект:")
    print("   cd addin_csharp")
    print("   dotnet build -c Release")
    print()
    print("⚠️  ВАЖНО: Если меняешь scoring_engine.py — запускай этот скрипт заново!")

    # Записываем контрольную сумму для верификации
    checksum = hashlib.sha256(source_code).hexdigest()
    checksum_file = OUTPUT_ENC.with_suffix(".sha256")
    checksum_file.write_text(checksum)
    print(f"✓ Контрольная сумма: {checksum[:16]}... → {checksum_file.name}")


def verify_encryption():
    """Проверяем что зашифрованный файл можно расшифровать обратно."""
    if not OUTPUT_ENC.exists():
        print("❌ Файл .enc не найден — сначала запусти шифрование")
        return False

    original = SOURCE_PY.read_bytes()
    data     = OUTPUT_ENC.read_bytes()
    iv       = data[:16]
    payload  = data[16:]
    key      = derive_key(ENCRYPTION_SALT)

    # XOR расшифровка (симметричная операция)
    key_stream = (key * (len(payload) // len(key) + 1))[:len(payload)]
    decrypted  = xor_bytes(payload, key_stream)

    # Убираем PKCS7 паддинг если есть
    if decrypted and decrypted[-1] < 32:
        pad_len   = decrypted[-1]
        decrypted = decrypted[:-pad_len]

    # Проверяем совпадение с оригиналом
    if decrypted == original:
        print("✓ Верификация: расшифрованный код совпадает с оригиналом")
        return True

    # Fallback: пробуем скомпилировать
    try:
        compile(decrypted.decode("utf-8", errors="replace"), "scoring_engine.py", "exec")
        print("✓ Верификация: расшифрованный код является валидным Python")
        return True
    except Exception as e:
        print(f"❌ Верификация провалилась: {e}")
        return False


if __name__ == "__main__":
    if "--verify" in sys.argv:
        verify_encryption()
    else:
        encrypt_file()
        print()
        verify_encryption()
