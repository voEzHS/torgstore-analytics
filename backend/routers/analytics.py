"""
GET  /api/v1/analytics/{session_id}   — готовая аналитика (из кэша или пересчёт)
POST /api/v1/analytics/{session_id}/recalculate — принудительный пересчёт
"""
import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.core.database import get_db
from backend.models.models import (
    Session, SessionTotals, SourceRow, Override, ManualSource,
    AnalyticsCache, SourceGroup, Setting, PeriodTarget
)
from backend.analytics.engine import (
    compute_kpi, compute_grouped_sources, compute_abc,
    compute_pareto, compute_anomalies, compute_insights, compute_forecast,
    EnrichedRow
)

router = APIRouter(tags=["Analytics"])

CURRENT_CACHE_VERSION = 1


async def _get_settings(db: AsyncSession) -> dict:
    """Загружаем настройки из БД"""
    result = await db.execute(select(Setting))
    settings = {}
    for s in result.scalars().all():
        val = s.value
        # Распаковываем JSON значения
        try:
            settings[s.key] = float(val) if isinstance(val, str) else val
        except (TypeError, ValueError):
            settings[s.key] = val
    return settings


async def _get_group_rules(db: AsyncSession) -> list[dict]:
    result = await db.execute(
        select(SourceGroup).where(SourceGroup.is_active == True).order_by(SourceGroup.priority)
    )
    return [
        {"group_name": g.group_name, "match_type": g.match_type,
         "pattern": g.pattern, "aliases": g.aliases or [],
         "priority": g.priority, "is_active": g.is_active}
        for g in result.scalars().all()
    ]


