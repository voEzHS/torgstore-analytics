"""
SQLAlchemy ORM модели — отражают схему БД из migrations/001_initial_schema.sql
"""
from sqlalchemy import (
    Column, String, Numeric, Boolean, Integer, Text,
    ForeignKey, ARRAY, UniqueConstraint, Index
)
from sqlalchemy.dialects.postgresql import UUID, JSONB, TIMESTAMP
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from backend.core.database import Base


class Manager(Base):
    __tablename__ = "managers"
    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name       = Column(Text, nullable=False)
    source     = Column(Text)
    color      = Column(Text, default="#2563EB")
    photo_url  = Column(Text)  # data-URL/base64 или внешняя ссылка; грузится вручную через UI (migrations/009_manager_photo.sql)
    is_active  = Column(Boolean, default=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

    sessions = relationship("Session", back_populates="manager")


class SourceGroup(Base):
    __tablename__ = "source_groups"
    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_name = Column(Text, nullable=False)
    match_type = Column(Text, nullable=False, default="contains")
    pattern    = Column(Text)
    aliases    = Column(ARRAY(Text))
    priority   = Column(Integer, default=100)
    color      = Column(Text, default="#6B7280")
    is_active  = Column(Boolean, default=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class Import(Base):
    __tablename__ = "imports"
    id               = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    filename         = Column(Text, nullable=False)
    raw_file_url     = Column(Text)
    parsed_json_url  = Column(Text)
    manager_id       = Column(UUID(as_uuid=True), ForeignKey("managers.id"))
    period           = Column(Text, nullable=False)
    period_year      = Column(Integer, nullable=False)
    period_month     = Column(Integer, nullable=False)
    uploaded_at      = Column(TIMESTAMP(timezone=True), server_default=func.now())
    row_count        = Column(Integer, default=0)
    valid_count      = Column(Integer, default=0)
    warn_count       = Column(Integer, default=0)
    invalid_count    = Column(Integer, default=0)
    status           = Column(Text, default="processing")

    validation_logs = relationship("ValidationLog", back_populates="import_")
    sessions        = relationship("Session", back_populates="import_")


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (UniqueConstraint("manager_id", "period"),)

    id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    import_id    = Column(UUID(as_uuid=True), ForeignKey("imports.id", ondelete="CASCADE"))
    manager_id   = Column(UUID(as_uuid=True), ForeignKey("managers.id"))
    period       = Column(Text, nullable=False)
    period_year  = Column(Integer, nullable=False)
    period_month = Column(Integer, nullable=False)
    created_at   = Column(TIMESTAMP(timezone=True), server_default=func.now())

    import_        = relationship("Import", back_populates="sessions")
    manager        = relationship("Manager", back_populates="sessions")
    totals         = relationship("SessionTotals", back_populates="session", uselist=False)
    source_rows    = relationship("SourceRow", back_populates="session")
    overrides      = relationship("Override", back_populates="session")
    manual_sources = relationship("ManualSource", back_populates="session")
    analytics      = relationship("AnalyticsCache", back_populates="session", uselist=False)


class SessionTotals(Base):
    __tablename__ = "session_totals"
    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id    = Column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), unique=True)
    leads         = Column(Numeric)
    new_leads     = Column(Numeric)
    rep_leads     = Column(Numeric)
    deals         = Column(Numeric)
    new_deals     = Column(Numeric)
    rep_deals     = Column(Numeric)
    revenue       = Column(Numeric)
    rep_revenue   = Column(Numeric)
    conv          = Column(Numeric)   # из Excel, в % (0-100)
    avg_check     = Column(Numeric)
    pct_rep_leads = Column(Numeric)
    pct_rep_deals = Column(Numeric)
    not_realized  = Column(Numeric)  # "Не реализовано" — лиды с финальным статусом "закрыто, не реализовано"

    session = relationship("Session", back_populates="totals")


