"""
Причины отказа (CRM custom field «Причина отказа») по лидам «Не реализовано».

ВАЖНО: это ВЫБОРКА, не исчерпывающий список — см. комментарий в
migrations/010_add_decline_reasons.sql. Поле не агрегируется нигде в
built-in отчётах/фильтрах/экспорте CRM Zymyran; собрано чтением по одному
лиду за раз через /api/crm/leads/details с обязательным rate-limit 3с
между запросами к CRM (см. CLAUDE.md).

POST /api/v1/decline-reasons/import — сохранить срез причин по менеджеру/периоду
     (полностью заменяет предыдущий срез для этого manager_id+period+pipeline —
     это снэпшот выборки, а не накопительный счётчик)
GET  /api/v1/decline-reasons/{manager_id} — прочитать причины менеджера
GET  /api/v1/decline-reasons — прочитать все (для сводной секции/аналитики)
"""
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.database import get_db
from backend.models.models import DeclineReasonStats, Manager
from backend.routers.imports import parse_period

router = APIRouter(prefix="/decline-reasons", tags=["Decline Reasons"])


class ReasonCount(BaseModel):
    reason: str
    count: int


class DeclineReasonImportIn(BaseModel):
    manager_id: str
    period: str
    pipeline: str = "Розница Алматы"
    sample_size: int = 0
    reasons: List[ReasonCount] = []


@router.post("/import")
async def import_decline_reasons(body: DeclineReasonImportIn, db: AsyncSession = Depends(get_db)):
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

    # Снэпшот выборки — старый срез для этого manager+period+pipeline полностью
    # заменяется новым (в отличие от invoices, тут нет смысла накапливать
    # частичные обновления по одной причине за раз).
    await db.execute(
        delete(DeclineReasonStats).where(
            DeclineReasonStats.manager_id == mgr_id,
            DeclineReasonStats.period == body.period,
            DeclineReasonStats.pipeline == body.pipeline,
        )
    )

    for rc in body.reasons:
        db.add(
            DeclineReasonStats(
                manager_id=mgr_id,
                period=body.period,
                period_year=period_year,
                period_month=period_month,
                pipeline=body.pipeline,
                reason=rc.reason,
                count=rc.count,
                sample_size=body.sample_size,
            )
        )

    await db.commit()
    return {
        "manager_id": body.manager_id,
        "manager_name": mgr.name,
        "period": body.period,
        "pipeline": body.pipeline,
        "sample_size": body.sample_size,
        "reasons_saved": len(body.reasons),
    }


@router.get("/{manager_id}")
async def get_decline_reasons(manager_id: str, period: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    try:
        mgr_id = uuid.UUID(manager_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверный manager_id")

    query = select(DeclineReasonStats).where(DeclineReasonStats.manager_id == mgr_id)
    if period:
        query = query.where(DeclineReasonStats.period == period)
    result = await db.execute(query)
    rows = result.scalars().all()
    return [
        {
            "id": str(r.id),
            "period": r.period,
            "pipeline": r.pipeline,
            "reason": r.reason,
            "count": r.count,
            "sample_size": r.sample_size,
            "snapshot_at": r.snapshot_at.isoformat() if r.snapshot_at else None,
        }
        for r in rows
    ]


@router.get("")
async def list_all_decline_reasons(period: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    # Фильтр Manager.is_active — см. аналогичный фикс в invoices.py
    # (аудит 11.08.2026: Воробьева, июль 2026, 6 лидов без причины, попадали
    # в компанейскую сводку, хотя менеджер деактивирован и нигде не виден).
    query = (
        select(DeclineReasonStats, Manager)
        .join(Manager, Manager.id == DeclineReasonStats.manager_id)
        .where(Manager.is_active == True)
    )
    if period:
        query = query.where(DeclineReasonStats.period == period)
    result = await db.execute(query)
    return [
        {
            "manager_id": str(r.manager_id),
            "manager_name": mgr.name,
            "period": r.period,
            "pipeline": r.pipeline,
            "reason": r.reason,
            "count": r.count,
            "sample_size": r.sample_size,
        }
        for r, mgr in result.all()
    ]
