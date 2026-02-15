from fastapi import APIRouter, Depends, Form, HTTPException, Request, File, UploadFile
from fastapi.responses import RedirectResponse, HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlmodel import Session, select
from datetime import time, datetime, timedelta
import secrets
import csv
import io
import json
import os
from typing import Optional

from app.db import get_session, Account, Folder, MessageTemplate, Job, Log, User
from app import services
from app.auth_manager import auth_manager
import random
import logging

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Добавляем фильтр from_json для Jinja2
import json
def from_json_filter(value):
    try:
        return json.loads(value or "[]")
    except:
        return []

templates.env.filters['from_json'] = from_json_filter
security = HTTPBasic()

# Простая переменная для отслеживания авторизации
is_authenticated = False

# Глобальная переменная для хранения настроек прокси
current_proxy_url = None

def check_auth(request: Request):
    global is_authenticated
    print(f"🔍 Проверка авторизации: статус = {is_authenticated}")

    if not is_authenticated:
        print("❌ Не авторизован, перенаправляем на логин")
        raise HTTPException(
            status_code=302,
            detail="Not authenticated",
            headers={"Location": "/auth/login"}
        )

    print("✅ Авторизация успешна")
    return True

def _parse_hhmm(time_str: str) -> time:
    try:
        h, m = map(int, time_str.split(":"))
        return time(h, m)
    except:
        raise HTTPException(400, "Неверный формат времени")

import hashlib

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password: str, hashed: str) -> bool:
    return hash_password(password) == hashed

# =============================
# АВТОРИЗАЦИЯ ПО ПАРОЛЮ
# =============================
@router.get("/auth/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@router.post("/auth/login")
async def login(
    request: Request,
    password: str = Form(...),
    session: Session = Depends(get_session)
):
    global is_authenticated

    # Проверяем пароль администратора
    admin_user = session.exec(select(User).where(User.username == "admin")).first()
    if not admin_user:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Пользователь admin не найден"
        })

    if not verify_password(password, admin_user.password_hash):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Неверный пароль"
        })

    # Устанавливаем флаг авторизации
    is_authenticated = True
    print(f"✅ Авторизация успешна для администратора")
    return RedirectResponse("/", status_code=303)

@router.get("/auth/logout")
def logout():
    global is_authenticated
    is_authenticated = False
    print("🚪 Выход из системы")
    return RedirectResponse("/auth/login", status_code=303)



# =============================
# АВТОРИЗАЦИЯ TELEGRAM АККАУНТОВ
# =============================
@router.get("/telegram_auth", response_class=HTMLResponse)
def telegram_auth_page(request: Request, auth: bool = Depends(check_auth)):
    return templates.TemplateResponse("telegram_auth.html", {
        "request": request,
        "step": "phone"
    })

@router.post("/telegram_auth/send_code")
async def send_telegram_code(
    request: Request,
    phone: str = Form(...),
    proxy_url: str = Form(None),
    session: Session = Depends(get_session),
    auth: bool = Depends(check_auth)
):
    # Очищаем истекшие сессии
    auth_manager.cleanup_expired_sessions()

    # Отправляем код используя start_login для синхронизации
    success, message, token = await auth_manager.start_login(phone, proxy_url)

    if success:
        return templates.TemplateResponse("telegram_auth.html", {
            "request": request,
            "step": "code",
            "phone": auth_manager._normalize_phone(phone),
            "proxy_url": proxy_url
        })
    else:
        return templates.TemplateResponse("telegram_auth.html", {
            "request": request,
            "step": "phone",
            "error": message
        })

