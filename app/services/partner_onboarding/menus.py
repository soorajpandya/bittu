"""CSV preview/commit and schema-preserving adapters for existing menu tables."""
import csv
import io
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from pydantic import Field

from . import assets, store
from .domain import Strict, Problem, fail, fingerprint, validate

TEMPLATE='category,name,description,price,dietary,available,gst_rate,pricing_type\n'


def minor(value):
    try:
        amount=Decimal(str(value))
        if not amount.is_finite() or amount<0 or amount>Decimal('99999999.99') or amount*100!=(amount*100).to_integral_value():
            raise ValueError()
        return int(amount*100)
    except (InvalidOperation,ValueError,TypeError):
        fail('Price must be nonnegative, with at most two decimal places','price')


class Preview(Strict):
    assetId: str
    service: str = 'delivery'
    mapping: dict[str,str] = Field(default_factory=dict)
    duplicateStrategy: str = 'skip'
    # Existing menu tax semantics must be selected explicitly; never invent GST.
    gstRate: int = Field(ge=0)
    pricingType: str


def pricing(price_minor,rate,kind):
    if rate not in (0,5,12,18,28) or kind not in ('mrp','configurable'):
        fail('Use a supported GST rate and mrp/configurable pricingType','gstRate')
    amount=Decimal(price_minor)/100
    if kind=='mrp':
        base=(amount/(1+Decimal(rate)/100)).quantize(Decimal('.01'))
        return amount,base,amount,True
    total=(amount*(1+Decimal(rate)/100)).quantize(Decimal('.01'))
    return amount,amount,total,False


async def preview(conn,rid,body,import_id=None):
    args=validate(Preview,body)
    if args['service'] not in ('delivery','dining') or args['duplicateStrategy']!='skip':
        fail('Unsupported service or duplicate strategy')
    pricing(0,args['gstRate'],args['pricingType'])
    meta,binary=await assets.ready_asset(conn,rid,args['assetId'],'menu')
    if meta['mimeType']!='text/csv':
        fail('Menu import requires a CSV asset','assetId')
    text=store.cipher().decrypt(bytes(binary)).decode('utf-8-sig')
    reader=csv.DictReader(io.StringIO(text))
    headers=reader.fieldnames or []
    if len(headers)!=len(set(headers)) or len(headers)>30:
        fail('Duplicate or too many CSV columns')
    columns=['category','name','description','price','dietary','available','gst_rate','pricing_type']
    mapping={c:args['mapping'].get(c,c) for c in columns}
    if set(args['mapping'])-set(columns):
        fail('Unknown canonical mapping column','mapping')
    for col in ['category','name','price','dietary','available']:
        if mapping[col] not in headers:
            fail('Missing required CSV column: '+col,'mapping.'+col)
    existing=await conn.fetch('SELECT "Category","Item_Name" FROM items WHERE restaurant_id=$1',store.uid(rid))
    seen={(str(r['Category'] or '').strip().casefold(),r['Item_Name'].strip().casefold()) for r in existing}
    accepted,errors,skipped=[],[],0
    for index,raw in enumerate(reader,start=2):
        if index>1001:
            fail('Maximum 1000 CSV rows')
        row={k:raw.get(v,'') or '' for k,v in mapping.items()}
        try:
            if None in raw:
                fail('Row has more fields than the header')
            name,category=row['name'].strip(),row['category'].strip()
            if not name or len(name)>255 or not category or len(category)>100 or len(row['description'])>4000:
                fail('Invalid name, category or description')
            if row['dietary'] not in ('veg','non-veg','vegan') or row['available'] not in ('true','false'):
                fail('Invalid dietary or available value')
            amount=minor(row['price'])
            try:
                rate=int(row['gst_rate']) if row['gst_rate'] else args['gstRate']
            except ValueError:
                fail('Invalid GST rate')
            kind=row['pricing_type'] or args['pricingType']
            pricing(amount,rate,kind)
            key=(category.casefold(),name.casefold())
            if key in seen:
                skipped+=1
                continue
            seen.add(key)
            accepted.append({'name':name,'category':category,'description':row['description'], 'priceMinor':amount,
                             'currency':'INR','dietary':row['dietary'],'available':row['available']=='true',
                             'gstRate':rate,'pricingType':kind})
        except Problem as exc:
            errors.append({'row':index,'message':exc.message})
    result={'id':import_id or str(uuid4()),'state':'preview','schemaVersion':'bittu-items-v1','input':args,
            'columns':headers,'mapping':mapping,'rows':accepted,'errors':errors,'accepted':len(accepted),
            'rejected':len(errors),'skipped':skipped,'service':args['service']}
    result['previewHash']=fingerprint(result)
    await store.put_resource(conn,rid,result['id'],'import',result)
    return result


