from sqlmodel import Session, select
from app.db import engine, User, init_db
import hashlib

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def create_default_admin():
    """Создает администратора по умолчанию если его нет"""
    init_db()

    with Session(engine) as session:
        # Проверяем есть ли уже админ
        existing_admin = session.exec(select(User).where(User.username == "admin")).first()

        if existing_admin:
            # Обновляем пароль существующего админа на правильный
            existing_admin.password_hash = hash_password("admin123")
            session.add(existing_admin)
            session.commit()
            print("✅ Пароль администратора обновлен на admin123")
        else:
            # Создаем нового админа с паролем admin123
            admin = User(
                username="admin",
                password_hash=hash_password("admin123"),
                is_active=True
            )
            session.add(admin)
            session.commit()
            print("✅ Создан администратор с паролем admin123")

if __name__ == "__main__":
    create_default_admin()