@router.post("/telegram_auth/verify_code")
async def verify_telegram_code(
    request: Request,
    phone: str = Form(...),
    code: str = Form(...),
    proxy_url: str = Form(None),
    twofa_password: str = Form(None),
    session: Session = Depends(get_session),
    auth: bool = Depends(check_auth)
):
    try:
        # Очищаем код от пробелов и нечисловых символов
        code = ''.join(filter(str.isdigit, code.strip()))
        
        # Проверяем корректность кода
        if not code or len(code) < 4 or len(code) > 8:
            logger.warning(f"Некорректный формат кода: длина {len(code)}")
            return templates.TemplateResponse("telegram_auth.html", {
                "request": request,
                "step": "code",
                "phone": phone,
                "proxy_url": proxy_url,
                "error": "Код должен содержать от 4 до 8 цифр. Проверьте код в Telegram и введите только цифры."
            })
        
        # Логируем попытку верификации
        logger.info(f"Попытка верификации кода для {phone}, код: {code[:2]}*** (длина: {len(code)})")
        
        # Верифицируем код (с возможным паролем 2FA)
        success, message, user_info = await auth_manager.confirm_login(phone, code, twofa_password)

        if success and user_info:
            logger.info(f"✅ Успешная авторизация для {phone}")
            return templates.TemplateResponse("telegram_auth.html", {
                "request": request,
                "step": "success",
                "user_info": user_info
            })
        elif "двухфакторная аутентификация" in message.lower() or "password" in message.lower():
            logger.info(f"🔐 Требуется 2FA для {phone}")
            return templates.TemplateResponse("telegram_auth.html", {
                "request": request,
                "step": "twofa",
                "phone": phone,
                "code": code,
                "proxy_url": proxy_url,
                "error": None
            })
        else:
            # Улучшенная обработка различных типов ошибок
            error_message = message
            
            if any(word in message.lower() for word in ["invalid", "wrong", "incorrect"]) and "code" in message.lower():
                error_message = "Неверный код. Убедитесь, что вы правильно скопировали код из Telegram и не истек срок его действия."
            elif any(word in message.lower() for word in ["expired", "timeout"]):
                error_message = "Код истек. Telegram коды действуют несколько минут. Запросите новый код."
            elif "flood" in message.lower() or "too many" in message.lower():
                error_message = "Слишком много попыток авторизации. Подождите 10-15 минут перед следующей попыткой."
            elif "phone" in message.lower() and any(word in message.lower() for word in ["invalid", "not registered"]):
                error_message = "Номер телефона не зарегистрирован в Telegram или указан неверно."
            elif "session" in message.lower():
                error_message = "Ошибка сессии. Попробуйте начать процесс авторизации заново."
            elif not message.strip():
                error_message = "Неизвестная ошибка при проверке кода. Попробуйте еще раз или запросите новый код."
            
            logger.error(f"❌ Ошибка верификации кода: {message}")
            
            # Определяем на какой шаг вернуться
            step = "twofa" if twofa_password else "code"
            return templates.TemplateResponse("telegram_auth.html", {
                "request": request,
                "step": step,
                "phone": phone,
                "code": code if step == "twofa" else None,
                "proxy_url": proxy_url,
                "error": error_message
            })
            
    except Exception as e:
        logger.error(f"❌ Исключение при верификации кода: {e}", exc_info=True)
        
        # Более детальное логирование ошибки
        error_details = str(e)
        user_friendly_error = "Произошла техническая ошибка при проверке кода."
        
        if "connection" in error_details.lower():
            user_friendly_error = "Ошибка подключения к Telegram. Проверьте интернет-соединение и настройки прокси."
        elif "timeout" in error_details.lower():
            user_friendly_error = "Превышено время ожидания. Попробуйте еще раз или используйте прокси."
        elif "proxy" in error_details.lower():
            user_friendly_error = "Ошибка прокси-соединения. Проверьте настройки прокси или попробуйте без прокси."
        elif "api" in error_details.lower():
            user_friendly_error = "Ошибка Telegram API. Попробуйте через несколько минут."
        
        return templates.TemplateResponse("telegram_auth.html", {
            "request": request,
            "step": "code",
            "phone": phone,
            "proxy_url": proxy_url,
            "error": f"{user_friendly_error} Детали: {error_details}"
        })

