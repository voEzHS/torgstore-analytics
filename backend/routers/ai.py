"""
AI-аналитик — роуты для чата и отчётов
POST /api/v1/ai/chat
POST /api/v1/ai/compare-analysis
POST /api/v1/ai/report/{session_id}
"""
import os
import uuid
import json
import hashlib
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from backend.core.database import get_db
from backend.models.models import Session, AnalyticsCache
from backend.analytics import team_center

router = APIRouter(tags=["AI"])

# In-memory кэш AI-ответов (1 час TTL). Раньше не имел верхнего предела размера —
# записи только читались (и молча игнорировались как "устаревшие" при TTL-проверке
# на чтении), но никогда не удалялись из словаря. При долгой работе процесса (без
# перезапуска) и разнообразных period/managers-комбинациях словарь рос бы неограниченно.
# _CACHE_MAX_ENTRIES — простой предохранитель: при превышении лимита выбрасываем
# самые старые записи перед добавлением новой.
_ai_cache: dict = {}
_CACHE_TTL = 3600
_CACHE_MAX_ENTRIES = 200


def _cache_key(prefix: str, data: str) -> str:
    return f"{prefix}:{hashlib.md5(data.encode()).hexdigest()}"


def _cache_set(key: str, text: str) -> None:
    if len(_ai_cache) >= _CACHE_MAX_ENTRIES:
        oldest_key = min(_ai_cache, key=lambda k: _ai_cache[k]["ts"])
        del _ai_cache[oldest_key]
    _ai_cache[key] = {"text": text, "ts": datetime.now()}


def _get_client():
    """Создаёт Anthropic клиент. Бросает 503 если ключ не настроен."""
    try:
        import anthropic
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="Библиотека anthropic не установлена. Выполни: pip install anthropic"
        )
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or api_key == "your-anthropic-api-key":
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY не настроен. Добавь ключ в .env файл."
        )
    import anthropic
    return anthropic.AsyncAnthropic(api_key=api_key)


# ─── Схемы ────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    history: Optional[List[dict]] = []


class CompareRequest(BaseModel):
    period: str
    force: bool = False
    data: dict  # {managers: [{name, revenue, conv, leads, deals, slitted, avgCheck, bestSrc}]}


# ─── POST /api/v1/ai/chat ─────────────────────────────────────────────────────

@router.post("/ai/chat")
async def ai_chat(req: ChatRequest, db: AsyncSession = Depends(get_db)):
    """
    AI-чат аналитик. Отвечает на вопросы о продажах.
    Если передан session_id — добавляет контекст из аналитики.
    """
    client = _get_client()

    # Контекст из аналитики текущей сессии
    context_block = ""
    if req.session_id:
        try:
            sess_uuid = uuid.UUID(req.session_id)
            cache_res = await db.execute(
                select(AnalyticsCache).where(AnalyticsCache.session_id == sess_uuid)
            )
            cache = cache_res.scalar_one_or_none()

            sess = await db.get(Session, sess_uuid)

            if cache and cache.kpi and sess:
                kpi = cache.kpi
                insights = cache.insights or []
                grouped = cache.grouped_sources or []

                top5 = sorted(grouped, key=lambda x: x.get("revenue") or 0, reverse=True)[:5]
                src_text = "; ".join(
                    f"{s.get('group', s.get('src', '?'))}: {s.get('revenue', 0):,.0f}₸ (конв {s.get('conv', 0):.1f}%)"
                    for s in top5
                )

                insight_text = " | ".join(
                    f"{i.get('title', '')}: {i.get('text', '')}"
                    for i in insights[:3]
                )

                context_block = f"""
=== Аналитика за {sess.period} ===
Выручка: {kpi.get('revenue', 0):,.0f} ₸ | Лиды: {kpi.get('leads', 0)} | Сделки: {kpi.get('deals', 0)}
Конверсия: {kpi.get('conv', 0):.1f}% | Средний чек: {kpi.get('avg_check', 0):,.0f} ₸
Слито: {kpi.get('slitted', 0)} лидов
План: {kpi.get('plan', 0):,.0f} ₸ | Выполнение: {kpi.get('plan_pct_company', 0) or kpi.get('plan_pct_adv', 0) or 0:.1f}%
Топ источники: {src_text}
Инсайты: {insight_text}
"""
        except Exception:
            pass

    system = f"""Ты AI-аналитик отдела продаж TorgStore.
Помогаешь руководителю анализировать данные продаж, находить точки роста и проблемы.
Отвечай чётко, кратко, на русском языке. Используй конкретные цифры из данных.
Если данных нет — честно скажи об этом.
{context_block}"""

    messages = []
    for h in (req.history or [])[-6:]:
        role = h.get("role", "")
        content = h.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": req.message})

    import anthropic
    response = await client.messages.create(
        model="claude-sonnet-5",
        max_tokens=1024,
        system=system,
        messages=messages,
    )

    reply = response.content[0].text if response.content else "Не удалось получить ответ"
    return {"reply": reply}


