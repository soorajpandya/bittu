"""Route integration tests: isolated PostgreSQL; gateway calls are simulated.

Run with Python 3.12, pytest, pytest-asyncio, asyncpg and pgserver installed.
No production database or Razorpay credentials are used.
"""
import asyncio
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
from fastapi import HTTPException

from app.core import webhook_security
from app.services.razorpay import route_safety, route_service, webhook_dispatcher
from app.services.razorpay.fee_policy import split_payment_paise, merchant_transfer_paise
from app.services.razorpay.merchant_finance_service import _allocation_totals

MERCHANT = '11111111-1111-1111-1111-111111111111'
OTHER = '22222222-2222-2222-2222-222222222222'


@pytest.fixture(scope='session')
def postgres(tmp_path_factory):
    pgserver = pytest.importorskip('pgserver')
    server = pgserver.get_server(tmp_path_factory.mktemp('route-postgres'), cleanup_mode='delete')
    yield server.get_uri()
    server.cleanup()


@pytest.fixture
async def db(postgres, monkeypatch):
    schema = 'test_' + uuid4().hex
    bootstrap = await asyncpg.connect(postgres)
    await bootstrap.execute(f'CREATE SCHEMA {schema}')
    pool = await asyncpg.create_pool(postgres, min_size=1, max_size=8, server_settings={'search_path': schema})
    @asynccontextmanager
    async def connect():
        async with pool.acquire() as conn:
            yield conn
    for mod in (route_safety, route_service, webhook_security, webhook_dispatcher):
        monkeypatch.setattr(mod, 'get_service_connection', connect)
    async with connect() as conn:
        await conn.execute('''
            CREATE TABLE payment_webhook_events (
              id uuid PRIMARY KEY, gateway text, event_id text, event_type text, event_hash text,
              signature_valid bool, processing_state text, headers jsonb, raw_payload jsonb,
              received_at timestamptz, processed_at timestamptz, latency_ms numeric, last_error text,
              retries int DEFAULT 0, UNIQUE(gateway,event_id));
            CREATE TABLE rzp_payments (
              razorpay_payment_id text PRIMARY KEY, merchant_id uuid, amount_paise bigint,
              currency text DEFAULT 'INR', captured bool DEFAULT true, razorpay_order_id text,
              captured_at timestamptz,created_at timestamptz DEFAULT now());
            CREATE TABLE rzp_refunds (
              refund_id text PRIMARY KEY, razorpay_payment_id text,merchant_id uuid,amount_paise bigint,status text);
            CREATE TABLE rzp_route_transfers (
              transfer_id text PRIMARY KEY,razorpay_payment_id text,merchant_id uuid,amount_paise bigint,status text);
        ''')
        await conn.execute(Path('migrations/086_route_payment_safety.sql').read_text())
        await conn.execute("INSERT INTO rzp_payments(razorpay_payment_id,merchant_id,amount_paise) VALUES('pay_test',$1::uuid,10000)", MERCHANT)
    yield connect
    await pool.close()
    await bootstrap.execute(f'DROP SCHEMA {schema} CASCADE')
    await bootstrap.close()


@pytest.fixture
async def gateway(db, monkeypatch):
    service = route_service.RzpRouteService()
    monkeypatch.setattr(service, '_existing_account', AsyncMock(return_value={
        'linked_account_id':'acc_test', 'status':'created', 'route_product_status':'activated'}))
    monkeypatch.setattr(service, 'upsert_transfer_from_razorpay', AsyncMock(side_effect=lambda **kw:kw['rzp_entity']))
    monkeypatch.setattr(route_service, '_safe_audit', AsyncMock())
    transfers = []
    async def create(payment_id, **kw):
        await asyncio.sleep(.01)
        body = kw['transfers'][0]
        entity = dict(id='trf_test', source=payment_id,recipient=body['account'],amount=body['amount'],status='processed',amount_reversed=0)
        transfers.append(entity)
        return {'items':[entity]}
    post = AsyncMock(side_effect=create)
    monkeypatch.setattr(route_service.route_api, 'create_transfers_for_payment', post)
    monkeypatch.setattr(route_service.route_api, 'fetch_transfers_for_payment', AsyncMock(side_effect=lambda *a,**kw:{'items':[dict(t) for t in transfers]}))
    monkeypatch.setattr(route_service.route_api, 'fetch_transfers_for_order', AsyncMock(return_value={'items':[]}))
    monkeypatch.setattr(route_service.route_api, 'fetch_transfer', AsyncMock(side_effect=lambda tid,**kw:dict(next(t for t in transfers if t['id']==tid))))
    return service, transfers, post


