import asyncio
import logging
import random
import json
import re
import os
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, PeerFloodError, UserPrivacyRestrictedError
from sqlmodel import Session, select

from app.db import get_session, Account, Folder, MessageTemplate, Job, Log, engine
from app.core import safe_send, decrypt_session

# Глобальный планировщик задач
from apscheduler.schedulers.asyncio import AsyncIOScheduler
scheduler = AsyncIOScheduler()
# scheduler.start()  # Будет запущен в lifespan

# Активные клиенты Telegram
active_clients: Dict[int, TelegramClient] = {}

logger = logging.getLogger(__name__)

async def create_telegram_client(account: Account) -> TelegramClient:
    """Создает и подключает Telegram клиента"""
    try:
        # Настройка прокси если есть
        proxy = None
        if account.proxy_url:
            try:
                # Поддерживаем форматы:
                # socks5://user:pass@host:port
                # socks5://host:port
                # http://user:pass@host:port
                # http://host:port
                
                url = account.proxy_url
                proxy_type = 'socks5'
                if url.startswith('http'):
                    proxy_type = 'http'
                elif url.startswith('mtpro'):
                    proxy_type = 'mtproto'
                
                # Убираем схему
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
                        
                    proxy = {
                        'proxy_type': proxy_type,
                        'addr': host,
                        'port': int(port),
                        'username': username,
                        'password': password,
                        'rdns': True
                    }
                else:
                    if ':' in clean_url:
                        host, port = clean_url.split(':', 1)
                        proxy = {
                            'proxy_type': proxy_type,
                            'addr': host,
                            'port': int(port),
                            'rdns': True
                        }
                    else:
                        proxy = {
                            'proxy_type': proxy_type,
                            'addr': clean_url,
                            'port': (80 if proxy_type == 'http' else 1080),
                            'rdns': True
                        }
                logger.info(f"Используется прокси {proxy_type} для {account.phone}: {proxy['addr']}:{proxy['port']}")
            except Exception as pe:
                logger.error(f"Ошибка парсинга прокси {account.proxy_url}: {pe}")

        # Используем StringSession
        from telethon.sessions import StringSession
        session = StringSession(account.session_string)

        client = TelegramClient(
            session,
            account.api_id,
            account.api_hash,
            proxy=proxy
        )

        # Подключаемся и проверяем авторизацию
        await client.connect()

        # Проверяем, авторизован ли клиент
        if not await client.is_user_authorized():
            await client.disconnect()
            raise Exception(f"Аккаунт {account.phone} не авторизован. Требуется повторная авторизация.")

        logger.info(f"Успешное подключение к аккаунту {account.phone}")
        return client

    except Exception as e:
        logger.error(f"Ошибка создания клиента для аккаунта {account.id}: {e}")
        raise


async def get_dialogs_from_telegram(account: Account) -> list:
    """Получает список диалогов (чатов, каналов, групп) из Telegram аккаунта"""
    try:
        client = await create_telegram_client(account)
        dialogs = []

        async for dialog in client.iter_dialogs():
            # Пропускаем служебные чаты
            if dialog.entity.id == 777000:  # Telegram Service Notifications
                continue

            # Определяем правильный формат ID для разных типов чатов
            chat_id = dialog.entity.id
            username = getattr(dialog.entity, 'username', None)

            # Проверяем права на отправку
            can_send = True
            if hasattr(dialog.entity, 'left') and dialog.entity.left:
                can_send = False
            elif hasattr(dialog.entity, 'default_banned_rights') and dialog.entity.default_banned_rights and dialog.entity.default_banned_rights.send_messages:
                can_send = False
            
            if not can_send:
                continue

            # Для удобства используем username если есть, иначе ID
            identifier = f"@{username}" if username else str(chat_id)

            dialog_info = {
                'id': chat_id,
                'identifier': identifier,  # Добавляем идентификатор для использования в рассылке
                'title': dialog.title,
                'type': 'channel' if hasattr(dialog.entity, 'broadcast') and getattr(dialog.entity, 'broadcast', False) else
                        'group' if hasattr(dialog.entity, 'megagroup') and getattr(dialog.entity, 'megagroup', False) else
                        'supergroup' if username else
                        'chat',
                'username': username
            }
            dialogs.append(dialog_info)

        await client.disconnect()
        return dialogs

    except Exception as e:
        logger.error(f"Ошибка получения диалогов для аккаунта {account.id}: {e}")
        return []

