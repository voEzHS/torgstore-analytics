"""
«Командный центр» — общая логика для рейтинга менеджеров и досье менеджера.

ВАЖНО: этот модуль намеренно повторяет формулы, которые уже существуют на
фронтенде (frontend/index.html, renderCompare() и соседние функции —
computeCompositeScore, explainRank, mgrRecommendation, calcLeadLossByManager).
Числа здесь должны совпадать с тем, что видно на вкладке «Сравнение» — если
формулы разъедутся, у руководителя на разных экранах будут разные цифры по
одному и тому же менеджеру за один и тот же период, что хуже, чем не иметь
новый раздел вообще.

Источники данных (по существующим таблицам, ничего заново не считаем):
- SessionTotals            — leads/deals/revenue(реклама)/conv/avg_check по сессии
- InvoiceStats              — накладные (реальные деньги по кассе), может отсутствовать
- AnalyticsCache.grouped_sources / insights / anomalies — уже посчитанный бэкендом
  разрез по источникам конкретной сессии (compute_and_cache в analytics.py)
- PeriodTarget с period='mgr:<uuid>:<период>' — план менеджера (тот же хак,
  которым уже пользуется фронтенд — см. period_targets.py)

Статус (good/watch/risk) — НОВАЯ логика для Фазы 1 «Командного центра»,
её на сайте раньше не было. Дизайн: сочетание тренда конверсии (к прошлому
периоду) + % выполнения плана + наличия аномалий уровня "danger" в
AnalyticsCache.anomalies — специально НЕ только по выручке, как просили в ТЗ.

Отчёт про рейтинг мест (rank-move: ▲/▼), серии побед и рекорды — Фаза 2,
здесь сознательно не реализовано (см. ТЗ «Командный центр»).
"""
from __future__ import annotations

from statistics import mean
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.models import (
    Manager, Session, SessionTotals, InvoiceStats, PeriodTarget, AnalyticsCache,
)
from backend.routers.imports import parse_period, DEPT_MANAGER_ID

MONTH_NAMES_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}


def prev_period_str(period: str) -> Optional[str]:
    """'Июль 2026' -> 'Июнь 2026'. None если период не парсится."""
    try:
        year, month = parse_period(period)
    except ValueError:
        return None
    if month == 1:
        year, month = year - 1, 12
    else:
        month -= 1
    return f"{MONTH_NAMES_RU[month]} {year}"


# ── Низкоуровневые запросы ──────────────────────────────────────────────────

async def _active_managers(db: AsyncSession) -> list[Manager]:
    result = await db.execute(
        select(Manager).where(Manager.is_active == True, Manager.id != DEPT_MANAGER_ID)
    )
    return list(result.scalars().all())


async def _session_with_totals(db: AsyncSession, manager_id, period: str):
    """Возвращает (Session, SessionTotals) или (None, None)."""
    result = await db.execute(
        select(Session, SessionTotals)
        .join(SessionTotals, SessionTotals.session_id == Session.id, isouter=True)
        .where(Session.manager_id == manager_id, Session.period == period)
    )
    row = result.first()
    if not row:
        return None, None
    return row[0], row[1]


async def _invoice_net(db: AsyncSession, manager_id, period: str) -> Optional[float]:
    """Сумма net_revenue по всем каналам/городам менеджера за период.
    None — если по менеджеру за период вообще нет накладных (тогда на сайте
    используется рекламная выручка как база, см. totalRevenue ниже)."""
    result = await db.execute(
        select(func.count(InvoiceStats.id), func.coalesce(func.sum(InvoiceStats.net_revenue), 0))
        .where(InvoiceStats.manager_id == manager_id, InvoiceStats.period == period)
    )
    cnt, total = result.one()
    if not cnt:
        return None
    return float(total or 0)


