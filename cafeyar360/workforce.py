"""Additive employee, table and attendance services for the integrated app.

All writes use the caller's SQLite transaction; no credentials enter audit payloads.
"""
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone, date
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import Field, field_validator
from typing import Literal

TEHRAN = ZoneInfo('Asia/Tehran')


def backup_before_upgrade():
    path=s.DB_PATH
    if not path.exists():return
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as source:
        tables={r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'users' not in tables or not source.execute('SELECT 1 FROM users LIMIT 1').fetchone():return
        if 'schema_migrations' in tables and source.execute('SELECT 1 FROM schema_migrations WHERE version=4').fetchone():return
        target=path.parent/'backups'/('before-workforce-v4-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-'+secrets.token_hex(4)+'.sqlite3')
        target.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(target) as output:source.backup(output)


def migrate(c):
    additions = {
        'users': [('active', 'INTEGER NOT NULL DEFAULT 1'), ('revision', 'INTEGER NOT NULL DEFAULT 0')],
        'sessions': [('created_at', 'REAL NOT NULL DEFAULT 0')],
        'cafe_tables': [('display_number', 'INTEGER'), ('title', "TEXT NOT NULL DEFAULT ''"),
                        ('capacity', 'INTEGER NOT NULL DEFAULT 4'), ('revision', 'INTEGER NOT NULL DEFAULT 0')],
        'orders': [('table_number_snapshot', 'INTEGER')],
        'attendance': [('shift_id', 'INTEGER REFERENCES employee_shifts(id)'), ('revision', 'INTEGER NOT NULL DEFAULT 0'),
                       ('voided', 'INTEGER NOT NULL DEFAULT 0')],
    }
    # executescript would commit an active migration transaction; use execute individually.
    statements = [
        '''CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY, actor_id INTEGER REFERENCES users(id),
           kind TEXT NOT NULL, target TEXT NOT NULL, reason TEXT NOT NULL, before_json TEXT, after_json TEXT, created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS security_events(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, identity TEXT NOT NULL, created_at REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS login_attempts(identity TEXT PRIMARY KEY, failures INTEGER NOT NULL, window_start REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS employee_shifts(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
           starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, actor_id INTEGER NOT NULL REFERENCES users(id))''',
        '''CREATE INDEX IF NOT EXISTS employee_shift_user ON employee_shifts(user_id,starts_at)''',
        '''CREATE TABLE IF NOT EXISTS table_numbers(number INTEGER PRIMARY KEY, table_id INTEGER NOT NULL REFERENCES cafe_tables(number))''',
        '''CREATE TABLE IF NOT EXISTS telegram_inbox(bot_id TEXT NOT NULL, update_id INTEGER NOT NULL, sender_id TEXT,
           event_at TEXT, status TEXT NOT NULL, response TEXT, chat_id TEXT, delivered INTEGER NOT NULL DEFAULT 0,
           created_at TEXT NOT NULL, PRIMARY KEY(bot_id,update_id))''',
        '''CREATE TABLE IF NOT EXISTS telegram_offsets(bot_id TEXT PRIMARY KEY, next_offset INTEGER NOT NULL)''',
        '''CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT,'immutable audit'); END''',
        '''CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT,'immutable audit'); END''',
    ]
    for sql in statements:
        c.execute(sql)
    for table, columns in additions.items():
        present = {r['name'] for r in c.execute('PRAGMA table_info(' + table + ')')}
        for name, definition in columns:
            if name not in present:
                c.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + name + ' ' + definition)
    if not c.execute('SELECT 1 FROM schema_migrations WHERE version=4').fetchone():
        for row in c.execute('SELECT id,created_at FROM attendance').fetchall():
            c.execute('UPDATE attendance SET created_at=? WHERE id=?',(iso(stamp(row['created_at'])),row['id']))
    sync_tables(c)
    c.execute('UPDATE orders SET table_number_snapshot=table_num WHERE table_number_snapshot IS NULL AND table_num IS NOT NULL')
    c.execute('INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(4,?)', (s.now(),))


