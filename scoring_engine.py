"""
DebtScoring Engine v1.0
=======================
Движок скоринга дебиторской задолженности.
Работает полностью локально — никакие данные не покидают контур клиента.
Вызывается из C++ Native API AddIn через process_request(json_string).

Зависимости: ТОЛЬКО стандартная библиотека Python 3.8+
"""

import json
import statistics
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional, Any


# ─────────────────────────────────────────────────────────────────────────────
# КОНСТАНТЫ
# ─────────────────────────────────────────────────────────────────────────────

VERSION = "1.0.0"

# Веса факторов в итоговом скоринге (сумма abs = 1.0)
WEIGHTS = {
    "avg_delay":    -0.30,   # Средняя просрочка: чем больше — тем хуже
    "max_delay":    -0.20,   # Максимальная просрочка за историю
    "regularity":   +0.20,   # Доля платежей вовремя (±3 дня)
    "debt_ratio":   -0.15,   # Текущий долг / общий оборот
    "trend":        +0.10,   # Тренд: платит лучше (+) или хуже (-)
    "experience":   +0.05,   # Количество сделок (опытный контрагент)
}

RISK_THRESHOLDS = {
    "CRITICAL":  (0,  25),
    "HIGH":      (25, 40),
    "MEDIUM":    (40, 55),
    "ACCEPTABLE":(55, 70),
    "LOW":       (70, 101),
}

RISK_LABELS = {
    "CRITICAL":   "Критический",
    "HIGH":       "Высокий",
    "MEDIUM":     "Требует внимания",
    "ACCEPTABLE": "Приемлемый",
    "LOW":        "Надёжный",
}

RISK_COLORS = {
    "CRITICAL":   "#FF3B30",
    "HIGH":       "#FF9500",
    "MEDIUM":     "#FFCC00",
    "ACCEPTABLE": "#34C759",
    "LOW":        "#007AFF",
}


# ─────────────────────────────────────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ─────────────────────────────────────────────────────────────────────────────