async def _mgr_plan(db: AsyncSession, manager_id, period: str) -> float:
    """План менеджера: сначала конкретный период ('mgr:<id>:<период>'),
    иначе дефолтный ('mgr:<id>:default'), иначе 0 — как на фронтенде
    (mgrPlans[id][period] || mgrPlans[id]['default'] || 0)."""
    specific = f"mgr:{manager_id}:{period}"
    default = f"mgr:{manager_id}:default"
    result = await db.execute(
        select(PeriodTarget.period, PeriodTarget.plan)
        .where(PeriodTarget.period.in_([specific, default]))
    )
    rows = {p: float(v or 0) for p, v in result.all()}
    if specific in rows and rows[specific]:
        return rows[specific]
    return rows.get(default, 0.0)


async def _analytics_cache(db: AsyncSession, session_id) -> Optional[AnalyticsCache]:
    """Читает готовый AnalyticsCache. Если его ещё нет — считает (как это уже
    делает GET /analytics/{id}), но не трогает cache_version-инвалидацию —
    для целей рейтинга/досье устаревший на 1 версию кэш не критичен, важно
    чтобы insights/grouped_sources/anomalies вообще были посчитаны хоть раз."""
    result = await db.execute(
        select(AnalyticsCache).where(AnalyticsCache.session_id == session_id)
    )
    cache = result.scalar_one_or_none()
    if cache:
        return cache
    from backend.routers.analytics import compute_and_cache
    await compute_and_cache(str(session_id), db)
    result = await db.execute(
        select(AnalyticsCache).where(AnalyticsCache.session_id == session_id)
    )
    return result.scalar_one_or_none()


# ── Сборка данных по одному менеджеру за период (без кросс-менеджерских полей) ──

async def _manager_raw(db: AsyncSession, mgr: Manager, period: str) -> Optional[dict]:
    sess, totals = await _session_with_totals(db, mgr.id, period)
    if not sess or not totals:
        return None

    ad_revenue = float(totals.revenue or 0)
    conv = float(totals.conv) if totals.conv is not None else None
    avg_check = float(totals.avg_check or 0)
    leads = float(totals.leads or 0)
    deals = float(totals.deals or 0)

    inv_net = await _invoice_net(db, mgr.id, period)
    has_invoice = inv_net is not None
    total_revenue = inv_net if has_invoice else ad_revenue

    plan = await _mgr_plan(db, mgr.id, period)
    plan_pct = (total_revenue / plan * 100) if plan else None

    cache = await _analytics_cache(db, sess.id)
    grouped = (cache.grouped_sources if cache else None) or []
    anomalies = (cache.anomalies if cache else None) or []
    insights = (cache.insights if cache else None) or []

    return {
        "id": str(mgr.id),
        "name": mgr.name,
        "color": mgr.color or "#2563EB",
        "session_id": str(sess.id),
        "leads": leads,
        "deals": deals,
        "revenue": ad_revenue,          # только реклама — см. предупреждение вверху файла
        "conv": conv,
        "avgCheck": avg_check,
        "hasInvoice": has_invoice,
        "totalRevenue": total_revenue,
        "plan": plan,
        "planPct": plan_pct,
        "grouped": grouped,
        "anomalies": anomalies,
        "insights": insights,
    }


# ── Кросс-менеджерский анализ (портирован дословно из computeCompositeScore /
#    mgrAnalysis-блока в frontend/index.html) ────────────────────────────────

def _dept_context(managers_raw: list[dict]) -> dict:
    n = len(managers_raw)
    avg_ad_rev_per_mgr = mean(m["revenue"] for m in managers_raw) if n else 0.0
    valid_conv = [m for m in managers_raw if m["conv"] is not None and m["conv"] <= 100]
    avg_conv = mean(m["conv"] for m in valid_conv) if valid_conv else 0.0
    dept_avg_check = mean(m["avgCheck"] for m in managers_raw) if n else 0.0
    total_leads_all = sum(m["leads"] for m in managers_raw)

    dept_conv_by_src: dict[str, dict] = {}
    for m in managers_raw:
        for g in m["grouped"]:
            name = g.get("group") or "Другое"
            d = dept_conv_by_src.setdefault(name, {"leads": 0.0, "deals": 0.0})
            d["leads"] += float(g.get("leads") or 0)
            d["deals"] += float(g.get("deals") or 0)

    return {
        "n": n,
        "avgAdRevPerMgr": avg_ad_rev_per_mgr,
        "avgConv": avg_conv,
        "deptAvgCheck": dept_avg_check,
        "totalLeadsAll": total_leads_all,
        "deptConvBySrc": dept_conv_by_src,
        "validConvIds": {m["id"] for m in valid_conv},
    }