def sync_tables(c):
    c.execute('UPDATE cafe_tables SET display_number=number WHERE display_number IS NULL')
    c.execute('INSERT OR IGNORE INTO table_numbers(number,table_id) SELECT number,number FROM cafe_tables')


def audit(c, actor, kind, target, reason, before=None, after=None):
    c.execute('INSERT INTO audit_log(actor_id,kind,target,reason,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?)',
              (actor, kind, str(target), reason, json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), s.now()))


def stamp(value):
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            raise ValueError()
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        s.api_error(422, 'زمان باید ISO و دارای منطقه زمانی باشد')


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds')


def active_user(c, uid):
    u = c.execute('SELECT * FROM users WHERE id=? AND active=1', (uid,)).fetchone()
    if not u:
        s.api_error(403, 'حساب فعال نیست')
    return u


def active_admin(c,uid):
    user=active_user(c,uid)
    if user['role']!='admin':s.api_error(403,'دسترسی مدیر لازم است')
    return user


def safe_user(row):
    return {k: row[k] for k in ('id', 'name', 'username', 'role', 'active', 'revision', 'telegram_user_id')}


def revoke(c, uid):
    c.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
    c.execute('DELETE FROM telegram_links WHERE user_id=?', (uid,))
    c.execute('UPDATE users SET telegram_user_id=NULL WHERE id=?', (uid,))


def resolve_table(c, number):
    return c.execute('SELECT t.* FROM table_numbers n JOIN cafe_tables t ON t.number=n.table_id WHERE n.number=?', (number,)).fetchone()


def table_dict(row):
    return {'id': row['number'], 'number': row['display_number'], 'title': row['title'],
            'capacity': row['capacity'], 'active': bool(row['active']), 'revision': row['revision']}


def punch(c, uid, action, source, at=None):
    active_user(c, uid)
    if action not in ('in', 'out'):
        s.api_error(422, 'نوع تردد نامعتبر است')
    event = stamp(at) if at else datetime.now(timezone.utc)
    if event > datetime.now(timezone.utc) + timedelta(minutes=1):
        s.api_error(409, 'زمان تردد در آینده است؛ بررسی مدیر لازم است')
    last = c.execute('SELECT * FROM attendance WHERE user_id=? AND voided=0 ORDER BY created_at DESC,id DESC LIMIT 1', (uid,)).fetchone()
    if last and event < stamp(last['created_at']):
        s.api_error(409, 'تردد قدیمی یا خارج از ترتیب؛ اصلاح مدیر لازم است')
    if (not last and action == 'out') or (last and last['action'] == action):
        s.api_error(409, 'ترتیب ورود و خروج معتبر نیست')
    candidates=[r for r in c.execute('SELECT * FROM employee_shifts WHERE user_id=? ORDER BY starts_at DESC',(uid,)) if stamp(r['starts_at'])<=event<=stamp(r['ends_at'])]
    shift=next((r for r in candidates if action=='out' and last and r['id']==last['shift_id']),None) if action=='out' else (candidates[0] if candidates else None)
    if not shift:
        s.api_error(409, 'ورود و خروج باید داخل شیفت تعیین‌شده باشد؛ اصلاح مدیر لازم است')
    if action == 'out' and last['shift_id'] != shift['id']:
        s.api_error(409, 'ورود باز مربوط به شیفت دیگری است؛ اصلاح مدیر لازم است')
    eid = c.execute('INSERT INTO attendance(user_id,action,source,created_at,shift_id) VALUES(?,?,?,?,?)',
                    (uid, action, source, iso(event), shift['id'])).lastrowid
    return {'id': eid, 'action': action, 'created_at': iso(event)}


def csrf(raw):
    return hashlib.sha256(('csrf:' + raw).encode()).hexdigest()


def security(c, kind, identity):
    c.execute('INSERT INTO security_events(kind,identity,created_at) VALUES(?,?,?)', (kind, identity, time.time()))