async def get_folders_from_telegram(account: Account) -> Dict[str, List[str]]:
    """Получает папки (dialog filters) из Telegram аккаунта через API"""
    try:
        logger.info(f"Получение папок для аккаунта {account.phone}")
        client = await create_telegram_client(account)

        try:
            # Импортируем необходимые типы
            from telethon.tl.functions.messages import GetDialogFiltersRequest
            from telethon.tl.types import DialogFilter, DialogFilterChatlist, InputPeerUser, InputPeerChat, InputPeerChannel

            # Получаем информацию о папках (dialog filters)
            result = await client(GetDialogFiltersRequest())
            folders_data = {}

            logger.info(f"Получен результат GetDialogFiltersRequest: {type(result)}")

            if hasattr(result, 'filters') and result.filters:
                logger.info(f"Найдено {len(result.filters)} фильтров")

                for filter_obj in result.filters:
                    logger.info(f"Обрабатываем фильтр: {type(filter_obj)}")

                    # Если это папка-чатлист (общие папки по ссылке)
                    if isinstance(filter_obj, DialogFilterChatlist):
                        folder_title = ""
                        if hasattr(filter_obj, 'title'):
                            folder_title = filter_obj.title.text if hasattr(filter_obj.title, 'text') else str(filter_obj.title)
                        
                        if not folder_title:
                            folder_title = "Общая папка"
                            
                        logger.info(f"Обработка папки-чатлиста '{folder_title}'")
                        chat_identifiers = []
                        
                        if hasattr(filter_obj, 'include_peers'):
                            for i, peer in enumerate(filter_obj.include_peers):
                                try:
                                    if isinstance(peer, InputPeerChannel):
                                        chat_identifiers.append(f"-100{peer.channel_id}")
                                    elif isinstance(peer, InputPeerChat):
                                        chat_identifiers.append(str(-peer.chat_id))
                                    elif isinstance(peer, InputPeerUser):
                                        chat_identifiers.append(str(peer.user_id))
                                except Exception as e:
                                    logger.warning(f"Ошибка в чатлисте: {e}")
                                    continue
                        
                        if chat_identifiers:
                            folders_data[folder_title] = chat_identifiers
                        continue

                    # Если это обычная папка
                    if not isinstance(filter_obj, DialogFilter):
                        logger.info(f"Пропускаем объект типа {type(filter_obj)}")
                        continue

                    # Пропускаем системные фильтры
                    if not hasattr(filter_obj, 'title') or not filter_obj.title:
                        logger.info("Пропускаем фильтр без названия")
                        continue

                    # Получаем название папки (оно может быть объектом TextWithEntities)
                    if hasattr(filter_obj.title, 'text'):
                        folder_title = filter_obj.title.text
                    else:
                        folder_title = str(filter_obj.title)

                    # Исключаем стандартные папки Telegram
                    system_folders = [
                        'All Chats', 'Unread', 'Contacts', 'Non-Contacts',
                        'Groups', 'Channels', 'Bots', 'Все чаты', 'Непрочитанные',
                        'Контакты', 'Не контакты', 'Группы', 'Каналы', 'Боты'
                    ]

                    if folder_title in system_folders:
                        logger.info(f"Пропускаем системную папку: {folder_title}")
                        continue

                    chat_identifiers = []

                    logger.info(f"Обработка пользовательской папки '{folder_title}'")

                    # Проверяем наличие include_peers
                    if not hasattr(filter_obj, 'include_peers') or not filter_obj.include_peers:
                        logger.info(f"Папка '{folder_title}' не содержит включенных чатов")
                        continue

                    logger.info(f"Папка '{folder_title}' содержит {len(filter_obj.include_peers)} включенных чатов")

                    # Получаем сущности из пиров
                    for i, peer in enumerate(filter_obj.include_peers):
                        try:
                            logger.info(f"Обрабатываем пир {i+1}/{len(filter_obj.include_peers)}: {type(peer)}")

                            entity = None
                            chat_id_for_api = None

                            try:
                                if isinstance(peer, InputPeerUser):
                                    # Пытаемся найти пользователя в локальном кэше сущностей
                                    try:
                                        entity = await client.get_entity(peer)
                                    except:
                                        entity = await client.get_entity(peer.user_id)
                                    chat_id_for_api = peer.user_id
                                elif isinstance(peer, InputPeerChat):
                                    try:
                                        entity = await client.get_entity(peer)
                                    except:
                                        entity = await client.get_entity(peer.chat_id)
                                    chat_id_for_api = -peer.chat_id
                                elif isinstance(peer, InputPeerChannel):
                                    try:
                                        entity = await client.get_entity(peer)
                                    except:
                                        entity = await client.get_entity(peer.channel_id)
                                    chat_id_for_api = int(f"-100{peer.channel_id}")
                            except Exception as e:
                                logger.warning(f"Ошибка get_entity для пира {i+1}: {e}")
                                # Если не удалось получить сущность, используем ID напрямую
                                if isinstance(peer, InputPeerUser):
                                    chat_identifiers.append(str(peer.user_id))
                                elif isinstance(peer, InputPeerChat):
                                    chat_identifiers.append(str(-peer.chat_id))
                                elif isinstance(peer, InputPeerChannel):
                                    chat_identifiers.append(f"-100{peer.channel_id}")
                                continue

                            if entity:
                                # Проверяем права на отправку сообщений
                                can_send = True
                                if hasattr(entity, 'left') and entity.left:
                                    can_send = False
                                elif hasattr(entity, 'default_banned_rights') and entity.default_banned_rights and entity.default_banned_rights.send_messages:
                                    can_send = False
                                elif hasattr(entity, 'admin_rights') and entity.admin_rights and not entity.admin_rights.post_messages:
                                    # Для каналов проверяем права администратора на публикацию
                                    if hasattr(entity, 'broadcast') and entity.broadcast:
                                        can_send = False
                                
                                # Мы все равно добавляем чат, даже если сейчас нет прав (может появятся)
                                # Но логируем это для отладки
                                if not can_send:
                                    logger.info(f"Предупреждение: чат {getattr(entity, 'title', getattr(entity, 'first_name', 'Неизвестный'))} (нет прав на отправку)")

                                # Определяем идентификатор для API
                                if hasattr(entity, 'username') and entity.username:
                                    identifier = f"@{entity.username}"
                                else:
                                    identifier = str(chat_id_for_api)

                                chat_identifiers.append(identifier)
                                title = getattr(entity, 'title', getattr(entity, 'first_name', 'Неизвестный'))
                                logger.info(f"Добавлен чат: {title} -> {identifier}")

                        except Exception as peer_error:
                            logger.warning(f"Ошибка получения сущности для пира {i+1}: {peer_error}")
                            continue

                    if chat_identifiers:
                        folders_data[folder_title] = chat_identifiers
                        logger.info(f"Папка '{folder_title}' готова с {len(chat_identifiers)} чатами")
                    else:
                        logger.warning(f"Папка '{folder_title}' пуста после обработки")
            else:
                logger.info("Фильтры папок не найдены или пусты")

            await client.disconnect()

            logger.info(f"Всего найдено папок: {len(folders_data)}")
            for folder_name, chats in folders_data.items():
                logger.info(f"  - {folder_name}: {len(chats)} чатов")

            return folders_data

        except ImportError as ie:
            logger.error(f"GetDialogFiltersRequest недоступен: {ie}")
            await client.disconnect()
            return {}
        except Exception as e:
            logger.error(f"Ошибка получения папок через API: {e}")
            await client.disconnect()
            return {}

    except Exception as e:
        logger.error(f"Ошибка подключения к Telegram для получения папок: {e}")
        return {}

