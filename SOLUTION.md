# Basket quote service and after-sales ledger

A standalone, Python 3.11 solution for the brief in `README.md`:

* an **optimal basket quote service** with immutable published snapshots,
  deterministic two-level tie-breaking and proportional per-unit discount
  allocation;
* an **after-sales ledger** that binds each sale to a complete published quote
  and refunds exactly the per-unit amount that sale paid, with idempotent
  requests, all-or-nothing overlap rejection, and transactional stock moves;
* a **comparison catalog** (对照目录) for promotion-term changes: registered
  physical samples and display baskets, vendor change statements pinned to
  frozen old/new snapshots, per-unit return-liability comparison reports and
  replayable counterexample search over the real baskets only;
* an **exhibition planner** (布展借单): exact max-weight booth/basket lending
  orders under per-sample exclusivity, collection archiving, withdrawal
  re-planning of uncollected bookings, and reopening of exhibited statements
  with their original quotes and return amounts.

No external dependencies are required — only the standard library and SQLite.

## Quick start

```bash
# Full demo: rebuilds data/demo.db and writes results/demo_plan.json +
# results/demo_results.json, including independent cross-checks.
python3 -m basket_service demo

# Unit / differential / concurrency tests (40 tests).
python3 -m basket_service self-check
# or: python3 -m unittest discover -s tests -v

# Run a JSON plan against a real database.
python3 -m basket_service run-plan results/demo_plan.json --db data/plan.db
```

Callable entry point:

```python
from basket_service import open_service

svc = open_service("data/shop.db")

snap = svc.publish_snapshot(products=[...], coupons=[...])   # immutable snapshot
quote = svc.quote(basket, coupons=[], request_id="q1", snapshot_id=snap)
sale = svc.register_sale("S1", quote=quote)                   # binds the full quote
result = svc.return_units("r1", "S1", ["b"], {"b": "RESTOCK"})
svc.adjust_inventory("adj1", "B", 3, "RESTOCK")               # independent stock event

# comparison catalog / exhibition planning
svc.register_basket("L", [{"unit_id": "a", "sku": "A", "price": 10}, ...])
svc.register_statement("S3", vendor="乙", weight=8, old_snapshot=old, new_snapshot=new,
                       allowed_baskets=["L"],
                       assertions=[{"kind": "UNIT_PAID_UNCHANGED", "unit_id": "a"}])
svc.find_counterexamples("S3")          # allowed real baskets with replayable evidence
plan = svc.plan_exhibition(booths=2)    # max-weight lending order
svc.collect_booking(plan["plan_id"], "L")
svc.withdraw_sample("a")                # re-plans the uncollected bookings
svc.reopen_statement("S3")              # original quotes + original return amounts
```

`register_sale` also accepts `request_id=`/`quote_id=` when the ledger shares
the quote database; or pass `quote=` with any complete quote payload (for
example one issued by an independent quote service) — the ledger validates the
payload structurally and **never re-runs the optimizer** for sales or refunds.

## Contract summary

### Quote

* A basket is a list of physical units with unique `unit_id`, a `sku` and a
  positive integer `price` (booleans rejected). Input order never affects the
  result.
* A coupon has a stable `coupon_id`, a positive integer `D`, and
  `requires: sku -> count`. One use consumes exactly those units, which must
  have total original price at least `D`; coupons share no physical units; each
  coupon is used at most once; untouched units stay at full price.
* The total is the minimum possible `sum(price) - sum(D)`.
* Tie-break: smaller sorted tuple of used `coupon_id`s first; if equal, compare
  the per-coupon sorted `unit_id` lists in coupon-id order.
* Each coupon's `D` is split over its units proportionally to original price:
  floor of the exact share, then one cent each to the largest fractional
  remainders, ties by `unit_id` ascending. Per-unit discount never exceeds the
  unit price and the paid amounts sum to the total.
* The service stores quotes by `request_id`: same request + canonical basket +
  snapshot replays the identical full quote; different content is
  `REQUEST_CONFLICT`. New snapshots never change old quotes.

### Ledger

* A `sale_id` binds one complete quote; the same body replays, different content
  is `SALE_CONFLICT`. Per-unit original paid amounts are persisted.
* `return(request_id, sale_id, unit_ids, grade)` refunds each unit's stored
  original paid amount, regardless of current prices/promotions. `grade` is
  `RESTOCK` or `SCRAP` per unit and drives the stock bucket.
* A unit returns at most once. A request is all-or-nothing: any already-returned
  or ineligible unit rejects the whole request, processing nothing.
* Same `request_id` + same body replays the first result (even after other
  units were returned); same id with a different body is `REQUEST_CONFLICT`.