# =============================
# ГЛАВНАЯ СТРАНИЦА
# =============================
@router.get("/", response_class=HTMLResponse)
def index(request: Request, auth: bool = Depends(check_auth)):
    return templates.TemplateResponse("base.html", {"request": request})

# =============================
# АККАУНТЫ
# =============================
@router.get("/accounts", response_class=HTMLResponse)
def list_accounts(request: Request, session: Session = Depends(get_session), auth: bool = Depends(check_auth)):
    accounts = session.exec(select(Account)).all()
    return templates.TemplateResponse("accounts.html", {"request": request, "accounts": accounts})





@router.post("/accounts/add")
async def add_account(
    title: str = Form(...),
    phone: str = Form(...),
    api_id: int = Form(...),
    api_hash: str = Form(...),
    proxy_url: str = Form(None),
    session: Session = Depends(get_session),
    auth: bool = Depends(check_auth)
):
    try:
        # Создаем Telegram клиента для получения сессии
        from telethon import TelegramClient
        import tempfile

        # Создаем временный файл для сессии
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            session_file = tmp.name

        client = TelegramClient(session_file, api_id, api_hash)
        await client.connect()
        await client.start(phone=phone)

        # Получаем строку сессии
        session_string = client.session.save()
        await client.disconnect()

        # Удаляем временный файл
        import os
        os.unlink(session_file)

        account = Account(
            title=title,
            phone=phone,
            api_id=api_id,
            api_hash=api_hash,
            session_string=session_string,
            proxy_url=proxy_url,
            is_active=True
        )
        session.add(account)
        session.commit()

        return RedirectResponse("/accounts", status_code=303)

    except Exception as e:
        logger.error(f"Error in add_account: {e}")
        return templates.TemplateResponse("accounts.html", {
            "request": request,
            "accounts": session.exec(select(Account)).all(),
            "error": f"Ошибка подключения: {str(e)}"
        })

# =============================
# ПАПКИ
# =============================
@router.get("/folders/{account_id}", response_class=HTMLResponse)
def list_folders(account_id: int, request: Request, session: Session = Depends(get_session), auth: bool = Depends(check_auth)):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")
    folders = session.exec(select(Folder).where(Folder.account_id == account_id)).all()
    return templates.TemplateResponse("folders.html", {
        "request": request,
        "account": account,
        "folders": folders
    })

