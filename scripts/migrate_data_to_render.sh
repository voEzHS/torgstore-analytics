#!/usr/bin/env bash
# Перенос данных из локальной docker-базы (схема public) в Render kaspi-db
# (схема torgstore). Ничего не удаляет и не трогает схему public самой
# kaspi-db (данные Kaspi не затрагиваются) — работает только со схемой
# torgstore, которая уже создана миграциями сайта.
#
# Использование:
#   RENDER_EXTERNAL_DB_URL="postgresql://kaspi_analytics_user:...@dpg-....render.com/kaspi_analytics" \
#     ./migrate_data_to_render.sh
#
# Где взять RENDER_EXTERNAL_DB_URL:
#   Render Dashboard → kaspi-db → кнопка "Connect" (справа сверху) →
#   вкладка "External" → "External Database URL" → иконка копирования.
#
# Скрипт запускает pg_dump/psql ВНУТРИ уже работающего локального контейнера
# db (не требует psql/pg_dump на самом Mac) и сам переносит данные из схемы
# public (локально) в схему torgstore (на Render) — без хардкода списка
# таблиц, всё, что есть в public, кроме служебной schema_migrations.

set -euo pipefail

: "${RENDER_EXTERNAL_DB_URL:?Нужно передать RENDER_EXTERNAL_DB_URL (см. комментарий в начале файла)}"

cd "$(dirname "$0")/.." 2>/dev/null || true

CONTAINER=$(docker compose ps -q db 2>/dev/null || true)
if [ -z "$CONTAINER" ]; then
  echo "❌ Локальный контейнер db не найден/не запущен. Запусти сначала: docker compose up -d db"
  exit 1
fi
echo "✓ Локальный контейнер: $CONTAINER"

# Список таблиц берём из локальной базы динамически — не хардкодим.
TABLES=$(docker exec "$CONTAINER" psql -U postgres -d torgstore -t -A -c \
  "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'schema_migrations' ORDER BY tablename;")

count_remote_table () {
  docker exec -i "$CONTAINER" psql "$RENDER_EXTERNAL_DB_URL" -t -A -c \
    "SELECT count(*) FROM torgstore.\"$1\";" 2>/dev/null | tr -d '[:space:]'
}
count_local_table () {
  docker exec "$CONTAINER" psql -U postgres -d torgstore -t -A -c \
    "SELECT count(*) FROM public.\"$1\";" | tr -d '[:space:]'
}

echo
echo "→ Проверяю, что схема torgstore на Render сейчас пустая (защита от повторного запуска)..."
REMOTE_TOTAL=0
for t in $TABLES; do
  c=$(count_remote_table "$t" || echo 0)
  REMOTE_TOTAL=$((REMOTE_TOTAL + ${c:-0}))
done
if [ "$REMOTE_TOTAL" != "0" ] && [ -z "${FORCE:-}" ]; then
  echo "❌ В схеме torgstore на Render уже есть строки (всего $REMOTE_TOTAL). Похоже, перенос уже делался."
  echo "   Если точно нужно перезалить поверх — запусти с FORCE=1."
  exit 1
fi
echo "✓ Схема torgstore пустая, можно переносить."

DUMP_FILE="/tmp/torgstore_data_$(date +%s).sql"

echo
echo "→ 1/3 Дамп данных из локальной базы (схема public, только данные, без schema_migrations)..."
docker exec "$CONTAINER" pg_dump \
  -U postgres -d torgstore \
  --data-only --no-owner \
  --exclude-table=public.schema_migrations \
  -n public \
  > "$DUMP_FILE"

echo "→ 2/3 Перенаправляю целевую схему public → torgstore (только структурные строки, не данные)..."
sed -i.bak \
  -e '/^COPY public\./ s/^COPY public\./COPY torgstore./' \
  -e "/^SELECT pg_catalog\.setval/ s/'public\./'torgstore./" \
  -e '/^ALTER TABLE public\./ s/^ALTER TABLE public\./ALTER TABLE torgstore./' \
  "$DUMP_FILE"
rm -f "${DUMP_FILE}.bak"

N_TABLES=$(grep -c '^COPY torgstore\.' "$DUMP_FILE" || true)
echo "  Таблиц с данными для переноса: $N_TABLES"

echo
echo "→ 3/3 Заливаю в Render (kaspi-db, схема torgstore), одной транзакцией..."
{
  echo "SET CONSTRAINTS ALL DEFERRED;"
  cat "$DUMP_FILE"
} | docker exec -i "$CONTAINER" psql "$RENDER_EXTERNAL_DB_URL" -v ON_ERROR_STOP=1 --single-transaction

echo
echo "→ Проверка: строк локально (public) vs на Render (torgstore)"
MISMATCH=0
for t in $TABLES; do
  l=$(count_local_table "$t")
  r=$(count_remote_table "$t" || echo "?")
  mark="✓"
  if [ "$l" != "$r" ]; then mark="⚠️"; MISMATCH=1; fi
  printf "  %s %-28s локально=%-8s render=%-8s\n" "$mark" "$t" "$l" "$r"
done

echo
if [ "$MISMATCH" = "1" ]; then
  echo "⚠️  Есть расхождения по строкам — посмотри таблицы с ⚠️ выше."
else
  echo "✅ Все таблицы совпадают. Перенос успешен."
fi
echo "   Дамп сохранён тут (можно удалить): $DUMP_FILE"
echo "   Проверь сайт: https://torgstore-api.onrender.com"