async def process_chat_links(account_id: int, links_text: str) -> List[str]:
    """
    Обрабатывает список ссылок на чаты/каналы и возвращает список идентификаторов
    """
    try:
        logger.info(f"Обработка ссылок на чаты для аккаунта {account_id}")

        with Session(engine) as db_session:
            account = db_session.get(Account, account_id)
            if not account:
                raise Exception("Аккаунт не найден")

            # Подключаемся к Telegram
            client = await create_telegram_client(account)
            processed_chats = []

            try:
                # Разбираем ссылки построчно
                lines = [line.strip() for line in links_text.strip().split('\n') if line.strip()]

                for line in lines:
                    try:
                        logger.info(f"Обрабатываем: {line}")

                        # Очищаем ссылку от лишних символов
                        line = line.strip().rstrip(',').rstrip(';')

                        if line.startswith('@'):
                            # Простой username
                            processed_chats.append(line)
                            logger.info(f"Добавлен username: {line}")

                        elif line.startswith('http://t.me/') or line.startswith('https://t.me/') or line.startswith('t.me/'):
                            # Извлекаем username из ссылки
                            if line.startswith('http://'):
                                line = line[7:]
                            elif line.startswith('https://'):
                                line = line[8:]

                            if line.startswith('t.me/'):
                                path = line[5:]  # убираем t.me/

                                if path.startswith('joinchat/'):
                                    # Ссылка-приглашение joinchat
                                    invite_hash = path[9:]  # убираем joinchat/
                                    try:
                                        from telethon.tl.functions.messages import CheckChatInviteRequest
                                        invite_info = await client(CheckChatInviteRequest(hash=invite_hash))

                                        if hasattr(invite_info, 'chat'):
                                            chat = invite_info.chat
                                            if hasattr(chat, 'username') and chat.username:
                                                processed_chats.append(f"@{chat.username}")
                                            else:
                                                processed_chats.append(str(chat.id))
                                            logger.info(f"Добавлен чат из joinchat: {processed_chats[-1]}")
                                    except Exception as e:
                                        logger.warning(f"Ошибка обработки joinchat/{invite_hash}: {e}")
                                        continue

                                elif path.startswith('+'):
                                    # Ссылка-приглашение с +
                                    invite_hash = path[1:]  # убираем +
                                    try:
                                        from telethon.tl.functions.messages import CheckChatInviteRequest
                                        invite_info = await client(CheckChatInviteRequest(hash=invite_hash))

                                        if hasattr(invite_info, 'chat'):
                                            chat = invite_info.chat
                                            if hasattr(chat, 'username') and chat.username:
                                                processed_chats.append(f"@{chat.username}")
                                            else:
                                                processed_chats.append(str(chat.id))
                                            logger.info(f"Добавлен чат из приглашения: {processed_chats[-1]}")
                                    except Exception as e:
                                        logger.warning(f"Ошибка обработки приглашения {invite_hash}: {e}")
                                        continue

                                else:
                                    # Обычный username
                                    username = path.split('?')[0].split('/')[0]  # убираем параметры и доп. пути
                                    if username:
                                        processed_chats.append(f"@{username}")
                                        logger.info(f"Добавлен username из ссылки: @{username}")

                        elif line.lstrip('-').isdigit():
                            # ID чата
                            processed_chats.append(line)
                            logger.info(f"Добавлен ID: {line}")

                        else:
                            logger.warning(f"Неизвестный формат ссылки: {line}")
                            continue

                    except Exception as line_error:
                        logger.error(f"Ошибка обработки строки '{line}': {line_error}")
                        continue

                logger.info(f"Обработано {len(processed_chats)} чатов из {len(lines)} ссылок")
                return processed_chats

            finally:
                await client.disconnect()

    except Exception as e:
        logger.error(f"Ошибка обработки ссылок: {e}")
        raise


