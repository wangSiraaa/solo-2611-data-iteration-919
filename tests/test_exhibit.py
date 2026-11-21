"""Tests for exhibition planning: lending orders, collection, withdrawal, reopen."""

from __future__ import annotations

import itertools
import random
import tempfile
import unittest
from pathlib import Path

from basket_service import open_service
from basket_service.errors import BasketError
from basket_service.exhibit import optimize_bookings

from test_compare import PRODUCTS, build_worked_example


def brute_force_best_weight(options, sample_masks, weights, booths):
    """Independent enumeration of every booking set, for cross-checking."""

    candidates = [(b, v) for b in options for v in options[b]]
    best = 0
    for size in range(0, booths + 1):
        for combo in itertools.combinations(candidates, size):
            used = 0
            covered = 0
            feasible = True
            for basket, vendor in combo:
                if sample_masks[basket] & used:
                    feasible = False
                    break
                used |= sample_masks[basket]
                covered |= options[basket][vendor]
            if not feasible:
                continue
            best = max(best, sum(w for i, w in enumerate(weights) if covered >> i & 1))
    return best


class WorkedExamplePlanTests(unittest.TestCase):
    def test_two_booths_pick_yi_and_bing(self):
        svc = build_worked_example(open_service(":memory:"))
        plan = svc.plan_exhibition(2)
        self.assertEqual(plan["total_weight"], 13)
        self.assertEqual(
            [(b["basket_id"], b["vendor"], b["booth"]) for b in plan["bookings"]],
            [("L", "乙", 1), ("M", "丙", 2)],
        )
        self.assertEqual(plan["covered_statement_ids"], ["S3", "S4"])
        self.assertEqual(plan["pending_statement_ids"], ["S1", "S2"])
        # 甲's K cannot share a booth's samples with L (sample a) or M (b).
        self.assertEqual(plan["bookings"][0]["samples"], ["a", "c"])
        self.assertEqual(plan["bookings"][1]["samples"], ["b"])

    def test_one_booth_picks_jia(self):
        svc = build_worked_example(open_service(":memory:"))
        plan = svc.plan_exhibition(1)
        self.assertEqual(plan["total_weight"], 10)
        self.assertEqual([(b["basket_id"], b["vendor"]) for b in plan["bookings"]], [("K", "甲")])
        self.assertEqual(plan["covered_statement_ids"], ["S1", "S2"])

    def test_plan_names_basket_booth_and_quote_pair_per_statement(self):
        svc = build_worked_example(open_service(":memory:"))
        plan = svc.plan_exhibition(2)
        booking = plan["bookings"][0]
        evidence = booking["statements"][0]
        self.assertEqual(evidence["statement_id"], "S3")
        self.assertEqual(evidence["weight"], 8)
        self.assertEqual(evidence["failing_assertions"][0]["observed"], {"paid_old": 3, "paid_new": 5})
        # The named quote pair replays from the quote store.
        old_quote = svc.get_quote(evidence["old_quote_request"])
        new_quote = svc.get_quote(evidence["new_quote_request"])
        self.assertEqual((old_quote["total"], new_quote["total"]), (13, 13))
        self.assertEqual(old_quote["basket"], svc.get_basket("L")["items"])

    def test_plan_is_deterministic(self):
        svc = build_worked_example(open_service(":memory:"))
        first = svc.plan_exhibition(2)
        second = svc.plan_exhibition(2)
        self.assertEqual(
            [(b["basket_id"], b["vendor"]) for b in first["bookings"]],
            [(b["basket_id"], b["vendor"]) for b in second["bookings"]],
        )
        self.assertEqual(first["total_weight"], second["total_weight"])

    def test_weight_counted_once_across_baskets(self):
        svc = open_service(":memory:")
        svc.publish_snapshot(PRODUCTS, [{"coupon_id": "AB", "D": 10, "requires": {"A": 1, "B": 1}}], snapshot_id="o")
        svc.publish_snapshot(PRODUCTS, [{"coupon_id": "A8", "D": 8, "requires": {"A": 1}}], snapshot_id="n")
        # Two baskets with the same SKUs but fully separate physical samples;
        # both violate TOTAL_UNCHANGED under the same snapshot pair.
        svc.register_basket("K1", [{"unit_id": "a1", "sku": "A", "price": 10}, {"unit_id": "b1", "sku": "B", "price": 10}])
        svc.register_basket("K2", [{"unit_id": "a2", "sku": "A", "price": 10}, {"unit_id": "b2", "sku": "B", "price": 10}])
        svc.register_statement(
            "SX", vendor="甲", weight=6, old_snapshot="o", new_snapshot="n",
            allowed_baskets=["K1", "K2"], assertions=[{"kind": "TOTAL_UNCHANGED"}],
        )
        plan = svc.plan_exhibition(2)
        # Both baskets prove SX, but its weight counts once and a second
        # booking adds nothing, so only one basket is booked.
        self.assertEqual(plan["total_weight"], 6)
        self.assertEqual(len(plan["bookings"]), 1)
        self.assertEqual(plan["covered_statement_ids"], ["SX"])

    def test_invalid_booths(self):
        svc = build_worked_example(open_service(":memory:"))
        for bad in (-1, True, "2"):
            with self.assertRaises(BasketError) as ctx:
                svc.plan_exhibition(bad)
            self.assertEqual(ctx.exception.code, "INVALID_INPUT")
        empty = svc.plan_exhibition(0)
        self.assertEqual((empty["total_weight"], empty["bookings"]), (0, []))


