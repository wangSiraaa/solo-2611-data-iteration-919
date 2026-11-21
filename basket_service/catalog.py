"""Display-basket comparison catalog (对照目录) for promotion-change reviews.

The catalog is the counter-facing half of changing bundle-promotion terms
without touching every shelf tag:

* The store registers a finite set of *real* display baskets.  Every unit is a
  physical sample with a stable ``unit_id``, its SKU and its original price.
  The sample registry pins ``unit_id -> (sku, price)``: a second basket that
  mentions the same sample must agree on its attributes, so a fabricated
  cheaper unit can never be substituted for the specified sample
  (``SAMPLE_CONFLICT``).  There is deliberately no API that quotes a
  registered basket from caller-supplied items — comparisons always run on
  the registered units verbatim.
* A vendor *change statement* names the frozen old/new snapshots, the allowed
  demo baskets and the financial assertions to check (e.g. "this unit's paid
  amount stays", "this unit's paid share drops after the switch").
* Both promotion versions are quoted from their immutable snapshots against
  the *whole* registered basket, and the comparison report carries per-unit
  return liability (paid amounts), not just the basket total — so a
  same-total re-allocation, or a coupon id that moved to a different physical
  unit, is visible instead of being masked by an unchanged total.
* ``find_counterexamples`` tells the counter on which allowed real baskets a
  statement has a replayable counterexample.  Evidence quotes are stored
  under deterministic request ids (``cmp:<basket>:<old>:<new>:old|new``), so
  the counter can re-run them at any time and replay byte-for-byte.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .db import Database, canonical_json, json_loads, utc_now
from .errors import (
    BASKET_CONFLICT,
    INVALID_ASSERTION,
    INVALID_INPUT,
    SAMPLE_CONFLICT,
    STATEMENT_CONFLICT,
    UNKNOWN_BASKET,
    UNKNOWN_STATEMENT,
    BasketError,
)
from .quote_service import QuoteService
from .validation import is_int, require_id, validate_basket

ASSERTION_KINDS = (
    "UNIT_PAID_UNCHANGED",
    "UNIT_PAID_NOT_ABOVE",
    "UNIT_PAID_DECREASES",
    "UNIT_DISCOUNT_NOT_ABOVE",
    "TOTAL_UNCHANGED",
    "TOTAL_NOT_ABOVE",
    "COUPON_SAME_UNITS",
)

_UNIT_KINDS = frozenset({"UNIT_PAID_UNCHANGED", "UNIT_PAID_NOT_ABOVE", "UNIT_PAID_DECREASES", "UNIT_DISCOUNT_NOT_ABOVE"})
_AMOUNT_KINDS = frozenset({"UNIT_PAID_NOT_ABOVE", "UNIT_DISCOUNT_NOT_ABOVE", "TOTAL_NOT_ABOVE"})

# Comparison report findings.
TOTAL_CHANGED = "TOTAL_CHANGED"
ALLOCATION_CHANGED = "ALLOCATION_CHANGED"
SAME_TOTAL_DIFFERENT_ALLOCATION = "SAME_TOTAL_DIFFERENT_ALLOCATION"
COUPON_RETARGETED = "COUPON_RETARGETED"
UNCHANGED = "UNCHANGED"


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_assertions(assertions: Any) -> List[Dict[str, Any]]:
    """Canonicalise a statement's assertion list; reject unknown shapes."""

    if not isinstance(assertions, Sequence) or isinstance(assertions, (str, bytes)) or len(assertions) == 0:
        raise BasketError(INVALID_ASSERTION, "assertions must be a non-empty list")
    result: List[Dict[str, Any]] = []
    for index, item in enumerate(assertions):
        if not isinstance(item, Mapping):
            raise BasketError(INVALID_ASSERTION, "assertion must be an object", {"index": index})
        kind = item.get("kind")
        if kind not in ASSERTION_KINDS:
            raise BasketError(INVALID_ASSERTION, "unknown assertion kind", {"index": index, "kind": kind})
        canonical: Dict[str, Any] = {"kind": kind}
        if kind in _UNIT_KINDS:
            canonical["unit_id"] = require_id(item.get("unit_id"), INVALID_ASSERTION, f"assertions[{index}].unit_id")
        if kind == "COUPON_SAME_UNITS":
            canonical["coupon_id"] = require_id(item.get("coupon_id"), INVALID_ASSERTION, f"assertions[{index}].coupon_id")
        if kind in _AMOUNT_KINDS:
            amount = item.get("amount")
            if not is_int(amount) or amount < 0:
                raise BasketError(INVALID_ASSERTION, "amount must be a non-negative integer", {"index": index, "amount": amount})
            canonical["amount"] = amount
        result.append(canonical)
    return result


