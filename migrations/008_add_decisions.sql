-- ============================================================
-- TorgStore Analytics — Migration 008
-- «Доклад» / управленческие решения — реализация ux_architecture.md,
-- interaction_model.md, causal_model_constitution.md (см. корень проекта).
--
-- Один прецедент = одна запись в decisions (Product Vision / UX Architecture:
-- «сигнал, версии объяснения, выбранная проверка, решение и результат,
-- слитые в одну запись» — сознательно НЕ две таблицы decisions+precedents).
--
-- ЧЕСТНОЕ ОГРАНИЧЕНИЕ V1 (важно для чтения кода decisions.py дальше):
-- в этой системе нет ни одного write-интеграции с CRM (torgstore.zymyran.com
-- используется только на чтение, см. crm-import-runbook.md) — то есть
-- система физически не может сама «перевести 40% лидов с одного менеджера
-- на другого» или сделать что-либо ещё в CRM. Поэтому default_window_ends_at
-- и requires_explicit_confirmation существуют в схеме (готовы на будущее,
-- когда/если появится write-актуатор), но в v1 generate-логика всегда
-- ставит requires_explicit_confirmation=true — ни одно решение не исполняется
-- само по себе молча, все проходят через явный ответ руководителя. Строить
-- фейковый auto-execute, который ничего не делает по-настоящему в CRM, было
-- бы обманом интерфейса — хуже, чем не иметь default-режим вообще.
--
-- Идемпотентная миграция — безопасно запускать повторно.
-- ============================================================

CREATE TABLE IF NOT EXISTS decisions (
    id                    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- Кого/что касается. subject_id/label денормализованы — переживают
    -- переименование или деактивацию менеджера (Зона ответственности
    -- в domain_model.md переживает смену человека, запись о решении не
    -- должна стать нечитаемой, если менеджер уволился).
    subject_type          TEXT NOT NULL DEFAULT 'manager',   -- 'manager' в v1; 'source'/'team' — на будущее
    subject_id            UUID REFERENCES managers(id),
    subject_label         TEXT NOT NULL,

    period                TEXT NOT NULL,
    period_year           INTEGER NOT NULL,
    period_month          INTEGER NOT NULL,

    -- Класс причины по причинно-следственной модели (Классы причин:
    -- Исполнитель/Процесс/Вход/Среда/Данные/Структура/Недостаточно информации) —
    -- здесь сужено до того, что team_center.py реально умеет различать сегодня.
    signal_type           TEXT NOT NULL,     -- 'conversion_gap' | 'plan_shortfall' | 'data_anomaly'

    -- Заголовок — одна фраза с суммой (Interaction Model, п.1), не голый процент.
    title                 TEXT NOT NULL,
    money_impact          NUMERIC NOT NULL DEFAULT 0,   -- материальность в ₸, определяет бюджет/сортировку доклада

    -- «Рассказ» (Interaction Model, п.3): что заметили, какие версии
    -- рассматривались и почему часть отпала, что подтвердилось и чем именно
    -- доказано, что предлагается, какой эффект ожидается. Структура —
    -- see decisions.py: build_narrative(). Не генерируется LLM — собирается
    -- из уже посчитанных team_center.py статусов/инсайтов/аномалий.
    narrative              JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Что предлагается сделать — структурировано, чтобы «изменить параметр»
    -- (конечный ход из interaction_model.md) менял конкретные поля, а не
    -- свободный текст.
    proposed_action        JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Критерий успеха показывается ДО решения (Interaction Model, п.3) и сам
    -- является параметром, который можно менять (Вторая волна, итерация 8).
    success_criteria        JSONB NOT NULL DEFAULT '{}'::jsonb,

    reversible                     BOOLEAN NOT NULL DEFAULT true,
    requires_explicit_confirmation BOOLEAN NOT NULL DEFAULT true,  -- v1: всегда true, см. комментарий выше

    -- Жизненный цикл. 'default_pending'/'default_executed' зарезервированы
    -- под будущий write-актуатор — v1 их не проставляет.
    status                TEXT NOT NULL DEFAULT 'open',
    -- open | accepted | modified | rejected | postponed | default_pending |
    -- default_executed | verifying | confirmed_worked | confirmed_failed |
    -- inconclusive | insufficient_data | superseded

    response_reason        TEXT,             -- причина при отклонении (обязательна для 'rejected')
    response_params         JSONB,            -- изменённые параметры при 'modified' (включая success_criteria)
    decided_at              TIMESTAMPTZ,

    default_window_ends_at  TIMESTAMPTZ,      -- зарезервировано, см. комментарий выше
    postponed_until          TIMESTAMPTZ,

    verify_after             TIMESTAMPTZ,      -- когда возвращаться сверить результат (Interaction Model, п.6)
    verified_at               TIMESTAMPTZ,
    actual_effect              NUMERIC,          -- фактический эффект в ₸ на момент проверки

    -- Дедупликация/связывание: одинаковый ключ у решений про один и тот же
    -- subject+signal_type — используется, чтобы не порождать дубль, если
    -- предыдущее решение по этому же вопросу ещё открыто (см. decisions.py),
    -- и чтобы находить историю «это уже предлагалось» (находка №6 аудита).
    precedent_key             TEXT NOT NULL,
    superseded_by              UUID REFERENCES decisions(id),

    created_at                  TIMESTAMPTZ DEFAULT now(),
    updated_at                   TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status);
CREATE INDEX IF NOT EXISTS idx_decisions_subject ON decisions(subject_type, subject_id);
CREATE INDEX IF NOT EXISTS idx_decisions_precedent_key ON decisions(precedent_key);
CREATE INDEX IF NOT EXISTS idx_decisions_period ON decisions(period);
CREATE INDEX IF NOT EXISTS idx_decisions_verify_after ON decisions(verify_after) WHERE status = 'verifying';

COMMENT ON TABLE decisions IS
  'Управленческое решение = прецедент, слитые в одну запись (ux_architecture.md). Главный объект продукта для пользователя — см. interaction_model.md.';

-- Append-only журнал переходов состояния — техническая гарантия того, что
-- «продукт помнит судьбу своих рекомендаций» (Product Vision), а не только
-- текущий статус в decisions.status. Не редактируется, только INSERT.
CREATE TABLE IF NOT EXISTS decision_events (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    decision_id   UUID NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    event_type    TEXT NOT NULL,   -- 'created' | 'responded' | 'verified' | 'superseded' | 'deep_dive_paused' | 'resumed'
    note          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_decision_events_decision ON decision_events(decision_id, created_at);

COMMENT ON TABLE decision_events IS
  'Append-only история жизни решения — источник для «это уже предлагалось, чем закончилось» и для будущего Индекса руководителя (ux_architecture.md).';
