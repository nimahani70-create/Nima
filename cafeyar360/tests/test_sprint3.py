"""Workforce acceptance, financial regressions, security and atomicity (offline)."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
import app as server
import workforce
import telegram_bot as bot
from test_flow import client, auth, new_order

PASSWORD = 'test-only-new-employee-password'


def employee(c, role='staff', name='worker'):
    r = c.post('/api/admin/employees', json={'name':'کارمند نمونه', 'username':name, 'role':role,
             'password':PASSWORD, 'reason':'تعریف حساب آزمایشی'})
    assert r.status_code == 201, r.text
    return r.json()


def update(c, u, **changes):
    return c.put('/api/admin/employees/'+str(u['id']), json={**u, 'reason':'اصلاح آزمایشی مدیر', **changes})


def signin(c, username, password=PASSWORD):
    r=c.post('/api/auth/login',json={'username':username,'password':password})
    assert r.status_code==200,r.text
    c.headers['X-CSRF-Token']=r.json()['csrf_token']
    return r.json()


def shift(c, uid, start, end):
    return c.post('/api/admin/employee-shifts',json={'user_id':uid,'starts_at':start,'ends_at':end,'reason':'برنامه کاری نمونه'})


def correction(c, uid, action, at, **changes):
    return c.post('/api/admin/attendance/corrections',json={'user_id':uid,'action':action,'at':at,
                  'reason':'اصلاح تردد جاافتاده',**changes})


@pytest.mark.parametrize('role',['admin','cashier','barista','staff'])
def test_create_employee_role_and_permissions(client, role):
    auth(client);u=employee(client,role)
    assert 'password_hash' not in json.dumps(u)
    signin(client,'worker')
    assert client.get('/api/finance').status_code==(200 if role=='admin' else 403)
    assert client.get('/api/admin/employees').status_code==(200 if role=='admin' else 403)
    assert client.get('/api/orders').status_code==200
    if role in ('barista','staff'):
        order=client.get('/api/orders').json()[0]
        assert 'payment' not in order and 'cost_total' not in order and 'customer_phone' not in order
        assert 'cost_total_minor' not in order and 'waste_cost_minor' not in order
        assert client.post('/api/orders/1/pay',json={'method':'cash'}).status_code==403


def test_employee_disable_revokes_sessions_telegram_and_codes(client):
    auth(client);u=employee(client)
    with TestClient(server.app) as other:
        signin(other,'worker')
        code=other.post('/api/attendance/telegram-link').json()['code']
        with server.db() as c:c.execute('UPDATE users SET telegram_user_id=? WHERE id=?',('987001',u['id']))
        r=update(client,u,active=False);assert r.status_code==200
        assert other.get('/api/auth/me').status_code==401
        assert other.post('/api/attendance/punch',json={'action':'in'}).status_code==401
        with server.db() as c:
            assert not c.execute('SELECT 1 FROM sessions WHERE user_id=?',(u['id'],)).fetchone()
            assert not c.execute('SELECT 1 FROM telegram_links WHERE code=?',(code,)).fetchone()
            assert c.execute('SELECT telegram_user_id FROM users WHERE id=?',(u['id'],)).fetchone()[0] is None
        assert other.post('/api/auth/login',json={'username':'worker','password':PASSWORD}).status_code==401
    msg={'from':{'id':987001},'chat':{'id':987001,'type':'private'},'date':int(time.time()),'text':'/in'}
    result=bot.process_update({'update_id':1,'message':msg},'disabled-test-bot')
    assert result['status']=='rejected'


def test_last_admin_and_revision_protection(client):
    auth(client);u=client.get('/api/admin/employees').json()[0]
    assert update(client,u,active=False).status_code==409
    assert update(client,u,role='staff').status_code==409
    assert update(client,u,name='مدیر ویرایش‌شده').status_code==200
    assert update(client,u,name='تغییر قدیمی').status_code==409
    assert client.post('/api/admin/employees',json={'name':'نام','username':'admin','role':'staff','password':PASSWORD,'reason':'تکراری'}).status_code==409


def test_password_change_and_session_revocation_and_audit(client):
    auth(client);u=employee(client)
    with TestClient(server.app) as other:
        signin(other,'worker')
        sessions=client.get('/api/admin/sessions').json()
        sid=next(x['id'] for x in sessions if x['user_id']==u['id'])
        assert client.request('DELETE','/api/admin/sessions/'+sid,json={'reason':'ابطال نمونه'}).status_code==200
        assert other.get('/api/auth/me').status_code==401
        signin(other,'worker')
        r=update(client,u,password='test-only-rotated-employee-password');assert r.status_code==200
        assert other.get('/api/auth/me').status_code==401
        assert other.post('/api/auth/login',json={'username':'worker','password':PASSWORD}).status_code==401
        signin(other,'worker','test-only-rotated-employee-password')
    logs=client.get('/api/admin/audit').json();dump=json.dumps(logs)
    assert PASSWORD not in dump and 'password_hash' not in dump
    assert any(x['kind']=='employee_updated' and x['actor_id']==1 for x in logs)
    with server.db() as c:
        with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE audit_log SET reason='modified'")
        with pytest.raises(sqlite3.IntegrityError):c.execute('DELETE FROM audit_log')


def test_csrf_missing_forged_foreign_origin_and_expired_session(client):
    auth(client);token=client.headers.pop('X-CSRF-Token')
    assert client.post('/api/cashier/shifts',json={'opening_amount':0,'idempotency_key':'csrf-protected'}).status_code==403
    assert client.post('/api/attendance/telegram-link',headers={'X-CSRF-Token':'forged'}).status_code==403
    client.headers['X-CSRF-Token']=token
    assert client.post('/api/attendance/telegram-link').status_code==200
    assert client.post('/api/attendance/telegram-link',headers={'Origin':'https://evil.example'}).status_code==403
    assert client.post('/api/auth/login',json={'username':'admin','password':PASSWORD},headers={'Origin':'https://evil.example'}).status_code==403
    with server.db() as c:c.execute('UPDATE sessions SET expires=0')
    assert client.get('/api/auth/me').status_code==401
    assert client.post('/api/attendance/punch',json={'action':'in'}).status_code==401


def test_login_limiter_persistent_window_and_no_sensitive_logging(client,monkeypatch):
    for _ in range(5):
        assert client.post('/api/auth/login',json={'username':'admin','password':'test-only-wrong-password'}).status_code==401
    assert client.post('/api/auth/login',json={'username':'admin','password':'test-only-admin-long-password'}).status_code==429
    with server.db() as c:
        assert c.execute('SELECT count(*) FROM security_events').fetchone()[0]>=6
        assert 'wrong-password' not in json.dumps([dict(x) for x in c.execute('SELECT * FROM security_events')])
        c.execute('UPDATE login_attempts SET window_start=window_start-901')
    auth(client)


def test_tables_keep_identity_old_qr_and_historical_order_number(client):
    auth(client)
    before=new_order(client,key='before-renumbering')
    t=next(t for t in client.get('/api/admin/tables').json() if t['number']==3)
    r=client.put('/api/admin/tables/3',json={**t,'number':33,'title':'پنجره','capacity':6,'reason':'تغییر چیدمان'})
    assert r.status_code==200 and r.json()['id']==3
    assert client.get('/api/qr/3').status_code==200
    assert client.get('/api/qr/33').status_code==200
    assert client.get('/api/public/tables/3').json()['number']==33
    order=new_order(client,key='old-printed-qr')  # number 3 still resolves to same physical table
    rows=client.get('/api/orders').json()
    assert next(x for x in rows if x['id']==before['id'])['table_num']==3
    assert next(x for x in rows if x['id']==order['id'])['table_num']==33
    with server.db() as c:assert c.execute('SELECT table_num FROM orders WHERE id=?',(order['id'],)).fetchone()[0]==3
    assert client.post('/api/admin/tables',json={'number':3,'capacity':4,'reason':'تلاش تکرار'}).status_code==409
    assert client.post('/api/admin/tables',json={'number':33,'capacity':4,'reason':'تلاش تکرار'}).status_code==409
    assert 'QR' in client.get('/tables/3/print').text
    t=r.json();r=client.put('/api/admin/tables/3',json={**t,'active':False,'reason':'توقف پذیرش'})
    assert r.status_code==200
    body={'items':[{'product_id':1,'qty':1}],'fulfillment':'table','table':3,'payment_method':'cash','idempotency_key':'disabled-table'}
    assert client.post('/api/orders',json=body).status_code==422
    assert client.post('/api/orders',json={**body,'table':33}).status_code==422
    assert client.get('/api/qr/3').status_code==404
    assert client.get('/api/public/tables/3').status_code==404
    assert client.get('/api/orders/'+before['code']+'/status?token='+before['token']).json()['table_num']==3
    # A successful old request can be safely retried even after table deactivation.
    assert new_order(client,key='old-printed-qr')['duplicate']


def test_table_create_and_validation_and_optimistic_revision(client):
    auth(client)
    b={'number':80,'title':'باغ','capacity':2,'reason':'تعریف میز'}
    r=client.post('/api/admin/tables',json=b);assert r.status_code==201
    assert client.put('/api/admin/tables/80',json={**b,'capacity':3}).status_code==200
    assert client.put('/api/admin/tables/80',json={**b,'capacity':4}).status_code==409
    assert client.post('/api/admin/tables',json={**b,'number':0}).status_code==422
    assert client.post('/api/admin/tables',json={**b,'number':81,'capacity':0}).status_code==422
    assert client.post('/api/admin/tables',json={**b,'number':81,'reason':'   '}).status_code==422


def test_overnight_shift_overlap_and_boundary_validation(client):
    auth(client);u=employee(client)
    assert shift(client,u['id'],'2026-10-01T22:00:00+03:30','2026-10-02T06:00:00+03:30').status_code==201
    assert shift(client,u['id'],'2026-10-02T05:59:00+03:30','2026-10-02T09:00:00+03:30').status_code==409
    assert shift(client,u['id'],'2026-10-02T06:00:00+03:30','2026-10-02T08:00:00+03:30').status_code==201
    assert shift(client,u['id'],'2026-10-02T06:00:00','2026-10-02T08:00:00').status_code==422
    assert shift(client,u['id'],'2026-10-02T06:00:00+03:30','2026-10-01T08:00:00+03:30').status_code==422
    assert shift(client,u['id'],'2026-10-02T06:00:00+03:30','2026-10-04T08:00:00+03:30').status_code==422


def test_punch_requires_shift_and_alternating_events(client):
    auth(client);u=employee(client)
    signin(client,'worker')
    assert client.post('/api/attendance/punch',json={'action':'in'}).status_code==409
    assert client.post('/api/attendance/punch',json={'action':'out'}).status_code==409
    auth(client);dt=datetime.now(timezone.utc)
    assert shift(client,u['id'],(dt-timedelta(hours=1)).isoformat(),(dt+timedelta(hours=1)).isoformat()).status_code==201
    signin(client,'worker')
    assert client.post('/api/attendance/punch',json={'action':'in'}).status_code==200
    assert client.post('/api/attendance/punch',json={'action':'in'}).status_code==409
    assert client.post('/api/attendance/punch',json={'action':'out'}).status_code==200
    assert client.post('/api/attendance/punch',json={'action':'out'}).status_code==409
    assert client.post('/api/admin/employee-shifts',json={}).status_code==403
    assert len(client.get('/api/attendance/hours').json()['items'])==1


def test_telegram_send_time_dedup_offsets_delayed_and_outside_shift(client):
    auth(client);u=employee(client);uid=u['id'];dt=datetime.now(timezone.utc)-timedelta(hours=3)
    start=int(dt.timestamp());end=start+7200
    assert shift(client,uid,datetime.fromtimestamp(start,timezone.utc).isoformat(),datetime.fromtimestamp(end,timezone.utc).isoformat()).status_code==201
    with server.db() as c:c.execute('UPDATE users SET telegram_user_id=? WHERE id=?',('90099',uid))
    def event(i,cmd,at):return {'update_id':i,'message':{'from':{'id':90099},'chat':{'type':'private','id':90099},'date':at,'text':cmd}}
    incoming=event(10,'/in',start)
    assert bot.process_update(incoming,'test-bot')['status']=='processed'
    assert bot.process_update(incoming,'test-bot')['status']=='processed'
    assert bot.durable_offset('test-bot')==11
    assert bot.process_update(event(11,'/out',start-10),'test-bot')['status']=='needs_review'
    assert bot.process_update(event(12,'/out',end+1),'test-bot')['status']=='needs_review'
    assert bot.process_update(event(13,'/out',end),'test-bot')['status']=='processed'
    assert bot.durable_offset('test-bot')==14
    rows=client.get('/api/attendance').json()['punches'];ours=[x for x in rows if x['user_id']==uid]
    assert len(ours)==2 and ours[0]['source']=='telegram'
    hours=next(x for x in client.get('/api/attendance/hours').json()['items'] if x['user_id']==uid)
    assert hours['seconds']==7200 and hours['open_since'] is None
    assert len(client.get('/api/admin/telegram-inbox').json())==2


def test_telegram_network_retry_and_process_restart_do_not_duplicate_punch(client,monkeypatch):
    auth(client);u=employee(client);dt=datetime.now(timezone.utc)
    shift(client,u['id'],(dt-timedelta(hours=1)).isoformat(),(dt+timedelta(hours=1)).isoformat())
    with server.db() as c:c.execute('UPDATE users SET telegram_user_id=? WHERE id=?',('10019',u['id']))
    event={'update_id':90,'message':{'from':{'id':10019},'chat':{'id':10019,'type':'private'},'date':int(time.time()),'text':'/in'}}
    bot.process_update(event,'retry-bot')
    def fail(*args):raise OSError('network unavailable')
    monkeypatch.setattr(bot,'telegram',fail)
    with pytest.raises(OSError):bot.flush_replies('retry-bot')
    bot.process_update(event,'retry-bot')
    monkeypatch.setattr(bot,'telegram',lambda *args:{'ok':True})
    bot.flush_replies('retry-bot');bot.flush_replies('retry-bot')
    with server.db() as c:
        assert c.execute('SELECT count(*) FROM attendance WHERE user_id=?',(u['id'],)).fetchone()[0]==1
        assert c.execute("SELECT delivered FROM telegram_inbox WHERE bot_id='retry-bot'").fetchone()[0]==1
    assert bot.durable_offset('retry-bot')==91


def test_manager_correction_hours_tehran_clipping_revision_and_rollback(client):
    auth(client);u=employee(client);uid=u['id']
    a=correction(client,uid,'in','2026-10-01T23:00:00+03:30');assert a.status_code==200
    b=correction(client,uid,'out','2026-10-02T03:30:00+03:30');assert b.status_code==200
    data=client.get('/api/attendance/hours?start=2026-10-02&end=2026-10-02').json()
    assert next(x for x in data['items'] if x['user_id']==uid)['seconds']==3.5*3600
    result=correction(client,uid,'in','2026-10-01T22:00:00+03:30',event_id=a.json()['id']);assert result.status_code==200
    assert correction(client,uid,'in','2026-10-01T21:00:00+03:30',event_id=a.json()['id']).status_code==409
    # Moving an in event after its out must roll back both event and audit.
    before=client.get('/api/admin/audit').json()
    assert correction(client,uid,'in','2026-10-02T04:00:00+03:30',event_id=a.json()['id'],revision=1).status_code==409
    with server.db() as c:assert c.execute('SELECT revision FROM attendance WHERE id=?',(a.json()['id'],)).fetchone()[0]==1
    assert client.get('/api/admin/audit').json()==before
    assert client.get('/api/attendance/hours?start=2026-10-03&end=2026-10-01').status_code==422


def concurrent(c, operation):
    cookies=dict(c.cookies);token=c.headers['X-CSRF-Token']
    def call(n):
        with TestClient(server.app) as other:
            other.cookies.update(cookies);other.headers['X-CSRF-Token']=token
            return operation(other,n)
    with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(call,range(2)))


def test_concurrent_last_admin_deactivation_keeps_one(client):
    auth(client);second=employee(client,'admin','secondadmin');first=client.get('/api/admin/employees').json()[0]
    # Same authenticated administrator races to disable either admin.
    def change(c,n):return update(c,first if n==0 else second,active=False).status_code
    results=concurrent(client,change)
    assert results.count(200)==1
    assert all(x in (200,401,403,409) for x in results)
    with server.db() as c:assert c.execute("SELECT count(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]==1


def test_concurrent_table_number_reservation(client):
    auth(client)
    def create(c,n):return c.post('/api/admin/tables',json={'number':99,'title':str(n),'capacity':4,'reason':'تست هم‌زمان'}).status_code
    assert sorted(concurrent(client,create))==[201,409]
    with server.db() as c:assert c.execute('SELECT count(*) FROM cafe_tables WHERE display_number=99').fetchone()[0]==1


def test_concurrent_shift_and_punch_transactions(client):
    auth(client);u=employee(client);dt=datetime.now(timezone.utc)
    assert sorted(concurrent(client,lambda c,n:shift(c,u['id'],(dt-timedelta(hours=1)).isoformat(),(dt+timedelta(hours=1)).isoformat()).status_code))==[201,409]
    signin(client,'worker')
    assert sorted(concurrent(client,lambda c,n:c.post('/api/attendance/punch',json={'action':'in'}).status_code))==[200,409]


def test_employee_transaction_rolls_back_on_audit_failure(client,monkeypatch):
    auth(client);u=employee(client)
    def fail(*args,**kwargs):raise sqlite3.OperationalError('synthetic failure')
    monkeypatch.setattr(workforce,'audit',fail)
    assert update(client,u,active=False).status_code==503
    with server.db() as c:assert c.execute('SELECT active,revision FROM users WHERE id=?',(u['id'],)).fetchone()[:]==(1,0)


def test_v14_upgrade_backs_up_and_preserves_finance_tables_and_attendance(tmp_path,monkeypatch):
    from pathlib import Path
    path=tmp_path/'cafe_nima.sqlite3';monkeypatch.setattr(server,'DB_PATH',path)
    with sqlite3.connect(path) as c:
        c.executescript((Path(__file__).parent/'fixtures/schema_v3.sql').read_text())
        c.execute('INSERT INTO users(id,name,username,role,password_hash) VALUES(1,?,?,?,?)',('مدیر قدیمی','legacy','admin',server.hpass(PASSWORD)))
        c.execute('INSERT INTO cafe_tables(number) VALUES(3)')
        c.execute("INSERT INTO orders(id,code,idempotency_key,table_num,fulfillment,payment_method,total,status,stock_applied,customer_token,created_at,completed_at) VALUES(1,'old','old',3,'table','cash',100,'completed',1,'test-only-customer-token','2026-10-01T12:00:00+03:30','2026-10-01T12:00:00+03:30')")
        c.execute("INSERT INTO payments(order_id,amount,method,created_at) VALUES(1,100,'cash','2026-10-01T12:00:00+03:30')")
        c.execute("INSERT INTO attendance(user_id,action,source,created_at) VALUES(1,'in','panel','2026-10-01T09:00:00+03:30')")
    server.init_db()
    backups=list((tmp_path/'backups').glob('before-workforce-v4-*.sqlite3'));assert len(backups)==1
    with sqlite3.connect(backups[0]) as c:
        assert 'active' not in [r[1] for r in c.execute('pragma table_info(users)')]
        assert c.execute('SELECT amount FROM payments').fetchone()[0]==100
    with server.db() as c:
        assert c.execute('SELECT table_number_snapshot FROM orders').fetchone()[0]==3
        assert c.execute('SELECT amount FROM payments').fetchone()[0]==100
        assert c.execute('SELECT active FROM users').fetchone()[0]==1
        assert c.execute('SELECT display_number FROM cafe_tables').fetchone()[0]==3
        assert c.execute('SELECT created_at FROM attendance').fetchone()[0]=='2026-10-01T05:30:00.000000+00:00'
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
    server.init_db()
    assert len(list((tmp_path/'backups').glob('before-workforce-v4-*.sqlite3')))==1


def test_shift_edit_cannot_exclude_existing_punch_or_move_user(client):
    auth(client);u=employee(client);dt=datetime.now(timezone.utc)-timedelta(hours=4)
    r=shift(client,u['id'],dt.isoformat(),(dt+timedelta(hours=2)).isoformat());sh=r.json()
    server.punch_for_user(u['id'],'in','panel',(dt+timedelta(minutes=30)).isoformat())
    body={**sh,'starts_at':(dt+timedelta(hours=1)).isoformat(),'reason':'ویرایش نامعتبر'}
    assert client.put('/api/admin/employee-shifts/'+str(sh['id']),json=body).status_code==409
    assert client.put('/api/admin/employee-shifts/'+str(sh['id']),json={**sh,'user_id':2,'reason':'تغییر کارمند'}).status_code==409
    assert server.punch_for_user(u['id'],'out','panel',(dt+timedelta(hours=2)).isoformat())['action']=='out'


def test_late_checkout_requires_manager_correction(client):
    auth(client);u=employee(client);dt=datetime.now(timezone.utc)-timedelta(hours=4)
    shift(client,u['id'],dt.isoformat(),(dt+timedelta(hours=2)).isoformat())
    server.punch_for_user(u['id'],'in','panel',dt.isoformat())
    with pytest.raises(server.HTTPException) as exc:server.punch_for_user(u['id'],'out','panel',(dt+timedelta(hours=3)).isoformat())
    assert exc.value.status_code==409
    assert correction(client,u['id'],'out',(dt+timedelta(hours=3)).isoformat()).status_code==200
    item=next(x for x in client.get('/api/attendance/hours').json()['items'] if x['user_id']==u['id'])
    assert item['hours']==3 and item['open_since'] is None


def test_inbox_and_punch_roll_back_together_on_database_failure(client):
    auth(client);u=employee(client);dt=datetime.now(timezone.utc)
    shift(client,u['id'],(dt-timedelta(hours=1)).isoformat(),(dt+timedelta(hours=1)).isoformat())
    with server.db() as c:
        c.execute('UPDATE users SET telegram_user_id=? WHERE id=?',('99881',u['id']))
        c.execute("CREATE TRIGGER synthetic_inbox_failure BEFORE INSERT ON telegram_inbox BEGIN SELECT RAISE(ABORT,'test only'); END")
    event={'update_id':1,'message':{'from':{'id':99881},'chat':{'id':99881,'type':'private'},'date':int(time.time()),'text':'/in'}}
    with pytest.raises(sqlite3.IntegrityError):bot.process_update(event,'atomic-bot')
    assert bot.durable_offset('atomic-bot')==0
    with server.db() as c:
        assert c.execute('SELECT count(*) FROM attendance WHERE user_id=?',(u['id'],)).fetchone()[0]==0
        c.execute('DROP TRIGGER synthetic_inbox_failure')
    assert bot.process_update(event,'atomic-bot')['status']=='processed'


def test_concurrent_telegram_update_is_applied_once(client):
    auth(client);u=employee(client);dt=datetime.now(timezone.utc)
    shift(client,u['id'],(dt-timedelta(hours=1)).isoformat(),(dt+timedelta(hours=1)).isoformat())
    with server.db() as c:c.execute('UPDATE users SET telegram_user_id=? WHERE id=?',('442299',u['id']))
    event={'update_id':20,'message':{'from':{'id':442299},'chat':{'id':442299,'type':'private'},'date':int(time.time()),'text':'/in'}}
    with ThreadPoolExecutor(max_workers=2) as pool:assert all(r['status']=='processed' for r in pool.map(lambda _:bot.process_update(event,'concurrent-bot'),range(2)))
    with server.db() as c:
        assert c.execute('SELECT count(*) FROM attendance WHERE user_id=?',(u['id'],)).fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM telegram_inbox WHERE bot_id='concurrent-bot'").fetchone()[0]==1


def test_financial_mutations_require_csrf_including_refund_and_correction(client):
    auth(client);client.headers.pop('X-CSRF-Token')
    for path,method in [('/api/orders/1/pay','POST'),('/api/payments/1/refunds','POST'),('/api/payments/1/correction','PATCH'),('/api/cashier/shifts/1/close','POST')]:
        assert client.request(method,path,json={}).status_code==403


def test_telegram_missing_future_timestamp_and_foreign_chat_cannot_punch(client):
    auth(client);u=employee(client)
    with server.db() as c:c.execute('UPDATE users SET telegram_user_id=? WHERE id=?',('555990',u['id']))
    msg={'from':{'id':555990},'chat':{'id':555990,'type':'private'},'text':'/in'}
    assert bot.process_update({'update_id':1,'message':msg},'time-bot')['status']=='needs_review'
    assert bot.process_update({'update_id':2,'message':{**msg,'date':int(time.time())+7200}},'time-bot')['status']=='needs_review'
    assert bot.process_update({'update_id':3,'message':{**msg,'chat':{'id':555991,'type':'private'},'date':int(time.time())}},'time-bot')['status']=='ignored'
    with server.db() as c:assert c.execute('SELECT count(*) FROM attendance WHERE user_id=?',(u['id'],)).fetchone()[0]==0


def test_inbox_review_requires_reason_and_is_audited_once(client):
    auth(client);u=employee(client)
    with server.db() as c:
        c.execute('UPDATE users SET telegram_user_id=? WHERE id=?',('330911',u['id']))
    msg={'from':{'id':330911},'chat':{'id':330911,'type':'private'},'text':'/out','date':int(time.time())}
    assert bot.process_update({'update_id':12,'message':msg},'review-bot')['status']=='needs_review'
    body={'bot_id':'review-bot','update_id':12,'reason':'خروج بدون ورود؛ بررسی و رد درخواست'}
    assert client.post('/api/admin/telegram-inbox/review',json={**body,'reason':' '}).status_code==422
    assert client.post('/api/admin/telegram-inbox/review',json=body).status_code==200
    assert client.post('/api/admin/telegram-inbox/review',json=body).status_code==409
    assert client.get('/api/admin/telegram-inbox').json()==[]
    assert any(x['kind']=='telegram_reviewed' for x in client.get('/api/admin/audit').json())


def test_hours_reject_unrepresentable_end_date(client):
    auth(client)
    assert client.get('/api/attendance/hours?end=9999-12-31').status_code==422
