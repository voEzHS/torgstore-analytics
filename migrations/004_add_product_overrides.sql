-- ============================================================
-- TorgStore Analytics — Migration 004
-- Точечные правки товарной аналитики, переживающие повторный импорт.
--
-- Проблема: CRM-аналитика может обновляться задним числом (новые
-- доставки, исправления) даже за прошлые месяцы. При повторной
-- выгрузке из CRM данные за менеджера/период полностью замещаются —
-- любая ручная правка в Excel потерялась бы. Это решение по аналогии
-- с уже существующим механизмом overrides для воронки источников
-- (см. 001_initial_schema.sql, таблица overrides).
--
-- target_type: 'summary' | 'category' | 'item' — какая таблица
-- target_id:   id строки в product_sales_summary / _categories / _items
-- field:       имя поля (positions, qty, revenue, category, product_name...)
-- value:       хранится как TEXT (числа парсятся на чтении)
--
-- Идемпотентная миграция — безопасно запускать повторно.
-- ============================================================

CREATE TABLE IF NOT EXISTS product_overrides (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    target_type TEXT NOT NULL CHECK (target_type IN ('summary','category','item')),
    target_id   UUID NOT NULL,
    field       TEXT NOT NULL,
    value       TEXT NOT NULL,
    created_by  TEXT DEFAULT 'user',
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (target_type, target_id, field)
);

CREATE INDEX IF NOT EXISTS idx_product_overrides_target ON product_overrides(target_type, target_id);

COMMENT ON TABLE product_overrides IS
  'Ручные правки товарной аналитики. Применяются поверх сырых импортированных значений при чтении (GET /products/{manager_id}) и не удаляются при повторном импорте того же периода.';
