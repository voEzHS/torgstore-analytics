-- ============================================================
-- TorgStore Analytics — Migration 007
-- Скидки менеджеров — новый, отдельный источник, не смешивать
-- с invoice_stats (тот источник — Розничные продажи/Алматы).
--
-- Источник в CRM: Склад → Накладные, фильтр Статус=Доставлено +
-- Дата создания=период + Менеджер=X (БЕЗ фильтра по каналу — сам
-- эндпоинт CRM `.../requests/total-sum-ajax` не сужается параметром
-- sale_channel на практике, отдаёт сумму по ВСЕМ каналам сразу).
-- Поэтому sale_amount здесь — это НЕ то же самое число, что
-- invoice_stats.gross_revenue (тот скоупнут на Розничные продажи/
-- Алматы) — сверено вживую 26.07.2026, для части менеджеров
-- (Ануар Болысбек, Сабира Ашимова и др.) числа сильно разошлись,
-- потому что их invoice_stats.gross_revenue = 0 (нет розничных
-- накладных за период), а sale_amount здесь > 0 (есть накладные
-- по другим каналам).
--
-- CRM-поля ответа (названы вводяще в заблуждение на стороне CRM):
--   totalSum               -> sale_amount (сумма к оплате, ПОСЛЕ скидки)
--   totalSumWithoutDiscount -> discount_amount (это фактически СУММА
--                              СКИДКИ, а не "сумма без скидки" — имя
--                              поля от CRM, проверено 26.07.2026: у
--                              Клары Кабилановой totalSumWithoutDiscount
--                              = 2 237 768 ₸ совпало один-в-один с
--                              тултипом "Сумма скидок" в интерфейсе CRM).
--
-- Идемпотентная миграция — безопасно запускать повторно.
-- ============================================================

CREATE TABLE IF NOT EXISTS manager_discounts (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    manager_id      UUID NOT NULL REFERENCES managers(id),
    period          TEXT NOT NULL,
    period_year     INTEGER NOT NULL,
    period_month    INTEGER NOT NULL,
    sale_amount     NUMERIC DEFAULT 0,   -- CRM totalSum: к оплате, после скидки, все каналы, статус "Доставлено"
    discount_amount NUMERIC DEFAULT 0,   -- CRM totalSumWithoutDiscount: фактически сумма скидки
    discount_pct    NUMERIC,             -- discount_amount / (sale_amount + discount_amount) * 100
    source          TEXT DEFAULT 'warehouse_requests_total_sum_ajax',
    snapshot_at     TIMESTAMPTZ DEFAULT now(),
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (manager_id, period)
);

CREATE INDEX IF NOT EXISTS idx_manager_discounts_manager_period ON manager_discounts(manager_id, period);

COMMENT ON TABLE manager_discounts IS
  'Скидки, которые менеджер дал клиентам — из CRM Склад/Накладные (статус Доставлено, все каналы). Независимый скоуп от invoice_stats (тот — только Розничные продажи/Алматы), суммы между ними не складывать и не сравнивать напрямую.';