class SourceRow(Base):
    __tablename__ = "source_rows"
    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id      = Column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"))
    src             = Column(Text, nullable=False)
    campaign        = Column(Text)
    content         = Column(Text)
    group_name      = Column(Text)
    leads           = Column(Numeric)
    new_leads       = Column(Numeric)
    rep_leads       = Column(Numeric)
    deals           = Column(Numeric)
    new_deals       = Column(Numeric)
    rep_deals       = Column(Numeric)
    revenue         = Column(Numeric)
    rep_revenue     = Column(Numeric)
    conv_excel      = Column(Numeric)
    conv_calc       = Column(Numeric)
    conv            = Column(Numeric)
    avg_check_excel = Column(Numeric)
    avg_check       = Column(Numeric)
    pct_rep_deals   = Column(Numeric)
    pct_rep_leads   = Column(Numeric)
    not_realized    = Column(Numeric)  # "Не реализовано" — лиды с финальным статусом "закрыто, не реализовано"
    status          = Column(Text, default="VALID")
    issues          = Column(JSONB, default=list)
    is_manual       = Column(Boolean, default=False)
    created_at      = Column(TIMESTAMP(timezone=True), server_default=func.now())

    session = relationship("Session", back_populates="source_rows")



class Override(Base):
    __tablename__ = "overrides"
    __table_args__ = (UniqueConstraint("session_id", "row_key", "field"),)
    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"))
    row_key    = Column(Text, nullable=False)
    field      = Column(Text, nullable=False)
    value      = Column(Numeric, nullable=False)
    created_by = Column(Text, default="user")
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

    session = relationship("Session", back_populates="overrides")


class ManualSource(Base):
    __tablename__ = "manual_sources"
    __table_args__ = (UniqueConstraint("session_id", "src"),)
    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"))
    src        = Column(Text, nullable=False)
    group_name = Column(Text)
    leads      = Column(Numeric, default=0)
    deals      = Column(Numeric, default=0)
    revenue    = Column(Numeric, default=0)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    session = relationship("Session", back_populates="manual_sources")


class PeriodTarget(Base):
    __tablename__ = "period_targets"
    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    period          = Column(Text, nullable=False, unique=True)
    period_year     = Column(Integer, nullable=False)
    period_month    = Column(Integer, nullable=False)
    plan            = Column(Numeric, default=0)
    company_revenue = Column(Numeric, default=0)
    updated_at      = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())


class AnalyticsCache(Base):
    __tablename__ = "analytics_cache"
    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id      = Column(UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), unique=True)
    computed_at     = Column(TIMESTAMP(timezone=True), server_default=func.now())
    cache_version   = Column(Integer, default=1)
    kpi             = Column(JSONB)
    grouped_sources = Column(JSONB)
    abc             = Column(JSONB)
    pareto          = Column(JSONB)
    insights        = Column(JSONB)
    anomalies       = Column(JSONB)
    forecast        = Column(JSONB)
    quality_report  = Column(JSONB)

    session = relationship("Session", back_populates="analytics")


class ValidationLog(Base):
    __tablename__ = "validation_log"
    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    import_id  = Column(UUID(as_uuid=True), ForeignKey("imports.id", ondelete="CASCADE"))
    row_number = Column(Integer)
    src        = Column(Text)
    rule_code  = Column(Text, nullable=False)
    severity   = Column(Text, nullable=False)
    message    = Column(Text, nullable=False)
    raw_data   = Column(JSONB)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    import_ = relationship("Import", back_populates="validation_logs")


class Setting(Base):
    __tablename__ = "settings"
    key         = Column(Text, primary_key=True)
    value       = Column(JSONB, nullable=False)
    description = Column(Text)
    updated_at  = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())


class ProductImport(Base):
    __tablename__ = "product_imports"
    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    filename      = Column(Text, nullable=False)
    uploaded_at   = Column(TIMESTAMP(timezone=True), server_default=func.now())
    manager_count = Column(Integer, default=0)
    row_count     = Column(Integer, default=0)
    status        = Column(Text, default="done")

    summaries = relationship("ProductSalesSummary", back_populates="import_")


class ProductSalesSummary(Base):
    __tablename__ = "product_sales_summary"
    __table_args__ = (UniqueConstraint("manager_id", "period"),)

    id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    import_id    = Column(UUID(as_uuid=True), ForeignKey("product_imports.id", ondelete="SET NULL"))
    manager_id   = Column(UUID(as_uuid=True), ForeignKey("managers.id"), nullable=False)
    period       = Column(Text, nullable=False)
    period_year  = Column(Integer, nullable=False)
    period_month = Column(Integer, nullable=False)
    positions    = Column(Numeric, default=0)
    qty          = Column(Numeric, default=0)
    revenue      = Column(Numeric, default=0)
    updated_at   = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

    import_    = relationship("ProductImport", back_populates="summaries")
    manager    = relationship("Manager")
    categories = relationship("ProductSalesCategory", back_populates="summary", cascade="all, delete-orphan")
    items      = relationship("ProductSalesItem", back_populates="summary", cascade="all, delete-orphan")