def _apply_mgr_analysis(m: dict, ctx: dict) -> None:
    """gapDeals/gapRevenue/leadSkewPct — дословно как mgrAnalysis[m.id] на фронте."""
    exp_leads = exp_deals = 0.0
    for g in m["grouped"]:
        name = g.get("group") or "Другое"
        d = ctx["deptConvBySrc"].get(name)
        g_leads = float(g.get("leads") or 0)
        if d and d["leads"] > 0:
            src_conv = d["deals"] / d["leads"]
            exp_deals += g_leads * src_conv
            exp_leads += g_leads
    expected_conv = (exp_deals / exp_leads * 100) if exp_leads else None

    avg_conv = ctx["avgConv"]
    if avg_conv and m["conv"] is not None and m["conv"] <= 100 and m["conv"] < avg_conv:
        gap_deals = (avg_conv - m["conv"]) / 100 * m["leads"]
    else:
        gap_deals = 0.0
    gap_revenue = gap_deals * (m["avgCheck"] or ctx["deptAvgCheck"] or 0)

    total_leads_all = ctx["totalLeadsAll"]
    lead_share = (m["leads"] / total_leads_all * 100) if total_leads_all else 0.0
    expected_share = (100 / ctx["n"]) if ctx["n"] else 0.0
    lead_skew_pct = ((lead_share - expected_share) / expected_share * 100) if expected_share else 0.0

    m["expectedConv"] = expected_conv
    m["gapDeals"] = gap_deals
    m["gapRevenue"] = gap_revenue
    m["leadShare"] = lead_share
    m["leadSkewPct"] = lead_skew_pct


def _apply_composite(managers_raw: list[dict], ctx: dict) -> None:
    n = ctx["n"]
    by_rev = sorted(managers_raw, key=lambda x: -x["totalRevenue"])
    rev_rank_of = {m["id"]: i for i, m in enumerate(by_rev)}

    valid_conv = [m for m in managers_raw if m["id"] in ctx["validConvIds"]]
    by_conv = sorted(valid_conv, key=lambda x: -x["conv"])
    conv_rank_of = {m["id"]: i for i, m in enumerate(by_conv)}

    for m in managers_raw:
        rev_rank = rev_rank_of[m["id"]]
        rev_pct = ((n - 1 - rev_rank) / (n - 1) * 100) if n > 1 else 100.0

        if m["id"] in conv_rank_of:
            cn = len(by_conv)
            conv_rank = conv_rank_of[m["id"]]
            conv_pct = ((cn - 1 - conv_rank) / (cn - 1) * 100) if cn > 1 else 100.0
        else:
            conv_pct = 50.0

        avg_ad = ctx["avgAdRevPerMgr"]
        risk_pct = min(100.0, m["gapRevenue"] / avg_ad * 100) if (m["gapRevenue"] > 0 and avg_ad) else 0.0

        composite = round(0.4 * conv_pct + 0.4 * rev_pct + 0.2 * (100 - risk_pct))
        composite = max(0, min(100, composite))

        m["revPct"] = rev_pct
        m["convPct"] = conv_pct
        m["riskPct"] = risk_pct
        m["composite"] = composite
        m["revRank"] = rev_rank + 1


# ── Статус good/watch/risk (новая логика Фазы 1, см. докстринг файла) ───────

