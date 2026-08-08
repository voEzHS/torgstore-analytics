"""
Pace — темп выполнения плана с учётом даты.

Проблема, которую это закрывает: везде на сайте (Обзор, Менеджер, Сравнение,
Командный центр, «Сегодня») "% плана" считался как revenue/plan*100 без
единого намёка на то, какой сегодня день месяца. На 3-й день месяца это
выглядит как катастрофа (типично ~10% плана), на 28-й — как всё ещё
терпимо, если день выдался средним. compute_pace() — единственный источник
этой логики на бэкенде; дословный аналог обязан быть на фронтенде
(computePace() в index.html) — числа обязаны совпадать, как и с остальной
частью team_center.py (см. докстринг того файла про дублирование формул).

ЧЕСТНОЕ ОГРАНИЧЕНИЕ: точка отсчёта "сегодня" — реальная календарная дата
(datetime.now), а НЕ дата последней выгрузки данных из CRM. В БД нет поля
"по какое число фактически загружены данные за период" — period это только
календарный месяц-label (см. parse_period), не диапазон дат факта. Поэтому
если импорт отстаёт от календаря на несколько дней, pace будет казаться
чуть хуже, чем есть на самом деле: ожидание считается по календарю, а факт —
по последней загруженной выгрузке. Не путать с /api/v1/data-freshness
(routers/freshness.py) — та эндпоинт про "давно ли обновляли данные вообще",
это другой вопрос.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timezone
from typing import Optional

from backend.routers.imports import parse_period


def compute_pace(period: str, plan: Optional[float], actual_revenue: Optional[float],
                  today: Optional[date] = None) -> Optional[dict]:
    """
    Pace-контекст за один период-месяц, или None если:
    - план не задан (plan пусто/0) — сравнивать не с чем;
    - период не парсится (не "Месяц ГГГГ");
    - период ещё не наступил (план на будущее — темп сравнивать не с чем).

    gapPct/gapRevenue — факт МИНУС ОЖИДАНИЕ НА СЕГОДНЯ (не минус план целиком).
    Отрицательное значение = отстаём от графика, ноль/положительное = в
    графике или впереди. onPace — с допуском ±5 п.п., чтобы не дёргаться от
    шума одного дня (например, ноль продаж именно сегодня к вечеру).
    """
    if not plan:
        return None
    try:
        year, month = parse_period(period)
    except ValueError:
        return None

    today = today or datetime.now(timezone.utc).date()
    if (year, month) > (today.year, today.month):
        return None  # план на будущий период — темп сравнивать не с чем

    days_in_month = calendar.monthrange(year, month)[1]
    is_current = (year, month) == (today.year, today.month)
    day_of_month = today.day if is_current else days_in_month  # прошедший период = месяц закрыт целиком

    actual_revenue = float(actual_revenue or 0)
    expected_pct = day_of_month / days_in_month * 100
    expected_revenue = plan * day_of_month / days_in_month
    actual_pct = actual_revenue / plan * 100

    gap_pct = actual_pct - expected_pct
    gap_revenue = actual_revenue - expected_revenue

    return {
        "daysInMonth": days_in_month,
        "dayOfMonth": day_of_month,
        "isCurrentPeriod": is_current,
        "isPastPeriod": not is_current,
        "expectedPct": round(expected_pct, 1),
        "expectedRevenue": round(expected_revenue),
        "actualPct": round(actual_pct, 1),
        "gapPct": round(gap_pct, 1),
        "gapRevenue": round(gap_revenue),
        "onPace": gap_pct >= -5,
    }