@router.post("/folders/{account_id}/add")
async def add_folder(
    request: Request,
    account_id: int,
    title: str = Form(...),
    chat_links: str = Form(""),
    manual_chats: str = Form(""),
    session: Session = Depends(get_session),
    auth: bool = Depends(check_auth)
):
    try:
        account = session.get(Account, account_id)
        if not account:
            return templates.TemplateResponse("folders.html", {
                "request": request,
                "account": None,
                "folders": [],
                "error": "Аккаунт не найден"
            })

        chats = []

        # Приоритет: сначала обрабатываем ссылки, потом ручной ввод
        if chat_links.strip():
            try:
                logger.info(f"Обработка ссылок для аккаунта {account.phone}")
                chats = await services.process_chat_links(account_id, chat_links.strip())
                if not chats:
                    return templates.TemplateResponse("folders.html", {
                        "request": request,
                        "account": account,
                        "folders": session.exec(select(Folder).where(Folder.account_id == account_id)).all(),
                        "error": "Не удалось получить чаты из указанных ссылок. Проверьте правильность ссылок и доступность чатов."
                    })
            except Exception as e:
                logger.error(f"Ошибка обработки ссылок: {e}")
                return templates.TemplateResponse("folders.html", {
                    "request": request,
                    "account": account,
                    "folders": session.exec(select(Folder).where(Folder.account_id == account_id)).all(),
                    "error": f"Ошибка обработки ссылок: {str(e)}"
                })
        elif manual_chats.strip():
            try:
                # Разбираем ручной ввод
                chat_list = [chat.strip() for chat in manual_chats.strip().split(',') if chat.strip()]
                if chat_list:
                    chats = chat_list
                else:
                    return templates.TemplateResponse("folders.html", {
                        "request": request,
                        "account": account,
                        "folders": session.exec(select(Folder).where(Folder.account_id == account_id)).all(),
                        "error": "Не удалось разобрать список чатов. Проверьте формат."
                    })
            except Exception as e:
                return templates.TemplateResponse("folders.html", {
                    "request": request,
                    "account": account,
                    "folders": session.exec(select(Folder).where(Folder.account_id == account_id)).all(),
                    "error": f"Ошибка обработки ручного ввода: {str(e)}"
                })
        else:
            return templates.TemplateResponse("folders.html", {
                "request": request,
                "account": account,
                "folders": session.exec(select(Folder).where(Folder.account_id == account_id)).all(),
                "error": "Необходимо указать ссылки на чаты или ввести их вручную"
            })

        # Автоматически назначаем название если не указано
        if not title.strip() or title.strip() == "Моя папка":
            if chat_links.strip():
                title = f"Импорт по ссылкам ({len(chats)} чатов)"
            else:
                title = f"Ручная папка ({len(chats)} чатов)"

        # Создаем папку
        folder = Folder(
            account_id=account_id,
            title=title,
            chats_json=json.dumps(chats)
        )
        session.add(folder)
        session.commit()

        return templates.TemplateResponse("folders.html", {
            "request": request,
            "account": account,
            "folders": session.exec(select(Folder).where(Folder.account_id == account_id)).all(),
            "success": f"Папка '{title}' создана с {len(chats)} чатами!"
        })

    except Exception as e:
        account = session.get(Account, account_id) if account_id else None
        return templates.TemplateResponse("folders.html", {
            "request": request,
            "account": account,
            "folders": session.exec(select(Folder).where(Folder.account_id == account_id)).all() if account else [],
            "error": f"Ошибка создания папки: {str(e)}"
        })

@router.post("/folders/{folder_id}/update")
def update_folder(
    request: Request,
    folder_id: int,
    title: str = Form(...),
    chats_json: str = Form(...),
    session: Session = Depends(get_session),
    auth: bool = Depends(check_auth)
):
    try:
        folder = session.get(Folder, folder_id)
        if not folder:
            raise HTTPException(404, "Папка не найдена")

        # Преобразуем в JSON если нужно
        if not chats_json.strip().startswith('['):
            chats = [chat.strip() for chat in chats_json.split(',') if chat.strip()]
            chats_json = json.dumps(chats)

        folder.title = title
        folder.chats_json = chats_json
        session.commit()

        account = session.get(Account, folder.account_id)
        return templates.TemplateResponse("folders.html", {
            "request": request,
            "account": account,
            "folders": session.exec(select(Folder).where(Folder.account_id == folder.account_id)).all(),
            "success": f"Папка '{title}' обновлена!"
        })
    except Exception as e:
        folder = session.get(Folder, folder_id)
        account = session.get(Account, folder.account_id) if folder else None
        return templates.TemplateResponse("folders.html", {
            "request": request,
            "account": account,
            "folders": session.exec(select(Folder).where(Folder.account_id == folder.account_id)).all() if folder else [],
            "error": f"Ошибка обновления: {str(e)}"
        })

@router.post("/folders/{folder_id}/restore_chat/{chat_id}")
async def restore_chat(
    folder_id: int,
    chat_id: str,
    session: Session = Depends(get_session),
    auth: bool = Depends(check_auth)
):
    folder = session.get(Folder, folder_id)
    if not folder:
        raise HTTPException(404, "Folder not found")
    
    chats = json.loads(folder.chats_json or "[]")
    failed_chats = json.loads(folder.failed_chats_json or "[]")
    reasons = json.loads(folder.failure_reasons_json or "{}")
    
    if chat_id in failed_chats:
        failed_chats.remove(chat_id)
        if chat_id not in chats:
            chats.append(chat_id)
        if chat_id in reasons:
            del reasons[chat_id]
            
        folder.chats_json = json.dumps(chats)
        folder.failed_chats_json = json.dumps(failed_chats)
        folder.failure_reasons_json = json.dumps(reasons)
        session.commit()
        
    return RedirectResponse(f"/folders/{folder.account_id}", status_code=303)

