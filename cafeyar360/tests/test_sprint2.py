from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import app as server
from test_flow import client, auth, new_order


def complete(c,oid):
    for state in ('accepted','preparing','ready','completed'):
        r=c.patch(f'/api/orders/{oid}/status',json={'status':state})
        assert r.status_code==200,r.text


def test_advance_is_not_profit_and_receivables_use_completed_cost(client):
    auth(client)
    before=client.get('/api/finance').json()['summary']
    order=new_order(client,key='advance-vs-sales')
    assert client.post(f"/api/orders/{order['id']}/pay",json={'method':'cash'}).status_code==200
    prepaid=client.get('/api/finance').json()['summary']
    assert prepaid['revenue']==before['revenue']
    assert prepaid['gross_profit']==before['gross_profit']
    assert prepaid['cash_received']==before['cash_received']+order['total']
    assert prepaid['advances']==before['advances']+order['total']
    complete(client,order['id'])
    delivered=client.get('/api/finance').json()['summary']
    cost=72*1900+440*120
    assert delivered['revenue']==before['revenue']+order['total']
    assert delivered['cogs']==before['cogs']+cost
    assert delivered['advances']==before['advances']
    unpaid=new_order(client,key='unpaid-delivered-order')
    complete(client,unpaid['id'])
    final=client.get('/api/finance').json()['summary']
    assert final['revenue']==delivered['revenue']+unpaid['total']
    assert final['cogs']==delivered['cogs']+cost
    assert final['receivables']==delivered['receivables']+unpaid['total']
    assert final['cash_received']==delivered['cash_received']


def test_report_period_matches_completion_and_payment_dates(client,monkeypatch):
    auth(client)
    monkeypatch.setattr(server,'now',lambda:'2024-01-31T23:00:00+03:30')
    order=new_order(client,key='cross-month-order')
    client.post(f"/api/orders/{order['id']}/pay",json={'method':'pos'})
    monkeypatch.setattr(server,'now',lambda:'2024-02-01T01:00:00+03:30')
    complete(client,order['id'])
    client.post('/api/finance/expenses',json={'title':'هزینه آزمایشی','amount':1000})
    january=client.get('/api/finance?start=2024-01-01&end=2024-01-31').json()
    assert january['summary']['cash_received']==order['total']
    assert january['summary']['revenue']==0 and january['summary']['cogs']==0
    feb=client.get('/api/finance?start=2024-02-01&end=2024-02-01').json()
    assert feb['summary']['revenue']==order['total'] and feb['summary']['cash_received']==0
    assert feb['summary']['operating_expenses']==1000
    assert feb['summary']['average_order_value']==order['total']
    assert feb['monthly'][0]['month']=='1402-11' and feb['monthly'][0]['sales']==order['total']
    analytics=client.get('/api/analytics?start=2024-02-01&end=2024-02-01').json()
    assert analytics['trend']==[{'day':'2024-02-01','orders':1,'sales':order['total']}]
    assert sum(x['revenue'] for x in analytics['top'])==order['total']
    assert client.get('/api/finance?start=2024-02-02&end=2024-02-01').status_code==422
    assert client.get('/api/analytics?start=invalid').status_code==422
    empty=client.get('/api/analytics?start=1900-01-01&end=1900-01-31').json()
    assert empty['summary']['average_order_value']==0 and empty['trend']==[]


def test_receipt_roles_snapshot_escaping_and_payment_actor(client):
    auth(client)
    name='<script>alert(1)</script>'
    client.put('/api/admin/products/2',json={'name':name,'price':135000,'category_id':1})
    order=new_order(client,key='receipt-escaping-test')
    assert client.get(f"/api/orders/{order['id']}/receipt").status_code==409
    client.post('/api/auth/logout');auth(client,'cashier')
    assert client.post(f"/api/orders/{order['id']}/pay",json={'method':'pos','reference':'<b>TEST</b>'}).status_code==200
    response=client.get(f"/api/orders/{order['id']}/receipt")
    receipt=response.json()
    assert response.headers['cache-control']=='no-store'
    assert receipt['payment']['actor']=='cashier'
    assert receipt['items'][0]['name']==name
    assert sum(x['line_total'] for x in receipt['items'])==receipt['payment']['amount']==order['total']
    html=client.get(f"/receipts/{order['id']}")
    assert html.status_code==200 and '&lt;script&gt;' in html.text
    assert '<script>alert(1)</script>' not in html.text and '&lt;b&gt;TEST&lt;/b&gt;' in html.text
    assert "frame-ancestors 'none'" in html.headers['content-security-policy']
    client.post('/api/auth/logout');auth(client,'barista')
    assert client.get(f"/receipts/{order['id']}").status_code==403
    assert client.get(f"/api/orders/{order['id']}/receipt").status_code==403
    client.post('/api/auth/logout')
    assert client.get(f"/receipts/{order['id']}").status_code==401