async def compute_and_cache(session_id: str, db: AsyncSession) -> dict:
    """
    Вся аналитика считается здесь на Python.
    Результат сохраняется в analytics_cache.
    Источник истины — таблицы БД (session_totals, source_rows, overrides, manual_sources).
    """
    sess_uuid = uuid.UUID(session_id)

    # Загружаем данные из БД
    sess = await db.get(Session, sess_uuid)
    if not sess:
        raise HTTPException(status_code=404, detail="Сессия не найдена")

    totals_row = await db.execute(
        select(SessionTotals).where(SessionTotals.session_id == sess_uuid)
    )
    totals = totals_row.scalar_one_or_none()

    rows_result = await db.execute(
        select(SourceRow).where(SourceRow.session_id == sess_uuid)
    )
    rows = rows_result.scalars().all()

    overrides_result = await db.execute(
        select(Override).where(Override.session_id == sess_uuid)
    )
    overrides_list = overrides_result.scalars().all()

    manual_result = await db.execute(
        select(ManualSource).where(ManualSource.session_id == sess_uuid)
    )
    manual_rows = [{"src": m.src, "leads": float(m.leads or 0),
                    "deals": float(m.deals or 0), "revenue": float(m.revenue or 0)}
                   for m in manual_result.scalars().all()]

    # Настройки
    settings = await _get_settings(db)
    conv_good = float(settings.get("conv_threshold_good", 15))
    conv_ok   = float(settings.get("conv_threshold_ok",   8))
    cache_ver = int(settings.get("cache_version", 1))

    # Period targets — план компании (bare period-key) применим ТОЛЬКО для
    # сессии «Весь отдел» (manager_id == DEPT_MANAGER_ID). Для сессии
    # конкретного менеджера нужен ЕГО личный план ("mgr:<id>:<период>",
    # иначе "mgr:<id>:default") — иначе plan_pct в compute_insights() считает
    # "выручка одного менеджера ÷ план всей компании", что даёт ложные ~0.4%
    # вместо ~40% в «Зонах роста»/«Сильных сторонах» досье (тот же паттерн
    # уже верно реализован в team_center._mgr_plan(), продублирован здесь).
    DEPT_MANAGER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
    if sess.manager_id == DEPT_MANAGER_ID:
        pt_result = await db.execute(
            select(PeriodTarget).where(PeriodTarget.period == sess.period)
        )
        pt = pt_result.scalar_one_or_none()
        plan            = float(pt.plan or 0)            if pt else 0
        company_revenue = float(pt.company_revenue or 0) if pt else 0
    else:
        specific_key = f"mgr:{sess.manager_id}:{sess.period}"
        default_key  = f"mgr:{sess.manager_id}:default"
        pt_result = await db.execute(
            select(PeriodTarget.period, PeriodTarget.plan)
            .where(PeriodTarget.period.in_([specific_key, default_key]))
        )
        pt_rows = {p: float(v or 0) for p, v in pt_result.all()}
        plan = pt_rows.get(specific_key) or pt_rows.get(default_key, 0.0)
        company_revenue = 0.0  # ручной ввод «общей выручки» существует только для «Весь отдел»

    # Overrides словарь
    overrides_itogo  = {}
    overrides_by_row = {}
    for ov in overrides_list:
        if ov.row_key == "itogo":
            overrides_itogo[ov.field] = float(ov.value)
        else:
            if ov.row_key not in overrides_by_row:
                overrides_by_row[ov.row_key] = {}
            overrides_by_row[ov.row_key][ov.field] = float(ov.value)

    # Создаём EnrichedRow объекты из ORM для движка аналитики
    from backend.analytics.engine import EnrichedRow, Totals
    enriched_rows = []
    for r in rows:
        er = EnrichedRow(
            src=r.src, campaign=r.campaign or "", content=r.content or "",
            leads=float(r.leads) if r.leads is not None else None,
            new_leads=float(r.new_leads) if r.new_leads else None,
            rep_leads=float(r.rep_leads) if r.rep_leads else None,
            deals=float(r.deals) if r.deals is not None else None,
            new_deals=float(r.new_deals) if r.new_deals else None,
            rep_deals=float(r.rep_deals) if r.rep_deals else None,
            revenue=float(r.revenue) if r.revenue is not None else None,
            rep_revenue=float(r.rep_revenue) if r.rep_revenue else None,
            conv_excel=float(r.conv_excel) if r.conv_excel is not None else None,
            avg_check_excel=float(r.avg_check_excel) if r.avg_check_excel else None,
            pct_rep_leads=float(r.pct_rep_leads) if r.pct_rep_leads else None,
            pct_rep_deals=float(r.pct_rep_deals) if r.pct_rep_deals else None,
            row_number=0, raw_data={},
            status=r.status, issues=r.issues or [],
            group_name=r.group_name or "",
            conv_calc=float(r.conv_calc) if r.conv_calc is not None else None,
            conv=float(r.conv) if r.conv is not None else None,
            avg_check=float(r.avg_check) if r.avg_check is not None else None,
            not_realized=float(r.not_realized) if r.not_realized is not None else None,
            id=str(r.id),
        )
        # Применяем row-level overrides
        row_ov = overrides_by_row.get(str(r.id), {})
        if row_ov:
            if "leads"   in row_ov: er.leads   = row_ov["leads"]
            if "deals"   in row_ov: er.deals   = row_ov["deals"]
            if "revenue" in row_ov: er.revenue = row_ov["revenue"]
            if er.leads and er.leads > 0:
                er.conv = (er.deals or 0) / er.leads * 100
            if er.deals and er.deals > 0 and er.revenue:
                er.avg_check = er.revenue / er.deals
        enriched_rows.append(er)

    # Добавляем ручные источники как EnrichedRow
    for ms in manual_rows:
        leads, deals, revenue = ms["leads"], ms["deals"], ms["revenue"]
        conv = deals / leads * 100 if leads > 0 else None
        avg_check = revenue / deals if deals > 0 else None
        group_rules = await _get_group_rules(db)
        from backend.analytics.engine import detect_group
        er = EnrichedRow(
            src=ms["src"], campaign="", content="",
            leads=leads, deals=deals, revenue=revenue,
            new_leads=None, rep_leads=None, new_deals=None, rep_deals=None,
            rep_revenue=None, conv_excel=None, avg_check_excel=None,
            pct_rep_leads=None, pct_rep_deals=None,
            row_number=0, raw_data={},
            status="VALID", issues=[],
            group_name=detect_group(ms["src"], group_rules),
            conv_calc=conv, conv=conv, avg_check=avg_check,
        )
        er.is_manual = True
        enriched_rows.append(er)

    # Totals объект
    from backend.analytics.engine import Totals as TotalsObj
    t_obj = TotalsObj(
        leads         = float(totals.leads or 0)         if totals else 0,
        new_leads     = float(totals.new_leads or 0)     if totals else None,
        rep_leads     = float(totals.rep_leads or 0)     if totals else None,
        deals         = float(totals.deals or 0)         if totals else 0,
        new_deals     = float(totals.new_deals or 0)     if totals else None,
        rep_deals     = float(totals.rep_deals or 0)     if totals else None,
        revenue       = float(totals.revenue or 0)       if totals else 0,
        rep_revenue   = float(totals.rep_revenue or 0)   if totals else None,
        conv          = float(totals.conv or 0)          if totals else None,
        avg_check     = float(totals.avg_check or 0)     if totals else None,
        pct_rep_leads = float(totals.pct_rep_leads or 0) if totals else None,
        pct_rep_deals = float(totals.pct_rep_deals or 0) if totals else None,
        not_realized  = float(totals.not_realized) if (totals and totals.not_realized is not None) else None,
    )

    # ── ВЫЧИСЛЯЕМ ВСЮ АНАЛИТИКУ ──────────────────────────────────────

    kpi = compute_kpi(t_obj, overrides_itogo, manual_rows, plan, company_revenue)

    grouped = compute_grouped_sources(enriched_rows, t_obj, overrides_by_row)

    abc     = compute_abc(grouped)

    pareto  = compute_pareto(grouped)

    anomalies = compute_anomalies(enriched_rows, t_obj)

    insights  = compute_insights(grouped, kpi, conv_good, conv_ok)

    # Качество данных
    quality_report = {
        "total":   len(enriched_rows),
        "valid":   sum(1 for r in enriched_rows if r.status == "VALID"),
        "warning": sum(1 for r in enriched_rows if r.status == "WARNING"),
        "invalid": sum(1 for r in enriched_rows if r.status == "INVALID"),
        "issues":  [
            {"src": r.src, "issues": r.issues}
            for r in enriched_rows if r.issues
        ][:50],  # лимит
    }

    cache_data = {
        "kpi":             kpi,
        "grouped_sources": grouped,
        "abc":             abc,
        "pareto":          pareto,
        "anomalies":       anomalies,
        "insights":        insights,
        "quality_report":  quality_report,
    }

    # Сохраняем в analytics_cache
    existing = await db.execute(
        select(AnalyticsCache).where(AnalyticsCache.session_id == sess_uuid)
    )
    cache = existing.scalar_one_or_none()
    if cache:
        cache.kpi             = kpi
        cache.grouped_sources = grouped
        cache.abc             = abc
        cache.pareto          = pareto
        cache.anomalies       = anomalies
        cache.insights        = insights
        cache.quality_report  = quality_report
        cache.cache_version   = cache_ver
    else:
        cache = AnalyticsCache(
            session_id      = sess_uuid,
            cache_version   = cache_ver,
            kpi             = kpi,
            grouped_sources = grouped,
            abc             = abc,
            pareto          = pareto,
            anomalies       = anomalies,
            insights        = insights,
            quality_report  = quality_report,
        )
        db.add(cache)

    await db.commit()
    return cache_data