# ─── POST /api/v1/ai/compare-analysis ────────────────────────────────────────

@router.post("/ai/compare-analysis")
async def compare_analysis(req: CompareRequest):
    """
    Сравнительный AI-анализ нескольких менеджеров за период.
    Кэшируется на 1 час (по хэшу данных). force=True сбрасывает кэш.
    """
    client = _get_client()

    managers = req.data.get("managers", [])
    if not managers:
        return {"analysis": "Нет данных для анализа", "cached": False}

    # Кэш
    cache_key = _cache_key("compare", f"{req.period}:{json.dumps(managers, sort_keys=True)}")
    if not req.force and cache_key in _ai_cache:
        entry = _ai_cache[cache_key]
        if datetime.now() - entry["ts"] < timedelta(seconds=_CACHE_TTL):
            return {"analysis": entry["text"], "cached": True}

    # Формируем таблицу менеджеров
    lines = []
    for m in managers:
        lines.append(
            f"• {m.get('name','?')}: выручка {m.get('revenue','—')}, "
            f"конв {m.get('conv','—')}%, "
            f"лиды {m.get('leads','—')}, сделки {m.get('deals','—')}, "
            f"слито {m.get('slitted','—')}, "
            f"ср.чек {m.get('avgCheck','—')}, "
            f"лучший источник: {m.get('bestSrc','—')}"
        )

    mgr_block = "\n".join(lines)

    prompt = f"""Проанализируй результаты менеджеров отдела продаж TorgStore за {req.period}:

{mgr_block}

Составь структурированный анализ:
1. **Краткая оценка каждого менеджера** (2-3 предложения, с цифрами)
2. **Топ-3 инсайта по отделу** — что работает хорошо, что плохо
3. **Конкретные рекомендации** — что делать на следующий период

Пиши профессионально, по-русски, с конкретными цифрами."""

    import anthropic
    response = await client.messages.create(
        model="claude-sonnet-5",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text if response.content else "Анализ недоступен"
    _cache_set(cache_key, text)

    return {"analysis": text, "cached": False}


# ─── POST /api/v1/ai/report/{session_id} ─────────────────────────────────────

@router.post("/ai/report/{session_id}")
async def ai_report(session_id: str, db: AsyncSession = Depends(get_db)):
    """
    Генерирует аналитический отчёт по конкретной сессии.
    Использует данные из analytics_cache.
    """
    client = _get_client()

    try:
        sess_uuid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверный session_id")

    sess = await db.get(Session, sess_uuid)
    if not sess:
        raise HTTPException(status_code=404, detail="Сессия не найдена")

    cache_res = await db.execute(
        select(AnalyticsCache).where(AnalyticsCache.session_id == sess_uuid)
    )
    cache = cache_res.scalar_one_or_none()

    if not cache or not cache.kpi:
        return {"report": None, "period": sess.period}

    kpi = cache.kpi
    insights = cache.insights or []
    grouped = cache.grouped_sources or []
    anomalies = cache.anomalies or []

    # Топ-5 источников
    top5 = sorted(grouped, key=lambda x: x.get("revenue") or 0, reverse=True)[:5]
    src_lines = "\n".join(
        f"  • {s.get('group', s.get('src', '?'))}: "
        f"{s.get('revenue', 0):,.0f} ₸, конв {s.get('conv', 0):.1f}%, "
        f"лиды {s.get('leads', 0)}, сделки {s.get('deals', 0)}"
        for s in top5
    )

    # Инсайты
    insight_lines = "\n".join(
        f"  • [{i.get('type','?')}] {i.get('title','')}: {i.get('text','')}"
        for i in insights[:5]
    )

    # Аномалии
    anomaly_lines = "\n".join(
        f"  • {a.get('text', '')}"
        for a in anomalies[:3]
    ) if anomalies else "  Аномалий не обнаружено"

    plan_pct = kpi.get("plan_pct_company") or kpi.get("plan_pct_adv") or 0

    prompt = f"""Напиши профессиональный аналитический отчёт по рекламным продажам TorgStore за {sess.period}.

ДАННЫЕ:
Выручка: {kpi.get('revenue', 0):,.0f} ₸
Лиды: {kpi.get('leads', 0)} | Сделки: {kpi.get('deals', 0)} | Слито: {kpi.get('slitted', 0)}
Конверсия: {kpi.get('conv', 0):.2f}% | Средний чек: {kpi.get('avg_check', 0):,.0f} ₸
План: {kpi.get('plan', 0):,.0f} ₸ | Выполнение плана: {plan_pct:.1f}%
Выручка компании: {kpi.get('company_revenue', 0):,.0f} ₸

ТОП-5 ИСТОЧНИКОВ:
{src_lines}

ИНСАЙТЫ СИСТЕМЫ:
{insight_lines if insight_lines else '  Нет данных'}

АНОМАЛИИ:
{anomaly_lines}

Напиши отчёт (250-350 слов) в деловом стиле:
1. Общая оценка периода (2-3 предложения)
2. Ключевые достижения
3. Проблемные зоны и потери
4. Рекомендации на следующий период (3-4 конкретных действия)

Используй числа. Пиши по-русски."""

    import anthropic
    response = await client.messages.create(
        model="claude-sonnet-5",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )

    report_text = response.content[0].text if response.content else ""
    return {"report": report_text, "period": sess.period}


# ─── «Командный центр»: короткий ИИ-дайджест рейтинга ────────────────────────

class LeaderboardDigestRequest(BaseModel):
    period: str
    force: bool = False


@router.post("/ai/leaderboard-digest")
async def leaderboard_digest(req: LeaderboardDigestRequest, db: AsyncSession = Depends(get_db)):
    """
    Короткий (3-4 предложения) дайджест рейтинга менеджеров: кто лучший,
    кто быстрее всех растёт, на кого стоит обратить внимание. НЕ полный
    отчёт как /ai/report — сознательно короче и заточен под "Командный центр".
    """
    client = _get_client()

    snapshot = await team_center.build_period_snapshot(db, req.period)
    managers = snapshot["managers"]
    if not managers:
        return {"digest": None, "cached": False}

    cache_key = _cache_key(
        "leaderboard_digest",
        f"{req.period}:" + json.dumps(
            [{"id": m["id"], "composite": m["composite"], "status": m["status"],
              "revDeltaPct": m["revDeltaPct"]} for m in managers],
            sort_keys=True,
        ),
    )
    if not req.force and cache_key in _ai_cache:
        entry = _ai_cache[cache_key]
        if datetime.now() - entry["ts"] < timedelta(seconds=_CACHE_TTL):
            return {"digest": entry["text"], "cached": True}

    best = max(managers, key=lambda m: m["composite"])
    with_delta = [m for m in managers if m.get("revDeltaPct") is not None]
    fastest = max(with_delta, key=lambda m: m["revDeltaPct"]) if with_delta else None
    risk_list = sorted([m for m in managers if m["status"] == "risk"], key=lambda m: m["composite"])
    watch_list = sorted([m for m in managers if m["status"] == "watch"], key=lambda m: m["composite"])
    attention = (risk_list or watch_list or [None])[0]

    lines = [f"Лидер рейтинга: {best['name']} — composite {best['composite']}/100, статус «{best['status']}»."]
    if fastest and fastest["id"] != best["id"]:
        lines.append(f"Быстрее всех растёт {fastest['name']}: выручка {fastest['revDeltaPct']:+.0f}% к прошлому периоду.")
    if attention:
        lines.append(f"Стоит обратить внимание на {attention['name']}: {attention['statusReason']}.")
    context_block = "\n".join(lines)

    prompt = f"""Ты AI-аналитик отдела продаж TorgStore. Вот сводка по рейтингу менеджеров за {req.period}:

{context_block}

Напиши короткий дайджест (3-4 предложения) для руководителя: кто лучший, кто быстрее всех растёт,
на кого стоит обратить внимание. Тон — наблюдение, а не приговор: например
«конверсия падает при сохранении объёма лидов», а не «менеджер плохо работает».
Пиши по-русски, используй конкретные цифры из сводки выше, не выдумывай новых."""

    response = await client.messages.create(
        model="claude-sonnet-5",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text if response.content else "Дайджест недоступен"
    _cache_set(cache_key, text)
    return {"digest": text, "cached": False}


# ─── «Командный центр»: короткая ИИ-рекомендация по одному менеджеру ─────────

class ManagerRecommendationRequest(BaseModel):
    manager_id: str
    period: str
    force: bool = False


@router.post("/ai/manager-recommendation")
async def manager_recommendation(req: ManagerRecommendationRequest, db: AsyncSession = Depends(get_db)):
    """
    Короткая (2-3 предложения) рекомендация для ОДНОГО менеджера — скоуп
    строго его найденные сильные/слабые стороны, не общий совет по отделу.
    """
    client = _get_client()

    snapshot = await team_center.build_period_snapshot(db, req.period)
    m = next((x for x in snapshot["managers"] if x["id"] == req.manager_id), None)
    if not m:
        raise HTTPException(
            status_code=404,
            detail=f"Нет данных по менеджеру за период «{req.period}»",
        )

    good, bad = team_center.build_strengths_and_growth(
        m, snapshot["dept"]["avgConv"], snapshot["dept"]["deptAvgCheck"]
    )

    cache_key = _cache_key(
        "mgr_reco", f"{req.manager_id}:{req.period}:{m['composite']}:" + "|".join(bad)
    )
    if not req.force and cache_key in _ai_cache:
        entry = _ai_cache[cache_key]
        if datetime.now() - entry["ts"] < timedelta(seconds=_CACHE_TTL):
            return {"recommendation": entry["text"], "cached": True}

    prompt = f"""Менеджер {m['name']}, период {req.period}. Статус: {m['status']} ({m['statusReason']}).
Сильные стороны: {'; '.join(good)}
Зоны роста: {'; '.join(bad)}

Дай короткую (2-3 предложения) рекомендацию именно для этого менеджера — что конкретно
стоит сделать в следующем периоде. Тон — наблюдение и конкретное действие, а не оценка
личности или приговор («конверсия падает при сохранении объёма лидов, стоит прослушать
звонки за 2 недели» — хорошо; «менеджер плохо работает» — плохо).
Пиши по-русски, опирайся только на данные выше, не выдумывай новых фактов."""

    response = await client.messages.create(
        model="claude-sonnet-5",
        max_tokens=250,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text if response.content else "Рекомендация недоступна"
    _cache_set(cache_key, text)
    return {"recommendation": text, "cached": False}