def test_correction_audits_old_values_and_rejects_stale_revision(client):
    order=new_order(client,key='correction-ledger-test')
    auth(client,'cashier')
    client.post(f"/api/orders/{order['id']}/pay",json={'method':'cash','reference':'old'})
    receipt=client.get(f"/api/orders/{order['id']}/receipt").json();pid=receipt['payment']['id']
    body={'method':'pos','reference':'new','reason':'اصلاح آزمایشی روش','expected_revision':0}
    assert client.patch(f'/api/payments/{pid}/correction',json=body).status_code==403
    client.post('/api/auth/logout');auth(client)
    before=client.get('/api/finance').json()['summary']
    assert client.patch(f'/api/payments/{pid}/correction',json=body).status_code==200
    updated=client.get(f"/api/orders/{order['id']}/receipt").json()
    assert updated['receipt_number']==receipt['receipt_number']
    assert updated['payment']['amount']==order['total'] and updated['payment']['revision']==1
    change=updated['corrections'][0]
    assert (change['old_method'],change['new_method'],change['old_reference'],change['new_reference'],change['actor'])==('cash','pos','old','new','admin')
    assert client.patch(f'/api/payments/{pid}/correction',json=body).status_code==409
    assert client.patch(f'/api/payments/{pid}/correction',json={**body,'expected_revision':1}).status_code==409
    assert client.get('/api/finance').json()['summary']==before


def test_discrepancy_is_audited_idempotent_and_does_not_move_money(client):
    order=new_order(client,key='discrepancy-order')
    auth(client)
    client.post(f"/api/orders/{order['id']}/pay",json={'method':'cash'})
    pid=client.get(f"/api/orders/{order['id']}/receipt").json()['payment']['id']
    before=client.get('/api/finance').json()['summary']
    body={'reported_amount':order['total']-10000,'reason':'کسری آزمایشی صندوق','idempotency_key':'discrepancy-retry-key'}
    client.post('/api/auth/logout');auth(client,'cashier')
    r=client.post(f'/api/payments/{pid}/discrepancies',json=body);assert r.status_code==201
    did=r.json()['id']
    assert client.post(f'/api/payments/{pid}/discrepancies',json=body).json()=={'id':did,'duplicate':True}
    assert client.post(f'/api/payments/{pid}/discrepancies',json={**body,'reported_amount':1}).status_code==409
    assert client.patch(f'/api/cashier/discrepancies/{did}/resolve',json={'resolution':'بررسی آزمایشی'}).status_code==403
    row=client.get('/api/cashier/discrepancies').json()[0]
    assert row['reported_amount']-row['expected_amount']==-10000 and row['actor']=='cashier'
    client.post('/api/auth/logout');auth(client)
    assert client.patch(f'/api/cashier/discrepancies/{did}/resolve',json={'resolution':'تطبیق رسید انجام شد'}).status_code==200
    assert client.patch(f'/api/cashier/discrepancies/{did}/resolve',json={'resolution':'تلاش دوباره'}).status_code==409
    row=client.get('/api/cashier/discrepancies').json()[0]
    assert row['resolved_by']=='admin' and row['reason']==body['reason'] and row['status']=='resolved'
    assert client.get('/api/finance').json()['summary']==before


def test_failed_correction_rolls_back_audit_and_payment(client):
    order=new_order(client,key='correction-rollback')
    auth(client);client.post(f"/api/orders/{order['id']}/pay",json={'method':'cash'})
    pid=client.get(f"/api/orders/{order['id']}/receipt").json()['payment']['id']
    with server.db() as c:
        c.execute("CREATE TRIGGER fail_payment_update BEFORE UPDATE ON payments BEGIN SELECT RAISE(ABORT,'simulated failure'); END")
    assert client.patch(f'/api/payments/{pid}/correction',json={'method':'pos','reason':'تست تراکنش','expected_revision':0}).status_code==409
    receipt=client.get(f"/api/orders/{order['id']}/receipt").json()
    assert receipt['payment']['method']=='cash' and receipt['payment']['revision']==0 and receipt['corrections']==[]


def test_concurrent_correction_allows_only_one_revision(client):
    order=new_order(client,key='concurrent-correction')
    auth(client);client.post(f"/api/orders/{order['id']}/pay",json={'method':'cash'})
    pid=client.get(f"/api/orders/{order['id']}/receipt").json()['payment']['id'];cookies=dict(client.cookies)
    def correct(n):
        with TestClient(server.app) as other:
            other.cookies.update(cookies)
            return other.patch(f'/api/payments/{pid}/correction',json={'method':'pos','reference':str(n),'reason':'اصلاح هم‌زمان آزمایشی','expected_revision':0}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(correct,range(2)))==[200,409]
    assert len(client.get(f"/api/orders/{order['id']}/receipt").json()['corrections'])==1


def test_cashier_daily_total_has_no_history_limit(client,monkeypatch):
    auth(client);monkeypatch.setattr(server,'now',lambda:'2024-03-01T12:00:00+03:30')
    with server.db() as c:
        c.execute("UPDATE payments SET created_at='2024-02-01T12:00:00+03:30'")
    order=new_order(client,key='old-paid-daily-total')
    client.post(f"/api/orders/{order['id']}/pay",json={'method':'cash'})
    for i in range(101):new_order(client,key=f'new-unpaid-summary-{i}')
    assert order['id'] not in {o['id'] for o in client.get('/api/orders').json()}
    summary=client.get('/api/cashier/summary').json()
    assert summary['received_today']==order['total'] and summary['payments_today']==1