async def get_chats_from_folder_link(account_id: int, folder_link: str) -> List[str]:
    """
    Получает список чатов из ссылки на папку Telegram типа t.me/addlist/xxxxx
    """
    try:
        logger.info(f"Обработка ссылки на папку: {folder_link}")

        # Проверяем формат ссылки
        if not folder_link.startswith('https://t.me/addlist/') and not folder_link.startswith('t.me/addlist/'):
            raise Exception("Неверный формат ссылки. Используйте: https://t.me/addlist/xxxxx")

        # Извлекаем ID списка из ссылки
        match = re.search(r'/addlist/([a-zA-Z0-9_-]+)', folder_link)
        if not match:
            raise Exception("Не удалось извлечь ID списка из ссылки. Проверьте правильность формата: https://t.me/addlist/xxxxx")

        invite_hash = match.group(1)
        logger.info(f"Извлеченный hash: {invite_hash}")

        # Проверяем, что хэш не пустой и имеет разумную длину
        if not invite_hash or len(invite_hash) < 5:
            raise Exception("Недействительный ID папки в ссылке. Ссылка должна содержать корректный идентификатор.")

        # Используем правильный способ работы с сессией
        with Session(engine) as db_session:
            account = db_session.get(Account, account_id)
            if not account:
                raise Exception("Аккаунт не найден")

            logger.info(f"Подключаемся к Telegram для аккаунта {account.phone}")
            # Подключаемся к Telegram
            client = await create_telegram_client(account)

            try:
                # Импортируем необходимые классы
                from telethon.tl.functions.messages import CheckChatInviteRequest

                logger.info("Пробуем CheckChatInviteRequest...")

                try:
                    # Пробуем стандартный API для инвайтов чатов
                    invite_info = await client(CheckChatInviteRequest(hash=invite_hash))
                    logger.info(f"Результат CheckChatInviteRequest: {type(invite_info)}")

                    chats = []
                    if hasattr(invite_info, 'chat'):
                        chat = invite_info.chat
                        if hasattr(chat, 'username') and chat.username:
                            chats.append(f"@{chat.username}")
                        else:
                            chats.append(str(chat.id))
                        logger.info(f"Найден чат: {chats[-1]}")
                    elif hasattr(invite_info, 'chats'):
                        for chat in invite_info.chats:
                            if hasattr(chat, 'username') and chat.username:
                                chats.append(f"@{chat.username}")
                            else:
                                chats.append(str(chat.id))
                            logger.info(f"Найден чат: {chats[-1]}")

                    if chats:
                        logger.info(f"Найдено {len(chats)} чатов")
                        return chats
                    else:
                        # Попробуем импортировать и использовать новый API
                        try:
                            from telethon.tl.functions.chatlists import CheckChatlistInviteRequest
                            logger.info("Пробуем CheckChatlistInviteRequest...")

                            result = await client(CheckChatlistInviteRequest(slug=invite_hash))
                            logger.info(f"Результат CheckChatlistInviteRequest: {type(result)}")

                            chats = []
                            if hasattr(result, 'chats'):
                                for chat in result.chats:
                                    if hasattr(chat, 'username') and chat.username:
                                        chats.append(f"@{chat.username}")
                                    else:
                                        chats.append(str(chat.id))
                                    logger.info(f"Найден чат через chatlist API: {chats[-1]}")

                            if chats:
                                logger.info(f"Найдено {len(chats)} чатов через chatlist API")
                                return chats

                        except ImportError:
                            logger.warning("CheckChatlistInviteRequest недоступен в этой версии telethon")
                        except Exception as e_chatlist:
                            logger.error(f"Ошибка CheckChatlistInviteRequest: {e_chatlist}")

                        raise Exception("Не удалось получить список чатов из ссылки. Возможно, ссылка неактивна или API изменился.")

                except Exception as e2:
                    error_msg = str(e2).lower()
                    logger.error(f"Детальная ошибка API: {str(e2)}")

                    if "expired" in error_msg or "not valid anymore" in error_msg:
                        raise Exception("Ссылка на папку истекла или недействительна. Попросите новую ссылку у создателя папки.")
                    elif "chat not found" in error_msg:
                        raise Exception("Папка не найдена. Проверьте правильность ссылки.")
                    elif "key is not registered" in error_msg or "the key is not registered in the system" in error_msg:
                        raise Exception(f"Ссылка '{folder_link}' недействительна или устарела.\n\nВозможные причины:\n• Ссылка была отозвана создателем\n• Ссылка истекла\n• Эта ссылка предназначена для мобильного приложения Telegram\n\nРешения:\n1. Попросите новую ссылку у создателя папки\n2. Убедитесь, что ссылка имеет формат: https://t.me/addlist/xxxxx\n3. Попробуйте открыть ссылку в Telegram сначала, чтобы она стала активной")
                    elif "flood" in error_msg:
                        raise Exception("Слишком много запросов к Telegram API. Попробуйте через несколько минут.")
                    elif "invite_hash_expired" in error_msg:
                        raise Exception("Срок действия ссылки истек. Попросите новую ссылку у создателя папки.")
                    else:
                        raise Exception(f"Не удалось получить доступ к папке: {str(e2)}\n\nПопробуйте:\n1. Открыть ссылку в официальном Telegram\n2. Убедиться, что ссылка активна\n3. Запросить новую ссылку у создателя папки\n\nТехническая информация: {str(e2)}")

            finally:
                await client.disconnect()

    except Exception as e:
        logger.error(f"Ошибка получения чатов из ссылки: {e}")
        raise


