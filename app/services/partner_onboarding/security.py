"""Revocable admin sessions and tenant-specific permission grants."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from app.core.auth import _decode_token
from . import store
from .domain import Problem

COOKIE = 'bittu_onboarding_session'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


async def grants(conn,actor,tenant):
    # Re-check platform membership and permissions on every request (revocation is immediate).
    if not await conn.fetchval('SELECT fn_is_platform_admin($1::uuid)',store.uid(actor)):
        raise Problem(403,'PERMISSION_DENIED','Platform admin access is required')
    row=await conn.fetchrow('SELECT permissions FROM onboarding_grants WHERE actor_id=$1 AND tenant_id=$2',store.uid(actor),store.uid(tenant))
    if not row:
        raise Problem(403,'PERMISSION_DENIED','No onboarding grant for this tenant')
    return list(row['permissions'])


def bearer(request):
    value=request.headers.get('authorization','')
    if not value.startswith('Bearer '):
        raise Problem(401,'UNAUTHENTICATED','A valid admin Bearer token is required')
    try:
        claims=_decode_token(value[7:])
        if not claims.get('sub'):
            raise ValueError()
        return claims
    except Exception:
        raise Problem(401,'UNAUTHENTICATED','Invalid or expired token') from None


async def actor(request,permission='restaurant:read'):
    token=request.cookies.get(COOKIE)
    async with store.transaction() as conn:
        if token:
            row=await conn.fetchrow('SELECT * FROM onboarding_sessions WHERE token_hash=$1 AND expires_at>now()',digest(token))
            if not row:
                raise Problem(401,'UNAUTHENTICATED','Session has expired')
            if request.method not in ('GET','HEAD','OPTIONS'):
                csrf=request.headers.get('x-csrf-token','')
                if not csrf or not secrets.compare_digest(digest(csrf),row['csrf_hash']):
                    raise Problem(403,'CSRF_FAILED','Valid X-CSRF-Token is required')
            aid,tenant,expiry=str(row['actor_id']),str(row['tenant_id']),row['expires_at'].isoformat()
        else:
            claims=bearer(request)
            aid,tenant=claims['sub'],request.headers.get('x-onboarding-tenant')
            if not tenant:
                raise Problem(403,'PERMISSION_DENIED','X-Onboarding-Tenant is required for bearer requests')
            expiry=datetime.fromtimestamp(claims['exp'],timezone.utc).isoformat()
        permissions=await grants(conn,aid,tenant)
    if permission not in permissions:
        raise Problem(403,'PERMISSION_DENIED','Missing permission: '+permission)
    sensitive=request.query_params.get('sensitive')=='true'
    if sensitive and not ({'onboarding:edit','document:review'} & set(permissions)):
        raise Problem(403,'PERMISSION_DENIED','Sensitive fields require edit or review permission')
    return {'id':aid,'tenant':tenant,'permissions':permissions,'expiresAt':expiry,'sensitive':sensitive}


async def create_session(request,tenant):
    claims=bearer(request)
    token,csrf=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
    expiry=min(datetime.now(timezone.utc)+timedelta(hours=8),datetime.fromtimestamp(claims['exp'],timezone.utc))
    async with store.transaction() as conn:
        permissions=await grants(conn,claims['sub'],tenant)
        await conn.execute('''INSERT INTO onboarding_sessions(token_hash,actor_id,tenant_id,csrf_hash,expires_at)
            VALUES($1,$2,$3,$4,$5)''',digest(token),store.uid(claims['sub']),store.uid(tenant),digest(csrf),expiry)
    return token,{'actor':{'id':claims['sub']},'tenant':tenant,'permissions':permissions,'expiresAt':expiry.isoformat(),'csrfToken':csrf}
