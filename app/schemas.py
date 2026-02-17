from datetime import datetime, time
from typing import Optional, List
from pydantic import BaseModel, Field, validator


# ==========================
# COMMON
# ==========================
class OkResp(BaseModel):
    ok: bool = True


# ==========================
# ACCOUNTS
# ==========================
class AccountBase(BaseModel):
    title: str = Field(..., description="Метка аккаунта")
    api_id: int
    api_hash: str
    proxy_url: Optional[str] = Field(
        None, description="socks5://user:pass@host:port или http://user:pass@host:port"
    )
    is_active: bool = True


class AccountCreate(AccountBase):
    session_str: str = Field(..., description="StringSession для Telethon")


class AccountRead(BaseModel):
    id: int
    title: str
    api_id: int
    proxy_url: Optional[str] = None
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True  # pydantic <- sqlmodel


# ==========================
# FOLDERS (Dialog Filters)
# ==========================
class FolderRead(BaseModel):
    id: int
    account_id: int
    title: str
    chats: List[int] = Field(default_factory=list)
    created_at: datetime

    class Config:
        from_attributes = True

    @classmethod
    def from_db(cls, folder) -> "FolderRead":
        import json
        chats = []
        try:
            chats = json.loads(folder.chats_json or "[]")
        except Exception:
            chats = []
        return cls(
            id=folder.id,
            account_id=folder.account_id,
            title=folder.title,
            chats=chats,
            created_at=folder.created_at,
        )


# ==========================
# MESSAGE TEMPLATES
# ==========================
class MessageTemplateBase(BaseModel):
    title: str
    variants: List[str] = Field(..., description="Список вариантов сообщений")
    message_link: Optional[str] = Field(None, description="Ссылка на пост для премиум эмодзи")
    media_path: Optional[str] = Field(None, description="Путь к файлу (опционально)")

    @validator("variants")
    def _not_empty(cls, v):
        if not v:
            raise ValueError("Список вариантов сообщений не может быть пустым")
        return v


class MessageTemplateCreate(MessageTemplateBase):
    pass


class MessageTemplateRead(BaseModel):
    id: int
    title: str
    variants: List[str]
    media_path: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True

    @classmethod
    def from_db(cls, tmpl) -> "MessageTemplateRead":
        import json
        variants = []
        try:
            variants = json.loads(tmpl.variants_json or "[]")
        except Exception:
            variants = []
        return cls(
            id=tmpl.id,
            title=tmpl.title,
            variants=variants,
            message_link=getattr(tmpl, 'message_link', None),
            media_path=tmpl.media_path,
            created_at=tmpl.created_at,
        )


# ==========================
# JOBS (campaigns)
# ==========================
class JobBase(BaseModel):
    account_id: int
    folder_id: int
    template_id: int
    min_interval: int = Field(20, ge=1)
    max_interval: int = Field(60, ge=1)
    daily_limit: int = Field(100, ge=1)
    active_from: time = time(9, 0, 0)
    active_to: time = time(22, 0, 0)

    @validator("max_interval")
    def _check_intervals(cls, v, values):
        mi = values.get("min_interval", 1)
        if v < mi:
            raise ValueError("max_interval не может быть меньше min_interval")
        return v


class JobCreate(JobBase):
    pass


class JobRead(BaseModel):
    id: int
    account_id: int
    folder_id: int
    template_id: int
    min_interval: int
    max_interval: int
    daily_limit: int
    active_from: time
    active_to: time
    is_running: bool
    created_at: datetime

    class Config:
        from_attributes = True


# ==========================
# LOGS
# ==========================
class LogRead(BaseModel):
    id: int
    account_id: int
    chat_id: Optional[int]
    chat_title: Optional[str]
    message: Optional[str]
    status: str  # "OK" / "ERROR"
    error_reason: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True
