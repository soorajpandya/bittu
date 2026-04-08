-- Migration 005: Zoho Books-style Accounting System
-- Creates all tables for the full accounting module

BEGIN;

-- ══════════════════════════════════════════════════════════════
-- ORGANIZATIONS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_organizations (
    organization_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    is_default_org BOOLEAN DEFAULT false,
    account_created_date DATE,
    time_zone VARCHAR(100),
    language_code VARCHAR(10) DEFAULT 'en',
    date_format VARCHAR(30) DEFAULT 'dd/MM/yyyy',
    field_separator VARCHAR(5) DEFAULT '/',
    fiscal_year_start_month INT DEFAULT 1,
    currency_code VARCHAR(10) NOT NULL DEFAULT 'INR',
    currency_symbol VARCHAR(10) DEFAULT '₹',
    price_precision INT DEFAULT 2,
    industry_type VARCHAR(100),
    industry_size VARCHAR(50),
    portal_name VARCHAR(100),
    company_id_label VARCHAR(100),
    company_id_value VARCHAR(100),
    tax_id_label VARCHAR(100),
    tax_id_value VARCHAR(100),
    address JSONB DEFAULT '{}',
    phone VARCHAR(30),
    fax VARCHAR(30),
    website VARCHAR(255),
    email VARCHAR(255),
    is_org_active BOOLEAN DEFAULT true,
    user_id UUID NOT NULL,
    branch_id UUID,
    restaurant_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ══════════════════════════════════════════════════════════════
-- CURRENCIES
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_currencies (
    currency_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    currency_code VARCHAR(10) NOT NULL,
    currency_name VARCHAR(100),
    currency_symbol VARCHAR(10),
    price_precision INT DEFAULT 2,
    currency_format VARCHAR(50),
    is_base_currency BOOLEAN DEFAULT false,
    exchange_rate NUMERIC(20,6) DEFAULT 1.0,
    effective_date DATE,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(currency_code, user_id)
);

-- ══════════════════════════════════════════════════════════════
-- CHART OF ACCOUNTS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_chart_of_accounts (
    account_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_name VARCHAR(200) NOT NULL,
    account_code VARCHAR(50),
    account_type VARCHAR(50) NOT NULL, -- income, expense, equity, asset, liability
    currency_id UUID REFERENCES acc_currencies(currency_id),
    description TEXT,
    is_active BOOLEAN DEFAULT true,
    is_user_created BOOLEAN DEFAULT true,
    is_system_account BOOLEAN DEFAULT false,
    is_standalone_account BOOLEAN DEFAULT false,
    show_on_dashboard BOOLEAN DEFAULT false,
    include_in_vat_return BOOLEAN DEFAULT false,
    parent_account_id UUID REFERENCES acc_chart_of_accounts(account_id),
    depth INT DEFAULT 0,
    current_balance NUMERIC(20,2) DEFAULT 0,
    custom_fields JSONB DEFAULT '[]',
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_coa_user ON acc_chart_of_accounts(user_id);
CREATE INDEX IF NOT EXISTS idx_acc_coa_type ON acc_chart_of_accounts(account_type);

-- ══════════════════════════════════════════════════════════════
-- TAXES
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_taxes (
    tax_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tax_name VARCHAR(100) NOT NULL,
    tax_percentage NUMERIC(10,4) DEFAULT 0,
    tax_type VARCHAR(50), -- tax, compound_tax
    tax_factor VARCHAR(20), -- inclusive, exclusive
    tax_specific_type VARCHAR(50),
    tax_authority_name VARCHAR(200),
    tax_authority_id UUID,
    country_code VARCHAR(10),
    is_editable BOOLEAN DEFAULT true,
    is_value_added BOOLEAN DEFAULT false,
    purchase_tax_expense_account_id UUID REFERENCES acc_chart_of_accounts(account_id),
    status VARCHAR(20) DEFAULT 'active',
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS acc_tax_groups (
    tax_group_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tax_group_name VARCHAR(100) NOT NULL,
    tax_group_percentage NUMERIC(10,4) DEFAULT 0,
    taxes JSONB DEFAULT '[]', -- array of {tax_id, tax_name, tax_percentage}
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ══════════════════════════════════════════════════════════════
-- CONTACTS (Vendors + Customers for accounting)
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_contacts (
    contact_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_name VARCHAR(200) NOT NULL,
    company_name VARCHAR(200),
    contact_type VARCHAR(20) DEFAULT 'customer', -- customer, vendor
    customer_sub_type VARCHAR(50),
    status VARCHAR(20) DEFAULT 'active',
    website VARCHAR(255),
    language_code VARCHAR(10) DEFAULT 'en',
    credit_limit NUMERIC(20,2),
    contact_number VARCHAR(50),
    currency_id UUID REFERENCES acc_currencies(currency_id),
    payment_terms INT,
    payment_terms_label VARCHAR(100),
    notes TEXT,
    billing_address JSONB DEFAULT '{}',
    shipping_address JSONB DEFAULT '{}',
    default_templates JSONB DEFAULT '{}',
    custom_fields JSONB DEFAULT '[]',
    tags TEXT[],
    is_portal_enabled BOOLEAN DEFAULT false,
    vat_reg_no VARCHAR(50),
    tax_reg_no VARCHAR(50),
    tax_exemption_certificate_number VARCHAR(100),
    country_code VARCHAR(10),
    vat_treatment VARCHAR(50),
    tax_treatment VARCHAR(50),
    tax_regime VARCHAR(50),
    legal_name VARCHAR(200),
    is_tds_registered BOOLEAN DEFAULT false,
    place_of_contact VARCHAR(100),
    gst_no VARCHAR(50),
    outstanding_receivable_amount NUMERIC(20,2) DEFAULT 0,
    outstanding_payable_amount NUMERIC(20,2) DEFAULT 0,
    unused_credits_receivable_amount NUMERIC(20,2) DEFAULT 0,
    unused_credits_payable_amount NUMERIC(20,2) DEFAULT 0,
    first_name VARCHAR(100),
    last_name VARCHAR(100),
    email VARCHAR(255),
    phone VARCHAR(30),
    mobile VARCHAR(30),
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_contacts_user ON acc_contacts(user_id);
CREATE INDEX IF NOT EXISTS idx_acc_contacts_type ON acc_contacts(contact_type);

-- ══════════════════════════════════════════════════════════════
-- CONTACT PERSONS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_contact_persons (
    contact_person_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id UUID NOT NULL REFERENCES acc_contacts(contact_id) ON DELETE CASCADE,
    salutation VARCHAR(10),
    first_name VARCHAR(100),
    last_name VARCHAR(100),
    email VARCHAR(255),
    phone VARCHAR(30),
    mobile VARCHAR(30),
    designation VARCHAR(100),
    department VARCHAR(100),
    is_primary_contact BOOLEAN DEFAULT false,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ══════════════════════════════════════════════════════════════
-- ACCOUNTING ITEMS (products/services for invoicing)
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_items (
    item_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    description TEXT,
    rate NUMERIC(20,2) NOT NULL DEFAULT 0,
    sku VARCHAR(100),
    product_type VARCHAR(50) DEFAULT 'goods', -- goods, service
    item_type VARCHAR(50) DEFAULT 'sales', -- sales, purchases, sales_and_purchases, inventory
    unit VARCHAR(50),
    hsn_or_sac VARCHAR(50),
    sat_item_key_code VARCHAR(100),
    unitkey_code VARCHAR(100),
    is_taxable BOOLEAN DEFAULT true,
    tax_id UUID REFERENCES acc_taxes(tax_id),
    tax_percentage NUMERIC(10,4),
    tax_exemption_id UUID,
    purchase_tax_exemption_id UUID,
    account_id UUID REFERENCES acc_chart_of_accounts(account_id),
    purchase_description TEXT,
    purchase_rate NUMERIC(20,2),
    purchase_account_id UUID REFERENCES acc_chart_of_accounts(account_id),
    inventory_account_id UUID REFERENCES acc_chart_of_accounts(account_id),
    vendor_id UUID REFERENCES acc_contacts(contact_id),
    reorder_level INT,
    stock_on_hand NUMERIC(20,4) DEFAULT 0,
    initial_stock NUMERIC(20,4) DEFAULT 0,
    initial_stock_rate NUMERIC(20,2) DEFAULT 0,
    item_tax_preferences JSONB DEFAULT '{}',
    custom_fields JSONB DEFAULT '[]',
    status VARCHAR(20) DEFAULT 'active',
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_items_user ON acc_items(user_id);

-- ══════════════════════════════════════════════════════════════
-- INVOICES
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_invoices (
    invoice_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_number VARCHAR(100),
    customer_id UUID NOT NULL REFERENCES acc_contacts(contact_id),
    contact_persons JSONB DEFAULT '[]',
    currency_id UUID REFERENCES acc_currencies(currency_id),
    status VARCHAR(30) DEFAULT 'draft', -- draft, sent, overdue, paid, void, partially_paid
    reference_number VARCHAR(100),
    template_id UUID,
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    due_date DATE,
    payment_terms INT,
    payment_terms_label VARCHAR(100),
    discount NUMERIC(20,2) DEFAULT 0,
    is_discount_before_tax BOOLEAN DEFAULT true,
    discount_type VARCHAR(20) DEFAULT 'entity_level', -- entity_level, item_level
    is_inclusive_tax BOOLEAN DEFAULT false,
    exchange_rate NUMERIC(20,6) DEFAULT 1.0,
    location_id UUID,
    recurring_invoice_id UUID,
    invoiced_estimate_id UUID,
    salesperson_name VARCHAR(200),
    salesperson_id UUID,
    custom_fields JSONB DEFAULT '[]',
    tags TEXT[],
    notes TEXT,
    terms TEXT,
    sub_total NUMERIC(20,2) DEFAULT 0,
    tax_total NUMERIC(20,2) DEFAULT 0,
    total NUMERIC(20,2) DEFAULT 0,
    balance NUMERIC(20,2) DEFAULT 0,
    payment_made NUMERIC(20,2) DEFAULT 0,
    credits_applied NUMERIC(20,2) DEFAULT 0,
    write_off_amount NUMERIC(20,2) DEFAULT 0,
    place_of_supply VARCHAR(100),
    vat_treatment VARCHAR(50),
    tax_treatment VARCHAR(50),
    gst_treatment VARCHAR(50),
    gst_no VARCHAR(50),
    is_reverse_charge_applied BOOLEAN DEFAULT false,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_invoices_user ON acc_invoices(user_id);
CREATE INDEX IF NOT EXISTS idx_acc_invoices_customer ON acc_invoices(customer_id);
CREATE INDEX IF NOT EXISTS idx_acc_invoices_status ON acc_invoices(status);
CREATE INDEX IF NOT EXISTS idx_acc_invoices_date ON acc_invoices(date);

-- ══════════════════════════════════════════════════════════════
-- INVOICE LINE ITEMS (shared for invoices, bills, estimates, etc.)
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_line_items (
    line_item_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- polymorphic parent
    parent_id UUID NOT NULL, -- invoice_id, bill_id, estimate_id, etc.
    parent_type VARCHAR(30) NOT NULL, -- invoice, bill, estimate, credit_note, sales_order, purchase_order, sales_receipt, vendor_credit, journal, debit_note
    item_id UUID REFERENCES acc_items(item_id),
    project_id UUID,
    name VARCHAR(200),
    description TEXT,
    rate NUMERIC(20,4) DEFAULT 0,
    quantity NUMERIC(20,4) DEFAULT 1,
    unit VARCHAR(50),
    discount_amount NUMERIC(20,2) DEFAULT 0,
    discount NUMERIC(20,2) DEFAULT 0,
    discount_type VARCHAR(20),
    tax_id UUID REFERENCES acc_taxes(tax_id),
    tax_name VARCHAR(100),
    tax_type VARCHAR(50),
    tax_percentage NUMERIC(10,4) DEFAULT 0,
    tds_tax_id UUID,
    product_type VARCHAR(50),
    hsn_or_sac VARCHAR(50),
    sat_item_key_code VARCHAR(100),
    unitkey_code VARCHAR(100),
    item_total NUMERIC(20,2) DEFAULT 0,
    tax_total NUMERIC(20,2) DEFAULT 0,
    account_id UUID REFERENCES acc_chart_of_accounts(account_id),
    account_name VARCHAR(200),
    -- journal-specific
    debit_or_credit VARCHAR(10), -- debit, credit
    amount NUMERIC(20,2),
    -- ordering
    item_order INT DEFAULT 0,
    location_id UUID,
    tags TEXT[],
    custom_fields JSONB DEFAULT '[]',
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_line_items_parent ON acc_line_items(parent_id, parent_type);

-- ══════════════════════════════════════════════════════════════
-- ESTIMATES
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_estimates (
    estimate_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    estimate_number VARCHAR(100),
    customer_id UUID NOT NULL REFERENCES acc_contacts(contact_id),
    contact_persons JSONB DEFAULT '[]',
    currency_id UUID REFERENCES acc_currencies(currency_id),
    status VARCHAR(30) DEFAULT 'draft', -- draft, sent, invoiced, accepted, declined, expired
    reference_number VARCHAR(100),
    template_id UUID,
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    expiry_date DATE,
    exchange_rate NUMERIC(20,6) DEFAULT 1.0,
    discount NUMERIC(20,2) DEFAULT 0,
    is_discount_before_tax BOOLEAN DEFAULT true,
    discount_type VARCHAR(20) DEFAULT 'entity_level',
    is_inclusive_tax BOOLEAN DEFAULT false,
    salesperson_name VARCHAR(200),
    custom_body TEXT,
    custom_subject VARCHAR(255),
    custom_fields JSONB DEFAULT '[]',
    tags TEXT[],
    notes TEXT,
    terms TEXT,
    sub_total NUMERIC(20,2) DEFAULT 0,
    tax_total NUMERIC(20,2) DEFAULT 0,
    total NUMERIC(20,2) DEFAULT 0,
    place_of_supply VARCHAR(100),
    gst_treatment VARCHAR(50),
    gst_no VARCHAR(50),
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_estimates_user ON acc_estimates(user_id);
CREATE INDEX IF NOT EXISTS idx_acc_estimates_customer ON acc_estimates(customer_id);

-- ══════════════════════════════════════════════════════════════
-- SALES ORDERS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_sales_orders (
    salesorder_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    salesorder_number VARCHAR(100),
    customer_id UUID NOT NULL REFERENCES acc_contacts(contact_id),
    contact_persons JSONB DEFAULT '[]',
    currency_id UUID REFERENCES acc_currencies(currency_id),
    status VARCHAR(30) DEFAULT 'draft', -- draft, confirmed, closed, void
    reference_number VARCHAR(100),
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    shipment_date DATE,
    delivery_method VARCHAR(100),
    exchange_rate NUMERIC(20,6) DEFAULT 1.0,
    discount NUMERIC(20,2) DEFAULT 0,
    is_discount_before_tax BOOLEAN DEFAULT true,
    discount_type VARCHAR(20) DEFAULT 'entity_level',
    is_inclusive_tax BOOLEAN DEFAULT false,
    salesperson_id UUID,
    salesperson_name VARCHAR(200),
    merchant_id UUID,
    location_id UUID,
    custom_fields JSONB DEFAULT '[]',
    tags TEXT[],
    notes TEXT,
    terms TEXT,
    sub_total NUMERIC(20,2) DEFAULT 0,
    tax_total NUMERIC(20,2) DEFAULT 0,
    total NUMERIC(20,2) DEFAULT 0,
    place_of_supply VARCHAR(100),
    gst_treatment VARCHAR(50),
    gst_no VARCHAR(50),
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_sales_orders_user ON acc_sales_orders(user_id);

-- ══════════════════════════════════════════════════════════════
-- SALES RECEIPTS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_sales_receipts (
    sales_receipt_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    receipt_number VARCHAR(100),
    customer_id UUID NOT NULL REFERENCES acc_contacts(contact_id),
    payment_mode VARCHAR(50) NOT NULL,
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    deposit_to_account_id UUID REFERENCES acc_chart_of_accounts(account_id),
    currency_id UUID REFERENCES acc_currencies(currency_id),
    exchange_rate NUMERIC(20,6) DEFAULT 1.0,
    status VARCHAR(30) DEFAULT 'draft',
    notes TEXT,
    terms TEXT,
    custom_fields JSONB DEFAULT '[]',
    tags TEXT[],
    sub_total NUMERIC(20,2) DEFAULT 0,
    tax_total NUMERIC(20,2) DEFAULT 0,
    total NUMERIC(20,2) DEFAULT 0,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_sales_receipts_user ON acc_sales_receipts(user_id);

-- ══════════════════════════════════════════════════════════════
-- CREDIT NOTES
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_credit_notes (
    creditnote_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    creditnote_number VARCHAR(100),
    customer_id UUID NOT NULL REFERENCES acc_contacts(contact_id),
    contact_persons JSONB DEFAULT '[]',
    currency_id UUID REFERENCES acc_currencies(currency_id),
    status VARCHAR(30) DEFAULT 'draft', -- draft, sent, open, closed, void
    reference_number VARCHAR(100),
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    exchange_rate NUMERIC(20,6) DEFAULT 1.0,
    is_inclusive_tax BOOLEAN DEFAULT false,
    location_id UUID,
    custom_fields JSONB DEFAULT '[]',
    tags TEXT[],
    notes TEXT,
    terms TEXT,
    sub_total NUMERIC(20,2) DEFAULT 0,
    tax_total NUMERIC(20,2) DEFAULT 0,
    total NUMERIC(20,2) DEFAULT 0,
    balance NUMERIC(20,2) DEFAULT 0,
    gst_treatment VARCHAR(50),
    tax_treatment VARCHAR(50),
    gst_no VARCHAR(50),
    is_reverse_charge_applied BOOLEAN DEFAULT false,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_credit_notes_user ON acc_credit_notes(user_id);

-- ══════════════════════════════════════════════════════════════
-- CUSTOMER DEBIT NOTES
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_debit_notes (
    debit_note_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    debit_note_number VARCHAR(100),
    customer_id UUID NOT NULL REFERENCES acc_contacts(contact_id),
    currency_id UUID REFERENCES acc_currencies(currency_id),
    status VARCHAR(30) DEFAULT 'draft',
    reference_number VARCHAR(100),
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    exchange_rate NUMERIC(20,6) DEFAULT 1.0,
    is_inclusive_tax BOOLEAN DEFAULT false,
    location_id UUID,
    custom_fields JSONB DEFAULT '[]',
    tags TEXT[],
    notes TEXT,
    terms TEXT,
    sub_total NUMERIC(20,2) DEFAULT 0,
    tax_total NUMERIC(20,2) DEFAULT 0,
    total NUMERIC(20,2) DEFAULT 0,
    balance NUMERIC(20,2) DEFAULT 0,
    gst_treatment VARCHAR(50),
    tax_treatment VARCHAR(50),
    gst_no VARCHAR(50),
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ══════════════════════════════════════════════════════════════
-- CUSTOMER PAYMENTS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_customer_payments (
    payment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_number VARCHAR(100),
    customer_id UUID NOT NULL REFERENCES acc_contacts(contact_id),
    payment_mode VARCHAR(50) NOT NULL,
    amount NUMERIC(20,2) NOT NULL,
    bank_charges NUMERIC(20,2) DEFAULT 0,
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    reference_number VARCHAR(100),
    description TEXT,
    exchange_rate NUMERIC(20,6) DEFAULT 1.0,
    account_id UUID REFERENCES acc_chart_of_accounts(account_id),
    tax_amount_withheld NUMERIC(20,2) DEFAULT 0,
    location_id UUID,
    invoices JSONB DEFAULT '[]', -- [{invoice_id, amount_applied}]
    custom_fields JSONB DEFAULT '[]',
    tags TEXT[],
    unused_amount NUMERIC(20,2) DEFAULT 0,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_cust_payments_user ON acc_customer_payments(user_id);
CREATE INDEX IF NOT EXISTS idx_acc_cust_payments_customer ON acc_customer_payments(customer_id);

-- ══════════════════════════════════════════════════════════════
-- BILLS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_bills (
    bill_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bill_number VARCHAR(100),
    vendor_id UUID NOT NULL REFERENCES acc_contacts(contact_id),
    currency_id UUID REFERENCES acc_currencies(currency_id),
    status VARCHAR(30) DEFAULT 'draft', -- draft, open, overdue, paid, void, partially_paid
    reference_number VARCHAR(100),
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    due_date DATE,
    payment_terms INT,
    payment_terms_label VARCHAR(100),
    exchange_rate NUMERIC(20,6) DEFAULT 1.0,
    is_item_level_tax_calc BOOLEAN DEFAULT true,
    is_inclusive_tax BOOLEAN DEFAULT false,
    adjustment NUMERIC(20,2) DEFAULT 0,
    adjustment_description VARCHAR(255),
    location_id UUID,
    recurring_bill_id UUID,
    purchaseorder_ids JSONB DEFAULT '[]',
    custom_fields JSONB DEFAULT '[]',
    tags TEXT[],
    notes TEXT,
    terms TEXT,
    sub_total NUMERIC(20,2) DEFAULT 0,
    tax_total NUMERIC(20,2) DEFAULT 0,
    total NUMERIC(20,2) DEFAULT 0,
    balance NUMERIC(20,2) DEFAULT 0,
    payment_made NUMERIC(20,2) DEFAULT 0,
    place_of_supply VARCHAR(100),
    source_of_supply VARCHAR(100),
    destination_of_supply VARCHAR(100),
    gst_treatment VARCHAR(50),
    tax_treatment VARCHAR(50),
    gst_no VARCHAR(50),
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_bills_user ON acc_bills(user_id);
CREATE INDEX IF NOT EXISTS idx_acc_bills_vendor ON acc_bills(vendor_id);
CREATE INDEX IF NOT EXISTS idx_acc_bills_status ON acc_bills(status);

-- ══════════════════════════════════════════════════════════════
-- PURCHASE ORDERS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_purchase_orders (
    purchaseorder_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    purchaseorder_number VARCHAR(100),
    vendor_id UUID NOT NULL REFERENCES acc_contacts(contact_id),
    currency_id UUID REFERENCES acc_currencies(currency_id),
    status VARCHAR(30) DEFAULT 'draft', -- draft, open, billed, cancelled, closed
    reference_number VARCHAR(100),
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    delivery_date DATE,
    exchange_rate NUMERIC(20,6) DEFAULT 1.0,
    discount NUMERIC(20,2) DEFAULT 0,
    is_discount_before_tax BOOLEAN DEFAULT true,
    discount_type VARCHAR(20) DEFAULT 'entity_level',
    is_inclusive_tax BOOLEAN DEFAULT false,
    location_id UUID,
    custom_fields JSONB DEFAULT '[]',
    tags TEXT[],
    notes TEXT,
    terms TEXT,
    sub_total NUMERIC(20,2) DEFAULT 0,
    tax_total NUMERIC(20,2) DEFAULT 0,
    total NUMERIC(20,2) DEFAULT 0,
    place_of_supply VARCHAR(100),
    gst_treatment VARCHAR(50),
    gst_no VARCHAR(50),
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_purchase_orders_user ON acc_purchase_orders(user_id);

-- ══════════════════════════════════════════════════════════════
-- VENDOR PAYMENTS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_vendor_payments (
    vendorpayment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_number VARCHAR(100),
    vendor_id UUID NOT NULL REFERENCES acc_contacts(contact_id),
    amount NUMERIC(20,2) NOT NULL,
    payment_mode VARCHAR(50),
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    reference_number VARCHAR(100),
    description TEXT,
    exchange_rate NUMERIC(20,6) DEFAULT 1.0,
    paid_through_account_id UUID REFERENCES acc_chart_of_accounts(account_id),
    check_details JSONB DEFAULT '{}',
    is_paid_via_print_check BOOLEAN DEFAULT false,
    location_id UUID,
    bills JSONB DEFAULT '[]', -- [{bill_id, amount_applied}]
    custom_fields JSONB DEFAULT '[]',
    tags TEXT[],
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_vendor_payments_user ON acc_vendor_payments(user_id);

-- ══════════════════════════════════════════════════════════════
-- VENDOR CREDITS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_vendor_credits (
    vendorcredit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vendorcredit_number VARCHAR(100),
    vendor_id UUID NOT NULL REFERENCES acc_contacts(contact_id),
    currency_id UUID REFERENCES acc_currencies(currency_id),
    status VARCHAR(30) DEFAULT 'draft', -- draft, open, closed, void
    reference_number VARCHAR(100),
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    exchange_rate NUMERIC(20,6) DEFAULT 1.0,
    is_inclusive_tax BOOLEAN DEFAULT false,
    location_id UUID,
    custom_fields JSONB DEFAULT '[]',
    tags TEXT[],
    notes TEXT,
    sub_total NUMERIC(20,2) DEFAULT 0,
    tax_total NUMERIC(20,2) DEFAULT 0,
    total NUMERIC(20,2) DEFAULT 0,
    balance NUMERIC(20,2) DEFAULT 0,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ══════════════════════════════════════════════════════════════
-- EXPENSES
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_expenses (
    expense_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    expense_number VARCHAR(100),
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    account_id UUID NOT NULL REFERENCES acc_chart_of_accounts(account_id),
    amount NUMERIC(20,2) NOT NULL,
    paid_through_account_id UUID REFERENCES acc_chart_of_accounts(account_id),
    tax_id UUID REFERENCES acc_taxes(tax_id),
    is_inclusive_tax BOOLEAN DEFAULT false,
    is_billable BOOLEAN DEFAULT false,
    reference_number VARCHAR(100),
    description TEXT,
    customer_id UUID REFERENCES acc_contacts(contact_id),
    vendor_id UUID REFERENCES acc_contacts(contact_id),
    currency_id UUID REFERENCES acc_currencies(currency_id),
    exchange_rate NUMERIC(20,6) DEFAULT 1.0,
    project_id UUID,
    mileage_type VARCHAR(50),
    start_reading NUMERIC(20,2),
    end_reading NUMERIC(20,2),
    distance NUMERIC(20,2),
    mileage_unit VARCHAR(20),
    status VARCHAR(30) DEFAULT 'unbilled',
    location_id UUID,
    place_of_supply VARCHAR(100),
    source_of_supply VARCHAR(100),
    destination_of_supply VARCHAR(100),
    hsn_or_sac VARCHAR(50),
    gst_no VARCHAR(50),
    vat_treatment VARCHAR(50),
    tax_treatment VARCHAR(50),
    product_type VARCHAR(50),
    custom_fields JSONB DEFAULT '[]',
    tags TEXT[],
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_expenses_user ON acc_expenses(user_id);

-- ══════════════════════════════════════════════════════════════
-- BANK ACCOUNTS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_bank_accounts (
    account_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_name VARCHAR(200) NOT NULL,
    account_type VARCHAR(50) NOT NULL, -- bank, credit_card
    account_number VARCHAR(50),
    account_code VARCHAR(50),
    currency_id UUID REFERENCES acc_currencies(currency_id),
    currency_code VARCHAR(10),
    description TEXT,
    bank_name VARCHAR(200),
    routing_number VARCHAR(50),
    is_primary_account BOOLEAN DEFAULT false,
    is_paypal_account BOOLEAN DEFAULT false,
    paypal_type VARCHAR(50),
    paypal_email_address VARCHAR(255),
    balance NUMERIC(20,2) DEFAULT 0,
    unconfirmed_balance NUMERIC(20,2) DEFAULT 0,
    is_active BOOLEAN DEFAULT true,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_bank_accounts_user ON acc_bank_accounts(user_id);

-- ══════════════════════════════════════════════════════════════
-- BANK TRANSACTIONS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_bank_transactions (
    bank_transaction_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_type VARCHAR(50) NOT NULL, -- deposit, expense_refund, owner_contribution, transfer_fund, etc.
    from_account_id UUID REFERENCES acc_bank_accounts(account_id),
    to_account_id UUID REFERENCES acc_bank_accounts(account_id),
    amount NUMERIC(20,2) NOT NULL DEFAULT 0,
    payment_mode VARCHAR(50),
    exchange_rate NUMERIC(20,6) DEFAULT 1.0,
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    customer_id UUID REFERENCES acc_contacts(contact_id),
    reference_number VARCHAR(100),
    description TEXT,
    currency_id UUID REFERENCES acc_currencies(currency_id),
    tax_id UUID REFERENCES acc_taxes(tax_id),
    is_inclusive_tax BOOLEAN DEFAULT false,
    bank_charges NUMERIC(20,2) DEFAULT 0,
    status VARCHAR(30) DEFAULT 'manually_added', -- manually_added, matched, excluded, categorized
    custom_fields JSONB DEFAULT '[]',
    tags TEXT[],
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_bank_txns_user ON acc_bank_transactions(user_id);

-- ══════════════════════════════════════════════════════════════
-- BANK RULES
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_bank_rules (
    rule_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_name VARCHAR(200) NOT NULL,
    account_id UUID REFERENCES acc_bank_accounts(account_id),
    target_account_id UUID REFERENCES acc_chart_of_accounts(account_id),
    rule_order INT DEFAULT 0,
    apply_to VARCHAR(50) DEFAULT 'withdrawals', -- withdrawals, deposits, both
    criteria_type VARCHAR(20) DEFAULT 'and', -- and, or
    criterion JSONB DEFAULT '[]', -- [{field, comparator, value}]
    record_as VARCHAR(50), -- expense, bill
    is_active BOOLEAN DEFAULT true,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ══════════════════════════════════════════════════════════════
-- JOURNALS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_journals (
    journal_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    journal_number VARCHAR(100),
    journal_date DATE NOT NULL DEFAULT CURRENT_DATE,
    reference_number VARCHAR(100),
    notes TEXT,
    journal_type VARCHAR(50) DEFAULT 'both', -- debit, credit, both
    status VARCHAR(30) DEFAULT 'published', -- draft, published
    currency_id UUID REFERENCES acc_currencies(currency_id),
    exchange_rate NUMERIC(20,6) DEFAULT 1.0,
    location_id UUID,
    vat_treatment VARCHAR(50),
    include_in_vat_return BOOLEAN DEFAULT false,
    product_type VARCHAR(50),
    is_bas_adjustment BOOLEAN DEFAULT false,
    total NUMERIC(20,2) DEFAULT 0,
    custom_fields JSONB DEFAULT '[]',
    tags TEXT[],
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_journals_user ON acc_journals(user_id);

-- ══════════════════════════════════════════════════════════════
-- OPENING BALANCES
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_opening_balances (
    opening_balance_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    accounts JSONB DEFAULT '[]', -- [{account_id, debit, credit}]
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ══════════════════════════════════════════════════════════════
-- BASE CURRENCY ADJUSTMENTS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_base_currency_adjustments (
    adjustment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    adjustment_date DATE NOT NULL DEFAULT CURRENT_DATE,
    exchange_rate NUMERIC(20,6) NOT NULL,
    currency_id UUID REFERENCES acc_currencies(currency_id),
    notes TEXT,
    gain_or_loss NUMERIC(20,2) DEFAULT 0,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ══════════════════════════════════════════════════════════════
-- RECURRING INVOICES
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_recurring_invoices (
    recurringinvoice_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recurrence_name VARCHAR(200),
    customer_id UUID NOT NULL REFERENCES acc_contacts(contact_id),
    contact_persons JSONB DEFAULT '[]',
    currency_id UUID REFERENCES acc_currencies(currency_id),
    status VARCHAR(30) DEFAULT 'active', -- active, stopped, expired
    repeat_every INT DEFAULT 1,
    recurrence_frequency VARCHAR(30) DEFAULT 'months', -- weeks, months, years
    start_date DATE,
    end_date DATE,
    payment_terms INT,
    payment_terms_label VARCHAR(100),
    discount NUMERIC(20,2) DEFAULT 0,
    is_discount_before_tax BOOLEAN DEFAULT true,
    discount_type VARCHAR(20) DEFAULT 'entity_level',
    is_inclusive_tax BOOLEAN DEFAULT false,
    exchange_rate NUMERIC(20,6) DEFAULT 1.0,
    salesperson_name VARCHAR(200),
    custom_fields JSONB DEFAULT '[]',
    tags TEXT[],
    notes TEXT,
    terms TEXT,
    sub_total NUMERIC(20,2) DEFAULT 0,
    tax_total NUMERIC(20,2) DEFAULT 0,
    total NUMERIC(20,2) DEFAULT 0,
    last_sent_date DATE,
    next_invoice_date DATE,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ══════════════════════════════════════════════════════════════
-- RECURRING BILLS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_recurring_bills (
    recurringbill_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recurrence_name VARCHAR(200),
    vendor_id UUID NOT NULL REFERENCES acc_contacts(contact_id),
    currency_id UUID REFERENCES acc_currencies(currency_id),
    status VARCHAR(30) DEFAULT 'active',
    repeat_every INT DEFAULT 1,
    recurrence_frequency VARCHAR(30) DEFAULT 'months',
    start_date DATE,
    end_date DATE,
    payment_terms INT,
    payment_terms_label VARCHAR(100),
    exchange_rate NUMERIC(20,6) DEFAULT 1.0,
    is_inclusive_tax BOOLEAN DEFAULT false,
    custom_fields JSONB DEFAULT '[]',
    tags TEXT[],
    notes TEXT,
    sub_total NUMERIC(20,2) DEFAULT 0,
    tax_total NUMERIC(20,2) DEFAULT 0,
    total NUMERIC(20,2) DEFAULT 0,
    last_created_date DATE,
    next_bill_date DATE,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ══════════════════════════════════════════════════════════════
-- RECURRING EXPENSES
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_recurring_expenses (
    recurringexpense_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recurrence_name VARCHAR(200),
    account_id UUID NOT NULL REFERENCES acc_chart_of_accounts(account_id),
    amount NUMERIC(20,2) NOT NULL,
    paid_through_account_id UUID REFERENCES acc_chart_of_accounts(account_id),
    status VARCHAR(30) DEFAULT 'active',
    repeat_every INT DEFAULT 1,
    recurrence_frequency VARCHAR(30) DEFAULT 'months',
    start_date DATE,
    end_date DATE,
    tax_id UUID REFERENCES acc_taxes(tax_id),
    is_inclusive_tax BOOLEAN DEFAULT false,
    is_billable BOOLEAN DEFAULT false,
    reference_number VARCHAR(100),
    description TEXT,
    customer_id UUID REFERENCES acc_contacts(contact_id),
    vendor_id UUID REFERENCES acc_contacts(contact_id),
    currency_id UUID REFERENCES acc_currencies(currency_id),
    exchange_rate NUMERIC(20,6) DEFAULT 1.0,
    project_id UUID,
    custom_fields JSONB DEFAULT '[]',
    tags TEXT[],
    last_created_date DATE,
    next_expense_date DATE,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ══════════════════════════════════════════════════════════════
-- LOCATIONS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_locations (
    location_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    location_name VARCHAR(200) NOT NULL,
    location_code VARCHAR(50),
    address JSONB DEFAULT '{}',
    is_primary BOOLEAN DEFAULT false,
    status VARCHAR(20) DEFAULT 'active',
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ══════════════════════════════════════════════════════════════
-- REPORTING TAGS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_reporting_tags (
    tag_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tag_name VARCHAR(200) NOT NULL,
    tag_options JSONB DEFAULT '[]', -- [{option_id, option_name, is_default}]
    is_active BOOLEAN DEFAULT true,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ══════════════════════════════════════════════════════════════
-- FIXED ASSETS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_fixed_assets (
    fixed_asset_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    asset_name VARCHAR(200) NOT NULL,
    description TEXT,
    acquisition_date DATE,
    acquisition_cost NUMERIC(20,2),
    residual_value NUMERIC(20,2) DEFAULT 0,
    useful_life_years INT,
    depreciation_method VARCHAR(50) DEFAULT 'straight_line', -- straight_line, declining_balance
    depreciation_rate NUMERIC(10,4),
    asset_account_id UUID REFERENCES acc_chart_of_accounts(account_id),
    depreciation_account_id UUID REFERENCES acc_chart_of_accounts(account_id),
    accumulated_depreciation_account_id UUID REFERENCES acc_chart_of_accounts(account_id),
    status VARCHAR(30) DEFAULT 'active', -- active, disposed, fully_depreciated
    current_value NUMERIC(20,2),
    accumulated_depreciation NUMERIC(20,2) DEFAULT 0,
    serial_number VARCHAR(100),
    location_id UUID,
    custom_fields JSONB DEFAULT '[]',
    tags TEXT[],
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ══════════════════════════════════════════════════════════════
-- PROJECTS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_projects (
    project_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_name VARCHAR(200) NOT NULL,
    customer_id UUID NOT NULL REFERENCES acc_contacts(contact_id),
    description TEXT,
    billing_type VARCHAR(50) NOT NULL DEFAULT 'fixed_cost_for_project',
    status VARCHAR(30) DEFAULT 'active', -- active, inactive, completed
    rate NUMERIC(20,2) DEFAULT 0,
    budget_type VARCHAR(50),
    budget_hours NUMERIC(10,2),
    budget_amount NUMERIC(20,2),
    cost_budget_amount NUMERIC(20,2),
    currency_id UUID REFERENCES acc_currencies(currency_id),
    total_hours NUMERIC(20,2) DEFAULT 0,
    billable_hours NUMERIC(20,2) DEFAULT 0,
    billed_hours NUMERIC(20,2) DEFAULT 0,
    un_billed_hours NUMERIC(20,2) DEFAULT 0,
    custom_fields JSONB DEFAULT '[]',
    tags TEXT[],
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_projects_user ON acc_projects(user_id);

-- ══════════════════════════════════════════════════════════════
-- TASKS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_tasks (
    task_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID REFERENCES acc_projects(project_id) ON DELETE CASCADE,
    task_name VARCHAR(200) NOT NULL,
    description TEXT,
    rate NUMERIC(20,2) DEFAULT 0,
    budget_hours NUMERIC(10,2),
    total_hours NUMERIC(20,2) DEFAULT 0,
    billed_hours NUMERIC(20,2) DEFAULT 0,
    un_billed_hours NUMERIC(20,2) DEFAULT 0,
    status VARCHAR(30) DEFAULT 'active',
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ══════════════════════════════════════════════════════════════
-- TIME ENTRIES
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_time_entries (
    timeentry_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES acc_projects(project_id),
    task_id UUID REFERENCES acc_tasks(task_id),
    staff_user_id UUID,
    log_date DATE NOT NULL DEFAULT CURRENT_DATE,
    log_time VARCHAR(10), -- HH:MM format
    begin_time VARCHAR(10),
    end_time VARCHAR(10),
    is_billable BOOLEAN DEFAULT true,
    is_billed BOOLEAN DEFAULT false,
    notes TEXT,
    timer_started_at TIMESTAMPTZ,
    timer_duration_in_minutes INT DEFAULT 0,
    tags TEXT[],
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ══════════════════════════════════════════════════════════════
-- RETAINER INVOICES
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_retainer_invoices (
    retainerinvoice_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    retainerinvoice_number VARCHAR(100),
    customer_id UUID NOT NULL REFERENCES acc_contacts(contact_id),
    contact_persons JSONB DEFAULT '[]',
    currency_id UUID REFERENCES acc_currencies(currency_id),
    status VARCHAR(30) DEFAULT 'draft', -- draft, sent, paid, void
    reference_number VARCHAR(100),
    template_id UUID,
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    due_date DATE,
    payment_terms INT,
    payment_terms_label VARCHAR(100),
    exchange_rate NUMERIC(20,6) DEFAULT 1.0,
    is_inclusive_tax BOOLEAN DEFAULT false,
    custom_fields JSONB DEFAULT '[]',
    tags TEXT[],
    notes TEXT,
    terms TEXT,
    sub_total NUMERIC(20,2) DEFAULT 0,
    tax_total NUMERIC(20,2) DEFAULT 0,
    total NUMERIC(20,2) DEFAULT 0,
    balance NUMERIC(20,2) DEFAULT 0,
    payment_made NUMERIC(20,2) DEFAULT 0,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ══════════════════════════════════════════════════════════════
-- CUSTOMIZATION TABLES
-- ══════════════════════════════════════════════════════════════

-- Custom Fields
CREATE TABLE IF NOT EXISTS acc_custom_fields (
    field_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    module VARCHAR(50) NOT NULL, -- invoices, bills, contacts, etc.
    label VARCHAR(200) NOT NULL,
    data_type VARCHAR(30) DEFAULT 'string', -- string, number, date, dropdown, checkbox, url, email
    placeholder VARCHAR(200),
    is_mandatory BOOLEAN DEFAULT false,
    show_in_all_pdf BOOLEAN DEFAULT false,
    is_active BOOLEAN DEFAULT true,
    options JSONB DEFAULT '[]',
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Custom Views
CREATE TABLE IF NOT EXISTS acc_custom_views (
    customview_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    module VARCHAR(50) NOT NULL,
    view_name VARCHAR(200) NOT NULL,
    sort_column VARCHAR(100),
    sort_order VARCHAR(10) DEFAULT 'asc',
    criteria JSONB DEFAULT '[]',
    fields JSONB DEFAULT '[]',
    is_default BOOLEAN DEFAULT false,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Blueprints
CREATE TABLE IF NOT EXISTS acc_blueprints (
    blueprint_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    module VARCHAR(50) NOT NULL,
    blueprint_name VARCHAR(200) NOT NULL,
    description TEXT,
    transitions JSONB DEFAULT '[]',
    is_active BOOLEAN DEFAULT true,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Webhooks
CREATE TABLE IF NOT EXISTS acc_webhooks (
    webhook_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    webhook_name VARCHAR(200) NOT NULL,
    url VARCHAR(500) NOT NULL,
    http_method VARCHAR(10) DEFAULT 'POST',
    module VARCHAR(50) NOT NULL,
    events JSONB DEFAULT '[]',
    headers JSONB DEFAULT '{}',
    is_active BOOLEAN DEFAULT true,
    last_triggered_at TIMESTAMPTZ,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Workflows
CREATE TABLE IF NOT EXISTS acc_workflows (
    workflow_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_name VARCHAR(200) NOT NULL,
    module VARCHAR(50) NOT NULL,
    description TEXT,
    trigger_type VARCHAR(50), -- on_create, on_update, on_create_or_update, date_based
    trigger_config JSONB DEFAULT '{}',
    criteria JSONB DEFAULT '[]',
    actions JSONB DEFAULT '[]',
    is_active BOOLEAN DEFAULT true,
    last_triggered_at TIMESTAMPTZ,
    execution_count INT DEFAULT 0,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Users (accounting-specific roles)
CREATE TABLE IF NOT EXISTS acc_users (
    acc_user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    email VARCHAR(255) NOT NULL,
    role_id UUID,
    role_name VARCHAR(100),
    is_active BOOLEAN DEFAULT true,
    photo_url VARCHAR(500),
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

COMMIT;