def test_payment_migration_keeps_legacy_payments(client):
    with server.db() as c:
        original=[tuple(r) for r in c.execute('SELECT id,order_id,amount,method FROM payments ORDER BY id')]
        c.execute('ALTER TABLE payments DROP COLUMN actor_id')
        c.execute('ALTER TABLE payments DROP COLUMN revision')
        c.execute('DELETE FROM schema_migrations WHERE version=2')
    server.init_db();server.init_db()
    with server.db() as c:
        assert original==[tuple(r) for r in c.execute('SELECT id,order_id,amount,method FROM payments ORDER BY id')]
        assert c.execute('SELECT COUNT(*) FROM schema_migrations WHERE version=2').fetchone()[0]==1
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_financial_input_validation_and_staff_permissions(client):
    order=new_order(client,key='financial-validation')
    auth(client);client.post(f"/api/orders/{order['id']}/pay",json={'method':'cash'})
    pid=client.get(f"/api/orders/{order['id']}/receipt").json()['payment']['id']
    assert client.patch(f'/api/payments/{pid}/correction',json={'method':'online','reason':'تست نامعتبر','expected_revision':0}).status_code==422
    assert client.patch(f'/api/payments/{pid}/correction',json={'method':'pos','reason':'   ','expected_revision':0}).status_code==422
    base={'reported_amount':order['total'],'reason':'تست ورودی','idempotency_key':'validate-discrepancy'}
    assert client.post(f'/api/payments/{pid}/discrepancies',json=base).status_code==422
    assert client.post(f'/api/payments/{pid}/discrepancies',json={**base,'reported_amount':-1}).status_code==422
    assert client.get('/api/orders/999999/receipt').status_code==404
    client.post('/api/auth/logout');auth(client,'barista')
    assert client.get('/api/cashier/summary').status_code==403
    assert client.get('/api/cashier/discrepancies').status_code==403
    assert client.post(f'/api/payments/{pid}/discrepancies',json={**base,'reported_amount':1}).status_code==403
    assert client.patch(f'/api/payments/{pid}/correction',json={'method':'pos','reason':'تست مجوز','expected_revision':0}).status_code==403

# Complete sprint: shifts, refunds and coordinated exceptions.
from decimal import Decimal
from datetime import date
import pytest


def active_shift(c):
    return c.get('/api/cashier/shifts').json()['active']


def finish_shift(c):
    shift=active_shift(c)
    result=c.post(f"/api/cashier/shifts/{shift['id']}/close",json={'counted_amount':str(Decimal(shift['expected_minor'])/100)})
    assert result.status_code==200,result.text
    return result.json()


def settle(c,key='refund-subject',method='cash'):
    order=new_order(c,key=key)
    r=c.post(f"/api/orders/{order['id']}/pay",json={'method':method})
    assert r.status_code==200,r.text
    return order,c.get(f"/api/orders/{order['id']}/receipt").json()['payment']


def refund(c,payment,amount,key='refund-test-key',**extra):
    return c.post(f"/api/payments/{payment['id']}/refunds",json={'amount':str(amount),'method':'cash','reason':'استرداد آزمایشی','expected_revision':payment['revision'],'idempotency_key':key,**extra})


def test_shift_required_and_open_close_idempotency(client):
    auth(client);finish_shift(client)
    order=new_order(client,key='no-open-shift-test')
    assert client.post(f"/api/orders/{order['id']}/pay",json={'method':'cash'}).status_code==409
    body={'opening_amount':'1000.25','idempotency_key':'open-shift-idempotent'}
    first=client.post('/api/cashier/shifts',json=body)
    assert first.status_code==201
    sid=first.json()['id']
    assert client.post('/api/cashier/shifts',json=body).json()['id']==sid
    assert client.post('/api/cashier/shifts',json={**body,'idempotency_key':'second-open-attempt'}).status_code==409
    assert client.post(f'/api/cashier/shifts/{sid}/close',json={'counted_amount':'1000'}).status_code==422
    close={'counted_amount':'1000','note':'اختلاف شمارش آزمایشی'}
    result=client.post(f'/api/cashier/shifts/{sid}/close',json=close).json()
    assert result['expected_minor']==100025 and result['difference_minor']==-25
    assert client.post(f'/api/cashier/shifts/{sid}/close',json=close).json()==result
    assert client.post(f'/api/cashier/shifts/{sid}/close',json={**close,'counted_amount':'900'}).status_code==409


def test_shift_tenders_cash_expense_and_no_double_opening_effect(client):
    auth(client);finish_shift(client)
    client.post('/api/cashier/shifts',json={'opening_amount':1000,'idempotency_key':'expense-shift-open'})
    a,p=settle(client,key='cash-tender-order')
    b,_=settle(client,key='pos-tender-order',method='pos')
    client.post('/api/finance/expenses',json={'title':'پرداخت از صندوق','amount':'10.25','from_drawer':True})
    client.post('/api/finance/expenses',json={'title':'پرداخت بیرون صندوق','amount':500})
    r=refund(client,p,'20.50');assert r.status_code==201
    shift=active_shift(client)
    assert shift['expected_minor']==100000+a['total']*100-1025-2050
    assert shift['pos_net_minor']==b['total']*100
    closed=finish_shift(client)
    client.post('/api/cashier/shifts',json={'opening_amount':'250','idempotency_key':'new-counted-opening'})
    assert active_shift(client)['expected_minor']==25000
    old=client.get('/api/cashier/shifts').json()['history'][1]
    assert old==closed


