"""
«Доклад» — генерация и жизненный цикл управленческих решений.

Источники истины (не переписывать логику, только читать):
  ux_architecture.md, interaction_model.md, causal_model_constitution.md,
  product_vision.md, domain_model.md — в корне проекта.

ЧЕСТНОЕ ОГРАНИЧЕНИЕ V1 (см. также migrations/008_add_decisions.sql):
эта система не имеет write-доступа к CRM (torgstore.zymyran.com используется
только на чтение). Значит она не может сама выполнить предложенное действие
(например, реально перераспределить лиды) — только предложить его человеку.
Поэтому:
  - requires_explicit_confirmation всегда True в generate-логике; полей
    default_window_ends_at эта версия не проставляет — они зарезервированы
    в схеме на случай, если появится write-актуатор.
  - proposed_action.type == 'investigate' для всех решений v1 — это
    единственное действие, которое честно отражает, что умеет система:
    указать, что и почему требует внимания человека, а не "нажми кнопку —
    сделаем сами".
  - «изменить параметр» (конечный ход из interaction_model.md) в v1
    применяется к success_criteria (окно проверки/целевое значение) — это
    единственный параметр, которым в этой версии реально можно управлять.

Ничего из этого не придумывает новых причин или объяснений — вся причинность
уже посчитана в backend/analytics/team_center.py (_status_and_reason,
build_strengths_and_growth) и backend/analytics/engine.py (compute_anomalies,
compute_insights). Этот модуль только упаковывает уже проверенные выводы в
формат «решение» и управляет их жизненным циклом.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.models import Decision, DecisionEvent, Setting, Manager
from backend.analytics import team_center
from backend.routers.imports import parse_period

# ── Константы бюджета/материальности (Причинно-следственная модель) ────────
# Пока без UI для настройки — намеренно, чтобы не строить экран ради двух
# чисел раньше, чем появится реальная потребность их часто менять (калибровка
# масштаба). Если понадобится — вынести в settings (таблица уже есть).
MATERIALITY_MIN_TENGE = 50_000     # ниже — сигнал существует, но решение не порождается
DAILY_BUDGET = 5                    # максимум НОВЫХ решений в одном докладе
DEFAULT_VERIFY_WINDOW_DAYS = 14

OPEN_LIKE_STATUSES = ("open", "postponed", "verifying")
TERMINAL_STATUSES = (
    "rejected", "confirmed_worked", "confirmed_failed",
    "inconclusive", "insufficient_data", "superseded",
)


def _now():
    return datetime.now(timezone.utc)


async def _log_event(db: AsyncSession, decision_id, event_type: str, note: dict):
    db.add(DecisionEvent(decision_id=decision_id, event_type=event_type, note=note or {}))


async def _rule_suspended(db: AsyncSession, signal_type: str) -> Optional[dict]:
    """Стресс-тест Interaction Model, п.4: один подтверждённо вредный исход
    приостанавливает дальнейшие решения ЭТОГО ЖЕ правила до пересмотра
    человеком — не ждём статистики повторных провалов. Переиспользуем
    таблицу settings вместо новой сущности."""
    key = f"decision_rule_suspended:{signal_type}"
    row = await db.get(Setting, key)
    return row.value if row else None


# ── Расчёт материальности и типа сигнала (ничего не придумываем — берём то,
#    что уже посчитал team_center._status_and_reason) ───────────────────────

def _money_impact_and_signal(m: dict) -> tuple[float, str, str]:
    """Возвращает (money_impact, signal_type, basis) — basis для narrative."""
    plan = m.get("plan") or 0
    plan_pct = m.get("planPct")
    total_rev = m.get("totalRevenue") or 0
    gap_revenue = m.get("gapRevenue") or 0

    plan_shortfall = (plan - total_rev) if (plan and plan_pct is not None and plan_pct < 100) else 0
    plan_shortfall = max(0.0, plan_shortfall)

    candidates = [
        ("plan_shortfall", plan_shortfall),
        ("conversion_gap", gap_revenue if gap_revenue > 0 else 0.0),
    ]
    # Аномалия без числовой базы — если это единственный триггер риска,
    # оцениваем по той же формуле, что и insight ② (engine.py compute_insights):
    # потерянные лиды источника × его средний чек.
    if m.get("anomalies"):
        best_anomaly_impact = 0.0
        for a in m["anomalies"]:
            if a.get("cls") != "danger":
                continue
            row = next((g for g in m["grouped"] if g.get("group") == a.get("group")), None)
            if row and row.get("leads") and row.get("conv") is not None:
                lost = float(row["leads"]) - float(row.get("deals") or 0)
                avg_ck = row.get("avg_check") or m.get("avgCheck") or 0
                best_anomaly_impact = max(best_anomaly_impact, lost * avg_ck)
        candidates.append(("data_anomaly", best_anomaly_impact))

    signal_type, money_impact = max(candidates, key=lambda c: c[1])
    basis = {
        "plan_shortfall": f"недобор до плана {plan:,.0f} ₸".replace(",", " "),
        "conversion_gap": "разрыв конверсии к среднему по отделу",
        "data_anomaly": "аномалия по источнику",
    }[signal_type]
    return money_impact, signal_type, basis


# ── Narrative — «рассказ» (Interaction Model, п.3) ──────────────────────────

def _build_narrative(m: dict, signal_type: str, ctx_avg_conv: float, prior_attempt: Optional[dict]) -> dict:
    versions = []
    conv_now = m.get("conv")
    conv_prev = m.get("convPrev")
    conv_delta = (conv_now - conv_prev) if (conv_now is not None and conv_prev is not None) else None

    if signal_type == "conversion_gap":
        steady = conv_delta is not None and conv_delta <= -5
        versions.append({
            "text": "Объём лидов тоже упал — тогда падение конверсии может быть просто эффектом малой базы, а не реальной проблемой",
            "status": "ruled_out" if steady else "not_checked",
        })
        versions.append({
            "text": f"Конверсия ниже среднего по отделу ({ctx_avg_conv:.1f}%) при сохранении объёма лидов",
            "status": "confirmed",
        })
        noticed = f"{m['name']}: конверсия {conv_now:.1f}%" + (f" против {conv_prev:.1f}% в прошлом периоде" if conv_prev is not None else "") + f", при среднем по отделу {ctx_avg_conv:.1f}%"
        proof = m.get("statusReason") or noticed
        proposed = "Разобрать слитые лиды за период вместе с менеджером — конкретные сделки, а не общий разговор"
        expected = f"Конверсия возвращается к уровню отдела (~{ctx_avg_conv:.1f}%) в течение окна проверки"

    elif signal_type == "plan_shortfall":
        plan_pct = m.get("planPct") or 0
        versions.append({
            "text": "Это временный провал одного периода, план обычно закрывается позже в месяце",
            "status": "ruled_out" if plan_pct < 70 else "not_checked",
        })
        versions.append({
            "text": f"Выполнение плана {plan_pct:.0f}% — отставание от графика подтверждено по факту накладных/рекламы",
            "status": "confirmed",
        })
        noticed = f"{m['name']}: план выполнен на {plan_pct:.0f}%"
        proof = m.get("statusReason") or noticed
        proposed = "Проверить темп по неделям и причину отставания — источник лидов, загрузка менеджера или сезонность"
        expected = "Темп выравнивается до ≥90% выполнения плана к концу окна проверки"

    else:  # data_anomaly
        danger = [a for a in (m.get("anomalies") or []) if a.get("cls") == "danger"]
        msg = danger[0]["msg"] if danger else "обнаружено отклонение в данных по источнику"
        versions.append({"text": "Это ошибка данных импорта, а не реальная проблема", "status": "not_checked"})
        versions.append({"text": msg, "status": "confirmed"})
        noticed = f"{m['name']}: {msg}"
        proof = msg
        proposed = "Проверить конкретные сделки по этому источнику — плохие лиды или простой менеджера"
        expected = "Аномалия не повторяется в следующем снимке данных"

    narrative = {
        "noticed": noticed,
        "versions_considered": versions,
        "confirmed_explanation": proof,
        "proof": proof,
        "proposed": proposed,
        "expected_effect": expected,
    }
    if prior_attempt:
        narrative["prior_attempt"] = prior_attempt
    return narrative


async def _prior_attempt(db: AsyncSession, precedent_key: str) -> Optional[dict]:
    """Память о судьбе прошлых решений по этому же вопросу — находка №6
    аудита / Interaction Model п.5 («причина уже учтена в следующий раз»)."""
    result = await db.execute(
        select(Decision)
        .where(Decision.precedent_key == precedent_key, Decision.status.in_(TERMINAL_STATUSES))
        .order_by(Decision.created_at.desc())
        .limit(1)
    )
    prior = result.scalar_one_or_none()
    if not prior:
        return None
    return {
        "period": prior.period,
        "outcome": prior.status,
        "reason": prior.response_reason,
        "summary": prior.title,
    }


# ── Генерация ────────────────────────────────────────────────────────────

async def generate_decisions_for_period(db: AsyncSession, period: str) -> list[Decision]:
    try:
        year, month = parse_period(period)
    except ValueError:
        return []

    snapshot = await team_center.build_period_snapshot(db, period)
    created: list[Decision] = []

    for m in snapshot["managers"]:
        if m["status"] == "good":
            continue

        money_impact, signal_type, _basis = _money_impact_and_signal(m)
        if money_impact < MATERIALITY_MIN_TENGE:
            continue

        if await _rule_suspended(db, signal_type):
            continue  # правило приостановлено после подтверждённо вредного исхода (п.4)

        precedent_key = f"manager:{m['id']}:{signal_type}"

        existing = await db.execute(
            select(Decision).where(
                Decision.precedent_key == precedent_key,
                Decision.status.in_(OPEN_LIKE_STATUSES),
            )
        )
        if existing.scalar_one_or_none():
            continue  # уже есть открытое решение по этому же вопросу — не дублируем

        # Не переоткрываем в тот же период то, что уже отклонили в этот же период.
        same_period_rejected = await db.execute(
            select(Decision).where(
                Decision.precedent_key == precedent_key,
                Decision.period == period,
                Decision.status == "rejected",
            )
        )
        if same_period_rejected.scalar_one_or_none():
            continue

        prior = await _prior_attempt(db, precedent_key)
        narrative = _build_narrative(m, signal_type, snapshot["dept"]["avgConv"], prior)

        title = f"{m['name']} — {narrative['noticed'].split(': ', 1)[-1]} (~{money_impact:,.0f} ₸)".replace(",", " ")

        decision = Decision(
            id=uuid.uuid4(),
            subject_type="manager",
            subject_id=uuid.UUID(m["id"]),
            subject_label=m["name"],
            period=period, period_year=year, period_month=month,
            signal_type=signal_type,
            title=title,
            money_impact=money_impact,
            narrative=narrative,
            proposed_action={"type": "investigate", "summary": narrative["proposed"]},
            success_criteria={
                "metric": signal_type,
                "window_days": DEFAULT_VERIFY_WINDOW_DAYS,
                "expected": narrative["expected_effect"],
            },
            reversible=True,
            requires_explicit_confirmation=True,   # v1: всегда — см. докстринг файла
            status="open",
            precedent_key=precedent_key,
        )
        db.add(decision)
        await db.flush()
        await _log_event(db, decision.id, "created", {"money_impact": money_impact, "signal_type": signal_type})
        created.append(decision)

    await db.commit()
    return created


# ── Разрешение отложенных / самоисчезнувших сигналов ────────────────────────

async def resolve_stale_decisions(db: AsyncSession, period: str) -> None:
    """Вызывается перед каждым чтением /decisions/today — без отдельного
    cron/воркера (соло-разработка, docker-compose без Celery): достаточно
    проверять «лениво», на каждый заход в доклад, а не в реальном времени."""
    now = _now()

    # 1) Отложенные, у которых истёк срок — возвращаются в открытые.
    postponed = await db.execute(
        select(Decision).where(Decision.status == "postponed", Decision.postponed_until <= now)
    )
    for d in postponed.scalars().all():
        d.status = "open"
        await _log_event(db, d.id, "resumed", {"from": "postponed"})

    # 2) Открытые, чей сигнал по свежим данным больше не подтверждается —
    #    закрываются сами, без участия человека (Interaction Model, стресс-тест п.2).
    snapshot = await team_center.build_period_snapshot(db, period)
    status_by_mgr = {m["id"]: m["status"] for m in snapshot["managers"]}
    open_decisions = await db.execute(
        select(Decision).where(Decision.status == "open", Decision.period == period)
    )
    for d in open_decisions.scalars().all():
        current = status_by_mgr.get(str(d.subject_id))
        if current == "good":
            d.status = "superseded"
            await _log_event(db, d.id, "superseded", {"reason": "сигнал вернулся к норме без вмешательства"})

    await db.commit()


# ── Чтение: сегодняшний доклад ──────────────────────────────────────────────

async def get_today_decisions(db: AsyncSession, period: str, budget: int = DAILY_BUDGET) -> dict:
    await generate_decisions_for_period(db, period)
    await resolve_stale_decisions(db, period)

    now = _now()

    due_for_verification = await db.execute(
        select(Decision)
        .where(Decision.status == "verifying", Decision.verify_after <= now)
        .order_by(Decision.money_impact.desc())
    )
    verification_items = list(due_for_verification.scalars().all())

    open_q = await db.execute(
        select(Decision)
        .where(Decision.status == "open")
        .order_by(Decision.money_impact.desc())
    )
    all_open = list(open_q.scalars().all())
    today_open = all_open[:budget]
    remaining_open = max(0, len(all_open) - len(today_open))

    # build_period_snapshot заново не пересчитывает тяжёлую часть — читает
    # уже прогретый AnalyticsCache (см. team_center._analytics_cache) —
    # достаточно дёшево для одного вызова на открытие доклада.
    snapshot = await team_center.build_period_snapshot(db, period)
    n_managers_checked = len(snapshot["managers"])

    return {
        "period": period,
        "checked_at": now.isoformat(),
        "managers_checked": n_managers_checked,
        "verification": [serialize_decision(d) for d in verification_items],
        "decisions": [serialize_decision(d) for d in today_open],
        "remaining_open": remaining_open,
        "is_empty": not verification_items and not today_open,
    }


def serialize_decision(d: Decision) -> dict:
    return {
        "id": str(d.id),
        "subject_type": d.subject_type,
        "subject_id": str(d.subject_id) if d.subject_id else None,
        "subject_label": d.subject_label,
        "period": d.period,
        "signal_type": d.signal_type,
        "title": d.title,
        "money_impact": float(d.money_impact or 0),
        "narrative": d.narrative,
        "proposed_action": d.proposed_action,
        "success_criteria": d.success_criteria,
        "reversible": d.reversible,
        "requires_explicit_confirmation": d.requires_explicit_confirmation,
        "status": d.status,
        "response_reason": d.response_reason,
        "verify_after": d.verify_after.isoformat() if d.verify_after else None,
        "actual_effect": float(d.actual_effect) if d.actual_effect is not None else None,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


async def get_decision(db: AsyncSession, decision_id: uuid.UUID) -> Optional[Decision]:
    return await db.get(Decision, decision_id)


async def get_subject_history(db: AsyncSession, subject_type: str, subject_id: uuid.UUID,
                               exclude_id: Optional[uuid.UUID] = None, limit: int = 8) -> list[dict]:
    q = select(Decision).where(
        Decision.subject_type == subject_type, Decision.subject_id == subject_id,
    ).order_by(Decision.created_at.desc()).limit(limit)
    result = await db.execute(q)
    rows = [d for d in result.scalars().all() if d.id != exclude_id]
    return [serialize_decision(d) for d in rows]


# ── Ответ на решение (конечный набор ходов — interaction_model.md) ─────────

async def respond_to_decision(db: AsyncSession, decision_id: uuid.UUID, action: str,
                               reason: Optional[str] = None, params: Optional[dict] = None,
                               postpone_until: Optional[datetime] = None) -> Decision:
    decision = await db.get(Decision, decision_id)
    if not decision:
        raise ValueError("Решение не найдено")
    if decision.status != "open":
        raise ValueError(f"Решение уже в статусе «{decision.status}» — повторный ответ невозможен")

    now = _now()

    if action == "accept":
        decision.status = "verifying"
        decision.decided_at = now
        window = (decision.success_criteria or {}).get("window_days", DEFAULT_VERIFY_WINDOW_DAYS)
        decision.verify_after = now + timedelta(days=window)

    elif action == "modify":
        if not params:
            raise ValueError("Для 'изменить параметр' нужно передать params")
        # v1: единственный реально изменяемый параметр — success_criteria
        # (окно проверки/целевое значение) — см. докстринг файла.
        sc = dict(decision.success_criteria or {})
        if "window_days" in params:
            sc["window_days"] = int(params["window_days"])
        if "expected" in params:
            sc["expected"] = params["expected"]
        decision.success_criteria = sc
        decision.response_params = params
        decision.status = "verifying"
        decision.decided_at = now
        decision.verify_after = now + timedelta(days=sc.get("window_days", DEFAULT_VERIFY_WINDOW_DAYS))

    elif action == "reject":
        if not reason or not reason.strip():
            raise ValueError("Отклонение требует причины (interaction_model.md, п.5)")
        decision.status = "rejected"
        decision.response_reason = reason.strip()
        decision.decided_at = now

    elif action == "postpone":
        if not postpone_until:
            raise ValueError("Отложить нужно на конкретный срок")
        decision.status = "postponed"
        decision.postponed_until = postpone_until
        decision.decided_at = now

    else:
        raise ValueError(f"Неизвестный ход: {action!r}")

    await _log_event(db, decision.id, "responded", {"action": action, "reason": reason, "params": params})
    await db.commit()
    await db.refresh(decision)
    return decision


async def verify_decision(db: AsyncSession, decision_id: uuid.UUID, outcome: str,
                           actual_effect: Optional[float] = None) -> Decision:
    # insufficient_data — не то же самое, что inconclusive. inconclusive: сравнение
    # «было/стало» честно проведено, но по факту не даёт уверенного ответа. insufficient_data —
    # само сравнение нельзя было честно провести (см. causal_model_constitution.md,
    # «Ограничения модели»): статистически мало данных, чтобы отличить отклонение от
    # случайности; сигнал возник во время/сразу после структурного изменения; конкурирующие
    # версии неразличимы доступными способами проверки; либо признаки противоречат друг
    # другу настолько, что ни одна версия не проходит проверку целиком. До этой правки
    # значение существовало только в TERMINAL_STATUSES и во фронтенд-словаре меток, но
    # никогда не принималось здесь — честный исход был физически недостижим.
    if outcome not in ("confirmed_worked", "confirmed_failed", "inconclusive", "insufficient_data"):
        raise ValueError(f"Неизвестный исход проверки: {outcome!r}")

    decision = await db.get(Decision, decision_id)
    if not decision:
        raise ValueError("Решение не найдено")
    if decision.status != "verifying":
        raise ValueError(f"Решение в статусе «{decision.status}» — проверка результата невозможна")

    decision.status = outcome
    decision.verified_at = _now()
    decision.actual_effect = actual_effect
    await _log_event(db, decision.id, "verified", {"outcome": outcome, "actual_effect": actual_effect})

    if outcome == "confirmed_failed":
        # Interaction Model, стресс-тест п.4: один подтверждённо вредный исход
        # немедленно приостанавливает дальнейшие решения этого же правила —
        # не ждём накопления статистики провалов.
        key = f"decision_rule_suspended:{decision.signal_type}"
        existing = await db.get(Setting, key)
        payload = {
            "suspended_at": _now().isoformat(),
            "reason": f"Решение по «{decision.subject_label}» ({decision.period}) подтверждено как вредное",
            "decision_id": str(decision.id),
        }
        if existing:
            existing.value = payload
        else:
            db.add(Setting(key=key, value=payload, description="Автоматически приостановлено после вредного исхода"))

    await db.commit()
    await db.refresh(decision)
    return decision


async def unsuspend_rule(db: AsyncSession, signal_type: str) -> None:
    key = f"decision_rule_suspended:{signal_type}"
    existing = await db.get(Setting, key)
    if existing:
        await db.delete(existing)
        await db.commit()


async def list_suspended_rules(db: AsyncSession) -> list[dict]:
    result = await db.execute(select(Setting).where(Setting.key.like("decision_rule_suspended:%")))
    return [{"signal_type": s.key.split(":", 1)[1], **(s.value or {})} for s in result.scalars().all()]