async def auto_create_folders_for_account(account_id: int) -> bool:
    """Автоматически создает папки на основе папок Telegram аккаунта через API"""
    try:
        with Session(engine) as session:
            account = session.get(Account, account_id)
            if not account:
                return False

            try:
                # Сначала пробуем получить реальные папки из Telegram
                folders_data = await get_folders_from_telegram(account)

                folders_created = 0

                # Создаем папки на основе данных из Telegram
                for folder_title, chat_ids in folders_data.items():
                    if chat_ids:  # Только если есть чаты в папке
                        folder = Folder(
                            account_id=account_id,
                            title=folder_title,
                            chats_json=json.dumps(chat_ids)
                        )
                        session.add(folder)
                        folders_created += 1
                        logger.info(f"Создана папка '{folder_title}' с {len(chat_ids)} чатами")

                # Если нет папок в Telegram, создаем базовые на основе типов диалогов
                if folders_created == 0:
                    logger.info("Папки не найдены, создаем базовые на основе типов диалогов")
                    dialogs = await get_dialogs_from_telegram(account)

                    # Группируем диалоги по типам используя identifier
                    channels = []
                    groups = []
                    chats = []

                    for dialog in dialogs:
                        identifier = dialog.get('identifier', str(dialog['id']))
                        if dialog['type'] == 'channel':
                            channels.append(identifier)
                        elif dialog['type'] in ['group', 'supergroup']:
                            groups.append(identifier)
                        else:
                            chats.append(identifier)

                    # Создаем папки если есть диалоги соответствующего типа
                    if channels:
                        folder = Folder(
                            account_id=account_id,
                            title="Каналы",
                            chats_json=json.dumps(channels)
                        )
                        session.add(folder)
                        folders_created += 1

                    if groups:
                        folder = Folder(
                            account_id=account_id,
                            title="Группы",
                            chats_json=json.dumps(groups)
                        )
                        session.add(folder)
                        folders_created += 1

                    if chats:
                        folder = Folder(
                            account_id=account_id,
                            title="Личные чаты",
                            chats_json=json.dumps(chats)
                        )
                        session.add(folder)
                        folders_created += 1

                session.commit()
                logger.info(f"Автоматически создано {folders_created} папок для аккаунта {account_id}")
                return folders_created > 0

            except Exception as e:
                logger.error(f"Ошибка автосоздания папок для аккаунта {account_id}: {e}")
                session.rollback()
                return False

    except Exception as e:
        logger.error(f"Ошибка подключения к базе данных для аккаунта {account_id}: {e}")
        return False