def _parse_date(value: Any) -> Optional[date]:
    """Парсит дату из строки ISO формата или None."""
    if value is None or value == "" or value == "null":
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _clamp(value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    return max(min_val, min(max_val, value))


def _mean_safe(values: list) -> float:
    return statistics.mean(values) if values else 0.0


def _stdev_safe(values: list) -> float:
    return statistics.stdev(values) if len(values) >= 2 else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# НОРМАЛИЗАЦИЯ ВХОДНЫХ ДАННЫХ
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_records(payment_history: List[Dict]) -> List[Dict]:
    """
    Принимает сырые данные из 1С и добавляет вычисляемые поля.

    Входной формат каждой записи:
    {
        "invoice_date":  "2024-01-15",   # Дата счёта
        "due_date":      "2024-02-15",   # Плановая дата оплаты
        "payment_date":  "2024-02-20",   # Фактическая дата оплаты (None = не оплачено)
        "amount":        150000.0,        # Сумма счёта
        "paid_amount":   150000.0         # Сумма фактической оплаты
    }
    """
    today = date.today()
    result = []

    for r in payment_history:
        due_date     = _parse_date(r.get("due_date"))
        payment_date = _parse_date(r.get("payment_date"))
        amount       = float(r.get("amount", 0) or 0)
        paid_amount  = float(r.get("paid_amount", 0) or 0)

        if due_date is None:
            continue  # пропускаем записи без даты оплаты

        is_paid   = payment_date is not None and paid_amount >= amount * 0.99
        is_partial = payment_date is not None and 0 < paid_amount < amount * 0.99

        if is_paid:
            delay_days = max(0, (payment_date - due_date).days)
        elif is_partial:
            delay_days = max(0, (today - due_date).days)
        else:
            # Не оплачен совсем
            delay_days = max(0, (today - due_date).days) if today > due_date else 0

        overdue_amount = max(0.0, amount - paid_amount)
        is_overdue     = not is_paid and today > due_date

        result.append({
            "invoice_date":    _parse_date(r.get("invoice_date")),
            "due_date":        due_date,
            "payment_date":    payment_date,
            "amount":          amount,
            "paid_amount":     paid_amount,
            "overdue_amount":  overdue_amount,
            "delay_days":      delay_days,
            "is_paid":         is_paid,
            "is_partial":      is_partial,
            "is_overdue":      is_overdue,
        })

    # Сортируем по дате счёта
    result.sort(key=lambda x: x["due_date"])
    return result


# ─────────────────────────────────────────────────────────────────────────────
# ВЫЧИСЛЕНИЕ ФАКТОРОВ
# ─────────────────────────────────────────────────────────────────────────────

def _factor_avg_delay(records: List[Dict]) -> float:
    """Средняя просрочка по оплаченным счетам (дни)."""
    paid = [r["delay_days"] for r in records if r["is_paid"]]
    return _mean_safe(paid)


def _factor_max_delay(records: List[Dict]) -> float:
    """Максимальная просрочка за всю историю (дни)."""
    delays = [r["delay_days"] for r in records]
    return max(delays) if delays else 0.0


def _factor_regularity(records: List[Dict]) -> float:
    """Доля счетов, оплаченных вовремя (просрочка ≤ 3 дня). 0.0–1.0."""
    paid = [r for r in records if r["is_paid"]]
    if not paid:
        return 0.0
    on_time = sum(1 for r in paid if r["delay_days"] <= 3)
    return on_time / len(paid)


def _factor_debt_ratio(records: List[Dict]) -> float:
    """Доля текущего просроченного долга от общего оборота. 0.0–1.0+."""
    total_overdue = sum(r["overdue_amount"] for r in records if r["is_overdue"])
    total_amount  = sum(r["amount"] for r in records)
    if total_amount == 0:
        return 0.0
    return total_overdue / total_amount


def _factor_trend(records: List[Dict]) -> float:
    """
    Тренд платёжной дисциплины: +1.0 = стало лучше, -1.0 = стало хуже.
    Сравниваем первую и вторую половину истории.
    """
    paid = [r for r in records if r["is_paid"]]
    if len(paid) < 4:
        return 0.0

    half = len(paid) // 2
    first_half_avg  = _mean_safe([r["delay_days"] for r in paid[:half]])
    second_half_avg = _mean_safe([r["delay_days"] for r in paid[half:]])

    # Положительное значение = просрочка уменьшилась (хорошо)
    diff = first_half_avg - second_half_avg
    return _clamp(diff / 30.0, -1.0, 1.0)


def _factor_experience(records: List[Dict]) -> float:
    """Нормализованное кол-во сделок. 20+ сделок = 1.0."""
    return _clamp(len(records) / 20.0)


# ─────────────────────────────────────────────────────────────────────────────
# НОРМАЛИЗАЦИЯ ФАКТОРОВ → 0.0–1.0
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_factors(raw: Dict[str, float]) -> Dict[str, float]:
    """Приводит сырые значения факторов к диапазону 0.0–1.0."""
    return {
        # Просрочка 0 дней = 1.0, 90+ дней = 0.0
        "avg_delay":   _clamp(1.0 - raw["avg_delay"] / 90.0),
        # Макс просрочка 0 дней = 1.0, 180+ дней = 0.0
        "max_delay":   _clamp(1.0 - raw["max_delay"] / 180.0),
        # Уже 0.0–1.0
        "regularity":  raw["regularity"],
        # Долг 0% = 1.0, 100%+ = 0.0
        "debt_ratio":  _clamp(1.0 - raw["debt_ratio"]),
        # -1..+1 → 0..1
        "trend":       (raw["trend"] + 1.0) / 2.0,
        # Уже 0.0–1.0
        "experience":  raw["experience"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# ИТОГОВЫЙ СКОРИНГ
# ─────────────────────────────────────────────────────────────────────────────

def _calculate_raw_score(normalized: Dict[str, float]) -> float:
    """
    Взвешенная сумма нормализованных факторов 0..1
    Каждый фактор нормализован так, что 1.0 = хорошо, 0.0 = плохо.
    Итоговый score = взвешенное среднее × 100.

    Веса уже хранят знак (+/-) для семантики, но нормализованные
    факторы уже отражают «хорошесть», поэтому считаем просто
    взвешенное среднее (sum(w * v) / sum(w)), где все w > 0.
    """
    # Используем абсолютные значения весов как коэффициенты важности.
    # normalized[f] = 1.0 означает «всё хорошо», 0.0 — «всё плохо».
    total_weight = sum(abs(w) for w in WEIGHTS.values())
    weighted_sum = sum(
        normalized[factor] * abs(weight)
        for factor, weight in WEIGHTS.items()
    )
    score = (weighted_sum / total_weight) * 100.0
    return _clamp(score, 0.0, 100.0)


def _get_risk_level(score: float) -> str:
    for level, (low, high) in RISK_THRESHOLDS.items():
        if low <= score < high:
            return level
    return "CRITICAL"


# ─────────────────────────────────────────────────────────────────────────────
# РЕКОМЕНДАЦИИ
# ─────────────────────────────────────────────────────────────────────────────

def _recommended_credit_limit(score: float, records: List[Dict]) -> float:
    """Рекомендуемый кредитный лимит на основе скоринга и среднего счёта."""
    amounts = [r["amount"] for r in records if r["amount"] > 0]
    if not amounts:
        return 0.0
    avg_invoice = _mean_safe(amounts)
    # score 100 → 3 средних счёта, score 0 → 0
    multiplier = (score / 100.0) * 3.0
    limit = avg_invoice * multiplier
    # Округляем до 10 000
    return round(limit / 10_000) * 10_000


def _recommended_delay_days(score: float) -> int:
    """Рекомендуемая отсрочка платежа."""
    if score >= 70: return 30
    if score >= 55: return 14
    if score >= 40: return 7
    return 0  # только предоплата


def _recommended_action(score: float, raw_factors: Dict) -> str:
    """Текстовая рекомендация менеджеру."""
    if score >= 70:
        return "Работать в штатном режиме. Можно увеличить лимит."
    if score >= 55:
        return "Работать стандартно. Контролировать сроки оплаты."
    if score >= 40:
        return "Сократить отсрочку. Усилить контроль дебиторки."
    if score >= 25:
        return "Перейти на предоплату или значительно сократить лимит."
    return "⛔ Рекомендована только предоплата. Рассмотреть судебное взыскание."


# ─────────────────────────────────────────────────────────────────────────────
# АЛЕРТЫ
# ─────────────────────────────────────────────────────────────────────────────

def _generate_alerts(raw_factors: Dict, records: List[Dict]) -> List[str]:
    alerts = []

    if raw_factors["avg_delay"] > 30:
        alerts.append(
            f"⚠️ Средняя просрочка {raw_factors['avg_delay']:.0f} дн. — выше нормы"
        )

    if raw_factors["max_delay"] > 60:
        alerts.append(
            f"🔴 Максимальная просрочка {raw_factors['max_delay']:.0f} дн."
        )

    overdue_records = [r for r in records if r["is_overdue"]]
    if overdue_records:
        total_overdue = sum(r["overdue_amount"] for r in overdue_records)
        count         = len(overdue_records)
        alerts.append(
            f"💸 Текущий просроченный долг: {total_overdue:,.0f} ₽ ({count} счёт(ов))"
        )

    if raw_factors["trend"] < -0.3:
        alerts.append("📉 Платёжная дисциплина ухудшается")

    if raw_factors["regularity"] < 0.5:
        pct = raw_factors["regularity"] * 100
        alerts.append(f"🕐 Вовремя платит только {pct:.0f}% счетов")

    partial = [r for r in records if r["is_partial"]]
    if partial:
        alerts.append(f"⚡ Частичные оплаты: {len(partial)} счёт(ов)")

    if not alerts:
        alerts.append("✅ Нарушений не обнаружено")

    return alerts


# ─────────────────────────────────────────────────────────────────────────────
# ПРОГНОЗ СЛЕДУЮЩЕГО ПЛАТЕЖА
# ─────────────────────────────────────────────────────────────────────────────

def _forecast_next_payment(records: List[Dict]) -> Dict:
    """Прогноз на основе последних 5 платежей."""
    paid = [r for r in records if r["is_paid"]]
    if len(paid) < 3:
        return {
            "available": False,
            "message": "Недостаточно данных (нужно минимум 3 оплаченных счёта)"
        }

    recent = paid[-5:]
    avg_delay = _mean_safe([r["delay_days"] for r in recent])
    std_delay = _stdev_safe([r["delay_days"] for r in recent])

    return {
        "available":       True,
        "expected_delay_days":   round(avg_delay),
        "delay_range_days": [
            max(0, round(avg_delay - std_delay)),
            round(avg_delay + std_delay),
        ],
        "message": (
            f"Ожидаемая задержка ~{round(avg_delay)} дн. "
            f"(диапазон: {max(0, round(avg_delay - std_delay))}–"
            f"{round(avg_delay + std_delay)} дн.)"
        )
    }


# ─────────────────────────────────────────────────────────────────────────────
# ДИНАМИКА СКОРИНГА (история изменений)
# ─────────────────────────────────────────────────────────────────────────────

def _score_history(records: List[Dict]) -> List[Dict]:
    """
    Скользящий скоринг по окнам по 5 сделок.
    Показывает динамику надёжности контрагента во времени.
    """
    if len(records) < 5:
        return []

    history = []
    for i in range(4, len(records)):
        window = records[max(0, i - 9): i + 1]  # окно 10 записей
        raw = {
            "avg_delay":  _factor_avg_delay(window),
            "max_delay":  _factor_max_delay(window),
            "regularity": _factor_regularity(window),
            "debt_ratio": _factor_debt_ratio(window),
            "trend":      _factor_trend(window),
            "experience": _factor_experience(window),
        }
        norm  = _normalize_factors(raw)
        score = _calculate_raw_score(norm)
        history.append({
            "date":  records[i]["due_date"].isoformat(),
            "score": round(score, 1),
        })

    return history


# ─────────────────────────────────────────────────────────────────────────────
# ГЛАВНАЯ ФУНКЦИЯ СКОРИНГА
# ─────────────────────────────────────────────────────────────────────────────

def score_contractor(payment_history: List[Dict]) -> Dict:
    """
    Основная функция. Принимает историю платежей, возвращает полный скоринг.

    Возвращает:
    {
        "score":                    float,   # 0–100
        "risk_level":               str,     # LOW / ACCEPTABLE / MEDIUM / HIGH / CRITICAL
        "risk_label":               str,     # Человекочитаемый уровень риска
        "risk_color":               str,     # HEX цвет для UI
        "recommended_credit_limit": float,   # Рекомендуемый кредитный лимит ₽
        "recommended_delay_days":   int,     # Рекомендуемая отсрочка
        "recommended_action":       str,     # Текстовая рекомендация
        "factors":                  dict,    # Детали факторов
        "alerts":                   list,    # Список алертов
        "forecast":                 dict,    # Прогноз следующего платежа
        "score_history":            list,    # Динамика скоринга
        "summary": {
            "total_invoices":       int,
            "total_amount":         float,
            "total_paid":           float,
            "total_overdue":        float,
            "overdue_count":        int,
        }
    }
    """
    if not payment_history:
        return _empty_result()

    records = _normalize_records(payment_history)

    if not records:
        return _empty_result("Не удалось обработать переданные данные")

    # Считаем факторы
    raw_factors = {
        "avg_delay":  _factor_avg_delay(records),
        "max_delay":  _factor_max_delay(records),
        "regularity": _factor_regularity(records),
        "debt_ratio": _factor_debt_ratio(records),
        "trend":      _factor_trend(records),
        "experience": _factor_experience(records),
    }

    normalized   = _normalize_factors(raw_factors)
    score        = _calculate_raw_score(normalized)
    risk_level   = _get_risk_level(score)

    return {
        "score":       round(score, 1),
        "risk_level":  risk_level,
        "risk_label":  RISK_LABELS[risk_level],
        "risk_color":  RISK_COLORS[risk_level],

        "recommended_credit_limit": _recommended_credit_limit(score, records),
        "recommended_delay_days":   _recommended_delay_days(score),
        "recommended_action":       _recommended_action(score, raw_factors),

        "factors": {
            "avg_delay_days":          round(raw_factors["avg_delay"], 1),
            "max_delay_days":          round(raw_factors["max_delay"], 1),
            "payment_regularity_pct":  round(raw_factors["regularity"] * 100, 1),
            "current_debt_ratio_pct":  round(raw_factors["debt_ratio"] * 100, 1),
            "trend_direction": (
                "улучшается" if raw_factors["trend"] >  0.1 else
                "ухудшается" if raw_factors["trend"] < -0.1 else
                "стабильно"
            ),
            "total_deals": len(records),
        },

        "alerts":        _generate_alerts(raw_factors, records),
        "forecast":      _forecast_next_payment(records),
        "score_history": _score_history(records),

        "summary": {
            "total_invoices": len(records),
            "total_amount":   sum(r["amount"] for r in records),
            "total_paid":     sum(r["paid_amount"] for r in records),
            "total_overdue":  sum(r["overdue_amount"] for r in records if r["is_overdue"]),
            "overdue_count":  sum(1 for r in records if r["is_overdue"]),
        }
    }


def _empty_result(message: str = "Нет данных") -> Dict:
    return {
        "score": None,
        "risk_level": "UNKNOWN",
        "risk_label": message,
        "risk_color": "#8E8E93",
        "recommended_credit_limit": 0,
        "recommended_delay_days": 0,
        "recommended_action": "Недостаточно данных для анализа",
        "factors": {},
        "alerts": [f"ℹ️ {message}"],
        "forecast": {"available": False, "message": message},
        "score_history": [],
        "summary": {
            "total_invoices": 0,
            "total_amount": 0,
            "total_paid": 0,
            "total_overdue": 0,
            "overdue_count": 0,
        }
    }


# ─────────────────────────────────────────────────────────────────────────────
# ТОЧКА ВХОДА ИЗ C++ (единственный публичный метод для AddIn)
# ─────────────────────────────────────────────────────────────────────────────

def process_request(json_input: str) -> str:
    """
    Единственный метод, вызываемый из C++ Native API AddIn.
    Принимает JSON-строку, возвращает JSON-строку.

    Поддерживаемые действия:
    - score_single: скоринг одного контрагента
    - score_batch:  скоринг списка контрагентов за один вызов
    - version:      возвращает версию движка
    """
    try:
        data = json.loads(json_input)
        action = data.get("action", "")

        if action == "score_single":
            result = score_contractor(data.get("payment_history", []))

        elif action == "score_batch":
            # contractors: {"guid_1": [...], "guid_2": [...]}
            contractors = data.get("contractors", {})
            result = {
                contractor_id: score_contractor(history)
                for contractor_id, history in contractors.items()
            }

        elif action == "version":
            result = {"version": VERSION, "status": "ok"}

        else:
            result = {"error": f"Unknown action: '{action}'",
                      "supported": ["score_single", "score_batch", "version"]}

        return json.dumps(result, ensure_ascii=False, default=str)

    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {e}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
