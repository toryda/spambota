
import os
import base64
from pydantic_settings import BaseSettings
from cryptography.fernet import Fernet
from telethon.errors import FloodWaitError
import asyncio


# ===============================
# Настройки приложения
# ===============================
class Settings(BaseSettings):
    DATABASE_URL: str = "sqlite:///./app.db"
    SECRET_KEY: str = "CHANGE_ME"
    TELEGRAM_DEFAULT_API_ID: int | None = None
    TELEGRAM_DEFAULT_API_HASH: str | None = None

    class Config:
        env_file = ".env"


settings = Settings()


# ===============================
# Безопасность (шифрование)
# ===============================
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "your-secret-key-here-32-chars!")

def get_fernet():
    """Получает объект Fernet для шифрования/расшифровки"""
    key = base64.urlsafe_b64encode(ENCRYPTION_KEY.encode()[:32].ljust(32, b'0'))
    return Fernet(key)


def encrypt_session(session_string: str) -> str:
    """Шифрует строку сессии"""
    f = get_fernet()
    encrypted = f.encrypt(session_string.encode())
    return base64.b64encode(encrypted).decode()


def decrypt_session(encrypted_session: str) -> str:
    """Расшифровывает строку сессии"""
    f = get_fernet()
    encrypted_bytes = base64.b64decode(encrypted_session.encode())
    decrypted = f.decrypt(encrypted_bytes)
    return decrypted.decode()


# ===============================
# Обработка FloodWait и ошибок
# ===============================
async def safe_send(func, *args, **kwargs):
    """
    Выполняем Telethon-метод с защитой от FloodWait.
    Если Telegram требует подождать — ждём и пробуем снова.
    """
    try:
        return await func(*args, **kwargs)
    except FloodWaitError as e:
        print(f"FloodWait: ждем {e.seconds} секунд...")
        await asyncio.sleep(e.seconds)
        return await func(*args, **kwargs)
    except Exception as e:
        print(f"Ошибка при выполнении {func.__name__}: {e}")
        return None
