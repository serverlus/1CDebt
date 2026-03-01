"""
Тесты для DebtScoring Engine
Запуск: python3 tests/test_scoring.py
"""

import sys
import json
import unittest
from datetime import date, timedelta

sys.path.insert(0, "../python")
from scoring_engine import (
    score_contractor, process_request, _normalize_records,
    _factor_avg_delay, _factor_regularity, _factor_trend,
    RISK_LABELS, VERSION
)


# ─────────────────────────────────────────────────────────────────────────────
# ФАБРИКИ ТЕСТОВЫХ ДАННЫХ
# ─────────────────────────────────────────────────────────────────────────────

def make_payment(amount: float, delay_days: int = 0, months_ago: int = 1,
                 paid: bool = True) -> dict:
    """Создаёт одну запись платежа."""
    today        = date.today()
    invoice_date = today - timedelta(days=months_ago * 30 + 15)
    due_date     = today - timedelta(days=months_ago * 30)
    payment_date = due_date + timedelta(days=delay_days) if paid else None
    paid_amount  = amount if paid else 0.0

    return {
        "invoice_date": invoice_date.isoformat(),
        "due_date":     due_date.isoformat(),
        "payment_date": payment_date.isoformat() if payment_date else None,
        "amount":       amount,
        "paid_amount":  paid_amount,
    }


def make_history_ideal(n: int = 12) -> list:
    """Идеальный контрагент: всегда платит за 2 дня до срока."""
    return [make_payment(100_000, delay_days=-2, months_ago=i) for i in range(1, n + 1)]


def make_history_bad(n: int = 12) -> list:
    """Плохой контрагент: всегда просрочивает на 45 дней."""
    return [make_payment(100_000, delay_days=45, months_ago=i * 2) for i in range(1, n + 1)]


def make_history_critical(n: int = 6) -> list:
    """Критический: частично оплачивает, долги копятся."""
    history = [make_payment(100_000, delay_days=90, months_ago=i * 2)
               for i in range(1, n + 1)]
    # Добавляем неоплаченные счета
    for i in range(1, 4):
        history.append(make_payment(100_000, months_ago=i, paid=False))
    return history


def make_history_improving() -> list:
    """Контрагент, который начал плохо, но стал лучше."""
    history = []
    # Первые 6 месяцев — просрочивал сильно
    for i in range(6, 12):
        history.append(make_payment(100_000, delay_days=40, months_ago=i))
    # Последние 6 месяцев — платит вовремя
    for i in range(1, 7):
        history.append(make_payment(100_000, delay_days=1, months_ago=i))
    return history


def make_history_worsening() -> list:
    """Контрагент, который деградирует."""
    history = []
    # Сначала платил хорошо
    for i in range(6, 12):
        history.append(make_payment(100_000, delay_days=2, months_ago=i))
    # Потом начал просрочивать
    for i in range(1, 7):
        history.append(make_payment(100_000, delay_days=50, months_ago=i))
    return history


# ─────────────────────────────────────────────────────────────────────────────
# ТЕСТЫ НОРМАЛИЗАЦИИ
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalization(unittest.TestCase):

    def test_parse_valid_records(self):
        history = [make_payment(50_000, delay_days=5, months_ago=3)]
        records = _normalize_records(history)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["is_paid"])
        self.assertEqual(records[0]["delay_days"], 5)

    def test_parse_unpaid_records(self):
        history = [make_payment(50_000, months_ago=2, paid=False)]
        records = _normalize_records(history)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["is_overdue"])
        self.assertFalse(records[0]["is_paid"])
        self.assertGreater(records[0]["delay_days"], 0)

    def test_skip_records_without_due_date(self):
        history = [{"invoice_date": "2024-01-01", "due_date": None,
                    "payment_date": None, "amount": 100_000, "paid_amount": 0}]
        records = _normalize_records(history)
        self.assertEqual(len(records), 0)

    def test_overdue_amount_calculation(self):
        history = [make_payment(100_000, paid=False, months_ago=3)]
        records = _normalize_records(history)
        self.assertAlmostEqual(records[0]["overdue_amount"], 100_000)


# ─────────────────────────────────────────────────────────────────────────────
# ТЕСТЫ ФАКТОРОВ
# ─────────────────────────────────────────────────────────────────────────────

class TestFactors(unittest.TestCase):

    def test_avg_delay_zero_for_on_time(self):
        records = _normalize_records(make_history_ideal())
        avg = _factor_avg_delay(records)
        self.assertEqual(avg, 0)

    def test_avg_delay_correct_value(self):
        history = [make_payment(100_000, delay_days=30, months_ago=i)
                   for i in range(1, 5)]
        records = _normalize_records(history)
        avg = _factor_avg_delay(records)
        self.assertAlmostEqual(avg, 30, delta=1)

    def test_regularity_perfect(self):
        records = _normalize_records(make_history_ideal())
        reg = _factor_regularity(records)
        self.assertAlmostEqual(reg, 1.0)

    def test_regularity_zero(self):
        records = _normalize_records(make_history_bad())
        reg = _factor_regularity(records)
        self.assertAlmostEqual(reg, 0.0)

    def test_trend_improving(self):
        records = _normalize_records(make_history_improving())
        trend = _factor_trend(records)
        self.assertGreater(trend, 0)

    def test_trend_worsening(self):
        records = _normalize_records(make_history_worsening())
        trend = _factor_trend(records)
        self.assertLess(trend, 0)