async def create(service, merchant=MERCHANT, amount=9500):
    return await service.create_transfer(merchant_id=merchant,razorpay_payment_id='pay_test',amount_paise=amount)


@pytest.mark.parametrize('gateway_fee',[0,165,236,500,800])
def test_gateway_charges_do_not_reduce_merchant_share(gateway_fee):
    assert split_payment_paise(10000,gateway_fee)==(9500,500-gateway_fee,gateway_fee)


def test_rounding_conserves_every_paisa():
    for gross in range(1,20000):
        split=split_payment_paise(gross,165)
        assert sum(split)==gross
        assert split[0]==merchant_transfer_paise(gross)


async def test_concurrent_capture_creates_one_transfer(gateway):
    service,transfers,post=gateway
    await asyncio.gather(create(service),create(service),create(service))
    assert post.await_count==1
    assert transfers[0]['amount']==9500


async def test_historical_order_transfer_is_not_repriced(gateway):
    service,transfers,post=gateway
    transfers.append(dict(id='trf_old',recipient='acc_test',amount=9793,status='processed'))
    result=await create(service)
    assert result['transfers'][0]['amount']==9793
    post.assert_not_awaited()


async def test_owner_and_split_enforced(gateway):
    service,_,post=gateway
    with pytest.raises(PermissionError):
        await create(service,merchant=OTHER)
    with pytest.raises(ValueError):
        await create(service,amount=9900)
    post.assert_not_awaited()


async def test_timeout_without_visible_transfer_does_not_repeat_post(gateway):
    service,_,post=gateway
    post.side_effect=TimeoutError('uncertain')
    with pytest.raises(TimeoutError):
        await create(service)
    with pytest.raises(RuntimeError,match='outcome unknown'):
        await create(service)
    assert post.await_count==1


async def test_timeout_with_completed_transfer_is_reconciled(gateway, db):
    service,transfers,post=gateway
    async def sent_then_timeout(*a,**kw):
        transfers.append(dict(id='trf_sent',recipient='acc_test',amount=9500,status='processed'))
        raise TimeoutError()
    post.side_effect=sent_then_timeout
    with pytest.raises(TimeoutError):
        await create(service)
    result=await create(service)
    assert result['transfers'][0]['id']=='trf_sent'
    assert post.await_count==1
    async with db() as conn:
        operation = await conn.fetchrow("SELECT state,response FROM rzp_route_operations")
    assert operation['state'] == 'succeeded'
    assert json.loads(operation['response'])['items'][0]['id'] == 'trf_sent'


async def test_timeout_with_wrong_transfer_amount_remains_unresolved(gateway, db):
    service, transfers, post = gateway
    async def sent_then_timeout(*args, **kwargs):
        transfers.append(dict(id='trf_wrong', recipient='acc_test', amount=9000, status='processed'))
        raise TimeoutError()
    post.side_effect = sent_then_timeout
    with pytest.raises(TimeoutError):
        await create(service)
    with pytest.raises(RuntimeError, match='saved entitlement'):
        await create(service)
    async with db() as conn:
        assert await conn.fetchval("SELECT state FROM rzp_route_operations") == 'unknown'
    assert post.await_count == 1


@pytest.mark.parametrize('overrides,exception', [
    ({'transfer_status': 'failed', 'status': 'processed'}, RuntimeError),
    ({'amount': 9000}, RuntimeError),
    ({'recipient': 'acc_other'}, ValueError),
])
async def test_invalid_transfer_response_cannot_complete_capture(gateway, overrides, exception):
    service, _, post = gateway
    entity = dict(id='trf_bad', recipient='acc_test', amount=9500, status='processed')
    entity.update(overrides)
    post.side_effect = None
    post.return_value = {'items': [entity]}
    for _ in range(2):
        with pytest.raises(exception):
            await create(service)
    # A cached failed/malformed response must not send another money movement.
    assert post.await_count == 1
    service.upsert_transfer_from_razorpay.assert_not_awaited()


