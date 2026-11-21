"""Demo driver: builds a real persistent database and a JSON result record.

Run ``python3 -m basket_service demo`` to (re)generate::

    data/demo.db            real SQLite database
    results/demo_plan.json  the JSON plan that was executed for the main run
    results/demo_results.json  every quote, sale, return and independent check

All *expectations* in this file are produced by ``basket_service.reference``
(brute-force enumeration and Fraction arithmetic) and by reading the raw
ledger records, never by calling the production optimizer/refund functions to
compute the expected answer.
"""

from __future__ import annotations

import itertools
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List

from . import open_service
from .errors import BasketError
from .reference import exhaustive_best, expected_allocation, expected_return
from .validation import validate_basket, validate_coupons

ROOT = Path(__file__).resolve().parent.parent


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _brute(basket, coupons):
    units = validate_basket(basket)
    canonical = validate_coupons(coupons, {u["sku"] for u in units})
    total, used, unit_lists, mapping = exhaustive_best(units, canonical)
    allocation = expected_allocation(units, mapping, {c["coupon_id"]: c["D"] for c in canonical})
    return total, mapping, allocation


def _case_material() -> Dict[str, Any]:
    case = json.loads((ROOT / "case.json").read_text(encoding="utf-8"))
    basket = case["basket"]
    before = case["before_coupons"]
    added = case["added_coupons"]
    return {"basket": basket, "before": before, "added": added, "return_request": case["return_request"]}