def login(c, username, password, host):
    # Both account+IP and aggregate IP limits resist username cycling; no raw identifiers logged.
    keys = [hashlib.sha256((host + '|' + username).encode()).hexdigest(), hashlib.sha256(('ip|' + host).encode()).hexdigest()]
    limits = (5, 30)
    ts = time.time()
    c.execute('BEGIN IMMEDIATE')
    for key, limit in zip(keys, limits):
        row = c.execute('SELECT * FROM login_attempts WHERE identity=?', (key,)).fetchone()
        if row and ts - row['window_start'] < 900 and row['failures'] >= limit:
            security(c, 'login_limited', key)
            c.execute('COMMIT')
            s.api_error(429, 'تلاش‌های ورود بیش از حد است؛ ۱۵ دقیقه بعد تلاش کنید')
    u = c.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    valid = s.verify(password, u['password_hash'] if u else DUMMY_HASH)
    if not u or not u['active'] or not valid:
        for key in keys:
            c.execute('''INSERT INTO login_attempts(identity,failures,window_start) VALUES(?,1,?)
              ON CONFLICT(identity) DO UPDATE SET failures=CASE WHEN ?-window_start>=900 THEN 1 ELSE failures+1 END,
              window_start=CASE WHEN ?-window_start>=900 THEN ? ELSE window_start END''', (key, ts, ts, ts, ts))
        security(c, 'login_failed', keys[0])
        c.execute('COMMIT')
        s.api_error(401, 'نام کاربری یا رمز عبور نادرست است')
    c.execute('DELETE FROM login_attempts WHERE identity=?', (keys[0],))
    raw = secrets.token_urlsafe(32)
    c.execute('INSERT INTO sessions(token_hash,user_id,expires,created_at) VALUES(?,?,?,?)',
              (hashlib.sha256(raw.encode()).hexdigest(), u['id'], ts + 12 * 3600, ts))
    c.execute('COMMIT')
    return u, raw