# ─────────────────────────────────────────────────────────────────────────────
# ТЕСТЫ СКОРИНГА
# ─────────────────────────────────────────────────────────────────────────────

class TestScoring(unittest.TestCase):

    def test_score_in_range(self):
        result = score_contractor(make_history_ideal())
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)

    def test_ideal_contractor_high_score(self):
        result = score_contractor(make_history_ideal(24))
        print(f"\n  [IDEAL]    score={result['score']:.1f}  risk={result['risk_level']}")
        self.assertGreaterEqual(result["score"], 65)
        self.assertIn(result["risk_level"], ["LOW", "ACCEPTABLE"])

    def test_bad_contractor_low_score(self):
        result = score_contractor(make_history_bad())
        print(f"\n  [BAD]      score={result['score']:.1f}  risk={result['risk_level']}")
        # 45-дн просрочка = "требует внимания" — правильно что < 60
        self.assertLessEqual(result["score"], 60)
        self.assertNotIn(result["risk_level"], ["LOW"])

    def test_critical_contractor(self):
        result = score_contractor(make_history_critical())
        print(f"\n  [CRITICAL] score={result['score']:.1f}  risk={result['risk_level']}")
        self.assertLessEqual(result["score"], 40)

    def test_improving_contractor(self):
        result = score_contractor(make_history_improving())
        print(f"\n  [IMPROVE]  score={result['score']:.1f}  risk={result['risk_level']}")
        self.assertGreater(result["score"], 40)

    def test_empty_history(self):
        result = score_contractor([])
        self.assertIsNone(result["score"])
        self.assertEqual(result["risk_level"], "UNKNOWN")

    def test_single_payment(self):
        result = score_contractor([make_payment(100_000, delay_days=5)])
        self.assertIsNotNone(result["score"])

    def test_score_has_all_required_fields(self):
        result = score_contractor(make_history_ideal())
        required = [
            "score", "risk_level", "risk_label", "risk_color",
            "recommended_credit_limit", "recommended_delay_days",
            "recommended_action", "factors", "alerts", "forecast",
            "score_history", "summary"
        ]
        for field in required:
            self.assertIn(field, result, f"Missing field: {field}")

    def test_recommended_limit_positive(self):
        result = score_contractor(make_history_ideal(12))
        self.assertGreater(result["recommended_credit_limit"], 0)

    def test_bad_contractor_zero_delay(self):
        result = score_contractor(make_history_critical())
        self.assertEqual(result["recommended_delay_days"], 0)

    def test_ideal_contractor_30_day_delay(self):
        result = score_contractor(make_history_ideal(20))
        self.assertEqual(result["recommended_delay_days"], 30)

    def test_alerts_generated(self):
        result = score_contractor(make_history_bad())
        self.assertIsInstance(result["alerts"], list)
        self.assertGreater(len(result["alerts"]), 0)

    def test_forecast_available_enough_data(self):
        result = score_contractor(make_history_ideal(10))
        self.assertTrue(result["forecast"]["available"])

    def test_forecast_unavailable_little_data(self):
        result = score_contractor([make_payment(100_000)])
        self.assertFalse(result["forecast"]["available"])

    def test_score_history_generated(self):
        result = score_contractor(make_history_ideal(10))
        self.assertGreater(len(result["score_history"]), 0)

    def test_summary_totals(self):
        history = [make_payment(100_000, months_ago=i) for i in range(1, 6)]
        result = score_contractor(history)
        self.assertEqual(result["summary"]["total_invoices"], 5)
        self.assertAlmostEqual(result["summary"]["total_amount"], 500_000)

    def test_worsening_has_alert(self):
        result = score_contractor(make_history_worsening())
        alerts_text = " ".join(result["alerts"])
        self.assertIn("📉", alerts_text)


# ─────────────────────────────────────────────────────────────────────────────
# ТЕСТЫ БАТЧ-СКОРИНГА И JSON API
# ─────────────────────────────────────────────────────────────────────────────

