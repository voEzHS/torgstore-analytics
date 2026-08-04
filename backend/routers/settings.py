"""settings router"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from backend.core.database import get_db
from backend.models.models import Setting

router = APIRouter(prefix="/settings", tags=["Settings"])

class SettingUpdate(BaseModel):
    value: float | str | dict

@router.get("")
async def get_settings(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Setting))
    return {s.key: s.value for s in result.scalars().all()}

@router.patch("/{key}")
async def update_setting(key: str, data: SettingUpdate, db: AsyncSession = Depends(get_db)):
    s = await db.get(Setting, key)
    if not s:
        s = Setting(key=key, value=data.value)
        db.add(s)
    else:
        s.value = data.value
    await db.commit()
    return {"key": key, "value": s.value}
