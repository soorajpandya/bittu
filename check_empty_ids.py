import asyncio
import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()

async def check():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], statement_cache_size=0)
    
    print("=== Checking for empty UUID keys ===")
    tables_uuid = [
        ("acc_expenses", "expense_id"),
        ("acc_bills", "bill_id"),
        ("acc_journals", "journal_id"),
        ("acc_contacts", "contact_id"),
        ("acc_chart_of_accounts", "account_id"),
        ("acc_bank_accounts", "account_id"),
        ("acc_taxes", "tax_id"),
        ("acc_invoices", "invoice_id"),
    ]
    for t, pk in tables_uuid:
        try:
            rows = await conn.fetch(
                f"SELECT * FROM {t} WHERE {pk}::text = '' OR {pk} IS NULL LIMIT 5"
            )
            if rows:
                print(f"  {t}.{pk}: {len(rows)} EMPTY!")
            else:
                cnt = await conn.fetchval(f"SELECT count(*) FROM {t}")
                print(f"  {t}.{pk}: OK ({cnt} rows)")
        except Exception as e:
            print(f"  {t}.{pk}: error - {e}")
    
    print("\n=== Fields used as Select values ===")
    empties = await conn.fetch(
        "SELECT contact_id, contact_name FROM acc_contacts WHERE contact_name IS NULL OR contact_name = ''"
    )
    print(f"  contacts with empty name: {len(empties)}")
    empties = await conn.fetch(
        "SELECT account_id, account_name FROM acc_chart_of_accounts WHERE account_name IS NULL OR account_name = ''"
    )
    print(f"  COA with empty name: {len(empties)}")
    empties = await conn.fetch(
        "SELECT account_id, account_name FROM acc_bank_accounts WHERE account_name IS NULL OR account_name = ''"
    )
    print(f"  bank accounts with empty name: {len(empties)}")
    
    print("\n=== All invoices ===")
    inv = await conn.fetch("SELECT invoice_id, invoice_number, status, customer_id FROM acc_invoices")
    for r in inv:
        print(f"  id={r['invoice_id']}, number='{r['invoice_number']}', status={r['status']}, customer={r['customer_id']}")
    
    print("\n=== Line items tables ===")
    for t in ["acc_invoice_line_items", "acc_bill_line_items", "acc_expense_line_items", "acc_journal_line_items"]:
        try:
            cnt = await conn.fetchval(f"SELECT count(*) FROM {t}")
            cols = await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name = $1 ORDER BY ordinal_position", t
            )
            col_names = [c['column_name'] for c in cols]
            print(f"  {t}: {cnt} rows, cols={col_names[:8]}...")
        except Exception as e:
            print(f"  {t}: {e}")
    
    await conn.close()

asyncio.run(check())