@router.get("/analytics/{session_id}")
async def get_analytics(session_id: str, db: AsyncSession = Depends(get_db)):
    """
    Возвращает готовую аналитику.
    Если кэш актуален — из кэша. Иначе пересчитывает.
    """
    try:
        sess_uuid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверный session_id")

    # Проверяем кэш
    cache_result = await db.execute(
        select(AnalyticsCache).where(AnalyticsCache.session_id == sess_uuid)
    )
    cache = cache_result.scalar_one_or_none()

    settings = await _get_settings(db)
    current_ver = int(settings.get("cache_version", 1))

    if cache and cache.cache_version == current_ver:
        return {
            "session_id":    session_id,
            "from_cache":    True,
            "computed_at":   cache.computed_at.isoformat() if cache.computed_at else None,
            "kpi":           cache.kpi,
            "grouped_sources": cache.grouped_sources,
            "abc":           cache.abc,
            "pareto":        cache.pareto,
            "anomalies":     cache.anomalies,
            "insights":      cache.insights,
            "quality_report": cache.quality_report,
        }

    # Кэш устарел или отсутствует — пересчитываем
    data = await compute_and_cache(session_id, db)
    return {"session_id": session_id, "from_cache": False, **data}


@router.post("/analytics/{session_id}/recalculate")
async def recalculate(session_id: str, db: AsyncSession = Depends(get_db)):
    """Принудительный пересчёт аналитики (после override, manual_source и т.д.)"""
    data = await compute_and_cache(session_id, db)
    return {"session_id": session_id, "recalculated": True, **data}