def _status_and_reason(m: dict, conv_prev: Optional[float], leads_prev: Optional[float]) -> tuple[str, str]:
    conv_now = m["conv"]
    plan_pct = m["planPct"]
    anomalies = m["anomalies"] or []
    has_danger = any(a.get("cls") == "danger" for a in anomalies)
    has_any_anomaly = len(anomalies) > 0

    conv_delta_pp = None
    if conv_now is not None and conv_prev is not None:
        conv_delta_pp = conv_now - conv_prev

    steady_volume = True
    if leads_prev is not None and leads_prev > 0 and m["leads"] is not None:
        steady_volume = m["leads"] >= leads_prev * 0.85  # объём лидов не просел одновременно

    is_risk = (
        (plan_pct is not None and plan_pct < 70)
        or (conv_delta_pp is not None and conv_delta_pp <= -5)
        or has_danger
    )
    is_watch = (
        (plan_pct is not None and plan_pct < 100)
        or (conv_delta_pp is not None and conv_delta_pp < 0)
        or has_any_anomaly
    )

    if is_risk:
        status = "risk"
    elif is_watch:
        status = "watch"
    else:
        status = "good"

    # Формулировка — наблюдение, не приговор (см. ТЗ)
    if conv_delta_pp is not None and conv_delta_pp <= -5 and steady_volume:
        reason = f"конверсия просела на {abs(conv_delta_pp):.1f} п.п. при сохранении объёма лидов — стоит разобраться в причинах"
    elif conv_delta_pp is not None and conv_delta_pp <= -5:
        reason = f"конверсия просела на {abs(conv_delta_pp):.1f} п.п. к прошлому периоду"
    elif plan_pct is not None and plan_pct < 70:
        reason = f"выполнение плана {plan_pct:.0f}% — заметно отстаёт от графика"
    elif has_danger:
        danger_msgs = [a.get("msg") for a in anomalies if a.get("cls") == "danger" and a.get("msg")]
        reason = danger_msgs[0] if danger_msgs else "обнаружена аномалия в данных по источникам"
    elif plan_pct is not None and plan_pct < 100:
        reason = f"план выполнен на {plan_pct:.0f}% — пока не дотянуто"
    elif conv_delta_pp is not None and conv_delta_pp < 0:
        reason = f"конверсия немного снизилась ({conv_delta_pp:+.1f} п.п.) к прошлому периоду"
    elif has_any_anomaly:
        reason = "есть отклонения по отдельным источникам — детали в разборе по менеджеру"
    else:
        reason = "показатели на уровне отдела или выше, явных отклонений нет"

    return status, reason


# ── Публичная точка входа: снимок по всем менеджерам за период ─────────────

async def build_period_snapshot(db: AsyncSession, period: str) -> dict:
    managers = await _active_managers(db)
    managers_raw = []
    for mgr in managers:
        raw = await _manager_raw(db, mgr, period)
        if raw:
            managers_raw.append(raw)

    if not managers_raw:
        return {"period": period, "managers": [], "dept": None}

    ctx = _dept_context(managers_raw)
    for m in managers_raw:
        _apply_mgr_analysis(m, ctx)
    _apply_composite(managers_raw, ctx)

    prev_period = prev_period_str(period)
    mgr_by_id = {str(mgr.id): mgr for mgr in managers}
    for m in managers_raw:
        prev_raw = None
        if prev_period:
            prev_raw = await _manager_raw(db, mgr_by_id[m["id"]], prev_period)

        conv_prev = prev_raw["conv"] if prev_raw else None
        leads_prev = prev_raw["leads"] if prev_raw else None

        status, reason = _status_and_reason(m, conv_prev, leads_prev)
        m["status"] = status
        m["statusReason"] = reason
        m["convPrev"] = conv_prev

        m["revDeltaPct"] = (
            (m["totalRevenue"] - prev_raw["totalRevenue"]) / prev_raw["totalRevenue"] * 100
            if prev_raw and prev_raw["totalRevenue"] else None
        )
        m["avgCheckDeltaPct"] = (
            (m["avgCheck"] - prev_raw["avgCheck"]) / prev_raw["avgCheck"] * 100
            if prev_raw and prev_raw["avgCheck"] else None
        )

    managers_raw.sort(key=lambda x: -x["composite"])
    for i, m in enumerate(managers_raw):
        m["rank"] = i + 1

    return {
        "period": period,
        "prevPeriod": prev_period,
        "managers": managers_raw,
        "dept": {
            "avgConv": ctx["avgConv"],
            "avgAdRevPerMgr": ctx["avgAdRevPerMgr"],
            "deptAvgCheck": ctx["deptAvgCheck"],
            "n": ctx["n"],
        },
    }