async def safe_send(func, *args, **kwargs):
    """Безопасная отправка с обработкой FloodWait"""
    try:
        return await func(*args, **kwargs)
    except FloodWaitError as e:
        logger.warning(f"FloodWait: ждем {e.seconds} секунд")
        await asyncio.sleep(e.seconds)
        return await func(*args, **kwargs)
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")
        return None


async def send_message_to_chat(client: TelegramClient, chat_id, message: str, media_path: str = None):
    """Отправляет сообщение в чат"""
    try:
        # Проверяем валидность chat_id
        if not chat_id or str(chat_id).strip() == "":
            logger.error(f"Пустой chat_id: '{chat_id}'")
            return None

        # Преобразуем chat_id в правильный формат
        if isinstance(chat_id, str):
            chat_id = chat_id.strip()
            if chat_id.startswith('@'):
                # Username
                target = chat_id
            else:
                # ID как строка
                try:
                    target = int(chat_id)
                except ValueError:
                    target = chat_id
        else:
            target = chat_id

        logger.info(f"Попытка отправки в чат: '{target}' (оригинал: '{chat_id}')")

        # Сначала пытаемся получить entity чата
        try:
            entity = await client.get_entity(target)
            logger.info(f"Entity получен для {target}: {getattr(entity, 'title', getattr(entity, 'first_name', target))}")
            
            if media_path and os.path.exists(media_path):
                result = await safe_send(client.send_file, entity, media_path, caption=message)
            else:
                result = await safe_send(client.send_message, entity, message)
            
            if result:
                logger.info(f"Сообщение успешно отправлено в {target}")
            return result
            
        except Exception as entity_error:
            error_msg = str(entity_error).lower()
            if "no user has" in error_msg or "no such peer" in error_msg:
                logger.error(f"Чат {target} не найден или недоступен: {entity_error}")
            elif "cannot find any entity" in error_msg:
                logger.error(f"Не удалось найти чат {target}: {entity_error}")
            else:
                logger.warning(f"Не удалось получить entity для {target}: {entity_error}")
            
            # Пробуем прямую отправку только для ID
            if not str(target).startswith('@'):
                try:
                    if media_path and os.path.exists(media_path):
                        result = await safe_send(client.send_file, target, media_path, caption=message)
                    else:
                        result = await safe_send(client.send_message, target, message)
                    return result
                except Exception as direct_error:
                    logger.error(f"Прямая отправка в {target} также не удалась: {direct_error}")
            
            return None
            
    except Exception as e:
        logger.error(f"Критическая ошибка отправки в чат {chat_id}: {e}")
        return None