@router.get("/analytics/{session_id}/source-rows")
async def get_source_rows(
    session_id: str,
    status: str = None,  # VALID, WARNING, INVALID или None=все
    db: AsyncSession = Depends(get_db)
):
    """Строки источников для таблицы"""
    try:
        sess_uuid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверный session_id")

    query = select(SourceRow).where(SourceRow.session_id == sess_uuid)
    if status:
        query = query.where(SourceRow.status == status.upper())

    result = await db.execute(query)
    rows = result.scalars().all()

    return [
        {
            "id":          str(r.id),
            "src":         r.src,
            "campaign":    r.campaign,
            "group_name":  r.group_name,
            "leads":       float(r.leads or 0),
            "new_leads":   float(r.new_leads or 0) if r.new_leads else None,
            "rep_leads":   float(r.rep_leads or 0) if r.rep_leads else None,
            "deals":       float(r.deals or 0),
            "new_deals":   float(r.new_deals or 0) if r.new_deals else None,
            "rep_deals":   float(r.rep_deals or 0) if r.rep_deals else None,
            "revenue":     float(r.revenue or 0),
            "rep_revenue": float(r.rep_revenue or 0) if r.rep_revenue else None,
            "conv":        float(r.conv) if r.conv is not None else None,
            "avg_check":   float(r.avg_check) if r.avg_check else None,
            "pct_rep_deals": float(r.pct_rep_deals) if r.pct_rep_deals else None,
            "not_realized": float(r.not_realized) if r.not_realized is not None else None,
            "in_progress": (
                max(0.0, float(r.leads or 0) - float(r.deals or 0) - float(r.not_realized))
                if r.not_realized is not None else None
            ),
            "status":      r.status,
            "issues":      r.issues or [],
            "is_manual":   r.is_manual,
        }
        for r in rows
    ]


@router.get("/forecast")
async def get_forecast(
    manager_id: str = None,
    db: AsyncSession = Depends(get_db)
):
    """Прогноз на основе нескольких периодов"""
    from backend.models.models import PeriodTarget

    mgr_uuid = uuid.UUID(manager_id) if manager_id else uuid.UUID("00000000-0000-0000-0000-000000000001")

    sessions_result = await db.execute(
        select(Session, SessionTotals, PeriodTarget)
        .join(SessionTotals, SessionTotals.session_id == Session.id, isouter=True)
        .join(PeriodTarget, PeriodTarget.period == Session.period, isouter=True)
        .where(Session.manager_id == mgr_uuid)
        .order_by(Session.period_year, Session.period_month)
    )

    sessions_data = []
    for sess, totals, pt in sessions_result.all():
        sessions_data.append({
            "period":           sess.period,
            "period_year":      sess.period_year,
            "period_month":     sess.period_month,
            "leads":            float(totals.leads or 0) if totals else None,
            "deals":            float(totals.deals or 0) if totals else None,
            "revenue":          float(totals.revenue or 0) if totals else None,
            "conv":             float(totals.conv or 0) if totals else None,
            "company_revenue":  float(pt.company_revenue or 0) if pt else None,
            "plan":             float(pt.plan or 0) if pt else None,
        })

    if len(sessions_data) < 2:
        return {"error": "Нужно минимум 2 периода для прогноза", "data": sessions_data}

    return compute_forecast(sessions_data)