async def test_refund_validates_all_recipients_before_any_reversal(gateway, db, monkeypatch):
    service, transfers, _ = gateway
    await create(service)
    transfers.append(dict(id='trf_other', recipient='acc_other', amount=500, status='processed'))
    reverse = AsyncMock()
    monkeypatch.setattr(route_service.route_api, 'reverse_transfer', reverse)
    async with db() as conn:
        await conn.execute("INSERT INTO rzp_refunds VALUES('r1','pay_test',$1::uuid,10000,'processed')", MERCHANT)
    with pytest.raises(PermissionError, match='another linked account'):
        await service.reverse_refunded_share(razorpay_payment_id='pay_test', refund_id='r1')
    reverse.assert_not_awaited()


async def test_order_owned_split_waits_instead_of_creating_duplicate(gateway,db,monkeypatch):
    service,_,post=gateway
    async with db() as conn:
        await conn.execute("UPDATE rzp_payments SET razorpay_order_id='order_old'")
    monkeypatch.setattr(route_service.route_api,'fetch_transfers_for_order',AsyncMock(return_value={'items':[{'id':'trf_pending'}]}))
    with pytest.raises(RuntimeError,match='order-owned'):
        await create(service)
    post.assert_not_awaited()


async def test_equal_partial_refunds_and_replay_reverse_exactly_original_share(gateway,db,monkeypatch):
    service,transfers,_=gateway
    await create(service)
    async def reverse(tid,**kw):
        transfers[0]['amount_reversed']+=kw['amount_paise']
        return {'id':'reversal_'+str(transfers[0]['amount_reversed'])}
    reverse_mock=AsyncMock(side_effect=reverse)
    monkeypatch.setattr(route_service.route_api,'reverse_transfer',reverse_mock)
    for index in range(3):
        amount=3333 if index<2 else 3334
        async with db() as conn:
            await conn.execute("INSERT INTO rzp_refunds VALUES($1,'pay_test',$2::uuid,$3,'processed')",f'r{index}',MERCHANT,amount)
        await service.reverse_refunded_share(razorpay_payment_id='pay_test',refund_id=f'r{index}')
        await service.reverse_refunded_share(razorpay_payment_id='pay_test',refund_id=f'r{index}')
    assert transfers[0]['amount_reversed']==9500
    assert reverse_mock.await_count==3
    assert [c.kwargs['amount_paise'] for c in reverse_mock.await_args_list]==[3166,3166,3168]


async def test_finance_preserves_historical_split_and_refund_proportion(db):
    async with db() as conn:
        await conn.execute("INSERT INTO rzp_route_transfers VALUES('trf_old','pay_test',$1::uuid,9793,'processed')",MERCHANT)
        await conn.execute("INSERT INTO rzp_refunds VALUES('ref','pay_test',$1::uuid,5000,'processed')",MERCHANT)
        result=await _allocation_totals(conn,MERCHANT)
        assert result['merchant_paise']==9793
        assert result['deduction_paise']==207
        assert result['refund_share_paise']==4896
        assert result['unresolved_payments']==0
        other=await _allocation_totals(conn,OTHER)
        assert other['merchant_paise']==0


async def test_unallocated_historical_payment_is_flagged_not_repriced(db):
    async with db() as conn:
        result=await _allocation_totals(conn,MERCHANT)
        assert result['merchant_paise']==0
        assert result['unresolved_payments']==1


async def register():
    body=json.dumps({'event':'payment.captured','id':'evt_test'}).encode()
    signature=hmac.new(b'test_secret',body,hashlib.sha256).hexdigest()
    request=SimpleNamespace(headers={'X-Razorpay-Signature':signature})
    return await webhook_security.verify_and_register_webhook(request=request,body=body,gateway='razorpay',secret='test_secret')