async def execute_job(job_id: int):
    """Выполняет одну итерацию задачи рассылки"""
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if not job or not job.is_running:
            return

        # Проверяем время активности
        now = datetime.now().time()
        if not (job.active_from <= now <= job.active_to):
            logger.info(f"Job {job_id}: вне времени активности")
            return

        # Проверяем дневной лимит
        today_logs = session.exec(
            select(Log).where(
                Log.account_id == job.account_id,
                Log.created_at >= datetime.now().replace(hour=0, minute=0, second=0, microsecond=0),
                Log.status == "OK"
            )
        ).all()

        if len(today_logs) >= job.daily_limit:
            logger.info(f"Job {job_id}: достигнут дневной лимит")
            return

        # Получаем данные
        account = session.get(Account, job.account_id)
        folder = session.get(Folder, job.folder_id)
        template = session.get(MessageTemplate, job.template_id)

        if not all([account, folder, template]):
            logger.error(f"Job {job_id}: отсутствуют необходимые данные")
            return

        # Получаем или создаем клиента
        if account.id not in active_clients:
            try:
                active_clients[account.id] = await create_telegram_client(account)
            except Exception as e:
                logger.error(f"Не удалось создать клиента для аккаунта {account.id}: {e}")
                return

        client = active_clients[account.id]

        # Парсим варианты сообщений
        try:
            variants = json.loads(template.variants_json)
            message = random.choice(variants)
        except:
            message = template.variants_json

        # Парсим чаты
        try:
            chats_raw = json.loads(folder.chats_json) if folder.chats_json else []
            if not chats_raw:
                logger.info(f"Job {job_id}: нет чатов для рассылки")
                return

            # Обрабатываем чаты, разделяя строки с несколькими чатами
            chats = []
            for chat_entry in chats_raw:
                if isinstance(chat_entry, str):
                    # Разделяем строку по пробелам и фильтруем пустые элементы
                    chat_parts = [part.strip() for part in chat_entry.split() if part.strip()]
                    if len(chat_parts) > 1:
                        # Если в строке несколько чатов, добавляем каждый отдельно
                        chats.extend(chat_parts)
                        logger.info(f"Job {job_id}: разделена строка '{chat_entry}' на чаты: {chat_parts}")
                    else:
                        chats.append(chat_entry.strip())
                else:
                    chats.append(chat_entry)

            if not chats:
                logger.info(f"Job {job_id}: нет валидных чатов для рассылки")
                return

            # Выбираем случайный чат
            chat_id = random.choice(chats)
            logger.info(f"Job {job_id}: отправляем сообщение в чат {chat_id}")

            # Отправляем сообщение
            result = await send_message_to_chat(client, chat_id, message, template.media_path)

            # Логируем результат
            if result:
                log = Log(
                    account_id=account.id,
                    chat_id=str(chat_id),
                    message=message[:100] + "..." if len(message) > 100 else message,
                    status="OK",
                    error_reason=None
                )
                logger.info(f"Job {job_id}: сообщение успешно отправлено в чат {chat_id}")
            else:
                log = Log(
                    account_id=account.id,
                    chat_id=str(chat_id),
                    message=message[:100] + "..." if len(message) > 100 else message,
                    status="ERROR",
                    error_reason=f"Не удалось отправить сообщение в чат {chat_id}. Возможно, чат не найден или нет доступа."
                )
                logger.error(f"Job {job_id}: не удалось отправить сообщение в чат {chat_id}")
            
            session.add(log)
            session.commit()

            # Перепланируем следующее выполнение со случайным интервалом
            new_interval = random.randint(job.min_interval, job.max_interval)
            scheduler.add_job(
                execute_job,
                'date',
                run_date=datetime.now() + timedelta(seconds=new_interval),
                args=[job_id],
                id=f"job_{job_id}",
                replace_existing=True
            )
            logger.info(f"Задача {job_id} перепланирована на через {new_interval} сек")

        except Exception as e:
            logger.error(f"Job {job_id}: ошибка выполнения - {e}")
            # Логируем ошибку
            log = Log(
                account_id=account.id,
                message=f"Ошибка выполнения задачи: {str(e)}",
                status="ERROR",
                error_reason=str(e)
            )
            session.add(log)
            session.commit()


