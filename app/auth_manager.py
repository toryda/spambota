import asyncio
import tempfile
import os
import secrets
from datetime import datetime
from typing import Dict, Optional
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from sqlmodel import Session, select, SQLModel, create_engine
from app.db import get_session, Account
from urllib.parse import urlparse
import logging

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Предполагаем, что engine определен где-то еще, например, в app.db
# Для примера, создадим заглушку, если она не импортируется
try:
    from app.db import engine
except ImportError:
    # Создаем заглушку engine, если app.db не доступен или не содержит engine
    # В реальном приложении это должно быть правильно настроено
    # Например, engine = create_engine("sqlite:///database.db")
    class MockEngine:
        def __init__(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass
        def connect(self):
            return self
        def execute(self, query):
            pass
        def commit(self):
            pass
        def refresh(self, obj):
            pass
        def add(self, obj):
            pass
        def exec(self, statement):
            class MockResult:
                def first(self):
                    return None
            return MockResult()
    engine = MockEngine()


class TelegramAuthManager:
    def __init__(self):
        # Используем фиксированные API данные
        self.api_id = 29449224
        self.api_hash = '0b4beececa095ca88a2524bf4781f4c7'

        # Хранилище активных сессий
        self.auth_sessions: Dict[str, dict] = {}
        self._pending_clients: Dict[str, TelegramClient] = {}
        self.pending_sessions: Dict[str, dict] = {}

    def _proxy_kwargs(self, proxy_url: Optional[str] = None) -> dict:
        """Парсит прокси URL и возвращает параметры для Telethon"""
        if not proxy_url:
            return {}

        try:
            url = proxy_url.strip()
            proxy_type = 'socks5'
            if url.startswith('http'):
                proxy_type = 'http'
            elif url.startswith('mtpro'):
                proxy_type = 'mtproto'
            
            clean_url = url.split('://')[-1]
            
            if '@' in clean_url:
                auth, addr = clean_url.split('@')
                if ':' in auth:
                    username, password = auth.split(':', 1)
                else:
                    username, password = auth, None
                
                if ':' in addr:
                    host, port = addr.split(':', 1)
                else:
                    host, port = addr, (80 if proxy_type == 'http' else 1080)
                    
                return {
                    'proxy': {
                        'proxy_type': proxy_type,
                        'addr': host,
                        'port': int(port),
                        'username': username,
                        'password': password,
                        'rdns': True
                    }
                }
            else:
                if ':' in clean_url:
                    host, port = clean_url.split(':', 1)
                    return {
                        'proxy': {
                            'proxy_type': proxy_type,
                            'addr': host,
                            'port': int(port),
                            'rdns': True
                        }
                    }
                else:
                    return {
                        'proxy': {
                            'proxy_type': proxy_type,
                            'addr': clean_url,
                            'port': (80 if proxy_type == 'http' else 1080),
                            'rdns': True
                        }
                    }
        except Exception as e:
            logger.error(f"⚠️ Ошибка парсинга прокси: {e}")
            return {}

    def _normalize_phone(self, phone: str) -> str:
        """Нормализует номер телефона"""
        phone = phone.strip().replace(' ', '').replace('-', '').replace('(', '').replace(')', '').replace('.', '')
        if not phone.startswith('+'):
            if phone.startswith('8'):
                phone = '+7' + phone[1:]
            elif phone.startswith('7'):
                phone = '+' + phone
            else:
                phone = '+' + phone
        return phone

    def _parse_proxy(self, proxy_url: str) -> dict:
        """Парсит URL прокси для Telethon"""
        kwargs = self._proxy_kwargs(proxy_url)
        return kwargs.get('proxy', {})

    async def start_login(self, phone: str, proxy_url: Optional[str] = None) -> tuple[bool, str, Optional[str]]:
        """
        Стартуем логин: создаём пустую StringSession, подключаемся,
        отправляем код на телефон, возвращаем токен для продолжения.
        """
        try:
            # Нормализуем номер телефона
            phone = self._normalize_phone(phone)

            # Проверяем формат после нормализации
            phone_digits = phone[1:] if phone.startswith('+') else phone
            if not phone_digits.isdigit():
                return False, "Номер должен содержать только цифры", None

            if len(phone_digits) < 10 or len(phone_digits) > 15:
                return False, "Неверная длина номера телефона", None

            # Создаем клиента с прокси если нужно
            proxy_kwargs = self._proxy_kwargs(proxy_url)
            client = TelegramClient(StringSession(), self.api_id, self.api_hash, **proxy_kwargs)

            # Сохраняем URL прокси для последующего использования
            client._proxy_url = proxy_url

            await client.connect()

            # Проверяем подключение к Telegram серверам
            if not client.is_connected():
                await client.connect()

            # Отправляем код через Telegram (не SMS)
            result = await client.send_code_request(phone, force_sms=False)

            # Генерируем токен для продолжения
            token = secrets.token_urlsafe(16)
            self._pending_clients[token] = client

            # Сохраняем данные для верификации
            self.pending_sessions[phone] = {
                'token': token,
                'client': client,
                'phone': phone,
                'phone_code_hash': result.phone_code_hash,
                'proxy_url': proxy_url,
                'created_at': datetime.now()
            }

            logger.info(f"✅ Код успешно отправлен в Telegram для {phone}")
            return True, "Код отправлен в приложение Telegram", token

        except FloodWaitError as e:
            logger.error(f"❌ FloodWait: {e.seconds} секунд")
            return False, f"Слишком много попыток. Подождите {e.seconds} секунд", None
        except Exception as e:
            logger.error(f"❌ Ошибка отправки кода: {str(e)}")
            if "api_id" in str(e).lower() and "invalid" in str(e).lower():
                return False, "Ошибка API конфигурации. Обратитесь к администратору", None
            return False, f"Не удалось отправить код. Проверьте номер телефона", None

    async def confirm_login(self, phone: str, code: str, twofa_password: Optional[str] = None) -> tuple[bool, str, Optional[dict]]:
        """
        Завершаем логин: подтверждаем код (и при необходимости 2FA),
        возвращаем итоговый StringSession.
        """
        # Нормализуем номер телефона
        phone = self._normalize_phone(phone)

        if phone not in self.pending_sessions:
            return False, "Сессия авторизации не найдена. Запросите код заново", None

        # Проверяем время жизни сессии (15 минут)
        session_data = self.pending_sessions[phone]
        current_time = datetime.now()
        if (current_time - session_data['created_at']).total_seconds() > 900:  # 15 минут
            self.cleanup_pending(session_data.get('token'))
            del self.pending_sessions[phone]
            return False, "Сессия истекла, запросите код заново", None

        token = session_data.get('token')
        client = self._pending_clients.get(token)
        if not client:
            return False, "Сессия авторизации не найдена или устарела", None

        try:
            # Очищаем код от пробелов и дефисов
            clean_code = code.replace(' ', '').replace('-', '')
            phone_code_hash = session_data.get('phone_code_hash')

            try:
                # Пробуем войти с кодом
                await client.sign_in(phone=phone, code=clean_code, phone_code_hash=phone_code_hash)
            except SessionPasswordNeededError:
                logger.info(f"Требуется 2FA для {phone}")
                if not twofa_password:
                    # Если требуется 2FA, но пароль не дали — просим ещё раз
                    return False, "Включена двухфакторная аутентификация. Введите пароль от аккаунта", None
                # Если пароль дали — пробуем с ним
                try:
                    await client.sign_in(password=twofa_password)
                except Exception as twofa_error:
                    logger.error(f"Ошибка 2FA: {twofa_error}")
                    if "password" in str(twofa_error).lower() and "invalid" in str(twofa_error).lower():
                        return False, "Неверный пароль двухфакторной аутентификации", None
                    else:
                        return False, f"Ошибка двухфакторной аутентификации: {str(twofa_error)}", None

            # Получаем информацию о пользователе
            me = await client.get_me()

            # Получаем строку сессии
            session_string = client.session.save()

            # Сохраняем аккаунт в базу данных
            with Session(engine) as db_session:
                # Проверяем, не существует ли уже такой аккаунт
                existing_account = db_session.exec(
                    select(Account).where(Account.phone == phone)
                ).first()

                if existing_account:
                    # Обновляем существующий аккаунт
                    existing_account.session_string = session_string
                    existing_account.is_active = True
                    existing_account.proxy_url = session_data.get('proxy_url')
                    db_session.add(existing_account)
                    account = existing_account
                else:
                    # Создаем новый аккаунт
                    account = Account(
                        title=f"{me.first_name} {me.last_name or ''}".strip() or phone,
                        phone=phone,
                        api_id=self.api_id,
                        api_hash=self.api_hash,
                        session_string=session_string,
                        proxy_url=session_data.get('proxy_url'),
                        is_active=True
                    )
                    db_session.add(account)

                db_session.commit()
                db_session.refresh(account)

                user_info = {
                    'account_id': account.id,
                    'first_name': me.first_name,
                    'last_name': me.last_name,
                    'username': me.username,
                    'phone': me.phone
                }

            # Закрываем клиента
            await client.disconnect()

            # Удаляем временную сессию
            if phone in self.pending_sessions:
                del self.pending_sessions[phone]

            return True, "Успешно авторизован", user_info

        except Exception as e:
            logger.error(f"Ошибка подтверждения входа: {e}", exc_info=True)

            try:
                await client.disconnect()
            except:
                pass

            # Удаляем сессию из хранилища при ошибке
            if phone in self.pending_sessions:
                del self.pending_sessions[phone]

            # Детальная обработка различных типов ошибок
            error_str = str(e).lower()

            if "phonecodemptyerror" in error_str or "phone_code_empty" in error_str:
                return False, "Код не может быть пустым", None
            elif "phonecodexpirederror" in error_str or "phone_code_expired" in error_str:
                return False, "Код истек. Запросите новый код", None
            elif "phonecodeinvaliderror" in error_str or "phone_code_invalid" in error_str:
                return False, "Неверный код. Проверьте правильность ввода", None
            elif "phonecodehashemptyerror" in error_str:
                return False, "Сессия истекла. Запросите новый код", None
            elif "floodwaiterror" in error_str or "flood_wait" in error_str:
                return False, "Слишком много попыток. Подождите 10-15 минут", None
            elif "unauthorized" in error_str:
                return False, "Ошибка авторизации. Начните процесс заново", None
            elif "connection" in error_str or "network" in error_str:
                return False, "Ошибка подключения к Telegram. Проверьте интернет или настройки прокси", None
            elif "timeout" in error_str:
                return False, "Превышено время ожидания. Проверьте интернет-соединение", None
            elif "proxy" in error_str:
                return False, "Ошибка прокси. Проверьте настройки прокси или попробуйте без прокси", None
            else:
                return False, f"Ошибка: {str(e)}", None

    def cleanup_pending(self, token: Optional[str]):
        """На случай отмены/сбоя — закрыть клиент и удалить из pending"""
        if not token:
            return
        client = self._pending_clients.pop(token, None)
        if client:
            try:
                # закрыть аккуратно
                asyncio.create_task(client.disconnect())
            except Exception:
                pass

    def cleanup_expired_sessions(self):
        """Очищает истекшие сессии"""
        current_time = datetime.now()
        expired_phones = [
            phone for phone, data in self.pending_sessions.items()
            if (current_time - data['created_at']).total_seconds() > 600  # 10 минут
        ]
        for phone in expired_phones:
            session_data = self.pending_sessions[phone]
            self.cleanup_pending(session_data.get('token'))
            del self.pending_sessions[phone]

    # Методы для обратной совместимости
    async def send_code(self, phone: str, proxy_url: str = None) -> tuple[bool, str]:
        """Отправляет SMS код на указанный номер"""
        try:
            # Нормализуем номер телефона
            normalized_phone = self._normalize_phone(phone)

            # Очищаем истекшие сессии
            self.cleanup_expired_sessions()

            # Настройка прокси
            proxy = None
            if proxy_url and proxy_url.strip():
                try:
                    proxy = self._parse_proxy(proxy_url.strip())
                except Exception as e:
                    return False, f"Неверный формат прокси: {str(e)}"

            # Создаем временного клиента
            session = StringSession()
            client = TelegramClient(
                session,
                self.api_id,
                self.api_hash,
                proxy=proxy
            )

            await client.connect()

            # Отправляем код
            result = await client.send_code_request(normalized_phone)

            # Генерируем токен для продолжения
            token = secrets.token_urlsafe(16)
            self._pending_clients[token] = client

            # Сохраняем данные сессии в pending_sessions для совместимости с verify_code
            session_data = {
                'token': token,
                'client': client,
                'phone': normalized_phone,
                'phone_code_hash': result.phone_code_hash,
                'proxy': proxy,
                'proxy_url': proxy_url,
                'created_at': datetime.now()
            }
            self.pending_sessions[normalized_phone] = session_data

            return True, "Код отправлен"

        except Exception as e:
            logger.error(f"Ошибка отправки кода (send_code): {str(e)}")
            return False, f"Ошибка отправки кода: {str(e)}"

    async def verify_code(self, phone: str, code: str, twofa_password: Optional[str] = None) -> tuple[bool, str, Optional[dict]]:
        """Верифицирует код и возвращает информацию о пользователе"""
        # Нормализуем номер телефона
        phone = self._normalize_phone(phone)

        if phone not in self.pending_sessions:
            return False, "Сессия авторизации не найдена. Запросите код заново", None

        session_data = self.pending_sessions[phone]
        client = session_data.get('client')
        proxy_url = session_data.get('proxy_url')

        if not client:
            return False, "Сессия авторизации не найдена или устарела", None

        try:
            # Очищаем код от пробелов и дефисов
            clean_code = code.replace(' ', '').replace('-', '')
            phone_code_hash = session_data.get('phone_code_hash')
            logger.info(f"Попытка входа с кодом {clean_code} для {phone}")

            try:
                # Пробуем войти с кодом
                await client.sign_in(phone=phone, code=clean_code, phone_code_hash=phone_code_hash)
            except SessionPasswordNeededError:
                logger.info(f"Требуется 2FA для {phone} (verify_code)")
                if not twofa_password:
                    # Если требуется 2FA, но пароль не дали — просим ещё раз
                    return False, "Включена двухфакторная аутентификация. Введите пароль от аккаунта", None
                # Если пароль дали — пробуем с ним
                try:
                    await client.sign_in(password=twofa_password)
                except Exception as twofa_error:
                    logger.error(f"Ошибка 2FA (verify_code): {twofa_error}")
                    if "password" in str(twofa_error).lower() and "invalid" in str(twofa_error).lower():
                        return False, "Неверный пароль двухфакторной аутентификации", None
                    else:
                        return False, f"Ошибка двухфакторной аутентификации: {str(twofa_error)}", None

            # Получаем информацию о пользователе
            me = await client.get_me()

            # Получаем строку сессии
            session_string = client.session.save()

            # Сохраняем аккаунт в базу данных
            with Session(engine) as db_session:
                # Проверяем, не существует ли уже такой аккаунт
                existing_account = db_session.exec(
                    select(Account).where(Account.phone == phone)
                ).first()

                if existing_account:
                    # Обновляем существующий аккаунт
                    existing_account.session_string = session_string
                    existing_account.is_active = True
                    existing_account.proxy_url = session_data.get('proxy_url')
                    db_session.add(existing_account)
                    account = existing_account
                else:
                    # Создаем новый аккаунт
                    account = Account(
                        title=f"{me.first_name} {me.last_name or ''}".strip() or phone,
                        phone=phone,
                        api_id=self.api_id,
                        api_hash=self.api_hash,
                        session_string=session_string,
                        proxy_url=session_data.get('proxy_url'),
                        is_active=True
                    )
                    db_session.add(account)

                db_session.commit()
                db_session.refresh(account)

                user_info = {
                    'account_id': account.id,
                    'first_name': me.first_name,
                    'last_name': me.last_name,
                    'username': me.username,
                    'phone': me.phone
                }

            # Закрываем клиента
            await client.disconnect()

            # Удаляем временную сессию
            if phone in self.pending_sessions:
                del self.pending_sessions[phone]

            return True, "Успешно авторизован", user_info

        except Exception as e:
            logger.error(f"Ошибка верификации кода (verify_code): {str(e)}", exc_info=True)
            try:
                await client.disconnect()
            except:
                pass

            if phone in self.pending_sessions:
                del self.pending_sessions[phone]

            # Детальная обработка различных типов ошибок
            error_str = str(e).lower()

            if "phonecodemptyerror" in error_str or "phone_code_empty" in error_str:
                return False, "Код не может быть пустым", None
            elif "phonecodexpirederror" in error_str or "phone_code_expired" in error_str:
                return False, "Код истек. Запросите новый код", None
            elif "phonecodeinvaliderror" in error_str or "phone_code_invalid" in error_str:
                return False, "Неверный код. Проверьте правильность ввода", None
            elif "phonecodehashemptyerror" in error_str:
                return False, "Сессия истекла. Запросите новый код", None
            elif "floodwaiterror" in error_str or "flood_wait" in error_str:
                return False, "Слишком много попыток. Подождите 10-15 минут", None
            elif "unauthorized" in error_str:
                return False, "Ошибка авторизации. Начните процесс заново", None
            elif "connection" in error_str or "network" in error_str:
                return False, "Ошибка подключения к Telegram. Проверьте интернет или настройки прокси", None
            elif "timeout" in error_str:
                return False, "Превышено время ожидания. Проверьте интернет-соединение", None
            elif "proxy" in error_str:
                return False, "Ошибка прокси. Проверьте настройки прокси или попробуйте без прокси", None
            else:
                return False, f"Ошибка: {str(e)}", None


# Глобальный экземпляр менеджера авторизации
auth_manager = TelegramAuthManager()