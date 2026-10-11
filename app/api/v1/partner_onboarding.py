"""Admin onboarding REST contract. Separate from subscription onboarding."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.routing import APIRoute
from starlette.responses import JSONResponse, Response

from app.services.partner_onboarding import assets, contracts, domain as d, menus, security, store


class OnboardingRoute(APIRoute):
    def get_route_handler(self):
        handler=super().get_route_handler()
        async def wrapped(request):
            try:
                response=await handler(request)
            except d.Problem as exc:
                error={'code':exc.code,'message':exc.message,'fields':exc.fields,
                       'requestId':getattr(request.state,'request_id',str(uuid4()))}
                if exc.revision is not None:
                    error['revision']=exc.revision
                response=JSONResponse({'error':error},status_code=exc.status)
                if exc.revision is not None:
                    response.headers['ETag']=f'"{exc.revision}"'
            response.headers['Cache-Control']='no-store'
            response.headers['X-Content-Type-Options']='nosniff'
            return response
        return wrapped


router=APIRouter(tags=['Admin restaurant onboarding'],route_class=OnboardingRoute)


async def body(request):
    raw=bytearray()
    async for chunk in request.stream():
        if len(raw)+len(chunk)>2*1024*1024:
            raise d.Problem(413,'SIZE_LIMIT','JSON body exceeds 2 MB')
        raw.extend(chunk)
    if not raw:
        return {}
    try:
        value=json.loads(raw)
        if not isinstance(value,dict):
            raise ValueError()
        return value
    except (ValueError,UnicodeError):
        d.fail('Expected a JSON object')


def response(value,revision=None,status=200):
    result=JSONResponse(jsonable_encoder(value),status_code=status)
    if revision is not None:
        result.headers['ETag']=f'"{revision}"'
    return result


def editable(row):
    if row['status'] not in d.EDITABLE:
        raise d.Problem(409,'INVALID_TRANSITION','Only Draft or Changes requested records can be edited')


def page(request):
    try:
        limit=int(request.query_params.get('limit','50'))
        if not 1<=limit<=100:
            raise ValueError()
        return limit,store.cursor_decode(request.query_params.get('cursor'))
    except ValueError:
        d.fail('Page limit must be 1–100')


@router.post('/admin/session')
async def start_session(request:Request):
    payload=await body(request)
    if set(payload)!={'tenantId'}:
        d.fail('Supply tenantId')
    token,value=await security.create_session(request,payload['tenantId'])
    result=response(value)
    result.set_cookie(security.COOKIE,token,httponly=True,secure=True,samesite='strict',path='/api/v1',max_age=28800)
    return result


@router.get('/admin/session')
async def session(request:Request):
    actor=await security.actor(request)
    return response({'actor':{'id':actor['id']},'tenant':actor['tenant'],'permissions':actor['permissions'],'expiresAt':actor['expiresAt']})


@router.post('/admin/logout')
async def logout(request:Request):
    await security.actor(request)
    token=request.cookies.get(security.COOKIE)
    if not token:
        raise d.Problem(409,'NO_COOKIE_SESSION','Use /auth/logout for bearer sessions')
    async with store.transaction() as conn:
        await conn.execute('DELETE FROM onboarding_sessions WHERE token_hash=$1',security.digest(token))
    result=response({'loggedOut':True})
    result.delete_cookie(security.COOKIE,path='/api/v1',secure=True,httponly=True,samesite='strict')
    return result


@router.get('/onboarding/policy')
async def get_policy(request:Request):
    await security.actor(request)
    if request.query_params.get('country','IN')!='IN':
        d.fail('Only India is configured','country')
    return response(d.policy())


@router.get('/catalogs/cuisines')
async def cuisines(request:Request):
    await security.actor(request)
    return response({'items':d.CUISINES,'nextCursor':None,'maxSelection':8})


@router.get('/catalogs/locations')
async def locations(request:Request):
    await security.actor(request)
    if request.query_params.get('country','IN')!='IN':
        d.fail('Only India is configured','country')
    return response({'country':'IN','postalCodePattern':'^[1-9][0-9]{5}$','states':[], 'cities':[],
                     'freeTextStateCity':True,'requiredFields':d.policy()['requiredFields'],'nextCursor':None})


@router.get('/locations/search')
@router.get('/locations/reverse')
async def geocoding(request:Request):
    await security.actor(request)
    raise d.Problem(503,'DEPENDENCY_UNAVAILABLE','Geocoding provider is not configured')


async def legacy_user(request):
    from fastapi.security import HTTPAuthorizationCredentials
    from app.core.auth import get_current_user
    value=request.headers.get('authorization','')
    if not value.startswith('Bearer '):
        raise d.Problem(401,'UNAUTHENTICATED','Authentication required')
    return await get_current_user(HTTPAuthorizationCredentials(scheme='Bearer',credentials=value[7:]))


def managed_request(request):
    return bool(request.cookies.get(security.COOKIE) or request.headers.get('x-onboarding-tenant'))


@router.post('/restaurants',status_code=201)
async def create_restaurant(request:Request):
    payload=await body(request)
    if not managed_request(request):
        # Preserve the existing merchant setup contract. New admin clients always use a session or tenant header.
        from app.api.v1.restaurants import create_restaurant as legacy,CreateRestaurantIn
        return await legacy(CreateRestaurantIn(**payload),await legacy_user(request))
    actor=await security.actor(request,'onboarding:edit')
    if set(payload)-{'restaurant','owner','primaryContact','location','media','operations','policyVersion'}:
        d.fail('Unknown create fields')
    if payload.get('policyVersion')!=d.policy()['version']:
        raise d.Problem(409,'POLICY_CHANGED','Fetch current onboarding policy')
    data=d.validate_draft({k:v for k,v in payload.items() if k!='policyVersion'})
    async with store.transaction() as conn:
        key,replay=await store.idempotency(conn,request,actor,payload)
        if replay:
            return response(*[replay[0],replay[2],replay[1]])
        rid=uuid4()
        await assets.validate_references(conn,rid,data)
        await conn.execute('''INSERT INTO restaurants(id,owner_id,name,is_active) VALUES($1,$2,$3,false)''',rid,actor['tenant'],data['restaurant']['name'] or 'Untitled draft')
        await conn.execute('''INSERT INTO sub_branches(id,restaurant_id,owner_id,name,is_main_branch,is_active)
                             VALUES($1,$2,$3,'Main',true,false)''',uuid4(),rid,actor['tenant'])
        data.update(documents={},contract=None)
        row=await conn.fetchrow('''INSERT INTO onboarding_records(restaurant_id,tenant_id,policy_version,payload,updated_by)
            VALUES($1,$2,$3,$4,$5) RETURNING *''',rid,store.uid(actor['tenant']),d.policy()['version'],store.seal(data),store.uid(actor['id']))
        await store.audit(conn,row,actor,'created')
        value=await store.represent(conn,row,data,actor)
        await store.remember(conn,actor,key,value,201,row['revision'])
        return response(value,row['revision'],201)


@router.get('/restaurants')
async def list_restaurants(request:Request):
    if not managed_request(request):
        from app.api.v1.restaurants import get_restaurants
        return await get_restaurants(await legacy_user(request))
    actor=await security.actor(request)
    limit,cursor=page(request)
    status=request.query_params.get('status')
    search=request.query_params.get('search','')[:200]
    async with store.transaction() as conn:
        rows=await conn.fetch('''SELECT o.*,r.name,r.city FROM onboarding_records o JOIN restaurants r ON r.id=o.restaurant_id
          WHERE o.tenant_id=$1 AND ($2::text IS NULL OR o.status=$2) AND r.name ILIKE $3
          AND ($4::uuid IS NULL OR o.restaurant_id>$4) ORDER BY o.restaurant_id LIMIT $5''',store.uid(actor['tenant']),status,
          '%'+search+'%',store.uid(cursor) if cursor else None,limit+1)
    return response({'items':[{'id':str(r['restaurant_id']),'name':r['name'],'city':r['city'],'status':r['status'],'revision':r['revision']} for r in rows[:limit]],
                     'nextCursor':store.cursor_encode(rows[limit-1]['restaurant_id']) if len(rows)>limit else None})


@router.get('/restaurants/duplicates')
async def duplicates(request:Request):
    actor=await security.actor(request)
    name=request.query_params.get('name','').strip()
    city=request.query_params.get('city','').strip()
    outlet=request.query_params.get('outletCode','').strip()
    async with store.transaction() as conn:
        rows=await conn.fetch('''SELECT r.id,r.name,r.city,o.payload FROM restaurants r
          LEFT JOIN onboarding_records o ON o.restaurant_id=r.id WHERE r.owner_id=$1
          AND ($2='' OR lower(r.name)=lower($2)) ORDER BY r.id LIMIT 100''',actor['tenant'],name)
        results=[]
        for row in rows:
            reasons=[]
            if name and row['name'].casefold()==name.casefold(): reasons.append('same_name')
            if city and (row['city'] or '').casefold()==city.casefold(): reasons.append('same_city')
            if outlet and row['payload'] and store.unseal(row['payload'])['restaurant'].get('outletCode')==outlet: reasons.append('same_outlet_code')
            if reasons and (name or 'same_outlet_code' in reasons):
                results.append({'id':str(row['id']),'name':row['name'],'city':row['city'],'reasons':reasons})
    return response({'items':results,'nextCursor':None,'blocksCreation':False})


@router.get('/restaurants/{rid}/onboarding')
@router.get('/restaurants/{rid}/readiness')
async def get_record(rid:str,request:Request):
    actor=await security.actor(request)
    async with store.transaction() as conn:
        row,data=await store.record(conn,rid,actor)
        value=await store.represent(conn,row,data,actor)
    if request.url.path.endswith('/readiness'):
        value={k:value[k] for k in ('id','revision','policyVersion','readiness','progress','allowedActions')}
    return response(value,row['revision'])


async def mutation(request,rid,permission,operation,handler,*,idempotent=False,edit=False):
    actor=await security.actor(request,permission)
    payload=await body(request)
    async with store.transaction() as conn:
        # Access is rechecked even for cached idempotent responses.
        row,data=await store.record(conn,rid,actor,lock=True)
        key=None
        if idempotent:
            key,replay=await store.idempotency(conn,request,actor,payload)
            if replay:
                return response(replay[0],replay[2],replay[1])
        store.precondition(request,row)
        if edit:
            editable(row)
        result,new_status=await handler(conn,row,data,actor,payload)
        row=await store.save(conn,row,data,actor,operation,{'fieldPaths':list(payload)},new_status)
        value=await store.represent(conn,row,data,actor) if result is None else result
        if isinstance(value,dict):
            value.update(revision=row['revision'],updatedAt=row['updated_at'].isoformat(),updatedBy=actor['id'])
        if key:
            await store.remember(conn,actor,key,value,200,row['revision'])
        return response(value,row['revision'])


@router.patch('/restaurants/{rid}/onboarding')
async def patch_record(rid:str,request:Request):
    async def run(conn,row,data,actor,payload):
        if payload.get('policyVersion')!=d.policy()['version']:
            raise d.Problem(409,'POLICY_CHANGED','Fetch current onboarding policy')
        allowed=set(d.Draft.model_fields)
        if set(payload)-allowed-{'policyVersion'}:
            d.fail('Only onboarding fields and policyVersion can be patched')
        old={k:data[k] for k in allowed}
        updated=d.validate_draft(d.merge(old,{k:v for k,v in payload.items() if k!='policyVersion'}))
        await assets.validate_references(conn,rid,updated)
        if updated['restaurant']!=old['restaurant'] or updated['owner']!=old['owner']:
            await invalidate_contract(conn,rid,data,'Legal/contact details changed')
        data.update(updated)
        loc=data['location']
        await conn.execute('''UPDATE restaurants SET name=$2,phone=$3,email=$4,address=$5,city=$6,state=$7,pincode=$8,
            latitude=$9,longitude=$10,updated_at=now() WHERE id=$1''',store.uid(rid),data['restaurant']['name'] or 'Untitled draft',
            data['primaryContact']['phone'],data['owner']['email'],' '.join(loc[k] for k in ('building','street','area')),
            loc['city'],loc['state'],loc['postalCode'],loc['latitude'],loc['longitude'])
        return None,None
    return await mutation(request,rid,'onboarding:edit','draft.updated',run,edit=True)


async def invalidate_contract(conn,rid,data,reason):
    contract=data.get('contract')
    if contract and contract['status']!='Superseded':
        contract.update(status='Superseded',reason=reason,supersededAt=assets.now().isoformat())
        await store.put_resource(conn,rid,contract['id'],'contract',contract)


def workflow(operation,permission):
    async def endpoint(rid:str,request:Request):
        async def run(conn,row,data,actor,payload):
            state=row['status']
            ready=d.readiness(data,state,await store.counts(conn,rid))
            if operation=='submit':
                if not ready['canSubmit']:
                    raise d.Problem(409,'NOT_READY','Submission prerequisites are not satisfied',ready['blockers'])
                await assets.validate_references(conn,rid,data)
                await conn.execute('INSERT INTO onboarding_snapshots(id,restaurant_id,revision,payload) VALUES($1,$2,$3,$4)',uuid4(),store.uid(rid),row['revision'],store.seal(data))
                data['submittedBy']=actor['id']
                data['submittedAt']=assets.now().isoformat()
                return None,'Submitted'
            if operation=='review/start':
                if state!='Submitted': raise d.Problem(409,'INVALID_TRANSITION','Expected Submitted')
                return None,'Under review'
            if operation=='review/decisions':
                if state!='Under review' or payload.get('decision') not in ('request_changes','reject'):
                    raise d.Problem(409,'INVALID_TRANSITION','Expected Under review and request_changes/reject')
                reason=payload.get('reason')
                if not isinstance(reason,str) or not reason.strip() or len(reason)>2000:
                    d.fail('A reason is required','reason')
                paths=payload.get('fieldPaths',[])
                if not isinstance(paths,list) or len(paths)>100 or any(not isinstance(x,str) or len(x)>200 for x in paths):
                    d.fail('fieldPaths must be a list of field paths','fieldPaths')
                event={'decision':payload['decision'],'reason':reason,'fieldPaths':paths,'actorId':actor['id'],'at':assets.now().isoformat()}
                await store.put_resource(conn,rid,str(uuid4()),'review',event)
                data['latestReview']=event
                return None,'Changes requested' if payload['decision']=='request_changes' else 'Rejected'
            if operation in ('approve','activate'):
                if not ready['canApprove' if operation=='approve' else 'canActivate']:
                    raise d.Problem(409,'NOT_READY','Review, contract or activation prerequisites are not satisfied',ready['blockers'])
                if operation=='approve' and data.get('submittedBy')==actor['id']:
                    raise d.Problem(403,'SEPARATION_OF_DUTIES','Submission must be approved by another reviewer')
                await assets.validate_references(conn,rid,data)
                for kind,doc in data['documents'].items():
                    d.document_valid(kind,{k:v for k,v in doc.items() if k in d.Document.model_fields})
                    await assets.ready_asset(conn,rid,doc['assetId'],kind)
                contract=data.get('contract')
                if contract and contract['configurationHash']!=d.fingerprint(contracts.config(actor['tenant'])):
                    raise d.Problem(409,'CONTRACT_CHANGED','Configured terms changed; prepare a new contract')
                if operation=='activate':
                    await conn.execute('UPDATE restaurants SET is_active=true,updated_at=now() WHERE id=$1',store.uid(rid))
                    await conn.execute('UPDATE sub_branches SET is_active=true WHERE restaurant_id=$1',store.uid(rid))
                    # Dining can explicitly reuse delivery menu without flattening its customizations.
                    if data['operations']['useDeliveryMenuForDining'] and data['operations']['diningEnabled']:
                        await conn.execute('UPDATE items SET dine_in_available=true WHERE restaurant_id=$1 AND delivery_available=true',store.uid(rid))
                return None,'Approved' if operation=='approve' else 'Active'
            raise d.Problem(404,'NOT_FOUND','Unknown action')
        return await mutation(request,rid,permission,operation,run,idempotent=True)
    endpoint.__name__='onboarding_'+operation.replace('/','_')
    return endpoint


for _operation,_permission in [('submit','onboarding:submit'),('review/start','document:review'),
    ('review/decisions','onboarding:approve'),('approve','onboarding:approve'),('activate','restaurant:activate')]:
    router.add_api_route('/restaurants/{rid}/'+_operation,workflow(_operation,_permission),methods=['POST'])


@router.get('/restaurants/{rid}/audit')
async def audit(rid:str,request:Request):
    actor=await security.actor(request,'audit:read')
    limit,cursor=page(request)
    try: after=int(cursor) if cursor else 0
    except ValueError: d.fail('Invalid cursor')
    async with store.transaction() as conn:
        row,_=await store.record(conn,rid,actor)
        rows=await conn.fetch('SELECT * FROM onboarding_audit WHERE restaurant_id=$1 AND id>$2 ORDER BY id LIMIT $3',store.uid(rid),after,limit+1)
    return response({'items':[dict(r) for r in rows[:limit]],'nextCursor':store.cursor_encode(rows[limit-1]['id']) if len(rows)>limit else None},row['revision'])

@router.post('/restaurants/{rid}/uploads')
async def initiate_upload(rid:str,request:Request):
    async def run(conn,row,data,actor,payload):
        return await assets.initiate(conn,rid,payload),None
    return await mutation(request,rid,'onboarding:edit','upload.initiated',run,idempotent=True,edit=True)


@router.put('/restaurants/{rid}/uploads/{upload_id}/content')
async def upload_content(rid:str,upload_id:str,request:Request):
    actor=await security.actor(request,'onboarding:edit')
    async with store.transaction() as conn:
        row,data=await store.record(conn,rid,actor,lock=True)
        editable(row)
        value=await assets.receive(conn,rid,upload_id,request)
    # Binary transfer only changes processing state; completion increments the aggregate revision.
    return response(value,row['revision'])


@router.get('/restaurants/{rid}/uploads/{upload_id}')
async def upload_status(rid:str,upload_id:str,request:Request):
    actor=await security.actor(request,'onboarding:edit')
    async with store.transaction() as conn:
        row,_=await store.record(conn,rid,actor)
        meta,_=await store.resource(conn,rid,upload_id,'asset')
    return response(assets.safe_metadata(meta),row['revision'])


def upload_action(op):
    async def endpoint(rid:str,upload_id:str,request:Request):
        async def run(conn,row,data,actor,payload):
            if op=='complete':
                return await assets.complete(conn,rid,upload_id),None
            meta,binary=await store.resource(conn,rid,upload_id,'asset')
            if meta['status']=='ready' or (meta['status']=='failed' and not meta.get('retryable')):
                raise d.Problem(409,'INVALID_TRANSITION','Upload cannot be retried; initiate a new upload')
            if binary:
                meta['status']='processing'
                await store.put_resource(conn,rid,upload_id,'asset',meta)
                return assets.safe_metadata(meta),None
            meta['status']='pending'
            result=assets.transfer(meta,rid)
            await store.put_resource(conn,rid,upload_id,'asset',meta)
            return result,None
        return await mutation(request,rid,'onboarding:edit','upload.'+op,run,edit=True)
    endpoint.__name__='upload_'+op
    return endpoint


for _op in ('complete','retry'):
    router.add_api_route('/restaurants/{rid}/uploads/{upload_id}/'+_op,upload_action(_op),methods=['POST'])


@router.patch('/restaurants/{rid}/assets/{asset_id}')
async def patch_asset(rid:str,asset_id:str,request:Request):
    async def run(conn,row,data,actor,payload):
        meta,_=await assets.ready_asset(conn,rid,asset_id)
        if set(payload)-{'caption','category','crop','order'}:
            d.fail('Unknown asset metadata')
        if 'caption' in payload and (not isinstance(payload['caption'],str) or len(payload['caption'])>500):
            d.fail('Caption must be at most 500 characters')
        if 'order' in payload and (type(payload['order']) is not int or payload['order']<0):
            d.fail('Order must be a nonnegative integer')
        if 'category' in payload:
            if meta['purpose'] not in {'entrance','interior','profile','food'} or payload['category'] not in {'entrance','interior','profile','food'}:
                d.fail('Only images can change category')
            # Require detaching old references first, so required category checks stay accurate.
            if asset_id in json.dumps(data):
                raise d.Problem(409,'ASSET_REFERENCED','Detach asset references in onboarding PATCH before changing category')
            meta['purpose']=payload['category']
        if 'crop' in payload:
            crop=payload['crop']
            if not isinstance(crop,dict) or set(crop)!={'x','y','width','height'} or any(type(x) not in (int,float) or not 0<=x<=1 for x in crop.values()) or crop['width']==0 or crop['height']==0 or crop['x']+crop['width']>1 or crop['y']+crop['height']>1:
                d.fail('Crop uses normalized x/y/width/height within the image')
        meta.update({k:v for k,v in payload.items() if k!='category'})
        await store.put_resource(conn,rid,asset_id,'asset',meta)
        return assets.safe_metadata(meta),None
    return await mutation(request,rid,'onboarding:edit','asset.updated',run,edit=True)


@router.delete('/restaurants/{rid}/assets/{asset_id}')
async def detach_asset(rid:str,asset_id:str,request:Request):
    async def run(conn,row,data,actor,payload):
        meta,_=await store.resource(conn,rid,asset_id,'asset')
        if asset_id in json.dumps(data):
            raise d.Problem(409,'ASSET_REFERENCED','Replace or detach record references first')
        meta.update(detached=True,detachedAt=assets.now().isoformat())
        await store.put_resource(conn,rid,asset_id,'asset',meta)
        return {'id':asset_id,'detached':True,'retained':True},None
    return await mutation(request,rid,'onboarding:edit','asset.detached',run,edit=True)


async def download_link(request,rid,resource_id,kind):
    actor=await security.actor(request,'document:read')
    async with store.transaction() as conn:
        row,_=await store.record(conn,rid,actor,lock=True)
        meta,binary=await store.resource(conn,rid,resource_id,kind)
        if kind=='asset' and (meta['status']!='ready' or meta.get('detached')):
            raise d.Problem(409,'ASSET_NOT_READY','Asset is not ready')
        if not binary:
            raise d.Problem(404,'NOT_FOUND','Content unavailable')
        token=secrets.token_urlsafe(32)
        meta.update(downloadHash=security.digest(token),downloadExpiresAt=(assets.now()+timedelta(minutes=5)).isoformat(),downloadActor=actor['id'])
        await store.put_resource(conn,rid,resource_id,kind,meta)
    return response({'url':f'/api/v1/restaurants/{rid}/downloads/{resource_id}',
                     'method':'GET','headers':{'X-Download-Token':token},'expiresAt':meta['downloadExpiresAt']},row['revision'])


@router.post('/restaurants/{rid}/assets/{asset_id}/download')
async def asset_download(rid:str,asset_id:str,request:Request):
    return await download_link(request,rid,asset_id,'asset')


@router.get('/restaurants/{rid}/downloads/{resource_id}')
async def download(rid:str,resource_id:str,request:Request):
    actor=await security.actor(request,'document:read')
    async with store.transaction() as conn:
        await store.record(conn,rid,actor)
        meta,binary=await store.resource(conn,rid,resource_id)
        token=request.headers.get('x-download-token','')
        if not meta.get('downloadHash') or not secrets.compare_digest(security.digest(token),meta['downloadHash']) or meta.get('downloadActor')!=actor['id'] or assets.now()>datetime.fromisoformat(meta['downloadExpiresAt']):
            raise d.Problem(403,'LINK_EXPIRED','Download permission has expired or belongs to another actor')
        if meta.get('detached') or (meta.get('mimeType') and meta.get('status')!='ready'):
            raise d.Problem(409,'ASSET_NOT_READY','Asset unavailable')
        raw=store.cipher().decrypt(bytes(binary))
    # Attachment + sandbox prevents uploaded HTML/script content running under the admin origin.
    return Response(raw,media_type=meta.get('mimeType','application/pdf'),headers={
        'Content-Disposition':f'attachment; filename="{resource_id}"','Content-Security-Policy':"sandbox; default-src 'none'"})


@router.get('/menu-imports/template')
async def menu_template(request:Request):
    await security.actor(request)
    if request.query_params.get('format','csv')!='csv':
        raise d.Problem(415,'UNSUPPORTED_TYPE','Only CSV imports are supported')
    return Response(menus.TEMPLATE,media_type='text/csv',headers={'Content-Disposition':'attachment; filename="menu-template.csv"'})


@router.post('/restaurants/{rid}/menu-imports/preview')
async def menu_preview(rid:str,request:Request):
    async def run(conn,row,data,actor,payload):
        return menus.public_preview(await menus.preview(conn,rid,payload)),None
    return await mutation(request,rid,'onboarding:edit','menu.previewed',run,idempotent=True,edit=True)


@router.get('/restaurants/{rid}/menu-imports/{import_id}')
async def menu_import(rid:str,import_id:str,request:Request):
    actor=await security.actor(request)
    limit,cursor=page(request)
    try: offset=int(cursor) if cursor else 0
    except ValueError: d.fail('Invalid cursor')
    if offset<0: d.fail('Invalid cursor')
    async with store.transaction() as conn:
        row,_=await store.record(conn,rid,actor)
        value,_=await store.resource(conn,rid,import_id,'import')
    return response(menus.public_preview(value,offset,limit),row['revision'])


@router.patch('/restaurants/{rid}/menu-imports/{import_id}')
async def patch_import(rid:str,import_id:str,request:Request):
    async def run(conn,row,data,actor,payload):
        prev,_=await store.resource(conn,rid,import_id,'import')
        if prev['state']!='preview': raise d.Problem(409,'INVALID_TRANSITION','Committed import cannot be changed')
        if set(payload)-{'mapping','duplicateStrategy'}: d.fail('Only mapping/duplicateStrategy can be changed')
        return menus.public_preview(await menus.preview(conn,rid,{**prev['input'],**payload},import_id)),None
    return await mutation(request,rid,'onboarding:edit','menu.preview.updated',run,edit=True)


@router.post('/restaurants/{rid}/menu-imports/{import_id}/confirm')
async def confirm_import(rid:str,import_id:str,request:Request):
    async def run(conn,row,data,actor,payload):
        return await menus.confirm(conn,rid,import_id,payload,row,actor),None
    return await mutation(request,rid,'onboarding:edit','menu.committed',run,idempotent=True,edit=True)


@router.get('/restaurants/{rid}/menu-items')
async def menu_items(rid:str,request:Request):
    actor=await security.actor(request)
    limit,cursor=page(request)
    try: after=int(cursor) if cursor else 0
    except ValueError: d.fail('Invalid cursor')
    service=request.query_params.get('service','delivery')
    if service not in ('delivery','dining'): d.fail('Unknown service')
    async with store.transaction() as conn:
        row,data=await store.record(conn,rid,actor)
        column='delivery_available' if service=='delivery' or data['operations']['useDeliveryMenuForDining'] else 'dine_in_available'
        rows=await conn.fetch(f'SELECT * FROM items WHERE restaurant_id=$1 AND "Item_ID">$2 AND {column} ORDER BY "Item_ID" LIMIT $3',store.uid(rid),after,limit+1)
        values=[await menus.item_out(conn,rid,item) for item in rows[:limit]]
    return response({'items':values,'nextCursor':store.cursor_encode(rows[limit-1]['Item_ID']) if len(rows)>limit else None,'menuRevision':row['revision']},row['revision'])


@router.patch('/restaurants/{rid}/menu-items/{item_id}')
@router.delete('/restaurants/{rid}/menu-items/{item_id}')
async def mutate_item(rid:str,item_id:int,request:Request):
    async def run(conn,row,data,actor,payload):
        return await menus.edit_item(conn,rid,item_id,payload,actor,delete=request.method=='DELETE'),None
    return await mutation(request,rid,'onboarding:edit','menu.item.updated',run,edit=True)


@router.get('/restaurants/{rid}/documents')
async def documents(rid:str,request:Request):
    actor=await security.actor(request,'document:read')
    async with store.transaction() as conn:
        row,data=await store.record(conn,rid,actor)
        value=await store.represent(conn,row,data,actor)
    return response({'items':value['documents'],'nextCursor':None},row['revision'])


@router.put('/restaurants/{rid}/documents/{kind}')
async def save_document(rid:str,kind:str,request:Request):
    async def run(conn,row,data,actor,payload):
        clean=d.document_valid(kind,payload)
        await assets.ready_asset(conn,rid,clean['assetId'],kind)
        clean.update(status='Uploaded',updatedAt=assets.now().isoformat(),updatedBy=actor['id'])
        # Any replacement invalidates the prior approval, even when its asset ID is unchanged.
        data['documents'][kind]=clean
        await invalidate_contract(conn,rid,data,'Document/legal or banking details changed')
        await store.put_resource(conn,rid,str(uuid4()),'document-history',{'kind':kind,'action':'uploaded','document':clean})
        return None,None
    return await mutation(request,rid,'onboarding:edit','document.updated',run,edit=True)


def document_action(operation,permission):
    async def endpoint(rid:str,kind:str,request:Request):
        async def run(conn,row,data,actor,payload):
            doc=data.get('documents',{}).get(kind)
            if not doc: raise d.Problem(404,'NOT_FOUND','Document not found')
            if row['status'] in ('Active','Approved','Rejected'):
                raise d.Problem(409,'INVALID_TRANSITION','Document review is locked')
            if operation=='submit':
                if doc['status']!='Uploaded': raise d.Problem(409,'INVALID_TRANSITION','Expected Uploaded document')
                await assets.ready_asset(conn,rid,doc['assetId'],kind)
                doc.update(status='Under review',submittedBy=actor['id'])
            elif operation=='decisions':
                if doc['status']!='Under review': raise d.Problem(409,'INVALID_TRANSITION','Expected Under review document')
                decision=payload.get('decision')
                if decision not in ('approve','request_changes','reject'): d.fail('Invalid decision')
                reason=payload.get('reason','')
                if not isinstance(reason,str) or len(reason)>2000 or (decision!='approve' and not reason.strip()):
                    d.fail('A reason is required for corrections/rejection','reason')
                if doc.get('submittedBy')==actor['id']:
                    raise d.Problem(403,'SEPARATION_OF_DUTIES','Document must be reviewed by another actor')
                if decision=='approve':
                    d.document_valid(kind,{k:v for k,v in doc.items() if k in d.Document.model_fields})
                    await assets.ready_asset(conn,rid,doc['assetId'],kind)
                doc.update(status={'approve':'Approved','request_changes':'Changes requested','reject':'Rejected'}[decision],
                           reviewedBy=actor['id'],reason=reason)
                if decision!='approve': await invalidate_contract(conn,rid,data,'Document approval invalidated')
            else:
                raise d.Problem(503,'DEPENDENCY_UNAVAILABLE','External document verification is disabled by policy')
            doc['updatedAt']=assets.now().isoformat()
            await store.put_resource(conn,rid,str(uuid4()),'document-history',{'kind':kind,'action':operation,'document':doc.copy()})
            return None,None
        return await mutation(request,rid,permission,'document.'+operation,run,idempotent=True)
    endpoint.__name__='document_'+operation
    return endpoint


for _op,_perm in [('submit','onboarding:submit'),('decisions','document:review'),('verification','document:review')]:
    router.add_api_route('/restaurants/{rid}/documents/{kind}/'+_op,document_action(_op,_perm),methods=['POST'])


@router.get('/restaurants/{rid}/documents/{kind}/history')
async def document_history(rid:str,kind:str,request:Request):
    actor=await security.actor(request,'document:read')
    limit,cursor=page(request)
    # Each event stores a kind prefix outside document values for filtering after decryption.
    async with store.transaction() as conn:
        row,_=await store.record(conn,rid,actor)
        rows=await conn.fetch("SELECT id,payload FROM onboarding_resources WHERE restaurant_id=$1 AND kind='document-history' AND ($2::uuid IS NULL OR id>$2) ORDER BY id",store.uid(rid),store.uid(cursor) if cursor else None)
        values=[]
        for item in rows:
            event=store.unseal(item['payload'])
            if event['kind']!=kind: continue
            doc=event['document']
            # History exposes decisions and immutable evidence references, never old raw identifiers.
            values.append({'id':str(item['id']),'kind':kind,'action':event['action'],
                           **{k:doc[k] for k in ('assetId','status','updatedAt','updatedBy','reviewedBy','reason') if k in doc}})
            if len(values)>limit: break
    return response({'items':values[:limit],'nextCursor':store.cursor_encode(values[limit-1]['id']) if len(values)>limit else None},row['revision'])


@router.get('/restaurants/{rid}/verifications/{verification_id}')
async def verification(rid:str,verification_id:str,request:Request):
    actor=await security.actor(request,'document:read')
    async with store.transaction() as conn:
        await store.record(conn,rid,actor)
    raise d.Problem(503,'DEPENDENCY_UNAVAILABLE','External document verification is disabled by policy')


@router.get('/restaurants/{rid}/contracts/current')
async def current_contract(rid:str,request:Request):
    actor=await security.actor(request,'document:read')
    async with store.transaction() as conn:
        row,data=await store.record(conn,rid,actor)
    return response(data.get('contract'),row['revision'])


@router.post('/restaurants/{rid}/contracts')
async def prepare_contract(rid:str,request:Request):
    async def run(conn,row,data,actor,payload):
        if payload: d.fail('Commercial terms come from approved server configuration; body must be empty')
        if row['status'] in ('Active','Approved','Rejected'): raise d.Problem(409,'INVALID_TRANSITION','Contract preparation is locked')
        return await contracts.prepare(conn,rid,data,actor),None
    return await mutation(request,rid,'contract:prepare','contract.prepared',run,idempotent=True)


@router.post('/restaurants/{rid}/contracts/{contract_id}/download')
async def contract_download(rid:str,contract_id:str,request:Request):
    return await download_link(request,rid,contract_id,'contract')


def contract_action(operation,permission):
    async def endpoint(rid:str,contract_id:str,request:Request):
        async def run(conn,row,data,actor,payload):
            if row['status'] in ('Active','Rejected'):
                raise d.Problem(409,'INVALID_TRANSITION','Contract changes require a configured change-review policy')
            result=await contracts.action(conn,rid,data,contract_id,operation,payload,actor)
            # Superseding an approved partner's agreement removes approval and activation readiness.
            return result,'Under review' if row['status']=='Approved' else None
        return await mutation(request,rid,permission,'contract.'+operation,run,idempotent=True)
    endpoint.__name__='contract_'+operation.replace('/','_')
    return endpoint


for _op,_perm in [('signed-copy','contract:prepare'),('signature-requests','contract:send'),
                  ('signed-copy/approve','document:review'),('supersede','contract:prepare')]:
    router.add_api_route('/restaurants/{rid}/contracts/{contract_id}/'+_op,contract_action(_op,_perm),methods=['POST'])


@router.get('/restaurants/{rid}/contracts/{contract_id}/signature-requests/{signature_id}')
async def signature_status(rid:str,contract_id:str,signature_id:str,request:Request):
    actor=await security.actor(request,'document:read')
    async with store.transaction() as conn:
        _,data=await store.record(conn,rid,actor)
        await contracts.current(conn,rid,data,contract_id)
    raise d.Problem(503,'DEPENDENCY_UNAVAILABLE','E-sign provider is not configured')


@router.post('/integrations/esign/webhooks/{provider}')
async def esign_webhook(provider:str,request:Request):
    # No provider is enabled. Never trust an unsigned callback or mutate signature state.
    raise d.Problem(503,'DEPENDENCY_UNAVAILABLE','E-sign provider is not configured')
