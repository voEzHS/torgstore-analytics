"""
Sessions router — /api/v1/sessions
"""
import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from backend.core.database import get_db
from backend.models.models import Session, SessionTotals, Import, Manager, PeriodTarget

router = APIRouter(prefix="/sessions", tags=["Sessions"])


def _parse_uuid(value: str, field_name: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail=f"Неверный {field_name}: {value!r}")


@router.get("")
async def list_sessions(
    period:     str = None,
    manager_id: str = None,
    db: AsyncSession = Depends(get_db)
):
    query = select(Session, SessionTotals, Manager).join(
        SessionTotals, SessionTotals.session_id == Session.id, isouter=True
    ).join(
        Manager, Manager.id == Session.manager_id, isouter=True
    )
    if period:
        query = query.where(Session.period == period)
    if manager_id:
        query = query.where(Session.manager_id == _parse_uuid(manager_id, "manager_id"))
    query = query.order_by(desc(Session.period_year), desc(Session.period_month))

    result = await db.execute(query)
    rows = result.all()

    # Один запрос на все планы периодов вместо запроса внутри цикла на каждую
    # сессию (N+1) — раньше при N сессиях делалось N дополнительных запросов
    # к period_targets вместо одного.
    periods_needed = {sess.period for sess, _totals, _mgr in rows}
    pt_by_period = {}
    if periods_needed:
        pt_result = await db.execute(
            select(PeriodTarget).where(PeriodTarget.period.in_(periods_needed))
        )
        pt_by_period = {pt.period: pt for pt in pt_result.scalars().all()}

    sessions = []
    for sess, totals, mgr in rows:
        pt = pt_by_period.get(sess.period)
        sessions.append({
            "id":           str(sess.id),
            "period":       sess.period,
            "period_year":  sess.period_year,
            "period_month": sess.period_month,
            "manager_id":   str(sess.manager_id),
            "manager_name": mgr.name if mgr else "Весь отдел",
            "manager_color": mgr.color if mgr else "#1E3A5F",
            "totals": {
                "leads":   float(totals.leads or 0)   if totals else None,
                "deals":   float(totals.deals or 0)   if totals else None,
                "revenue": float(totals.revenue or 0) if totals else None,
                "conv":    float(totals.conv or 0)    if totals else None,
                "avg_check": float(totals.avg_check or 0) if totals else None,
            } if totals else None,
            "plan":            float(pt.plan or 0)            if pt else None,
            "company_revenue": float(pt.company_revenue or 0) if pt else None,
        })
    return sessions


@router.get("/periods")
async def list_periods(db: AsyncSession = Depends(get_db)):
    """Все доступные периоды с метаданными"""
    result = await db.execute(
        select(Session.period, Session.period_year, Session.period_month)
        .distinct()
        .order_by(desc(Session.period_year), desc(Session.period_month))
    )
    return [
        {"period": r.period, "year": r.period_year, "month": r.period_month}
        for r in result.all()
    ]


@router.delete("/{session_id}")
async def delete_session(session_id: str, db: AsyncSession = Depends(get_db)):
    sess_uuid = _parse_uuid(session_id, "session_id")
    sess = await db.get(Session, sess_uuid)
    if not sess:
        raise HTTPException(status_code=404, detail="Сессия не найдена")
    await db.delete(sess)
    await db.commit()
    return {"deleted": session_id}
