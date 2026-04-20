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
    # ── ERP ──
    accounting,
    settlements,
    ai_ingredients,
    invoice_import,
    erp,
    # ── Food Images ──
    food_images,
    # ── Waitlist ──
    waitlist,
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
# ── Onboard alias (Flutter sends redirect_to /onboard/callback) ──
router.add_api_route(
    "/onboard/callback",
    digilocker.digilocker_callback,
    methods=["GET"],
    tags=["DigiLocker KYC"],
)
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
# ── ERP ──
router.include_router(accounting.router)
router.include_router(accounting.accounts_router)
router.include_router(accounting.reports_router)
router.include_router(settlements.router)
router.include_router(settlements.rules_router)
router.include_router(ai_ingredients.router)
router.include_router(invoice_import.router)
router.include_router(erp.router)
# ── Invoices / Expenses / Sub-Ledger / Tax ──
from app.api.v1 import invoices, expenses, subledger_tax
router.include_router(invoices.router)
router.include_router(expenses.router)
router.include_router(subledger_tax.subledger_router)
router.include_router(subledger_tax.tax_router)
# ── Food Images ──
router.include_router(food_images.router)
# ── Waitlist ──
router.include_router(waitlist.router)
