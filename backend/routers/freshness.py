"""
Свежесть данных по всем 6 источникам — для ежедневного напоминания
(scheduled task, НЕ трогает CRM, только читает свою же БД).

Отдаётся без Basic Auth (см. main.py _UNPROTECTED_PATHS) — специально,
чтобы scheduled task мог дёргать этот эндпоинт без хранения CRM-логина/пароля
сайта в теле задачи. Поэтому ответ НЕ должен содержать ничего чувствительного
(выручка, имена менеджеров, суммы и т.п.) — только метаданные о времени
последнего импорта по источнику.

Правило источника: "последнее обновление" = момент, когда соответствующая
строка была реально пересоздана/перезаписана при импорте, а не просто первая
загрузка когда-либо (см. комментарии у InvoiceStats/DeclineReasonStats/
ManagerDiscount.updated_at в models.py — там был баг, updated_at не
обновлялся на повторном impорте, пока не поставили onupdate=func.now()).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func as sa_func

from backend.core.database import get_db
from backend.models.models import (
    SourceRow, InvoiceStats, ManagerDiscount,
    ProductImport, PeriodTarget, DeclineReasonStats,
)

router = APIRouter(tags=["Freshness"])

# Порог "устарело" по источнику, дней. Подобраны под фактический темп
# актуализации в этом проекте (см. crm-import-runbook.md §5): Сквозная/
# Накладные обновляются чаще всего, Планы — раз в месяц, поэтому у них
# большой порог, иначе напоминание будет орать про план каждую неделю зря.
THRESHOLDS = {
    "skvoznaya":       3,
    "invoices":        3,
    "discounts":       5,
    "products":        7,
    "decline_reasons": 7,
    "plans":           30,
}

LABELS = {
    "skvoznaya":       "Сквозная аналитика",
    "invoices":        "Накладные",
    "discounts":       "Скидки",
    "products":        "Товарная аналитика",
    "decline_reasons": "Причины отказа",
    "plans":           "Планы продаж",
}


async def _max(db: AsyncSession, column):
    result = await db.execute(select(sa_func.max(column)))
    return result.scalar_one_or_none()


@router.get("/data-freshness")
async def data_freshness(db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)

    raw = {
        "skvoznaya":       await _max(db, SourceRow.created_at),
        "invoices":        await _max(db, InvoiceStats.updated_at),
        "discounts":       await _max(db, ManagerDiscount.updated_at),
        "products":        await _max(db, ProductImport.uploaded_at),
        "decline_reasons": await _max(db, DeclineReasonStats.updated_at),
        "plans":           await _max(db, PeriodTarget.updated_at),
    }

    sources = []
    any_stale = False
    for key, ts in raw.items():
        days_since = None
        if ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            days_since = (now - ts).total_seconds() / 86400.0
        threshold = THRESHOLDS[key]
        stale = (ts is None) or (days_since is not None and days_since > threshold)
        if stale:
            any_stale = True
        sources.append({
            "key": key,
            "label": LABELS[key],
            "last_updated_at": ts.isoformat() if ts else None,
            "days_since": round(days_since, 1) if days_since is not None else None,
            "threshold_days": threshold,
            "stale": stale,
        })

    return {
        "checked_at": now.isoformat(),
        "sources": sources,
        "any_stale": any_stale,
    }