@router.post("/folders/{folder_id}/delete_chat/{chat_id}")
async def delete_failed_chat(
    folder_id: int,
    chat_id: str,
    session: Session = Depends(get_session),
    auth: bool = Depends(check_auth)
):
    folder = session.get(Folder, folder_id)
    if not folder:
        raise HTTPException(404, "Folder not found")
    
    failed_chats = json.loads(folder.failed_chats_json or "[]")
    reasons = json.loads(folder.failure_reasons_json or "{}")
    
    if chat_id in failed_chats:
        failed_chats.remove(chat_id)
        if chat_id in reasons:
            del reasons[chat_id]
            
        folder.failed_chats_json = json.dumps(failed_chats)
        folder.failure_reasons_json = json.dumps(reasons)
        session.commit()
        
    return RedirectResponse(f"/folders/{folder.account_id}", status_code=303)



@router.post("/folders/{account_id}/import_from_telegram")
async def import_folders_from_telegram(
    request: Request,
    account_id: int,
    session: Session = Depends(get_session),
    auth: bool = Depends(check_auth)
):
    try:
        account = session.get(Account, account_id)
        if not account:
            return templates.TemplateResponse("folders.html", {
                "request": request,
                "account": None,
                "folders": [],
                "error": "Аккаунт не найден"
            })

        logger.info(f"Начинаем импорт папок для аккаунта {account.phone}")

        # Получаем папки из Telegram через API
        folders_data = await services.get_folders_from_telegram(account)
        
        folders_created = 0
        folders_updated = 0
        
        if folders_data:
            logger.info(f"Получено {len(folders_data)} папок из Telegram")
            
            # Создаем папки на основе данных из Telegram
            for folder_title, chat_ids in folders_data.items():
                if not chat_ids:  # Пропускаем пустые папки
                    logger.info(f"Пропускаем пустую папку '{folder_title}'")
                    continue
                
                # Проверяем, не существует ли уже папка с таким названием
                existing_folder = session.exec(
                    select(Folder).where(
                        Folder.account_id == account_id,
                        Folder.title == folder_title
                    )
                ).first()
                
                if existing_folder:
                    # Обновляем существующую папку
                    existing_folder.chats_json = json.dumps(chat_ids)
                    session.add(existing_folder)
                    folders_updated += 1
                    logger.info(f"Обновлена папка '{folder_title}' с {len(chat_ids)} чатами")
                else:
                    # Создаем новую папку
                    folder = Folder(
                        account_id=account_id,
                        title=folder_title,
                        chats_json=json.dumps(chat_ids)
                    )
                    session.add(folder)
                    folders_created += 1
                    logger.info(f"Создана папка '{folder_title}' с {len(chat_ids)} чатами")
            
            if folders_created > 0 or folders_updated > 0:
                session.commit()
                success_msg = []
                if folders_created > 0:
                    success_msg.append(f"создано {folders_created}")
                if folders_updated > 0:
                    success_msg.append(f"обновлено {folders_updated}")
                
                return templates.TemplateResponse("folders.html", {
                    "request": request,
                    "account": account,
                    "folders": session.exec(select(Folder).where(Folder.account_id == account_id)).all(),
                    "success": f"Импорт папок завершен: {' и '.join(success_msg)} папок!"
                })
            else:
                return templates.TemplateResponse("folders.html", {
                    "request": request,
                    "account": account,
                    "folders": session.exec(select(Folder).where(Folder.account_id == account_id)).all(),
                    "info": "Все папки в Telegram пустые или уже существуют с актуальными данными"
                })
        else:
            logger.warning(f"Не найдено пользовательских папок в Telegram для аккаунта {account.phone}")
            return templates.TemplateResponse("folders.html", {
                "request": request,
                "account": account,
                "folders": session.exec(select(Folder).where(Folder.account_id == account_id)).all(),
                "info": "В Telegram не найдено пользовательских папок (folders). Создайте папки с чатами в мобильном приложении Telegram: Настройки → Папки → Создать папку, добавьте туда нужные чаты, затем повторите импорт."
            })
            
    except Exception as e:
        logger.error(f"Ошибка импорта папок: {e}")
        account = session.get(Account, account_id)
        return templates.TemplateResponse("folders.html", {
            "request": request,
            "account": account,
            "folders": session.exec(select(Folder).where(Folder.account_id == account_id)).all(),
            "error": f"Ошибка импорта папок: {str(e)}"
        })

