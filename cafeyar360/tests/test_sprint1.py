from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest
from fastapi.testclient import TestClient
import app as server
import manage
from test_flow import client, auth, new_order
from conftest import TEST_PASSWORDS


def test_operational_bootstrap_requires_passwords_and_has_no_sample_data(tmp_path, monkeypatch):
    monkeypatch.setattr(server,'DB_PATH',tmp_path/'clean.sqlite3')
    monkeypatch.setattr(server,'DEMO_MODE',False)
    for name in ('ADMIN','CASHIER','BARISTA'):monkeypatch.delenv('CAFE_PASSWORD_'+name,raising=False)
    with pytest.raises(RuntimeError,match='CAFE_PASSWORD_ADMIN'):
        server.init_db()
    for who in ('ADMIN','CASHIER','BARISTA'):
        monkeypatch.setenv('CAFE_PASSWORD_'+who,'test-only-long-password-'+who)
    with TestClient(server.app) as c:
        assert c.post('/api/auth/login',json={'username':'admin','password':'123456'}).status_code==401
        auth(c,password='test-only-long-password-ADMIN')
        assert c.get('/api/orders').json()==[]
        assert c.get('/api/crm').json()==[]
        assert c.get('/api/public/menu').json()['products']==[]
        assert c.get('/api/public/config').json()['demo'] is False
        assert c.get('/api/public/config').json()['tables']==0
    server.init_db()  # Restart must neither insert demo data nor overwrite users.


def test_legacy_password_rotation_revokes_session(client,monkeypatch):
    auth(client)
    with server.db() as db:db.execute('UPDATE users SET password_hash=? WHERE username=?',(server.hpass('123456'),'admin'))
    monkeypatch.setattr(server,'DEMO_MODE',False)
    with pytest.raises(RuntimeError,match='Unsafe demo password'):
        server.init_db()
    for who in ('admin','cashier','barista'):
        manage.change_password(who,'test-only-rotated-password')
    assert client.get('/api/auth/me').status_code==401
    server.init_db()
    assert client.post('/api/auth/login',json={'username':'admin','password':'123456'}).status_code==401
    auth(client,password='test-only-rotated-password')


def test_recipe_snapshot_survives_catalog_edits_and_restart(client):
    order=new_order(client,key='frozen-recipe-order')
    auth(client)
    before={i['id']:i['stock'] for i in client.get('/api/inventory').json()['ingredients']}
    assert client.delete('/api/admin/addons/2').status_code==200
    assert client.put('/api/inventory/recipes/2',json={'parts':[{'ingredient_id':1,'quantity':1}]}).status_code==200
    server.init_db()
    for state in ('accepted','preparing','ready','completed'):
        assert client.patch(f"/api/orders/{order['id']}/status",json={'status':state}).status_code==200
    after={i['id']:i['stock'] for i in client.get('/api/inventory').json()['ingredients']}
    assert before[1]-after[1]==72
    assert before[2]-after[2]==440
    assert client.patch(f"/api/orders/{order['id']}/status",json={'status':'completed'}).status_code==409


def test_restart_does_not_restore_deleted_recipe_parts(client):
    auth(client)
    parts=[{'ingredient_id':6,'delta':200}]
    assert client.put('/api/admin/addons/3/recipe',json={'parts':parts}).status_code==200
    server.init_db()
    rows=client.get('/api/admin/addons/3/recipe').json()
    assert [(x['ingredient_id'],x['delta']) for x in rows]==[(6,200)]


def test_changed_idempotency_payload_conflicts(client):
    o=new_order(client,key='repeat-payload-key')
    same=new_order(client,key='repeat-payload-key')
    assert same['id']==o['id'] and same['duplicate']
    body={'idempotency_key':'repeat-payload-key','fulfillment':'takeaway','payment_method':'cash','items':[{'product_id':1,'qty':1}]}
    assert client.post('/api/orders',json=body).status_code==409


def test_queue_preserves_old_unpaid_and_active_orders(client):
    oldest=new_order(client,key='oldest-queue-order')
    for i in range(101):
        new_order(client,key=f'queue-overflow-{i}')
    auth(client)
    assert oldest['id'] not in {o['id'] for o in client.get('/api/orders').json()}
    for queue in ('active','unpaid'):
        assert oldest['id'] in {o['id'] for o in client.get('/api/orders',params={'queue':queue}).json()}
    page1=client.get('/api/orders?limit=10').json()
    page2=client.get('/api/orders?limit=10&offset=10').json()
    assert len(page1)==len(page2)==10
    assert not {x['id'] for x in page1}&{x['id'] for x in page2}
    assert client.get('/api/orders?limit=0').status_code==422
    assert client.get('/api/orders?queue=invalid').status_code==422