class ProductSalesCategory(Base):
    __tablename__ = "product_sales_categories"
    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    summary_id = Column(UUID(as_uuid=True), ForeignKey("product_sales_summary.id", ondelete="CASCADE"), nullable=False)
    category   = Column(Text, nullable=False)
    qty        = Column(Numeric, default=0)
    revenue    = Column(Numeric, default=0)
    rank       = Column(Integer, default=0)

    summary = relationship("ProductSalesSummary", back_populates="categories")


class ProductSalesItem(Base):
    __tablename__ = "product_sales_items"
    id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    summary_id   = Column(UUID(as_uuid=True), ForeignKey("product_sales_summary.id", ondelete="CASCADE"), nullable=False)
    product_name = Column(Text, nullable=False)
    qty          = Column(Numeric, default=0)
    revenue      = Column(Numeric, default=0)
    rank         = Column(Integer, default=0)

    summary = relationship("ProductSalesSummary", back_populates="items")


class ProductOverride(Base):
    __tablename__ = "product_overrides"
    __table_args__ = (UniqueConstraint("target_type", "target_id", "field"),)

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    target_type = Column(Text, nullable=False)  # 'summary' | 'category' | 'item'
    target_id   = Column(UUID(as_uuid=True), nullable=False)
    field       = Column(Text, nullable=False)
    value       = Column(Text, nullable=False)
    created_by  = Column(Text, default="user")
    created_at  = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at  = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())


class InvoiceStats(Base):
    """
    Факт продаж по накладным (касса/отгрузка) — из CRM "Аналитика → Топ
    продаж", виджет "Менеджеры" (фильтр город+канал). Независимый источник
    денег, не пересекается с sessions/session_totals (та ветка — только
    конверсия и атрибуция по источникам трафика, см. комментарий в
    migrations/005_add_invoice_stats.sql).
    """
    __tablename__ = "invoice_stats"
    __table_args__ = (UniqueConstraint("manager_id", "period", "channel", "city"),)

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    manager_id     = Column(UUID(as_uuid=True), ForeignKey("managers.id"), nullable=False)
    period         = Column(Text, nullable=False)
    period_year    = Column(Integer, nullable=False)
    period_month   = Column(Integer, nullable=False)
    channel        = Column(Text, default="Розничные продажи")
    city           = Column(Text, default="Алматы")
    gross_revenue  = Column(Numeric, default=0)
    doc_count      = Column(Integer, default=0)
    returns_amount = Column(Numeric, default=0)
    returns_count  = Column(Integer, default=0)
    net_revenue    = Column(Numeric, default=0)
    source         = Column(Text, default="top_prodazh_widget")
    snapshot_at    = Column(TIMESTAMP(timezone=True), server_default=func.now())
    created_at     = Column(TIMESTAMP(timezone=True), server_default=func.now())
    # onupdate — без него это поле было "created_at под другим именем": при
    # повторном импорте (upsert по manager_id+period+channel+city) значение
    # никогда не обновлялось, сигнал "когда данные реально в последний раз
    # подтягивались" был неверным. Нужно для /data-freshness (08.08.2026).
    updated_at     = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())


class DeclineReasonStats(Base):
    """
    Причины отказа (CRM custom field «Причина отказа») по лидам «Не
    реализовано» — ВЫБОРКА, не исчерпывающий список (см. sample_size).
    Поле не агрегируется нигде в built-in отчётах CRM Zymyran; собрано
    чтением по одному лиду за раз через /api/crm/leads/details с
    соблюдением rate-limit 3с/запрос (см. migrations/010_add_decline_reasons.sql).
    """
    __tablename__ = "decline_reason_stats"
    __table_args__ = (UniqueConstraint("manager_id", "period", "pipeline", "reason"),)

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    manager_id    = Column(UUID(as_uuid=True), ForeignKey("managers.id"), nullable=False)
    period        = Column(Text, nullable=False)
    period_year   = Column(Integer, nullable=False)
    period_month  = Column(Integer, nullable=False)
    pipeline      = Column(Text, default="Розница Алматы")
    reason        = Column(Text, nullable=False)
    count         = Column(Integer, default=0)
    sample_size   = Column(Integer, default=0)
    source        = Column(Text, default="crm_lead_custom_field_sample")
    snapshot_at   = Column(TIMESTAMP(timezone=True), server_default=func.now())
    created_at    = Column(TIMESTAMP(timezone=True), server_default=func.now())
    # onupdate — см. комментарий у InvoiceStats.updated_at выше (тот же баг).
    updated_at    = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())