@router.post("/folders/{folder_id}/delete")
def delete_folder(
    request: Request,
    folder_id: int,
    session: Session = Depends(get_session),
    auth: bool = Depends(check_auth)
):
    try:
        folder = session.get(Folder, folder_id)
        if not folder:
            raise HTTPException(404, "Папка не найдена")

        account_id = folder.account_id
        title = folder.title

        # Проверяем, используется ли папка в активных задачах
        active_jobs = session.exec(select(Job).where(Job.folder_id == folder_id, Job.is_running == True)).all()
        if active_jobs:
            account = session.get(Account, account_id)
            return templates.TemplateResponse("folders.html", {
                "request": request,
                "account": account,
                "folders": session.exec(select(Folder).where(Folder.account_id == account_id)).all(),
                "error": f"Нельзя удалить папку '{title}' - она используется в активных задачах!"
            })

        session.delete(folder)
        session.commit()

        account = session.get(Account, account_id)
        return templates.TemplateResponse("folders.html", {
            "request": request,
            "account": account,
            "folders": session.exec(select(Folder).where(Folder.account_id == account_id)).all(),
            "success": f"Папка '{title}' удалена!"
        })

    except Exception as e:
        # В случае ошибки все равно показываем страницу
        return templates.TemplateResponse("folders.html", {
            "request": request,
            "account": None,
            "folders": [],
            "error": f"Ошибка удаления: {str(e)}"
        })

# =============================
# СООБЩЕНИЯ
# =============================
@router.get("/messages", response_class=HTMLResponse)
def list_messages(request: Request, session: Session = Depends(get_session), auth: bool = Depends(check_auth)):
    messages = session.exec(select(MessageTemplate)).all()
    return templates.TemplateResponse("messages.html", {"request": request, "messages": messages})

@router.post("/messages/{template_id}/delete")
def delete_message_template(
    template_id: int,
    session: Session = Depends(get_session),
    auth: bool = Depends(check_auth)
):
    template = session.get(MessageTemplate, template_id)
    if not template:
        raise HTTPException(404, "Шаблон не найден")
    
    # Сначала удаляем все связанные задания, так как template_id в Job не может быть NULL
    jobs = session.exec(select(Job).where(Job.template_id == template_id)).all()
    for job in jobs:
        session.delete(job)
    
    session.delete(template)
    session.commit()
    return RedirectResponse("/messages", status_code=303)

@router.post("/messages/add")
async def add_message(
    title: str = Form(...),
    variants_json: str = Form(...),
    media_path: str = Form(None),
    message_link: str = Form(None),
    media_file: Optional[UploadFile] = File(None),
    session: Session = Depends(get_session),
    auth: bool = Depends(check_auth)
):
    final_media_path = media_path

    # Если загружен файл, сохраняем его
    if media_file and media_file.filename:
        os.makedirs("app/media", exist_ok=True)
        file_path = f"app/media/{media_file.filename}"
        with open(file_path, "wb") as f:
            content = await media_file.read()
            f.write(content)
        final_media_path = file_path

    tmpl = MessageTemplate(
        title=title, 
        variants_json=variants_json, 
        media_path=final_media_path,
        message_link=message_link
    )
    session.add(tmpl)
    session.commit()
    return RedirectResponse("/messages", status_code=303)