def register(server):
    global s, DUMMY_HASH
    s = server
    DUMMY_HASH = s.hpass(secrets.token_urlsafe(24))
    app = s.app
    class Base(s.InputModel):
        @field_validator('reason',check_fields=False)
        @classmethod
        def reason_required(cls,v):
            if not v.strip():raise ValueError('دلیل خالی است')
            return v.strip()
    admin = s.allowed('admin')

    @app.middleware('http')
    async def mutation_guard(request, call_next):
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and request.url.path.startswith('/api/'):
            origin = request.headers.get('origin')
            source = urlsplit(origin) if origin else None
            if source and (source.scheme, source.netloc) != (request.url.scheme, request.url.netloc):
                return JSONResponse({'detail': 'مبدأ درخواست مجاز نیست'}, status_code=403)
            raw = request.cookies.get(s.COOKIE_NAME)
            if raw and request.url.path not in ('/api/auth/login', '/api/orders'):
                with s.db() as c:
                    row = c.execute('SELECT 1 FROM sessions JOIN users ON users.id=sessions.user_id WHERE token_hash=? AND expires>? AND users.active=1',
                                    (hashlib.sha256(raw.encode()).hexdigest(), time.time())).fetchone()
                    if row and not hmac.compare_digest(request.headers.get('x-csrf-token', ''), csrf(raw)):
                        security(c, 'csrf_rejected', hashlib.sha256(raw.encode()).hexdigest()[:16])
                        return JSONResponse({'detail': 'توکن CSRF نامعتبر است'}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    class Employee(Base):
        name: str = Field(min_length=1, max_length=100)
        username: str = Field(min_length=3, max_length=60, pattern=r'^[a-zA-Z0-9_.-]+$')
        role: Literal['admin', 'cashier', 'barista', 'staff']
        active: bool = True
        password: str | None = Field(default=None, min_length=12, max_length=256)
        revision: int = Field(default=0, ge=0)
        reason: str = Field(min_length=3, max_length=500)

        @field_validator('name', 'reason')
        @classmethod
        def meaningful(cls, v):
            if not v.strip():
                raise ValueError('متن خالی است')
            return v.strip()

    @app.get('/api/admin/employees')
    def employees(user=Depends(admin)):
        with s.db() as c:
            return [safe_user(r) for r in c.execute('SELECT * FROM users ORDER BY id')]

    @app.post('/api/admin/employees', status_code=201)
    def create_employee(body: Employee, user=Depends(admin)):
        if not body.password:
            s.api_error(422, 'رمز اولیه ضروری است')
        with s.db() as c:
            c.execute('BEGIN IMMEDIATE')
            active_admin(c, user['id'])
            uid = c.execute('INSERT INTO users(name,username,role,active,password_hash) VALUES(?,?,?,?,?)',
                            (body.name, body.username, body.role, int(body.active), s.hpass(body.password))).lastrowid
            result = safe_user(c.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone())
            audit(c, user['id'], 'employee_created', uid, body.reason, after=result)
            c.execute('COMMIT')
            return result

    @app.put('/api/admin/employees/{uid}')
    def update_employee(uid: int, body: Employee, user=Depends(admin)):
        with s.db() as c:
            c.execute('BEGIN IMMEDIATE')
            active_admin(c, user['id'])
            old = c.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
            if not old:
                s.api_error(404, 'کارمند یافت نشد')
            if old['revision'] != body.revision:
                s.api_error(409, 'رکورد تغییر کرده است؛ تازه‌سازی کنید')
            if old['active'] and old['role'] == 'admin' and (not body.active or body.role != 'admin'):
                if c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0] <= 1:
                    s.api_error(409, 'آخرین مدیر فعال قابل غیرفعال‌سازی نیست')
            c.execute('UPDATE users SET name=?,username=?,role=?,active=?,revision=revision+1 WHERE id=?',
                      (body.name, body.username, body.role, int(body.active), uid))
            if body.password:
                c.execute('UPDATE users SET password_hash=? WHERE id=?', (s.hpass(body.password), uid))
            if body.password or not body.active or body.role != old['role']:
                revoke(c, uid)
            result = safe_user(c.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone())
            audit(c, user['id'], 'employee_updated', uid, body.reason, safe_user(old),
                  {**result, 'password_changed': bool(body.password)})
            c.execute('COMMIT')
            return result

    @app.get('/api/admin/sessions')
    def sessions(user=Depends(admin)):
        with s.db() as c:
            return [dict(r) for r in c.execute('SELECT token_hash AS id,user_id,created_at,expires FROM sessions WHERE expires>?', (time.time(),))]

    class Revoke(Base):
        reason: str = Field(min_length=3, max_length=500)

    @app.delete('/api/admin/sessions/{sid}')
    def revoke_session(sid: str, body: Revoke, user=Depends(admin)):
        with s.db() as c:
            c.execute('BEGIN IMMEDIATE')
            active_admin(c, user['id'])
            row = c.execute('SELECT user_id FROM sessions WHERE token_hash=?', (sid,)).fetchone()
            if not row:
                s.api_error(404, 'نشست یافت نشد')
            c.execute('DELETE FROM sessions WHERE token_hash=?', (sid,))
            audit(c, user['id'], 'session_revoked', row['user_id'], body.reason)
            c.execute('COMMIT')
        return {'ok': True}

    @app.get('/api/admin/audit')
    def audit_list(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), user=Depends(admin)):
        with s.db() as c:
            return [dict(r) for r in c.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?', (limit, offset))]

    class Table(Base):
        number: int = Field(ge=1, le=9999)
        title: str = Field(default='', max_length=100)
        capacity: int = Field(ge=1, le=100)
        active: bool = True
        revision: int = Field(default=0, ge=0)
        reason: str = Field(min_length=3, max_length=500)

    @app.get('/api/public/tables/{number}')
    def public_table(number:int):
        with s.db() as c:
            table=resolve_table(c,number)
            if not table or not table['active']:s.api_error(404,'میز غیرفعال یا نامعتبر است')
            return table_dict(table)

    @app.get('/api/admin/tables')
    def tables(user=Depends(admin)):
        with s.db() as c:
            return [table_dict(r) for r in c.execute('SELECT * FROM cafe_tables ORDER BY display_number')]

    def save_table(c, tid, body, user):
        old = c.execute('SELECT * FROM cafe_tables WHERE number=?', (tid,)).fetchone() if tid else None
        if tid and not old:
            s.api_error(404, 'میز یافت نشد')
        if old and old['revision'] != body.revision:
            s.api_error(409, 'میز تغییر کرده است؛ تازه‌سازی کنید')
        owner = c.execute('SELECT table_id FROM table_numbers WHERE number=?', (body.number,)).fetchone()
        if owner and (not old or owner['table_id'] != tid):
            s.api_error(409, 'شماره فعلی یا تاریخی میز دیگری است')
        if old:
            c.execute('UPDATE cafe_tables SET display_number=?,title=?,capacity=?,active=?,revision=revision+1 WHERE number=?',
                      (body.number, body.title, body.capacity, int(body.active), tid))
        else:
            tid = body.number
            c.execute('INSERT INTO cafe_tables(number,display_number,title,capacity,active) VALUES(?,?,?,?,?)',
                      (tid, body.number, body.title, body.capacity, int(body.active)))
        c.execute('INSERT OR IGNORE INTO table_numbers(number,table_id) VALUES(?,?)', (body.number, tid))
        result = table_dict(c.execute('SELECT * FROM cafe_tables WHERE number=?', (tid,)).fetchone())
        audit(c, user['id'], 'table_saved', tid, body.reason, table_dict(old) if old else None, result)
        return result

    @app.post('/api/admin/tables', status_code=201)
    def create_table(body: Table, user=Depends(admin)):
        with s.db() as c:
            c.execute('BEGIN IMMEDIATE')
            active_admin(c, user['id'])
            result = save_table(c, None, body, user)
            c.execute('COMMIT')
            return result

    @app.put('/api/admin/tables/{tid}')
    def update_table(tid: int, body: Table, user=Depends(admin)):
        with s.db() as c:
            c.execute('BEGIN IMMEDIATE')
            active_admin(c, user['id'])
            result = save_table(c, tid, body, user)
            c.execute('COMMIT')
            return result

    @app.get('/tables/{tid}/print', response_class=HTMLResponse)
    def print_table(tid: int, user=Depends(admin)):
        from html import escape
        with s.db() as c:
            t = c.execute('SELECT * FROM cafe_tables WHERE number=?', (tid,)).fetchone()
            if not t:
                s.api_error(404, 'میز یافت نشد')
            html = f'''<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><title>QR میز {t['display_number']}</title>
            <link rel="stylesheet" href="/static/styles.css"><script src="/static/receipt.js" defer></script><body><main class="panel"><h1>کافه نیما — میز {t['display_number']}</h1>
            <p>{escape(t['title'])} · ظرفیت {t['capacity']}</p><img width="300" height="300" alt="QR میز" src="/api/qr/{t['display_number']}">
            <p>کافه یار ۳۶۰</p><button id="print-receipt" class="btn btn-primary">چاپ QR</button></main></body></html>'''
            return HTMLResponse(html, headers={'Cache-Control': 'no-store','Content-Security-Policy':"default-src 'none'; style-src 'self'; img-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'"})

    class Shift(Base):
        user_id: int = Field(ge=1)
        starts_at: str = Field(max_length=64)
        ends_at: str = Field(max_length=64)
        revision: int = Field(default=0, ge=0)
        reason: str = Field(min_length=3, max_length=500)

    @app.get('/api/employee-shifts')
    def employee_shifts(user=Depends(s.current_user)):
        with s.db() as c:
            return [dict(r) for r in c.execute('SELECT * FROM employee_shifts WHERE (?=1 OR user_id=?) ORDER BY starts_at',
                                               (int(user['role'] == 'admin'), user['id']))]

    def save_shift(sid, body, user):
        start, end = stamp(body.starts_at), stamp(body.ends_at)
        if end <= start or end - start > timedelta(hours=24):
            s.api_error(422, 'پایان شیفت باید پس از شروع و حداکثر ۲۴ ساعت باشد')
        with s.db() as c:
            c.execute('BEGIN IMMEDIATE')
            active_admin(c, user['id'])
            active_user(c, body.user_id)
            old = c.execute('SELECT * FROM employee_shifts WHERE id=?', (sid,)).fetchone() if sid else None
            if sid and not old:
                s.api_error(404, 'شیفت یافت نشد')
            if old and old['revision'] != body.revision:
                s.api_error(409, 'شیفت تغییر کرده است')
            for r in c.execute('SELECT * FROM employee_shifts WHERE user_id=? AND id<>?', (body.user_id, sid or -1)):
                if start < stamp(r['ends_at']) and end > stamp(r['starts_at']):
                    s.api_error(409, 'شیفت هم‌پوشان است')
            if old:
                for a in c.execute('SELECT * FROM attendance WHERE shift_id=? AND voided=0', (sid,)):
                    if body.user_id != old['user_id'] or not start <= stamp(a['created_at']) <= end:
                        s.api_error(409, 'شیفت دارای تردد معتبر را نمی‌توان ناسازگار تغییر داد')
                c.execute('UPDATE employee_shifts SET starts_at=?,ends_at=?,user_id=?,revision=revision+1 WHERE id=?',
                          (iso(start), iso(end), body.user_id, sid))
            else:
                sid = c.execute('INSERT INTO employee_shifts(user_id,starts_at,ends_at,actor_id) VALUES(?,?,?,?)',
                                (body.user_id, iso(start), iso(end), user['id'])).lastrowid
            result = dict(c.execute('SELECT * FROM employee_shifts WHERE id=?', (sid,)).fetchone())
            audit(c, user['id'], 'employee_shift_saved', sid, body.reason, dict(old) if old else None, result)
            c.execute('COMMIT')
            return result

    @app.post('/api/admin/employee-shifts', status_code=201)
    def create_shift(body: Shift, user=Depends(admin)):
        return save_shift(None, body, user)

    @app.put('/api/admin/employee-shifts/{sid}')
    def update_shift(sid: int, body: Shift, user=Depends(admin)):
        return save_shift(sid, body, user)

    class Correction(Base):
        user_id: int = Field(ge=1)
        event_id: int | None = Field(default=None, ge=1)
        action: Literal['in', 'out']
        at: str = Field(max_length=64)
        voided: bool = False
        revision: int = Field(default=0, ge=0)
        reason: str = Field(min_length=3, max_length=500)

    @app.post('/api/admin/attendance/corrections')
    def correction(body: Correction, user=Depends(admin)):
        dt = stamp(body.at)
        if dt > datetime.now(timezone.utc) + timedelta(minutes=1):
            s.api_error(422, 'تردد آینده مجاز نیست')
        with s.db() as c:
            c.execute('BEGIN IMMEDIATE')
            active_admin(c, user['id'])
            if not c.execute('SELECT 1 FROM users WHERE id=?', (body.user_id,)).fetchone():
                s.api_error(404, 'کارمند یافت نشد')
            old = c.execute('SELECT * FROM attendance WHERE id=? AND user_id=?', (body.event_id, body.user_id)).fetchone()
            if body.event_id and not old:
                s.api_error(404, 'تردد یافت نشد')
            if old and old['revision'] != body.revision:
                s.api_error(409, 'تردد تغییر کرده است')
            if old:
                eid = old['id']
                c.execute('UPDATE attendance SET action=?,created_at=?,voided=?,shift_id=NULL,revision=revision+1 WHERE id=?',
                          (body.action, iso(dt), int(body.voided), eid))
            else:
                if body.voided:
                    s.api_error(422, 'تردد جدید قابل ابطال نیست')
                eid = c.execute('INSERT INTO attendance(user_id,action,source,created_at) VALUES(?,?,?,?)',
                                (body.user_id, body.action, 'manager', iso(dt))).lastrowid
            expected = 'in'
            for a in c.execute('SELECT * FROM attendance WHERE user_id=? AND voided=0 ORDER BY created_at,id', (body.user_id,)):
                if a['action'] != expected:
                    s.api_error(409, 'اصلاح باعث ترتیب نامعتبر ترددها می‌شود')
                expected = 'out' if expected == 'in' else 'in'
            result = dict(c.execute('SELECT * FROM attendance WHERE id=?', (eid,)).fetchone())
            audit(c, user['id'], 'attendance_corrected', eid, body.reason, dict(old) if old else None, result)
            c.execute('COMMIT')
            return result

    @app.get('/api/attendance/hours')
    def hours(start: date | None = None, end: date | None = None, user=Depends(s.current_user)):
        if end==date.max:s.api_error(422,'تاریخ پایان بیش از محدوده مجاز است')
        if start and end and start > end:
            s.api_error(422, 'بازه تاریخ معتبر نیست')
        lower = datetime.combine(start, datetime.min.time(), TEHRAN).astimezone(timezone.utc) if start else None
        upper = datetime.combine(end + timedelta(days=1), datetime.min.time(), TEHRAN).astimezone(timezone.utc) if end else None
        with s.db() as c:
            people = c.execute('SELECT id,name FROM users WHERE (?=1 OR id=?)', (int(user['role'] == 'admin'), user['id'])).fetchall()
            result = []
            for person in people:
                total, opened = 0, None
                for a in c.execute('SELECT * FROM attendance WHERE user_id=? AND voided=0 ORDER BY created_at,id', (person['id'],)):
                    at = stamp(a['created_at'])
                    if a['action'] == 'in':
                        opened = at
                    elif opened is not None:
                        left = max(opened, lower) if lower else opened
                        right = min(at, upper) if upper else at
                        total += max(0, (right - left).total_seconds())
                        opened = None
                result.append({'user_id': person['id'], 'name': person['name'], 'seconds': total,
                               'hours': round(total / 3600, 4), 'open_since': iso(opened) if opened else None})
        return {'timezone': 'Asia/Tehran', 'items': result}



    class InboxReview(Base):
        bot_id:str=Field(min_length=1,max_length=100)
        update_id:int=Field(ge=0)
        correction_id:int|None=Field(default=None,ge=1)
        reason:str=Field(min_length=3,max_length=500)

    @app.post('/api/admin/telegram-inbox/review')
    def review_inbox(body:InboxReview,user=Depends(admin)):
        with s.db() as c:
            c.execute('BEGIN IMMEDIATE')
            active_admin(c,user['id'])
            row=c.execute('SELECT * FROM telegram_inbox WHERE bot_id=? AND update_id=?',(body.bot_id,body.update_id)).fetchone()
            if not row:s.api_error(404,'پیام یافت نشد')
            if row['status']!='needs_review':s.api_error(409,'پیام قبلاً بررسی شده است')
            if body.correction_id and not c.execute('SELECT 1 FROM attendance WHERE id=?',(body.correction_id,)).fetchone():s.api_error(422,'تردد اصلاح‌شده یافت نشد')
            c.execute("UPDATE telegram_inbox SET status='reviewed' WHERE bot_id=? AND update_id=?",(body.bot_id,body.update_id))
            audit(c,user['id'],'telegram_reviewed',str(body.update_id),body.reason,{'status':row['status']},{'status':'reviewed','correction_id':body.correction_id})
            c.execute('COMMIT')
        return {'ok':True}

    @app.get('/api/admin/telegram-inbox')
    def inbox(limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0),user=Depends(admin)):
        with s.db() as c:
            return [dict(r) for r in c.execute("SELECT bot_id,update_id,event_at,status,response FROM telegram_inbox WHERE status='needs_review' ORDER BY created_at DESC LIMIT ? OFFSET ?",(limit,offset))]


