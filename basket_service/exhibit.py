"""Exhibition lending plans (布展借单) for vendor counterexample demos.

Model
-----
* The sample library lends each physical sample to at most one booth per day.
* A booth hosts exactly one basket, booked by one vendor; that vendor may
  prove any number of its statements with the basket's paired old/new quotes.
  Other vendors cannot share the lent samples, so the baskets of two bookings
  must be sample-disjoint and a basket appears in at most one booking.
* A statement is *proved* when some booking (basket, vendor of the statement)
  uses a basket from the statement's allowed list on which the frozen old/new
  snapshots violate at least one assertion — a replayable counterexample.
  Statement weights are non-negative and counted once, however many bookings
  could prove the statement.
* ``plan_exhibition`` computes an exact max-weight lending order naming, for
  every proved statement, the real basket, the booth and the old/new quote
  pair that supports it.  Collecting a booking (签领) moves its statements
  into the exhibited archive together with the original quotes and the
  per-unit return amounts.  Withdrawing a sample supersedes the active plan
  and re-plans only the not-yet-collected bookings; exhibited history is
  never rewritten and can be reopened with its original evidence.

The search (:func:`optimize_bookings`) is an exact branch-and-bound over
baskets: a suffix-OR upper bound prunes dominated branches, and once the
remaining baskets cannot cover anything new the branch is closed by
considering the current selection.  At the stated limits (<= 20 baskets,
<= 16 statements) this is effectively instantaneous.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .catalog import ComparisonCatalog, evaluate_assertions
from .db import Database, canonical_json, json_loads, utc_now
from .errors import (
    INVALID_INPUT,
    PLAN_NOT_ACTIVE,
    STATEMENT_NOT_EXHIBITED,
    UNKNOWN_BOOKING,
    UNKNOWN_PLAN,
    UNKNOWN_SAMPLE,
    BasketError,
)
from .quote_service import QuoteService
from .validation import is_int, require_id


def optimize_bookings(
    options: Mapping[str, Mapping[str, int]],
    sample_masks: Mapping[str, int],
    weights: Sequence[int],
    booths: int,
) -> Tuple[int, List[Tuple[str, str]]]:
    """Exact max-weight booking search (pure function).

    ``options[basket][vendor]`` is the bitmask of statements that booking
    ``(basket, vendor)`` would prove; ``sample_masks[basket]`` is the basket's
    sample bitmask; ``weights[i]`` is statement ``i``'s weight; ``booths``
    caps the number of bookings.  Two bookings may not share samples and a
    basket may be booked at most once (its samples would collide with
    themselves).  Returns ``(covered_mask, bookings)`` with bookings sorted.

    Ties are broken deterministically: fewer bookings first, then the
    lexicographically smallest sorted ``(basket, vendor)`` list.  Bookings
    that add no new covered statement are never included.
    """

    ids = sorted(set(options) | set(sample_masks))
    n = len(ids)
    opts: Dict[str, List[Tuple[str, int]]] = {b: sorted(options.get(b, {}).items()) for b in ids}
    masks: Dict[str, int] = {b: sample_masks.get(b, 0) for b in ids}
    suffix_or = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        union = 0
        for _, mask in opts[ids[i]]:
            union |= mask
        suffix_or[i] = suffix_or[i + 1] | union

    weight_cache: Dict[int, int] = {0: 0}

    def mask_weight(mask: int) -> int:
        cached = weight_cache.get(mask)
        if cached is None:
            total = 0
            remaining = mask
            while remaining:
                lsb = remaining & -remaining
                total += weights[lsb.bit_length() - 1]
                remaining ^= lsb
            weight_cache[mask] = total
            return total
        return cached

    # best = [weight, sorted bookings tuple, covered mask]
    best: List[Any] = [0, (), 0]

    def consider(covered: int, chosen: List[Tuple[str, str]]) -> None:
        weight = mask_weight(covered)
        bookings = tuple(sorted(chosen))
        incumbent_weight, incumbent_bookings = best[0], best[1]
        better = (
            weight > incumbent_weight
            or (weight == incumbent_weight and len(bookings) < len(incumbent_bookings))
            or (weight == incumbent_weight and len(bookings) == len(incumbent_bookings) and bookings < incumbent_bookings)
        )
        if better:
            best[0], best[1], best[2] = weight, bookings, covered

    # Greedy warm start: repeatedly take the feasible booking with the
    # largest marginal weight (smallest (basket, vendor) on ties).
    covered, chosen, used = 0, [], 0
    while len(chosen) < booths:
        candidates = []
        for b in ids:
            if masks[b] & used:
                continue
            for vendor, mask in opts[b]:
                gain = mask_weight(mask & ~covered)
                if gain > 0:
                    candidates.append((-gain, b, vendor, mask))
        if not candidates:
            break
        candidates.sort()
        _, b, vendor, mask = candidates[0]
        chosen.append((b, vendor))
        used |= masks[b]
        covered |= mask
    consider(covered, chosen)

    seen = set()

    def search(i: int, used: int, covered: int, chosen: List[Tuple[str, str]]) -> None:
        if suffix_or[i] & ~covered == 0:
            # Nothing new is reachable from here: the current selection is the
            # best completion of this branch.
            consider(covered, chosen)
            return
        if mask_weight(covered | suffix_or[i]) < best[0]:
            return  # even the optimistic bound cannot beat the incumbent
        key = (i, used, covered)
        if key in seen:
            return
        seen.add(key)
        basket = ids[i]
        if len(chosen) < booths and not (masks[basket] & used):
            for vendor, mask in opts[basket]:
                if mask & ~covered:
                    search(i + 1, used | masks[basket], covered | mask, chosen + [(basket, vendor)])
        search(i + 1, used, covered, chosen)

    search(0, 0, 0, [])
    return best[2], list(best[1])


class ExhibitionPlanner:
    """Plans, collects and re-plans exhibition lending orders."""

    def __init__(self, db: Database, quotes: QuoteService, catalog: ComparisonCatalog):
        self.db = db
        self.quotes = quotes
        self.catalog = catalog

    # -- planning --------------------------------------------------------------
    def plan_exhibition(self, booths: Any) -> Dict[str, Any]:
        """Compute and store a max-weight lending order over pending statements.

        Only lendable baskets (no withdrawn samples) are considered, and only
        statements not yet exhibited.  Any previous active plan is superseded
        — its uncollected bookings are replaced by this plan.
        """

        if not is_int(booths) or booths < 0:
            raise BasketError(INVALID_INPUT, "booths must be a non-negative integer", {"booths": booths})
        statements = self.catalog.pending_statements()
        baskets = self.catalog.basket_map()
        lendable = {bid: b for bid, b in baskets.items() if b["lendable"]}
        stmt_index = {st["statement_id"]: i for i, st in enumerate(statements)}
        weights = [st["weight"] for st in statements]

        options: Dict[str, Dict[str, int]] = {}
        evidence: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        warnings: List[Dict[str, Any]] = []
        for statement in statements:
            for basket_id in statement["allowed_baskets"]:
                if basket_id not in lendable:
                    continue
                try:
                    old_quote, new_quote = self.catalog.comparison_quotes(
                        basket_id, statement["old_snapshot"], statement["new_snapshot"]
                    )
                except BasketError as exc:
                    warnings.append({
                        "statement_id": statement["statement_id"],
                        "basket_id": basket_id,
                        "error": exc.to_dict()["error"],
                    })
                    continue
                failing = [
                    outcome
                    for outcome in evaluate_assertions(statement["assertions"], old_quote, new_quote)
                    if outcome["status"] == "VIOLATED"
                ]
                if not failing:
                    continue
                bit = 1 << stmt_index[statement["statement_id"]]
                options.setdefault(basket_id, {}).setdefault(statement["vendor"], 0)
                options[basket_id][statement["vendor"]] |= bit
                evidence.setdefault((basket_id, statement["vendor"]), []).append({
                    "statement_id": statement["statement_id"],
                    "weight": statement["weight"],
                    "failing_assertions": failing,
                    "old_quote_request": old_quote["request_id"],
                    "new_quote_request": new_quote["request_id"],
                })

        sample_ids = sorted({sample for basket in lendable.values() for sample in basket["samples"]})
        sample_bit = {sample: 1 << i for i, sample in enumerate(sample_ids)}
        sample_masks = {bid: sum(sample_bit[s] for s in basket["samples"]) for bid, basket in lendable.items()}
        covered, bookings = optimize_bookings(options, sample_masks, weights, booths)

        booking_entries = []
        for booth, (basket_id, vendor) in enumerate(bookings, start=1):
            booking_entries.append({
                "booth": booth,
                "basket_id": basket_id,
                "vendor": vendor,
                "samples": lendable[basket_id]["samples"],
                "statements": sorted(evidence[(basket_id, vendor)], key=lambda e: e["statement_id"]),
            })

        with self.db.transaction() as conn:
            previous = conn.execute("SELECT plan_id FROM exhibition_plans WHERE status = 'ACTIVE'").fetchone()
            if previous is not None:
                conn.execute("UPDATE exhibition_plans SET status = 'SUPERSEDED' WHERE plan_id = ?", (previous["plan_id"],))
            seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM exhibition_plans").fetchone()["next"]
            plan_id = f"plan-{seq}"
            plan = {
                "plan_id": plan_id,
                "booths": booths,
                "status": "ACTIVE",
                "supersedes": previous["plan_id"] if previous else None,
                "bookings": booking_entries,
                "covered_statement_ids": [sid for sid, i in stmt_index.items() if covered >> i & 1],
                "total_weight": sum(w for i, w in enumerate(weights) if covered >> i & 1),
                "pending_statement_ids": [sid for sid, i in stmt_index.items() if not (covered >> i & 1)],
                "warnings": warnings,
                "created_at": utc_now(),
            }
            conn.execute(
                "INSERT INTO exhibition_plans (plan_id, seq, booths, status, plan_json, created_at) VALUES (?, ?, ?, 'ACTIVE', ?, ?)",
                (plan_id, seq, booths, canonical_json(plan), plan["created_at"]),
            )
        return plan

    def get_plan(self, plan_id: str) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute("SELECT status, plan_json FROM exhibition_plans WHERE plan_id = ?", (plan_id,)).fetchone()
        if row is None:
            raise BasketError(UNKNOWN_PLAN, "unknown exhibition plan", {"plan_id": plan_id})
        plan = json_loads(row["plan_json"])
        plan["status"] = row["status"]
        return plan

    # -- collection (签领) -------------------------------------------------------
    def collect_booking(self, plan_id: str, basket_id: str) -> Dict[str, Any]:
        """Mark a booking collected; archive its statements as exhibited.

        The archived record binds the original old/new quotes and the per-unit
        return amounts, so an exhibited statement can later be reopened with
        exactly the evidence it was shown with.  Collecting twice replays the
        first record; collecting from a superseded plan is rejected.
        """

        plan_id = require_id(plan_id, INVALID_INPUT, "plan_id")
        basket_id = require_id(basket_id, INVALID_INPUT, "basket_id")
        with self.db.connection() as conn:
            row = conn.execute("SELECT status, plan_json FROM exhibition_plans WHERE plan_id = ?", (plan_id,)).fetchone()
            if row is None:
                raise BasketError(UNKNOWN_PLAN, "unknown exhibition plan", {"plan_id": plan_id})
            already = (
                conn.execute(
                    "SELECT 1 FROM collected_bookings WHERE plan_id = ? AND basket_id = ?", (plan_id, basket_id)
                ).fetchone()
                is not None
            )
        plan = json_loads(row["plan_json"])
        booking = next((b for b in plan["bookings"] if b["basket_id"] == basket_id), None)
        if booking is None:
            raise BasketError(UNKNOWN_BOOKING, "plan has no booking for this basket", {"plan_id": plan_id, "basket_id": basket_id})
        if already:
            return self._collection_record(plan_id, basket_id, replayed=True)
        if row["status"] != "ACTIVE":
            raise BasketError(
                PLAN_NOT_ACTIVE,
                "plan is no longer active; its uncollected bookings have been re-planned",
                {"plan_id": plan_id},
            )
        # Archive payloads are built from the immutable stored quotes, so
        # recomputing them here is deterministic.
        entries = []
        for statement_evidence in booking["statements"]:
            statement = self.catalog.get_statement(statement_evidence["statement_id"])
            report = self.catalog.compare_basket(basket_id, statement["old_snapshot"], statement["new_snapshot"])
            entries.append((statement, report))
        collected_at = utc_now()
        with self.db.transaction() as conn:
            guard = conn.execute("SELECT status FROM exhibition_plans WHERE plan_id = ?", (plan_id,)).fetchone()
            duplicate = conn.execute(
                "SELECT 1 FROM collected_bookings WHERE plan_id = ? AND basket_id = ?", (plan_id, basket_id)
            ).fetchone()
            if duplicate is not None:
                replayed = True
            else:
                if guard["status"] != "ACTIVE":
                    raise BasketError(
                        PLAN_NOT_ACTIVE,
                        "plan is no longer active; its uncollected bookings have been re-planned",
                        {"plan_id": plan_id},
                    )
                replayed = False
                conn.execute(
                    "INSERT INTO collected_bookings (plan_id, basket_id, collected_at) VALUES (?, ?, ?)",
                    (plan_id, basket_id, collected_at),
                )
                for statement, report in entries:
                    exists = conn.execute(
                        "SELECT 1 FROM exhibited_statements WHERE statement_id = ?", (statement["statement_id"],)
                    ).fetchone()
                    if exists is not None:
                        continue  # already exhibited via another booking; history is kept
                    refunds = {
                        "old": {u["unit_id"]: u["paid_old"] for u in report["units"]},
                        "new": {u["unit_id"]: u["paid_new"] for u in report["units"]},
                    }
                    conn.execute(
                        "INSERT INTO exhibited_statements"
                        " (statement_id, vendor, weight, basket_id, plan_id, booth, old_quote_request, new_quote_request,"
                        "  report_json, refunds_json, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            statement["statement_id"],
                            statement["vendor"],
                            statement["weight"],
                            basket_id,
                            plan_id,
                            booking["booth"],
                            report["old_quote_request"],
                            report["new_quote_request"],
                            canonical_json(report),
                            canonical_json(refunds),
                            collected_at,
                        ),
                    )
        return self._collection_record(plan_id, basket_id, replayed=replayed)

    def _collection_record(self, plan_id: str, basket_id: str, *, replayed: bool) -> Dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT collected_at FROM collected_bookings WHERE plan_id = ? AND basket_id = ?", (plan_id, basket_id)
            ).fetchone()
            exhibited = conn.execute(
                "SELECT statement_id, weight FROM exhibited_statements WHERE plan_id = ? AND basket_id = ? ORDER BY statement_id",
                (plan_id, basket_id),
            ).fetchall()
            plan_row = conn.execute("SELECT plan_json FROM exhibition_plans WHERE plan_id = ?", (plan_id,)).fetchone()
        plan = json_loads(plan_row["plan_json"])
        booking = next(b for b in plan["bookings"] if b["basket_id"] == basket_id)
        return {
            "plan_id": plan_id,
            "basket_id": basket_id,
            "booth": booking["booth"],
            "vendor": booking["vendor"],
            "collected_at": row["collected_at"],
            "exhibited": [{"statement_id": r["statement_id"], "weight": r["weight"]} for r in exhibited],
            "replayed": replayed,
        }

    # -- withdrawal and re-planning ---------------------------------------------
    def withdraw_sample(self, unit_id: str) -> Dict[str, Any]:
        """Withdraw a physical sample and re-plan the uncollected bookings.

        Baskets containing the sample stop being lendable.  The active plan
        is superseded and a fresh plan (same booth count) is computed over
        the still-pending statements; already-collected bookings and
        exhibited statements are untouched.  Withdrawing twice is a replay.
        """

        unit_id = require_id(unit_id, INVALID_INPUT, "unit_id")
        with self.db.transaction() as conn:
            row = conn.execute("SELECT withdrawn FROM samples WHERE unit_id = ?", (unit_id,)).fetchone()
            if row is None:
                raise BasketError(UNKNOWN_SAMPLE, "unknown sample", {"unit_id": unit_id})
            already = bool(row["withdrawn"])
            booths: Optional[int] = None
            superseded_id: Optional[str] = None
            dropped: List[Dict[str, Any]] = []
            if not already:
                conn.execute("UPDATE samples SET withdrawn = 1 WHERE unit_id = ?", (unit_id,))
                active = conn.execute("SELECT plan_id, booths, plan_json FROM exhibition_plans WHERE status = 'ACTIVE'").fetchone()
                if active is not None:
                    superseded_id = active["plan_id"]
                    booths = active["booths"]
                    plan = json_loads(active["plan_json"])
                    collected = {
                        r["basket_id"]
                        for r in conn.execute(
                            "SELECT basket_id FROM collected_bookings WHERE plan_id = ?", (superseded_id,)
                        ).fetchall()
                    }
                    dropped = [b for b in plan["bookings"] if b["basket_id"] not in collected]
                    conn.execute("UPDATE exhibition_plans SET status = 'SUPERSEDED' WHERE plan_id = ?", (superseded_id,))
        if already:
            with self.db.connection() as conn:
                active = conn.execute("SELECT plan_id FROM exhibition_plans WHERE status = 'ACTIVE'").fetchone()
            return {
                "unit_id": unit_id,
                "withdrawn": True,
                "already_withdrawn": True,
                "active_plan_id": active["plan_id"] if active else None,
            }
        replanned = self.plan_exhibition(booths) if booths is not None else None
        return {
            "unit_id": unit_id,
            "withdrawn": True,
            "already_withdrawn": False,
            "superseded_plan_id": superseded_id,
            "dropped_bookings": dropped,
            "replanned_plan": replanned,
        }

    # -- reopening exhibited history ---------------------------------------------
    def reopen_statement(self, statement_id: str) -> Dict[str, Any]:
        """Reopen an exhibited statement with its original evidence.

        Returns the archived record together with the full original old/new
        quotes (immutable in the quote store) and the original per-unit
        return amounts — exactly what was shown at the booth.
        """

        statement_id = require_id(statement_id, INVALID_INPUT, "statement_id")
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM exhibited_statements WHERE statement_id = ?", (statement_id,)).fetchone()
        if row is None:
            self.catalog.get_statement(statement_id)  # raises UNKNOWN_STATEMENT
            raise BasketError(STATEMENT_NOT_EXHIBITED, "statement has not been exhibited yet", {"statement_id": statement_id})
        return {
            "statement_id": statement_id,
            "vendor": row["vendor"],
            "weight": row["weight"],
            "basket_id": row["basket_id"],
            "plan_id": row["plan_id"],
            "booth": row["booth"],
            "old_quote": self.quotes.get_quote(row["old_quote_request"]),
            "new_quote": self.quotes.get_quote(row["new_quote_request"]),
            "report": json_loads(row["report_json"]),
            "refunds": json_loads(row["refunds_json"]),
            "collected_at": row["collected_at"],
        }

    def status(self) -> Dict[str, Any]:
        with self.db.connection() as conn:
            active = conn.execute("SELECT plan_json FROM exhibition_plans WHERE status = 'ACTIVE'").fetchone()
            exhibited = conn.execute(
                "SELECT statement_id, vendor, weight, basket_id, plan_id, booth FROM exhibited_statements ORDER BY statement_id"
            ).fetchall()
            withdrawn = conn.execute("SELECT unit_id FROM samples WHERE withdrawn = 1 ORDER BY unit_id").fetchall()
        return {
            "active_plan": json_loads(active["plan_json"]) if active else None,
            "exhibited_statements": [dict(r) for r in exhibited],
            "pending_statement_ids": [st["statement_id"] for st in self.catalog.pending_statements()],
            "withdrawn_samples": [r["unit_id"] for r in withdrawn],
        }


__all__ = ["ExhibitionPlanner", "optimize_bookings"]
