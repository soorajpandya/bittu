"""Onboarding contract regressions using isolated PostgreSQL, never production."""
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

from app.api.v1 import partner_onboarding as api
from app.services.partner_onboarding import assets, domain as d, security, store

A='11111111-1111-1111-1111-111111111111'
B='22222222-2222-2222-2222-222222222222'
T='33333333-3333-3333-3333-333333333333'
OTHER='44444444-4444-4444-4444-444444444444'


@pytest.fixture(scope='session')
def onboarding_postgres(tmp_path_factory):
    import pgserver
    server=pgserver.get_server(tmp_path_factory.mktemp('onboarding-pg'),cleanup_mode='delete')
    yield server.get_uri()
    server.cleanup()


@pytest.fixture
async def env(onboarding_postgres,monkeypatch,tmp_path):
    schema='ob_'+uuid4().hex
    bootstrap=await asyncpg.connect(onboarding_postgres)
    await bootstrap.execute(f'CREATE SCHEMA {schema}')
    pool=await asyncpg.create_pool(onboarding_postgres,min_size=1,max_size=8,server_settings={'search_path':schema})
    @asynccontextmanager
    async def connection():
        async with pool.acquire() as conn: yield conn
    monkeypatch.setattr(store,'get_service_connection',connection)
    monkeypatch.setenv('ONBOARDING_ENCRYPTION_KEYS',Fernet.generate_key().decode())
    monkeypatch.setenv('ONBOARDING_CLAMD_HOST','test-scanner')
    cfg=tmp_path/'contracts.json'
    cfg.write_text(json.dumps({'tenants':{T:{'version':'test-1','templateText':'Test agreement only.',
                                         'commercialTerms':{'currency':'INR','commissionBasisPoints':100}}}}))
    monkeypatch.setenv('ONBOARDING_CONTRACT_CONFIG',str(cfg))
    monkeypatch.setattr(security,'_decode_token',lambda token: {'sub':token,'exp':4102444800})
    async def clean(blob): return None
    monkeypatch.setattr(assets,'scan',clean)
    async with connection() as conn:
        await conn.execute(Path('migrations/001_initial_schema.sql').read_text().split('CREATE TABLE IF NOT EXISTS categories')[0])
        await conn.execute('''ALTER TABLE items ADD COLUMN pricing_type text DEFAULT 'mrp', ADD COLUMN gst_rate numeric DEFAULT 0,
          ADD COLUMN is_tax_inclusive bool, ADD COLUMN price_before_tax numeric, ADD COLUMN final_price numeric;
          CREATE FUNCTION fn_is_platform_admin(uuid) RETURNS boolean LANGUAGE sql AS 'SELECT true';''')
        migration=Path('migrations/089_partner_onboarding.sql').read_text()
        await conn.execute(migration)
        await conn.execute(migration)
        for actor in (A,B):
            await conn.execute('INSERT INTO onboarding_grants VALUES($1,$2,$3)',UUID(actor),UUID(T),d.PERMISSIONS)
        await conn.execute('INSERT INTO onboarding_grants VALUES($1,$2,$3)',UUID(A),UUID(OTHER),d.PERMISSIONS)
    app=FastAPI()
    app.include_router(api.router,prefix='/api/v1')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test') as client:
        yield client,connection
    await pool.close()
    await bootstrap.execute(f'DROP SCHEMA {schema} CASCADE')
    await bootstrap.close()


def headers(actor=A,tenant=T,revision=None,key=None):
    result={'Authorization':'Bearer '+actor,'X-Onboarding-Tenant':tenant}
    if revision is not None: result['If-Match']=f'"{revision}"'
    if key: result['Idempotency-Key']=key
    return result


async def create(client,**extra):
    r=await client.post('/api/v1/restaurants',headers=headers(key=str(uuid4())),json={'policyVersion':d.policy()['version'],**extra})
    assert r.status_code==201,r.text
    return r.json()