# ── История менеджера по нескольким последним периодам (для спарклайна) ────

async def build_manager_history(db: AsyncSession, manager_id, limit: int = 6) -> list[dict]:
    result = await db.execute(
        select(Session.period, Session.period_year, Session.period_month)
        .where(Session.manager_id == manager_id)
        .order_by(Session.period_year, Session.period_month)
    )
    all_periods = [row[0] for row in result.all()]
    recent = all_periods[-limit:] if len(all_periods) > limit else all_periods

    history = []
    for period in recent:
        sess, totals = await _session_with_totals(db, manager_id, period)
        if not totals:
            continue
        ad_revenue = float(totals.revenue or 0)
        inv_net = await _invoice_net(db, manager_id, period)
        total_revenue = inv_net if inv_net is not None else ad_revenue
        plan = await _mgr_plan(db, manager_id, period)
        plan_pct = (total_revenue / plan * 100) if plan else None
        history.append({
            "period": period,
            "revenue": total_revenue,
            "conv": float(totals.conv) if totals.conv is not None else None,
            "planPct": plan_pct,
        })
    return history


# ── Сильные стороны / зоны роста для досье (список наблюдений, не 1 фраза) ──

def build_strengths_and_growth(m: dict, ctx_avg_conv: float, dept_avg_check: float) -> tuple[list[str], list[str]]:
    good: list[str] = []
    bad: list[str] = []

    # Сначала — уже посчитанные бэкендом инсайты этой сессии (compute_insights
    # в analytics/engine.py, те же ①②③ что видны на карточке сессии). Это и
    # есть «уже существующая логика insights», на которую просили опираться —
    # наши дополнительные наблюдения ниже её только дополняют.
    for ins in (m.get("insights") or []):
        text = ins.get("text") or ins.get("title") or ""
        if not text:
            continue
        if ins.get("cls") == "success":
            good.append(text)
        elif ins.get("cls") == "danger":
            bad.append(text)
        # cls == "info"/иное — намеренно не дублируем в good/bad, это
        # нейтральные инсайты, не сильная/слабая сторона.

    valid_rows = [
        g for g in m["grouped"]
        if g.get("conv") is not None and g["conv"] <= 100 and float(g.get("leads") or 0) >= 2
    ]
    if valid_rows:
        best = max(valid_rows, key=lambda g: g["conv"])
        good.append(
            f"Лучший источник — «{best.get('group','—')}»: конверсия {best['conv']:.1f}%, "
            f"выручка {best.get('revenue', 0):,.0f} ₸".replace(",", " ")
        )
        if len(valid_rows) > 1:
            worst = min(valid_rows, key=lambda g: g["conv"])
            if worst.get("group") != best.get("group"):
                bad.append(
                    f"Источник «{worst.get('group','—')}» просаживает конверсию: {worst['conv']:.1f}% "
                    f"при {worst.get('leads',0):.0f} лидах — стоит разобрать эти сделки"
                )

    avg_conv = ctx_avg_conv
    conv = m["conv"]
    if avg_conv and conv is not None and conv <= 100:
        if conv < avg_conv * 0.85:
            bad.append(
                f"Конверсия {conv:.1f}% против {avg_conv:.1f}% по отделу — "
                f"разобрать слитые лиды и скрипт звонков"
            )
        elif conv >= avg_conv:
            good.append(f"Конверсия выше среднего по отделу ({conv:.1f}% против {avg_conv:.1f}%)")

    lead_skew = m.get("leadSkewPct") or 0
    if lead_skew <= -30:
        bad.append(
            f"Получает на {abs(lead_skew):.0f}% лидов меньше среднего по отделу — "
            f"стоит проверить логику распределения лидов"
        )
    elif lead_skew >= 30 and conv is not None and avg_conv and conv < avg_conv:
        bad.append(
            f"Получает на {lead_skew:.0f}% лидов больше среднего при конверсии ниже отдела — возможна перегрузка"
        )

    if dept_avg_check and m["avgCheck"] and m["avgCheck"] < dept_avg_check * 0.85:
        bad.append(
            f"Средний чек {m['avgCheck']:,.0f} ₸ ниже отдела ({dept_avg_check:,.0f} ₸) — "
            f"есть потенциал в допродажах".replace(",", " ")
        )
    elif dept_avg_check and m["avgCheck"] and m["avgCheck"] > dept_avg_check * 1.1:
        good.append(f"Средний чек стабильно выше среднего по отделу")

    if m.get("planPct") is not None:
        if m["planPct"] >= 100:
            good.append(f"План выполнен на {m['planPct']:.0f}%")
        elif m["planPct"] < 70:
            bad.append(f"Выполнение плана {m['planPct']:.0f}% — под угрозой месячная норма")

    if not good:
        good.append("Стабильная работа без явных провалов по ключевым метрикам")
    if not bad:
        bad.append("Явных зон роста не выявлено — показатели на уровне отдела или выше")

    return good, bad


