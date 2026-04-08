"""API v1 router package."""
from fastapi import APIRouter

from app.api.v1 import (
    auth,
    orders,
    payments,
    kitchen,
    tables,
    inventory,
    delivery,
    delivery_partners,
    staff,
    subscriptions,
    notifications,
    analytics,
    webhooks,
    health,
    items,
    # ── Payment gateways ──
    razorpay,
    phonepe,
    payu,
    paytm,
    cashfree,
    zivonpay,
    # ── Google Business Profile ──
    google_business,
    # ── KYC / Verification ──
    kyc,
    digilocker,
    # ── AI / Voice ──
    voice,
    menu_scan,
    # ── Menu / Catalog ──
    categories,
    combos,
    item_variants,
    item_addons,
    item_extras,
    item_ingredients,
    modifiers,
    item_stations,
    modifier_groups,
    # ── Customers & CRM ──
    customers,
    customer_addresses,
    feedback,
    favourites,
    # ── Business Operations ──
    coupons,
    offers,
    restaurant_settings,
    cash_transactions,
    due_payments,
    purchase_orders,
    pincodes,
    # ── Billing / Admin ──
    billing,
    audit_logs,
    help_articles,
    misc,
    restaurants,
    restaurant_tables,
    table_events,
    table_sessions,
    kitchen_stations,
    ingredients,
    favourite_items,
    dinein,
    # ── Accounting / Zoho Books ──
    acc_chart_of_accounts,
    acc_contacts,
    acc_contact_persons,
    acc_taxes,
    acc_currencies,
    acc_invoices,
    acc_estimates,
    acc_sales_orders,
    acc_sales_receipts,
    acc_credit_notes,
    acc_customer_payments,
    acc_debit_notes,
    acc_bills,
    acc_purchase_orders as acc_purchase_orders_mod,
    acc_vendor_payments,
    acc_vendor_credits,
    acc_expenses,
    acc_bank_accounts,
    acc_bank_transactions,
    acc_bank_rules,
    acc_journals,
    acc_opening_balances,
    acc_base_currency_adj,
    acc_recurring_invoices,
    acc_recurring_bills,
    acc_recurring_expenses,
    acc_organizations,
    acc_locations,
    acc_reporting_tags,
    acc_fixed_assets,
    acc_projects,
    acc_tasks,
    acc_time_entries,
    acc_items as acc_items_mod,
    acc_users,
    acc_retainer_invoices,
    acc_custom_fields,
    acc_custom_views,
    acc_blueprints,
    acc_webhooks as acc_webhooks_mod,
    acc_workflows,
    # ── Accounting / Advanced Modules ──
    acc_custom_actions,
    acc_custom_buttons,
    acc_custom_functions,
    acc_custom_modules,
    acc_custom_schedulers,
    acc_integrations,
    acc_module_renaming,
    acc_related_lists,
    acc_sandbox,
    acc_web_tabs,
    # ── Accounting / Sync (restaurant ↔ accounting bridge) ──
    acc_sync,
)

router = APIRouter(prefix="/api/v1")