def run_demo(db_path: str, out_path: str, plan_path: str) -> Dict[str, Any]:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(db_path + suffix)
        if candidate.exists():
            candidate.unlink()

    svc = open_service(db_path)
    results: Dict[str, Any] = {"database": str(db_path), "scenarios": {}, "checks": []}

    # ------------------------------------------------------------------
    # 1. Case material: two snapshots, quote under both, bind two sales.
    # ------------------------------------------------------------------
    material = _case_material()
    basket = material["basket"]
    snap_before = svc.publish_snapshot(
        [{"sku": "A", "price": 100}, {"sku": "B", "price": 100}, {"sku": "C", "price": 100}],
        material["before"],
        snapshot_id="case-before",
    )
    snap_added = svc.publish_snapshot(
        [{"sku": "A", "price": 100}, {"sku": "B", "price": 100}, {"sku": "C", "price": 100}],
        material["before"] + material["added"],
        snapshot_id="case-added",
    )
    quote_before = svc.quote(basket, [], "case-q-before", snap_before)
    quote_added = svc.quote(basket, [], "case-q-added", snap_added)
    brute_before = _brute(basket, material["before"])
    brute_added = _brute(basket, material["before"] + material["added"])
    results["scenarios"]["case_material"] = {
        "snapshots": {"before": snap_before, "added": snap_added},
        "quote_before": quote_before,
        "quote_added": quote_added,
        "independent_before": {"total": brute_before[0], "assignments": brute_before[1], "allocation": brute_before[2]},
        "independent_added": {"total": brute_added[0], "assignments": brute_added[1], "allocation": brute_added[2]},
    }
    results["checks"].append({
        "check": "case.before.total_matches_bruteforce",
        "ok": quote_before["total"] == brute_before[0],
        "expected": brute_before[0],
        "actual": quote_before["total"],
    })
    results["checks"].append({
        "check": "case.added.total_matches_bruteforce",
        "ok": quote_added["total"] == brute_added[0],
        "expected": brute_added[0],
        "actual": quote_added["total"],
    })
    results["checks"].append({
        "check": "old_quote_unchanged_after_new_snapshot",
        "ok": svc.get_quote("case-q-before") == quote_before,
        "expected": True,
        "actual": svc.get_quote("case-q-before") == quote_before,
    })

    # Two independent legal sales, different promotions -> different paid.
    sale_before = svc.register_sale("S1", quote=quote_before)
    sale_added = svc.register_sale("S2", quote=quote_added)
    results["scenarios"]["two_sales"] = {"S1": sale_before, "S2": sale_added}

    # Returns follow their own bound quote, not the newest promotion.
    ret_before_b = svc.return_units(material["return_request"]["request_id"], "S1", ["b"], {"b": "RESTOCK"})
    ret_added_b = svc.return_units("ret-added-b", "S2", ["b"], {"b": "SCRAP"})
    paid_before = {u["unit_id"]: u["paid"] for u in sale_before["units"]}
    paid_added = {u["unit_id"]: u["paid"] for u in sale_added["units"]}
    results["scenarios"]["bound_quote_refunds"] = {
        "case-return-request": ret_before_b,
        "ret-added-b": ret_added_b,
    }
    results["checks"].append({
        "check": "old_sale_refund_uses_old_quote",
        "ok": ret_before_b["refund_total"] == expected_return(paid_before, ["b"])[0],
        "expected": expected_return(paid_before, ["b"])[0],
        "actual": ret_before_b["refund_total"],
    })
    results["checks"].append({
        "check": "new_sale_refund_uses_new_quote",
        "ok": ret_added_b["refund_total"] == expected_return(paid_added, ["b"])[0],
        "expected": expected_return(paid_added, ["b"])[0],
        "actual": ret_added_b["refund_total"],
    })

    # Same sale_id with the other quote must be rejected; the old identity is
    # still a legitimate, different sale rather than a retry.
    try:
        svc.register_sale("S1", quote=quote_added)
        rebind_ok = False
        rebind_error = None
    except BasketError as exc:
        rebind_ok = True
        rebind_error = exc.code
    results["scenarios"]["sale_conflict"] = {"rejected": rebind_ok, "error": rebind_error}

    # Promotion switch: publish a third, stronger snapshot and quote again; the
    # old sale still refunds from its own binding.
    third_coupons = material["before"] + material["added"] + [
        {"coupon_id": "ABC", "D": 200, "requires": {"A": 1, "B": 1, "C": 1}}
    ]
    snap_third = svc.publish_snapshot(
        [{"sku": "A", "price": 100}, {"sku": "B", "price": 100}, {"sku": "C", "price": 100}],
        third_coupons,
        snapshot_id="case-third",
    )
    quote_third = svc.quote(basket, [], "case-q-third", snap_third)
    ret_before_a = svc.return_units("ret-before-a", "S1", ["a"], {"a": "SCRAP"})
    results["scenarios"]["promotion_switch"] = {
        "snapshot": snap_third,
        "quote": quote_third,
        "old_sale_refund_a_after_switch": ret_before_a,
    }
    results["checks"].append({
        "check": "post_switch_old_sale_refund_follows_old_paid",
        "ok": ret_before_a["refund_total"] == paid_before["a"],
        "expected": paid_before["a"],
        "actual": ret_before_a["refund_total"],
    })

    # ------------------------------------------------------------------
    # 2. Optimizer trap and tie-break examples.
    # ------------------------------------------------------------------
    greedy_basket = [
        {"unit_id": "a", "sku": "A", "price": 100},
        {"unit_id": "b", "sku": "B", "price": 100},
        {"unit_id": "c", "sku": "C", "price": 100},
        {"unit_id": "d", "sku": "D", "price": 100},
    ]
    greedy_coupons = [
        {"coupon_id": "COMBINED", "D": 190, "requires": {"A": 1, "B": 1, "C": 1, "D": 1}},
        {"coupon_id": "AB", "D": 120, "requires": {"A": 1, "B": 1}},
        {"coupon_id": "CD", "D": 120, "requires": {"C": 1, "D": 1}},
    ]
    gsnap = svc.publish_snapshot(None, greedy_coupons)
    greedy_quote = svc.quote(greedy_basket, [], "greedy-quote", gsnap)
    greedy_brute = _brute(greedy_basket, greedy_coupons)
    results["scenarios"]["greedy_trap"] = {
        "basket": greedy_basket,
        "coupons": greedy_coupons,
        "largest_D_first_total": 400 - 190,
        "optimal_quote": greedy_quote,
        "independent": {"total": greedy_brute[0], "assignments": greedy_brute[1], "allocation": greedy_brute[2]},
    }
    results["checks"].append({
        "check": "greedy_largest_d_is_not_optimal",
        "ok": greedy_quote["total"] < 400 - 190 and greedy_quote["total"] == greedy_brute[0],
        "expected": greedy_brute[0],
        "actual": greedy_quote["total"],
        "greedy_total": 400 - 190,
    })

    contention_basket = [
        {"unit_id": "a", "sku": "A", "price": 100},
        {"unit_id": "b", "sku": "B", "price": 100},
    ]
    contention_coupons = [
        {"coupon_id": "AB1", "D": 120, "requires": {"A": 1, "B": 1}},
        {"coupon_id": "AB2", "D": 110, "requires": {"A": 1, "B": 1}},
    ]
    csnap = svc.publish_snapshot(None, contention_coupons)
    contention_quote = svc.quote(contention_basket, [], "contention-quote", csnap)
    results["scenarios"]["contention"] = {
        "quote": contention_quote,
        "independent": _brute(contention_basket, contention_coupons)[:2],
    }

    price_basket = [
        {"unit_id": "cheap", "sku": "A", "price": 30},
        {"unit_id": "dear", "sku": "A", "price": 300},
        {"unit_id": "mid", "sku": "A", "price": 120},
    ]
    price_coupons = [
        {"coupon_id": "big", "D": 250, "requires": {"A": 1}},
        {"coupon_id": "small1", "D": 60, "requires": {"A": 1}},
        {"coupon_id": "small2", "D": 50, "requires": {"A": 1}},
    ]
    psnap = svc.publish_snapshot(None, price_coupons)
    price_quote = svc.quote(price_basket, [], "price-quote", psnap)
    results["scenarios"]["same_sku_different_prices"] = {
        "quote": price_quote,
        "independent": _brute(price_basket, price_coupons)[:2],
    }

    tie_basket = [
        {"unit_id": "a", "sku": "A", "price": 100},
        {"unit_id": "b", "sku": "A", "price": 100},
        {"unit_id": "c", "sku": "A", "price": 100},
    ]
    tie_coupons = [
        {"coupon_id": "z", "D": 50, "requires": {"A": 1}},
        {"coupon_id": "y", "D": 50, "requires": {"A": 1}},
        {"coupon_id": "x", "D": 50, "requires": {"A": 1}},
    ]
    tsnap = svc.publish_snapshot(None, tie_coupons)
    tie_quote = svc.quote(tie_basket, [], "tie-quote", tsnap)
    results["scenarios"]["two_level_tie_break"] = {
        "quote": tie_quote,
        "independent": _brute(tie_basket, tie_coupons)[:2],
    }
    results["checks"].append({
        "check": "tie_break_matches_bruteforce",
        "ok": tie_quote["assignments"] == _brute(tie_basket, tie_coupons)[1],
        "expected": _brute(tie_basket, tie_coupons)[1],
        "actual": tie_quote["assignments"],
    })

    remainder_basket = [
        {"unit_id": "z", "sku": "A", "price": 100},
        {"unit_id": "a", "sku": "A", "price": 100},
        {"unit_id": "m", "sku": "A", "price": 100},
        {"unit_id": "low", "sku": "A", "price": 1},
    ]
    remainder_coupons = [{"coupon_id": "third", "D": 100, "requires": {"A": 3}}]
    rsnap = svc.publish_snapshot(None, remainder_coupons)
    remainder_quote = svc.quote(remainder_basket, [], "remainder-quote", rsnap)
    results["scenarios"]["proportional_remainder"] = {
        "quote": remainder_quote,
        "independent": _brute(remainder_basket, remainder_coupons),
    }
    results["checks"].append({
        "check": "proportional_allocation_matches_fraction_reference",
        "ok": remainder_quote["allocation"] == _brute(remainder_basket, remainder_coupons)[2],
        "expected": _brute(remainder_basket, remainder_coupons)[2],
        "actual": remainder_quote["allocation"],
    })

    # ------------------------------------------------------------------
    # 3. Ten-unit basket: independent full enumeration + ledger audit.
    # ------------------------------------------------------------------
    ten_basket = [
        {"unit_id": "u0", "sku": "A", "price": 100},
        {"unit_id": "u1", "sku": "A", "price": 30},
        {"unit_id": "u2", "sku": "B", "price": 70},
        {"unit_id": "u3", "sku": "B", "price": 45},
        {"unit_id": "u4", "sku": "C", "price": 90},
        {"unit_id": "u5", "sku": "C", "price": 20},
        {"unit_id": "u6", "sku": "D", "price": 60},
        {"unit_id": "u7", "sku": "D", "price": 10},
        {"unit_id": "u8", "sku": "E", "price": 55},
        {"unit_id": "u9", "sku": "F", "price": 5},
    ]
    ten_coupons = [
        {"coupon_id": "c0", "D": 40, "requires": {"A": 1}},
        {"coupon_id": "c1", "D": 50, "requires": {"B": 1, "C": 1}},
        {"coupon_id": "c2", "D": 35, "requires": {"D": 2}},
        {"coupon_id": "c3", "D": 60, "requires": {"A": 2}},
        {"coupon_id": "c4", "D": 70, "requires": {"C": 1, "D": 1, "E": 1}},
        {"coupon_id": "c5", "D": 25, "requires": {"F": 1}},
        {"coupon_id": "c6", "D": 45, "requires": {"B": 2}},
        {"coupon_id": "c7", "D": 30, "requires": {"E": 1, "F": 1}},
    ]
    ten_snap = svc.publish_snapshot(None, ten_coupons)
    ten_quote = svc.quote(ten_basket, [], "ten-quote", ten_snap)
    ten_brute = _brute(ten_basket, ten_coupons)
    ten_sale = svc.register_sale("S-ten", quote=ten_quote)
    returned_units = ["u1", "u5", "u9", "u3"]
    grades = {"u1": "RESTOCK", "u5": "SCRAP", "u9": "RESTOCK", "u3": "RESTOCK"}
    ten_return = svc.return_units("ret-ten", "S-ten", returned_units, grades)
    paid = {row["unit_id"]: row["paid"] for row in ten_brute[2]}
    expected_refund, expected_per_unit = expected_return(paid, returned_units)
    results["scenarios"]["ten_unit"] = {
        "basket": ten_basket,
        "coupons": ten_coupons,
        "quote": ten_quote,
        "independent": {"total": ten_brute[0], "assignments": ten_brute[1], "allocation": ten_brute[2]},
        "sale": ten_sale,
        "return": ten_return,
        "independent_refund": {"total": expected_refund, "per_unit": expected_per_unit},
    }
    results["checks"].append({
        "check": "ten_unit_total_matches_full_enumeration",
        "ok": ten_quote["total"] == ten_brute[0] and ten_quote["assignments"] == ten_brute[1],
        "expected": ten_brute[0],
        "actual": ten_quote["total"],
    })
    results["checks"].append({
        "check": "ten_unit_return_matches_original_paid",
        "ok": ten_return["refund_total"] == expected_refund,
        "expected": expected_refund,
        "actual": ten_return["refund_total"],
    })

    # Independent ledger audit: reconstruct from sale_units + return_items, not
    # from the service's refund function.
    raw = svc.db
    with raw.connection() as conn:
        unit_rows = conn.execute(
            "SELECT unit_id, sku, paid, returned, grade FROM sale_units WHERE sale_id = ? ORDER BY unit_id", ("S-ten",)
        ).fetchall()
        return_rows = conn.execute(
            "SELECT ri.unit_id, ri.grade, ri.refund FROM return_items ri JOIN return_requests rr ON rr.request_id = ri.request_id WHERE rr.sale_id = ? ORDER BY ri.unit_id",
            ("S-ten",),
        ).fetchall()
        inv_rows = conn.execute("SELECT sku, bucket, SUM(delta) AS qty FROM inventory_events GROUP BY sku, bucket").fetchall()
    independent_ledger = {
        "sale_units": [dict(r) for r in unit_rows],
        "return_items": [dict(r) for r in return_rows],
        "inventory_from_raw_events": [dict(r) for r in inv_rows],
        "per_unit_paid_sum": sum(r["paid"] for r in unit_rows),
        "returned_sum": sum(r["refund"] for r in return_rows),
        "remaining": [r["unit_id"] for r in unit_rows if not r["returned"]],
    }
    results["scenarios"]["ten_unit"]["independent_ledger_audit"] = independent_ledger
    results["checks"].append({
        "check": "ten_unit_ledger_audit_per_unit_paid_sums_to_quote",
        "ok": independent_ledger["per_unit_paid_sum"] == ten_quote["total"],
        "expected": ten_quote["total"],
        "actual": independent_ledger["per_unit_paid_sum"],
    })
    results["checks"].append({
        "check": "ten_unit_ledger_audit_refund_sums_to_return",
        "ok": independent_ledger["returned_sum"] == ten_return["refund_total"],
        "expected": ten_return["refund_total"],
        "actual": independent_ledger["returned_sum"],
    })

    # ------------------------------------------------------------------
    # 4. Retry, overlap and independent stock movements.
    # ------------------------------------------------------------------
    lost_response_retry = svc.return_units("ret-ten", "S-ten", returned_units, grades)
    results["scenarios"]["lost_response_retry"] = lost_response_retry
    results["checks"].append({
        "check": "retry_replays_first_result",
        "ok": lost_response_retry == ten_return,
        "expected": ten_return,
        "actual": lost_response_retry,
    })

    try:
        svc.return_units("overlap", "S-ten", ["u0", "u1"], {"u0": "RESTOCK", "u1": "RESTOCK"})
        overlap = {"rejected": False}
    except BasketError as exc:
        overlap = {"rejected": True, "error": exc.code}
    results["scenarios"]["overlap_rejected"] = overlap
    results["checks"].append({
        "check": "overlap_rejects_whole_request_and_keeps_u0_refundable",
        "ok": overlap["rejected"] and "u0" in svc.get_sale("S-ten")["remaining_refundable"],
        "expected": True,
        "actual": overlap["rejected"],
    })

    adj = svc.adjust_inventory("adj-A-1", "A", 4, "RESTOCK")
    adj_scrap = svc.adjust_inventory("adj-B-1", "B", 2, "SCRAP")
    results["scenarios"]["adjustments"] = {"restock": adj, "scrap": adj_scrap}
    results["checks"].append({
        "check": "adjustment_does_not_change_refundable",
        "ok": "u0" in svc.get_sale("S-ten")["remaining_refundable"],
        "expected": True,
        "actual": "u0" in svc.get_sale("S-ten")["remaining_refundable"],
    })

    # ------------------------------------------------------------------
    # 5. Reopen the database and keep all state.
    # ------------------------------------------------------------------
    results["scenarios"]["before_reopen"] = {
        "inventory": svc.inventory(),
        "sale_ten": svc.get_sale("S-ten"),
        "audit_sales": svc.audit_sales(),
        "audit_inventory": svc.audit_inventory(),
    }
    del svc
    reopened = open_service(db_path)
    results["scenarios"]["after_reopen"] = {
        "get_quote_case": reopened.get_quote("case-q-before"),
        "get_sale_ten": reopened.get_sale("S-ten"),
        "get_return_ten": reopened.get_return("ret-ten"),
        "inventory": reopened.inventory(),
        "audit_sales": reopened.audit_sales(),
        "audit_inventory": reopened.audit_inventory(),
    }
    results["checks"].append({
        "check": "reopen_preserves_quote_sale_return_inventory",
        "ok": (
            reopened.get_quote("case-q-before") == quote_before
            and reopened.get_sale("S-ten")["remaining_refundable"] == ["u0", "u2", "u4", "u6", "u7", "u8"]
            and reopened.get_return("ret-ten")["refund_total"] == ten_return["refund_total"]
            and reopened.inventory() == results["scenarios"]["before_reopen"]["inventory"]
        ),
        "expected": "identical",
        "actual": "identical"
        if (
            reopened.get_quote("case-q-before") == quote_before
            and reopened.get_sale("S-ten")["remaining_refundable"] == ["u0", "u2", "u4", "u6", "u7", "u8"]
            and reopened.get_return("ret-ten")["refund_total"] == ten_return["refund_total"]
            and reopened.inventory() == results["scenarios"]["before_reopen"]["inventory"]
        )
        else "differs",
    })

    # ------------------------------------------------------------------
    # 6. Independent quote issuer -> ledger-only consumer.
    # ------------------------------------------------------------------
    ledger_path = str(Path(db_path).with_suffix(".ledger.db"))
    for suffix in ("", "-wal", "-shm"):
        stale = Path(ledger_path + suffix)
        if stale.exists():
            stale.unlink()
    ledger_only = open_service(ledger_path)
    consumer_sale = ledger_only.register_sale("S-independent", quote=quote_added)
    consumer_return = ledger_only.return_units("ret-independent", "S-independent", ["c"], {"c": "RESTOCK"})
    results["scenarios"]["independent_quote_consumer"] = {
        "sale": consumer_sale,
        "return": consumer_return,
        "quote_came_from": "case-q-added (validated payload; ledger never re-optimised)",
    }
    results["checks"].append({
        "check": "ledger_consumes_complete_external_quote",
        "ok": consumer_sale["total"] == quote_added["total"]
        and consumer_return["refund_total"] == expected_return(paid_added, ["c"])[0],
        "expected": expected_return(paid_added, ["c"])[0],
        "actual": consumer_return["refund_total"],
    })
    del ledger_only

    # ------------------------------------------------------------------
    # 7. Concurrent overlapping returns against the real file database.
    # ------------------------------------------------------------------
    race_svc = open_service(db_path)
    concurrent_quote = race_svc.quote(basket, [], "race-q", snap_before)
    race_svc.register_sale("S-race", quote=concurrent_quote)
    restock_b_before_race = race_svc.inventory()["RESTOCK"].get("B", 0)
    del race_svc
    barrier = threading.Barrier(2)
    outcomes: List[Dict[str, Any]] = []
    failures: List[str] = []
    lock = threading.Lock()

    def worker(request_id: str) -> None:
        client = open_service(db_path)
        try:
            barrier.wait()
            result = client.return_units(request_id, "S-race", ["b"], {"b": "RESTOCK"})
            with lock:
                outcomes.append({"request_id": request_id, "refund_total": result["refund_total"]})
        except BasketError as exc:
            with lock:
                failures.append(exc.code)

    threads = [threading.Thread(target=worker, args=(f"race-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    final = open_service(db_path)
    restock_b_after_race = final.inventory()["RESTOCK"].get("B", 0)
    race_state = {
        "successes": outcomes,
        "failures": failures,
        "inventory_B_restock": restock_b_after_race,
        "inventory_B_restock_delta": restock_b_after_race - restock_b_before_race,
        "remaining": final.get_sale("S-race")["remaining_refundable"],
    }
    results["scenarios"]["concurrent_returns"] = race_state
    results["checks"].append({
        "check": "concurrent_overlap_one_success_one_rejection_no_double_stock",
        "ok": len(outcomes) == 1
        and failures == ["UNIT_ALREADY_RETURNED"]
        and race_state["inventory_B_restock_delta"] == 1,
        "expected": {"successes": 1, "rejections": ["UNIT_ALREADY_RETURNED"], "restock_B_delta": 1},
        "actual": race_state,
    })
    del final

    # ------------------------------------------------------------------
    # 8. Comparison catalog and exhibition lending plan (对照目录 / 布展借单).
    #
    # The 甲/乙/丙 worked example is built entirely from quotable items and
    # coupons — nothing is hand-recorded as "proved".  乙's two snapshots
    # give basket L the same total while unit a's paid amount moves 3 -> 5,
    # so a total-only check would miss the counterexample.
    # ------------------------------------------------------------------
    svc = open_service(db_path)
    cat_products = [{"sku": s, "price": 10} for s in ("A", "B", "C")]
    svc.publish_snapshot(cat_products, [{"coupon_id": "AB", "D": 10, "requires": {"A": 1, "B": 1}}], snapshot_id="cmp-jia-old")
    svc.publish_snapshot(cat_products, [{"coupon_id": "B8", "D": 8, "requires": {"B": 1}}], snapshot_id="cmp-jia-new")
    svc.publish_snapshot(cat_products, [{"coupon_id": "VIPA", "D": 7, "requires": {"A": 1}}], snapshot_id="cmp-yi-old")
    svc.publish_snapshot(
        cat_products,
        [{"coupon_id": "VIPA", "D": 5, "requires": {"A": 1}}, {"coupon_id": "C2", "D": 2, "requires": {"C": 1}}],
        snapshot_id="cmp-yi-new",
    )
    svc.publish_snapshot(cat_products, [{"coupon_id": "B6", "D": 6, "requires": {"B": 1}}], snapshot_id="cmp-bing-old")
    svc.publish_snapshot(cat_products, [{"coupon_id": "B3", "D": 3, "requires": {"B": 1}}], snapshot_id="cmp-bing-new")
    svc.register_basket("K", [{"unit_id": "a", "sku": "A", "price": 10}, {"unit_id": "b", "sku": "B", "price": 10}])
    svc.register_basket("L", [{"unit_id": "a", "sku": "A", "price": 10}, {"unit_id": "c", "sku": "C", "price": 10}])
    svc.register_basket("M", [{"unit_id": "b", "sku": "B", "price": 10}])
    svc.register_statement(
        "S1", vendor="甲", weight=6, old_snapshot="cmp-jia-old", new_snapshot="cmp-jia-new",
        allowed_baskets=["K"], assertions=[{"kind": "UNIT_PAID_UNCHANGED", "unit_id": "a"}],
    )
    svc.register_statement(
        "S2", vendor="甲", weight=4, old_snapshot="cmp-jia-old", new_snapshot="cmp-jia-new",
        allowed_baskets=["K"], assertions=[{"kind": "TOTAL_UNCHANGED"}],
    )
    svc.register_statement(
        "S3", vendor="乙", weight=8, old_snapshot="cmp-yi-old", new_snapshot="cmp-yi-new",
        allowed_baskets=["L"], assertions=[{"kind": "UNIT_PAID_UNCHANGED", "unit_id": "a"}],
    )
    svc.register_statement(
        "S4", vendor="丙", weight=5, old_snapshot="cmp-bing-old", new_snapshot="cmp-bing-new",
        allowed_baskets=["M"], assertions=[{"kind": "UNIT_PAID_DECREASES", "unit_id": "b"}],
    )

    report_l = svc.compare_basket("L", "cmp-yi-old", "cmp-yi-new")
    ce_s3 = svc.find_counterexamples("S3")
    results["scenarios"]["comparison_catalog"] = {
        "report_L": report_l,
        "counterexamples_S3": ce_s3,
    }
    results["checks"].append({
        "check": "catalog.same_total_different_allocation_detected",
        "ok": (
            report_l["total"] == {"old": 13, "new": 13, "changed": False}
            and {u["unit_id"]: (u["paid_old"], u["paid_new"]) for u in report_l["units"]}["a"] == (3, 5)
            and "SAME_TOTAL_DIFFERENT_ALLOCATION" in report_l["findings"]
        ),
        "expected": {"total_old": 13, "total_new": 13, "a_paid": [3, 5]},
        "actual": {"total": report_l["total"], "units": report_l["units"], "findings": report_l["findings"]},
    })
    results["checks"].append({
        "check": "catalog.counterexample_replayable_on_real_basket",
        "ok": ce_s3["counterexample_baskets"] == ["L"]
        and svc.get_quote(ce_s3["baskets"][0]["old_quote_request"])["total"] == 13
        and ce_s3["baskets"][0]["failing_assertions"][0]["observed"] == {"paid_old": 3, "paid_new": 5},
        "expected": ["L"],
        "actual": ce_s3["counterexample_baskets"],
    })

    # A total-only assertion on the same basket finds nothing — this is why
    # the catalog checks per-unit return liability, not just the basket total.
    svc.register_statement(
        "S3-total", vendor="乙", weight=0, old_snapshot="cmp-yi-old", new_snapshot="cmp-yi-new",
        allowed_baskets=["L"], assertions=[{"kind": "TOTAL_UNCHANGED"}],
    )
    ce_total_only = svc.find_counterexamples("S3-total")
    results["scenarios"]["comparison_catalog"]["total_only_check"] = ce_total_only
    results["checks"].append({
        "check": "catalog.total_only_check_misses_L",
        "ok": ce_total_only["counterexample_baskets"] == [],
        "expected": [],
        "actual": ce_total_only["counterexample_baskets"],
    })

    # Exhibition lending plan: 2 booths -> 乙+丙 = 13; 1 booth -> 甲 = 10.
    plan_two = svc.plan_exhibition(2)
    plan_one = svc.plan_exhibition(1)
    plan_two_again = svc.plan_exhibition(2)
    results["scenarios"]["exhibition"] = {
        "plan_two_booths": plan_two,
        "plan_one_booth": plan_one,
        "plan_two_booths_replanned": plan_two_again,
    }
    results["checks"].append({
        "check": "exhibit.two_booths_weight_13",
        "ok": plan_two["total_weight"] == 13
        and [(b["basket_id"], b["vendor"]) for b in plan_two["bookings"]] == [("L", "乙"), ("M", "丙")],
        "expected": {"weight": 13, "bookings": [["L", "乙"], ["M", "丙"]]},
        "actual": {"weight": plan_two["total_weight"], "bookings": [[b["basket_id"], b["vendor"]] for b in plan_two["bookings"]]},
    })
    results["checks"].append({
        "check": "exhibit.one_booth_weight_10",
        "ok": plan_one["total_weight"] == 10
        and [(b["basket_id"], b["vendor"]) for b in plan_one["bookings"]] == [("K", "甲")],
        "expected": {"weight": 10, "bookings": [["K", "甲"]]},
        "actual": {"weight": plan_one["total_weight"], "bookings": [[b["basket_id"], b["vendor"]] for b in plan_one["bookings"]]},
    })

    # Collect 乙's booking, then the library withdraws sample a: only the
    # uncollected bookings are re-planned; 乙's exhibited statement stays.
    collected_l = svc.collect_booking(plan_two_again["plan_id"], "L")
    withdrawal = svc.withdraw_sample("a")
    replanned = withdrawal["replanned_plan"]
    results["scenarios"]["exhibition"]["collected_L"] = collected_l
    results["scenarios"]["exhibition"]["withdrawal_a"] = withdrawal
    results["checks"].append({
        "check": "exhibit.withdrawal_replans_uncollected_only",
        "ok": (
            collected_l["exhibited"] == [{"statement_id": "S3", "weight": 8}]
            and [b["basket_id"] for b in withdrawal["dropped_bookings"]] == ["M"]
            and replanned["total_weight"] == 5
            and [(b["basket_id"], b["vendor"]) for b in replanned["bookings"]] == [("M", "丙")]
            and replanned["pending_statement_ids"] == ["S1", "S2", "S3-total"]
            and svc.get_statement("S3")["status"] == "EXHIBITED"
        ),
        "expected": {"dropped": ["M"], "replanned_weight": 5, "bookings": [["M", "丙"]], "pending": ["S1", "S2", "S3-total"]},
        "actual": {
            "dropped": [b["basket_id"] for b in withdrawal["dropped_bookings"]],
            "replanned_weight": replanned["total_weight"],
            "bookings": [[b["basket_id"], b["vendor"]] for b in replanned["bookings"]],
            "pending": replanned["pending_statement_ids"],
        },
    })

    # Reopen the exhibited statement with its original quotes and return
    # amounts; the ledger refunds exactly the archived per-unit amount.
    reopened_s3 = svc.reopen_statement("S3")
    svc.register_sale("exhibit-S3", quote=reopened_s3["old_quote"])
    refund_a = svc.return_units("exhibit-ret-S3-a", "exhibit-S3", ["a"], {"a": "RESTOCK"})
    results["scenarios"]["exhibition"]["reopened_S3"] = reopened_s3
    results["scenarios"]["exhibition"]["ledger_refund_a"] = refund_a
    results["checks"].append({
        "check": "exhibit.reopen_returns_original_quotes_and_refunds",
        "ok": reopened_s3["refunds"] == {"old": {"a": 3, "c": 10}, "new": {"a": 5, "c": 8}}
        and reopened_s3["old_quote"]["total"] == 13,
        "expected": {"old": {"a": 3, "c": 10}, "new": {"a": 5, "c": 8}},
        "actual": reopened_s3["refunds"],
    })
    results["checks"].append({
        "check": "exhibit.ledger_refund_matches_archived_amount",
        "ok": refund_a["refund_total"] == reopened_s3["refunds"]["old"]["a"] == 3,
        "expected": 3,
        "actual": refund_a["refund_total"],
    })

    # 丁: the same coupon id moves to a different physical unit (同券号换了实物).
    # Registered after the lending-plan flow so the extra weight-3 statement
    # cannot tie-break the worked example's optimum.
    svc.publish_snapshot(
        [{"sku": "P", "price": 10}, {"sku": "Q", "price": 10}],
        [{"coupon_id": "VIP", "D": 6, "requires": {"P": 1}}],
        snapshot_id="cmp-ding-old",
    )
    svc.publish_snapshot(
        [{"sku": "P", "price": 10}, {"sku": "Q", "price": 10}],
        [{"coupon_id": "VIP", "D": 6, "requires": {"Q": 1}}],
        snapshot_id="cmp-ding-new",
    )
    svc.register_basket("N", [{"unit_id": "p", "sku": "P", "price": 10}, {"unit_id": "q", "sku": "Q", "price": 10}])
    svc.register_statement(
        "S5", vendor="丁", weight=3, old_snapshot="cmp-ding-old", new_snapshot="cmp-ding-new",
        allowed_baskets=["N"], assertions=[{"kind": "COUPON_SAME_UNITS", "coupon_id": "VIP"}],
    )
    report_n = svc.compare_basket("N", "cmp-ding-old", "cmp-ding-new")
    ce_s5 = svc.find_counterexamples("S5")
    results["scenarios"]["comparison_catalog"]["report_N"] = report_n
    results["checks"].append({
        "check": "catalog.coupon_retargeted_detected",
        "ok": "COUPON_RETARGETED" in report_n["findings"] and ce_s5["counterexample_baskets"] == ["N"],
        "expected": {"findings": "COUPON_RETARGETED", "counterexamples": ["N"]},
        "actual": {"findings": report_n["findings"], "counterexamples": ce_s5["counterexample_baskets"]},
    })

    # ------------------------------------------------------------------
    # 9. Scale: 20 display baskets, 16 statements, fully separate samples
    #    but heavily overlapping proven-statement sets (separate database).
    # ------------------------------------------------------------------
    scale_path = str(Path(db_path).with_suffix(".scale.db"))
    for suffix in ("", "-wal", "-shm"):
        stale = Path(scale_path + suffix)
        if stale.exists():
            stale.unlink()
    scale_svc = open_service(scale_path)
    scale_products = [{"sku": f"SKU{i}", "price": 100} for i in range(8)]
    scale_svc.publish_snapshot(
        scale_products,
        [{"coupon_id": f"old-{i}", "D": 40, "requires": {f"SKU{i}": 1}} for i in range(8)],
        snapshot_id="scale-old",
    )
    scale_svc.publish_snapshot(
        scale_products,
        [{"coupon_id": f"new-{i}", "D": 25, "requires": {f"SKU{i}": 1}} for i in range(8)],
        snapshot_id="scale-new",
    )
    for i in range(20):
        scale_svc.register_basket(
            f"B{i}",
            [{"unit_id": f"u{8 * i + j}", "sku": f"SKU{j}", "price": 100} for j in range(8)],
        )
    scale_weights = [9, 8, 7, 6, 5, 4, 3, 2, 6, 6, 5, 5, 4, 4, 5, 7]
    all_baskets = [f"B{i}" for i in range(20)]
    scale_assertions = []
    for k in range(16):
        if k < 8:
            assertions = [{"kind": "UNIT_PAID_UNCHANGED", "unit_id": f"u{8 * k}"}]
        elif k < 14:
            assertions = [{"kind": "TOTAL_UNCHANGED"}]
        elif k == 14:
            assertions = [{"kind": "TOTAL_NOT_ABOVE", "amount": 500}]
        else:
            assertions = [{"kind": "UNIT_PAID_NOT_ABOVE", "unit_id": "u11", "amount": 70}]
        scale_assertions.append(assertions)
        scale_svc.register_statement(
            f"s{k}", vendor=f"V{k // 4}", weight=scale_weights[k],
            old_snapshot="scale-old", new_snapshot="scale-new",
            allowed_baskets=all_baskets, assertions=assertions,
        )
    scale_plan = scale_svc.plan_exhibition(3)

    # Independent expectation: re-quote every basket directly, evaluate the
    # assertions inline here (not via the catalog), then enumerate all
    # booking sets brute-force (not via the production search).
    def _inline_violated(kind_assertion, old_q, new_q):
        kind = kind_assertion["kind"]
        old_paid = {e["unit_id"]: e["paid"] for e in old_q["allocation"]}
        new_paid = {e["unit_id"]: e["paid"] for e in new_q["allocation"]}
        if kind == "UNIT_PAID_UNCHANGED":
            unit = kind_assertion["unit_id"]
            return unit in old_paid and old_paid[unit] != new_paid[unit]
        if kind == "UNIT_PAID_NOT_ABOVE":
            unit = kind_assertion["unit_id"]
            return unit in new_paid and new_paid[unit] > kind_assertion["amount"]
        if kind == "TOTAL_UNCHANGED":
            return old_q["total"] != new_q["total"]
        if kind == "TOTAL_NOT_ABOVE":
            return new_q["total"] > kind_assertion["amount"]
        raise AssertionError(kind)

    independent_options: Dict[str, Dict[str, int]] = {}
    for k in range(16):
        statement = scale_svc.get_statement(f"s{k}")
        for basket_id in all_baskets:
            items = scale_svc.get_basket(basket_id)["items"]
            old_q = scale_svc.quote(items, [], f"ind:{basket_id}:old", "scale-old")
            new_q = scale_svc.quote(items, [], f"ind:{basket_id}:new", "scale-new")
            if any(_inline_violated(a, old_q, new_q) for a in scale_assertions[k]):
                independent_options.setdefault(basket_id, {}).setdefault(statement["vendor"], 0)
                independent_options[basket_id][statement["vendor"]] |= 1 << k
    candidates = [(b, v) for b in independent_options for v in independent_options[b]]
    independent_best = 0
    for size in range(0, 4):
        for combo in itertools.combinations(candidates, size):
            used_samples = set()
            covered = 0
            feasible = True
            for basket_id, vendor in combo:
                samples = set(scale_svc.get_basket(basket_id)["samples"])
                if used_samples & samples:
                    feasible = False
                    break
                used_samples |= samples
                covered |= independent_options[basket_id][vendor]
            if feasible:
                independent_best = max(
                    independent_best,
                    sum(scale_weights[k] for k in range(16) if covered >> k & 1),
                )
    results["scenarios"]["exhibition_scale"] = {
        "database": scale_path,
        "baskets": 20,
        "statements": 16,
        "booths": 3,
        "plan": scale_plan,
        "independent_brute_force_weight": independent_best,
    }
    results["checks"].append({
        "check": "exhibit.scale_matches_independent_brute_force",
        "ok": scale_plan["total_weight"] == independent_best == 51
        and sum(scale_weights[k] for k in range(16) if f"s{k}" in scale_plan["covered_statement_ids"]) == scale_plan["total_weight"],
        "expected": 51,
        "actual": {"plan": scale_plan["total_weight"], "brute_force": independent_best},
    })
    del scale_svc

    results["summary"] = {
        "checks": len(results["checks"]),
        "passed": sum(1 for c in results["checks"] if c["ok"]),
        "failed": sum(1 for c in results["checks"] if not c["ok"]),
    }
    results["plan"] = {
        "note": "The structured operations executed above, replayable with `python -m basket_service run-plan`.",
        "steps": [
            {"op": "publish_snapshot", "products": [{"sku": "A", "price": 100}, {"sku": "B", "price": 100}, {"sku": "C", "price": 100}], "coupons": material["before"], "snapshot_id": "case-before"},
            {"op": "publish_snapshot", "products": [{"sku": "A", "price": 100}, {"sku": "B", "price": 100}, {"sku": "C", "price": 100}], "coupons": material["before"] + material["added"], "snapshot_id": "case-added"},
            {"name": "q_before", "op": "quote", "basket": basket, "coupons": [], "request_id": "case-q-before", "snapshot_id": "case-before"},
            {"name": "q_added", "op": "quote", "basket": basket, "coupons": [], "request_id": "case-q-added", "snapshot_id": "case-added"},
            {"name": "s_before", "op": "register_sale", "sale_id": "S1", "quote": "$q_before"},
            {"name": "s_added", "op": "register_sale", "sale_id": "S2", "quote": "$q_added"},
            {"op": "return", "request_id": "r1", "sale_id": "S1", "unit_ids": ["b"], "grade": {"b": "RESTOCK"}},
            {"op": "return", "request_id": "ret-added-b", "sale_id": "S2", "unit_ids": ["b"], "grade": {"b": "SCRAP"}},
            {"op": "register_sale", "sale_id": "S1", "quote": "$q_added", "expect_error": "SALE_CONFLICT"},
            # Comparison catalog / exhibition lending plan (对照目录 / 布展借单).
            {"op": "publish_snapshot", "products": [{"sku": "A", "price": 10}, {"sku": "B", "price": 10}, {"sku": "C", "price": 10}], "coupons": [{"coupon_id": "AB", "D": 10, "requires": {"A": 1, "B": 1}}], "snapshot_id": "cmp-jia-old"},
            {"op": "publish_snapshot", "products": [{"sku": "A", "price": 10}, {"sku": "B", "price": 10}, {"sku": "C", "price": 10}], "coupons": [{"coupon_id": "B8", "D": 8, "requires": {"B": 1}}], "snapshot_id": "cmp-jia-new"},
            {"op": "publish_snapshot", "products": [{"sku": "A", "price": 10}, {"sku": "B", "price": 10}, {"sku": "C", "price": 10}], "coupons": [{"coupon_id": "VIPA", "D": 7, "requires": {"A": 1}}], "snapshot_id": "cmp-yi-old"},
            {"op": "publish_snapshot", "products": [{"sku": "A", "price": 10}, {"sku": "B", "price": 10}, {"sku": "C", "price": 10}], "coupons": [{"coupon_id": "VIPA", "D": 5, "requires": {"A": 1}}, {"coupon_id": "C2", "D": 2, "requires": {"C": 1}}], "snapshot_id": "cmp-yi-new"},
            {"op": "publish_snapshot", "products": [{"sku": "A", "price": 10}, {"sku": "B", "price": 10}, {"sku": "C", "price": 10}], "coupons": [{"coupon_id": "B6", "D": 6, "requires": {"B": 1}}], "snapshot_id": "cmp-bing-old"},
            {"op": "publish_snapshot", "products": [{"sku": "A", "price": 10}, {"sku": "B", "price": 10}, {"sku": "C", "price": 10}], "coupons": [{"coupon_id": "B3", "D": 3, "requires": {"B": 1}}], "snapshot_id": "cmp-bing-new"},
            {"op": "register_basket", "basket_id": "K", "items": [{"unit_id": "a", "sku": "A", "price": 10}, {"unit_id": "b", "sku": "B", "price": 10}]},
            {"op": "register_basket", "basket_id": "L", "items": [{"unit_id": "a", "sku": "A", "price": 10}, {"unit_id": "c", "sku": "C", "price": 10}]},
            {"op": "register_basket", "basket_id": "M", "items": [{"unit_id": "b", "sku": "B", "price": 10}]},
            {"op": "register_statement", "statement_id": "S1", "vendor": "甲", "weight": 6, "old_snapshot": "cmp-jia-old", "new_snapshot": "cmp-jia-new", "allowed_baskets": ["K"], "assertions": [{"kind": "UNIT_PAID_UNCHANGED", "unit_id": "a"}]},
            {"op": "register_statement", "statement_id": "S2", "vendor": "甲", "weight": 4, "old_snapshot": "cmp-jia-old", "new_snapshot": "cmp-jia-new", "allowed_baskets": ["K"], "assertions": [{"kind": "TOTAL_UNCHANGED"}]},
            {"op": "register_statement", "statement_id": "S3", "vendor": "乙", "weight": 8, "old_snapshot": "cmp-yi-old", "new_snapshot": "cmp-yi-new", "allowed_baskets": ["L"], "assertions": [{"kind": "UNIT_PAID_UNCHANGED", "unit_id": "a"}]},
            {"op": "register_statement", "statement_id": "S4", "vendor": "丙", "weight": 5, "old_snapshot": "cmp-bing-old", "new_snapshot": "cmp-bing-new", "allowed_baskets": ["M"], "assertions": [{"kind": "UNIT_PAID_DECREASES", "unit_id": "b"}]},
            {"op": "compare_basket", "basket_id": "L", "old_snapshot": "cmp-yi-old", "new_snapshot": "cmp-yi-new"},
            {"op": "find_counterexamples", "statement_id": "S3"},
            {"name": "exhibit_plan", "op": "plan_exhibition", "booths": 2},
            {"op": "collect_booking", "plan_id": "$exhibit_plan.plan_id", "basket_id": "L"},
            {"op": "withdraw_sample", "unit_id": "a"},
            {"op": "reopen_statement", "statement_id": "S3"},
            {"op": "exhibition_status"},
        ],
    }

    _write_json(Path(out_path), results)
    _write_json(Path(plan_path), results["plan"])
    return results


def _run_plan_file(db_path: str, plan_file: str, out_file: str | None) -> Dict[str, Any]:
    svc = open_service(db_path)
    plan = json.loads(Path(plan_file).read_text(encoding="utf-8"))
    result = svc.run(plan)
    if out_file:
        _write_json(Path(out_file), result)
    return result


def main(argv: List[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m basket_service", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="run the full demo and write database/results")
    demo.add_argument("--db", default=str(ROOT / "data" / "demo.db"))
    demo.add_argument("--out", default=str(ROOT / "results" / "demo_results.json"))
    demo.add_argument("--plan", default=str(ROOT / "results" / "demo_plan.json"))

    run_plan = sub.add_parser("run-plan", help="execute a JSON plan against a database")
    run_plan.add_argument("plan")
    run_plan.add_argument("--db", required=True)
    run_plan.add_argument("--out")

    selfcheck = sub.add_parser("self-check", help="run the unit test suite")
    selfcheck.add_argument("--db", default=str(ROOT / "data" / "selfcheck.db"))
    selfcheck.add_argument("--out", default=str(ROOT / "results" / "self_check.json"))

    args = parser.parse_args(argv)
    if args.command == "demo":
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        os.makedirs(os.path.dirname(args.db) or ".", exist_ok=True)
        results = run_demo(args.db, args.out, args.plan)
        print(json.dumps({"database": args.db, "results": args.out, "summary": results["summary"]}, indent=2))
        return 0 if results["summary"]["failed"] == 0 else 1
    if args.command == "run-plan":
        result = _run_plan_file(args.db, args.plan, args.out)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if args.command == "self-check":
        import unittest

        loader = unittest.TestLoader()
        suite = loader.discover(str(ROOT / "tests"))
        runner = unittest.TextTestRunner(verbosity=2)
        outcome = runner.run(suite)
        payload = {
            "ok": outcome.wasSuccessful(),
            "tests": outcome.testsRun,
            "failures": [str(t) for t, _ in outcome.failures],
            "errors": [str(t) for t, _ in outcome.errors],
        }
        _write_json(Path(args.out), payload)
        return 0 if outcome.wasSuccessful() else 1
    return 2