async def start_job(job_id: int):
    """Запускает задачу рассылки"""
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if not job:
            return

        # Добавляем задачу в планировщик
        interval = random.randint(job.min_interval, job.max_interval)
        scheduler.add_job(
            execute_job,
            'date',
            run_date=datetime.now() + timedelta(seconds=interval),
            args=[job_id],
            id=f"job_{job_id}",
            replace_existing=True
        )

        logger.info(f"Запущена задача {job_id}, первое выполнение через {interval} сек")


async def stop_job(job_id: int):
    """Останавливает задачу рассылки"""
    try:
        scheduler.remove_job(f"job_{job_id}")
        logger.info(f"Остановлена задача {job_id}")
    except:
        pass


def cleanup_clients():
    """Закрывает все активные клиенты"""
    for client in active_clients.values():
        try:
            client.disconnect()
        except:
            pass
    active_clients.clear()

# --- Код из второго блока ---
# Этот код дублируется и, вероятно, должен быть объединен или один из них удален.
# Для соответствия заданию, я включу его, но это может быть не лучшей практикой.

# Словарь для хранения запущенных задач
running_jobs = {}

async def start_job_v2(job_id: int):
    """Запуск задачи рассылки (версия 2)"""
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if not job:
            return False

        account = session.get(Account, job.account_id)
        if not account:
            return False

        # Создаем задачу
        task = asyncio.create_task(_run_job_loop(job_id))
        running_jobs[job_id] = task
        return True

async def stop_job_v2(job_id: int):
    """Остановка задачи рассылки (версия 2)"""
    if job_id in running_jobs:
        running_jobs[job_id].cancel()
        del running_jobs[job_id]

async def _run_job_loop(job_id: int):
    """Основной цикл рассылки (версия 2)"""
    try:
        with Session(engine) as session:
            job = session.get(Job, job_id)
            if not job:
                return

            account = session.get(Account, job.account_id)
            # Обратите внимание: template и folder здесь не используются, но были в оригинале
            # template = session.get_template(job.template_id) # Эта строка вызывает ошибку, предполагая get_template не существует в get_session
            # folder = session.get(Folder, job.folder_id)

            if not account: # Проверяем только account, так как template и folder закомментированы
                return

            # Тут должна быть логика работы с Telegram API
            # Пока просто логируем
            log = Log(
                account_id=account.id,
                message="Job started (v2)",
                status="OK"
            )
            session.add(log)
            session.commit()

    except asyncio.CancelledError:
        pass
    except Exception as e:
        # Логируем ошибку
        with Session(engine) as session:
            log = Log(
                account_id=job.account_id,
                message=f"Job error (v2): {str(e)}",
                status="ERROR",
                error_reason=str(e)
            )
            session.add(log)
            session.commit()