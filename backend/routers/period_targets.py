"""period_targets router"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from backend.core.database import get_db
from backend.models.models import PeriodTarget
from backend.routers.imports import parse_period

router = APIRouter(prefix="/period-targets", tags=["Period Targets"])


class PeriodTargetUpsert(BaseModel):
    period: str
    plan: Optional[float] = 0
    company_revenue: Optional[float] = 0


@router.get("")
async def list_targets(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PeriodTarget).order_by(
        PeriodTarget.period_year.desc(), PeriodTarget.period_month.desc()))
    return [{"period": p.period, "plan": float(p.plan or 0),
             "company_revenue": float(p.company_revenue or 0)}
            for p in result.scalars().all()]

@router.put("")
async def upsert_target(data: PeriodTargetUpsert, db: AsyncSession = Depends(get_db)):
    # Раньше здесь был отдельный, более слабый _parse_period: на нераспознанном
    # формате он молча возвращал (2026, 1) вместо ошибки — план с опечаткой в
    # периоде тихо записывался бы под "Январь 2026". Переиспользуем тот же
    # parse_period, что и в imports.py (там же MONTH_MAP) — одна реализация,
    # явная ошибка на плохом вводе вместо двух версий с разным поведением.
    #
    # БАГ (найден 30.07.2026 при попытке загрузить личные планы менеджеров из CRM):
    # план отдельного менеджера хранится под синтетическим ключом
    # 'mgr:<UUID>:<период>' (см. frontend saveMgrPlanPeriod/renderTeam) — год/месяц
    # для НЕГО не нужны для функциональности (frontend сам режет строку по ':'
    # при чтении), но parse_period() вызывался на всей строке целиком, включая
    # префикс 'mgr:<UUID>:', и .split() распознавал в ней 2 "слова", второе из
    # которых — валидный год, а первое — 'mgr:<UUID>:июль' вместо 'июль', что не
    # находится в MONTH_MAP → 400 "Неизвестный месяц". Из-за этого ЛЮБая попытка
    # сохранить план отдельного менеджера (что через форму на сайте, что через
    # API) стабильно падала с момента, когда parse_period был унифицирован
    # (см. история задач: "Убрать дублирование parse_period"). Раньше слабый
    # _parse_period такой ввод не валидировал вообще и тихо писал (2026, 1) —
    # функционально это работало (год/месяц у mgr:-строк decorative), просто
    # без явной проверки. Чиним, сохраняя строгую валидацию для обычных
    # периодов, но отдельно разбирая mgr:-префикс перед парсингом месяца/года.
    period_for_parsing = data.period
    if period_for_parsing.startswith('mgr:'):
        parts = period_for_parsing.split(':', 2)
        if len(parts) == 3:
            period_for_parsing = parts[2]
    try:
        year, month = parse_period(period_for_parsing)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = await db.execute(select(PeriodTarget).where(PeriodTarget.period == data.period))
    pt = result.scalar_one_or_none()
    if pt:
        pt.plan = data.plan
        pt.company_revenue = data.company_revenue
    else:
        pt = PeriodTarget(period=data.period, period_year=year,
                          period_month=month, plan=data.plan,
                          company_revenue=data.company_revenue)
        db.add(pt)
    await db.commit()
    return {"period": data.period, "plan": data.plan, "company_revenue": data.company_revenue}

@router.delete("/{period}")
async def delete_target(period: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PeriodTarget).where(PeriodTarget.period == period))
    pt = result.scalar_one_or_none()
    if pt:
        await db.delete(pt)
        await db.commit()
    return {"deleted": period}