def test_partial_before_completion_recognizes_discount_on_delivery(client):
    auth(client);order,pay=settle(client,key='pre-delivery-compensation')
    before=client.get('/api/finance').json()['summary']
    r=refund(client,pay,10000);assert r.status_code==201
    state=next(o for o in client.get('/api/orders').json() if o['id']==order['id'])
    assert state['status']=='new' and state['payment_status']=='partially_refunded'
    after=client.get('/api/finance').json()['summary']
    assert after['revenue']==before['revenue']
    assert after['advances']==before['advances']-10000
    complete(client,order['id'])
    delivered=client.get('/api/finance').json()['summary']
    assert delivered['revenue']==before['revenue']+order['total']-10000
    assert delivered['cogs']==before['cogs']+189600
    assert delivered['sales_returns']==10000
    receipt=client.get(f"/api/orders/{order['id']}/receipt").json()
    assert receipt['refunded_minor']==1000000
    assert receipt['remaining_refundable_minor']==(order['total']-10000)*100


@pytest.mark.parametrize('stage',['new','accepted','preparing','ready','completed'])
def test_full_refund_at_every_stage_stock_and_waste(client,stage):
    auth(client);order,pay=settle(client,key='refund-stage-'+stage)
    for status in ['accepted','preparing','ready','completed']:
        if stage=='new':break
        assert client.patch(f"/api/orders/{order['id']}/status",json={'status':status}).status_code==200
        if status==stage:break
    stock={r['id']:r['stock'] for r in client.get('/api/inventory').json()['ingredients']}
    before=client.get('/api/finance').json()['summary']
    result=refund(client,pay,order['total']);assert result.status_code==201,result.text
    after=client.get('/api/finance').json()['summary']
    row=next(o for o in client.get('/api/orders').json() if o['id']==order['id'])
    assert row['payment_status']=='refunded'
    assert row['status']==('completed' if stage=='completed' else 'cancelled')
    after_stock={r['id']:r['stock'] for r in client.get('/api/inventory').json()['ingredients']}
    if stage in ('preparing','ready'):
        assert after_stock[1]==stock[1]-72 and after_stock[2]==stock[2]-440
        assert after['waste_cost']-before['waste_cost']==189600
    else:assert after_stock==stock
    assert after['cogs']==before['cogs']
    assert after['revenue']==before['revenue']-(order['total'] if stage=='completed' else 0)
    assert after['cash_refunded']==before['cash_refunded']+order['total']
    # A retry must not refund or consume twice, even after cancellation.
    assert refund(client,pay,order['total']).json()['duplicate'] is True
    assert {r['id']:r['stock'] for r in client.get('/api/inventory').json()['ingredients']}==after_stock


def test_refund_limits_stale_revision_and_printable_history(client):
    auth(client);order,pay=settle(client,key='refund-revision-limits');complete(client,order['id'])
    one=refund(client,pay,10000);assert one.status_code==201
    assert refund(client,pay,20000,key='stale-refund-body').status_code==409
    current=client.get(f"/api/orders/{order['id']}/receipt").json()['payment']
    assert refund(client,current,order['total'],key='over-refund-body').status_code==409
    assert refund(client,current,order['total']-10000,key='remaining-refund-body').status_code==201
    html=client.get(f"/receipts/{order['id']}?refund_id={one.json()['id']}")
    assert html.status_code==200 and 'CY360-R' in html.text
    assert client.get(f"/receipts/{order['id']}?refund_id=999999").status_code==404
    assert client.get('/api/cashier/payments?payment_status=refunded').json()['items'][0]['id']==order['id']
    client.post('/api/auth/logout');auth(client,'cashier')
    assert refund(client,current,1,key='cashier-direct-refund').status_code==403


def test_concurrent_refunds_cannot_exceed_remaining(client):
    auth(client);order,pay=settle(client,key='concurrent-refund-order');cookies=dict(client.cookies)
    def run(n):
        with TestClient(server.app) as other:
            other.cookies.update(cookies)
            return refund(other,pay,order['total'],key=f'concurrent-refund-{n}').status_code
    with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(run,range(2)))==[201,409]
    with server.db() as c:
        assert c.execute('SELECT SUM(amount_minor) FROM refunds WHERE payment_id=?',(pay['id'],)).fetchone()[0]==order['total']*100


