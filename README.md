# Bittu Backend

FastAPI service powering the Bittu restaurant operations platform (orders, payments, inventory, accounting, ERP, dine-in, KYC, RBAC, and reporting).

## Stack

- **Runtime:** Python 3.12 (prod) / 3.13 (dev), FastAPI, Uvicorn, Gunicorn
- **Database:** Postgres (Supabase) with raw-SQL migrations + monthly partitioning + RLS deny-all on all `public` tables
- **Auth:** JWT (Supabase) + custom RBAC (`app/core/auth.py`)
- **Deploy:** EC2 + systemd (`bittu.service`), gunicorn with 4 uvicorn workers
- **Observability:** Prometheus + Grafana (compose only)

## Layout

```
backend/
├── app/                 Application code
│   ├── api/             HTTP routers (versioned under v1/, plus admin/, public/)
│   ├── core/            Config, auth, events, state machines, db
│   ├── dependencies/    FastAPI dependencies
│   ├── middleware/      Request middleware
│   ├── models/          ORM / domain models
│   ├── realtime/        WebSocket / SSE handlers
│   ├── schemas/         Pydantic request/response schemas
│   ├── services/        Business logic
│   └── templates/       Jinja templates (emails, receipts)
├── migrations/          Sequential raw-SQL migrations (NNN_*.sql)
├── tests/               Pytest suite
├── scripts/             Operational scripts (seed_rbac, etc.)
├── deploy/              Deployment artifacts
│   ├── nginx/           nginx config + SSL mounts
│   └── monitoring/      Prometheus config
├── openapi/             Hand-curated OpenAPI 3 spec (modular)
├── docs/                Architecture, DB, frontend integration guides
├── monitoring/          (moved → deploy/monitoring)
├── main.py              ASGI entrypoint
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── requirements.txt
```

## Local development

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env  # then fill in secrets
uvicorn main:app --reload
```

## Migrations

Migrations are plain SQL files in `migrations/` applied in lexical order. To apply a new one against the configured `DATABASE_URL`:

```powershell
python -c "from app.core.config import get_settings; import psycopg2; s=get_settings(); c=psycopg2.connect(s.DATABASE_URL); c.cursor().execute(open('migrations/NNN_xxx.sql').read()); c.commit()"
```

One-shot migration runner scripts (`_run_migration_*.py`) are intentionally **not** kept in version control — write them locally, run, then discard.

## Tests

```powershell
pytest -q
```

## Deploy

```powershell
git push origin main
ssh ubuntu@<host> "cd /home/ubuntu/bittu && git pull --ff-only origin main && sudo systemctl restart bittu.service && sudo systemctl is-active bittu.service"
```