class CollectionWithdrawalTests(unittest.TestCase):
    def setUp(self):
        self.svc = build_worked_example(open_service(":memory:"))
        self.plan = self.svc.plan_exhibition(2)

    def test_collect_exhibits_statements_with_archive(self):
        record = self.svc.collect_booking(self.plan["plan_id"], "L")
        self.assertEqual(record["exhibited"], [{"statement_id": "S3", "weight": 8}])
        self.assertFalse(record["replayed"])
        self.assertEqual(self.svc.get_statement("S3")["status"], "EXHIBITED")
        reopened = self.svc.reopen_statement("S3")
        self.assertEqual(reopened["refunds"], {"old": {"a": 3, "c": 10}, "new": {"a": 5, "c": 8}})
        self.assertEqual(reopened["old_quote"]["total"], 13)
        self.assertEqual(reopened["basket_id"], "L")
        self.assertEqual(reopened["booth"], 1)

    def test_collect_replay_and_errors(self):
        first = self.svc.collect_booking(self.plan["plan_id"], "L")
        replay = self.svc.collect_booking(self.plan["plan_id"], "L")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["exhibited"], first["exhibited"])
        with self.assertRaises(BasketError) as ctx:
            self.svc.collect_booking("plan-999", "L")
        self.assertEqual(ctx.exception.code, "UNKNOWN_PLAN")
        with self.assertRaises(BasketError) as ctx:
            self.svc.collect_booking(self.plan["plan_id"], "K")  # K is not booked
        self.assertEqual(ctx.exception.code, "UNKNOWN_BOOKING")

    def test_withdrawal_replans_uncollected_bookings_only(self):
        self.svc.collect_booking(self.plan["plan_id"], "L")
        outcome = self.svc.withdraw_sample("a")
        self.assertFalse(outcome["already_withdrawn"])
        # The uncollected (M, 丙) booking is dropped and re-planned; the
        # collected L booking is history and stays exhibited.
        self.assertEqual([b["basket_id"] for b in outcome["dropped_bookings"]], ["M"])
        replanned = outcome["replanned_plan"]
        self.assertEqual(replanned["booths"], 2)
        self.assertEqual(replanned["total_weight"], 5)
        self.assertEqual([(b["basket_id"], b["vendor"]) for b in replanned["bookings"]], [("M", "丙")])
        # K and L both need the withdrawn sample a, so S1/S2 stay pending.
        self.assertEqual(replanned["pending_statement_ids"], ["S1", "S2"])
        self.assertEqual(self.svc.get_statement("S3")["status"], "EXHIBITED")
        # The old plan no longer accepts collections.
        with self.assertRaises(BasketError) as ctx:
            self.svc.collect_booking(self.plan["plan_id"], "M")
        self.assertEqual(ctx.exception.code, "PLAN_NOT_ACTIVE")
        # The replanned order is collectable and archives 丙's statement.
        record = self.svc.collect_booking(replanned["plan_id"], "M")
        self.assertEqual(record["exhibited"], [{"statement_id": "S4", "weight": 5}])
        reopened = self.svc.reopen_statement("S4")
        self.assertEqual(reopened["refunds"], {"old": {"b": 4}, "new": {"b": 7}})

    def test_withdrawal_without_active_plan(self):
        svc = open_service(":memory:")
        svc.register_basket("B", [{"unit_id": "x", "sku": "A", "price": 10}])
        outcome = svc.withdraw_sample("x")
        self.assertIsNone(outcome["replanned_plan"])
        self.assertEqual(svc.get_basket("B")["lendable"], False)

    def test_withdraw_unknown_and_idempotent(self):
        with self.assertRaises(BasketError) as ctx:
            self.svc.withdraw_sample("nope")
        self.assertEqual(ctx.exception.code, "UNKNOWN_SAMPLE")
        self.svc.withdraw_sample("a")
        again = self.svc.withdraw_sample("a")
        self.assertTrue(again["already_withdrawn"])

    def test_reopen_requires_exhibited(self):
        with self.assertRaises(BasketError) as ctx:
            self.svc.reopen_statement("S1")
        self.assertEqual(ctx.exception.code, "STATEMENT_NOT_EXHIBITED")
        with self.assertRaises(BasketError) as ctx:
            self.svc.reopen_statement("nope")
        self.assertEqual(ctx.exception.code, "UNKNOWN_STATEMENT")

    def test_exhibited_archive_survives_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "svc.db")
            svc = build_worked_example(open_service(path))
            plan = svc.plan_exhibition(2)
            svc.collect_booking(plan["plan_id"], "L")
            svc.withdraw_sample("a")
            del svc
            reopened = open_service(path)
            record = reopened.reopen_statement("S3")
            self.assertEqual(record["refunds"]["old"], {"a": 3, "c": 10})
            self.assertEqual(record["old_quote"]["total"], 13)
            self.assertEqual(record["report"]["findings"], ["ALLOCATION_CHANGED", "SAME_TOTAL_DIFFERENT_ALLOCATION"])
            status = reopened.exhibition_status()
            self.assertEqual(status["withdrawn_samples"], ["a"])
            self.assertEqual([s["statement_id"] for s in status["exhibited_statements"]], ["S3"])