async def req(client,rid,path,method='POST',payload=None,actor=A,key=None,revision=None):
    if revision is None:
        r=await client.get(f'/api/v1/restaurants/{rid}/onboarding',headers=headers(actor))
        assert r.status_code==200,r.text
        revision=r.json()['revision']
    return await client.request(method,f'/api/v1/restaurants/{rid}/'+path,
        headers=headers(actor,revision=revision,key=key or str(uuid4())),json=payload or {})


async def upload(client,rid,purpose,mime,content):
    r=await req(client,rid,'uploads',payload={'purpose':purpose,'filename':'evidence','mimeType':mime,'bytes':len(content)})
    assert r.status_code==200,r.text
    init=r.json()
    r=await client.put(init['uploadUrl'],headers={**headers(),**init['headers']},content=content)
    assert r.status_code==200,r.text
    r=await req(client,rid,'uploads/'+init['uploadId']+'/complete')
    assert r.status_code==200 and r.json()['status']=='ready',r.text
    return init['uploadId']


async def test_draft_preconditions_idempotency_and_isolation(env):
    client,conn=env
    payload={'policyVersion':d.policy()['version'],'restaurant':{'name':'One'}}
    first=await client.post('/api/v1/restaurants',headers=headers(key='create'),json=payload)
    second=await client.post('/api/v1/restaurants',headers=headers(key='create'),json=payload)
    assert first.status_code==second.status_code==201
    assert first.json()==second.json()
    rid=first.json()['id']
    conflict=await client.post('/api/v1/restaurants',headers=headers(key='create'),json={**payload,'restaurant':{'name':'Two'}})
    assert conflict.status_code==409
    foreign=await client.get(f'/api/v1/restaurants/{rid}/onboarding',headers=headers(tenant=OTHER))
    assert foreign.status_code==404
    missing=await client.patch(f'/api/v1/restaurants/{rid}/onboarding',headers=headers(),json=payload)
    assert missing.status_code==428
    changed=await req(client,rid,'onboarding','PATCH',{'policyVersion':d.policy()['version'],'owner':{'fullName':'Owner'}})
    assert changed.status_code==200
    stale=await req(client,rid,'onboarding','PATCH',payload,revision=1)
    assert stale.status_code==412 and stale.json()['error']['revision']==2
    activate=await req(client,rid,'activate')
    assert activate.status_code==409
    async with conn() as c:
        assert await c.fetchval('SELECT is_active FROM restaurants WHERE id=$1',UUID(rid)) is False
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute('UPDATE restaurants SET is_active=true WHERE id=$1',UUID(rid))
        assert await c.fetchval('SELECT count(*) FROM onboarding_outbox')==2
        with pytest.raises(asyncpg.RaiseError):
            await c.execute('DELETE FROM onboarding_audit')


async def test_session_csrf_and_revocation(env):
    client,conn=env
    r=await client.post('/api/v1/admin/session',headers={'Authorization':'Bearer '+A},json={'tenantId':T})
    assert r.status_code==200,r.text
    assert 'HttpOnly' in r.headers['set-cookie'] and 'Secure' in r.headers['set-cookie']
    csrf=r.json()['csrfToken']
    assert (await client.get('/api/v1/admin/session')).status_code==200
    assert (await client.post('/api/v1/admin/logout')).status_code==403
    assert (await client.post('/api/v1/admin/logout',headers={'X-CSRF-Token':csrf})).status_code==200
    assert (await client.get('/api/v1/admin/session')).status_code==401


async def test_permission_denial(env):
    client,conn=env
    draft=await create(client)
    async with conn() as c:
        await c.execute('UPDATE onboarding_grants SET permissions=$3 WHERE actor_id=$1 AND tenant_id=$2',UUID(B),UUID(T),['restaurant:read'])
    r=await req(client,draft['id'],'onboarding','PATCH',{'policyVersion':d.policy()['version']},actor=B)
    assert r.status_code==403
    r=await client.get(f'/api/v1/restaurants/{draft["id"]}/onboarding?sensitive=true',headers=headers(B))
    assert r.status_code==403