* Refund and inventory receipt/scrap are written in one `BEGIN IMMEDIATE`
  transaction, so a failure or concurrent request never leaves a refund
  without its stock move or double-counts stock.
* `adjust_inventory(event_id, sku, quantity, bucket)` records independent
  restock/scrap events (idempotent by event id); it never changes any sale's
  refundable amounts and never resurrects returned units.
* Sales do not imply stock-out; after-sales stock starts at zero and is
  reconciled only from its own events.

### Comparison catalog (对照目录)

* The store registers a finite set of real display baskets; every unit is a
  physical sample with a stable `unit_id`, a SKU and an original price.  The
  sample registry pins `unit_id -> (sku, price)`: a second basket mentioning
  the same sample with different attributes is `SAMPLE_CONFLICT`, so a
  generated cheaper unit can never replace a specified sample.  Baskets and
  statements are idempotent by content (`BASKET_CONFLICT` /
  `STATEMENT_CONFLICT` on id reuse with different content).
* A change statement names the vendor, a non-negative integer weight, the
  frozen `old_snapshot`/`new_snapshot`, the allowed demo baskets (which must
  be registered) and a non-empty list of financial assertions.  Assertion
  kinds: `UNIT_PAID_UNCHANGED`, `UNIT_PAID_NOT_ABOVE`, `UNIT_PAID_DECREASES`,
  `UNIT_DISCOUNT_NOT_ABOVE` (all per `unit_id`), `TOTAL_UNCHANGED`,
  `TOTAL_NOT_ABOVE`, and `COUPON_SAME_UNITS` (per `coupon_id`).
* `compare_basket` quotes the *registered* basket under both snapshots and
  reports, beyond the totals, every unit's paid/discount/coupon in both
  versions (the return liability) and every coupon's bound physical units.
  Findings flag `SAME_TOTAL_DIFFERENT_ALLOCATION` (同总价不同折扣归属) and
  `COUPON_RETARGETED` (同券号换了实物) explicitly.
* `find_counterexamples` evaluates a statement on exactly its allowed real
  baskets — never on generated items.  A basket is a counterexample iff at
  least one assertion is `VIOLATED` (`NOT_APPLICABLE` when the asserted
  unit/coupon is absent does not count).  Evidence quotes are stored under
  deterministic request ids (`cmp:<basket>:<old>:<new>:old|new`) and replay
  byte-for-byte.  A basket that cannot be quoted (e.g. its SKU is absent
  from the statement's snapshot catalogue) is recorded with its error, never
  silently counted as a counterexample.

### Exhibition planning (布展借单)

* Each physical sample is lent to at most one booth per day; a booth hosts
  one basket booked by one vendor, who may prove any number of statements
  with it.  Other vendors cannot share lent samples, so booked baskets are
  pairwise sample-disjoint and a basket is booked at most once.
* A statement is proved when some booking pairs its vendor with an allowed
  basket that is a replayable counterexample for it.  Weights are counted
  once per statement, however many bookings could prove it.
* `plan_exhibition(booths)` computes an exact max-weight lending order
  (branch-and-bound over baskets with a suffix-OR bound; deterministic
  tie-break: fewer bookings, then the lexicographically smallest booking
  list).  Each booking names the booth, the real basket, and per proved
  statement the old/new quote request pair that supports it.
* `collect_booking` (签领) archives the booking's statements as exhibited
  with the full comparison report and the per-unit return amounts of both
  versions; collecting twice replays.  `withdraw_sample` supersedes the
  active plan and re-plans only the not-yet-collected bookings over the
  remaining lendable baskets — exhibited history is never rewritten, and
  `reopen_statement` returns it with the original old/new quotes and the
  original return amounts, even after a restart.

### Error codes

`INVALID_BASKET`, `INVALID_COUPON`, `INVALID_INPUT`, `INVALID_GRADE`,
`INVALID_QUOTE`, `UNKNOWN_SNAPSHOT`, `SNAPSHOT_CONFLICT`, `REQUEST_CONFLICT`,
`QUOTE_CONFLICT`, `UNKNOWN_QUOTE`, `UNKNOWN_SALE`, `SALE_CONFLICT`,
`QUOTE_MISMATCH`, `UNIT_INELIGIBLE`, `UNIT_ALREADY_RETURNED`,
`OVERLAPPING_RETURN`, `INVENTORY_CONFLICT`, `UNKNOWN_BASKET`,
`BASKET_CONFLICT`, `UNKNOWN_STATEMENT`, `STATEMENT_CONFLICT`,
`INVALID_ASSERTION`, `SAMPLE_CONFLICT`, `UNKNOWN_SAMPLE`, `UNKNOWN_PLAN`,
`PLAN_NOT_ACTIVE`, `UNKNOWN_BOOKING`, `STATEMENT_NOT_EXHIBITED`.

Every public operation either commits a complete result or raises before any
partial state is written.

## Design trade-offs

* **Exact optimization, no greedy.** A bitmask DP over ≤10 units computes the
  maximum discount; an ignored-coupon subset scan then finds the
  lexicographically smallest coupon-id set reaching it; a feasibility-guided
  prefix greedy fixes the lexicographically smallest unit-id lists. This is
  fast (milliseconds at the specified limits) and deterministic. Greedy
  "largest D first" is demonstrably wrong (see the `greedy_trap` scenario).
* **Snapshot-bound quotes.** Quotes carry the snapshot id, the canonical
  basket, the coupon catalogue, assignments, the full allocation and the total.
  Passing a different ad-hoc coupon list under an existing snapshot is a
  conflict; publishing a new snapshot never mutates an old quote.
* **Ledger uses stored facts.** Refund eligibility and amounts come from the
  sale's stored unit rows (protected by primary keys and conditional updates
  inside a SQLite write transaction). Nothing about historical returns,
  condition or stock counts is inferred from a quote.
