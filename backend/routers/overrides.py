"""overrides router"""
import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from backend.core.database import get_db
from backend.models.models import Override, ManualSource, SourceGroup

# ── OVERRIDES ─────────────────────────────────────────────────────────
router = APIRouter(prefix="/overrides", tags=["Overrides"])


def _parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail=f"Неверный session_id: {value!r}")


class OverrideUpsert(BaseModel):
    session_id: str
    row_key:    str    # 'itogo' или UUID строки
    field:      str    # 'leads', 'deals', 'revenue'
    value:      float

@router.get("/{session_id}")
async def get_overrides(session_id: str, db: AsyncSession = Depends(get_db)):
    sess_uuid = _parse_uuid(session_id)
    result = await db.execute(
        select(Override).where(Override.session_id == sess_uuid))
    return [{"row_key": o.row_key, "field": o.field, "value": float(o.value)}
            for o in result.scalars().all()]

@router.put("")
async def upsert_override(data: OverrideUpsert, db: AsyncSession = Depends(get_db)):
    sess_uuid = _parse_uuid(data.session_id)
    result = await db.execute(
        select(Override).where(
            Override.session_id == sess_uuid,
            Override.row_key == data.row_key,
            Override.field == data.field
        ))
    ov = result.scalar_one_or_none()
    if ov:
        ov.value = data.value
    else:
        ov = Override(session_id=sess_uuid, row_key=data.row_key,
                      field=data.field, value=data.value)
        db.add(ov)
    await db.commit()
    # Инвалидируем кэш аналитики сразу здесь, а не полагаемся на то, что вызывающий
    # код (сегодня — фронтенд, reloadSession()) отдельно дёрнет /recalculate. Раньше
    # без этого GET /analytics/{id} мог отдавать устаревшие данные любому клиенту,
    # который не знал, что после PUT /overrides нужен ещё один вызов.
    from backend.routers.analytics import compute_and_cache
    await compute_and_cache(str(sess_uuid), db)
    return {"ok": True}

@router.delete("/{session_id}")
async def clear_overrides(session_id: str, db: AsyncSession = Depends(get_db)):
    sess_uuid = _parse_uuid(session_id)
    await db.execute(delete(Override).where(Override.session_id == sess_uuid))
    await db.commit()
    from backend.routers.analytics import compute_and_cache
    await compute_and_cache(str(sess_uuid), db)
    return {"cleared": session_id}
