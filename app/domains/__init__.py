"""Domain-driven business logic packages.

Each subpackage is a bounded context. The HTTP layer (app/api/*) is a
thin adapter that calls into these packages.

Phase-0 skeleton — handlers and services move in here in later phases.
See docs/ARCHITECTURE_V2.md §2 (Folder Layout) and §21 (Migration Phases).

Domains:
    identity         auth, users, roles, sessions, JWT
    tenancy          merchants, branches, memberships
    catalog          menu, items, categories, modifiers, combos
    ordering         orders, dine-in sessions, kitchen tickets
    inventory        stock, ingredients, snapshots, deductions
    customers        crm, addresses, favourites, loyalty
    payments         gateway adapters, payment intents, captures
    financial        ledger, journal, accounting, recon, settlement,
                     payout, refunds, disputes, fees, events
    erp              purchase orders, expenses, invoices
    compliance       kyc, risk, audit
    notifications    email/SMS/push fanout
    shared           cross-domain value objects, exceptions
"""
