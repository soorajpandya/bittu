"""Run migration 041: statements & tax invoices."""
import asyncio
from pathlib import Path
from app.core.database import init_db_pool, close_db_pool, get_connection

SQL_FILE = Path("migrations/041_statements_invoices.sql")


async def m():
    sql = SQL_FILE.read_text(encoding="utf-8")
    await init_db_pool()
    async with get_connection() as c:
        await c.execute(sql)
        n_perms = await c.fetchval(
            "SELECT COUNT(*) FROM permissions WHERE key IN "
            "('invoice.read','invoice.write','invoice.admin',"
            "'statement.read','statement.generate')"
        )
        n_enums = await c.fetchval(
            "SELECT COUNT(*) FROM pg_type "
            "WHERE typname IN ('invoice_status','statement_status')"
        )
        n_tables = await c.fetchval(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name IN ('tax_invoices','tax_invoice_line_items',"
            "'tax_invoice_seq','merchant_statements')"
        )
        n_fns = await c.fetchval(
            "SELECT COUNT(*) FROM pg_proc WHERE proname IN "
            "('fn_indian_fy_code','fn_next_invoice_number','fn_compute_statement')"
        )
        print(f"perms={n_perms} enums={n_enums} tables={n_tables} fns={n_fns}")
    await close_db_pool()


if __name__ == "__main__":
    asyncio.run(m())