def test_attendance_permissions_and_initial_out(client):
    auth(client)
    assert client.post('/api/attendance/punch',json={'action':'in'}).status_code==200
    client.post('/api/auth/logout')
    auth(client,'barista')
    assert client.post('/api/attendance/punch',json={'action':'out'}).status_code==409
    rows=client.get('/api/attendance').json()
    assert [u['username'] for u in rows['users']]==['barista']
    assert rows['punches']==[]
    assert client.post('/api/attendance/punch',json={'action':'in'}).status_code==200
    assert client.post('/api/attendance/punch',json={'action':'out'}).status_code==200
    assert len(client.get('/api/attendance').json()['punches'])==2
    client.post('/api/auth/logout');auth(client)
    assert len(client.get('/api/attendance').json()['punches'])==3


def test_database_rollback_and_backup(client,tmp_path):
    with pytest.raises(RuntimeError):
        with server.db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE ingredients SET stock=0 WHERE id=1")
            raise RuntimeError('simulate failure')
    with server.db() as c:
        assert c.execute('SELECT stock FROM ingredients WHERE id=1').fetchone()[0]==8600
    target=tmp_path/'backup.sqlite3'
    manage.backup(target)
    with sqlite3.connect(target) as copy:
        assert copy.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert copy.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert copy.execute('SELECT COUNT(*) FROM orders').fetchone()[0]==27
    with pytest.raises(ValueError):
        manage.backup(target)


def test_concurrent_payment_is_recorded_once(client):
    order=new_order(client,key='concurrent-payment')
    auth(client)
    cookie=dict(client.cookies)
    def settle(_):
        with TestClient(server.app) as other:
            other.cookies.update(cookie)
            return other.post(f"/api/orders/{order['id']}/pay",json={'method':'cash'}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(settle,range(2)))==[200,409]
    with server.db() as c:
        assert c.execute('SELECT COUNT(*) FROM payments WHERE order_id=?',(order['id'],)).fetchone()[0]==1


def test_database_errors_and_non_finite_numbers_are_controlled(client):
    auth(client)
    invalid={'name':'نام آزمایشی','price':100,'category_id':99999}
    assert client.put('/api/admin/products/1',json=invalid).status_code==409
    assert client.post('/api/inventory/1/restock',content='{"quantity": Infinity,"unit_cost": 1}',headers={'Content-Type':'application/json'}).status_code==422
    assert client.put('/api/admin/addons/3/recipe',json={'parts':[{'ingredient_id':6,'delta':'nan'}]}).status_code==422
    assert client.get('/api/admin/addons/3/recipe').json()  # rollback retained original recipe


def test_secure_cookie_and_no_customer_token_in_staff_list(client,monkeypatch):
    monkeypatch.setattr(server,'COOKIE_SECURE',True)
    response=client.post('/api/auth/login',json={'username':'admin','password':TEST_PASSWORDS['admin']})
    assert 'Secure' in response.headers['set-cookie'].split('; ')
    client.cookies.clear()
    monkeypatch.setattr(server,'COOKIE_SECURE',False)
    auth(client)
    row=client.get('/api/orders').json()[0]
    assert 'customer_token' not in row and 'request_hash' not in row


def test_upgrade_legacy_schema_freezes_pending_orders_once(client):
    order=new_order(client,key='pre-migration-order')
    with server.db() as c:
        c.execute('DROP TABLE order_materials')
        c.execute('DROP TABLE schema_migrations')
        c.execute('ALTER TABLE orders DROP COLUMN request_hash')
        c.execute('ALTER TABLE orders DROP COLUMN recipe_snapshot')
    server.init_db()
    with server.db() as c:
        row=c.execute('SELECT * FROM orders WHERE id=?',(order['id'],)).fetchone()
        assert row['customer_token']==order['token'] and row['total']==order['total']
        assert row['recipe_snapshot']==1
        assert c.execute('SELECT quantity FROM order_materials WHERE order_id=? AND ingredient_id=1',(order['id'],)).fetchone()[0]==72
        assert c.execute('SELECT COUNT(*) FROM schema_migrations WHERE version=1').fetchone()[0]==1
    server.init_db()
    with server.db() as c:
        assert c.execute('SELECT COUNT(*) FROM order_materials WHERE order_id=?',(order['id'],)).fetchone()[0]==2


