"""sources router — source groups management"""
import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from backend.core.database import get_db
from backend.models.models import SourceGroup

router = APIRouter(prefix="/sources", tags=["Sources"])


def _parse_uuid(value: str, field_name: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail=f"Неверный {field_name}: {value!r}")


class SourceGroupCreate(BaseModel):
    group_name: str
    match_type: str = "contains"   # regex | alias | prefix | contains
    pattern:    Optional[str] = None
    aliases:    Optional[List[str]] = None
    priority:   int = 100
    color:      str = "#6B7280"

@router.get("/groups")
async def list_groups(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(SourceGroup).where(SourceGroup.is_active == True).order_by(SourceGroup.priority))
    return [
        {"id": str(g.id), "group_name": g.group_name, "match_type": g.match_type,
         "pattern": g.pattern, "aliases": g.aliases, "priority": g.priority, "color": g.color}
        for g in result.scalars().all()
    ]

@router.post("/groups")
async def create_group(data: SourceGroupCreate, db: AsyncSession = Depends(get_db)):
    g = SourceGroup(**data.model_dump())
    db.add(g)
    await db.commit()
    return {"id": str(g.id), "group_name": g.group_name}

@router.put("/groups/{group_id}")
async def update_group(group_id: str, data: SourceGroupCreate, db: AsyncSession = Depends(get_db)):
    g = await db.get(SourceGroup, _parse_uuid(group_id, "group_id"))
    if not g:
        raise HTTPException(status_code=404, detail="Группа не найдена")
    for k, v in data.model_dump().items():
        setattr(g, k, v)
    await db.commit()
    return {"id": str(g.id), "group_name": g.group_name}

@router.delete("/groups/{group_id}")
async def delete_group(group_id: str, db: AsyncSession = Depends(get_db)):
    g = await db.get(SourceGroup, _parse_uuid(group_id, "group_id"))
    if g:
        g.is_active = False
        await db.commit()
    return {"deleted": group_id}

# Manual sources
from backend.models.models import ManualSource
from backend.routers.analytics import compute_and_cache

class ManualSourceCreate(BaseModel):
    session_id: str
    src:        str
    leads:      float = 0
    deals:      float = 0
    revenue:    float = 0

@router.post("/manual")
async def add_manual_source(data: ManualSourceCreate, db: AsyncSession = Depends(get_db)):
    sess_uuid = _parse_uuid(data.session_id, "session_id")
    result = await db.execute(
        select(ManualSource).where(
            ManualSource.session_id == sess_uuid, ManualSource.src == data.src))
    ms = result.scalar_one_or_none()
    if ms:
        ms.leads = data.leads; ms.deals = data.deals; ms.revenue = data.revenue
    else:
        from backend.analytics.engine import detect_group
        from backend.models.models import SourceGroup as SG
        rules_res = await db.execute(select(SG).where(SG.is_active == True).order_by(SG.priority))
        rules = [{"group_name": g.group_name, "match_type": g.match_type,
                  "pattern": g.pattern, "aliases": g.aliases or [], "priority": g.priority}
                 for g in rules_res.scalars().all()]
        ms = ManualSource(session_id=sess_uuid, src=data.src,
                          group_name=detect_group(data.src, rules),
                          leads=data.leads, deals=data.deals, revenue=data.revenue)
        db.add(ms)
    await db.commit()
    await compute_and_cache(data.session_id, db)
    return {"ok": True, "src": data.src}

@router.delete("/manual/{session_id}/{src}")
async def delete_manual_source(session_id: str, src: str, db: AsyncSession = Depends(get_db)):
    sess_uuid = _parse_uuid(session_id, "session_id")
    result = await db.execute(
        select(ManualSource).where(ManualSource.session_id == sess_uuid, ManualSource.src == src))
    ms = result.scalar_one_or_none()
    if ms:
        await db.delete(ms)
        await db.commit()
        await compute_and_cache(session_id, db)
    return {"deleted": src}

@router.get("/manual/{session_id}")
async def get_manual_sources(session_id: str, db: AsyncSession = Depends(get_db)):
    sess_uuid = _parse_uuid(session_id, "session_id")
    result = await db.execute(
        select(ManualSource).where(ManualSource.session_id == sess_uuid))
    return [{"src": m.src, "leads": float(m.leads or 0), "deals": float(m.deals or 0),
             "revenue": float(m.revenue or 0), "group_name": m.group_name}
            for m in result.scalars().all()]
