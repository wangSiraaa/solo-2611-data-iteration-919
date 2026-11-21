"""Tests for the comparison catalog: samples, baskets, statements, reports."""

from __future__ import annotations

import unittest

from basket_service import open_service
from basket_service.catalog import evaluate_assertions
from basket_service.errors import BasketError

PRODUCTS = [{"sku": s, "price": 10} for s in ("A", "B", "C")]


def build_worked_example(svc):
    """The 甲/乙/丙 worked example, built entirely from quotable items/coupons.

    Samples a(A,10), b(B,10), c(C,10); baskets K={a,b}, L={a,c}, M={b}.
    乙's two snapshots give basket L the same total (13) while unit a's paid
    amount moves 3 -> 5, so only a per-unit check sees the counterexample.
    """

    svc.publish_snapshot(PRODUCTS, [{"coupon_id": "AB", "D": 10, "requires": {"A": 1, "B": 1}}], snapshot_id="jia-old")
    svc.publish_snapshot(PRODUCTS, [{"coupon_id": "B8", "D": 8, "requires": {"B": 1}}], snapshot_id="jia-new")
    svc.publish_snapshot(PRODUCTS, [{"coupon_id": "VIPA", "D": 7, "requires": {"A": 1}}], snapshot_id="yi-old")
    svc.publish_snapshot(
        PRODUCTS,
        [{"coupon_id": "VIPA", "D": 5, "requires": {"A": 1}}, {"coupon_id": "C2", "D": 2, "requires": {"C": 1}}],
        snapshot_id="yi-new",
    )
    svc.publish_snapshot(PRODUCTS, [{"coupon_id": "B6", "D": 6, "requires": {"B": 1}}], snapshot_id="bing-old")
    svc.publish_snapshot(PRODUCTS, [{"coupon_id": "B3", "D": 3, "requires": {"B": 1}}], snapshot_id="bing-new")

    svc.register_basket("K", [{"unit_id": "a", "sku": "A", "price": 10}, {"unit_id": "b", "sku": "B", "price": 10}])
    svc.register_basket("L", [{"unit_id": "a", "sku": "A", "price": 10}, {"unit_id": "c", "sku": "C", "price": 10}])
    svc.register_basket("M", [{"unit_id": "b", "sku": "B", "price": 10}])

    svc.register_statement(
        "S1", vendor="甲", weight=6, old_snapshot="jia-old", new_snapshot="jia-new",
        allowed_baskets=["K"], assertions=[{"kind": "UNIT_PAID_UNCHANGED", "unit_id": "a"}],
    )
    svc.register_statement(
        "S2", vendor="甲", weight=4, old_snapshot="jia-old", new_snapshot="jia-new",
        allowed_baskets=["K"], assertions=[{"kind": "TOTAL_UNCHANGED"}],
    )
    svc.register_statement(
        "S3", vendor="乙", weight=8, old_snapshot="yi-old", new_snapshot="yi-new",
        allowed_baskets=["L"], assertions=[{"kind": "UNIT_PAID_UNCHANGED", "unit_id": "a"}],
    )
    svc.register_statement(
        "S4", vendor="丙", weight=5, old_snapshot="bing-old", new_snapshot="bing-new",
        allowed_baskets=["M"], assertions=[{"kind": "UNIT_PAID_DECREASES", "unit_id": "b"}],
    )
    return svc