class ManagerDiscount(Base):
    """
    Скидки менеджеров — из CRM Склад/Накладные (эндпоинт
    .../requests/total-sum-ajax, статус "Доставлено"). Независимый скоуп
    от InvoiceStats: тот источник ограничен Розничные продажи/Алматы, этот —
    все каналы сразу (сам эндпоинт CRM не режется параметром sale_channel на
    практике — проверено 26.07.2026). Суммы между двумя таблицами не складывать
    и не сравнивать напрямую, см. комментарий в migrations/007_add_manager_discounts.sql.
    """
    __tablename__ = "manager_discounts"
    __table_args__ = (UniqueConstraint("manager_id", "period"),)

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    manager_id      = Column(UUID(as_uuid=True), ForeignKey("managers.id"), nullable=False)
    period          = Column(Text, nullable=False)
    period_year     = Column(Integer, nullable=False)
    period_month    = Column(Integer, nullable=False)
    sale_amount     = Column(Numeric, default=0)
    discount_amount = Column(Numeric, default=0)
    discount_pct    = Column(Numeric)
    source          = Column(Text, default="warehouse_requests_total_sum_ajax")
    snapshot_at     = Column(TIMESTAMP(timezone=True), server_default=func.now())
    created_at      = Column(TIMESTAMP(timezone=True), server_default=func.now())
    # onupdate — см. комментарий у InvoiceStats.updated_at выше (тот же баг).
    updated_at      = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())


class Decision(Base):
    """
    Управленческое решение = прецедент, слитые в одну запись — см.
    ux_architecture.md («Главный объект») и migrations/008_add_decisions.sql
    (там же — честное ограничение v1: нет write-актуатора в CRM, поэтому
    requires_explicit_confirmation в generate-логике всегда true).
    """
    __tablename__ = "decisions"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    subject_type   = Column(Text, nullable=False, default="manager")
    subject_id     = Column(UUID(as_uuid=True), ForeignKey("managers.id"))
    subject_label  = Column(Text, nullable=False)

    period         = Column(Text, nullable=False)
    period_year    = Column(Integer, nullable=False)
    period_month   = Column(Integer, nullable=False)

    signal_type    = Column(Text, nullable=False)
    title          = Column(Text, nullable=False)
    money_impact   = Column(Numeric, nullable=False, default=0)

    narrative        = Column(JSONB, nullable=False, default=dict)
    proposed_action  = Column(JSONB, nullable=False, default=dict)
    success_criteria = Column(JSONB, nullable=False, default=dict)

    reversible                     = Column(Boolean, nullable=False, default=True)
    requires_explicit_confirmation = Column(Boolean, nullable=False, default=True)

    status = Column(Text, nullable=False, default="open")

    response_reason = Column(Text)
    response_params  = Column(JSONB)
    decided_at        = Column(TIMESTAMP(timezone=True))

    default_window_ends_at = Column(TIMESTAMP(timezone=True))
    postponed_until          = Column(TIMESTAMP(timezone=True))

    verify_after  = Column(TIMESTAMP(timezone=True))
    verified_at    = Column(TIMESTAMP(timezone=True))
    actual_effect   = Column(Numeric)

    precedent_key   = Column(Text, nullable=False)
    superseded_by   = Column(UUID(as_uuid=True), ForeignKey("decisions.id"))

    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    subject = relationship("Manager", foreign_keys=[subject_id])
    events  = relationship("DecisionEvent", back_populates="decision", order_by="DecisionEvent.created_at")


class DecisionEvent(Base):
    """Append-only журнал переходов — см. комментарий в migrations/008_add_decisions.sql."""
    __tablename__ = "decision_events"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    decision_id = Column(UUID(as_uuid=True), ForeignKey("decisions.id", ondelete="CASCADE"), nullable=False)
    event_type  = Column(Text, nullable=False)
    note        = Column(JSONB, nullable=False, default=dict)
    created_at  = Column(TIMESTAMP(timezone=True), server_default=func.now())

    decision = relationship("Decision", back_populates="events")