def test_failed_refund_rolls_back_cash_stock_status_and_audit(client):
    auth(client);order,pay=settle(client,key='refund-failure-order')
    for state in ('accepted','preparing'):client.patch(f"/api/orders/{order['id']}/status",json={'status':state})
    before=active_shift(client);stock=client.get('/api/inventory').json()['ingredients']
    with server.db() as c:
        c.execute("CREATE TRIGGER fail_cancel BEFORE UPDATE OF status ON orders WHEN NEW.status='cancelled' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    assert refund(client,pay,order['total']).status_code==409
    assert active_shift(client)==before
    assert client.get('/api/inventory').json()['ingredients']==stock
    assert client.get(f"/api/payments/{pay['id']}/refunds").json()==[]
    receipt=client.get(f"/api/orders/{order['id']}/receipt").json()
    assert receipt['order']['status']=='preparing' and receipt['payment']['revision']==0


def test_direct_admin_exception_and_cashier_approval_outside_shift(client):
    auth(client);finish_shift(client)
    a=new_order(client,key='direct-exception-order')
    authorization={'coordination':'in_person','reference':'مدیر آزمایشی','reason':'هماهنگی حضوری آزمایشی'}
    assert client.post(f"/api/orders/{a['id']}/pay",json={'method':'cash','authorization':authorization}).status_code==200
    assert active_shift(client) is None
    assert len(client.get('/api/cashier/exceptions').json())==1
    client.post('/api/auth/logout');auth(client,'cashier')
    b=new_order(client,key='pending-request-payment')
    assert client.post(f"/api/orders/{b['id']}/pay",json={'method':'cash','authorization':authorization}).status_code==409
    body={'kind':'payment','target_id':b['id'],'payload':{'method':'pos'},'coordination':'ticket','reference':'TICKET-TEST-1','reason':'درخواست هماهنگی آزمایشی','idempotency_key':'exception-request-key'}
    request=client.post('/api/cashier/requests',json=body);assert request.status_code==201
    rid=request.json()['id']
    assert client.post('/api/cashier/requests',json=body).json()['id']==rid
    assert client.get(f"/api/orders/{b['id']}/receipt").status_code==409
    review={'decision':'approved','resolution':'تأیید تیکت آزمایشی'}
    assert client.patch(f'/api/cashier/requests/{rid}/review',json=review).status_code==403
    client.post('/api/auth/logout');auth(client)
    preview=next(row for row in client.get('/api/cashier/requests').json() if row['id']==rid)
    assert preview['requested_amount_minor']==b['total']*100 and preview['order_id']==b['id'] and preview['requested_method']=='pos'
    assert client.patch(f'/api/cashier/requests/{rid}/review',json=review).status_code==200
    assert client.patch(f'/api/cashier/requests/{rid}/review',json=review).status_code==200
    assert active_shift(client) is None
    with server.db() as c:
        assert c.execute('SELECT COUNT(*) FROM payments WHERE order_id=?',(b['id'],)).fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM financial_exceptions').fetchone()[0]==2
    client.post('/api/cashier/shifts',json={'opening_amount':500,'idempotency_key':'after-exception-opening'})
    assert active_shift(client)['expected_minor']==50000  # already-counted cash is not added twice


def test_pending_refund_request_approval_rejection_and_stale_revalidation(client):
    auth(client);order,pay=settle(client,key='request-refund-order')
    client.post('/api/auth/logout');auth(client,'cashier')
    payload={'amount':'1000','method':'cash','reason':'استرداد با تیکت','expected_revision':0,'idempotency_key':'request-refund-apply'}
    body={'kind':'refund','target_id':pay['id'],'payload':payload,'coordination':'ticket','reference':'TICKET-REFUND','reason':'درخواست استرداد آزمایشی','idempotency_key':'request-refund-create'}
    r=client.post('/api/cashier/requests',json=body);rid=r.json()['id']
    assert client.get(f"/api/payments/{pay['id']}/refunds").json()==[]
    client.post('/api/auth/logout');auth(client)
    assert client.patch(f'/api/cashier/requests/{rid}/review',json={'decision':'approved','resolution':'تأیید مدیر آزمایشی'}).status_code==200
    assert len(client.get(f"/api/payments/{pay['id']}/refunds").json())==1
    stale=client.post('/api/cashier/requests',json={**body,'idempotency_key':'stale-request-create','payload':{**payload,'idempotency_key':'stale-request-apply'}}).json()['id']
    assert client.patch(f'/api/cashier/requests/{stale}/review',json={'decision':'approved','resolution':'تأیید مجدد آزمایشی'}).status_code==409
    assert next(r for r in client.get('/api/cashier/requests').json() if r['id']==stale)['status']=='pending'
    assert client.patch(f'/api/cashier/requests/{stale}/review',json={'decision':'rejected','resolution':'رد به دلیل قدیمی بودن نسخه'}).status_code==200
    assert len(client.get(f"/api/payments/{pay['id']}/refunds").json())==1


def test_closed_shift_reclassification_does_not_rewrite_cash(client):
    auth(client);order,pay=settle(client,key='closed-shift-correction')
    closed=finish_shift(client)
    client.post('/api/cashier/shifts',json={'opening_amount':250,'idempotency_key':'correction-next-shift'})
    assert client.patch(f"/api/payments/{pay['id']}/correction",json={'method':'pos','reference':'corrected','reason':'اصلاح شیفت قبلی','expected_revision':0}).status_code==200
    assert active_shift(client)['expected_minor']==25000
    assert next(r for r in client.get('/api/cashier/shifts').json()['history'] if r['id']==closed['id'])==closed


def test_exact_costs_expenses_and_money_validation(client):
    auth(client)
    assert client.post('/api/finance/expenses',json={'title':'هزینه دقیق','amount':'0.125'}).status_code==422
    assert client.post('/api/finance/expenses',json={'title':'هزینه دقیق','amount':'0.12'}).status_code==200
    item={'name':'ماده اعشاری','unit':'گرم','stock':10,'unit_cost':'0.15','reorder':0}
    iid=client.post('/api/inventory/ingredients',json=item).json()['id']
    assert client.put('/api/inventory/recipes/2',json={'parts':[{'ingredient_id':iid,'quantity':'0.1'}]}).status_code==200
    r=client.post('/api/orders',json={'idempotency_key':'decimal-cost-test','fulfillment':'takeaway','payment_method':'cash','items':[{'product_id':2,'qty':1}]}).json()
    complete(client,r['id'])
    with server.db() as c:
        assert c.execute('SELECT cost_total_minor FROM orders WHERE id=?',(r['id'],)).fetchone()[0]==2  # .015 -> .02 once, HALF_UP
        assert c.execute('SELECT amount_minor FROM expenses WHERE title=?',('هزینه دقیق',)).fetchone()[0]==12
    assert client.put('/api/inventory/recipes/2',json={'parts':[{'ingredient_id':iid,'quantity':'0.0000001'}]}).status_code==422


def test_tehran_nowruz_week_and_month_boundaries(client,monkeypatch):
    auth(client)
    monkeypatch.setattr(server,'now',lambda:'2025-03-20T20:29:00+00:00')  # 1403/12/30 23:59 Tehran
    a,_=settle(client,key='before-nowruz-order');complete(client,a['id'])
    monkeypatch.setattr(server,'now',lambda:'2025-03-20T20:31:00+00:00')  # 1404/01/01 Tehran
    b,pay=settle(client,key='after-nowruz-order');complete(client,b['id'])
    march=client.get('/api/finance?start=2025-03-20&end=2025-03-21').json()
    assert {r['month'] for r in march['monthly']}=={'1403-12','1404-01'}
    assert [r['day'] for r in march['sales_daily']]==['2025-03-20','2025-03-21']
    assert march['calendar']=='jalali' and march['timezone']=='Asia/Tehran'
    monkeypatch.setattr(server,'now',lambda:'2025-03-22T12:00:00+03:30')  # Saturday, new week
    refund(client,pay,1000)
    report=client.get('/api/finance?start=2025-03-20&end=2025-03-22').json()
    assert [r['week'] for r in report['weekly']]==['2025-03-15','2025-03-22']
    assert report['weekly'][1]['sales']==-1000 and report['weekly'][1]['orders']==0
    assert report['summary']['revenue']==a['total']+b['total']-1000


def test_sprint2_additive_migration_preserves_legacy_amounts_and_identifiers(client):
    with server.db() as c:
        c.execute('UPDATE expenses SET amount_minor=NULL WHERE id=1')
        c.execute('UPDATE orders SET cost_total_minor=NULL WHERE id=1')
        original=tuple(c.execute('SELECT id,code,total,cost_total FROM orders WHERE id=1').fetchone())
        c.execute('DELETE FROM schema_migrations WHERE version=3')
    server.init_db();server.init_db()
    with server.db() as c:
        assert tuple(c.execute('SELECT id,code,total,cost_total FROM orders WHERE id=1').fetchone())==original
        assert c.execute('SELECT cost_total_minor FROM orders WHERE id=1').fetchone()[0]==server.money_minor(original[3])
        assert c.execute('SELECT COUNT(*) FROM schema_migrations WHERE version=3').fetchone()[0]==1
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_concurrent_shift_opening_has_one_winner(client):
    auth(client);finish_shift(client);cookies=dict(client.cookies)
    def opening(n):
        with TestClient(server.app) as other:
            other.cookies.update(cookies)
            return other.post('/api/cashier/shifts',json={'opening_amount':0,'idempotency_key':f'concurrent-shift-{n}'}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(opening,range(2)))==[201,409]
    with server.db() as c:assert c.execute('SELECT COUNT(*) FROM cashier_shifts WHERE closed_at IS NULL').fetchone()[0]==1


def test_close_payment_race_never_books_into_closed_shift(client):
    auth(client);order=new_order(client,key='payment-close-race');sid=active_shift(client)['id'];cookies=dict(client.cookies)
    def action(n):
        with TestClient(server.app) as other:
            other.cookies.update(cookies)
            if n==0:return other.post(f"/api/orders/{order['id']}/pay",json={'method':'cash'}).status_code
            return other.post(f'/api/cashier/shifts/{sid}/close',json={'counted_amount':0,'note':'تست هم‌زمانی'}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        payment,closed=list(pool.map(action,range(2)))
    assert closed==200 and payment in (200,409)
    with server.db() as c:
        row=c.execute('SELECT * FROM cashier_shifts WHERE id=?',(sid,)).fetchone()
        actual=c.execute("SELECT COALESCE(SUM(amount_minor),0) FROM cash_movements WHERE shift_id=? AND method='cash'",(sid,)).fetchone()[0]
        assert row['expected_minor']==row['opening_minor']+actual


def test_approval_failure_leaves_pending_and_no_exception_money(client):
    auth(client);finish_shift(client);order=new_order(client,key='approval-rollback-order')
    request={'kind':'payment','target_id':order['id'],'payload':{'method':'cash'},'coordination':'ticket','reference':'ROLLBACK-TEST','reason':'تست خرابی تأیید','idempotency_key':'rollback-approval-key'}
    rid=client.post('/api/cashier/requests',json=request).json()['id']
    with server.db() as c:c.execute("CREATE TRIGGER fail_request_review BEFORE UPDATE ON financial_requests BEGIN SELECT RAISE(ABORT,'injected'); END")
    assert client.patch(f'/api/cashier/requests/{rid}/review',json={'decision':'approved','resolution':'تأیید آزمایشی'}).status_code==409
    with server.db() as c:
        assert c.execute('SELECT status FROM financial_requests WHERE id=?',(rid,)).fetchone()[0]=='pending'
        assert c.execute('SELECT COUNT(*) FROM payments WHERE order_id=?',(order['id'],)).fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM financial_exceptions').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM cash_movements').fetchone()[0]==0


def test_staff_cannot_access_shifts_requests_or_refunds(client):
    auth(client,'barista')
    assert client.get('/api/cashier/shifts').status_code==403
    assert client.get('/api/cashier/requests').status_code==403
    assert client.get('/api/cashier/exceptions').status_code==403
    assert client.post('/api/cashier/shifts',json={'opening_amount':0,'idempotency_key':'unauthorized-shift'}).status_code==403
    assert client.get('/api/payments/1/refunds').status_code==403


def test_refund_decimal_retry_is_canonical_and_insufficient_cash_rolls_back(client):
    auth(client);order,pay=settle(client,key='decimal-refund-retry')
    assert refund(client,pay,'1000.0').status_code==201
    assert refund(client,pay,'1000.00').json()['duplicate'] is True
    finish_shift(client)
    client.post('/api/cashier/shifts',json={'opening_amount':0,'idempotency_key':'no-cash-refund-shift'})
    current=client.get(f"/api/orders/{order['id']}/receipt").json()['payment']
    assert refund(client,current,'1',key='insufficient-drawer').status_code==409
    assert len(client.get(f"/api/payments/{pay['id']}/refunds").json())==1


def test_refund_in_next_period_keeps_prior_gross_sales_and_cost(client,monkeypatch):
    auth(client);monkeypatch.setattr(server,'now',lambda:'2024-01-31T12:00:00+03:30')
    order,pay=settle(client,key='cross-period-refund');complete(client,order['id'])
    before=client.get('/api/finance?start=2024-01-01&end=2024-01-31').json()['summary']
    monkeypatch.setattr(server,'now',lambda:'2024-02-01T12:00:00+03:30');refund(client,pay,order['total'])
    assert client.get('/api/finance?start=2024-01-01&end=2024-01-31').json()['summary']==before
    feb=client.get('/api/finance?start=2024-02-01&end=2024-02-01').json()['summary']
    assert feb['gross_sales']==0 and feb['sales_returns']==order['total'] and feb['revenue']==-order['total']
    assert feb['cogs']==0 and feb['cash_refunded']==order['total']


def test_payment_history_pagination_has_no_gaps(client):
    auth(client)
    first=client.get('/api/cashier/payments?limit=10').json()
    second=client.get('/api/cashier/payments?limit=10&offset=10').json()
    assert first['total']==second['total']==24
    assert not {o['id'] for o in first['items']}&{o['id'] for o in second['items']}
    assert client.get('/api/cashier/payments?payment_status=wrong').status_code==422


def test_drawer_expense_and_purchase_fail_atomically_when_cash_is_missing(client):
    auth(client)
    with server.db() as c:count=c.execute('SELECT COUNT(*) FROM expenses').fetchone()[0]
    stock=client.get('/api/inventory').json()['ingredients'][0]
    assert client.post('/api/finance/expenses',json={'title':'هزینه بدون وجه','amount':1,'from_drawer':True}).status_code==409
    assert client.post(f"/api/inventory/{stock['id']}/restock",json={'quantity':1,'unit_cost':1,'from_drawer':True}).status_code==409
    after=client.get('/api/inventory').json()['ingredients'][0]
    assert (after['stock'],after['unit_cost_minor'])==(stock['stock'],stock['unit_cost_minor'])
    with server.db() as c:assert c.execute('SELECT COUNT(*) FROM expenses').fetchone()[0]==count


def test_cashier_can_request_correction_but_only_owner_or_admin_closes_shift(client):
    auth(client);order,pay=settle(client,key='cashier-correction-request')
    sid=active_shift(client)['id']
    client.post('/api/auth/logout');auth(client,'cashier')
    assert client.post(f'/api/cashier/shifts/{sid}/close',json={'counted_amount':order['total']}).status_code==403
    request={'kind':'correction','target_id':pay['id'],'payload':{'method':'pos','reason':'اصلاح با هماهنگی','expected_revision':0},'coordination':'in_person','reference':'مدیر آزمایشی','reason':'هماهنگی اصلاح روش','idempotency_key':'cashier-correction-request'}
    rid=client.post('/api/cashier/requests',json=request).json()['id']
    assert client.get(f"/api/orders/{order['id']}/receipt").json()['payment']['method']=='cash'
    client.post('/api/auth/logout');auth(client)
    assert client.patch(f'/api/cashier/requests/{rid}/review',json={'decision':'approved','resolution':'تأیید روش درست'}).status_code==200
    assert active_shift(client)['expected_minor']==0
    assert active_shift(client)['pos_net_minor']==order['total']*100


def test_partial_then_full_refund_before_delivery_never_recognizes_sales(client):
    auth(client);order,pay=settle(client,key='partial-full-before-delivery')
    before=client.get('/api/finance').json()['summary']
    assert refund(client,pay,1000,key='first-compensation-key').status_code==201
    for state in ('accepted','preparing'):client.patch(f"/api/orders/{order['id']}/status",json={'status':state})
    pay=client.get(f"/api/orders/{order['id']}/receipt").json()['payment']
    assert refund(client,pay,order['total']-1000,key='finish-compensation-key').status_code==201
    after=client.get('/api/finance').json()['summary']
    assert after['revenue']==before['revenue'] and after['sales_returns']==before['sales_returns']
    assert after['advances']==before['advances']-order['total']
    assert after['waste_cost']==before['waste_cost']+189600


def test_pending_requests_are_private_to_requester_and_have_no_money_effect(client):
    auth(client);order=new_order(client,key='private-coordination-request')
    packet={'kind':'payment','target_id':order['id'],'payload':{'method':'cash'},'coordination':'ticket','reference':'PRIVATE-TEST-TICKET','reason':'درخواست داخلی آزمایشی','idempotency_key':'private-request-key'}
    rid=client.post('/api/cashier/requests',json=packet).json()['id']
    client.post('/api/auth/logout');auth(client,'cashier')
    assert rid not in {r['id'] for r in client.get('/api/cashier/requests').json()}
    assert client.get(f"/api/orders/{order['id']}/receipt").status_code==409
    assert client.post('/api/cashier/requests',json={**packet,'reference':'   ','idempotency_key':'bad-coordination-key'}).status_code==422


def test_expense_and_purchase_retry_cannot_double_book(client):
    auth(client)
    body={'title':'هزینه با کلید تکرار','amount':'0.25','idempotency_key':'expense-retry-test'}
    first=client.post('/api/finance/expenses',json=body)
    assert first.status_code==200
    assert client.post('/api/finance/expenses',json=body).json()==first.json()
    assert client.post('/api/finance/expenses',json={**body,'amount':'0.50'}).status_code==409
    before=client.get('/api/inventory').json()['ingredients'][0]['stock']
    purchase={'quantity':'0.1','unit_cost':'0.15','idempotency_key':'purchase-retry-test'}
    assert client.post('/api/inventory/1/restock',json=purchase).status_code==200
    assert client.post('/api/inventory/1/restock',json=purchase).status_code==200
    assert client.get('/api/inventory').json()['ingredients'][0]['stock']==before+0.1
    with server.db() as c:
        assert c.execute('SELECT COUNT(*) FROM expenses WHERE title=?',(body['title'],)).fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM financial_operation_keys').fetchone()[0]==2


def test_refund_updates_customer_net_spend_without_deleting_purchase_history(client):
    auth(client);order=new_order(client,key='crm-net-refund',consent=True)
    complete(client,order['id']);client.post(f"/api/orders/{order['id']}/pay",json={'method':'cash'})
    pay=client.get(f"/api/orders/{order['id']}/receipt").json()['payment']
    before=next(r for r in client.get('/api/crm').json() if r['phone']=='09120000999')
    assert refund(client,pay,'1000.25').status_code==201
    after=next(r for r in client.get('/api/crm').json() if r['id']==before['id'])
    assert after['spend_minor']==before['spend_minor']-100025
    assert after['spend']==before['spend']-1000.25 and after['visits']==before['visits']


def test_delivery_and_full_refund_race_consumes_recipe_only_once(client):
    auth(client);order,pay=settle(client,key='refund-delivery-race')
    for status in ('accepted','preparing','ready'):client.patch(f"/api/orders/{order['id']}/status",json={'status':status})
    before={r['id']:r['stock'] for r in client.get('/api/inventory').json()['ingredients']};cookies=dict(client.cookies)
    def action(n):
        with TestClient(server.app) as other:
            other.cookies.update(cookies)
            return other.patch(f"/api/orders/{order['id']}/status",json={'status':'completed'}).status_code if n==0 else refund(other,pay,order['total']).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:delivery,returned=list(pool.map(action,range(2)))
    assert delivery in (200,409) and returned==201
    after={r['id']:r['stock'] for r in client.get('/api/inventory').json()['ingredients']}
    assert before[1]-after[1]==72 and before[2]-after[2]==440
    with server.db() as c:
        row=c.execute('SELECT * FROM orders WHERE id=?',(order['id'],)).fetchone()
        assert (row['cost_total_minor'] or 0)+row['waste_cost_minor']==18960000
        assert row['status'] in ('completed','cancelled')
