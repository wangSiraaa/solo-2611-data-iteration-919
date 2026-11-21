"""Independent basket pricing and after-sales ledger service.

Quick start::

    from basket_service import open_service

    svc = open_service("data/shop.db")
    quote = svc.quote(basket, coupons, request_id="q1", snapshot_id=snap)
    sale = svc.register_sale("S1", quote=quote)
    result = svc.return_units("r1", "S1", ["b"], {"b": "RESTOCK"})
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .catalog import ComparisonCatalog
from .db import Database
from .errors import BasketError
from .exhibit import ExhibitionPlanner
from .ledger import AfterSalesLedger
from .quote_service import QuoteService


class Service:
    """Thin façade wiring the quote service and ledger onto one database."""

    def __init__(self, path: str = ":memory:", *, busy_timeout_ms: int = 30000):
        self.db = Database(path, busy_timeout_ms=busy_timeout_ms)
        self.quotes = QuoteService(self.db)
        self.ledger = AfterSalesLedger(self.db)
        self.catalog = ComparisonCatalog(self.db, self.quotes)
        self.exhibit = ExhibitionPlanner(self.db, self.quotes, self.catalog)

    # quote surface -------------------------------------------------------
    def publish_snapshot(self, products=None, coupons=None, *, snapshot_id=None) -> str:
        return self.quotes.publish_snapshot(products, coupons, snapshot_id=snapshot_id)

    def get_snapshot(self, snapshot_id: str):
        return self.quotes.get_snapshot(snapshot_id)

    def quote(self, basket, coupons, request_id: Optional[str] = None, snapshot_id: Optional[str] = None):
        return self.quotes.quote(basket, coupons, request_id, snapshot_id)

    def get_quote(self, request_id: str):
        return self.quotes.get_quote(request_id)

    # ledger surface ------------------------------------------------------
    def register_sale(self, sale_id: str, **kwargs):
        return self.ledger.register_sale(sale_id, **kwargs)

    def get_sale(self, sale_id: str):
        return self.ledger.get_sale(sale_id)

    def return_units(self, request_id: str, sale_id: str, unit_ids, grade):
        return self.ledger.return_units(request_id, sale_id, unit_ids, grade)

    def get_return(self, request_id: str):
        return self.ledger.get_return(request_id)

    def adjust_inventory(self, event_id: str, sku: str, quantity: int, bucket: str):
        return self.ledger.adjust_inventory(event_id, sku, quantity, bucket)

    def inventory(self, skus=None):
        return self.ledger.inventory(skus)

    def audit_inventory(self):
        return self.ledger.audit_inventory()

    def audit_sales(self):
        return self.ledger.audit_sales()

    # comparison catalog surface (对照目录) ---------------------------------
    def register_basket(self, basket_id: str, items):
        return self.catalog.register_basket(basket_id, items)

    def get_basket(self, basket_id: str):
        return self.catalog.get_basket(basket_id)

    def list_baskets(self):
        return self.catalog.list_baskets()

    def catalog_samples(self):
        return self.catalog.samples()

    def register_statement(self, statement_id: str, **kwargs):
        return self.catalog.register_statement(statement_id, **kwargs)

    def get_statement(self, statement_id: str):
        return self.catalog.get_statement(statement_id)

    def list_statements(self):
        return self.catalog.list_statements()

    def compare_basket(self, basket_id: str, old_snapshot: str, new_snapshot: str):
        return self.catalog.compare_basket(basket_id, old_snapshot, new_snapshot)

    def find_counterexamples(self, statement_id: str):
        return self.catalog.find_counterexamples(statement_id)

    # exhibition planning surface (布展借单) --------------------------------
    def plan_exhibition(self, booths: int):
        return self.exhibit.plan_exhibition(booths)

    def get_plan(self, plan_id: str):
        return self.exhibit.get_plan(plan_id)

    def collect_booking(self, plan_id: str, basket_id: str):
        return self.exhibit.collect_booking(plan_id, basket_id)

    def withdraw_sample(self, unit_id: str):
        return self.exhibit.withdraw_sample(unit_id)

    def reopen_statement(self, statement_id: str):
        return self.exhibit.reopen_statement(statement_id)

    def exhibition_status(self):
        return self.exhibit.status()

    # cross-cutting -------------------------------------------------------
    def run(self, plan):
        """Execute a JSON plan; see ``basket_service.planner``."""

        from .planner import run_plan

        return run_plan(self, plan)


def open_service(path: str = ":memory:", *, busy_timeout_ms: int = 30000) -> Service:
    return Service(path, busy_timeout_ms=busy_timeout_ms)


__all__ = ["Service", "open_service", "BasketError", "Database"]
