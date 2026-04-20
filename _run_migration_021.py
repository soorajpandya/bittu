"""Run migration 021 on production database."""
import asyncio
import asyncpg
import os

async def run():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'], statement_cache_size=0)
    
    # Check which tables already exist
    existing = await conn.fetch("""
        SELECT table_name FROM information_schema.tables 
        WHERE table_name IN ('customer_ledger','supplier_ledger','invoices','invoice_items',
                             'expense_categories','expenses','tax_liability')
    """)
    print(f"Already existing tables: {[r['table_name'] for r in existing]}")
    
    # Check if payments table has customer_id column (might be the issue)
    pcols = await conn.fetch("""
        SELECT column_name FROM information_schema.columns WHERE table_name = 'payments'
    """)
    print(f"payments columns: {[r['column_name'] for r in pcols]}")
    
    # Run migration
    sql = open('migrations/021_subledger_invoices_expenses_tax.sql').read()
    try:
        await conn.execute(sql)
        print("Migration 021 applied successfully!")
    except Exception as e:
        print(f"Migration error: {e}")
        # Try to get more context
        print("Attempting individual sections...")
        raise
    finally:
        await conn.close()

asyncio.run(run())
