"""Bounded encrypted uploads in PostgreSQL, with fail-closed ClamAV scanning."""
import asyncio
import hashlib
import io
import os
import secrets
import struct
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from PIL import Image
from pydantic import Field

from . import store
from .domain import Strict, Problem, policy, validate, fail


class Upload(Strict):
    purpose: str
    filename: str = Field(min_length=1,max_length=255)
    mimeType: str
    bytes: int = Field(gt=0)
    sha256: str | None = Field(None,pattern=r'^[a-fA-F0-9]{64}$')


def now():
    return datetime.now(timezone.utc)


def transfer(meta,rid):
    token=secrets.token_urlsafe(32)
    meta['tokenHash']=hashlib.sha256(token.encode()).hexdigest()
    meta['expiresAt']=(now()+timedelta(minutes=15)).isoformat()
    return {'uploadId':meta['id'],'uploadUrl':f'/api/v1/restaurants/{rid}/uploads/{meta["id"]}/content',
            'method':'PUT','headers':{'Content-Type':meta['mimeType'],'X-Upload-Token':token},'expiresAt':meta['expiresAt']}


async def initiate(conn,rid,body):
    p=policy()
    if not p['features']['uploads']:
        raise Problem(503,'DEPENDENCY_UNAVAILABLE','Configure ClamAV before uploading evidence')
    data=validate(Upload,body)
    if data['purpose'] not in {'entrance','interior','profile','food','menu','PAN','FSSAI','bank','signed-agreement'}:
        fail('Unsupported purpose','purpose')
    image=data['purpose'] in {'entrance','interior','profile','food'}
    allowed=['image/jpeg','image/png','image/webp'] if image else p['mimeTypes'][:-1]
    if data['purpose']=='menu':
        allowed=p['mimeTypes']
    if data['mimeType'] not in allowed:
        raise Problem(415,'UNSUPPORTED_TYPE','Unsupported MIME type for purpose')
    maximum=p['imageBytes'] if image else p['documentBytes']
    if data['mimeType']=='text/csv':
        maximum=min(maximum,2*1024*1024)
    if data['bytes']>maximum:
        raise Problem(413,'SIZE_LIMIT','File exceeds upload limit')
    resources=await conn.fetch("SELECT payload FROM onboarding_resources WHERE restaurant_id=$1 AND kind='asset'",store.uid(rid))
    if sum(store.unseal(r['payload'])['bytes'] for r in resources)+data['bytes']>p['restaurantBytes']:
        raise Problem(413,'QUOTA_EXCEEDED','Restaurant retained-file quota exceeded')
    data.update(id=str(uuid4()),status='pending',detached=False,createdAt=now().isoformat())
    result=transfer(data,rid)
    await store.put_resource(conn,rid,data['id'],'asset',data)
    return result


def safe_metadata(meta):
    return {k:v for k,v in meta.items() if k not in {'tokenHash','downloadHash'}}


def sniff(blob,mime):
    if blob[:2]==b'MZ' or blob[:4]==b'\x7fELF':
        raise Problem(415,'UNSUPPORTED_TYPE','Executable content is forbidden')
    if mime.startswith('image/'):
        try:
            with Image.open(io.BytesIO(blob)) as img:
                expected={'image/jpeg':'JPEG','image/png':'PNG','image/webp':'WEBP'}[mime]
                if img.format!=expected or img.width*img.height>40_000_000:
                    raise ValueError()
                img.verify()
        except Exception:
            raise Problem(415,'UNSUPPORTED_TYPE','Image bytes do not match MIME type or dimensions exceed limit') from None
    elif mime=='application/pdf':
        if not blob.startswith(b'%PDF-') or b'%%EOF' not in blob[-4096:]:
            raise Problem(415,'UNSUPPORTED_TYPE','Invalid PDF')
    elif mime=='text/csv':
        try:
            text=blob.decode('utf-8-sig')
            if '\x00' in text or '<script' in text.lower() or text.lstrip().startswith('<'):
                raise ValueError()
        except (ValueError,UnicodeError):
            raise Problem(415,'UNSUPPORTED_TYPE','CSV must be UTF-8 text') from None