def telegram_message(c, message):
    """Called only with Bot API messages, never with browser-supplied Telegram IDs."""
    if message.get('chat', {}).get('type') != 'private':
        return None, 'ignored'
    sender = message.get('from', {}).get('id')
    chat = message.get('chat', {}).get('id')
    if not isinstance(sender, int) or sender != chat:
        return None, 'ignored'
    words = str(message.get('text', ''))[:1000].strip().split()
    if not words:
        return None, 'ignored'
    command = words[0].split('@')[0].lower()
    if command == '/start':
        return 'کد اتصال پنل را با /link CODE ارسال کنید؛ سپس /in و /out در ساعات شیفت.', 'processed'
    if command == '/link':
        if len(words) != 2:
            return 'کد را به صورت /link CODE وارد کنید.', 'rejected'
        link = c.execute('SELECT l.user_id FROM telegram_links l JOIN users u ON u.id=l.user_id WHERE code=? AND expires>? AND u.active=1',
                         (words[1].upper(), time.time())).fetchone()
        if not link:
            return 'کد اتصال نامعتبر یا منقضی شده است.', 'rejected'
        if c.execute('SELECT 1 FROM users WHERE telegram_user_id=? AND id<>?', (str(sender), link['user_id'])).fetchone():
            return 'حساب تلگرام قبلاً متصل شده است.', 'rejected'
        c.execute('UPDATE users SET telegram_user_id=? WHERE id=?', (str(sender), link['user_id']))
        c.execute('DELETE FROM telegram_links WHERE user_id=?', (link['user_id'],))
        audit(c, link['user_id'], 'telegram_linked', link['user_id'], 'اتصال با کد یک‌بارمصرف')
        return 'حساب تلگرام متصل شد.', 'processed'
    u = c.execute('SELECT id FROM users WHERE telegram_user_id=? AND active=1', (str(sender),)).fetchone()
    if not u:
        return 'ابتدا از پنل کد اتصال دریافت کنید؛ حساب باید فعال باشد.', 'rejected'
    if command == '/status':
        last = c.execute('SELECT action,created_at FROM attendance WHERE user_id=? AND voided=0 ORDER BY created_at DESC,id DESC LIMIT 1', (u['id'],)).fetchone()
        return 'آخرین ثبت: ' + (('ورود' if last['action'] == 'in' else 'خروج') + ' | ' + last['created_at'] if last else 'هیچ ترددی ثبت نشده'), 'processed'
    if command not in ('/in', '/out'):
        return 'دستورهای معتبر: /link CODE، /in، /out، /status', 'rejected'
    sent = message.get('date')
    if not isinstance(sent, int) or isinstance(sent, bool) or sent <= 0:
        return 'زمان پیام معتبر نیست؛ بررسی مدیر لازم است.', 'needs_review'
    try:
        at = iso(datetime.fromtimestamp(sent, timezone.utc))
        item = punch(c, u['id'], command[1:], 'telegram', at)
        return ('ورود' if command == '/in' else 'خروج') + ' شما ثبت شد. ' + item['created_at'], 'processed'
    except (OverflowError, ValueError, OSError):
        return 'زمان پیام معتبر نیست؛ بررسی مدیر لازم است.', 'needs_review'
    except s.HTTPException as exc:
        # punch validates before writing; failed message stays durably reviewable.
        audit(c, u['id'], 'telegram_punch_review', u['id'], str(exc.detail))
        return 'ثبت انجام نشد: ' + str(exc.detail), 'needs_review'