class OptimizerFuzzTests(unittest.TestCase):
    def test_matches_brute_force_on_random_instances(self):
        rng = random.Random(20261121)
        for trial in range(60):
            n_baskets = rng.randint(1, 8)
            n_statements = rng.randint(1, 6)
            vendors = ["v0", "v1", "v2"]
            n_samples = rng.randint(n_baskets, 10)
            weights = [rng.randint(0, 9) for _ in range(n_statements)]
            booths = rng.randint(0, 3)
            options = {}
            sample_masks = {}
            for b in range(n_baskets):
                basket = f"B{b}"
                chosen = sorted(rng.sample(range(n_samples), rng.randint(1, min(3, n_samples))))
                sample_masks[basket] = sum(1 << s for s in chosen)
                for vendor in vendors:
                    mask = sum(1 << s for s in range(n_statements) if rng.random() < 0.4)
                    if mask:
                        options.setdefault(basket, {})[vendor] = mask
            covered, bookings = optimize_bookings(options, sample_masks, weights, booths)
            expected = brute_force_best_weight(options, sample_masks, weights, booths)
            actual = sum(w for i, w in enumerate(weights) if covered >> i & 1)
            self.assertEqual(actual, expected, f"trial {trial}")
            # The returned plan is itself feasible and reproduces the coverage.
            used = 0
            rebuilt = 0
            self.assertEqual(bookings, sorted(bookings))
            self.assertLessEqual(len(bookings), booths)
            for basket, vendor in bookings:
                self.assertFalse(sample_masks[basket] & used)
                used |= sample_masks[basket]
                rebuilt |= options[basket][vendor]
            self.assertEqual(rebuilt, covered)

    def test_edge_cases(self):
        self.assertEqual(optimize_bookings({}, {}, [], 2), (0, []))
        self.assertEqual(optimize_bookings({"B": {"v": 0b1}}, {"B": 1}, [5], 0), (0, []))
        # Zero-weight statements never justify a booking.
        self.assertEqual(optimize_bookings({"B": {"v": 0b1}}, {"B": 1}, [0], 1), (0, []))
        # Deterministic tie-break: equal options -> lexicographically smallest.
        covered, bookings = optimize_bookings(
            {"B1": {"v": 0b1}, "B0": {"v": 0b1}}, {"B0": 1, "B1": 2}, [7], 1
        )
        self.assertEqual((covered, bookings), (0b1, [("B0", "v")]))


