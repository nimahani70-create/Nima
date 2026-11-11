import pytest
from fastapi.testclient import TestClient
import app as server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'DB_PATH', tmp_path / 'test.sqlite3')
    with TestClient(server.app) as c:
        yield c


def auth(c, who='admin', password='123456'):
    r=c.post('/api/auth/login',json={'username':who,'password':password})
    assert r.status_code==200,r.text
    return r.json()


def new_order(c, key='unique-key-a', consent=False, allergy_note=''):
    r=c.post('/api/orders',json={
        'idempotency_key':key,'fulfillment':'table','table':3,
        'items':[{'product_id':2,'qty':2,'addon_id':2}],
        'payment_method':'cash','customer_name':'مشتری جدید',
        'phone':'09120000999','consent':consent,'note':'بدون شکر','allergy_note':allergy_note})
    assert r.status_code==201,r.text
    return r.json()


def test_bootstrap_menu_and_qr(client):
    menu=client.get('/api/public/menu').json()
    assert len(menu['products'])>=10
    assert len(menu['tables'])==12
    assert menu['products'][1]['addons']
    assert client.get('/api/qr/3').content[:8]==b'\x89PNG\r\n\x1a\n'
    assert client.get('/api/qr/999').status_code==404
    assert client.get('/api/demo/health').json()['online_payments'] is False
    for page in ('menu','staff','cashier','admin','inventory','finance','analytics','crm','attendance','presentation'):
        assert client.get('/'+page).status_code==200


def test_order_idempotency_price_security_and_tracking(client):
    assert client.get('/api/orders').status_code==401
    # 2 lattes @135,000 + 2 extra shots @35,000 = 340,000, server-side
    o=new_order(client,key='dup-order-test')
    assert o['total']==340000
    b=new_order(client,key='dup-order-test')
    assert b['id']==o['id'] and b['duplicate']
    assert client.get('/api/orders/'+o['code']+'/status?token=wrong').status_code==404
    assert client.get('/api/orders/'+o['code']+'/status?token='+o['token']).json()['status']=='new'
    r=client.post('/api/orders',json={'idempotency_key':'online-order','fulfillment':'takeaway','items':[{'product_id':1,'qty':1}], 'payment_method':'online'})
    assert r.status_code==422
    invalid=client.post('/api/orders',json={'idempotency_key':'bad-addon-test','fulfillment':'takeaway','items':[{'product_id':1,'qty':1,'addon_id':3}], 'payment_method':'cash'})
    assert invalid.status_code==422
    assert client.get('/api/crm').status_code==401


def test_complete_order_stock_cash_finance_and_crm(client):
    o=new_order(client,key='full-flow-order',consent=True,allergy_note='حساسیت به لبنیات')
    auth(client)
    before=client.get('/api/inventory').json()['ingredients']
    start={i['id']:i['stock'] for i in before}
    assert client.patch(f"/api/orders/{o['id']}/status",json={'status':'ready'}).status_code==409
    assert client.patch(f"/api/orders/{o['id']}/status",json={'status':'accepted'}).status_code==409
    assert client.patch(f"/api/orders/{o['id']}/status",json={'status':'accepted','allergy_ack':True}).status_code==200
    assert client.patch(f"/api/orders/{o['id']}/status",json={'status':'preparing'}).status_code==200
    assert client.patch(f"/api/orders/{o['id']}/status",json={'status':'ready'}).status_code==200
    assert client.patch(f"/api/orders/{o['id']}/status",json={'status':'completed'}).status_code==200
    assert client.patch(f"/api/orders/{o['id']}/status",json={'status':'completed'}).status_code==409
    after={i['id']:i['stock'] for i in client.get('/api/inventory').json()['ingredients']}
    assert after[1]==start[1]-72
    assert after[2]==start[2]-440
    pay=client.post(f"/api/orders/{o['id']}/pay",json={'method':'pos','reference':'BANK1234'})
    assert pay.status_code==200
    assert client.post(f"/api/orders/{o['id']}/pay",json={'method':'pos'}).status_code==409
    finance=client.get('/api/finance').json()
    assert finance['summary']['revenue']>=o['total']
    crm=client.get('/api/crm').json()
    assert any(p['phone']=='09120000999' and p['spend']>=o['total'] for p in crm)
    analytics=client.get('/api/analytics').json()
    assert analytics['insights'] and analytics['top']
    assert client.get('/api/orders/'+o['code']+'/status?token='+o['token']).json()['payment_id'] is not None