* **Concurrency.** Writers use `BEGIN IMMEDIATE` with a busy timeout; the
  unit-level guard is a conditional `UPDATE ... WHERE returned = 0` re-checked
  in-transaction, so two connections that both read "returnable" still produce
  exactly one success. This is database locking, not an in-process set.
* **Reopen.** Snapshots, quotes, sales, return requests/items, adjustments,
  inventory events and totals all live in SQLite and survive process restart.
* **Catalog integrity.** Counterexample search has no generative path: the
  search space is exactly the statement's allowed list of registered baskets,
  quoted from the registered items, and the sample registry pins each
  physical sample's SKU and price across baskets (`SAMPLE_CONFLICT`).
* **Exact exhibition search, no scoring heuristic.** The lending plan is
  optimized by branch-and-bound over baskets with a suffix-OR upper bound
  and a redundant-tail collapse; ties resolve deterministically (fewer
  bookings, then lexicographic order).  At the stated limits (<= 20 baskets,
  <= 16 statements) it returns the provably maximum weight instantly, and
  the unit suite fuzzes it against exhaustive enumeration.

## Layout

```
basket_service/
  __init__.py      Service facade / open_service
  errors.py        typed error codes
  validation.py    strict input + quote payload validation, allocation
  optimizer.py     exact bitmask optimizer + deterministic tie-break
  reference.py     independent brute-force / Fraction reference (tests only)
  db.py            SQLite schema and transaction helpers
  quote_service.py snapshots and quote API
  ledger.py        sales, returns, inventory, audits
  catalog.py       samples, display baskets, statements, paired-quote reports
  exhibit.py       max-weight lending plans, collection, withdrawal, reopen
  planner.py       JSON plan runner
  demo.py          demo driver + CLI (demo / run-plan / self-check)
tests/             optimizer, quote-service, ledger, reference, catalog,
                   exhibition tests
data/              real SQLite databases (created by the demo)
results/           JSON plans and result records
```

## Verification

`python3 -m basket_service demo` runs 29 checks, all computed against
independent references:

* case-material quotes vs. exhaustive enumeration;
* two sales under different promotions, refunds following each sale's own
  bound quote (including after a promotion switch);
* greedy-largest-D counterexample and contention/tie-break/remainder examples;
* a 10-unit basket optimized by full enumeration and audited from raw ledger
  rows (per-unit paid sums, refund sums, remaining refundable, stock totals);
* lost-response retry replay, whole-request overlap rejection, independent
  stock adjustments, and a two-thread concurrent return race with no double
  stock;
* reopening the database preserves everything;
* the 甲/乙/丙 worked example built from real quotable items and coupons:
  乙's basket L keeps the same total (13) while unit a's paid amount moves
  3 -> 5, so the per-unit report flags `SAME_TOTAL_DIFFERENT_ALLOCATION`
  and a total-only assertion finds no counterexample; two booths prove
  weight 13 (乙+丙), one booth proves 10 (甲); after collecting 乙's booking
  and withdrawing sample a, only the uncollected bookings are re-planned,
  and the exhibited statement reopens with its original quotes and return
  amounts (the ledger refunds exactly the archived 3);
* a coupon id moving to a different physical unit (`COUPON_RETARGETED`);
* a 20-basket, 16-statement exhibition with fully separate samples but
  heavily overlapping proven-statement sets, cross-checked against an
  independent brute-force enumeration of every booking set (optimum 51).

`python3 -m basket_service self-check` runs the 71-test unit suite, which
additionally fuzzes the booking optimizer against exhaustive enumeration on
random instances.
