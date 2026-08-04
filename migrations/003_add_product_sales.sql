-- ============================================================
-- TorgStore Analytics — Migration 003
-- Товарная аналитика по менеджерам (категории/конкретные товары),
-- собранная из CRM Zymyran (Аналитика → Товары, фильтр по дате доставки).
--
-- Независимо от воронки источников трафика (sessions/source_rows) —
-- НЕ трогает существующие таблицы. Ключ связи: (manager_id, period),
-- тот же формат периода, что и в sessions ("Июнь 2026").
--
-- Идемпотентная миграция — безопасно запускать повторно.
-- ============================================================

CREATE TABLE IF NOT EXISTS product_imports (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    filename      TEXT NOT NULL,
    uploaded_at   TIMESTAMPTZ DEFAULT now(),
    manager_count INTEGER DEFAULT 0,
    row_count     INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'done'
);

CREATE TABLE IF NOT EXISTS product_sales_summary (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    import_id    UUID REFERENCES product_imports(id) ON DELETE SET NULL,
    manager_id   UUID NOT NULL REFERENCES managers(id),
    period       TEXT NOT NULL,
    period_year  INTEGER NOT NULL,
    period_month INTEGER NOT NULL,
    positions    NUMERIC DEFAULT 0,
    qty          NUMERIC DEFAULT 0,
    revenue      NUMERIC DEFAULT 0,
    updated_at   TIMESTAMPTZ DEFAULT now(),
    UNIQUE (manager_id, period)
);

CREATE TABLE IF NOT EXISTS product_sales_categories (
    id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    summary_id UUID NOT NULL REFERENCES product_sales_summary(id) ON DELETE CASCADE,
    category   TEXT NOT NULL,
    qty        NUMERIC DEFAULT 0,
    revenue    NUMERIC DEFAULT 0,
    rank       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS product_sales_items (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    summary_id   UUID NOT NULL REFERENCES product_sales_summary(id) ON DELETE CASCADE,
    product_name TEXT NOT NULL,
    qty          NUMERIC DEFAULT 0,
    revenue      NUMERIC DEFAULT 0,
    rank         INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_pss_manager_period ON product_sales_summary(manager_id, period);
CREATE INDEX IF NOT EXISTS idx_psc_summary ON product_sales_categories(summary_id);
CREATE INDEX IF NOT EXISTS idx_psi_summary ON product_sales_items(summary_id);

COMMENT ON TABLE product_sales_summary IS
  'Итоги товарных продаж по менеджеру за период (из CRM Zymyran, разрез по дате доставки). Не связано с воронкой источников трафика (sessions).';
COMMENT ON TABLE product_sales_categories IS
  'До 10 крупнейших по выручке товарных категорий на менеджера/период, rank = позиция по убыванию выручки.';
COMMENT ON TABLE product_sales_items IS
  'До 8 наиболее продаваемых по выручке конкретных товарных позиций (SKU) на менеджера/период.';
