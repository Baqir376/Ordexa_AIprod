"""Permission catalogue (SRS 7.2). Checks are always made against permissions, never role names."""

from __future__ import annotations

PERMISSIONS: dict[str, list[str]] = {
    "administration": ["user.manage", "permission.assign", "settings.configure", "integration.configure"],
    "customers": ["customer.view", "customer.create", "customer.update", "customer.credit_limit.change"],
    "products": ["product.view", "product.manage", "price.view", "price.override"],
    "sales": [
        "sales_order.view",
        "sales_order.create",
        "sales_order.approve",
        "sales_order.cancel",
        "invoice.create",
        "ai_alias.approve",
    ],
    "inventory": ["inventory.view", "inventory.receive", "inventory.transfer", "inventory.adjust", "dispatch.process"],
    "procurement": [
        "supplier.manage",
        "purchase_order.view",
        "purchase_order.create",
        "purchase_order.approve",
        "goods_receipt.create",
    ],
    "finance": ["finance.view", "invoice.view", "invoice.post", "payment.record", "expense.record", "finance.write_off"],
    "reports": ["reports.view", "audit.view"],
}

ALL_PERMISSIONS: frozenset[str] = frozenset(p for perms in PERMISSIONS.values() for p in perms)

# Administrator-only in the MVP: never grantable to Staff.
ADMIN_ONLY: frozenset[str] = frozenset(PERMISSIONS["administration"])

GRANTABLE: frozenset[str] = ALL_PERMISSIONS - ADMIN_ONLY

# Convenience presets (not roles) offered in the admin screen.
PRESETS: dict[str, list[str]] = {
    "counter_billing": [
        "customer.view",
        "product.view",
        "price.view",
        "inventory.view",
        "invoice.create",
        "invoice.view",
        "payment.record",
    ],
    "sales": ["customer.view", "customer.create", "sales_order.create", "sales_order.view", "product.view", "inventory.view"],
    "warehouse": ["inventory.view", "inventory.receive", "inventory.transfer", "dispatch.process", "product.view"],
    "finance": ["invoice.view", "invoice.post", "payment.record", "finance.view", "reports.view"],
}
