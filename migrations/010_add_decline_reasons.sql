-- ============================================================
-- TorgStore Analytics — Migration 010
-- Причины отказа (Причина отказа) по лидам в статусе «Не реализовано».
--
-- Контекст: в CRM (Zymyran) «Причина отказа» — обязательное
-- пользовательское поле на лиде (не агрегируется нигде в built-in
-- отчётах CRM — ни в Отчётах, ни в Аналитике, ни в фильтрах, ни в
-- экспорте). Единственный способ получить данные — прочитать поле по
-- каждому лиду в статусе «Не реализовано» через API
-- /api/crm/leads/details?lead_id=.
--
-- Из-за правила 3 секунд между запросами к CRM (см. CLAUDE.md) полная
-- выгрузка по всем лидам за период непрактична при больших объёмах —
-- поэтому здесь хранится ВЫБОРКА (например, последние 20-30 отказов на
-- менеджера), а не исчерпывающий список. sample_size — сколько лидов
-- реально прочитано для этого среза, чтобы на фронтенде было явно
-- видно «это выборка из N», а не полная картина.
--
-- Строка на (менеджер, период, причина) — количество попаданий этой
-- причины в выборке. Отдельная причина '(пусто/не указано)' — лиды, где
-- обязательное поле не заполнено (реальная проблема дисциплины ввода
-- данных у части менеджеров, обнаруженная при выгрузке за июль 2026).
--
-- Идемпотентная миграция — безопасно запускать повторно.
-- ============================================================

CREATE TABLE IF NOT EXISTS decline_reason_stats (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    manager_id    UUID NOT NULL REFERENCES managers(id),
    period        TEXT NOT NULL,
    period_year   INTEGER NOT NULL,
    period_month  INTEGER NOT NULL,
    pipeline      TEXT NOT NULL DEFAULT 'Розница Алматы',  -- воронка CRM, откуда выгружено
    reason        TEXT NOT NULL,                            -- значение поля «Причина отказа», или '(пусто/не указано)'
    count         INTEGER NOT NULL DEFAULT 0,                -- сколько раз причина встретилась в выборке
    sample_size   INTEGER NOT NULL DEFAULT 0,                -- размер всей выборки менеджер+период+воронка (для расчёта доли/fill rate)
    source        TEXT DEFAULT 'crm_lead_custom_field_sample',
    snapshot_at   TIMESTAMPTZ DEFAULT now(),
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now(),
    UNIQUE (manager_id, period, pipeline, reason)
);

CREATE INDEX IF NOT EXISTS idx_decline_reason_stats_manager_period ON decline_reason_stats(manager_id, period);

COMMENT ON TABLE decline_reason_stats IS
  'Причины отказа (CRM custom field «Причина отказа») по лидам «Не реализовано» — ВЫБОРКА (не исчерпывающий список, см. sample_size), т.к. поле не агрегируется в built-in отчётах CRM и читается по одному лиду за раз с rate-limit 3с/запрос.';