def telegram_update(bot_id, update):
    update_id = update.get('update_id')
    if not isinstance(update_id, int) or isinstance(update_id, bool) or update_id < 0:
        raise ValueError('Invalid Telegram update ID')
    message = update.get('message', {})
    with s.db() as c:
        c.execute('BEGIN IMMEDIATE')
        old = c.execute('SELECT * FROM telegram_inbox WHERE bot_id=? AND update_id=?', (bot_id, update_id)).fetchone()
        if old:
            c.execute('COMMIT')
            return dict(old)
        response, status = telegram_message(c, message)
        event_at = str(message.get('date', ''))[:30]
        chat = str(message.get('chat', {}).get('id', ''))[:40]
        c.execute('INSERT INTO telegram_inbox(bot_id,update_id,sender_id,event_at,status,response,chat_id,created_at) VALUES(?,?,?,?,?,?,?,?)',
                  (bot_id, update_id, str(message.get('from', {}).get('id', ''))[:40], event_at, status, response, chat, s.now()))
        c.execute('''INSERT INTO telegram_offsets(bot_id,next_offset) VALUES(?,?) ON CONFLICT(bot_id)
                     DO UPDATE SET next_offset=MAX(next_offset,excluded.next_offset)''', (bot_id, update_id + 1))
        row = dict(c.execute('SELECT * FROM telegram_inbox WHERE bot_id=? AND update_id=?', (bot_id, update_id)).fetchone())
        c.execute('COMMIT')
        return row
