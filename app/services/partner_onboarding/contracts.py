"""Configured contract snapshots and manual signature-evidence review."""
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from xml.sax.saxutils import escape

from . import assets, store
from .domain import Problem, fail, fingerprint


def config(tenant):
    location=os.getenv('ONBOARDING_CONTRACT_CONFIG')
    if not location:
        raise Problem(503,'DEPENDENCY_UNAVAILABLE','Approved contract configuration is required')
    try:
        settings=json.loads(Path(location).read_text())
        result=settings['tenants'][str(tenant)]
        if not result.get('version') or not result.get('templateText') or not isinstance(result.get('commercialTerms'),dict) or not result['commercialTerms']:
            raise ValueError()
        return result
    except (KeyError,ValueError,OSError):
        raise Problem(503,'DEPENDENCY_UNAVAILABLE','No valid approved contract configuration for tenant') from None


def pdf(data,template):
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    buf=io.BytesIO()
    styles=getSampleStyleSheet()
    document=SimpleDocTemplate(buf)
    blocks=[Paragraph('Restaurant partner agreement',styles['Title']),Spacer(1,16)]
    text=[f"Contract: {data['id']} | Template version: {data['templateVersion']}",
          f"Prepared: {data['preparedAt']}",f"Legal entity: {data['legalEntity']['legalName']}",
          f"Restaurant: {data['legalEntity']['restaurantName']}",template,
          'Commercial terms: '+json.dumps(data['commercialTerms'],ensure_ascii=False)]
    for part in text:
        blocks.append(Paragraph(escape(part).replace('\n','<br/>'),styles['BodyText']))
        blocks.append(Spacer(1,12))
    document.build(blocks)
    return buf.getvalue()


async def prepare(conn,rid,data,actor):
    existing=data.get('contract')
    if existing and existing['status']!='Superseded':
        raise Problem(409,'INVALID_TRANSITION','Supersede the current contract before preparing another')
    pan=data.get('documents',{}).get('PAN')
    if not pan or pan['status']!='Approved':
        fail('Approve PAN/legal entity before preparing the contract','documents.PAN')
    cfg=config(actor['tenant'])
    contract={'id':str(uuid4()),'status':'Draft','templateVersion':cfg['version'],
              'preparedAt':datetime.now(timezone.utc).isoformat(),
              'legalEntity':{'legalName':pan['legalName'],'restaurantName':data['restaurant']['name']},
              'commercialTerms':cfg['commercialTerms'],'configurationHash':fingerprint(cfg),'signatories':[]}
    binary=store.cipher().encrypt(pdf(contract,cfg['templateText']))
    await store.put_resource(conn,rid,contract['id'],'contract',contract,binary)
    data['contract']=contract
    return contract


async def current(conn,rid,data,cid):
    if not data.get('contract') or data['contract']['id']!=cid:
        raise Problem(404,'NOT_FOUND','Current contract not found')
    contract,_=await store.resource(conn,rid,cid,'contract')
    return contract


async def action(conn,rid,data,cid,op,body,actor):
    contract=await current(conn,rid,data,cid)
    if op=='signed-copy':
        if contract['status'] not in ('Draft','Awaiting signature'):
            raise Problem(409,'INVALID_TRANSITION','Contract is not awaiting signature evidence')
        for key in ('assetId','signatoryName','role'):
            if not isinstance(body.get(key),str) or not body[key].strip() or len(body[key])>200:
                fail('Required string',key)
        await assets.ready_asset(conn,rid,body['assetId'],'signed-agreement')
        contract.update(status='Awaiting signature',evidence={'assetId':body['assetId'],
                        'signatoryName':body['signatoryName'],'role':body['role'],'submittedBy':actor['id'],
                        'submittedAt':datetime.now(timezone.utc).isoformat()})
    elif op=='signed-copy/approve':
        if contract['status']!='Awaiting signature' or not contract.get('evidence'):
            raise Problem(409,'INVALID_TRANSITION','Signed copy must be submitted first')
        if contract['evidence']['submittedBy']==actor['id']:
            raise Problem(403,'SEPARATION_OF_DUTIES','Another authorized reviewer must validate signature evidence')
        await assets.ready_asset(conn,rid,contract['evidence']['assetId'],'signed-agreement')
        if contract['configurationHash']!=fingerprint(config(actor['tenant'])):
            raise Problem(409,'CONTRACT_CHANGED','Configured terms changed; supersede and prepare a new contract')
        contract.update(status='Signed',signedAt=datetime.now(timezone.utc).isoformat(),validatedBy=actor['id'])
        contract['signatories']=[{k:contract['evidence'][k] for k in ('signatoryName','role')}]
    elif op=='supersede':
        if contract['status']=='Superseded':
            raise Problem(409,'INVALID_TRANSITION','Contract already superseded')
        reason=body.get('reason','')
        if not isinstance(reason,str) or not reason.strip() or len(reason)>2000:
            fail('A reason is required','reason')
        contract.update(status='Superseded',supersededAt=datetime.now(timezone.utc).isoformat(),supersededBy=actor['id'],reason=reason)
    else:
        raise Problem(503,'DEPENDENCY_UNAVAILABLE','E-sign provider is not configured')
    await store.put_resource(conn,rid,cid,'contract',contract)
    data['contract']=contract
    return contract
