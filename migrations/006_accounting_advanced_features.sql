-- Migration 006: Advanced accounting features
-- Adds JSONB columns for comments/attachments/email-history,
-- plus sub-resource tables for refunds, credit applications,
-- tax authorities, tax exemptions, employees, fixed asset types, etc.

BEGIN;

-- ══════════════════════════════════════════════════════════════
-- JSONB columns on existing tables (comments, documents, email_history)
-- ══════════════════════════════════════════════════════════════

-- Invoices
ALTER TABLE acc_invoices ADD COLUMN IF NOT EXISTS comments JSONB DEFAULT '[]';
ALTER TABLE acc_invoices ADD COLUMN IF NOT EXISTS documents JSONB DEFAULT '[]';
ALTER TABLE acc_invoices ADD COLUMN IF NOT EXISTS email_history JSONB DEFAULT '[]';
ALTER TABLE acc_invoices ADD COLUMN IF NOT EXISTS payment_reminder_enabled BOOLEAN DEFAULT false;
ALTER TABLE acc_invoices ADD COLUMN IF NOT EXISTS billing_address JSONB DEFAULT '{}';
ALTER TABLE acc_invoices ADD COLUMN IF NOT EXISTS shipping_address JSONB DEFAULT '{}';
ALTER TABLE acc_invoices ADD COLUMN IF NOT EXISTS salesorder_id UUID;
ALTER TABLE acc_invoices ADD COLUMN IF NOT EXISTS write_off_amount NUMERIC(20,2) DEFAULT 0;

-- Estimates
ALTER TABLE acc_estimates ADD COLUMN IF NOT EXISTS comments JSONB DEFAULT '[]';
ALTER TABLE acc_estimates ADD COLUMN IF NOT EXISTS documents JSONB DEFAULT '[]';
ALTER TABLE acc_estimates ADD COLUMN IF NOT EXISTS email_history JSONB DEFAULT '[]';
ALTER TABLE acc_estimates ADD COLUMN IF NOT EXISTS billing_address JSONB DEFAULT '{}';
ALTER TABLE acc_estimates ADD COLUMN IF NOT EXISTS shipping_address JSONB DEFAULT '{}';

-- Sales Orders
ALTER TABLE acc_sales_orders ADD COLUMN IF NOT EXISTS comments JSONB DEFAULT '[]';
ALTER TABLE acc_sales_orders ADD COLUMN IF NOT EXISTS documents JSONB DEFAULT '[]';
ALTER TABLE acc_sales_orders ADD COLUMN IF NOT EXISTS email_history JSONB DEFAULT '[]';
ALTER TABLE acc_sales_orders ADD COLUMN IF NOT EXISTS billing_address JSONB DEFAULT '{}';
ALTER TABLE acc_sales_orders ADD COLUMN IF NOT EXISTS shipping_address JSONB DEFAULT '{}';

-- Purchase Orders
ALTER TABLE acc_purchase_orders ADD COLUMN IF NOT EXISTS comments JSONB DEFAULT '[]';
ALTER TABLE acc_purchase_orders ADD COLUMN IF NOT EXISTS documents JSONB DEFAULT '[]';
ALTER TABLE acc_purchase_orders ADD COLUMN IF NOT EXISTS email_history JSONB DEFAULT '[]';
ALTER TABLE acc_purchase_orders ADD COLUMN IF NOT EXISTS billing_address JSONB DEFAULT '{}';

-- Credit Notes
ALTER TABLE acc_credit_notes ADD COLUMN IF NOT EXISTS comments JSONB DEFAULT '[]';
ALTER TABLE acc_credit_notes ADD COLUMN IF NOT EXISTS documents JSONB DEFAULT '[]';
ALTER TABLE acc_credit_notes ADD COLUMN IF NOT EXISTS email_history JSONB DEFAULT '[]';
ALTER TABLE acc_credit_notes ADD COLUMN IF NOT EXISTS billing_address JSONB DEFAULT '{}';
ALTER TABLE acc_credit_notes ADD COLUMN IF NOT EXISTS shipping_address JSONB DEFAULT '{}';

