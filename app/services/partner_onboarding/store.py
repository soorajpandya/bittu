"""Encrypted PostgreSQL storage; explicit tenant checks before every operation."""
from __future__ import annotations

import base64
import json
import os
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.database import get_service_connection
from .domain import Problem, fingerprint, policy, readiness, progress


def cipher():
    keys = os.getenv('ONBOARDING_ENCRYPTION_KEYS','').split(',')
    try:
        return MultiFernet([Fernet(key.strip().encode()) for key in keys if key.strip()])
    except (ValueError, TypeError):
        raise Problem(503,'DEPENDENCY_UNAVAILABLE','Configure ONBOARDING_ENCRYPTION_KEYS with Fernet keys') from None


def seal(value):
    return cipher().encrypt(json.dumps(value, default=str, separators=(',',':')).encode())


def unseal(value):
    try:
        return json.loads(cipher().decrypt(bytes(value)))
    except InvalidToken:
        raise Problem(503,'DEPENDENCY_UNAVAILABLE','Onboarding encryption key unavailable') from None


def uid(value):
    try:
        return UUID(str(value))
    except (ValueError,TypeError):
        raise Problem(404,'NOT_FOUND','Resource not found') from None


@asynccontextmanager
async def transaction():
    async with get_service_connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.onboarding_write','on',true)")
            yield conn


async def record(conn, rid, actor, lock=False):
    row = await conn.fetchrow('SELECT * FROM onboarding_records WHERE restaurant_id=$1 AND tenant_id=$2' +
                              (' FOR UPDATE' if lock else ''), uid(rid), uid(actor['tenant']))
    if not row:
        raise Problem(404,'NOT_FOUND','Restaurant not found')
    return dict(row), unseal(row['payload'])


async def resource(conn, rid, resource_id, kind=None):
    row = await conn.fetchrow('SELECT * FROM onboarding_resources WHERE restaurant_id=$1 AND id=$2',uid(rid),uid(resource_id))
    if not row or (kind and row['kind'] != kind):
        raise Problem(404,'NOT_FOUND','Resource not found')
    return unseal(row['payload']), row['content_cipher']


async def put_resource(conn, rid, resource_id, kind, payload, binary=None):
    await conn.execute('''INSERT INTO onboarding_resources(id,restaurant_id,kind,payload,content_cipher) VALUES($1,$2,$3,$4,$5)
      ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,content_cipher=COALESCE(excluded.content_cipher,onboarding_resources.content_cipher)''',
                       uid(resource_id),uid(rid),kind,seal(payload),binary)


async def counts(conn,rid):
    row = await conn.fetchrow('''SELECT count(*) FILTER(WHERE delivery_available) AS delivery,
        count(*) FILTER(WHERE dine_in_available) AS dining FROM items WHERE restaurant_id=$1''',uid(rid))
    return dict(row)


async def represent(conn, row, data, actor):
    import copy
    out = copy.deepcopy(data)
    ready = readiness(data,row['status'],await counts(conn,row['restaurant_id']))
    out.update(id=str(row['restaurant_id']),status=row['status'],revision=row['revision'],policyVersion=row['policy_version'],
               createdAt=row['created_at'].isoformat(),updatedAt=row['updated_at'].isoformat(),updatedBy=str(row['updated_by']),
               readiness=ready,progress=progress(data,ready))
    out['documents'] = [{'kind':kind, **doc} for kind,doc in out.get('documents',{}).items()]
    # Full values only for an authorized editor/reviewer explicitly requesting them.
    for doc in out['documents']:
        if not actor.get('sensitive'):
            for key in ('number','accountNumber'):
                if doc.get(key):
                    doc[key] = '*' * max(0,len(doc[key])-4) + doc[key][-4:]
        if 'document:read' not in actor['permissions']:
            for key in list(doc):
                if key not in ('kind','status','updatedAt'):
                    doc.pop(key)
    out['allowedActions'] = []
    for action,perm in [('Submit','onboarding:submit'),('Approve','onboarding:approve'),('Activate','restaurant:activate')]:
        if ready['can'+action] and perm in actor['permissions']:
            out['allowedActions'].append(action.lower())
    return out


def precondition(request, row):
    match = request.headers.get('if-match')
    if match is None:
        raise Problem(428,'PRECONDITION_REQUIRED','If-Match is required',revision=row['revision'])
    if match != '"' + str(row['revision']) + '"':
        raise Problem(412,'STALE_RECORD','Reload the current record before retrying',revision=row['revision'])


async def save(conn,row,data,actor,action,details=None,status=None):
    new_status = status or row['status']
    updated = await conn.fetchrow('''UPDATE onboarding_records SET payload=$2,status=$3,revision=revision+1,
        updated_at=now(),updated_by=$4,policy_version=$5 WHERE restaurant_id=$1 RETURNING *''',
        row['restaurant_id'],seal(data),new_status,uid(actor['id']),policy()['version'])
    await audit(conn,updated,actor,action,details)
    return dict(updated)


async def audit(conn,row,actor,action,details=None):
    # Details must contain field paths/IDs only, never document values or free-form reasons.
    safe = details or {}
    await conn.execute('''INSERT INTO onboarding_audit(restaurant_id,actor_id,action,revision,details)
        VALUES($1,$2,$3,$4,$5::jsonb)''',row['restaurant_id'],uid(actor['id']),action,row['revision'],json.dumps(safe))
    await conn.execute('INSERT INTO onboarding_outbox(restaurant_id,event) VALUES($1,$2::jsonb)',
                       row['restaurant_id'],json.dumps({'action':action,'revision':row['revision'],'actorId':actor['id']}))


async def idempotency(conn,request,actor,body):
    key=request.headers.get('idempotency-key','')
    if not key or len(key)>128:
        raise Problem(428,'IDEMPOTENCY_REQUIRED','A 1–128 character Idempotency-Key is required')
    op=request.method+' '+request.url.path
    scope=actor['id']+actor['tenant']+op+key
    await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',scope)
    digest=fingerprint({'body':body,'permissions':sorted(actor['permissions']),'sensitive':actor.get('sensitive',False)})
    row=await conn.fetchrow('''SELECT * FROM onboarding_idempotency WHERE actor_id=$1 AND tenant_id=$2
      AND operation=$3 AND key=$4''',uid(actor['id']),uid(actor['tenant']),op,key)
    if row:
        if row['request_hash'] != digest:
            raise Problem(409,'IDEMPOTENCY_CONFLICT','Key was already used with a different payload')
        return (op,key,digest), (unseal(row['response']),row['status_code'],row['revision'])
    return (op,key,digest), None


async def remember(conn,actor,identity,value,status,revision):
    op,key,digest=identity
    await conn.execute('''INSERT INTO onboarding_idempotency(actor_id,tenant_id,operation,key,request_hash,response,status_code,revision)
      VALUES($1,$2,$3,$4,$5,$6,$7,$8)''',uid(actor['id']),uid(actor['tenant']),op,key,digest,seal(value),status,revision)


def cursor_encode(value):
    return base64.urlsafe_b64encode(str(value).encode()).decode().rstrip('=')


def cursor_decode(value):
    if not value:
        return None
    try:
        if len(value)>128:
            raise ValueError()
        return base64.urlsafe_b64decode(value+'='*(-len(value)%4)).decode()
    except Exception:
        raise Problem(422,'VALIDATION_FAILED','Invalid cursor') from None
