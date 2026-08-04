"""
Автоприменение схемы БД при старте приложения.

Отслеживает применённые миграции в таблице schema_migrations (по имени файла,
migrations/NNN_*.sql). При старте применяет по порядку все ещё не применённые
файлы — а не только 001_initial_schema.sql, как было раньше (миграции 002-005
приходилось накатывать пользователю вручную, без какого-либо отслеживания,
какие из них уже применены на конкретной базе).

Бэкфилл при переходе со старой логики: если таблица managers уже существует,
а schema_migrations пуста — 001_initial_schema.sql уже была применена раньше
(руками или старым кодом). Помечаем её применённой БЕЗ повторного запуска —
она не идемпотентна (CREATE TABLE без IF NOT EXISTS, INSERT без ON CONFLICT)
и упадёт при повторном исполнении. Миграции 002+ идемпотентны (IF NOT EXISTS /
ADD COLUMN IF NOT EXISTS), поэтому даже если пользователь уже накатывал их
вручную раньше — повторное применение безопасно, и с этого момента они тоже
попадают под отслеживание.
"""
import os
import logging
from sqlalchemy import text
from backend.core.database import engine, DB_SCHEMA

logger = logging.getLogger("torgstore.migrate")

MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "migrations",
)


async def _ensure_schema_migrations_table(conn):
    await conn.execute(text(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  filename TEXT PRIMARY KEY,"
        "  applied_at TIMESTAMPTZ DEFAULT now()"
        ")"
    ))


async def _applied_filenames(conn) -> set:
    result = await conn.execute(text("SELECT filename FROM schema_migrations"))
    return {row[0] for row in result.fetchall()}


async def _record_applied(conn, filename: str):
    await conn.execute(
        text("INSERT INTO schema_migrations (filename) VALUES (:f) ON CONFLICT (filename) DO NOTHING"),
        {"f": filename},
    )


async def _apply_sql_file(conn, path: str):
    with open(path, "r", encoding="utf-8") as f:
        sql = f.read()
    if DB_SCHEMA != "public":
        sql = f'SET search_path TO "{DB_SCHEMA}", public;\n' + sql
    # asyncpg не умеет выполнять несколько statement'ов через execute(text(...))
    # с параметрами, но обычный execution через raw connection умеет multi-statement.
    raw_conn = await conn.get_raw_connection()
    await raw_conn.driver_connection.execute(sql)


async def run_startup_migration():
    async with engine.begin() as conn:
        if DB_SCHEMA != "public":
            logger.info("Ensuring schema '%s' exists", DB_SCHEMA)
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{DB_SCHEMA}"'))

        await _ensure_schema_migrations_table(conn)
        applied = await _applied_filenames(conn)

        if "001_initial_schema.sql" not in applied:
            managers_exists = (await conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables "
                    "  WHERE table_schema = :schema AND table_name = 'managers'"
                    ")"
                ),
                {"schema": DB_SCHEMA},
            )).scalar()
            if managers_exists:
                logger.info(
                    "Таблица managers уже существует — считаю 001_initial_schema.sql "
                    "применённой без повторного запуска (бэкфилл при переходе на schema_migrations)"
                )
                await _record_applied(conn, "001_initial_schema.sql")
                applied.add("001_initial_schema.sql")

    if not os.path.isdir(MIGRATIONS_DIR):
        logger.warning("Каталог миграций не найден: %s — пропускаю", MIGRATIONS_DIR)
        return

    sql_files = sorted(f for f in os.listdir(MIGRATIONS_DIR) if f.endswith(".sql"))
    pending = [f for f in sql_files if f not in applied]

    if not pending:
        logger.info("Схема БД актуальна — %d миграций уже применено, применять нечего", len(applied))
        return

    for filename in pending:
        path = os.path.join(MIGRATIONS_DIR, filename)
        logger.info("Применяю миграцию %s ...", filename)
        async with engine.begin() as conn:
            await _apply_sql_file(conn, path)
            await _record_applied(conn, filename)
        logger.info("Миграция %s применена", filename)