-- Debit Notes
ALTER TABLE acc_debit_notes ADD COLUMN IF NOT EXISTS comments JSONB DEFAULT '[]';
ALTER TABLE acc_debit_notes ADD COLUMN IF NOT EXISTS documents JSONB DEFAULT '[]';
ALTER TABLE acc_debit_notes ADD COLUMN IF NOT EXISTS email_history JSONB DEFAULT '[]';

-- Bills
ALTER TABLE acc_bills ADD COLUMN IF NOT EXISTS comments JSONB DEFAULT '[]';
ALTER TABLE acc_bills ADD COLUMN IF NOT EXISTS documents JSONB DEFAULT '[]';
ALTER TABLE acc_bills ADD COLUMN IF NOT EXISTS billing_address JSONB DEFAULT '{}';

-- Retainer Invoices
ALTER TABLE acc_retainer_invoices ADD COLUMN IF NOT EXISTS comments JSONB DEFAULT '[]';
ALTER TABLE acc_retainer_invoices ADD COLUMN IF NOT EXISTS documents JSONB DEFAULT '[]';
ALTER TABLE acc_retainer_invoices ADD COLUMN IF NOT EXISTS email_history JSONB DEFAULT '[]';
ALTER TABLE acc_retainer_invoices ADD COLUMN IF NOT EXISTS billing_address JSONB DEFAULT '{}';

-- Vendor Credits
ALTER TABLE acc_vendor_credits ADD COLUMN IF NOT EXISTS comments JSONB DEFAULT '[]';
ALTER TABLE acc_vendor_credits ADD COLUMN IF NOT EXISTS documents JSONB DEFAULT '[]';

-- Expenses
ALTER TABLE acc_expenses ADD COLUMN IF NOT EXISTS comments JSONB DEFAULT '[]';
ALTER TABLE acc_expenses ADD COLUMN IF NOT EXISTS documents JSONB DEFAULT '[]';
ALTER TABLE acc_expenses ADD COLUMN IF NOT EXISTS receipt JSONB DEFAULT '{}';

-- Projects
ALTER TABLE acc_projects ADD COLUMN IF NOT EXISTS comments JSONB DEFAULT '[]';

-- Tasks
ALTER TABLE acc_tasks ADD COLUMN IF NOT EXISTS comments JSONB DEFAULT '[]';
ALTER TABLE acc_tasks ADD COLUMN IF NOT EXISTS documents JSONB DEFAULT '[]';
ALTER TABLE acc_tasks ADD COLUMN IF NOT EXISTS percentage NUMERIC(5,2) DEFAULT 0;

-- Fixed Assets
ALTER TABLE acc_fixed_assets ADD COLUMN IF NOT EXISTS comments JSONB DEFAULT '[]';
ALTER TABLE acc_fixed_assets ADD COLUMN IF NOT EXISTS history JSONB DEFAULT '[]';
ALTER TABLE acc_fixed_assets ADD COLUMN IF NOT EXISTS forecast JSONB DEFAULT '[]';
ALTER TABLE acc_fixed_assets ADD COLUMN IF NOT EXISTS write_off_date DATE;
ALTER TABLE acc_fixed_assets ADD COLUMN IF NOT EXISTS sold_date DATE;
ALTER TABLE acc_fixed_assets ADD COLUMN IF NOT EXISTS sold_amount NUMERIC(20,2);

