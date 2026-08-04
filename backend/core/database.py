"""
Database connection — asyncpg + SQLAlchemy async
Managed PostgreSQL (Render) как хранилище
"""
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
import os

# Раньше здесь был fallback на "postgresql+asyncpg://postgres:password@localhost:5432/torgstore",
# если DATABASE_URL не задан — рабочий дефолт для локальной разработки, но опасная
# практика: хардкод креда в исходниках, который тихо подключит приложение не туда,
# куда думаешь, если переменная окружения потеряется при деплое. Теперь без
# DATABASE_URL приложение явно падает при импорте с понятной ошибкой вместо
# молчаливого подключения к дефолтной базе.
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL не задан. Укажи его в .env (или в environment: docker-compose.yml) — "
        "например: postgresql+asyncpg://postgres:password@localhost:5432/torgstore для локальной разработки."
    )

# Render (и большинство managed-провайдеров) отдают connection string в виде
# postgres:// или postgresql:// — SQLAlchemy + asyncpg требуют явного диалекта
# postgresql+asyncpg://. Нормализуем автоматически, чтобы не редактировать
# .env руками при каждом деплое.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+asyncpg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# Схема Postgres для изоляции TorgStore внутри общего инстанса БД
# (актуально, если DATABASE_URL указывает на shared-инстанс с другими проектами —
# например, тот же Render Postgres, что используется под Kaspi).
# По умолчанию 'public' — если TorgStore живёт в собственной выделенной базе,
# ничего менять не нужно.
DB_SCHEMA = os.environ.get("DB_SCHEMA", "public")

_connect_args = {}
if DB_SCHEMA != "public":
    _connect_args = {"server_settings": {"search_path": f"{DB_SCHEMA},public"}}

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=20,
    connect_args=_connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

class Base(DeclarativeBase):
    pass

async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
