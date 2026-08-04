-- ============================================================
-- TorgStore Analytics — Migration 005
-- Накладные (факт продаж по кассе/отгрузке) — источник денег.
--
-- Проблема, которую решает эта таблица: Сквозная аналитика считает
-- выручку только по сделкам, доведённым в CRM до стадии "Успешно
-- реализовано" в периоде СОЗДАНИЯ лида. Реальные деньги по накладным
-- (касса, дата отгрузки) оказались в 3-6 раз больше на проверенных
-- менеджерах (Ерганат, Клара, июль 2026) — часть сделок физически
-- оплачена, но карточка в CRM не продвинута менеджером.
--
-- invoice_stats — независимый источник денег, из CRM "Аналитика →
-- Топ продаж" (виджет "Менеджеры", фильтр город+канал), НЕ пересекается
-- со sessions/session_totals (та таблица — для конверсии и атрибуции
-- по источникам, не для итоговой выручки).
--
-- Идемпотентная миграция — безопасно запускать повторно.
-- ============================================================

CREATE TABLE IF NOT EXISTS invoice_stats (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    manager_id     UUID NOT NULL REFERENCES managers(id),
    period         TEXT NOT NULL,
    period_year    INTEGER NOT NULL,
    period_month   INTEGER NOT NULL,
    channel        TEXT DEFAULT 'Розничные продажи',
    city           TEXT DEFAULT 'Алматы',
    gross_revenue  NUMERIC DEFAULT 0,   -- "Продано" из виджета
    doc_count      INTEGER DEFAULT 0,   -- кол-во накладных ("N шт")
    returns_amount NUMERIC DEFAULT 0,   -- "Возвраты" ₸
    returns_count  INTEGER DEFAULT 0,   -- "Возвраты" шт
    net_revenue    NUMERIC DEFAULT 0,   -- gross_revenue - returns_amount (считаем явно, не полагаемся на CRM)
    source         TEXT DEFAULT 'top_prodazh_widget',
    snapshot_at    TIMESTAMPTZ DEFAULT now(),  -- когда сняли срез (для прозрачности "по состоянию на")
    created_at     TIMESTAMPTZ DEFAULT now(),
    updated_at     TIMESTAMPTZ DEFAULT now(),
    UNIQUE (manager_id, period, channel, city)
);

CREATE INDEX IF NOT EXISTS idx_invoice_stats_manager_period ON invoice_stats(manager_id, period);

COMMENT ON TABLE invoice_stats IS
  'Факт продаж по накладным (касса/отгрузка) из CRM "Топ продаж" — источник денег. Независим от sessions (Сквозная аналитика = только конверсия/атрибуция, не выручка).';
