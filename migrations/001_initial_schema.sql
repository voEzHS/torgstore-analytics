-- ============================================================
-- TorgStore Analytics — Initial Schema
-- Migration 001
-- ============================================================

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm"; -- for fuzzy text search

-- ============================================================
-- MANAGERS
-- ============================================================
CREATE TABLE managers (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        TEXT NOT NULL,
    source      TEXT,               -- основной рекламный источник
    color       TEXT DEFAULT '#2563EB',
    is_active   BOOLEAN DEFAULT true,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- Системная запись: весь отдел
INSERT INTO managers (id, name, source, color)
VALUES ('00000000-0000-0000-0000-000000000001', 'Весь отдел', NULL, '#1E3A5F');

-- ============================================================
-- SOURCE GROUPS — правила группировки (не хардкод в коде)
-- ============================================================
CREATE TABLE source_groups (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    group_name  TEXT NOT NULL,      -- 'Google', 'Instagram / Meta'
    -- Тип матчинга: regex | alias | prefix | contains
    match_type  TEXT NOT NULL DEFAULT 'contains',
    pattern     TEXT,               -- regex или строка для prefix/contains
    aliases     TEXT[],             -- точные алиасы: ['google','gdn','www.google.com']
    priority    INT DEFAULT 100,    -- меньше = выше приоритет
    color       TEXT DEFAULT '#6B7280',
    is_active   BOOLEAN DEFAULT true,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Дефолтные правила группировки
INSERT INTO source_groups (group_name, match_type, pattern, aliases, priority, color) VALUES
('Google',           'regex',    'google|gdn|tagassist|com\.google',
    ARRAY['google','gdn','www.google.com','tagassistant.google.com',
          'com.google.android.googlequicksearchbox','google_search_provinciya'],
    10, '#4285F4'),
('Instagram / Meta', 'regex',    'instagram|facebook|meta',
    ARRAY['ig','instagram'],
    20, '#E1306C'),
('TikTok',           'alias',    NULL, ARRAY['tiktok'],                      30, '#010101'),
('YouTube',          'regex',    'youtube',  ARRAY['youtube','youtube_kz'],  40, '#FF0000'),
('Яндекс',           'regex',    'yandex',   ARRAY['yandex','yandex.kz'],    50, '#FFCC00'),
('OLX',              'regex',    '\bolx\b',  ARRAY['olx','OLX'],             60, '#002F34'),
('2GIS',             'regex',    '2gis',     ARRAY['2GIS','Torgstore 2gis'], 70, '#00B956'),
('Satu',             'contains', 'satu',     NULL,                           80, '#FF6B00'),
('Leadbros',         'contains', 'leadbros', NULL,                           90, '#7C3AED'),
('PikPro',           'contains', 'pikpro',   NULL,                           95, '#0891B2'),
('Лендинг',          'contains', 'lending',  NULL,                           98, '#059669'),
('Органика',         'alias',    NULL, ARRAY['chatgpt.com','organic'],       99, '#65A30D'),
('Другое',           'regex',    '.*',       NULL,                          999, '#9CA3AF');

-- ============================================================
-- IMPORTS — история загрузок файлов
-- ============================================================
CREATE TABLE imports (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    filename        TEXT NOT NULL,
    raw_file_url    TEXT,           -- Supabase Storage URL (Excel)
    parsed_json_url TEXT,           -- Supabase Storage URL (parsed JSON)
    manager_id      UUID REFERENCES managers(id),
    period          TEXT NOT NULL,  -- 'Июнь 2026'
    period_year     INT NOT NULL,
    period_month    INT NOT NULL,   -- 1-12
    uploaded_at     TIMESTAMPTZ DEFAULT now(),
    row_count       INT DEFAULT 0,
    valid_count     INT DEFAULT 0,
    warn_count      INT DEFAULT 0,
    invalid_count   INT DEFAULT 0,
    status          TEXT DEFAULT 'processing' -- processing | done | error
);

-- ============================================================
-- SESSIONS — аналитический период (менеджер + период)
-- ============================================================
CREATE TABLE sessions (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    import_id   UUID REFERENCES imports(id) ON DELETE CASCADE,
    manager_id  UUID REFERENCES managers(id),
    period      TEXT NOT NULL,
    period_year INT NOT NULL,
    period_month INT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE(manager_id, period)
);

-- ============================================================
-- SESSION TOTALS — строка ИТОГО (источник истины для KPI)
-- ============================================================
CREATE TABLE session_totals (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id      UUID REFERENCES sessions(id) ON DELETE CASCADE UNIQUE,
    leads           NUMERIC,
    new_leads       NUMERIC,
    rep_leads       NUMERIC,
    deals           NUMERIC,
    new_deals       NUMERIC,
    rep_deals       NUMERIC,
    revenue         NUMERIC,
    rep_revenue     NUMERIC,
    conv            NUMERIC,    -- конверсия в % (0-100), из Excel
    avg_check       NUMERIC,
    pct_rep_leads   NUMERIC,
    pct_rep_deals   NUMERIC
);

-- ============================================================
-- SOURCE ROWS — каждая UTM-строка источника
-- ============================================================
CREATE TABLE source_rows (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id      UUID REFERENCES sessions(id) ON DELETE CASCADE,
    src             TEXT NOT NULL,      -- оригинальное название из Excel/CRM
    campaign        TEXT,
    content         TEXT,
    group_name      TEXT,               -- результат группировки
    -- Лиды
    leads           NUMERIC,
    new_leads       NUMERIC,
    rep_leads       NUMERIC,
    -- Продажи
    deals           NUMERIC,
    new_deals       NUMERIC,
    rep_deals       NUMERIC,
    -- Выручка
    revenue         NUMERIC,
    rep_revenue     NUMERIC,
    -- Конверсия
    conv_excel      NUMERIC,    -- из колонки Excel, может быть >100% (ошибка CRM)
    conv_calc       NUMERIC,    -- deals/leads*100 (наш расчёт)
    conv            NUMERIC,    -- финальная: excel если валидна, иначе calc
    -- Средний чек
    avg_check_excel NUMERIC,    -- из Excel
    avg_check       NUMERIC,    -- финальный: excel или revenue/deals
    -- Доп. показатели
    pct_rep_deals   NUMERIC,
    pct_rep_leads   NUMERIC,
    -- Качество строки
    status          TEXT NOT NULL DEFAULT 'VALID', -- VALID | WARNING | INVALID
    issues          JSONB DEFAULT '[]',  -- [{code, msg, severity}]
    -- Ручной ввод
    is_manual       BOOLEAN DEFAULT false,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_source_rows_session ON source_rows(session_id);
CREATE INDEX idx_source_rows_status  ON source_rows(session_id, status);
CREATE INDEX idx_source_rows_group   ON source_rows(session_id, group_name);

-- ============================================================
-- CUSTOMERS — история клиентов (для LTV, Retention, когорт)
-- ============================================================
CREATE TABLE customers (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    external_id     TEXT,           -- ID из CRM если есть
    phone_hash      TEXT,           -- хэш телефона для дедупликации
    email_hash      TEXT,
    name            TEXT,
    first_seen      DATE,           -- дата первой покупки
    last_seen       DATE,           -- дата последней покупки
    source_first    TEXT,           -- источник первого привлечения
    manager_first_id UUID REFERENCES managers(id),
    total_orders    INT DEFAULT 0,
    total_revenue   NUMERIC DEFAULT 0,
    ltv             NUMERIC DEFAULT 0,
    cohort_month    TEXT,           -- 'Июнь 2026' (месяц первой покупки)
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_customers_cohort   ON customers(cohort_month);
CREATE INDEX idx_customers_phone    ON customers(phone_hash);

-- ============================================================
-- SALES — каждая сделка отдельной записью
-- ============================================================
CREATE TABLE sales (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id      UUID REFERENCES sessions(id) ON DELETE SET NULL,
    source_row_id   UUID REFERENCES source_rows(id) ON DELETE SET NULL,
    customer_id     UUID REFERENCES customers(id) ON DELETE SET NULL,
    manager_id      UUID REFERENCES managers(id),
    -- Данные сделки
    closed_at       DATE,           -- дата закрытия сделки
    amount          NUMERIC NOT NULL DEFAULT 0,
    is_repeat       BOOLEAN DEFAULT false,
    is_pipeline     BOOLEAN DEFAULT false,  -- закрытие сделки прошлых периодов
    -- Атрибуция
    source          TEXT,           -- источник лида
    campaign        TEXT,
    group_name      TEXT,
    -- Период в котором закрыта сделка
    period          TEXT,
    period_year     INT,
    period_month    INT,
    -- Период в котором пришёл лид (для когортного анализа)
    lead_period     TEXT,
    -- Метаданные
    external_id     TEXT,           -- ID сделки из CRM
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_sales_session    ON sales(session_id);
CREATE INDEX idx_sales_manager    ON sales(manager_id);
CREATE INDEX idx_sales_period     ON sales(period_year, period_month);
CREATE INDEX idx_sales_closed_at  ON sales(closed_at);
CREATE INDEX idx_sales_customer   ON sales(customer_id);

-- ============================================================
-- OVERRIDES — ручные корректировки данных пользователем
-- ============================================================
CREATE TABLE overrides (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id  UUID REFERENCES sessions(id) ON DELETE CASCADE,
    row_key     TEXT NOT NULL,      -- 'itogo' или source_row UUID
    field       TEXT NOT NULL,      -- 'leads', 'deals', 'revenue'
    value       NUMERIC NOT NULL,
    created_by  TEXT DEFAULT 'user',
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE(session_id, row_key, field)
);

-- ============================================================
-- MANUAL SOURCES — источники добавленные вручную
-- ============================================================
CREATE TABLE manual_sources (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id  UUID REFERENCES sessions(id) ON DELETE CASCADE,
    src         TEXT NOT NULL,
    group_name  TEXT,
    leads       NUMERIC DEFAULT 0,
    deals       NUMERIC DEFAULT 0,
    revenue     NUMERIC DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE(session_id, src)
);

-- ============================================================
-- PERIOD TARGETS — планы и выручка компании по периодам
-- ============================================================
CREATE TABLE period_targets (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    period          TEXT NOT NULL UNIQUE,
    period_year     INT NOT NULL,
    period_month    INT NOT NULL,
    plan            NUMERIC DEFAULT 0,
    company_revenue NUMERIC DEFAULT 0,
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- ANALYTICS CACHE — только кэш, не источник истины
-- ============================================================
CREATE TABLE analytics_cache (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id      UUID REFERENCES sessions(id) ON DELETE CASCADE UNIQUE,
    computed_at     TIMESTAMPTZ DEFAULT now(),
    cache_version   INT DEFAULT 1,  -- инкрементируется при изменении формул
    -- KPI (с учётом overrides + manual_sources)
    kpi             JSONB,
    -- Сгруппированные источники
    grouped_sources JSONB,
    -- Аналитика
    abc             JSONB,
    pareto          JSONB,
    insights        JSONB,
    anomalies       JSONB,
    -- Прогноз (для dept сессий при наличии нескольких периодов)
    forecast        JSONB,
    -- Сравнение
    quality_report  JSONB
);

-- ============================================================
-- VALIDATION LOG — лог всех ошибок импорта
-- ============================================================
CREATE TABLE validation_log (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    import_id   UUID REFERENCES imports(id) ON DELETE CASCADE,
    row_number  INT,
    src         TEXT,
    rule_code   TEXT NOT NULL,  -- CONV_GT_100, DEALS_GT_LEADS, etc.
    severity    TEXT NOT NULL,  -- INVALID | WARNING | INFO
    message     TEXT NOT NULL,
    raw_data    JSONB,          -- снимок исходной строки
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_validation_log_import ON validation_log(import_id);

-- ============================================================
-- SETTINGS — настройки системы
-- ============================================================
CREATE TABLE settings (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    description TEXT,
    updated_at  TIMESTAMPTZ DEFAULT now()
);

INSERT INTO settings (key, value, description) VALUES
('conv_threshold_good', '15',    'Конверсия "Отлично" ≥ %'),
('conv_threshold_ok',   '8',     'Конверсия "Средне" ≥ %'),
('repeat_warn',         '70',    'Порог повторных продаж для предупреждения %'),
('cache_version',       '1',     'Версия формул кэша — при изменении инвалидирует кэш'),
('app_name',            '"TorgStore Analytics"', 'Название приложения');

-- ============================================================
-- AUDIT LOG — история всех изменений
-- ============================================================
CREATE TABLE audit_log (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    table_name  TEXT NOT NULL,
    record_id   UUID,
    action      TEXT NOT NULL,  -- INSERT | UPDATE | DELETE
    old_data    JSONB,
    new_data    JSONB,
    user_id     TEXT,
    ip_address  TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_audit_log_table  ON audit_log(table_name, record_id);
CREATE INDEX idx_audit_log_time   ON audit_log(created_at);

-- ============================================================
-- VIEWS — удобные представления для API
-- ============================================================

-- Список периодов с метаданными
CREATE OR REPLACE VIEW v_periods AS
SELECT
    s.period,
    s.period_year,
    s.period_month,
    COUNT(DISTINCT s.id)       AS session_count,
    COUNT(DISTINCT s.manager_id) AS manager_count,
    MAX(i.uploaded_at)         AS last_uploaded,
    SUM(st.revenue)            AS total_revenue,
    SUM(st.deals)              AS total_deals
FROM sessions s
LEFT JOIN imports i         ON i.id = s.import_id
LEFT JOIN session_totals st ON st.session_id = s.id
GROUP BY s.period, s.period_year, s.period_month
ORDER BY s.period_year DESC, s.period_month DESC;

-- KPI отдела по периоду (из session_totals, не из source_rows)
CREATE OR REPLACE VIEW v_dept_kpi AS
SELECT
    s.period,
    s.period_year,
    s.period_month,
    st.leads, st.new_leads, st.rep_leads,
    st.deals, st.new_deals, st.rep_deals,
    st.revenue, st.rep_revenue,
    st.conv, st.avg_check,
    st.pct_rep_deals, st.pct_rep_leads,
    pt.plan, pt.company_revenue
FROM sessions s
JOIN session_totals st ON st.session_id = s.id
LEFT JOIN period_targets pt ON pt.period = s.period
WHERE s.manager_id = '00000000-0000-0000-0000-000000000001' -- весь отдел
ORDER BY s.period_year DESC, s.period_month DESC;
