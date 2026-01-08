# Telegram Poster

## Overview

Telegram Poster is a web-based automation tool for managing and sending bulk messages across Telegram accounts. The application allows users to connect multiple Telegram accounts, organize chats into folders, create message templates with variants for anti-spam purposes, and schedule automated message distribution with configurable intervals and daily limits.

The system is built as a FastAPI backend with Jinja2 templates for server-side rendered HTML pages, using SQLite for data persistence and the Telethon library for Telegram API integration.

## User Preferences

Preferred communication style: Simple, everyday language.

## System Architecture

### Backend Framework
- **FastAPI** serves as the main web framework, providing both REST API endpoints and HTML page rendering
- **Jinja2 templates** are used for server-side HTML rendering (located in `/templates` directory)
- **Uvicorn** runs as the ASGI server on port 5000

### Database Layer
- **SQLite** is the primary database (`app.db` file)
- **SQLModel** (built on SQLAlchemy + Pydantic) provides ORM capabilities
- Key models: `User`, `Account`, `Folder`, `MessageTemplate`, `Job`, `Log`
- Database session management uses generator-based dependency injection via `get_session()`

### Telegram Integration
- **Telethon** library handles all Telegram API communication
- Session strings (StringSession) are used for persistent authentication
- Sessions are encrypted using Fernet symmetric encryption before storage
- Support for SOCKS5/HTTP proxy configuration per account
- Multi-step login flow: phone → verification code → optional 2FA password

### Authentication
- Simple password-based admin authentication using SHA-256 hashing
- Default admin credentials: username `admin`, password `admin123`
- Session/cookie-based authentication for web interface

### Task Scheduling
- **APScheduler** (AsyncIOScheduler) manages scheduled message sending jobs
- Jobs support configurable parameters:
  - Random interval ranges (e.g., 20-60 seconds between messages)
  - Daily message limits per account
  - Active time windows (e.g., 09:00-22:00)

### Application Structure
```
/app
  ├── core.py       # Settings, encryption utilities
  ├── db.py         # Database models and engine
  ├── routers.py    # FastAPI routes (web pages + API)
  ├── services.py   # Business logic, Telegram client management
  ├── schemas.py    # Pydantic schemas for API
  └── init_admin.py # Admin user initialization

/templates          # Jinja2 HTML templates
/main.py            # Application entry point
```

### Key Design Decisions

1. **StringSession Storage**: Telegram sessions are stored as encrypted strings in the database rather than file-based sessions, enabling easier account management and portability.

2. **Folder-based Chat Organization**: The system imports Telegram's native Dialog Filters (folders) and automatically categorizes chats by write permissions.

3. **Message Variants**: Templates support multiple message variants stored as JSON arrays, with random selection for anti-spam measures.

4. **Generator-based DB Sessions**: Uses `yield` pattern for database sessions to ensure proper cleanup, though requires careful handling with `Session(engine)` context manager directly in async contexts.

## External Dependencies

### Python Packages
- **telethon**: Telegram MTProto API client library
- **fastapi**: Web framework
- **uvicorn**: ASGI server
- **sqlmodel**: ORM combining SQLAlchemy and Pydantic
- **apscheduler**: Task scheduling
- **cryptography**: Fernet encryption for session storage
- **pydantic-settings**: Configuration management
- **jinja2**: Template engine

### External Services
- **Telegram API**: Requires `api_id` and `api_hash` from my.telegram.org
- Optional: SOCKS5/HTTP proxy servers for Telegram connections

### Storage
- SQLite database file (`app.db`) for all persistent data
- Media files stored locally with paths referenced in database