-- Contacts
ALTER TABLE acc_contacts ADD COLUMN IF NOT EXISTS comments JSONB DEFAULT '[]';
ALTER TABLE acc_contacts ADD COLUMN IF NOT EXISTS addresses JSONB DEFAULT '[]';
ALTER TABLE acc_contacts ADD COLUMN IF NOT EXISTS track_1099 BOOLEAN DEFAULT false;
ALTER TABLE acc_contacts ADD COLUMN IF NOT EXISTS portal_enabled BOOLEAN DEFAULT false;
ALTER TABLE acc_contacts ADD COLUMN IF NOT EXISTS payment_reminder_enabled BOOLEAN DEFAULT true;

-- Workflows
ALTER TABLE acc_workflows ADD COLUMN IF NOT EXISTS sort_order INT DEFAULT 0;

-- Reporting Tags
ALTER TABLE acc_reporting_tags ADD COLUMN IF NOT EXISTS options JSONB DEFAULT '[]';
ALTER TABLE acc_reporting_tags ADD COLUMN IF NOT EXISTS criteria JSONB DEFAULT '{}';
ALTER TABLE acc_reporting_tags ADD COLUMN IF NOT EXISTS sort_order INT DEFAULT 0;

-- Custom Fields
ALTER TABLE acc_custom_fields ADD COLUMN IF NOT EXISTS sort_order INT DEFAULT 0;
ALTER TABLE acc_custom_fields ADD COLUMN IF NOT EXISTS dropdown_options JSONB DEFAULT '[]';
ALTER TABLE acc_custom_fields ADD COLUMN IF NOT EXISTS field_status VARCHAR(20) DEFAULT 'active';

