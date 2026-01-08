from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

from app.routers import router
from app.db import init_db

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Инициализация при старте
    print("🚀 Инициализация базы данных...")
    init_db()

    # Создаем админа по умолчанию
    from app.init_admin import create_default_admin
    create_default_admin()

    # Запуск планировщика
    from app.services import scheduler
    if not scheduler.running:
        scheduler.start()

    print("✅ База данных готова!")
    yield
    if scheduler.running:
        scheduler.shutdown()
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

if __name__ == "__main__":
    import uvicorn
    # Убираем строку запуска через файл и используем стандартный строковый импорт
    # Это помогает uvicorn правильно инициализироваться в некоторых средах
    # Используем прокси-заголовки для правильной работы за обратным прокси
    uvicorn.run("main:app", host="0.0.0.0", port=5000, log_level="info", proxy_headers=True, forwarded_allow_ips="*", timeout_keep_alive=65)