# ── Детерминированные (без LLM) дайджест/рекомендация ───────────────────────
# Раньше эти тексты собирал routers/ai.py через Anthropic API и карточка молча
# ломалась без ANTHROPIC_API_KEY («ИИ-дайджест недоступен...», «Рекомендация
# недоступна...»). Пользователь попросил убрать зависимость от API и выводить
# выводы без него. Ниже — та же структура сигналов, что раньше уходила в
# промпт (лидер / кто быстрее всех растёт / на кого обратить внимание —
# см. историю в routers/ai.py leaderboard_digest/manager_recommendation), но
# без обращения к LLM: просто собираем уже посчитанные факты в готовое
# предложение — не выдумываем ничего нового сверх того, что видно в rest этого
# же ответа (kpi/status/statusReason/strengths/growthAreas).

def build_leaderboard_digest(managers: list[dict]) -> Optional[str]:
    if not managers:
        return None
    best = max(managers, key=lambda m: m["composite"])
    with_delta = [m for m in managers if m.get("revDeltaPct") is not None]
    fastest = max(with_delta, key=lambda m: m["revDeltaPct"]) if with_delta else None
    risk_list = sorted([m for m in managers if m["status"] == "risk"], key=lambda m: m["composite"])
    watch_list = sorted([m for m in managers if m["status"] == "watch"], key=lambda m: m["composite"])
    attention = (risk_list or watch_list or [None])[0]

    parts = [f"Лидер рейтинга — {best['name']} (composite {best['composite']}/100)."]
    if fastest and fastest["id"] != best["id"]:
        parts.append(
            f"Быстрее всех растёт {fastest['name']}: выручка {fastest['revDeltaPct']:+.0f}% к прошлому периоду."
        )
    if attention:
        parts.append(f"В первую очередь стоит посмотреть {attention['name']}: {attention['statusReason']}.")
    return " ".join(parts)


def build_manager_recommendation(m: dict, good: list[str], bad: list[str]) -> str:
    status = m.get("status")
    reason = m.get("statusReason") or ""
    if status == "risk":
        head = f"Разобрать в первую очередь: {reason}." if reason else "Есть сигнал, который стоит разобрать в первую очередь."
    elif status == "watch":
        head = f"Стоит присмотреть: {reason}." if reason else "Пока не критично, но стоит присмотреть."
    else:
        head = "Явных отклонений нет — специальных действий не требуется."
    if bad and status != "good":
        head += f" Основная зона роста: {bad[0]}"
    return head
