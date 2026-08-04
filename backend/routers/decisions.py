"""decisions router — «Доклад» (см. backend/analytics/decisions.py)"""
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.database import get_db
from backend.analytics import decisions as decisions_svc

router = APIRouter(prefix="/decisions", tags=["Decisions"])


class RespondRequest(BaseModel):
    action: str                      # accept | modify | reject | postpone
    reason: Optional[str] = None
    params: Optional[dict] = None
    postpone_until: Optional[datetime] = None


class VerifyRequest(BaseModel):
    outcome: str                     # confirmed_worked | confirmed_failed | inconclusive | insufficient_data
    actual_effect: Optional[float] = None


@router.get("/today")
async def today(period: str, budget: int = decisions_svc.DAILY_BUDGET, db: AsyncSession = Depends(get_db)):
    from backend.routers.imports import parse_period
    try:
        parse_period(period)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await decisions_svc.get_today_decisions(db, period, budget=budget)


@router.get("/{decision_id}")
async def get_one(decision_id: str, db: AsyncSession = Depends(get_db)):
    try:
        d_uuid = uuid.UUID(decision_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Неверный decision_id: {decision_id!r}")
    d = await decisions_svc.get_decision(db, d_uuid)
    if not d:
        raise HTTPException(status_code=404, detail="Решение не найдено")
    return decisions_svc.serialize_decision(d)


@router.get("/{decision_id}/history")
async def history(decision_id: str, db: AsyncSession = Depends(get_db)):
    try:
        d_uuid = uuid.UUID(decision_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Неверный decision_id: {decision_id!r}")
    d = await decisions_svc.get_decision(db, d_uuid)
    if not d:
        raise HTTPException(status_code=404, detail="Решение не найдено")
    return await decisions_svc.get_subject_history(db, d.subject_type, d.subject_id, exclude_id=d.id)


@router.post("/{decision_id}/respond")
async def respond(decision_id: str, body: RespondRequest, db: AsyncSession = Depends(get_db)):
    try:
        d_uuid = uuid.UUID(decision_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Неверный decision_id: {decision_id!r}")
    try:
        d = await decisions_svc.respond_to_decision(
            db, d_uuid, body.action,
            reason=body.reason, params=body.params, postpone_until=body.postpone_until,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return decisions_svc.serialize_decision(d)


@router.post("/{decision_id}/verify")
async def verify(decision_id: str, body: VerifyRequest, db: AsyncSession = Depends(get_db)):
    try:
        d_uuid = uuid.UUID(decision_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Неверный decision_id: {decision_id!r}")
    try:
        d = await decisions_svc.verify_decision(db, d_uuid, body.outcome, actual_effect=body.actual_effect)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return decisions_svc.serialize_decision(d)


@router.get("/rules/suspended")
async def suspended_rules(db: AsyncSession = Depends(get_db)):
    return await decisions_svc.list_suspended_rules(db)


@router.post("/rules/{signal_type}/unsuspend")
async def unsuspend(signal_type: str, db: AsyncSession = Depends(get_db)):
    await decisions_svc.unsuspend_rule(db, signal_type)
    return {"signal_type": signal_type, "suspended": False}