@pytest.mark.parametrize('schedule',[
    {'monday':{'enabled':True,'slots':[{'opens':'10:00','closes':'10:00','closesNextDay':False}]}},
    {'monday':{'enabled':True,'slots':[{'opens':'12:00','closes':'10:00','closesNextDay':False}]}},
    {'sunday':{'enabled':True,'slots':[{'opens':'23:00','closes':'02:00','closesNextDay':True}]},
     'monday':{'enabled':True,'slots':[{'opens':'01:00','closes':'03:00','closesNextDay':False}]}},
])
def test_schedule_overlap(schedule):
    with pytest.raises(d.Problem): d.validate_draft({'operations':{'deliverySchedule':schedule}})


def test_validation_and_catalog():
    assert {'afghan','afghani'}<={c['id'] for c in d.CUISINES}
    with pytest.raises(d.Problem): d.validate_draft({'operations':{'cuisineIds':[c['id'] for c in d.CUISINES[:9]]}})
    with pytest.raises(d.Problem): d.validate_draft({'location':{'latitude':91}})
    with pytest.raises(d.Problem): d.validate_draft({'primaryContact':{'verification':{'status':'Verified'}}})
    data=d.validate_draft({'operations':{'deliveryEnabled':True,'diningEnabled':False}})
    ready=d.readiness(data,'Draft',{'delivery':1})
    assert not any('dining' in b['fieldPath'] for b in ready['blockers'])
    assert not any('food' in b['fieldPath'] for b in ready['blockers'])


async def test_import_atomicity_duplicates_and_customizations(env):
    client,conn=env
    rid=(await create(client))['id']
    aid=await upload(client,rid,'menu','text/csv',b'category,name,description,price,dietary,available\nMain,Rice,,12.50,vegan,true\nMain,rice,,12.50,veg,true\n')
    r=await req(client,rid,'menu-imports/preview',payload={'assetId':aid,'gstRate':0,'pricingType':'mrp'})
    assert r.status_code==200,r.text
    preview=r.json()
    assert preview['accepted']==1 and preview['skipped']==1
    async with conn() as c: assert await c.fetchval('SELECT count(*) FROM items')==0
    r=await req(client,rid,f'menu-imports/{preview["id"]}/confirm',payload={'previewHash':preview['previewHash'],'expectedMenuRevision':preview['revision']},key='commit')
    assert r.status_code==200,r.text
    committed=r.json()
    r=await req(client,rid,f'menu-imports/{preview["id"]}/confirm',payload={'previewHash':preview['previewHash'],'expectedMenuRevision':preview['revision']},key='commit',revision=preview['revision'])
    assert r.json()==committed
    iid=committed['itemIds'][0]
    r=await req(client,rid,f'menu-items/{iid}','PATCH',{'variants':[{'name':'Large','priceMinor':2500}],'addons':[{'name':'Sauce','priceMinor':100}]})
    assert r.status_code==200,r.text
    r=await req(client,rid,f'menu-items/{iid}','PATCH',{'description':'New'})
    assert len(r.json()['variants'])==len(r.json()['addons'])==1
    assert r.json()['priceMinor']==1250 and r.json()['dietary']=='vegan'
    r=await req(client,rid,f'menu-imports/{preview["id"]}','PATCH',{'mapping':{}})
    assert r.status_code==409


async def test_concurrent_same_revision_has_one_winner(env):
    client,_=env
    rid=(await create(client))['id']
    responses=await asyncio.gather(*[req(client,rid,'onboarding','PATCH',{'policyVersion':d.policy()['version'],'restaurant':{'name':name}},revision=1) for name in ('First','Second')])
    assert sorted(r.status_code for r in responses)==[200,412]