class ScaleTests(unittest.TestCase):
    """Twenty display baskets, sixteen statements, fully separate samples but
    heavily overlapping proven-statement sets.

    Every basket carries all eight SKUs (so the shared snapshot pair quotes
    it); the 160 physical samples are all distinct.  Old pays 60/unit, new
    pays 75/unit, so per-unit and total assertions fail on overlapping sets
    of baskets.
    """

    def build_scale(self, svc):
        products = [{"sku": f"SKU{i}", "price": 100} for i in range(8)]
        svc.publish_snapshot(
            products,
            [{"coupon_id": f"old-{i}", "D": 40, "requires": {f"SKU{i}": 1}} for i in range(8)],
            snapshot_id="scale-old",
        )
        svc.publish_snapshot(
            products,
            [{"coupon_id": f"new-{i}", "D": 25, "requires": {f"SKU{i}": 1}} for i in range(8)],
            snapshot_id="scale-new",
        )
        for i in range(20):
            svc.register_basket(
                f"B{i}",
                [{"unit_id": f"u{8 * i + j}", "sku": f"SKU{j}", "price": 100} for j in range(8)],
            )
        all_baskets = [f"B{i}" for i in range(20)]
        weights = [9, 8, 7, 6, 5, 4, 3, 2, 6, 6, 5, 5, 4, 4, 5, 7]
        for k in range(16):
            if k < 8:
                assertions = [{"kind": "UNIT_PAID_UNCHANGED", "unit_id": f"u{8 * k}"}]
            elif k < 14:
                assertions = [{"kind": "TOTAL_UNCHANGED"}]
            elif k == 14:
                assertions = [{"kind": "TOTAL_NOT_ABOVE", "amount": 500}]
            else:
                assertions = [{"kind": "UNIT_PAID_NOT_ABOVE", "unit_id": "u11", "amount": 70}]
            svc.register_statement(
                f"s{k}", vendor=f"V{k // 4}", weight=weights[k],
                old_snapshot="scale-old", new_snapshot="scale-new",
                allowed_baskets=all_baskets, assertions=assertions,
            )
        return weights

    def test_scale_plan_is_optimal_and_counts_weights_once(self):
        svc = open_service(":memory:")
        weights = self.build_scale(svc)
        plan = svc.plan_exhibition(3)
        # Independent cross-check: enumerate every booking set from the
        # counterexample lists without using the production search.
        options = {}
        sample_masks = {f"B{i}": sum(1 << (8 * i + j) for j in range(8)) for i in range(20)}
        for k in range(16):
            statement = svc.get_statement(f"s{k}")
            for basket_id in svc.find_counterexamples(f"s{k}")["counterexample_baskets"]:
                options.setdefault(basket_id, {}).setdefault(statement["vendor"], 0)
                options[basket_id][statement["vendor"]] |= 1 << k
        expected = brute_force_best_weight(options, sample_masks, weights, 3)
        self.assertEqual(plan["total_weight"], expected)
        self.assertEqual(expected, 51)
        # Deterministic tie-break: the V2 booking lands on the
        # lexicographically smallest free basket ("B10" < "B2" as strings).
        self.assertEqual(
            [(b["basket_id"], b["vendor"]) for b in plan["bookings"]],
            [("B0", "V0"), ("B1", "V3"), ("B10", "V2")],
        )
        # Weight counted once: covered statements sum exactly to the total.
        covered_weight = sum(weights[k] for k in range(16) if f"s{k}" in plan["covered_statement_ids"])
        self.assertEqual(covered_weight, plan["total_weight"])
        # All 160 samples are distinct, so feasibility is purely the booth cap.
        booked_samples = [s for b in plan["bookings"] for s in b["samples"]]
        self.assertEqual(len(booked_samples), len(set(booked_samples)))


if __name__ == "__main__":
    unittest.main()