class ComparisonReportTests(unittest.TestCase):
    def test_same_total_different_allocation(self):
        svc = build_worked_example(open_service(":memory:"))
        report = svc.compare_basket("L", "yi-old", "yi-new")
        self.assertEqual(report["total"], {"old": 13, "new": 13, "changed": False})
        paid = {u["unit_id"]: (u["paid_old"], u["paid_new"]) for u in report["units"]}
        self.assertEqual(paid["a"], (3, 5))
        self.assertEqual(paid["c"], (10, 8))
        self.assertIn("SAME_TOTAL_DIFFERENT_ALLOCATION", report["findings"])
        self.assertNotIn("TOTAL_CHANGED", report["findings"])

    def test_coupon_retargeted_to_different_physical_unit(self):
        svc = open_service(":memory:")
        products = [{"sku": "P", "price": 10}, {"sku": "Q", "price": 10}]
        svc.publish_snapshot(products, [{"coupon_id": "VIP", "D": 6, "requires": {"P": 1}}], snapshot_id="d-old")
        svc.publish_snapshot(products, [{"coupon_id": "VIP", "D": 6, "requires": {"Q": 1}}], snapshot_id="d-new")
        svc.register_basket("N", [{"unit_id": "p", "sku": "P", "price": 10}, {"unit_id": "q", "sku": "Q", "price": 10}])
        report = svc.compare_basket("N", "d-old", "d-new")
        self.assertIn("COUPON_RETARGETED", report["findings"])
        self.assertIn("SAME_TOTAL_DIFFERENT_ALLOCATION", report["findings"])
        coupon = report["coupons"][0]
        self.assertEqual((coupon["coupon_id"], coupon["units_old"], coupon["units_new"]), ("VIP", ["p"], ["q"]))
        svc.register_statement(
            "ST", vendor="丁", weight=3, old_snapshot="d-old", new_snapshot="d-new",
            allowed_baskets=["N"], assertions=[{"kind": "COUPON_SAME_UNITS", "coupon_id": "VIP"}],
        )
        result = svc.find_counterexamples("ST")
        self.assertEqual(result["counterexample_baskets"], ["N"])
        failing = result["baskets"][0]["failing_assertions"][0]
        self.assertEqual(failing["observed"], {"units_old": ["p"], "units_new": ["q"]})

    def test_unchanged_report(self):
        svc = open_service(":memory:")
        svc.publish_snapshot(PRODUCTS, [{"coupon_id": "B6", "D": 6, "requires": {"B": 1}}], snapshot_id="s1")
        svc.register_basket("M", [{"unit_id": "b", "sku": "B", "price": 10}])
        report = svc.compare_basket("M", "s1", "s1")
        self.assertEqual(report["findings"], ["UNCHANGED"])
        self.assertFalse(report["total"]["changed"])


class CounterexampleTests(unittest.TestCase):
    def test_counterexample_evidence_is_replayable(self):
        svc = build_worked_example(open_service(":memory:"))
        result = svc.find_counterexamples("S3")
        self.assertEqual(result["counterexample_baskets"], ["L"])
        entry = result["baskets"][0]
        self.assertTrue(entry["counterexample"])
        failing = entry["failing_assertions"][0]
        self.assertEqual(failing["assertion"], {"kind": "UNIT_PAID_UNCHANGED", "unit_id": "a"})
        self.assertEqual(failing["observed"], {"paid_old": 3, "paid_new": 5})
        # The evidence quotes re-run byte-for-byte from the frozen snapshots.
        old_quote = svc.get_quote(entry["old_quote_request"])
        new_quote = svc.get_quote(entry["new_quote_request"])
        self.assertEqual((old_quote["total"], new_quote["total"]), (13, 13))
        self.assertEqual({e["unit_id"]: e["paid"] for e in old_quote["allocation"]}, {"a": 3, "c": 10})
        self.assertEqual({e["unit_id"]: e["paid"] for e in new_quote["allocation"]}, {"a": 5, "c": 8})
        # Re-quoting through the catalog replays the same stored requests.
        again = svc.find_counterexamples("S3")
        self.assertEqual(again["baskets"][0]["old_quote_request"], entry["old_quote_request"])

    def test_total_only_check_misses_the_counterexample(self):
        svc = build_worked_example(open_service(":memory:"))
        svc.register_statement(
            "S3-total", vendor="乙", weight=0, old_snapshot="yi-old", new_snapshot="yi-new",
            allowed_baskets=["L"], assertions=[{"kind": "TOTAL_UNCHANGED"}],
        )
        result = svc.find_counterexamples("S3-total")
        self.assertEqual(result["counterexample_baskets"], [])
        self.assertEqual(result["baskets"][0]["assertion_results"][0]["status"], "HOLDS")

    def test_not_applicable_assertion_is_not_a_counterexample(self):
        svc = open_service(":memory:")
        svc.publish_snapshot(PRODUCTS, [], snapshot_id="empty-old")
        svc.publish_snapshot(PRODUCTS, [], snapshot_id="empty-new")
        svc.register_basket("B1", [{"unit_id": "x", "sku": "A", "price": 10}])
        svc.register_basket("B2", [{"unit_id": "y", "sku": "B", "price": 10}])
        svc.register_statement(
            "NA", vendor="v", weight=1, old_snapshot="empty-old", new_snapshot="empty-new",
            allowed_baskets=["B1", "B2"], assertions=[{"kind": "UNIT_PAID_UNCHANGED", "unit_id": "x"}],
        )
        result = svc.find_counterexamples("NA")
        statuses = {row["basket_id"]: row["assertion_results"][0]["status"] for row in result["baskets"]}
        self.assertEqual(statuses, {"B1": "HOLDS", "B2": "NOT_APPLICABLE"})
        self.assertEqual(result["counterexample_baskets"], [])

    def test_unquotable_basket_is_recorded_not_treated_as_counterexample(self):
        svc = open_service(":memory:")
        svc.publish_snapshot([{"sku": "A", "price": 10}], [], snapshot_id="only-a")
        svc.register_basket("Z", [{"unit_id": "z", "sku": "Z", "price": 10}])
        svc.register_statement(
            "ERR", vendor="v", weight=1, old_snapshot="only-a", new_snapshot="only-a",
            allowed_baskets=["Z"], assertions=[{"kind": "TOTAL_UNCHANGED"}],
        )
        result = svc.find_counterexamples("ERR")
        self.assertEqual(result["counterexample_baskets"], [])
        self.assertFalse(result["baskets"][0]["counterexample"])
        self.assertEqual(result["baskets"][0]["error"]["code"], "INVALID_BASKET")

    def test_unknown_statement_and_basket(self):
        svc = open_service(":memory:")
        with self.assertRaises(BasketError) as ctx:
            svc.find_counterexamples("nope")
        self.assertEqual(ctx.exception.code, "UNKNOWN_STATEMENT")
        with self.assertRaises(BasketError) as ctx:
            svc.compare_basket("nope", "a", "b")
        self.assertEqual(ctx.exception.code, "UNKNOWN_BASKET")