-- ══════════════════════════════════════════════════════════════
-- CREDIT NOTE REFUNDS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_creditnote_refunds (
    creditnote_refund_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    creditnote_id UUID NOT NULL REFERENCES acc_credit_notes(creditnote_id) ON DELETE CASCADE,
    date DATE,
    refund_mode VARCHAR(50),
    reference_number VARCHAR(100),
    amount NUMERIC(20,2) NOT NULL DEFAULT 0,
    exchange_rate NUMERIC(20,6) DEFAULT 1.0,
    from_account_id UUID,
    description TEXT,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_cn_refunds_cn ON acc_creditnote_refunds(creditnote_id);
CREATE INDEX IF NOT EXISTS idx_acc_cn_refunds_user ON acc_creditnote_refunds(user_id);

-- ══════════════════════════════════════════════════════════════
-- CREDIT NOTE INVOICE APPLICATIONS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_creditnote_invoices (
    creditnote_invoice_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    creditnote_id UUID NOT NULL REFERENCES acc_credit_notes(creditnote_id) ON DELETE CASCADE,
    invoice_id UUID NOT NULL REFERENCES acc_invoices(invoice_id),
    amount_applied NUMERIC(20,2) NOT NULL DEFAULT 0,
    date DATE,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_cn_inv_cn ON acc_creditnote_invoices(creditnote_id);

-- ══════════════════════════════════════════════════════════════
-- VENDOR CREDIT REFUNDS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_vendor_credit_refunds (
    vendor_credit_refund_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vendor_credit_id UUID NOT NULL REFERENCES acc_vendor_credits(vendorcredit_id) ON DELETE CASCADE,
    date DATE,
    refund_mode VARCHAR(50),
    reference_number VARCHAR(100),
    amount NUMERIC(20,2) NOT NULL DEFAULT 0,
    exchange_rate NUMERIC(20,6) DEFAULT 1.0,
    account_id UUID,
    description TEXT,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_vc_refunds_vc ON acc_vendor_credit_refunds(vendor_credit_id);

-- ══════════════════════════════════════════════════════════════
-- VENDOR CREDIT BILL APPLICATIONS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_vendor_credit_bills (
    vendor_credit_bill_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vendor_credit_id UUID NOT NULL REFERENCES acc_vendor_credits(vendorcredit_id) ON DELETE CASCADE,
    bill_id UUID NOT NULL REFERENCES acc_bills(bill_id),
    amount_applied NUMERIC(20,2) NOT NULL DEFAULT 0,
    date DATE,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_vcb_vc ON acc_vendor_credit_bills(vendor_credit_id);

-- ══════════════════════════════════════════════════════════════
-- BILL PAYMENTS (applied payments on bills)
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_bill_payments (
    bill_payment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bill_id UUID NOT NULL REFERENCES acc_bills(bill_id) ON DELETE CASCADE,
    payment_id UUID,
    date DATE,
    amount NUMERIC(20,2) NOT NULL DEFAULT 0,
    payment_mode VARCHAR(50),
    description TEXT,
    reference_number VARCHAR(100),
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_bp_bill ON acc_bill_payments(bill_id);

-- ══════════════════════════════════════════════════════════════
-- INVOICE PAYMENTS (applied payments on invoices)
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_invoice_payments (
    invoice_payment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id UUID NOT NULL REFERENCES acc_invoices(invoice_id) ON DELETE CASCADE,
    payment_id UUID,
    date DATE,
    amount NUMERIC(20,2) NOT NULL DEFAULT 0,
    payment_mode VARCHAR(50),
    description TEXT,
    reference_number VARCHAR(100),
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_ip_inv ON acc_invoice_payments(invoice_id);

-- ══════════════════════════════════════════════════════════════
-- INVOICE CREDITS APPLIED
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_invoice_credits (
    invoice_credit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id UUID NOT NULL REFERENCES acc_invoices(invoice_id) ON DELETE CASCADE,
    creditnote_id UUID REFERENCES acc_credit_notes(creditnote_id),
    amount_applied NUMERIC(20,2) NOT NULL DEFAULT 0,
    date DATE,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_ic_inv ON acc_invoice_credits(invoice_id);

-- ══════════════════════════════════════════════════════════════
-- VENDOR PAYMENT REFUNDS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_vendor_payment_refunds (
    vendorpayment_refund_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vendor_payment_id UUID NOT NULL REFERENCES acc_vendor_payments(vendorpayment_id) ON DELETE CASCADE,
    date DATE,
    refund_mode VARCHAR(50),
    reference_number VARCHAR(100),
    amount NUMERIC(20,2) NOT NULL DEFAULT 0,
    account_id UUID,
    description TEXT,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_vpr_vp ON acc_vendor_payment_refunds(vendor_payment_id);

-- ══════════════════════════════════════════════════════════════
-- CUSTOMER PAYMENT REFUNDS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_customer_payment_refunds (
    refund_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_payment_id UUID NOT NULL REFERENCES acc_customer_payments(payment_id) ON DELETE CASCADE,
    date DATE,
    refund_mode VARCHAR(50),
    reference_number VARCHAR(100),
    amount NUMERIC(20,2) NOT NULL DEFAULT 0,
    account_id UUID,
    description TEXT,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_cpr_cp ON acc_customer_payment_refunds(customer_payment_id);

-- ══════════════════════════════════════════════════════════════
-- TAX AUTHORITIES
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_tax_authorities (
    tax_authority_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tax_authority_name VARCHAR(200) NOT NULL,
    description TEXT,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_ta_user ON acc_tax_authorities(user_id);

-- ══════════════════════════════════════════════════════════════
-- TAX EXEMPTIONS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_tax_exemptions (
    tax_exemption_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tax_exemption_code VARCHAR(100),
    name VARCHAR(200) NOT NULL,
    description TEXT,
    exemption_type VARCHAR(50),
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_te_user ON acc_tax_exemptions(user_id);

-- ══════════════════════════════════════════════════════════════
-- EMPLOYEES (for expense tracking)
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_employees (
    employee_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    email VARCHAR(255),
    status VARCHAR(20) DEFAULT 'active',
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_emp_user ON acc_employees(user_id);

-- ══════════════════════════════════════════════════════════════
-- FIXED ASSET TYPES
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_fixed_asset_types (
    fixed_asset_type_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    description TEXT,
    depreciation_method VARCHAR(50),
    useful_life_years INT,
    asset_account_id UUID,
    depreciation_account_id UUID,
    accumulated_depreciation_account_id UUID,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_fat_user ON acc_fixed_asset_types(user_id);

-- ══════════════════════════════════════════════════════════════
-- PROJECT USERS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_project_users (
    project_user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES acc_projects(project_id) ON DELETE CASCADE,
    user_ref_id UUID NOT NULL,
    role VARCHAR(50) DEFAULT 'member',
    rate NUMERIC(20,2) DEFAULT 0,
    budget_hours NUMERIC(10,2) DEFAULT 0,
    email VARCHAR(255),
    name VARCHAR(200),
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_pu_project ON acc_project_users(project_id);

-- ══════════════════════════════════════════════════════════════
-- WORKFLOW CUSTOM TRIGGERS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_custom_triggers (
    trigger_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    description TEXT,
    module VARCHAR(100),
    api_key VARCHAR(255),
    trigger_url TEXT,
    is_active BOOLEAN DEFAULT true,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_ct_user ON acc_custom_triggers(user_id);

-- ══════════════════════════════════════════════════════════════
-- WORKFLOW RETRY POLICIES
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_retry_policies (
    policy_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    retry_count INT DEFAULT 3,
    retry_interval INT DEFAULT 60,
    description TEXT,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_rp_user ON acc_retry_policies(user_id);

-- ══════════════════════════════════════════════════════════════
-- WORKFLOW LOGS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_workflow_logs (
    log_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id UUID REFERENCES acc_workflows(workflow_id),
    entity_type VARCHAR(100),
    entity_id UUID,
    action VARCHAR(100),
    status VARCHAR(50) DEFAULT 'success',
    details JSONB DEFAULT '{}',
    executed_at TIMESTAMPTZ DEFAULT now(),
    user_id UUID NOT NULL,
    branch_id UUID
);
CREATE INDEX IF NOT EXISTS idx_acc_wl_workflow ON acc_workflow_logs(workflow_id);
CREATE INDEX IF NOT EXISTS idx_acc_wl_user ON acc_workflow_logs(user_id);

-- ══════════════════════════════════════════════════════════════
-- CONTACT ADDRESSES (separate table for multi-address)
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_contact_addresses (
    address_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id UUID NOT NULL REFERENCES acc_contacts(contact_id) ON DELETE CASCADE,
    attention VARCHAR(200),
    address VARCHAR(500),
    street2 VARCHAR(500),
    city VARCHAR(100),
    state_code VARCHAR(10),
    state VARCHAR(100),
    zip VARCHAR(20),
    country VARCHAR(100),
    country_code VARCHAR(10),
    phone VARCHAR(30),
    fax VARCHAR(30),
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_ca_contact ON acc_contact_addresses(contact_id);

-- ══════════════════════════════════════════════════════════════
-- CUSTOM ACTIONS (admin automation)
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_custom_actions (
    action_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    module VARCHAR(100),
    action_type VARCHAR(50),
    config JSONB DEFAULT '{}',
    is_active BOOLEAN DEFAULT true,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_cact_user ON acc_custom_actions(user_id);

-- ══════════════════════════════════════════════════════════════
-- CUSTOM BUTTONS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_custom_buttons (
    button_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    module VARCHAR(100),
    location VARCHAR(50),
    action_type VARCHAR(50),
    action_url TEXT,
    config JSONB DEFAULT '{}',
    is_active BOOLEAN DEFAULT true,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_cbtn_user ON acc_custom_buttons(user_id);

-- ══════════════════════════════════════════════════════════════
-- CUSTOM FUNCTIONS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_custom_functions (
    function_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    module VARCHAR(100),
    script TEXT,
    input_params JSONB DEFAULT '[]',
    return_type VARCHAR(50),
    is_active BOOLEAN DEFAULT true,
    description TEXT,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_cfn_user ON acc_custom_functions(user_id);

-- ══════════════════════════════════════════════════════════════
-- CUSTOM FUNCTION LOGS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_custom_function_logs (
    log_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    function_id UUID REFERENCES acc_custom_functions(function_id) ON DELETE CASCADE,
    status VARCHAR(50),
    input JSONB DEFAULT '{}',
    output JSONB DEFAULT '{}',
    executed_at TIMESTAMPTZ DEFAULT now(),
    user_id UUID NOT NULL,
    branch_id UUID
);
CREATE INDEX IF NOT EXISTS idx_acc_cfl_fn ON acc_custom_function_logs(function_id);

-- ══════════════════════════════════════════════════════════════
-- CUSTOM MODULES
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_custom_modules (
    module_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    module_name VARCHAR(200) NOT NULL,
    api_name VARCHAR(200),
    description TEXT,
    fields JSONB DEFAULT '[]',
    relationships JSONB DEFAULT '[]',
    is_active BOOLEAN DEFAULT true,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_cm_user ON acc_custom_modules(user_id);

-- ══════════════════════════════════════════════════════════════
-- CUSTOM MODULE RECORDS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_custom_module_records (
    record_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    module_id UUID NOT NULL REFERENCES acc_custom_modules(module_id) ON DELETE CASCADE,
    data JSONB DEFAULT '{}',
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_cmr_module ON acc_custom_module_records(module_id);

-- ══════════════════════════════════════════════════════════════
-- CUSTOM SCHEDULERS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_custom_schedulers (
    scheduler_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    function_id UUID REFERENCES acc_custom_functions(function_id),
    frequency VARCHAR(50),
    cron_expression VARCHAR(100),
    next_execution TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT true,
    description TEXT,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_cs_user ON acc_custom_schedulers(user_id);

-- ══════════════════════════════════════════════════════════════
-- INTEGRATIONS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_integrations (
    integration_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    service VARCHAR(100),
    config JSONB DEFAULT '{}',
    is_active BOOLEAN DEFAULT true,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_int_user ON acc_integrations(user_id);

-- ══════════════════════════════════════════════════════════════
-- MODULE RENAMING
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_module_renames (
    rename_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    original_name VARCHAR(200) NOT NULL,
    custom_name VARCHAR(200) NOT NULL,
    module_type VARCHAR(100),
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_mr_user ON acc_module_renames(user_id);

-- ══════════════════════════════════════════════════════════════
-- RELATED LISTS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_related_lists (
    related_list_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    module VARCHAR(100),
    related_module VARCHAR(100),
    field_mapping JSONB DEFAULT '{}',
    is_active BOOLEAN DEFAULT true,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_rl_user ON acc_related_lists(user_id);

-- ══════════════════════════════════════════════════════════════
-- SANDBOX
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_sandboxes (
    sandbox_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    status VARCHAR(50) DEFAULT 'active',
    description TEXT,
    config JSONB DEFAULT '{}',
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_sb_user ON acc_sandboxes(user_id);

-- ══════════════════════════════════════════════════════════════
-- SANDBOX CHANGES
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_sandbox_changes (
    change_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sandbox_id UUID NOT NULL REFERENCES acc_sandboxes(sandbox_id) ON DELETE CASCADE,
    entity_type VARCHAR(100),
    entity_id UUID,
    change_type VARCHAR(50),
    before_data JSONB DEFAULT '{}',
    after_data JSONB DEFAULT '{}',
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_sc_sb ON acc_sandbox_changes(sandbox_id);

-- ══════════════════════════════════════════════════════════════
-- WEB TABS
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS acc_web_tabs (
    web_tab_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    url TEXT,
    location VARCHAR(50),
    description TEXT,
    is_active BOOLEAN DEFAULT true,
    user_id UUID NOT NULL,
    branch_id UUID,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acc_wt_user ON acc_web_tabs(user_id);

COMMIT;
