"""managers router"""
import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from backend.core.database import get_db
from backend.models.models import Manager
from backend.analytics import team_center

router = APIRouter(prefix="/managers", tags=["Managers"])

class ManagerCreate(BaseModel):
    name: str
    source: Optional[str] = None
    color: Optional[str] = "#2563EB"

@router.get("")
async def list_managers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Manager).where(Manager.is_active == True))
    return [{"id": str(m.id), "name": m.name, "source": m.source, "color": m.color, "photo_url": m.photo_url}
            for m in result.scalars().all()]

@router.post("")
async def create_manager(data: ManagerCreate, db: AsyncSession = Depends(get_db)):
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Имя менеджера не может быть пустым")
    # Проверка на дубликат имени среди активных менеджеров (регистронезависимо) —
    # раньше можно было создать двух менеджеров с одинаковым именем, что ломало
    # сопоставление по имени при импорте товарной аналитики (products.py resolve_manager).
    existing = await db.execute(
        select(Manager).where(
            Manager.is_active == True,
            func.lower(Manager.name) == name.lower(),
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=f"Менеджер с именем «{name}» уже существует",
        )
    m = Manager(name=name, source=data.source, color=data.color)
    db.add(m)
    await db.commit()
    return {"id": str(m.id), "name": m.name}

@router.delete("/{manager_id}")
async def delete_manager(manager_id: str, db: AsyncSession = Depends(get_db)):
    try:
        mgr_uuid = uuid.UUID(manager_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Неверный manager_id: {manager_id!r}")
    m = await db.get(Manager, mgr_uuid)
    if not m:
        raise HTTPException(status_code=404, detail="Менеджер не найден")
    m.is_active = False
    await db.commit()
    return {"deleted": manager_id}


class ManagerPhotoUpdate(BaseModel):
    photo_url: Optional[str] = None


@router.patch("/{manager_id}/photo")
async def update_manager_photo(manager_id: str, data: ManagerPhotoUpdate, db: AsyncSession = Depends(get_db)):
    """
    Фото — только возможность загрузить/поменять/убрать (photo_url=None); сам
    подбор фото не автоматизирован (пользователь: «фото вторичный вопрос,
    добавь просто возможность добавить, я сам внутри сайта добавлю»).
    Фронтенд сам сжимает картинку в небольшой data-URL перед отправкой (см.
    handleManagerPhotoFile в index.html) — здесь только защитный потолок на
    случай прямого обращения к эндпоинту мимо фронтенда.
    """
    try:
        mgr_uuid = uuid.UUID(manager_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Неверный manager_id: {manager_id!r}")
    m = await db.get(Manager, mgr_uuid)
    if not m:
        raise HTTPException(status_code=404, detail="Менеджер не найден")
    if data.photo_url and len(data.photo_url) > 2_000_000:
        raise HTTPException(status_code=400, detail="Фото слишком большое (после сжатия должно быть <2МБ)")
    m.photo_url = data.photo_url
    await db.commit()
    return {"id": str(m.id), "photo_url": m.photo_url}


# ── «Командный центр» (Фаза 1) ──────────────────────────────────────────────
# GET /managers/leaderboard  — рейтинг менеджеров за период
# GET /managers/{id}/dossier — досье одного менеджера за период
# Вся формульная логика — в backend/analytics/team_center.py (портирована
# дословно из frontend/index.html, см. докстринг того модуля).

@router.get("/leaderboard")
async def managers_leaderboard(period: str, db: AsyncSession = Depends(get_db)):
    from backend.routers.imports import parse_period
    try:
        parse_period(period)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    snapshot = await team_center.build_period_snapshot(db, period)
    if not snapshot["managers"]:
        return {"period": period, "prevPeriod": snapshot.get("prevPeriod"), "team": None, "managers": []}

    managers_out = []
    for m in snapshot["managers"]:
        history = await team_center.build_manager_history(db, uuid.UUID(m["id"]), limit=6)
        conv_delta_pp = (
            (m["conv"] - m["convPrev"]) if (m["conv"] is not None and m["convPrev"] is not None) else None
        )
        managers_out.append({
            "id": m["id"],
            "name": m["name"],
            "color": m["color"],
            "rank": m["rank"],
            "composite": m["composite"],
            "status": m["status"],
            "statusReason": m["statusReason"],
            "revenue": m["totalRevenue"],
            "hasInvoice": m["hasInvoice"],
            "revDeltaPct": m["revDeltaPct"],
            "conv": m["conv"],
            "convTeamAvg": snapshot["dept"]["avgConv"],
            "convDeltaPp": conv_delta_pp,
            "avgCheck": m["avgCheck"],
            "avgCheckDeltaPct": m["avgCheckDeltaPct"],
            "plan": m["plan"],
            "planPct": m["planPct"],
            "spark": [h["revenue"] for h in history],
        })

    # Детерминированный дайджест (без LLM, см. team_center.build_leaderboard_digest) —
    # раньше фронтенд отдельным запросом дёргал /ai/leaderboard-digest, который
    # молча ломался без ANTHROPIC_API_KEY. Теперь считается сразу здесь и не
    # зависит от внешнего API.
    digest = team_center.build_leaderboard_digest(managers_out)

    return {
        "period": snapshot["period"],
        "prevPeriod": snapshot.get("prevPeriod"),
        "team": snapshot["dept"],
        "managers": managers_out,
        "digest": digest,
    }


@router.get("/{manager_id}/dossier")
async def manager_dossier(manager_id: str, period: str, db: AsyncSession = Depends(get_db)):
    from backend.routers.imports import parse_period
    from backend.analytics import decisions as decisions_svc
    try:
        mgr_uuid = uuid.UUID(manager_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Неверный manager_id: {manager_id!r}")
    try:
        parse_period(period)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    snapshot = await team_center.build_period_snapshot(db, period)
    m = next((x for x in snapshot["managers"] if x["id"] == str(mgr_uuid)), None)
    if not m:
        raise HTTPException(
            status_code=404,
            detail=f"Нет данных по менеджеру за период «{period}» (сессия не загружена)",
        )

    good, bad = team_center.build_strengths_and_growth(
        m, snapshot["dept"]["avgConv"], snapshot["dept"]["deptAvgCheck"]
    )
    history = await team_center.build_manager_history(db, mgr_uuid, limit=6)
    conv_delta_pp = (
        (m["conv"] - m["convPrev"]) if (m["conv"] is not None and m["convPrev"] is not None) else None
    )

    # Полное досье («FBI-карточка со всей информацией и проблемах» — прямой
    # запрос пользователя, замена прежнего узкого KPI-досье): фото —
    # Manager.photo_url, задаётся вручную через PATCH .../photo (см. выше);
    # decisionHistory — ПОЛНАЯ история решений по человеку за всё время, не
    # только текущий период и не только один precedent_key (get_subject_history
    # фильтрует по subject_type+subject_id без привязки к конкретному вопросу —
    # см. decisions.py). Это и есть «его проблемы» в терминах пользователя:
    # накопленные прецеденты, а не сырой дамп метрик.
    manager_row = await db.get(Manager, mgr_uuid)
    decision_history = await decisions_svc.get_subject_history(db, "manager", mgr_uuid, limit=100)

    # Детерминированная рекомендация (без LLM, см. team_center.build_manager_recommendation) —
    # раньше фронтенд отдельным запросом дёргал /ai/manager-recommendation,
    # который молча ломался без ANTHROPIC_API_KEY. Теперь считается сразу
    # здесь из того же good/bad, что и ниже в ответе.
    recommendation = team_center.build_manager_recommendation(m, good, bad)

    return {
        "id": m["id"],
        "name": m["name"],
        "color": m["color"],
        "photoUrl": manager_row.photo_url if manager_row else None,
        "period": period,
        "rank": m["rank"],
        "totalManagers": snapshot["dept"]["n"],
        "composite": m["composite"],
        "status": m["status"],
        "statusReason": m["statusReason"],
        "kpi": {
            "revenue": m["totalRevenue"],
            "hasInvoice": m["hasInvoice"],
            "revDeltaPct": m["revDeltaPct"],
            "conv": m["conv"],
            "convTeamAvg": snapshot["dept"]["avgConv"],
            "convDeltaPp": conv_delta_pp,
            "avgCheck": m["avgCheck"],
            "avgCheckDeltaPct": m["avgCheckDeltaPct"],
            "plan": m["plan"],
            "planPct": m["planPct"],
        },
        "history": history,
        "strengths": good,
        "growthAreas": bad,
        "decisionHistory": decision_history,
        "recommendation": recommendation,
    }