async def test_full_review_activation_and_document_invalidation(env):
    import io
    from PIL import Image
    client,conn=env
    draft=await create(client,restaurant={'name':'Test cafe'},owner={'fullName':'Owner','phone':'+919876543210'},
        primaryContact={'sameAsOwner':True},location={'building':'1','street':'Road','area':'Area','city':'Mumbai','state':'Maharashtra','postalCode':'400001','latitude':19,'longitude':72},
        operations={'deliveryEnabled':True,'cuisineIds':['afghan'],'deliverySchedule':{'monday':{'enabled':True,'slots':[{'opens':'09:00','closes':'22:00'}]}}})
    rid=draft['id']
    buf=io.BytesIO(); Image.new('RGB',(2,2)).save(buf,format='PNG')
    media={}
    for purpose in ('profile','entrance','interior'):
        aid=await upload(client,rid,purpose,'image/png',buf.getvalue())
        media[purpose+('AssetId' if purpose=='profile' else 'AssetIds')]=aid if purpose=='profile' else [aid]
    assert (await req(client,rid,'onboarding','PATCH',{'policyVersion':d.policy()['version'],'media':media})).status_code==200
    aid=await upload(client,rid,'menu','text/csv',b'category,name,price,dietary,available\nMain,Rice,10,veg,true\n')
    preview=(await req(client,rid,'menu-imports/preview',payload={'assetId':aid,'gstRate':0,'pricingType':'mrp'})).json()
    assert (await req(client,rid,f'menu-imports/{preview["id"]}/confirm',payload={'previewHash':preview['previewHash'],'expectedMenuRevision':preview['revision']})).status_code==200
    for kind,fields in {'PAN':{'legalName':'Owner','number':'ABCDE1234F'},'FSSAI':{'number':'12345678901234','businessName':'Cafe','expiry':'2099-01-01'},'bank':{'holder':'Owner','accountNumber':'1234567890','ifsc':'ABCD0123456','bankName':'Bank','branch':'Main'}}.items():
        aid=await upload(client,rid,kind,'image/png',buf.getvalue())
        r=await req(client,rid,f'documents/{kind}','PUT',{'assetId':aid,**fields})
        assert r.status_code==200,r.text
        assert (await req(client,rid,f'documents/{kind}/submit')).status_code==200
        assert (await req(client,rid,f'documents/{kind}/decisions',payload={'decision':'approve'})).status_code==403
        r=await req(client,rid,f'documents/{kind}/decisions',payload={'decision':'approve'},actor=B)
        assert r.status_code==200,r.text
    masked=await client.get(f'/api/v1/restaurants/{rid}/documents',headers=headers())
    assert 'ABCDE1234F' not in masked.text and '1234567890' not in masked.text
    prepared=await req(client,rid,'contracts')
    assert prepared.status_code==200,prepared.text
    cid=prepared.json()['id']
    signed=await upload(client,rid,'signed-agreement','image/png',buf.getvalue())
    assert (await req(client,rid,f'contracts/{cid}/signed-copy',payload={'assetId':signed,'signatoryName':'Owner','role':'Owner'})).status_code==200
    r=await req(client,rid,f'contracts/{cid}/signed-copy/approve',actor=B)
    assert r.status_code==200,r.text
    assert (await req(client,rid,'submit')).status_code==200
    assert (await req(client,rid,'review/start',actor=B)).status_code==200
    r=await req(client,rid,'review/decisions',payload={'decision':'request_changes','reason':'Correct details','fieldPaths':['owner.fullName']},actor=B)
    assert r.status_code==200,r.text
    # Changing approved banking details resets its approval and supersedes the contract.
    bank=next(x for x in r.json()['documents'] if x['kind']=='bank')
    r=await req(client,rid,'documents/bank','PUT',{'assetId':bank['assetId'],'holder':'Owner','accountNumber':'9876543210','ifsc':'ABCD0123456','bankName':'Bank','branch':'Main'})
    assert r.status_code==200 and r.json()['contract']['status']=='Superseded'
    assert (await req(client,rid,'documents/bank/submit')).status_code==200
    assert (await req(client,rid,'documents/bank/decisions',payload={'decision':'approve'},actor=B)).status_code==200
    cid=(await req(client,rid,'contracts')).json()['id']
    assert (await req(client,rid,f'contracts/{cid}/signed-copy',payload={'assetId':signed,'signatoryName':'Owner','role':'Owner'})).status_code==200
    assert (await req(client,rid,f'contracts/{cid}/signed-copy/approve',actor=B)).status_code==200
    assert (await req(client,rid,'submit')).status_code==200
    assert (await req(client,rid,'review/start',actor=B)).status_code==200
    assert (await req(client,rid,'approve',actor=B)).status_code==200
    r=await req(client,rid,'activate',actor=B)
    assert r.status_code==200 and r.json()['status']=='Active',r.text
    async with conn() as c:
        assert await c.fetchval('SELECT is_active FROM restaurants WHERE id=$1',UUID(rid))
        assert await c.fetchval('SELECT count(*) FROM onboarding_snapshots WHERE restaurant_id=$1',UUID(rid))==2
    r=await req(client,rid,'onboarding','PATCH',{'policyVersion':d.policy()['version'],'restaurant':{'name':'Edit active'}})
    assert r.status_code==409


