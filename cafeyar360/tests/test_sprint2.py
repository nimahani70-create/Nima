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
    assert feb['monthly'][0]['month']=='2024-02' and feb['monthly'][0]['sales']==order['total']
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
    client.post('/api/auth/logout');auth(client,'cashier','234567')
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
    client.post('/api/auth/logout');auth(client,'barista','345678')
    assert client.get(f"/receipts/{order['id']}").status_code==403
    assert client.get(f"/api/orders/{order['id']}/receipt").status_code==403
    client.post('/api/auth/logout')
    assert client.get(f"/receipts/{order['id']}").status_code==401


def test_correction_audits_old_values_and_rejects_stale_revision(client):
    order=new_order(client,key='correction-ledger-test')
    auth(client,'cashier','234567')
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
    client.post('/api/auth/logout');auth(client,'cashier','234567')
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
    client.post('/api/auth/logout');auth(client,'barista','345678')
    assert client.get('/api/cashier/summary').status_code==403
    assert client.get('/api/cashier/discrepancies').status_code==403
    assert client.post(f'/api/payments/{pid}/discrepancies',json={**base,'reported_amount':1}).status_code==403
    assert client.patch(f'/api/payments/{pid}/correction',json={'method':'pos','reason':'تست مجوز','expected_revision':0}).status_code==403