router.include_router(auth.router)
router.include_router(orders.router)
router.include_router(payments.router)
router.include_router(kitchen.router)
router.include_router(tables.router)
router.include_router(inventory.router)
router.include_router(delivery.router)
router.include_router(delivery_partners.router)
router.include_router(staff.router)
router.include_router(subscriptions.router)
router.include_router(notifications.router)
router.include_router(analytics.router)
router.include_router(webhooks.router)
router.include_router(health.router)
router.include_router(items.router)
# ── Payment gateways ──
router.include_router(razorpay.router)
router.include_router(phonepe.router)
router.include_router(payu.router)
router.include_router(paytm.router)
router.include_router(cashfree.router)
router.include_router(zivonpay.router)
# ── Google Business Profile ──
router.include_router(google_business.router)
# ── KYC / Verification ──
router.include_router(kyc.router)
router.include_router(digilocker.router)
# ── AI / Voice ──
router.include_router(voice.router)
router.include_router(menu_scan.router)
# ── Menu / Catalog ──
router.include_router(categories.router)
router.include_router(combos.router)
router.include_router(item_variants.router)
router.include_router(item_addons.router)
router.include_router(item_extras.router)
router.include_router(item_ingredients.router)
router.include_router(modifiers.router)
router.include_router(item_stations.router)
router.include_router(modifier_groups.router)
# ── Customers & CRM ──
router.include_router(customers.router)
router.include_router(customer_addresses.router)
router.include_router(feedback.router)
router.include_router(favourites.router)
# ── Business Operations ──
router.include_router(coupons.router)
router.include_router(offers.router)
router.include_router(restaurant_settings.router)
router.include_router(cash_transactions.router)
router.include_router(due_payments.router)
router.include_router(purchase_orders.router)
router.include_router(pincodes.router)
# ── Billing / Admin ──
router.include_router(billing.router)
router.include_router(audit_logs.router)
router.include_router(help_articles.router)
router.include_router(misc.router)
router.include_router(restaurants.router)
router.include_router(restaurant_tables.router)
router.include_router(table_events.router)
router.include_router(table_sessions.router)
router.include_router(kitchen_stations.router)
router.include_router(ingredients.router)
router.include_router(favourite_items.router)
# ── Dine-In Sessions (QR v2) ──
router.include_router(dinein.router)
# ── Accounting / Zoho Books ──
router.include_router(acc_chart_of_accounts.router)
router.include_router(acc_contacts.router)
router.include_router(acc_contact_persons.router)
router.include_router(acc_taxes.router)
router.include_router(acc_currencies.router)
router.include_router(acc_invoices.router)
router.include_router(acc_estimates.router)
router.include_router(acc_sales_orders.router)
router.include_router(acc_sales_receipts.router)
router.include_router(acc_credit_notes.router)
router.include_router(acc_customer_payments.router)
router.include_router(acc_debit_notes.router)
router.include_router(acc_bills.router)
router.include_router(acc_purchase_orders_mod.router)
router.include_router(acc_vendor_payments.router)
router.include_router(acc_vendor_credits.router)
router.include_router(acc_expenses.router)
router.include_router(acc_bank_accounts.router)
router.include_router(acc_bank_transactions.router)
router.include_router(acc_bank_rules.router)
router.include_router(acc_journals.router)
router.include_router(acc_opening_balances.router)
router.include_router(acc_base_currency_adj.router)
router.include_router(acc_recurring_invoices.router)
router.include_router(acc_recurring_bills.router)
router.include_router(acc_recurring_expenses.router)
router.include_router(acc_organizations.router)
router.include_router(acc_locations.router)
router.include_router(acc_reporting_tags.router)
router.include_router(acc_fixed_assets.router)
router.include_router(acc_projects.router)
router.include_router(acc_tasks.router)
router.include_router(acc_time_entries.router)
router.include_router(acc_items_mod.router)
router.include_router(acc_users.router)
router.include_router(acc_retainer_invoices.router)
router.include_router(acc_custom_fields.router)
router.include_router(acc_custom_views.router)
router.include_router(acc_blueprints.router)
router.include_router(acc_webhooks_mod.router)
router.include_router(acc_workflows.router)
# ── Accounting / Advanced Modules ──
router.include_router(acc_custom_actions.router)
router.include_router(acc_custom_buttons.router)
router.include_router(acc_custom_functions.router)
router.include_router(acc_custom_modules.router)
router.include_router(acc_custom_schedulers.router)
router.include_router(acc_integrations.router)
router.include_router(acc_module_renaming.router)
router.include_router(acc_related_lists.router)
router.include_router(acc_sandbox.router)
router.include_router(acc_web_tabs.router)
# ── Accounting / Sync (restaurant ↔ accounting bridge) ──
router.include_router(acc_sync.router)