def evaluate_assertions(
    assertions: Sequence[Mapping[str, Any]],
    old_quote: Mapping[str, Any],
    new_quote: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Evaluate assertions against a paired old/new quote of the same basket.

    Each result is ``{"assertion", "status", "observed"}`` with status
    ``HOLDS``, ``VIOLATED`` or ``NOT_APPLICABLE`` (the asserted unit/coupon
    does not occur in this basket, so the basket says nothing about it).
    A basket is a counterexample for a statement iff at least one assertion
    is ``VIOLATED``.
    """

    old_paid = {e["unit_id"]: e["paid"] for e in old_quote["allocation"]}
    new_paid = {e["unit_id"]: e["paid"] for e in new_quote["allocation"]}
    new_discount = {e["unit_id"]: e["discount"] for e in new_quote["allocation"]}
    old_assign = old_quote["assignments"]
    new_assign = new_quote["assignments"]
    results: List[Dict[str, Any]] = []
    for assertion in assertions:
        kind = assertion["kind"]
        if kind in _UNIT_KINDS:
            unit_id = assertion["unit_id"]
            if unit_id not in old_paid:  # both quotes share the basket, so absence is symmetric
                results.append({
                    "assertion": dict(assertion),
                    "status": "NOT_APPLICABLE",
                    "observed": {"unit_id": unit_id, "reason": "unit is not part of this basket"},
                })
                continue
            observed: Dict[str, Any] = {"paid_old": old_paid[unit_id], "paid_new": new_paid[unit_id]}
            if kind == "UNIT_PAID_UNCHANGED":
                holds = old_paid[unit_id] == new_paid[unit_id]
            elif kind == "UNIT_PAID_NOT_ABOVE":
                observed["bound"] = assertion["amount"]
                holds = new_paid[unit_id] <= assertion["amount"]
            elif kind == "UNIT_PAID_DECREASES":
                holds = new_paid[unit_id] < old_paid[unit_id]
            else:  # UNIT_DISCOUNT_NOT_ABOVE
                observed = {"discount_new": new_discount[unit_id], "bound": assertion["amount"]}
                holds = new_discount[unit_id] <= assertion["amount"]
        elif kind == "TOTAL_UNCHANGED":
            observed = {"total_old": old_quote["total"], "total_new": new_quote["total"]}
            holds = old_quote["total"] == new_quote["total"]
        elif kind == "TOTAL_NOT_ABOVE":
            observed = {"total_new": new_quote["total"], "bound": assertion["amount"]}
            holds = new_quote["total"] <= assertion["amount"]
        else:  # COUPON_SAME_UNITS
            coupon_id = assertion["coupon_id"]
            units_old = old_assign.get(coupon_id)
            units_new = new_assign.get(coupon_id)
            if units_old is None and units_new is None:
                results.append({
                    "assertion": dict(assertion),
                    "status": "NOT_APPLICABLE",
                    "observed": {"coupon_id": coupon_id, "reason": "coupon is unused in both versions"},
                })
                continue
            observed = {"units_old": units_old, "units_new": units_new}
            holds = units_old == units_new
        results.append({"assertion": dict(assertion), "status": "HOLDS" if holds else "VIOLATED", "observed": observed})
    return results


def build_report(
    basket_id: str,
    old_snapshot: str,
    new_snapshot: str,
    old_quote: Mapping[str, Any],
    new_quote: Mapping[str, Any],
) -> Dict[str, Any]:
    """Side-by-side comparison of one basket quoted under two snapshots.

    Beyond the totals, the report carries each unit's paid/discount/coupon in
    both versions (the return liability) and each coupon's bound physical
    units, plus machine-readable findings:

    * ``SAME_TOTAL_DIFFERENT_ALLOCATION`` — totals match but some unit's paid
      amount moved (同总价不同折扣归属);
    * ``COUPON_RETARGETED`` — the same coupon id is bound to different
      physical units in the two versions (同券号换了实物).
    """

    old_alloc = {e["unit_id"]: e for e in old_quote["allocation"]}
    new_alloc = {e["unit_id"]: e for e in new_quote["allocation"]}
    units: List[Dict[str, Any]] = []
    allocation_changed = False
    for unit in old_quote["basket"]:
        unit_id = unit["unit_id"]
        old_entry, new_entry = old_alloc[unit_id], new_alloc[unit_id]
        changed = old_entry["paid"] != new_entry["paid"]
        allocation_changed = allocation_changed or changed
        units.append({
            "unit_id": unit_id,
            "sku": unit["sku"],
            "price": unit["price"],
            "coupon_old": old_entry["coupon_id"],
            "coupon_new": new_entry["coupon_id"],
            "discount_old": old_entry["discount"],
            "discount_new": new_entry["discount"],
            "paid_old": old_entry["paid"],
            "paid_new": new_entry["paid"],
            "paid_changed": changed,
        })
    coupons: List[Dict[str, Any]] = []
    retargeted = False
    for coupon_id in sorted(set(old_quote["assignments"]) | set(new_quote["assignments"])):
        units_old = old_quote["assignments"].get(coupon_id)
        units_new = new_quote["assignments"].get(coupon_id)
        moved = units_old is not None and units_new is not None and units_old != units_new
        retargeted = retargeted or moved
        coupons.append({
            "coupon_id": coupon_id,
            "units_old": units_old,
            "units_new": units_new,
            "retargeted": moved,
            "dropped": units_old is not None and units_new is None,
            "added": units_old is None and units_new is not None,
        })
    findings: List[str] = []
    total_changed = old_quote["total"] != new_quote["total"]
    if total_changed:
        findings.append(TOTAL_CHANGED)
    if allocation_changed:
        findings.append(ALLOCATION_CHANGED)
    if not total_changed and allocation_changed:
        findings.append(SAME_TOTAL_DIFFERENT_ALLOCATION)
    if retargeted:
        findings.append(COUPON_RETARGETED)
    if not findings:
        findings.append(UNCHANGED)
    return {
        "basket_id": basket_id,
        "old_snapshot": old_snapshot,
        "new_snapshot": new_snapshot,
        "old_quote_request": old_quote["request_id"],
        "new_quote_request": new_quote["request_id"],
        "total": {"old": old_quote["total"], "new": new_quote["total"], "changed": total_changed},
        "units": units,
        "coupons": coupons,
        "findings": findings,
    }


class ComparisonCatalog:
    """Registered samples, display baskets, statements and paired quotes."""

    def __init__(self, db: Database, quotes: QuoteService):
        self.db = db
        self.quotes = quotes

    # -- samples and display baskets ----------------------------------------
    def register_basket(self, basket_id: str, items: Any) -> Dict[str, Any]:
        """Register a real display basket; pin its samples' attributes.

        Idempotent for identical content.  Re-registering with different
        items is ``BASKET_CONFLICT``; reusing a known sample id with a
        different sku/price is ``SAMPLE_CONFLICT`` — a generated cheap unit
        cannot replace a specified sample.
        """

        basket_id = require_id(basket_id, INVALID_INPUT, "basket_id")
        units = validate_basket(items)
        signature = _hash({"basket_id": basket_id, "items": units})
        with self.db.transaction() as conn:
            row = conn.execute("SELECT content_sig FROM display_baskets WHERE basket_id = ?", (basket_id,)).fetchone()
            if row is not None:
                if row["content_sig"] != signature:
                    raise BasketError(BASKET_CONFLICT, "basket id already registered with different items", {"basket_id": basket_id})
                replayed = True
            else:
                for unit in units:
                    sample = conn.execute("SELECT sku, price FROM samples WHERE unit_id = ?", (unit["unit_id"],)).fetchone()
                    if sample is None:
                        conn.execute(
                            "INSERT INTO samples (unit_id, sku, price, withdrawn, created_at) VALUES (?, ?, ?, 0, ?)",
                            (unit["unit_id"], unit["sku"], unit["price"], utc_now()),
                        )
                    elif sample["sku"] != unit["sku"] or sample["price"] != unit["price"]:
                        raise BasketError(
                            SAMPLE_CONFLICT,
                            "sample already registered with different attributes; a fabricated unit cannot replace the specified sample",
                            {
                                "unit_id": unit["unit_id"],
                                "registered": {"sku": sample["sku"], "price": sample["price"]},
                                "supplied": {"sku": unit["sku"], "price": unit["price"]},
                            },
                        )
                conn.execute(
                    "INSERT INTO display_baskets (basket_id, items_json, content_sig, created_at) VALUES (?, ?, ?, ?)",
                    (basket_id, canonical_json(units), signature, utc_now()),
                )
                replayed = False
        return {"basket_id": basket_id, "items": units, "samples": [u["unit_id"] for u in units], "replayed": replayed}

    def get_basket(self, basket_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM display_baskets WHERE basket_id = ?", (basket_id,)).fetchone()
            if row is None:
                raise BasketError(UNKNOWN_BASKET, "unknown display basket", {"basket_id": basket_id})
            items = json_loads(row["items_json"])
            withdrawn = {r["unit_id"] for r in conn.execute("SELECT unit_id FROM samples WHERE withdrawn = 1").fetchall()}
        samples = [u["unit_id"] for u in items]
        return {
            "basket_id": basket_id,
            "items": items,
            "samples": samples,
            "withdrawn_samples": [u for u in samples if u in withdrawn],
            "lendable": not any(u in withdrawn for u in samples),
            "created_at": row["created_at"],
        }

    def list_baskets(self) -> List[str]:
        with self.db.connection() as conn:
            rows = conn.execute("SELECT basket_id FROM display_baskets ORDER BY basket_id").fetchall()
        return [r["basket_id"] for r in rows]

    def samples(self) -> List[Dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute("SELECT * FROM samples ORDER BY unit_id").fetchall()
        return [
            {"unit_id": r["unit_id"], "sku": r["sku"], "price": r["price"], "withdrawn": bool(r["withdrawn"])}
            for r in rows
        ]

    def basket_map(self) -> Dict[str, Dict[str, Any]]:
        """All registered baskets: id -> items/samples/lendable (planner input)."""

        with self.db.connection() as conn:
            rows = conn.execute("SELECT basket_id, items_json FROM display_baskets ORDER BY basket_id").fetchall()
            withdrawn = {r["unit_id"] for r in conn.execute("SELECT unit_id FROM samples WHERE withdrawn = 1").fetchall()}
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            items = json_loads(row["items_json"])
            samples = [u["unit_id"] for u in items]
            result[row["basket_id"]] = {
                "items": items,
                "samples": samples,
                "lendable": not any(s in withdrawn for s in samples),
            }
        return result

    # -- change statements ----------------------------------------------------
    def register_statement(
        self,
        statement_id: str,
        *,
        vendor: str,
        weight: Any,
        old_snapshot: str,
        new_snapshot: str,
        allowed_baskets: Any,
        assertions: Any,
    ) -> Dict[str, Any]:
        """Register a vendor change statement (idempotent by content).

        The statement pins the frozen old/new snapshots, the allowed demo
        baskets (which must already be registered — no ad-hoc baskets) and
        the financial assertions to verify.  ``weight`` is a non-negative
        integer counted at most once per statement.
        """

        statement_id = require_id(statement_id, INVALID_INPUT, "statement_id")
        vendor = require_id(vendor, INVALID_INPUT, "vendor")
        if not is_int(weight) or weight < 0:
            raise BasketError(INVALID_INPUT, "weight must be a non-negative integer", {"weight": weight})
        old_snapshot = require_id(old_snapshot, INVALID_INPUT, "old_snapshot")
        new_snapshot = require_id(new_snapshot, INVALID_INPUT, "new_snapshot")
        old_snap = self.quotes.get_snapshot(old_snapshot)  # UNKNOWN_SNAPSHOT propagates
        new_snap = self.quotes.get_snapshot(new_snapshot)
        if not isinstance(allowed_baskets, Sequence) or isinstance(allowed_baskets, (str, bytes)) or len(allowed_baskets) == 0:
            raise BasketError(INVALID_INPUT, "allowed_baskets must be a non-empty list of basket ids")
        allowed = sorted({require_id(bid, INVALID_INPUT, "allowed_baskets[]") for bid in allowed_baskets})
        baskets = {bid: self.get_basket(bid) for bid in allowed}  # UNKNOWN_BASKET propagates
        canonical_assertions = validate_assertions(assertions)
        known_units = {u["unit_id"] for basket in baskets.values() for u in basket["items"]}
        snapshot_coupons = {c["coupon_id"] for c in old_snap["coupons"]} | {c["coupon_id"] for c in new_snap["coupons"]}
        for assertion in canonical_assertions:
            if "unit_id" in assertion and assertion["unit_id"] not in known_units:
                raise BasketError(
                    INVALID_ASSERTION,
                    "assertion unit is not part of any allowed basket",
                    {"unit_id": assertion["unit_id"]},
                )
            if "coupon_id" in assertion and assertion["coupon_id"] not in snapshot_coupons:
                raise BasketError(
                    INVALID_ASSERTION,
                    "assertion coupon is present in neither snapshot",
                    {"coupon_id": assertion["coupon_id"]},
                )
        content = {
            "statement_id": statement_id,
            "vendor": vendor,
            "weight": weight,
            "old_snapshot": old_snapshot,
            "new_snapshot": new_snapshot,
            "allowed_baskets": allowed,
            "assertions": canonical_assertions,
        }
        signature = _hash(content)
        with self.db.transaction() as conn:
            row = conn.execute("SELECT content_sig FROM change_statements WHERE statement_id = ?", (statement_id,)).fetchone()
            if row is not None:
                if row["content_sig"] != signature:
                    raise BasketError(STATEMENT_CONFLICT, "statement id already registered with different content", {"statement_id": statement_id})
                replayed = True
            else:
                conn.execute(
                    "INSERT INTO change_statements (statement_id, vendor, weight, old_snapshot, new_snapshot, allowed_json, assertions_json, content_sig, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        statement_id,
                        vendor,
                        weight,
                        old_snapshot,
                        new_snapshot,
                        canonical_json(allowed),
                        canonical_json(canonical_assertions),
                        signature,
                        utc_now(),
                    ),
                )
                replayed = False
        return dict(content, replayed=replayed)

    def get_statement(self, statement_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM change_statements WHERE statement_id = ?", (statement_id,)).fetchone()
            if row is None:
                raise BasketError(UNKNOWN_STATEMENT, "unknown change statement", {"statement_id": statement_id})
            exhibited = (
                conn.execute("SELECT 1 FROM exhibited_statements WHERE statement_id = ?", (statement_id,)).fetchone() is not None
            )
        return {
            "statement_id": row["statement_id"],
            "vendor": row["vendor"],
            "weight": row["weight"],
            "old_snapshot": row["old_snapshot"],
            "new_snapshot": row["new_snapshot"],
            "allowed_baskets": json_loads(row["allowed_json"]),
            "assertions": json_loads(row["assertions_json"]),
            "status": "EXHIBITED" if exhibited else "PENDING",
            "created_at": row["created_at"],
        }

    def list_statements(self) -> List[str]:
        with self.db.connection() as conn:
            rows = conn.execute("SELECT statement_id FROM change_statements ORDER BY statement_id").fetchall()
        return [r["statement_id"] for r in rows]

    def pending_statements(self) -> List[Dict[str, Any]]:
        """Registered statements not yet exhibited, ordered by id."""

        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT statement_id FROM change_statements"
                " WHERE statement_id NOT IN (SELECT statement_id FROM exhibited_statements) ORDER BY statement_id"
            ).fetchall()
        return [self.get_statement(row["statement_id"]) for row in rows]

    # -- paired quoting ---------------------------------------------------------
    def comparison_quotes(self, basket_id: str, old_snapshot: str, new_snapshot: str):
        """Quote the *registered* basket under both frozen snapshots.

        Request ids are deterministic, so the evidence is replayable: the
        same pair re-run later (or after a restart) returns the stored quotes
        byte-for-byte.  Items always come from the catalog, never from the
        caller.
        """

        basket = self.get_basket(basket_id)
        tag = f"cmp:{basket_id}:{old_snapshot}:{new_snapshot}"
        old_quote = self.quotes.quote(basket["items"], [], f"{tag}:old", old_snapshot)
        new_quote = self.quotes.quote(basket["items"], [], f"{tag}:new", new_snapshot)
        return old_quote, new_quote

    def compare_basket(self, basket_id: str, old_snapshot: str, new_snapshot: str) -> Dict[str, Any]:
        old_quote, new_quote = self.comparison_quotes(basket_id, old_snapshot, new_snapshot)
        return build_report(basket_id, old_snapshot, new_snapshot, old_quote, new_quote)

    def find_counterexamples(self, statement_id: str) -> Dict[str, Any]:
        """Check a statement on every allowed real basket.

        Returns, per allowed basket, the assertion outcomes and — for the
        baskets where at least one assertion is violated — the replayable
        counterexample evidence (paired quote request ids plus the comparison
        report).  Only registered catalog baskets are consulted; the search
        space is exactly the statement's allowed list.
        """

        statement = self.get_statement(statement_id)
        rows: List[Dict[str, Any]] = []
        counterexamples: List[str] = []
        for basket_id in statement["allowed_baskets"]:
            basket = self.get_basket(basket_id)
            entry: Dict[str, Any] = {"basket_id": basket_id, "lendable": basket["lendable"]}
            try:
                old_quote, new_quote = self.comparison_quotes(basket_id, statement["old_snapshot"], statement["new_snapshot"])
            except BasketError as exc:
                # e.g. a basket sku missing from the statement's snapshot
                # catalogue: recorded, never silently treated as a counterexample.
                entry.update({"counterexample": False, "error": exc.to_dict()["error"]})
                rows.append(entry)
                continue
            outcomes = evaluate_assertions(statement["assertions"], old_quote, new_quote)
            failing = [outcome for outcome in outcomes if outcome["status"] == "VIOLATED"]
            entry.update({
                "counterexample": bool(failing),
                "assertion_results": outcomes,
                "failing_assertions": failing,
                "report": build_report(basket_id, statement["old_snapshot"], statement["new_snapshot"], old_quote, new_quote),
                "old_quote_request": old_quote["request_id"],
                "new_quote_request": new_quote["request_id"],
            })
            if failing:
                counterexamples.append(basket_id)
            rows.append(entry)
        return {
            "statement_id": statement_id,
            "vendor": statement["vendor"],
            "weight": statement["weight"],
            "old_snapshot": statement["old_snapshot"],
            "new_snapshot": statement["new_snapshot"],
            "counterexample_baskets": counterexamples,
            "baskets": rows,
        }


__all__ = [
    "ASSERTION_KINDS",
    "ComparisonCatalog",
    "build_report",
    "evaluate_assertions",
    "validate_assertions",
]
