"""
Скидки менеджеров — независимый источник, отдельный от invoice_stats.
Источник в CRM: Склад → Накладные, фильтр Статус=Доставлено + Дата
создания=период + Менеджер=X. Данные снимаются с эндпоинта CRM
GET .../requests/total-sum-ajax — он не сужается параметром sale_channel
на практике, поэтому это сумма по ВСЕМ каналам сразу, а не только
Розничные продажи/Алматы как у invoice_stats. Не складывать и не
сравнивать напрямую с invoice_stats.gross_revenue.

POST /discounts/import — сохранить срез по менеджеру/периоду
GET  /discounts/{manager_id} — прочитать все периоды менеджера
GET  /discounts — прочитать все (для сводки по компании)
"""
import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.core.database import get_db
from backend.models.models import ManagerDiscount, Manager
from backend.routers.imports import parse_period

router = APIRouter(prefix="/discounts", tags=["Discounts"])


class DiscountImportIn(BaseModel):
    manager_id: str
    period: str
    sale_amount: float = 0        # CRM totalSum (к оплате, после скидки, все каналы, статус "Доставлено")
    discount_amount: float = 0    # CRM totalSumWithoutDiscount (по факту — сумма скидки)


@router.post("/import")
async def import_manager_discount(body: DiscountImportIn, db: AsyncSession = Depends(get_db)):
    try:
        period_year, period_month = parse_period(body.period)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        mgr_id = uuid.UUID(body.manager_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверный manager_id")

    mgr = await db.get(Manager, mgr_id)
    if not mgr:
        raise HTTPException(status_code=404, detail="Менеджер не найден")

    list_amount = body.sale_amount + body.discount_amount
    discount_pct = round(body.discount_amount / list_amount * 100, 2) if list_amount > 0 else None

    existing = await db.execute(
        select(ManagerDiscount).where(
            ManagerDiscount.manager_id == mgr_id,
            ManagerDiscount.period == body.period,
        )
    )
    row = existing.scalar_one_or_none()

    if row:
        row.sale_amount = body.sale_amount
        row.discount_amount = body.discount_amount
        row.discount_pct = discount_pct
        row.period_year = period_year
        row.period_month = period_month
    else:
        row = ManagerDiscount(
            manager_id=mgr_id,
            period=body.period,
            period_year=period_year,
            period_month=period_month,
            sale_amount=body.sale_amount,
            discount_amount=body.discount_amount,
            discount_pct=discount_pct,
        )
        db.add(row)

    await db.commit()
    return {
        "manager_id": body.manager_id,
        "manager_name": mgr.name,
        "period": body.period,
        "sale_amount": body.sale_amount,
        "discount_amount": body.discount_amount,
        "discount_pct": discount_pct,
    }


@router.get("/{manager_id}")
async def get_manager_discounts(manager_id: str, period: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    try:
        mgr_id = uuid.UUID(manager_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверный manager_id")

    query = select(ManagerDiscount).where(ManagerDiscount.manager_id == mgr_id)
    if period:
        query = query.where(ManagerDiscount.period == period)
    result = await db.execute(query)
    rows = result.scalars().all()
    return [
        {
            "id": str(r.id),
            "period": r.period,
            "sale_amount": float(r.sale_amount or 0),
            "discount_amount": float(r.discount_amount or 0),
            "discount_pct": float(r.discount_pct) if r.discount_pct is not None else None,
            "snapshot_at": r.snapshot_at.isoformat() if r.snapshot_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


@router.get("")
async def list_all_manager_discounts(period: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    # Фильтр Manager.is_active — тот же класс бага, что в invoices.py/decline_reasons.py
    # (аудит 11.08.2026), профилактически на случай данных у будущих деактивированных
    # менеджеров, даже если на 11.08.2026 сирот здесь не найдено.
    query = (
        select(ManagerDiscount, Manager)
        .join(Manager, Manager.id == ManagerDiscount.manager_id)
        .where(Manager.is_active == True)
    )
    if period:
        query = query.where(ManagerDiscount.period == period)
    result = await db.execute(query)
    return [
        {
            "manager_id": str(r.manager_id),
            "manager_name": mgr.name,
            "period": r.period,
            "sale_amount": float(r.sale_amount or 0),
            "discount_amount": float(r.discount_amount or 0),
            "discount_pct": float(r.discount_pct) if r.discount_pct is not None else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r, mgr in result.all()
    ]
