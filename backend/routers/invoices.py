"""
Накладные (факт продаж по кассе/отгрузке) — независимый источник денег.
POST /api/v1/invoices/import — сохранить срез по менеджеру/периоду
GET  /api/v1/invoices/{manager_id} — прочитать все периоды менеджера
GET  /api/v1/invoices — прочитать все (для сверки суммы по компании)
"""
import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.core.database import get_db
from backend.models.models import InvoiceStats, Manager
from backend.routers.imports import parse_period

router = APIRouter(prefix="/invoices", tags=["Invoices"])


class InvoiceImportIn(BaseModel):
    manager_id: str
    period: str
    channel: str = "Розничные продажи"
    city: str = "Алматы"
    gross_revenue: float = 0
    doc_count: int = 0
    returns_amount: float = 0
    returns_count: int = 0


@router.post("/import")
async def import_invoice_stats(body: InvoiceImportIn, db: AsyncSession = Depends(get_db)):
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

    existing = await db.execute(
        select(InvoiceStats).where(
            InvoiceStats.manager_id == mgr_id,
            InvoiceStats.period == body.period,
            InvoiceStats.channel == body.channel,
            InvoiceStats.city == body.city,
        )
    )
    row = existing.scalar_one_or_none()

    net = round(body.gross_revenue - body.returns_amount, 2)

    if row:
        row.gross_revenue = body.gross_revenue
        row.doc_count = body.doc_count
        row.returns_amount = body.returns_amount
        row.returns_count = body.returns_count
        row.net_revenue = net
    else:
        row = InvoiceStats(
            manager_id=mgr_id,
            period=body.period,
            period_year=period_year,
            period_month=period_month,
            channel=body.channel,
            city=body.city,
            gross_revenue=body.gross_revenue,
            doc_count=body.doc_count,
            returns_amount=body.returns_amount,
            returns_count=body.returns_count,
            net_revenue=net,
        )
        db.add(row)

    await db.commit()
    return {
        "manager_id": body.manager_id,
        "manager_name": mgr.name,
        "period": body.period,
        "gross_revenue": body.gross_revenue,
        "returns_amount": body.returns_amount,
        "net_revenue": net,
        "doc_count": body.doc_count,
    }


@router.get("/{manager_id}")
async def get_invoice_stats(manager_id: str, period: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    try:
        mgr_id = uuid.UUID(manager_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверный manager_id")

    query = select(InvoiceStats).where(InvoiceStats.manager_id == mgr_id)
    if period:
        query = query.where(InvoiceStats.period == period)
    result = await db.execute(query)
    rows = result.scalars().all()
    return [
        {
            "id": str(r.id),
            "period": r.period,
            "channel": r.channel,
            "city": r.city,
            "gross_revenue": float(r.gross_revenue or 0),
            "doc_count": r.doc_count,
            "returns_amount": float(r.returns_amount or 0),
            "returns_count": r.returns_count,
            "net_revenue": float(r.net_revenue or 0),
            "snapshot_at": r.snapshot_at.isoformat() if r.snapshot_at else None,
        }
        for r in rows
    ]


@router.get("")
async def list_all_invoice_stats(period: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    query = select(InvoiceStats, Manager).join(Manager, Manager.id == InvoiceStats.manager_id)
    if period:
        query = query.where(InvoiceStats.period == period)
    result = await db.execute(query)
    return [
        {
            "manager_id": str(r.manager_id),
            "manager_name": mgr.name,
            "period": r.period,
            "channel": r.channel,
            "city": r.city,
            "gross_revenue": float(r.gross_revenue or 0),
            "doc_count": r.doc_count,
            "returns_amount": float(r.returns_amount or 0),
            "returns_count": r.returns_count,
            "net_revenue": float(r.net_revenue or 0),
        }
        for r, mgr in result.all()
    ]