def public_preview(data,offset=0,limit=100):
    return {**{k:v for k,v in data.items() if k not in ('rows','input','errors')},
            'items':data['rows'][offset:offset+limit], 'errors':data['errors'][offset:offset+limit],
            'nextCursor':store.cursor_encode(offset+limit) if max(len(data['rows']),len(data['errors']))>offset+limit else None}


async def confirm(conn,rid,import_id,body,row,actor):
    data,_=await store.resource(conn,rid,import_id,'import')
    if data['state']!='preview':
        raise Problem(409,'INVALID_TRANSITION','Import already committed')
    if body.get('previewHash')!=data['previewHash'] or body.get('expectedMenuRevision')!=row['revision']:
        raise Problem(412,'STALE_RECORD','Preview hash or menu revision changed',revision=row['revision'])
    # Re-read CSV and current menu under the aggregate lock; require fresh confirmation if anything changed.
    fresh=await preview(conn,rid,data['input'],import_id)
    if fresh['previewHash']!=data['previewHash']:
        raise Problem(409,'PREVIEW_CHANGED','Menu changed; run preview again')
    if fresh['rejected']:
        fail('Correct rejected CSV rows before confirming')
    owner=str(actor['tenant'])
    branch=await conn.fetchval('SELECT id FROM sub_branches WHERE restaurant_id=$1 AND is_main_branch=true LIMIT 1',store.uid(rid))
    created=[]
    for item in fresh['rows']:
        price,base,total,inclusive=pricing(item['priceMinor'],item['gstRate'],item['pricingType'])
        iid=await conn.fetchval('''INSERT INTO items("Item_Name","Category","Description",price,"Available_Status",is_veg,
          restaurant_id,branch_id,user_id,delivery_available,dine_in_available,takeaway_available,
          pricing_type,gst_rate,is_tax_inclusive,price_before_tax,final_price)
          VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,false,$12,$13,$14,$15,$16) RETURNING "Item_ID"''',
          item['name'],item['category'],item['description'],price,item['available'],item['dietary']!='non-veg',
          store.uid(rid),branch,owner,data['service']=='delivery',data['service']=='dining',
          item['pricingType'],item['gstRate'],inclusive,base,total)
        await conn.execute('INSERT INTO onboarding_menu_links(restaurant_id,item_id,dietary) VALUES($1,$2,$3)',store.uid(rid),iid,item['dietary'])
        created.append(iid)
    fresh.update(state='committed',created=len(created),itemIds=created)
    await store.put_resource(conn,rid,import_id,'import',fresh)
    return public_preview(fresh)


class Customization(Strict):
    id: int | None = Field(None,gt=0)
    name: str = Field(min_length=1,max_length=255)
    priceMinor: int = Field(ge=0,le=9999999999)
    isActive: bool = True


class ItemPatch(Strict):
    name: str | None = Field(None,min_length=1,max_length=255)
    category: str | None = Field(None,min_length=1,max_length=100)
    description: str | None = Field(None,max_length=4000)
    priceMinor: int | None = Field(None,ge=0,le=9999999999)
    currency: str = 'INR'
    dietary: str | None = None
    available: bool | None = None
    deliveryAvailable: bool | None = None
    diningAvailable: bool | None = None
    gstRate: int | None = None
    pricingType: str | None = None
    variants: list[Customization] | None = Field(None,max_length=100)
    addons: list[Customization] | None = Field(None,max_length=100)
    extras: list[Customization] | None = Field(None,max_length=100)


