"""Import every module's models so ``Base.metadata`` is complete (Alembic, tests)."""

from ordexa.modules.ai.models import AiAttachment, AiOrderRequest, ProductAlias  # noqa: F401
from ordexa.modules.audit.models import AuditEvent  # noqa: F401
from ordexa.modules.catalogue.models import Batch, Product  # noqa: F401
from ordexa.modules.channels.models import WhatsappContact, WhatsappMessage, WhatsappNumber  # noqa: F401
from ordexa.modules.customers.models import Customer  # noqa: F401
from ordexa.modules.devices.models import Device  # noqa: F401
from ordexa.modules.finance.models import CreditNote, CreditNoteLine, Expense, LedgerEntry, Payment  # noqa: F401
from ordexa.modules.identity.models import User, UserPermission  # noqa: F401
from ordexa.modules.inventory.models import InventoryTransaction  # noqa: F401
from ordexa.modules.jobs.models import Job  # noqa: F401
from ordexa.modules.purchasing.models import PurchaseInvoice, PurchaseLine, Supplier  # noqa: F401
from ordexa.modules.sales.models import Invoice, InvoiceLine  # noqa: F401
from ordexa.modules.tenants.models import Branch, Tenant, Warehouse  # noqa: F401
from ordexa.shared.db import Base  # noqa: F401
from ordexa.shared.sequences import DocumentSequence  # noqa: F401