class IntegrityTests(unittest.TestCase):
    def test_fabricated_cheaper_sample_is_rejected(self):
        svc = build_worked_example(open_service(":memory:"))
        # Sample "a" is pinned to (A, 10) by baskets K and L; a cheaper "a"
        # or a different sku cannot be substituted in another basket.
        with self.assertRaises(BasketError) as ctx:
            svc.register_basket("F1", [{"unit_id": "a", "sku": "A", "price": 2}])
        self.assertEqual(ctx.exception.code, "SAMPLE_CONFLICT")
        with self.assertRaises(BasketError) as ctx:
            svc.register_basket("F2", [{"unit_id": "a", "sku": "Z", "price": 10}])
        self.assertEqual(ctx.exception.code, "SAMPLE_CONFLICT")
        # Agreeing on the pinned attributes is fine.
        ok = svc.register_basket("F3", [{"unit_id": "a", "sku": "A", "price": 10}])
        self.assertFalse(ok["replayed"])

    def test_basket_conflict_and_replay(self):
        svc = open_service(":memory:")
        items = [{"unit_id": "a", "sku": "A", "price": 10}]
        first = svc.register_basket("B", items)
        self.assertFalse(first["replayed"])
        replay = svc.register_basket("B", list(items))
        self.assertTrue(replay["replayed"])
        with self.assertRaises(BasketError) as ctx:
            svc.register_basket("B", [{"unit_id": "b", "sku": "B", "price": 10}])
        self.assertEqual(ctx.exception.code, "BASKET_CONFLICT")

    def test_statement_conflict_and_replay(self):
        svc = build_worked_example(open_service(":memory:"))
        again = svc.register_statement(
            "S1", vendor="甲", weight=6, old_snapshot="jia-old", new_snapshot="jia-new",
            allowed_baskets=["K"], assertions=[{"kind": "UNIT_PAID_UNCHANGED", "unit_id": "a"}],
        )
        self.assertTrue(again["replayed"])
        with self.assertRaises(BasketError) as ctx:
            svc.register_statement(
                "S1", vendor="甲", weight=7, old_snapshot="jia-old", new_snapshot="jia-new",
                allowed_baskets=["K"], assertions=[{"kind": "UNIT_PAID_UNCHANGED", "unit_id": "a"}],
            )
        self.assertEqual(ctx.exception.code, "STATEMENT_CONFLICT")

    def test_statement_validation(self):
        svc = build_worked_example(open_service(":memory:"))
        base = {"vendor": "v", "weight": 1, "old_snapshot": "jia-old", "new_snapshot": "jia-new",
                "allowed_baskets": ["K"], "assertions": [{"kind": "TOTAL_UNCHANGED"}]}

        def register(**overrides):
            payload = dict(base, **overrides)
            svc.register_statement("X", **payload)

        with self.assertRaises(BasketError) as ctx:
            register(allowed_baskets=["missing"])
        self.assertEqual(ctx.exception.code, "UNKNOWN_BASKET")
        with self.assertRaises(BasketError) as ctx:
            register(old_snapshot="missing")
        self.assertEqual(ctx.exception.code, "UNKNOWN_SNAPSHOT")
        with self.assertRaises(BasketError) as ctx:
            register(assertions=[{"kind": "NO_SUCH_KIND"}])
        self.assertEqual(ctx.exception.code, "INVALID_ASSERTION")
        with self.assertRaises(BasketError) as ctx:
            register(assertions=[{"kind": "UNIT_PAID_UNCHANGED", "unit_id": "zz"}])
        self.assertEqual(ctx.exception.code, "INVALID_ASSERTION")
        with self.assertRaises(BasketError) as ctx:
            register(assertions=[{"kind": "COUPON_SAME_UNITS", "coupon_id": "NOPE"}])
        self.assertEqual(ctx.exception.code, "INVALID_ASSERTION")
        with self.assertRaises(BasketError) as ctx:
            register(assertions=[{"kind": "UNIT_PAID_NOT_ABOVE", "unit_id": "a"}])
        self.assertEqual(ctx.exception.code, "INVALID_ASSERTION")
        with self.assertRaises(BasketError) as ctx:
            register(weight=-1)
        self.assertEqual(ctx.exception.code, "INVALID_INPUT")
        with self.assertRaises(BasketError) as ctx:
            register(weight=True)
        self.assertEqual(ctx.exception.code, "INVALID_INPUT")
        with self.assertRaises(BasketError) as ctx:
            register(allowed_baskets=[])
        self.assertEqual(ctx.exception.code, "INVALID_INPUT")
        with self.assertRaises(BasketError) as ctx:
            register(assertions=[])
        self.assertEqual(ctx.exception.code, "INVALID_ASSERTION")


