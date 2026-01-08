from fastapi import FastAPI, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic
from contextlib import asynccontextmanager

from app.routers import router
from app.db import init_db

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Инициализация при старте
    print("🚀 Инициализация базы данных...")
    init_db()
    print("✅ База данных готова!")
    yield
    print("🛑 Остановка приложения")

app = FastAPI(
    title="Telegram Poster",
    description="Автоматическая рассылка сообщений в Telegram",
    version="1.0.0",
    lifespan=lifespan
)

# Подключаем статичные файлы
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Подключаем роуты  
app.include_router(router)

@app.get("/health")
def health_check():
    return {"status": "ok"}