async def scan(blob):
    host=os.getenv('ONBOARDING_CLAMD_HOST')
    if not host:
        raise Problem(503,'DEPENDENCY_UNAVAILABLE','Malware scanner is not configured')
    async def run():
        reader,writer=await asyncio.open_connection(host,int(os.getenv('ONBOARDING_CLAMD_PORT','3310')))
        try:
            writer.write(b'zINSTREAM\x00')
            for offset in range(0,len(blob),65536):
                chunk=blob[offset:offset+65536]
                writer.write(struct.pack('!I',len(chunk))+chunk)
                await writer.drain()
            writer.write(struct.pack('!I',0))
            await writer.drain()
            answer=await reader.readuntil(b'\x00')
            if answer.rstrip(b'\x00\n')!=b'stream: OK':
                raise Problem(422,'SCAN_REJECTED','File failed malware scanning')
        finally:
            writer.close()
            await writer.wait_closed()
    try:
        await asyncio.wait_for(run(),timeout=30)
    except Problem:
        raise
    except Exception:
        raise Problem(503,'DEPENDENCY_UNAVAILABLE','Malware scanner unavailable; retry completion') from None


async def receive(conn,rid,upload_id,request):
    meta,_=await store.resource(conn,rid,upload_id,'asset')
    token=request.headers.get('x-upload-token','')
    if not secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(),meta.get('tokenHash','')):
        raise Problem(403,'PERMISSION_DENIED','Invalid upload token')
    if now()>datetime.fromisoformat(meta['expiresAt']):
        raise Problem(409,'UPLOAD_EXPIRED','Retry upload to obtain a fresh transfer token')
    if meta['status'] not in {'pending','uploading'}:
        raise Problem(409,'INVALID_TRANSITION','Upload already transferred')
    if request.headers.get('content-type','').split(';')[0]!=meta['mimeType']:
        raise Problem(415,'UNSUPPORTED_TYPE','Content-Type does not match upload')
    body=bytearray()
    async for chunk in request.stream():
        if len(body)+len(chunk)>meta['bytes']:
            raise Problem(413,'SIZE_LIMIT','Transferred bytes exceed declared size')
        body.extend(chunk)
    if len(body)!=meta['bytes']:
        fail('Transferred size does not match declared size','bytes')
    blob=bytes(body)
    checksum=hashlib.sha256(blob).hexdigest()
    if meta.get('sha256') and meta['sha256'].lower()!=checksum:
        fail('Checksum mismatch','sha256')
    sniff(blob,meta['mimeType'])
    meta.update(status='processing',sha256=checksum)
    meta.pop('tokenHash',None)
    await store.put_resource(conn,rid,upload_id,'asset',meta,store.cipher().encrypt(blob))
    return safe_metadata(meta)


async def complete(conn,rid,upload_id):
    meta,binary=await store.resource(conn,rid,upload_id,'asset')
    if meta['status']=='ready':
        return safe_metadata(meta)
    if not binary or meta['status'] not in {'processing','failed'}:
        raise Problem(409,'INVALID_TRANSITION','Transfer content before completing upload')
    try:
        blob=store.cipher().decrypt(bytes(binary))
        sniff(blob,meta['mimeType'])
        await scan(blob)
        meta.update(status='ready',scannedAt=now().isoformat(),retryable=False)
        meta.pop('error',None)
    except Problem as exc:
        meta.update(status='failed',error={'code':exc.code,'message':exc.message},retryable=exc.status==503)
    await store.put_resource(conn,rid,upload_id,'asset',meta)
    return safe_metadata(meta)


async def ready_asset(conn,rid,aid,purpose=None):
    meta,binary=await store.resource(conn,rid,aid,'asset')
    if meta['status']!='ready' or meta.get('detached') or (purpose and meta['purpose']!=purpose):
        fail('A ready, attached asset with the correct purpose is required','assetId')
    return meta,binary


async def validate_references(conn,rid,data):
    media=data['media']
    refs=[(media['profileAssetId'],'profile')] if media.get('profileAssetId') else []
    for purpose in ('entrance','interior'):
        refs.extend((a,purpose) for a in media[purpose+'AssetIds'])
    refs.extend((f['assetId'],'food') for f in media['food'])
    for service in ('delivery','dining'):
        refs.extend((a,'menu') for a in data['operations'][service+'MenuAssetIds'])
    for aid,purpose in refs:
        await ready_asset(conn,rid,aid,purpose)