async def test_failed_webhook_retries_and_completed_duplicate_is_skipped(db):
    first=await register()
    with pytest.raises(HTTPException) as exc:
        await register()
    assert exc.value.status_code==503
    await first.mark_processed(status='failed',error='timeout')
    second=await register()
    assert not second.duplicate
    assert second.event_row_id==first.event_row_id
    await first.mark_processed()  # expired worker cannot overwrite the new claim
    async with db() as conn:
        assert await conn.fetchval('SELECT processing_state FROM payment_webhook_events')=='received'
    await second.mark_processed()
    assert (await register()).duplicate


async def test_abandoned_webhook_claim_is_recoverable(db):
    first=await register()
    async with db() as conn:
        await conn.execute("UPDATE payment_webhook_events SET processing_started_at=now()-interval '11 minutes'")
    second=await register()
    assert second.event_row_id==first.event_row_id
    assert second.processing_token!=first.processing_token


async def test_capture_amount_mismatch_never_credits_or_transfers(db,monkeypatch):
    from decimal import Decimal
    async with db() as conn:
        await conn.execute('CREATE TABLE payments(id uuid,order_id uuid,amount numeric,method text,restaurant_id uuid,razorpay_payment_id text,razorpay_order_id text)')
        await conn.execute("INSERT INTO payments VALUES($1,$2,100,'upi',$3::uuid,NULL,'order_test')",uuid4(),uuid4(),MERCHANT)
    monkeypatch.setattr(webhook_dispatcher,'_resolve_order_context_by_rzp_order',AsyncMock(return_value={'merchant_id':MERCHANT}))
    mirror=AsyncMock()
    monkeypatch.setattr(webhook_dispatcher,'_upsert_rzp_payment',mirror)
    for amount,currency in [(9999,'INR'),(10000,'USD')]:
        envelope={'payload':{'payment':{'entity':{'id':'pay_test','order_id':'order_test','amount':amount,'currency':currency}}}}
        with pytest.raises(ValueError,match='mismatch'):
            await webhook_dispatcher._handle_payment_captured(envelope,None)
    mirror.assert_not_awaited()


async def test_qr_capture_recovers_order_and_propagates_route_failure(db,monkeypatch):
    order_id=uuid4()
    async with db() as conn:
        await conn.execute('CREATE TABLE payments(id uuid,order_id uuid,amount numeric,method text,restaurant_id uuid,razorpay_payment_id text,razorpay_order_id text)')
        await conn.execute('CREATE TABLE rzp_orders(razorpay_order_id text,internal_order_id uuid,merchant_id uuid,created_at timestamptz DEFAULT now())')
        await conn.execute("INSERT INTO payments VALUES($1,$2,100,'upi',$3::uuid,NULL,'order_test')",uuid4(),order_id,MERCHANT)
        await conn.execute("INSERT INTO rzp_orders VALUES('order_test',$1,$2::uuid,now())",order_id,MERCHANT)
    resolve=AsyncMock(side_effect=[None,{'merchant_id':MERCHANT,'internal_order_id':str(order_id)}])
    monkeypatch.setattr(webhook_dispatcher,'_resolve_order_context_by_rzp_order',resolve)
    monkeypatch.setattr(webhook_dispatcher,'_upsert_rzp_payment',AsyncMock())
    transfer=AsyncMock(side_effect=RuntimeError('gateway unavailable'))
    monkeypatch.setattr(route_service.rzp_route_service,'create_transfer',transfer)
    transition=AsyncMock()
    monkeypatch.setattr(webhook_dispatcher,'_mark_payments_row',transition)
    envelope={'payload':{'payment':{'entity':{'id':'pay_test','amount':10000,'currency':'INR',
                'notes':{'merchant_id':MERCHANT,'internal_order_id':str(order_id)}}}}}
    with pytest.raises(RuntimeError,match='gateway unavailable'):
        await webhook_dispatcher._handle_payment_captured(envelope,None)
    assert transfer.await_args.kwargs['amount_paise']==9500
    transition.assert_not_awaited()


