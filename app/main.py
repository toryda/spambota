from fastapi import FastAPI, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic
from contextlib import asynccontextmanager

from app.routers import router
from app.db import init_db

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Инициализация при старте
    from app.db import init_db
    from app.services import scheduler, start_job, engine
    from app.db import Job
    from sqlmodel import Session, select
    import logging

    logger = logging.getLogger(__name__)

    logger.info("🚀 Инициализация базы данных...")
    init_db()
    logger.info("✅ База данных готова!")
    
    # Запуск планировщика
    if not scheduler.running:
        scheduler.start()
        logger.info("⏰ Планировщик запущен")
    
    # Перезапуск активных задач
    try:
        with Session(engine) as session:
            active_jobs = session.exec(select(Job).where(Job.is_running == True)).all()
            for job in active_jobs:
                logger.info(f"🔄 Перезапуск активной задачи {job.id}...")
                await start_job(job.id)
    except Exception as e:
        logger.error(f"❌ Ошибка перезапуска задач: {e}")
        
    yield
    
    from app.services import cleanup_clients
    if scheduler.running:
        scheduler.shutdown()
    cleanup_clients()
    logger.info("🛑 Остановка приложения")

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