async def item_out(conn,rid,item):
    out={'id':item['Item_ID'],'name':item['Item_Name'],'category':item['Category'],'description':item['Description'],
         'priceMinor':minor(item['price']),'currency':'INR','available':item['Available_Status'],
         'deliveryAvailable':item['delivery_available'],'diningAvailable':item['dine_in_available'],
         'gstRate':int(item['gst_rate'] or 0),'pricingType':item['pricing_type'],
         'dietary':await conn.fetchval('SELECT dietary FROM onboarding_menu_links WHERE restaurant_id=$1 AND item_id=$2',store.uid(rid),item['Item_ID']) or ('veg' if item['is_veg'] else 'non-veg')}
    for name in ('variants','addons','extras'):
        rows=await conn.fetch(f'SELECT id,name,price,is_active FROM item_{name} WHERE item_id=$1 ORDER BY id',item['Item_ID'])
        out[name]=[{'id':r['id'],'name':r['name'],'priceMinor':minor(r['price']),'isActive':r['is_active']} for r in rows]
    return out


async def edit_item(conn,rid,iid,body,actor,delete=False):
    item=await conn.fetchrow('SELECT * FROM items WHERE restaurant_id=$1 AND "Item_ID"=$2 FOR UPDATE',store.uid(rid),iid)
    if not item:
        raise Problem(404,'NOT_FOUND','Menu item not found')
    if delete:
        await conn.execute('DELETE FROM items WHERE restaurant_id=$1 AND "Item_ID"=$2',store.uid(rid),iid)
        return {'deleted':True,'id':iid}
    args=validate(ItemPatch,body)
    if args['currency']!='INR' or args['dietary'] not in (None,'veg','non-veg','vegan'):
        fail('Unsupported currency or dietary value')
    values=dict(item)
    for key,col in {'name':'Item_Name','category':'Category','description':'Description','available':'Available_Status',
                    'deliveryAvailable':'delivery_available','diningAvailable':'dine_in_available'}.items():
        if args[key] is not None:
            values[col]=args[key]
    rate=args['gstRate'] if args['gstRate'] is not None else int(item['gst_rate'] or 0)
    kind=args['pricingType'] or item['pricing_type'] or 'mrp'
    price,base,total,inclusive=pricing(args['priceMinor'] if args['priceMinor'] is not None else minor(item['price']),rate,kind)
    await conn.execute('''UPDATE items SET "Item_Name"=$3,"Category"=$4,"Description"=$5,"Available_Status"=$6,
      delivery_available=$7,dine_in_available=$8,price=$9,price_before_tax=$10,final_price=$11,gst_rate=$12,
      pricing_type=$13,is_tax_inclusive=$14,is_veg=$15,updated_at=now() WHERE restaurant_id=$1 AND "Item_ID"=$2''',
      store.uid(rid),iid,values['Item_Name'],values['Category'],values['Description'],values['Available_Status'],
      values['delivery_available'],values['dine_in_available'],price,base,total,rate,kind,inclusive,
      args['dietary']!='non-veg' if args['dietary'] else item['is_veg'])
    if args['dietary']:
        await conn.execute('''INSERT INTO onboarding_menu_links(restaurant_id,item_id,dietary) VALUES($1,$2,$3)
          ON CONFLICT(restaurant_id,item_id) DO UPDATE SET dietary=excluded.dietary''',store.uid(rid),iid,args['dietary'])
    # Upsert explicitly supplied children; absent children and other customization systems remain intact.
    for name in ('variants','addons','extras'):
        for child in args[name] or []:
            if child['id']:
                result=await conn.execute(f'UPDATE item_{name} SET name=$3,price=$4,is_active=$5 WHERE item_id=$1 AND id=$2',
                                         iid,child['id'],child['name'],Decimal(child['priceMinor'])/100,child['isActive'])
                if result=='UPDATE 0':
                    raise Problem(404,'NOT_FOUND','Customization does not belong to item')
            else:
                await conn.execute(f'INSERT INTO item_{name}(item_id,user_id,name,price,is_active) VALUES($1,$2,$3,$4,$5)',
                                   iid,actor['tenant'],child['name'],Decimal(child['priceMinor'])/100,child['isActive'])
    return await item_out(conn,rid,await conn.fetchrow('SELECT * FROM items WHERE restaurant_id=$1 AND "Item_ID"=$2',store.uid(rid),iid))