async def test_http_mutation_network_failure_is_not_retried(monkeypatch):
    import httpx
    from app.services.razorpay import client as client_module
    client=client_module.RazorpayClient(max_retries=4)
    monkeypatch.setattr(client_module,'get_settings',lambda:SimpleNamespace(RAZORPAY_KEY_ID='test',RAZORPAY_KEY_SECRET='test'))
    for method in ('_insert_api_call','_finalise_api_call','_update_api_call_attempt'):
        monkeypatch.setattr(client,method,AsyncMock())
    monkeypatch.setattr(client,'_lookup_idempotent',AsyncMock(return_value=None))
    request=AsyncMock(side_effect=httpx.ReadTimeout('unknown result'))
    monkeypatch.setattr(client._client,'request',request)
    try:
        with pytest.raises(client_module.RazorpayError):
            await client.post('/v1/payments/pay_test/transfers',operation='route.transfers.create',json_body={'transfers':[]},idempotency_key='key')
        assert request.await_count==1
        assert 'X-Razorpay-Idempotency' not in request.await_args.kwargs['headers']
    finally:
        await client.aclose()


async def test_settlement_polling_reads_every_page(monkeypatch):
    from app.services.razorpay import settlement_polling_scheduler as scheduler,settlements
    from app.services.razorpay.settlement_service import rzp_settlement_service
    fetch=AsyncMock(side_effect=[{'items':[{'id':'s1'},{'id':'s2'}]},{'items':[{'id':'s3'}]}])
    monkeypatch.setattr(settlements,'list_settlements',fetch)
    upsert=AsyncMock()
    monkeypatch.setattr(rzp_settlement_service,'upsert_from_razorpay',upsert)
    assert await scheduler._pull_settlements_for_merchant(MERCHANT,'acc_test',lookback_days=14,page_size=2)==3
    assert [c.kwargs['skip'] for c in fetch.await_args_list]==[0,2]


async def test_database_tenant_stamp_survives_autocommit_and_pool_reset(postgres,monkeypatch):
    from app.core import database
    pool=await asyncpg.create_pool(postgres,min_size=1,max_size=1)
    monkeypatch.setattr(database,'get_pool',lambda:pool)
    token=database._current_tenant_id.set(MERCHANT)
    try:
        async with database.get_connection() as conn:
            assert await conn.fetchval("SELECT current_setting('app.tenant_id',true)")==MERCHANT
            assert await conn.fetchval("SELECT current_setting('app.rls_bypass',true)")!='on'
        async with database.get_service_connection() as conn:
            assert await conn.fetchval("SELECT current_setting('app.rls_bypass',true)")=='on'
        async with database.get_connection() as conn:
            assert await conn.fetchval("SELECT current_setting('app.tenant_id',true)")==MERCHANT
            assert await conn.fetchval("SELECT current_setting('app.rls_bypass',true)")!='on'
        async with pool.acquire() as conn:
            assert not await conn.fetchval("SELECT current_setting('app.rls_bypass',true)")
    finally:
        database._current_tenant_id.reset(token)
        await pool.close()


async def test_strict_ledger_failure_is_retryable(monkeypatch):
    from app.services import merchant_ledger_integration as integration
    monkeypatch.setattr(integration.merchant_ledger_service,'post_entry',AsyncMock(side_effect=RuntimeError('database failed')))
    with pytest.raises(RuntimeError,match='database failed'):
        await integration.post_payment_received(merchant_id=MERCHANT,payment_id=str(uuid4()),amount=100,strict=True)


async def test_unknown_reversal_blocks_a_later_refund(gateway,db,monkeypatch):
    service,_,_=gateway
    await create(service)
    reverse=AsyncMock(side_effect=TimeoutError('unknown'))
    monkeypatch.setattr(route_service.route_api,'reverse_transfer',reverse)
    async with db() as conn:
        await conn.execute("INSERT INTO rzp_refunds VALUES('r1','pay_test',$1::uuid,2000,'processed')",MERCHANT)
    with pytest.raises(TimeoutError):
        await service.reverse_refunded_share(razorpay_payment_id='pay_test',refund_id='r1')
    async with db() as conn:
        await conn.execute("INSERT INTO rzp_refunds VALUES('r2','pay_test',$1::uuid,2000,'processed')",MERCHANT)
    with pytest.raises(RuntimeError,match='previous reversal'):
        await service.reverse_refunded_share(razorpay_payment_id='pay_test',refund_id='r2')
    assert reverse.await_count==1