class AssertionEvaluationTests(unittest.TestCase):
    QUOTES = (
        {  # old quote
            "total": 13,
            "allocation": [
                {"unit_id": "a", "paid": 3, "discount": 7},
                {"unit_id": "c", "paid": 10, "discount": 0},
            ],
            "assignments": {"VIP": ["a"]},
        },
        {  # new quote
            "total": 13,
            "allocation": [
                {"unit_id": "a", "paid": 5, "discount": 5},
                {"unit_id": "c", "paid": 8, "discount": 2},
            ],
            "assignments": {"VIP": ["c"]},
        },
    )

    def evaluate(self, assertions):
        old_quote, new_quote = self.QUOTES
        return [r["status"] for r in evaluate_assertions(assertions, old_quote, new_quote)]

    def test_all_kinds(self):
        statuses = self.evaluate([
            {"kind": "UNIT_PAID_UNCHANGED", "unit_id": "a"},          # 3 != 5
            {"kind": "UNIT_PAID_NOT_ABOVE", "unit_id": "a", "amount": 6},   # 5 <= 6
            {"kind": "UNIT_PAID_NOT_ABOVE", "unit_id": "a", "amount": 4},   # 5 !<= 4
            {"kind": "UNIT_PAID_DECREASES", "unit_id": "a"},          # 5 !< 3
            {"kind": "UNIT_PAID_DECREASES", "unit_id": "c"},          # 8 < 10
            {"kind": "UNIT_DISCOUNT_NOT_ABOVE", "unit_id": "c", "amount": 2},  # 2 <= 2
            {"kind": "TOTAL_UNCHANGED"},                              # 13 == 13
            {"kind": "TOTAL_NOT_ABOVE", "amount": 12},                # 13 !<= 12
            {"kind": "COUPON_SAME_UNITS", "coupon_id": "VIP"},        # [a] != [c]
        ])
        self.assertEqual(
            statuses,
            ["VIOLATED", "HOLDS", "VIOLATED", "VIOLATED", "HOLDS", "HOLDS", "HOLDS", "VIOLATED", "VIOLATED"],
        )

    def test_per_assertion_statuses(self):
        old_quote, new_quote = self.QUOTES
        results = evaluate_assertions(
            [
                {"kind": "UNIT_PAID_DECREASES", "unit_id": "a"},
                {"kind": "UNIT_PAID_DECREASES", "unit_id": "c"},
                {"kind": "UNIT_PAID_UNCHANGED", "unit_id": "missing"},
                {"kind": "COUPON_SAME_UNITS", "coupon_id": "UNUSED"},
            ],
            old_quote,
            new_quote,
        )
        self.assertEqual([r["status"] for r in results], ["VIOLATED", "HOLDS", "NOT_APPLICABLE", "NOT_APPLICABLE"])


if __name__ == "__main__":
    unittest.main()