@pytest.mark.parametrize('demo',[True,False])
def test_no_runtime_bootstrap_password_fallback(tmp_path,monkeypatch,demo):
    monkeypatch.setattr(server,'DB_PATH',tmp_path/'no-default.sqlite3')
    monkeypatch.setattr(server,'DEMO_MODE',demo)
    for name in ('ADMIN','CASHIER','BARISTA'):monkeypatch.delenv('CAFE_PASSWORD_'+name,raising=False)
    with pytest.raises(RuntimeError,match='CAFE_PASSWORD_ADMIN'):server.init_db()
    with server.db() as c:assert c.execute('SELECT COUNT(*) FROM users').fetchone()[0]==0


def test_local_setup_reads_passwords_hidden_and_does_not_write_them(tmp_path,monkeypatch,capsys):
    from types import SimpleNamespace
    monkeypatch.setattr(server,'DB_PATH',tmp_path/'local.sqlite3')
    for name in ('ADMIN','CASHIER','BARISTA'):monkeypatch.delenv('CAFE_PASSWORD_'+name,raising=False)
    monkeypatch.setattr(manage.sys,'stdin',SimpleNamespace(isatty=lambda:True))
    values=iter([p for name in ('admin','cashier','barista') for p in (TEST_PASSWORDS[name],TEST_PASSWORDS[name])])
    monkeypatch.setattr(manage.getpass,'getpass',lambda prompt:next(values))
    manage.initialize()
    with server.db() as c:
        for row in c.execute('SELECT username,password_hash FROM users'):
            assert server.verify(TEST_PASSWORDS[row['username']],row['password_hash'])
        assert c.execute('SELECT COUNT(*) FROM products').fetchone()[0]==10
    assert not list(tmp_path.glob('*.env'))
    captured=capsys.readouterr()
    assert all(p not in captured.out+captured.err for p in TEST_PASSWORDS.values())


def test_noninteractive_setup_fails_without_credentials_and_without_creating_database(tmp_path,monkeypatch):
    from types import SimpleNamespace
    path=tmp_path/'not-created.sqlite3';monkeypatch.setattr(server,'DB_PATH',path)
    for name in ('ADMIN','CASHIER','BARISTA'):monkeypatch.delenv('CAFE_PASSWORD_'+name,raising=False)
    monkeypatch.setattr(manage.sys,'stdin',SimpleNamespace(isatty=lambda:False))
    with pytest.raises(ValueError,match='Non-interactive'):manage.initialize()
    assert not path.exists()


def test_password_rotation_makes_recoverable_backup_before_revocation(client):
    auth(client)
    manage.change_password('admin','test-only-new-local-password')
    snapshots=list((server.DB_PATH.parent/'backups').glob('before-password-*.sqlite3'))
    assert len(snapshots)==1
    with sqlite3.connect(snapshots[0]) as old:
        previous=old.execute("SELECT password_hash FROM users WHERE username='admin'").fetchone()[0]
        assert server.verify(TEST_PASSWORDS['admin'],previous)
        assert old.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]==1
        assert old.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert client.get('/api/auth/me').status_code==401
    auth(client,password='test-only-new-local-password')


def test_legacy_public_password_rejected_even_in_demo_mode(client):
    with server.db() as c:c.execute("UPDATE users SET password_hash=? WHERE username='admin'",(server.hpass('123456'),))
    with pytest.raises(RuntimeError,match='Unsafe demo password'):manage.initialize()
    assert list((server.DB_PATH.parent/'backups').glob('before-init-*.sqlite3'))


def test_demo_shortcuts_never_embed_passwords():
    js=(server.ROOT/'static/app.js').read_text(encoding='utf-8')
    assert "const DEMO_USERS={admin:'admin',cashier:'cashier',barista:'barista'}" in js
    assert "querySelector('[name=password]').value=''" in js
    for old in ('admin / 123456','cashier / 234567','barista / 345678'):
        assert old not in js