def test_roles_attendance_and_product_management(client):
    auth(client,'barista','345678')
    assert client.get('/api/orders').status_code==200
    assert client.get('/api/finance').status_code==403
    assert client.post('/api/attendance/punch',json={'action':'in'}).status_code==200
    assert client.post('/api/attendance/punch',json={'action':'in'}).status_code==409
    assert client.post('/api/attendance/punch',json={'action':'out'}).status_code==200
    link=client.post('/api/attendance/telegram-link',json={}).json()
    assert link['code'] and link['expires_seconds']==600
    client.post('/api/auth/logout')
    auth(client,'cashier','234567')
    assert client.get('/api/orders').status_code==200
    assert client.patch('/api/orders/1/status',json={'status':'accepted'}).status_code==403
    assert client.get('/api/inventory').status_code==403
    client.post('/api/auth/logout')
    auth(client)
    p=client.post('/api/admin/products',json={'name':'موکای ویژه','price':174000,'category_id':1,'description':'قهوه تلخ','emoji':'☕','active':True})
    assert p.status_code==200,p.text
    pid=p.json()['id']
    assert any(item['id']==pid for item in client.get('/api/public/menu').json()['products'])
    assert client.put(f'/api/admin/products/{pid}',json={'name':'موکای ویژه','price':184000,'category_id':1,'description':'قهوه تلخ','emoji':'☕','active':False}).status_code==200
    assert not any(item['id']==pid for item in client.get('/api/public/menu').json()['products'])
    assert client.post('/api/admin/categories',json={'name':'صبحانه'}).status_code==200
    assert client.get('/api/attendance').json()['punches']


def test_inventory_restock_recipe_expenses(client):
    auth(client)
    before=client.get('/api/inventory').json()['ingredients'][0]
    r=client.post(f"/api/inventory/{before['id']}/restock",json={'quantity':100,'unit_cost':1800})
    assert r.status_code==200 and r.json()['expense']==180000
    after=client.get('/api/inventory').json()['ingredients'][0]
    assert after['stock']==before['stock']+100
    f=client.get('/api/finance').json()
    assert f['summary']['inventory_purchases_cash_out']>=180000
    assert client.put('/api/inventory/recipes/1',json={'parts':[{'ingredient_id':1,'quantity':19}]}).status_code==200
    assert client.post('/api/finance/expenses',json={'title':'هزینه تست','amount':20000,'kind':'operating'}).status_code==200
    assert any(e['title']=='هزینه تست' for e in client.get('/api/finance').json()['expenses'])


def test_paid_order_cannot_cancel_without_refund(client):
    o=new_order(client,key='cannot-cancel-when-paid')
    auth(client)
    assert client.post(f"/api/orders/{o['id']}/pay",json={'method':'cash'}).status_code==200
    denied=client.patch(f"/api/orders/{o['id']}/status",json={'status':'cancelled'})
    assert denied.status_code==409


def test_addon_substitution_affects_inventory(client):
    menu=client.get('/api/public/menu').json()
    latte=next(p for p in menu['products'] if p['id']==2)
    plant=next(a for a in latte['addons'] if a['name']=='شیر گیاهی')
    r=client.post('/api/orders',json={'idempotency_key':'plant-milk-test','fulfillment':'takeaway','items':[{'product_id':2,'qty':1,'addon_id':plant['id']}],'payment_method':'pos'})
    assert r.status_code==201,r.text
    oid=r.json()['id']
    auth(client)
    before={x['id']:x['stock'] for x in client.get('/api/inventory').json()['ingredients']}
    for status in ('accepted','preparing','ready','completed'):
        assert client.patch(f'/api/orders/{oid}/status',json={'status':status}).status_code==200
    after={x['id']:x['stock'] for x in client.get('/api/inventory').json()['ingredients']}
    assert after[1]==before[1]-18
    assert after[2]==before[2] # replaced, not simultaneously consumed
    assert after[6]==before[6]-220
    rules=client.get(f'/api/admin/addons/{plant["id"]}/recipe')
    assert rules.status_code==200 and len(rules.json())==2
    assert client.put(f'/api/admin/addons/{plant["id"]}/recipe',json={'parts':[{'ingredient_id':6,'delta':200},{'ingredient_id':2,'delta':-200}]}).status_code==200
