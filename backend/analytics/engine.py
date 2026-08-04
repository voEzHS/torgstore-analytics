"""
Аналитический движок — все расчёты здесь, не на фронтенде.

СЕМАНТИЧЕСКИЙ КОНТРАКТ:
- conv хранится как % (0-100), например 13.39 = 13.39%
- conv:       deals / leads * 100 за период — та же цифра, что CRM возвращает в
              поле "conversion" ответа report/tree (лиды и сделки берутся по
              date_filter=created_at, т.е. уже корректно привязаны к одному и
              тому же когорту "лиды, поступившие в этот период"). НЕ путать с
              полем CRM "procent_of_new_sales" (new_sales/new_leads) — это другая
              метрика ("доля новых продаж"), а не конверсия, и на сайте нигде не
              показывается как "Конверсия".
- conv_excel: сырое значение "Конверсия"/"conversion" из Excel/CRM как есть
              (может быть >100 — ошибка CRM), используется только для сверки.
- conv_calc:  deals / leads * 100 — наш расчёт для сверки с conv_excel.
- avg_check:  тенге (revenue / deals)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import re
import math


# ── ТИПЫ ДАННЫХ ─────────────────────────────────────────────────────

@dataclass
class RawRow:
    """Строка из Excel до нормализации"""
    row_number: int
    data: dict


@dataclass
class NormalizedRow:
    """После нормализации: trim, убраны лишние пробелы"""
    src: str
    campaign: str
    content: str
    leads: Optional[float]
    new_leads: Optional[float]
    rep_leads: Optional[float]
    deals: Optional[float]
    new_deals: Optional[float]
    rep_deals: Optional[float]
    revenue: Optional[float]
    rep_revenue: Optional[float]
    conv_excel: Optional[float]
    avg_check_excel: Optional[float]
    pct_rep_leads: Optional[float]
    pct_rep_deals: Optional[float]
    not_realized: Optional[float] = None   # "Не реализовано" — финальный статус "закрыто, не реализовано"
    row_number: int = 0
    raw_data: dict = field(default_factory=dict)


@dataclass
class ValidatedRow(NormalizedRow):
    """После валидации: статус + список проблем"""
    status: str = "VALID"          # VALID | WARNING | INVALID
    issues: list = field(default_factory=list)


@dataclass
class EnrichedRow(ValidatedRow):
    """После обогащения: финальные расчётные поля"""
    group_name: str = ""
    conv_calc: Optional[float] = None
    conv: Optional[float] = None   # финальная конверсия
    avg_check: Optional[float] = None
    in_progress: Optional[float] = None  # лиды ещё в работе (не потеря!): leads - deals - not_realized
    id: Optional[str] = None  # UUID строки SourceRow (для overrides); None для ручных источников
    is_manual: bool = False  # ручной источник (добавлен вручную, не из CRM-выгрузки)


@dataclass
class Totals:
    """Строка ИТОГО из Excel — источник истины для KPI"""
    leads: Optional[float] = None
    new_leads: Optional[float] = None
    rep_leads: Optional[float] = None
    deals: Optional[float] = None
    new_deals: Optional[float] = None
    rep_deals: Optional[float] = None
    revenue: Optional[float] = None
    rep_revenue: Optional[float] = None
    conv: Optional[float] = None
    avg_check: Optional[float] = None
    pct_rep_leads: Optional[float] = None
    pct_rep_deals: Optional[float] = None
    not_realized: Optional[float] = None


# ── НОРМАЛИЗАЦИЯ ─────────────────────────────────────────────────────

def _num(val) -> Optional[float]:
    """Безопасное преобразование в число"""
    if val is None or val == "":
        return None
    try:
        f = float(val)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def _str(val) -> str:
    """Строка с нормализацией пробелов"""
    if val is None:
        return ""
    return re.sub(r"\s{2,}", " ", str(val).strip())


def normalize_row(raw: dict, row_number: int) -> NormalizedRow:
    """
    ЭТАП 1: Нормализация
    - Читает поля по имени заголовка (не по индексу)
    - trim + убирает двойные пробелы
    - Конвертирует числа безопасно
    """
    return NormalizedRow(
        src             = _str(raw.get("Источник")) or "",
        campaign        = _str(raw.get("Кампания")),
        content         = _str(raw.get("Контент")),
        leads           = _num(raw.get("Лиды")),
        new_leads       = _num(raw.get("Новые лиды")),
        rep_leads       = _num(raw.get("Повторные лиды")),
        deals           = _num(raw.get("Продажи")),
        new_deals       = _num(raw.get("Новые продажи")),
        rep_deals       = _num(raw.get("Повторные продажи")),
        revenue         = _num(raw.get("Выручка")),
        rep_revenue     = _num(raw.get("Выручка повторных продаж")),
        conv_excel      = _num(raw.get("Конверсия")),    # уже в %
        avg_check_excel = _num(raw.get("Средний чек")),
        pct_rep_leads   = _num(raw.get("% повторных лидов")),
        pct_rep_deals   = _num(raw.get("% повторных продаж")),
        not_realized    = _num(raw.get("Не реализовано")),
        row_number      = row_number,
        raw_data        = raw,
    )


def normalize_totals(raw: dict) -> Totals:
    """Нормализация строки ИТОГО"""
    t = Totals(
        leads         = _num(raw.get("Лиды")),
        new_leads     = _num(raw.get("Новые лиды")),
        rep_leads     = _num(raw.get("Повторные лиды")),
        deals         = _num(raw.get("Продажи")),
        new_deals     = _num(raw.get("Новые продажи")),
        rep_deals     = _num(raw.get("Повторные продажи")),
        revenue       = _num(raw.get("Выручка")),
        rep_revenue   = _num(raw.get("Выручка повторных продаж")),
        conv          = _num(raw.get("Конверсия")),
        avg_check     = _num(raw.get("Средний чек")),
        pct_rep_leads = _num(raw.get("% повторных лидов")),
        pct_rep_deals = _num(raw.get("% повторных продаж")),
        not_realized  = _num(raw.get("Не реализовано")),
    )
    # Fallback: avg_check = revenue/deals
    if t.avg_check is None and t.revenue and t.deals and t.deals > 0:
        t.avg_check = t.revenue / t.deals
    # Fallback: pct_rep_deals = rep_deals/deals*100
    if t.pct_rep_deals is None and t.rep_deals is not None and t.deals and t.deals > 0:
        t.pct_rep_deals = t.rep_deals / t.deals * 100
    if t.pct_rep_leads is None and t.rep_leads is not None and t.leads and t.leads > 0:
        t.pct_rep_leads = t.rep_leads / t.leads * 100
    return t


# ── ВАЛИДАЦИЯ ────────────────────────────────────────────────────────

VALIDATION_RULES = [
    # (code, severity, check_fn, message_fn)
    ("DEALS_GT_LEADS",   "INVALID",
     lambda r: r.deals is not None and r.leads is not None and r.deals > r.leads,
     lambda r: f"Продаж ({r.deals}) > Лидов ({r.leads})"),

    ("CONV_GT_100",      "INVALID",
     lambda r: r.conv_excel is not None and r.conv_excel > 100,
     lambda r: f"Конверсия из Excel ({r.conv_excel:.2f}%) > 100% — ошибка CRM"),

    ("NEGATIVE_LEADS",   "INVALID",
     lambda r: r.leads is not None and r.leads < 0,
     lambda r: "Лиды < 0"),

    ("NEGATIVE_DEALS",   "INVALID",
     lambda r: r.deals is not None and r.deals < 0,
     lambda r: "Продажи < 0"),

    ("NEGATIVE_REVENUE", "INVALID",
     lambda r: r.revenue is not None and r.revenue < 0,
     lambda r: "Выручка < 0"),

    ("REP_GT_DEALS",     "WARNING",
     lambda r: r.rep_deals is not None and r.deals is not None and r.rep_deals > r.deals,
     lambda r: f"Повт. продаж ({r.rep_deals}) > Продаж ({r.deals})"),

    ("REP_REV_GT_REV",   "WARNING",
     lambda r: r.rep_revenue is not None and r.revenue is not None and r.rep_revenue > r.revenue,
     lambda r: "Повторная выручка > Общей выручки"),

    ("NEW_GT_DEALS",     "WARNING",
     lambda r: r.new_deals is not None and r.deals is not None and r.new_deals > r.deals,
     lambda r: f"Новых продаж ({r.new_deals}) > Продаж ({r.deals})"),

    ("NEW_GT_LEADS",     "WARNING",
     lambda r: r.new_leads is not None and r.leads is not None and r.new_leads > r.leads,
     lambda r: f"Новых лидов ({r.new_leads}) > Лидов ({r.leads})"),
]


def validate_row(row: NormalizedRow) -> ValidatedRow:
    """
    ЭТАП 2: Валидация
    Проверяет бизнес-правила, присваивает статус VALID/WARNING/INVALID
    """
    issues = []
    status = "VALID"

    for code, severity, check, msg_fn in VALIDATION_RULES:
        if check(row):
            issue = {"code": code, "severity": severity, "msg": msg_fn(row)}
            issues.append(issue)
            if severity == "INVALID":
                status = "INVALID"
            elif severity == "WARNING" and status == "VALID":
                status = "WARNING"

    return ValidatedRow(**{
        **row.__dict__,
        "status": status,
        "issues": issues,
    })


# ── ГРУППИРОВКА ИСТОЧНИКОВ ───────────────────────────────────────────

def detect_group(src: str, rules: list[dict]) -> str:
    """
    ЭТАП 3: Группировка по правилам из БД (не хардкод!)
    rules: список {group_name, match_type, pattern, aliases, priority}
    Сортировка по priority (ASC) — меньше = выше приоритет
    """
    src_lower = src.lower()
    for rule in sorted(rules, key=lambda r: r.get("priority", 999)):
        if not rule.get("is_active", True):
            continue
        match_type = rule.get("match_type", "contains")
        aliases    = rule.get("aliases") or []
        pattern    = rule.get("pattern") or ""
        group      = rule["group_name"]

        if match_type == "alias":
            if src_lower in [a.lower() for a in aliases] or src in aliases:
                return group

        elif match_type == "regex":
            # Проверяем aliases точно, потом pattern как regex
            if aliases and src_lower in [a.lower() for a in aliases]:
                return group
            if pattern and re.search(pattern, src_lower):
                return group

        elif match_type == "prefix":
            if aliases and any(src_lower.startswith(a.lower()) for a in aliases):
                return group
            if pattern and src_lower.startswith(pattern.lower()):
                return group

        elif match_type == "contains":
            if aliases and any(a.lower() in src_lower for a in aliases):
                return group
            if pattern and pattern.lower() in src_lower:
                return group

    return "Другое"


# ── ОБОГАЩЕНИЕ ──────────────────────────────────────────────────────

def enrich_row(row: ValidatedRow, group_rules: list[dict]) -> EnrichedRow:
    """
    ЭТАП 4: Обогащение
    - Вычисляет conv_calc, conv, avg_check
    - Определяет группу источника
    - Добавляет fallback pct_rep
    """
    # Конверсия: conv в % (0-100)
    conv_calc = None
    if row.leads and row.leads > 0 and row.deals is not None:
        conv_calc = row.deals / row.leads * 100

    # Финальная конверсия: берём из Excel если валидна
    if row.conv_excel is not None:
        conv = row.conv_excel   # берём как есть (INVALID строки уже отфильтрованы)
    else:
        conv = conv_calc        # fallback: наш расчёт

    # Проверка расхождения conv_excel vs conv_calc
    issues = list(row.issues)
    if (row.conv_excel is not None and conv_calc is not None
            and abs(row.conv_excel - conv_calc) > 0.5
            and row.status != "INVALID"):
        issues.append({
            "code": "CONV_MISMATCH",
            "severity": "WARNING",
            "msg": f"Конверсия Excel {row.conv_excel:.2f}% ≠ расчётная {conv_calc:.2f}% "
                   f"(разница {abs(row.conv_excel - conv_calc):.2f}%)"
        })

    # Средний чек
    avg_check = row.avg_check_excel
    if avg_check is None or avg_check <= 0:
        if row.revenue and row.deals and row.deals > 0:
            avg_check = row.revenue / row.deals

    # Fallback pct_rep
    pct_rep_deals = row.pct_rep_deals
    if pct_rep_deals is None and row.rep_deals is not None and row.deals and row.deals > 0:
        pct_rep_deals = row.rep_deals / row.deals * 100
    pct_rep_leads = row.pct_rep_leads
    if pct_rep_leads is None and row.rep_leads is not None and row.leads and row.leads > 0:
        pct_rep_leads = row.rep_leads / row.leads * 100

    # "В работе" — лиды ещё не закрыты ни продажей, ни отказом (НЕ потеря).
    # Считается только если в выгрузке реально есть поле "Не реализовано" для этой строки —
    # иначе (старые периоды) оставляем None и фронтенд честно показывает старую грубую оценку.
    in_progress = None
    if row.leads is not None and row.deals is not None and row.not_realized is not None:
        in_progress = max(0.0, row.leads - row.deals - row.not_realized)

    return EnrichedRow(**{
        **row.__dict__,
        "issues":         issues,
        "group_name":     detect_group(row.src, group_rules),
        "conv_calc":      conv_calc,
        "conv":           conv,
        "avg_check":      avg_check,
        "pct_rep_deals":  pct_rep_deals,
        "pct_rep_leads":  pct_rep_leads,
        "in_progress":    in_progress,
    })


# ── АНАЛИТИКА — всё считается здесь ────────────────────────────────

def compute_kpi(totals: Totals, overrides: dict, manual_rows: list,
                period_plan: float, company_revenue: float) -> dict:
    """
    Вычисляет KPI с учётом overrides и ручных источников.
    overrides: {field: value} для 'itogo'
    """
    # Применяем overrides к totals
    t = totals.__dict__.copy()
    for field, val in overrides.items():
        t[field] = val

    # Пересчёт производных после override
    if t["leads"] and t["leads"] > 0 and t["deals"] is not None:
        t["conv"] = t["deals"] / t["leads"] * 100
    if t["deals"] and t["deals"] > 0 and t["revenue"] is not None:
        t["avg_check"] = t["revenue"] / t["deals"]

    # Добавляем ручные источники
    for ms in manual_rows:
        t["leads"]   = (t["leads"]   or 0) + (ms.get("leads") or 0)
        t["deals"]   = (t["deals"]   or 0) + (ms.get("deals") or 0)
        t["revenue"] = (t["revenue"] or 0) + (ms.get("revenue") or 0)
    # Пересчёт conv после manual
    if manual_rows and t["leads"] and t["leads"] > 0:
        t["conv"] = (t["deals"] or 0) / t["leads"] * 100

    # Производные
    leads  = t.get("leads") or 0
    deals  = t.get("deals") or 0
    revenue = t.get("revenue") or 0
    slitted = leads - deals
    slitted_pct = slitted / leads * 100 if leads > 0 else None

    # Точный разбор "слито": сколько реально проиграно (not_realized из CRM),
    # а сколько ещё просто в работе у менеджера — это НЕ потеря.
    not_realized = t.get("not_realized")
    has_lost_detail = not_realized is not None
    in_progress = max(0.0, leads - deals - not_realized) if has_lost_detail else None
    lost_confirmed = not_realized if has_lost_detail else None
    lost_confirmed_pct = (lost_confirmed / leads * 100) if has_lost_detail and leads > 0 else None
    plan_pct_ad = revenue / period_plan * 100 if period_plan else None
    plan_pct_co = company_revenue / period_plan * 100 if period_plan and company_revenue else None
    outside_revenue = (company_revenue - revenue) if company_revenue else None

    return {
        **t,
        "slitted":          slitted,
        "slitted_pct":      slitted_pct,
        "has_lost_detail":  has_lost_detail,
        "lost_confirmed":   lost_confirmed,
        "lost_confirmed_pct": lost_confirmed_pct,
        "in_progress":      in_progress,
        "plan":             period_plan,
        "company_revenue":  company_revenue,
        "plan_pct_adv":     plan_pct_ad,     # % плана от рекламной выручки
        "plan_pct_company": plan_pct_co,     # % плана от общей выручки
        "outside_revenue":  outside_revenue, # выручка вне рекламного отчёта
        "revenue_per_lead": revenue / leads if leads > 0 else None,
    }


def compute_grouped_sources(rows: list[EnrichedRow],
                            totals: Totals,
                            overrides_by_row: dict) -> list[dict]:
    """
    Группировка источников с агрегацией.
    conv группы = sum(deals) / sum(leads) * 100 (не среднее конверсий!)
    """
    groups: dict[str, dict] = {}
    total_rev   = (totals.revenue or 0)
    total_leads = (totals.leads or 0)
    total_deals = (totals.deals or 0)

    for row in rows:
        if row.status == "INVALID":
            continue

        # Применяем overrides к строке
        r_leads   = overrides_by_row.get(str(row.id), {}).get("leads",   row.leads   or 0)
        r_deals   = overrides_by_row.get(str(row.id), {}).get("deals",   row.deals   or 0)
        r_revenue = overrides_by_row.get(str(row.id), {}).get("revenue", row.revenue or 0)

        g = row.group_name or "Другое"
        if g not in groups:
            groups[g] = {
                "group": g, "leads": 0, "deals": 0, "revenue": 0,
                "rep_revenue": 0, "rep_deals": 0, "new_deals": 0,
                "new_leads": 0, "rep_leads": 0, "not_realized": 0,
                "has_lost_detail": False, "children": []
            }
        gr = groups[g]
        gr["leads"]       += r_leads or 0
        gr["deals"]       += r_deals or 0
        gr["revenue"]     += r_revenue or 0
        gr["rep_revenue"] += row.rep_revenue or 0
        gr["rep_deals"]   += row.rep_deals or 0
        gr["new_deals"]   += row.new_deals or 0
        gr["new_leads"]   += row.new_leads or 0
        gr["rep_leads"]   += row.rep_leads or 0
        if row.not_realized is not None:
            gr["not_realized"]    += row.not_realized
            gr["has_lost_detail"] = True
        gr["children"].append({
            "id":         str(row.id),
            "src":        row.src,
            "campaign":   row.campaign,
            "status":     row.status,
            "leads":      r_leads,
            "deals":      r_deals,
            "revenue":    r_revenue,
            "conv":       row.conv,
            "avg_check":  row.avg_check,
            "rep_deals":  row.rep_deals,
            "rep_revenue": row.rep_revenue,
            "issues":     row.issues,
            "is_manual":  row.is_manual,
        })

    result = []
    for g, gr in groups.items():
        leads, deals, rev = gr["leads"], gr["deals"], gr["revenue"]
        conv      = deals / leads * 100 if leads > 0 else None
        avg_check = rev / deals if deals > 0 else None
        pct_rep   = gr["rep_deals"] / deals * 100 if deals > 0 else None
        slitted   = leads - deals
        has_lost_detail = gr["has_lost_detail"]
        lost_confirmed = gr["not_realized"] if has_lost_detail else None
        in_progress    = max(0.0, leads - deals - gr["not_realized"]) if has_lost_detail else None
        result.append({
            **gr,
            "conv":           conv,
            "avg_check":      avg_check,
            "pct_rep_deals":  pct_rep,
            "slitted":        slitted,
            "lost_confirmed": lost_confirmed,
            "in_progress":    in_progress,
            "share_revenue":  rev / total_rev * 100      if total_rev > 0  else None,
            "share_leads":    leads / total_leads * 100  if total_leads > 0 else None,
            "share_deals":    deals / total_deals * 100  if total_deals > 0 else None,
            # "Потенциальные потери" теперь считаются по реально проигранным лидам,
            # если есть детализация; иначе — старая грубая оценка (leads-deals) для обратной совместимости.
            "potential_loss": (lost_confirmed if has_lost_detail else slitted) * (avg_check or 0),
        })

    return sorted(result, key=lambda x: x["revenue"] or 0, reverse=True)


def compute_abc(grouped: list[dict]) -> list[dict]:
    """ABC-анализ: A=80% выручки, B=15%, C=5%"""
    srcs = [g for g in grouped if (g.get("revenue") or 0) > 0]
    srcs.sort(key=lambda x: x["revenue"] or 0, reverse=True)
    total = sum(g["revenue"] or 0 for g in srcs)
    if total == 0:
        return []
    cum = 0
    result = []
    for g in srcs:
        cum += g["revenue"] or 0
        pct = cum / total * 100
        abc = "A" if pct <= 80 else ("B" if pct <= 95 else "C")
        result.append({**g, "abc": abc, "share_pct": (g["revenue"] or 0) / total * 100})
    return result


def compute_pareto(grouped: list[dict]) -> dict:
    """Pareto: какие 20% источников дают 80% выручки"""
    srcs = [g for g in grouped if (g.get("revenue") or 0) > 0]
    srcs.sort(key=lambda x: x["revenue"] or 0, reverse=True)
    total = sum(g["revenue"] or 0 for g in srcs)
    if total == 0:
        return {"sources": [], "pct_sources": 0, "pct_revenue": 80}
    cum = 0
    pareto_srcs = []
    for g in srcs:
        cum += g["revenue"] or 0
        pareto_srcs.append(g["group"])
        if cum / total >= 0.8:
            break
    return {
        "sources":     pareto_srcs,
        "count":       len(pareto_srcs),
        "total_count": len(srcs),
        "pct_sources": len(pareto_srcs) / len(srcs) * 100 if srcs else 0,
        "pct_revenue": 80,
    }


def compute_anomalies(rows: list[EnrichedRow], totals: Totals,
                      conv_threshold: float = 2.0,
                      leads_threshold: int = 30) -> list[dict]:
    """Поиск аномалий в данных"""
    anomalies = []
    valid_rows = [r for r in rows if r.status != "INVALID"]
    if not valid_rows:
        return anomalies

    # Медиана avg_check
    checks = sorted([r.avg_check for r in valid_rows if r.avg_check and r.avg_check > 0])
    med_check = checks[len(checks) // 2] if checks else None

    total_rev = totals.revenue or 0

    for r in valid_rows:
        leads = r.leads or 0
        deals = r.deals or 0

        # Много лидов, почти нет продаж
        if leads >= leads_threshold and r.conv is not None and r.conv < conv_threshold:
            anomalies.append({
                "type": "high_leads_low_conv", "src": r.src, "group": r.group_name,
                "msg": f"{r.src}: {leads} лидов, конверсия {r.conv:.2f}%",
                "cls": "danger"
            })

        # Мало лидов, большая доля выручки
        if leads <= 5 and leads > 0 and r.revenue and total_rev > 0:
            share = r.revenue / total_rev * 100
            if share > 15:
                anomalies.append({
                    "type": "low_leads_high_rev", "src": r.src, "group": r.group_name,
                    "msg": f"{r.src}: {leads} лидов, {share:.1f}% выручки отдела",
                    "cls": "warning"
                })

        # Очень высокий средний чек
        if med_check and r.avg_check and r.avg_check > med_check * 3 and deals > 0:
            anomalies.append({
                "type": "high_avg_check", "src": r.src, "group": r.group_name,
                "msg": f"{r.src}: средний чек {r.avg_check:,.0f} ₸ — в {r.avg_check/med_check:.1f}× выше медианы",
                "cls": "warning"
            })

        # Высокий % повторных
        if r.pct_rep_deals and r.pct_rep_deals > 80 and deals > 2:
            anomalies.append({
                "type": "high_repeat", "src": r.src, "group": r.group_name,
                "msg": f"{r.src}: {r.pct_rep_deals:.1f}% повторных продаж",
                "cls": "info"
            })

    return anomalies


def compute_insights(grouped: list[dict], kpi: dict,
                     conv_good: float, conv_ok: float) -> list[dict]:
    """Приоритетные инсайты ①②③ для руководителя"""
    insights = []
    valid_g = [g for g in grouped if g.get("conv") is not None and g["conv"] <= 100
               and (g.get("leads") or 0) >= 3]

    # ① Лучший источник для масштабирования
    scale = [g for g in valid_g if g["conv"] >= conv_good and (g.get("leads") or 0) < 50
             and (g.get("revenue") or 0) > 0]
    if scale:
        s = sorted(scale, key=lambda x: x["conv"], reverse=True)[0]
        avg_ck = s.get("avg_check") or (kpi.get("avg_check") or 0)
        proj_rev = round(100 * (s["conv"] / 100) * avg_ck)
        insights.append({
            "priority": 1, "type": "scale",
            "title": f"① Масштабируй: «{s['group']}»",
            "text": (f"Конверсия {s['conv']:.2f}% при {s['leads']} лидах — "
                     f"самый эффективный источник. "
                     f"При 100 лидах прогноз выручки {proj_rev:,} ₸."),
            "cls": "success"
        })

    # ② Проблемный источник
    problem = [g for g in valid_g if (g.get("leads") or 0) >= 30 and g["conv"] < conv_ok]
    if problem:
        s = sorted(problem, key=lambda x: x.get("leads") or 0, reverse=True)[0]
        lost = (s.get("leads") or 0) - (s.get("deals") or 0)
        avg_ck = s.get("avg_check") or (kpi.get("avg_check") or 0)
        insights.append({
            "priority": 2, "type": "problem",
            "title": f"② Разберись: «{s['group']}»",
            "text": (f"{s['leads']} лидов, конверсия {s['conv']:.2f}% — "
                     f"слито {lost} лидов. "
                     f"Потенциальные потери: {round(lost * avg_ck):,} ₸."),
            "cls": "danger"
        })

    # ③ Темп выполнения плана (если есть)
    plan = kpi.get("plan") or 0
    company_rev = kpi.get("company_revenue") or 0
    if plan and (company_rev or kpi.get("revenue")):
        fact = company_rev or (kpi.get("revenue") or 0)
        plan_pct = fact / plan * 100
        if plan_pct < 80:
            insights.append({
                "priority": 3, "type": "pace",
                "title": "③ Внимание: план под угрозой",
                "text": f"Выполнено {plan_pct:.1f}% плана. Нужно ещё {plan - fact:,.0f} ₸.",
                "cls": "danger"
            })
        elif plan_pct >= 100:
            insights.append({
                "priority": 3, "type": "pace",
                "title": "③ План выполнен 🚀",
                "text": f"Выполнено {plan_pct:.1f}% — перевыполнение на {fact - plan:,.0f} ₸.",
                "cls": "success"
            })

    return sorted(insights, key=lambda x: x["priority"])


def compute_forecast(sessions_data: list[dict]) -> dict:
    """
    Линейная регрессия по нескольким периодам.
    sessions_data: [{period, revenue, leads, deals, conv, company_revenue}]
    conv в % (0-100) — только валидные (<=100) используются для регрессии
    """
    if len(sessions_data) < 2:
        return {"error": "Нужно минимум 2 периода"}

    def lr(pairs):
        valid = [(x, y) for x, y in pairs if y is not None]
        if len(valid) < 2:
            return None
        n = len(valid)
        sx  = sum(x for x, y in valid)
        sy  = sum(y for x, y in valid)
        sxy = sum(x * y for x, y in valid)
        sxx = sum(x * x for x, y in valid)
        den = n * sxx - sx * sx
        if den == 0:
            return {"m": 0, "b": sy / n, "next": sy / n}
        m = (n * sxy - sx * sy) / den
        b = (sy - m * sx) / n
        return {"m": m, "b": b, "next": m * n + b}

    data = sorted(sessions_data, key=lambda x: (x.get("period_year", 0), x.get("period_month", 0)))
    conv_pairs  = [(i, d["conv"] if d.get("conv") is not None and d["conv"] <= 100 else None)
                   for i, d in enumerate(data)]
    rev_pairs   = [(i, d.get("revenue"))   for i, d in enumerate(data)]
    leads_pairs = [(i, d.get("leads"))     for i, d in enumerate(data)]
    comp_pairs  = [(i, d.get("company_revenue")) for i, d in enumerate(data)
                   if d.get("company_revenue") and d["company_revenue"] > 0]

    conv_r  = lr(conv_pairs)
    rev_r   = lr(rev_pairs)
    leads_r = lr(leads_pairs)
    comp_r  = lr(comp_pairs) if len(comp_pairs) >= 2 else None

    f_conv  = max(0, min(100, conv_r["next"]))  if conv_r  else None
    f_rev   = max(0, rev_r["next"])             if rev_r   else 0
    f_leads = max(0, round(leads_r["next"]))    if leads_r else 0
    f_comp  = max(0, comp_r["next"])            if comp_r  else 0
    f_deals = round(f_leads * f_conv / 100) if (f_leads and f_conv) else None

    return {
        "next_period_data": data[-1],
        "forecast": {
            "conv":           round(f_conv, 2)   if f_conv  else None,
            "revenue":        round(f_rev)        if f_rev   else 0,
            "leads":          f_leads,
            "deals":          f_deals,
            "company_revenue": round(f_comp)     if f_comp  else None,
        },
        "history": data,
    }


# ── ГЛАВНЫЙ PIPELINE ─────────────────────────────────────────────────

def run_import_pipeline(
    raw_rows: list[dict],
    group_rules: list[dict],
) -> dict:
    """
    Полный pipeline импорта:
    raw_rows → normalize → validate → enrich → split (valid/invalid)
    Возвращает готовые данные для сохранения в БД
    """
    itogo_raw = None
    data_raw  = []

    for raw in raw_rows:
        src = _str(raw.get("Источник", ""))
        if src.lower() == "итого":
            itogo_raw = raw
        elif src:
            data_raw.append(raw)

    # Pipeline
    normalized = [normalize_row(r, i) for i, r in enumerate(data_raw, 1)]
    validated  = [validate_row(r) for r in normalized]
    enriched   = [enrich_row(r, group_rules) for r in validated]

    analytics_rows = [r for r in enriched if r.status != "INVALID"]
    invalid_rows   = [r for r in enriched if r.status == "INVALID"]
    totals         = normalize_totals(itogo_raw) if itogo_raw else Totals()

    quality = {
        "total":   len(enriched),
        "valid":   sum(1 for r in enriched if r.status == "VALID"),
        "warning": sum(1 for r in enriched if r.status == "WARNING"),
        "invalid": len(invalid_rows),
    }

    return {
        "totals":         totals,
        "analytics_rows": analytics_rows,
        "invalid_rows":   invalid_rows,
        "quality":        quality,
        "has_itogo":      itogo_raw is not None,
    }