async def test_upload_validation_scan_retry_and_download_expiry(env,monkeypatch):
    client,_=env
    rid=(await create(client))['id']
    init=(await req(client,rid,'uploads',payload={'purpose':'menu','filename':'x.csv','mimeType':'text/csv','bytes':3})).json()
    r=await client.put(init['uploadUrl'],headers={**headers(),**init['headers']},content=b'1234')
    assert r.status_code==413
    r=await client.put(init['uploadUrl'],headers={**headers(),**init['headers']},content=b'123')
    assert r.status_code==200
    async def unavailable(blob): raise d.Problem(503,'DEPENDENCY_UNAVAILABLE','scanner unavailable')
    monkeypatch.setattr(assets,'scan',unavailable)
    r=await req(client,rid,'uploads/'+init['uploadId']+'/complete')
    assert r.json()['status']=='failed' and r.json()['retryable']
    assert (await req(client,rid,'uploads/'+init['uploadId']+'/retry')).json()['status']=='processing'
    async def clean(blob): return None
    monkeypatch.setattr(assets,'scan',clean)
    assert (await req(client,rid,'uploads/'+init['uploadId']+'/complete')).json()['status']=='ready'
    r=await req(client,rid,'assets/'+init['uploadId']+'/download')
    link=r.json()
    assert (await client.get(link['url'],headers={**headers(),**link['headers']})).content==b'123'
    assert (await client.get(link['url'],headers={**headers(B),**link['headers']})).status_code==403
    assert (await client.get(link['url'],headers=headers())).status_code==403


def test_money_exact():
    from app.services.partner_onboarding.menus import minor
    assert minor('0.29')==29
    for bad in ('NaN','Infinity','1.001','-1'):
        with pytest.raises(d.Problem): minor(bad)


def test_signature_exemption_is_narrow():
    from starlette.requests import Request
    from app.services.partner_onboarding.paths import is_onboarding_request
    def req(path,method='GET'):
        return Request({'type':'http','path':path,'method':method,'headers':[(b'x-onboarding-tenant',T.encode())]})
    assert is_onboarding_request(req('/api/v1/restaurants'))
    assert is_onboarding_request(req('/api/v1/restaurants/'+T+'/onboarding','PATCH'))
    assert not is_onboarding_request(req('/api/v1/restaurants/'+T,'PATCH'))
    assert not is_onboarding_request(req('/api/v1/items','POST'))