async def test_partial_refund_does_not_mark_whole_payment_refunded(db,monkeypatch):
    async with db() as conn:
        await conn.execute('''
            CREATE TYPE rzp_refund_state AS ENUM ('pending','processed','failed');
            CREATE TYPE payment_status AS ENUM ('completed','settled','refunded');
            CREATE TABLE payments(razorpay_payment_id text,amount numeric,status payment_status,updated_at timestamptz);
            INSERT INTO payments VALUES('pay_test',100,'completed',now());
            ALTER TABLE rzp_payments ADD COLUMN id uuid DEFAULT gen_random_uuid();
            CREATE TABLE rzp_payments_index AS SELECT razorpay_payment_id,id AS payment_uuid FROM rzp_payments;
            ALTER TABLE rzp_refunds ALTER COLUMN status TYPE rzp_refund_state USING status::rzp_refund_state;
            ALTER TABLE rzp_refunds ADD COLUMN currency text, ADD COLUMN speed_requested text,
              ADD COLUMN speed_processed text, ADD COLUMN reason text, ADD COLUMN batch_id text,
              ADD COLUMN acquirer_data jsonb, ADD COLUMN notes jsonb, ADD COLUMN raw_payload jsonb,
              ADD COLUMN processed_at timestamptz, ADD COLUMN updated_at timestamptz;
        ''')
    monkeypatch.setattr(webhook_dispatcher,'emit_and_publish',AsyncMock())
    one={'id':'refund1','payment_id':'pay_test','amount':5000}
    await webhook_dispatcher._upsert_rzp_refund(one,{},'processed')
    await webhook_dispatcher._upsert_rzp_refund(one,{},'pending')  # out of order
    async with db() as conn:
        assert await conn.fetchval('SELECT status FROM payments')=='completed'
        assert await conn.fetchval('SELECT status FROM rzp_refunds')=='processed'
    await webhook_dispatcher._upsert_rzp_refund({'id':'refund2','payment_id':'pay_test','amount':5000},{},'processed')
    async with db() as conn:
        assert await conn.fetchval('SELECT status FROM payments')=='refunded'


async def test_refund_client_key_deduplicates_and_rejects_changed_amount(db,monkeypatch):
    from app.services.refund_service import RefundService
    service=RefundService()
    send=AsyncMock(return_value={'id':1,'status':'processing'})
    monkeypatch.setattr(service,'_create_and_dispatch_once',send)
    kwargs=dict(request_key='client-request-1',merchant_id=MERCHANT,payment_id='pay_test',amount='10.00')
    results=await asyncio.gather(service.create_and_dispatch(**kwargs),service.create_and_dispatch(**kwargs))
    assert results[0]==results[1]
    assert send.await_count==1
    with pytest.raises(ValueError,match='different request'):
        await service.create_and_dispatch(**{**kwargs,'amount':'20.00'})
    assert send.await_count==1


async def test_refund_statement_entries_recover_rounding_on_last_refund(db):
    from app.services.razorpay.merchant_finance_service import _refund_allocations
    async with db() as conn:
        await conn.execute('ALTER TABLE rzp_refunds ADD COLUMN created_at timestamptz DEFAULT now(), ADD COLUMN processed_at timestamptz')
        await conn.execute("INSERT INTO rzp_route_transfers VALUES('trf_old','pay_test',$1::uuid,9500,'reversed')",MERCHANT)
        for index,amount in enumerate([3333,3333,3334]):
            await conn.execute("INSERT INTO rzp_refunds(refund_id,razorpay_payment_id,merchant_id,amount_paise,status) VALUES($1,'pay_test',$2::uuid,$3,'processed')",f'r{index}',MERCHANT,amount)
        rows=await _refund_allocations(conn,MERCHANT)
        assert [r['merchant_refund_share'] for r in rows]==[3166,3166,3168]
        assert sum(r['merchant_refund_share'] for r in rows)==9500
