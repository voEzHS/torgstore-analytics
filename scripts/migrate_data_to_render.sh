#!/usr/bin/env bash
# Перенос данных из локальной docker-базы (схема public) в Render kaspi-db
# (схема torgstore). Ничего не удаляет и не трогает схему public самой
# kaspi-db (данные Kaspi не затрагиваются) — работает только со схемой
# torgstore, которая уже создана миграциями сайта.
#
# Миграции сами сеют несколько reference-строк (managers: «Весь отдел»,
# settings, source_groups) — они уже есть и локально, и на Render. Для этих
# трёх таблиц используется INSERT ... ON CONFLICT DO NOTHING (совпадающие
# строки просто пропускаются, новые — вставляются). Все остальные таблицы
# переносятся быстрым bulk COPY.
#
# Использование:
#   RENDER_EXTERNAL_DB_URL="postgresql://kaspi_analytics_user:...@dpg-....render.com/kaspi_analytics" \
#     ./migrate_data_to_render.sh
#
# Где взять RENDER_EXTERNAL_DB_URL:
#   Render Dashboard → kaspi-db → кнопка "Connect" → вкладка "External" →
#   "External Database URL" → иконка копирования.
#
# ⚠️ Переменная окружения живёт только в рамках ОДНОЙ команды. Если открыл
# новый терминал/новую команду — впиши RENDER_EXTERNAL_DB_URL заново.

set -euo pipefail

: "${RENDER_EXTERNAL_DB_URL:?Нужно передать RENDER_EXTERNAL_DB_URL (см. комментарий в начале файла)}"

cd "$(dirname "$0")/.." 2>/dev/null || true

CONTAINER=$(docker compose ps -q db 2>/dev/null || true)
if [ -z "$CONTAINER" ]; then
  echo "❌ Локальный контейнер db не найден/не запущен. Запусти сначала: docker compose up -d db"
  exit 1
fi
echo "✓ Локальный контейнер: $CONTAINER"

# Таблицы, где на Render уже могут быть сид-строки от миграций —
# переносим через ON CONFLICT DO NOTHING, а не bulk COPY.
SPECIAL_TABLES="managers settings source_groups"

# Полный список локальных таблиц (кроме служебной schema_migrations) —
# берём динамически, не хардкодим.
ALL_TABLES=$(docker exec "$CONTAINER" psql -U postgres -d torgstore -t -A -c \
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
echo "→ Текущее состояние схемы torgstore на Render (для справки):"
for t in $ALL_TABLES; do
  r=$(count_remote_table "$t" || echo "?")
  [ "$r" != "0" ] && printf "  %-28s %s\n" "$t" "$r"
done

DUMP_FILE="/tmp/torgstore_data_$(date +%s).sql"
: > "$DUMP_FILE"

echo
echo "→ 1/4 Дамп reference-таблиц ($SPECIAL_TABLES) — INSERT с ON CONFLICT DO NOTHING..."
SPECIAL_ARGS=""
for t in $SPECIAL_TABLES; do SPECIAL_ARGS="$SPECIAL_ARGS -t public.$t"; done
# shellcheck disable=SC2086
docker exec "$CONTAINER" pg_dump -U postgres -d torgstore \
  --data-only --no-owner --inserts --on-conflict-do-nothing \
  $SPECIAL_ARGS >> "$DUMP_FILE"

echo "→ 2/4 Дамп остальных таблиц — bulk COPY (быстро, без reference-таблиц)..."
EXCLUDE_ARGS="--exclude-table=public.schema_migrations"
for t in $SPECIAL_TABLES; do EXCLUDE_ARGS="$EXCLUDE_ARGS --exclude-table=public.$t"; done
# shellcheck disable=SC2086
docker exec "$CONTAINER" pg_dump -U postgres -d torgstore \
  --data-only --no-owner \
  $EXCLUDE_ARGS -n public >> "$DUMP_FILE"

echo "→ 3/4 Перенаправляю целевую схему public → torgstore (только структурные строки, не данные)..."
sed -i.bak \
  -e '/^COPY public\./ s/^COPY public\./COPY torgstore./' \
  -e '/^INSERT INTO public\./ s/^INSERT INTO public\./INSERT INTO torgstore./' \
  -e "/^SELECT pg_catalog\.setval/ s/'public\./'torgstore./" \
  -e '/^ALTER TABLE public\./ s/^ALTER TABLE public\./ALTER TABLE torgstore./' \
  "$DUMP_FILE"
rm -f "${DUMP_FILE}.bak"

N_COPY=$(grep -c '^COPY torgstore\.' "$DUMP_FILE" || true)
N_INSERT=$(grep -c '^INSERT INTO torgstore\.' "$DUMP_FILE" || true)
echo "  Таблиц через COPY: $N_COPY, строк через INSERT (reference): $N_INSERT"

echo
echo "→ 4/4 Заливаю в Render (kaspi-db, схема torgstore), одной транзакцией..."
{
  echo "SET CONSTRAINTS ALL DEFERRED;"
  cat "$DUMP_FILE"
} | docker exec -i "$CONTAINER" psql "$RENDER_EXTERNAL_DB_URL" -v ON_ERROR_STOP=1 --single-transaction

echo
echo "→ Проверка: строк локально (public) vs на Render (torgstore)"
MISMATCH=0
for t in $ALL_TABLES; do
  l=$(count_local_table "$t")
  r=$(count_remote_table "$t" || echo "?")
  mark="✓"
  # для reference-таблиц Render может законно иметь ТУ ЖЕ или бОльшую логику
  # ON CONFLICT SKIP не меняет итог, если строки совпали 1-в-1 — числа должны сойтись
  if [ "$l" != "$r" ]; then mark="⚠️"; MISMATCH=1; fi
  printf "  %s %-28s локально=%-8s render=%-8s\n" "$mark" "$t" "$l" "$r"
done

echo
if [ "$MISMATCH" = "1" ]; then
  echo "⚠️  Есть расхождения по строкам — посмотри таблицы с ⚠️ выше (для reference-таблиц это может быть ожидаемо, если на Render уже были свои строки)."
else
  echo "✅ Все таблицы совпадают. Перенос успешен."
fi
echo "   Дамп сохранён тут (можно удалить): $DUMP_FILE"
echo "   Проверь сайт: https://torgstore-api.onrender.com"