@router.post("/messages/{message_id}/delete")
def delete_message(
    message_id: int,
    session: Session = Depends(get_session),
    auth: bool = Depends(check_auth)
):
    message = session.get(MessageTemplate, message_id)
    if not message:
        raise HTTPException(404, "Шаблон не найден")
    
    # Проверяем, используется ли шаблон в активных задачах
    active_jobs = session.exec(select(Job).where(Job.template_id == message_id, Job.is_running == True)).all()
    if active_jobs:
        return RedirectResponse("/messages", status_code=303)
    
    # Удаляем медиафайл если есть
    if message.media_path and os.path.exists(message.media_path):
        try:
            os.remove(message.media_path)
        except:
            pass
    
    session.delete(message)
    session.commit()
    return RedirectResponse("/messages", status_code=303)

# =============================
# JOBS (создание / запуск / стоп)
# =============================
@router.get("/launch", response_class=HTMLResponse)
def list_jobs(request: Request, session: Session = Depends(get_session), auth: bool = Depends(check_auth)):
    jobs = session.exec(select(Job)).all()
    accounts = session.exec(select(Account)).all()
    folders = session.exec(select(Folder)).all()
    templates_list = session.exec(select(MessageTemplate)).all()
    return templates.TemplateResponse(
        "launch.html",
        {
            "request": request,
            "jobs": jobs,
            "accounts": accounts,
            "folders": folders,
            "templates_list": templates_list,
        },
    )

@router.post("/launch/create")
def create_job(
    account_id: int = Form(...),
    folder_id: int = Form(...),
    template_id: int = Form(...),
    min_interval: int = Form(...),
    max_interval: int = Form(...),
    daily_limit: int = Form(...),
    active_from: str = Form(...),
    active_to: str = Form(...),
    session: Session = Depends(get_session),
    auth: bool = Depends(check_auth)
):
    af = _parse_hhmm(active_from)
    at = _parse_hhmm(active_to)

    job = Job(
        account_id=account_id,
        folder_id=folder_id,
        template_id=template_id,
        min_interval=min_interval,
        max_interval=max_interval,
        daily_limit=daily_limit,
        active_from=af,
        active_to=at,
        is_running=False,
    )
    session.add(job)
    session.commit()
    return RedirectResponse("/launch", status_code=303)

@router.get("/launch/start/{job_id}")
@router.post("/launch/start/{job_id}")
async def start_job(job_id: int, session: Session = Depends(get_session), auth: bool = Depends(check_auth)):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.is_running:
        return RedirectResponse("/launch", status_code=303)

    # Запуск задачи через services
    await services.start_job(job_id)

    job.is_running = True
    session.add(job)
    session.commit()
    return RedirectResponse("/launch", status_code=303)

@router.post("/launch/stop/{job_id}")
async def stop_job(job_id: int, session: Session = Depends(get_session), auth: bool = Depends(check_auth)):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.is_running:
        return RedirectResponse("/launch", status_code=303)

    # Остановка задачи через services
    await services.stop_job(job_id)

    job.is_running = False
    session.add(job)
    session.commit()
    return RedirectResponse("/launch", status_code=303)

@router.post("/launch/delete/{job_id}")
def delete_job(job_id: int, session: Session = Depends(get_session), auth: bool = Depends(check_auth)):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    
    # Останавливаем задачу если она запущена
    if job.is_running:
        import asyncio
        try:
            asyncio.create_task(services.stop_job(job_id))
        except:
            pass
    
    session.delete(job)
    session.commit()
    return RedirectResponse("/launch", status_code=303)

@router.post("/launch/update/{job_id}")
def update_job(
    job_id: int,
    active_from: str = Form(...),
    active_to: str = Form(...),
    min_interval: int = Form(...),
    max_interval: int = Form(...),
    daily_limit: int = Form(...),
    session: Session = Depends(get_session),
    auth: bool = Depends(check_auth)
):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    
    job.active_from = _parse_hhmm(active_from)
    job.active_to = _parse_hhmm(active_to)
    job.min_interval = min_interval
    job.max_interval = max_interval
    job.daily_limit = daily_limit
    
    session.add(job)
    session.commit()
    return RedirectResponse("/launch", status_code=303)

