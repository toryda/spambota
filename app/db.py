from sqlmodel import SQLModel, Field, create_engine, Session, Relationship
from typing import Optional, List
from datetime import datetime, time
import os

# URL базы данных - принудительно используем SQLite
DATABASE_URL = "sqlite:///./app.db"
engine = create_engine(DATABASE_URL, echo=False)


def get_session():
    """Получает сессию базы данных"""
    with Session(engine) as session:
        yield session


def init_db():
    """Инициализация базы данных"""
    SQLModel.metadata.create_all(engine)


# =====================================================
# МОДЕЛИ БАЗЫ ДАННЫХ
# =====================================================

class User(SQLModel, table=True):
    """Пользователь системы"""
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True)
    password_hash: str
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)

class Account(SQLModel, table=True):
    """Telegram аккаунт"""
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str                           # название аккаунта (для админа)
    phone: str                           # номер телефона аккаунта
    api_id: int                          # API ID из my.telegram.org
    api_hash: str                        # API Hash из my.telegram.org
    session_string: str                  # строка сессии (будет зашифрована)
    proxy_url: Optional[str] = None      # прокси (если нужен)
    is_active: bool = False              # статус подключения
    created_at: datetime = Field(default_factory=datetime.utcnow)

    jobs: List["Job"] = Relationship(back_populates="account")
    logs: List["Log"] = Relationship(back_populates="account")


class Folder(SQLModel, table=True):
    """Папка с чатами для рассылки"""
    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="account.id")
    title: str                           # название папки
    chats_json: str                      # JSON-массив с ID чатов
    created_at: datetime = Field(default_factory=datetime.utcnow)

    jobs: List["Job"] = Relationship(back_populates="folder")


class MessageTemplate(SQLModel, table=True):
    """Шаблон сообщений (несколько вариантов + медиа)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    variants_json: str                   # JSON-массив с вариантами сообщений
    media_path: Optional[str] = None     # путь к файлу (если есть медиа)
    message_link: Optional[str] = None   # ссылка на сообщение-донор (для Premium эмодзи)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    jobs: List["Job"] = Relationship(back_populates="template")


class Job(SQLModel, table=True):
    """Задача рассылки"""
    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="account.id")
    folder_id: int = Field(foreign_key="folder.id")
    template_id: int = Field(foreign_key="messagetemplate.id")

    # Настройки задержек и лимитов (админ задаёт сам!)
    min_interval: int = 20               # минимальная пауза (сек)
    max_interval: int = 60               # максимальная пауза (сек)
    daily_limit: int = 100               # сообщений в сутки
    active_from: time = time(9, 0, 0)    # начало активности
    active_to: time = time(22, 0, 0)     # конец активности

    is_running: bool = False             # активна ли задача
    created_at: datetime = Field(default_factory=datetime.utcnow)

    account: Account = Relationship(back_populates="jobs")
    template: MessageTemplate = Relationship(back_populates="jobs")
    folder: Folder = Relationship(back_populates="jobs")


class Log(SQLModel, table=True):
    """Логи рассылки"""
    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="account.id")
    chat_id: Optional[int] = None
    chat_title: Optional[str] = None
    message: Optional[str] = None
    status: str                          # "OK" или "ERROR"
    error_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    account: Account = Relationship(back_populates="logs")