class TestProcessRequest(unittest.TestCase):

    def test_version_action(self):
        response = json.loads(process_request('{"action": "version"}'))
        self.assertEqual(response["version"], VERSION)
        self.assertEqual(response["status"], "ok")

    def test_score_single_action(self):
        request = json.dumps({
            "action": "score_single",
            "payment_history": make_history_ideal()
        })
        response = json.loads(process_request(request))
        self.assertIn("score", response)
        self.assertIsNotNone(response["score"])

    def test_score_batch_action(self):
        request = json.dumps({
            "action": "score_batch",
            "contractors": {
                "guid-ideal":    make_history_ideal(),
                "guid-bad":      make_history_bad(),
                "guid-critical": make_history_critical(),
            }
        })
        response = json.loads(process_request(request))
        self.assertIn("guid-ideal", response)
        self.assertIn("guid-bad", response)
        self.assertIn("guid-critical", response)

        # Идеальный должен иметь скор выше плохого
        self.assertGreater(
            response["guid-ideal"]["score"],
            response["guid-bad"]["score"]
        )
        print(f"\n  BATCH scores: "
              f"ideal={response['guid-ideal']['score']:.1f}, "
              f"bad={response['guid-bad']['score']:.1f}, "
              f"critical={response['guid-critical']['score']:.1f}")

    def test_invalid_json(self):
        response = json.loads(process_request("not a json"))
        self.assertIn("error", response)

    def test_unknown_action(self):
        response = json.loads(process_request('{"action": "unknown"}'))
        self.assertIn("error", response)
        self.assertIn("supported", response)

    def test_response_is_valid_json(self):
        request = json.dumps({
            "action": "score_single",
            "payment_history": make_history_bad()
        })
        response_str = process_request(request)
        # Должен парситься без ошибок
        result = json.loads(response_str)
        self.assertIsInstance(result, dict)

    def test_cyrillic_in_response(self):
        """Проверяем что кириллица корректно сериализуется."""
        request = json.dumps({
            "action": "score_single",
            "payment_history": make_history_ideal()
        })
        response_str = process_request(request)
        result = json.loads(response_str)
        # risk_label должен содержать кириллицу
        self.assertRegex(result["risk_label"], r"[а-яА-Я]")


# ─────────────────────────────────────────────────────────────────────────────
# ТЕСТ ГРАНИЧНЫХ СЛУЧАЕВ
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgeCases(unittest.TestCase):

    def test_all_unpaid(self):
        history = [make_payment(100_000, months_ago=i, paid=False)
                   for i in range(1, 6)]
        result = score_contractor(history)
        self.assertIsNotNone(result["score"])
        # Все неоплачены — риск высокий, скор < 45
        self.assertLessEqual(result["score"], 45)
        self.assertIn(result["risk_level"], ["CRITICAL", "HIGH", "MEDIUM"])

    def test_very_large_amounts(self):
        history = [make_payment(50_000_000, months_ago=i) for i in range(1, 6)]
        result = score_contractor(history)
        self.assertIsNotNone(result["score"])

    def test_zero_amount_invoices(self):
        history = [{"invoice_date": "2024-01-01", "due_date": "2024-02-01",
                    "payment_date": "2024-02-01", "amount": 0, "paid_amount": 0}]
        result = score_contractor(history)
        self.assertIsNotNone(result)

    def test_single_invoice_no_crash(self):
        result = score_contractor([make_payment(100_000)])
        self.assertIsNotNone(result)

    def test_invalid_dates_skipped(self):
        history = [
            {"invoice_date": "bad-date", "due_date": "also-bad",
             "payment_date": None, "amount": 100_000, "paid_amount": 0},
            make_payment(100_000),  # одна нормальная запись
        ]
        result = score_contractor(history)
        # Должен обработать хотя бы одну запись
        self.assertIsNotNone(result)

    def test_mixed_paid_unpaid(self):
        history = (
            [make_payment(100_000, months_ago=i) for i in range(6, 12)] +
            [make_payment(100_000, months_ago=i, paid=False) for i in range(1, 6)]
        )
        result = score_contractor(history)
        self.assertIsNotNone(result["score"])
        self.assertGreater(result["summary"]["overdue_count"], 0)


# ─────────────────────────────────────────────────────────────────────────────
# ДЕМО-ВЫВОД
# ─────────────────────────────────────────────────────────────────────────────

def demo():
    """Демонстрация результата для реального контрагента."""
    print("\n" + "=" * 60)
    print("DEMO: Анализ контрагентов")
    print("=" * 60)

    scenarios = [
        ("ООО 'Платит всегда'",    make_history_ideal(24)),
        ("ООО 'Немного задерживает'", make_history_improving()),
        ("ООО 'Плохой плательщик'", make_history_bad()),
        ("ООО 'Банкрот почти'",    make_history_critical()),
    ]

    for name, history in scenarios:
        result = score_contractor(history)
        print(f"\n📊 {name}")
        print(f"   Скоринг:     {result['score']:.1f} / 100")
        print(f"   Риск:        {result['risk_label']} ({result['risk_level']})")
        print(f"   Лимит:       {result['recommended_credit_limit']:,.0f} ₽")
        print(f"   Отсрочка:    {result['recommended_delay_days']} дней")
        print(f"   Тренд:       {result['factors'].get('trend_direction', '–')}")
        print(f"   Рекомендация: {result['recommended_action']}")
        if result["alerts"] != ["✅ Нарушений не обнаружено"]:
            for alert in result["alerts"]:
                print(f"   {alert}")


if __name__ == "__main__":
    # Сначала демо
    demo()

    # Потом тесты
    print("\n" + "=" * 60)
    print("UNIT TESTS")
    print("=" * 60)
    unittest.main(verbosity=2, exit=False)