@router.post("/accounts/{account_id}/update_proxy")
async def update_account_proxy(
    account_id: int,
    proxy_url: str = Form(None),
    session: Session = Depends(get_session),
    auth: bool = Depends(check_auth)
):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")
    
    account.proxy_url = proxy_url.strip() if proxy_url else None
    session.add(account)
    session.commit()
    
    return RedirectResponse("/accounts", status_code=303)

@router.post("/accounts/{account_id}/delete")
def delete_account(
    request: Request,
    account_id: int,
    session: Session = Depends(get_session),
    auth: bool = Depends(check_auth)
):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")
    
    # Проверяем, есть ли активные задачи
    active_jobs = session.exec(select(Job).where(Job.account_id == account_id, Job.is_running == True)).all()
    if active_jobs:
        return templates.TemplateResponse("accounts.html", {
            "request": request,
            "accounts": session.exec(select(Account)).all(),
            "error": f"Нельзя удалить аккаунт '{account.title}' - у него есть активные рассылки!"
        })
    
    # Удаляем все связанные данные
    # Сначала удаляем задачи
    jobs = session.exec(select(Job).where(Job.account_id == account_id)).all()
    for job in jobs:
        session.delete(job)
    
    # Удаляем папки
    folders = session.exec(select(Folder).where(Folder.account_id == account_id)).all()
    for folder in folders:
        session.delete(folder)
    
    # Удаляем логи
    logs = session.exec(select(Log).where(Log.account_id == account_id)).all()
    for log in logs:
        session.delete(log)
    
    # Удаляем аккаунт
    session.delete(account)
    session.commit()
    
    return RedirectResponse("/accounts", status_code=303)

# =============================
# ЛОГИ
# =============================
@router.get("/logs", response_class=HTMLResponse)
def list_logs(request: Request, session: Session = Depends(get_session), auth: bool = Depends(check_auth)):
    logs = session.exec(select(Log).order_by(Log.created_at.desc()).limit(100)).all()
    return templates.TemplateResponse("logs.html", {"request": request, "logs": logs})

@router.get("/logs/export.csv")
def export_logs_csv(session: Session = Depends(get_session), auth: bool = Depends(check_auth)):
    rows = session.exec(select(Log).order_by(Log.created_at.desc())).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ID", "Account ID", "Chat ID", "Chat Title", "Message", "Status", "Error"])
    for r in rows:
        writer.writerow([r.id, r.account_id, r.chat_id or "", r.chat_title or "", r.message or "", r.status, r.error_reason or ""])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.read()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=logs.csv"},
    )

@router.get("/logs/export.json")
def export_logs_json(session: Session = Depends(get_session), auth: bool = Depends(check_auth)):
    rows = session.exec(select(Log).order_by(Log.created_at.desc())).all()
    payload = [
        {
            "created_at": str(r.created_at),
            "account_id": r.account_id,
            "chat_id": r.chat_id,
            "chat_title": r.chat_title,
            "message": r.message,
            "status": r.status,
            "error_reason": r.error_reason,
        }
        for r in rows
    ]
    return payload

# =============================
# API ENDPOINTS
# =============================

@router.get("/api/folders/{account_id}")
def get_folders_api(account_id: int, session: Session = Depends(get_session), auth: bool = Depends(check_auth)):
    """API endpoint для получения папок аккаунта"""
    try:
        folders = session.exec(select(Folder).where(Folder.account_id == account_id)).all()
        folder_data = []
        for folder in folders:
            try:
                chats = json.loads(folder.chats_json) if folder.chats_json else []
                chat_count = len(chats)
            except:
                chat_count = 0
            
            folder_data.append({
                "id": folder.id,
                "title": folder.title,
                "chat_count": chat_count,
                "chats": chats
            })
        
        return {"folders": folder_data}
    except Exception as e:
        logger.error(f"Ошибка получения папок для API: {e}")
        return {"error": str(e), "folders": []}