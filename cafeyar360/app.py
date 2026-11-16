"""Cafe Nima: offline-first, integrated, local demonstration.
Python 3.11+, FastAPI, SQLite. This is demo software, not audited payment/accounting software.
"""
from html import escape

import hashlib
import hmac
import io
import json
import math
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from typing import Literal

import jdatetime

import qrcode
from fastapi import FastAPI, Request, HTTPException, Depends, Query
from fastapi.responses import HTMLResponse, Response, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ConfigDict, field_validator, ValidationError

ROOT = Path(__file__).parent
DB_PATH = Path(os.getenv('CAFE_DB_PATH', str(ROOT / 'cafe_nima.sqlite3')))
DEMO_MODE = os.getenv('DEMO_MODE', '0') == '1'
COOKIE_SECURE = os.getenv('COOKIE_SECURE', '0') == '1'
PRODUCT_NAME = 'کافه یار ۳۶۰'
CAFE_NAME = 'کافه نیما'
PUBLIC_BASE_URL = os.getenv('PUBLIC_BASE_URL', '').rstrip('/')
COOKIE_NAME = 'cafe_nima_session'
ROLES = {'admin', 'cashier', 'staff'}
STATUS_FLOW = {'new': {'accepted', 'cancelled'}, 'accepted': {'preparing', 'cancelled'}, 'preparing': {'ready'}, 'ready': {'completed'}, 'completed': set(), 'cancelled': set()}


def now():
    return datetime.now(ZoneInfo('Asia/Tehran')).isoformat(timespec='seconds')


@contextmanager
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    cx = sqlite3.connect(str(DB_PATH), timeout=15, isolation_level=None)
    cx.row_factory = sqlite3.Row
    try:
        cx.execute('PRAGMA foreign_keys=ON')
        cx.execute('PRAGMA busy_timeout=15000')
        cx.execute('PRAGMA journal_mode=WAL')
        yield cx
    except BaseException:
        if cx.in_transaction:
            cx.rollback()
        raise
    finally:
        cx.close()


def snapshot_recipe(c, oid):
    needs={}
    for row in c.execute('SELECT * FROM order_items WHERE order_id=?',(oid,)).fetchall():
        parts=list(c.execute('SELECT ingredient_id,quantity AS value FROM recipes WHERE product_id=?',(row['product_id'],)))
        parts+=list(c.execute('SELECT ingredient_id,delta AS value FROM addon_recipes WHERE addon_id=?',(row['addon_id'],)))
        for part in parts:
            iid=part['ingredient_id']
            needs[iid]=needs.get(iid,Decimal(0))+quantity_decimal(part['value'])*row['qty']
    for iid,needed in needs.items():
        if needed<0:api_error(422,'جایگزینی مواد نباید مصرف منفی بسازد')
        if needed:c.execute('INSERT INTO order_materials(order_id,ingredient_id,quantity) VALUES(?,?,?)',(oid,iid,float(needed)))
    c.execute('UPDATE orders SET recipe_snapshot=1 WHERE id=?',(oid,))


def migrate(c):
    # Additive and atomic: pending legacy orders are frozen once at upgrade time.
    c.execute('BEGIN IMMEDIATE')
    columns={r['name'] for r in c.execute('PRAGMA table_info(orders)')}
    if 'request_hash' not in columns:
        c.execute("ALTER TABLE orders ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''")
    if 'recipe_snapshot' not in columns:
        c.execute('ALTER TABLE orders ADD COLUMN recipe_snapshot INTEGER NOT NULL DEFAULT 0')
    payment_columns={r['name'] for r in c.execute('PRAGMA table_info(payments)')}
    if 'actor_id' not in payment_columns:
        c.execute('ALTER TABLE payments ADD COLUMN actor_id INTEGER REFERENCES users(id)')
    if 'revision' not in payment_columns:
        c.execute('ALTER TABLE payments ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
    for row in c.execute('SELECT id FROM orders WHERE recipe_snapshot=0 AND stock_applied=0').fetchall():
        snapshot_recipe(c,row['id'])
    c.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(1,?)",(now(),))
    c.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(2,?)",(now(),))
    _migrate_financial(c)
    c.execute('COMMIT')


def validate_existing_passwords(c):
    # Legacy public values are a deny-list only, never bootstrap credentials.
    for row in c.execute('SELECT username,password_hash FROM users'):
        if any(verify(pwd,row['password_hash']) for pwd in ('123456','234567','345678')):
            raise RuntimeError('Unsafe demo password: use python manage.py password USER before LAN startup')


def hpass(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 180000).hex()
    return f'{salt}:{digest}'


def verify(password, stored):
    try:
        salt, digest = stored.split(':')
        return hmac.compare_digest(hpass(password, salt).split(':')[1], digest)
    except (ValueError, AttributeError):
        return False


def asdict(row):
    return dict(row) if row else None


def api_error(code, detail):
    raise HTTPException(status_code=code, detail=detail)


# All canonical money values are integers in hundredths of one toman.
TEHRAN = ZoneInfo('Asia/Tehran')
MAX_MINOR = 900_000_000_000_000


def money_minor(value, strict=False):
    try:
        dec=Decimal(str(value))
        if not dec.is_finite():raise ValueError()
        rounded=dec.quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)
        if strict and dec!=rounded:raise ValueError()
        result=int(rounded*100)
        if abs(result)>MAX_MINOR:raise ValueError()
        return result
    except (ValueError,InvalidOperation,OverflowError):
        api_error(422,'مبلغ خارج از محدوده یا دقت مجاز دو رقم اعشار است')


def quantity_decimal(value):
    try:
        dec=Decimal(str(value))
        if not dec.is_finite() or abs(dec)>Decimal('1000000000000') or dec!=dec.quantize(Decimal('.000001')):raise ValueError()
        return dec
    except (InvalidOperation,ValueError):api_error(422,'مقدار مواد باید حداکثر شش رقم اعشار و در محدوده مجاز باشد')


def local_datetime(value):
    stamp=datetime.fromisoformat(value)
    if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=TEHRAN)
    return stamp.astimezone(TEHRAN)


def _migrate_financial(c):
    additions={
        'payments':{'shift_id':'INTEGER REFERENCES cashier_shifts(id)','exception_id':'INTEGER REFERENCES financial_exceptions(id)'},
        'payment_corrections':{'shift_id':'INTEGER REFERENCES cashier_shifts(id)','exception_id':'INTEGER REFERENCES financial_exceptions(id)'},
        'ingredients':{'unit_cost_minor':'INTEGER'},
        'expenses':{'amount_minor':'INTEGER'},
        'orders':{'cost_total_minor':'INTEGER','waste_cost_minor':'INTEGER NOT NULL DEFAULT 0','wasted_at':'TEXT'},
    }
    for table,columns in additions.items():
        existing={r['name'] for r in c.execute('PRAGMA table_info('+table+')')}
        for name,kind in columns.items():
            if name not in existing:c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {kind}')
    for table,old,new in [('ingredients','unit_cost','unit_cost_minor'),('expenses','amount','amount_minor'),('orders','cost_total','cost_total_minor')]:
        for row in c.execute(f'SELECT id,{old} FROM {table} WHERE {new} IS NULL').fetchall():
            if Decimal(str(row[old]))<0:raise RuntimeError('Negative legacy financial value; restore backup and review before upgrading')
            c.execute(f'UPDATE {table} SET {new}=? WHERE id=?',(money_minor(row[old]),row['id']))
    if not c.execute('SELECT 1 FROM schema_migrations WHERE version=3').fetchone():
        for table,columns in [('ingredients',('stock','reorder')),('recipes',('quantity',)),('addon_recipes',('delta',)),('order_materials',('quantity',))]:
            for column in columns:
                for row in c.execute(f'SELECT rowid AS rid,{column} AS value FROM {table}').fetchall():
                    value=Decimal(str(row['value']));rounded=value.quantize(Decimal('.000001'),rounding=ROUND_HALF_UP)
                    if value!=rounded:
                        if abs(value-rounded)>Decimal('.000000000001'):raise RuntimeError('Legacy ingredient precision exceeds six decimals; review on a copy before upgrade')
                        c.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?',(float(rounded),row['rid']))
    c.execute('INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(3,?)',(now(),))


def cached_operation(c,kind,key,payload,user):
    fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if not key:return fingerprint,None
    old=c.execute('SELECT * FROM financial_operation_keys WHERE kind=? AND key=?',(kind,key)).fetchone()
    if old:
        if old['fingerprint']!=fingerprint or old['actor_id']!=user['id']:api_error(409,'کلید تکرار عملیات مالی متفاوت است')
        return fingerprint,json.loads(old['response'])
    return fingerprint,None


def save_operation(c,kind,key,fingerprint,user,result):
    if key:c.execute('INSERT INTO financial_operation_keys(kind,key,fingerprint,actor_id,response) VALUES(?,?,?,?,?)',(kind,key,fingerprint,user['id'],json.dumps(result)))


def financial_context(c,user,authorization,kind,target_id):
    shift=c.execute('SELECT id FROM cashier_shifts WHERE closed_at IS NULL').fetchone()
    if shift:return shift['id'],None
    if user['role']!='admin' or authorization is None:
        api_error(409,'شیفت صندوق باز نیست؛ شیفت را باز کنید یا درخواست هماهنگی برای مدیر بفرستید')
    exc=c.execute('INSERT INTO financial_exceptions(kind,target_id,coordination,reference,reason,actor_id,created_at) VALUES(?,?,?,?,?,?,?)',
        (kind,target_id,authorization.coordination,authorization.reference.strip(),authorization.reason.strip(),user['id'],now())).lastrowid
    return None,exc


def movement(c,kind,source,amount,method,context,user):
    if context[0] is not None and method=='cash' and amount<0:
        opening=c.execute('SELECT opening_minor FROM cashier_shifts WHERE id=?',(context[0],)).fetchone()[0]
        net=c.execute("SELECT COALESCE(SUM(amount_minor),0) FROM cash_movements WHERE shift_id=? AND method='cash'",(context[0],)).fetchone()[0]
        if opening+net+amount<0:api_error(409,'وجه نقد صندوق کافی نیست؛ روش پرداخت یا شمارش صندوق را بررسی کنید')
    c.execute('INSERT INTO cash_movements(kind,source_id,amount_minor,method,shift_id,exception_id,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?)',
        (kind,source,amount,method,*context,user['id'],now()))


def consume_materials(c,order,waste=False):
    if order['stock_applied']:return
    materials=c.execute('SELECT m.*,g.stock,g.name,g.unit_cost,g.unit_cost_minor FROM order_materials m JOIN ingredients g ON g.id=m.ingredient_id WHERE m.order_id=?',(order['id'],)).fetchall()
    cost=Decimal(0)
    for row in materials:
        needed=quantity_decimal(row['quantity']);stock=quantity_decimal(row['stock'])
        if stock<needed:api_error(409,f'موجودی {row["name"]} کافی نیست')
        unit=row['unit_cost_minor'] if row['unit_cost_minor'] is not None else money_minor(row['unit_cost'])
        cost+=needed*Decimal(unit)
        c.execute('UPDATE ingredients SET stock=? WHERE id=?',(float(stock-needed),row['ingredient_id']))
        c.execute('INSERT INTO inventory_moves(ingredient_id,delta,reason,order_id,created_at) VALUES(?,?,?,?,?)',(row['ingredient_id'],-float(needed),'waste' if waste else 'sale',order['id'],now()))
    cost_minor=int(cost.quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    if cost_minor>MAX_MINOR:api_error(422,'بهای سفارش خارج از محدوده مجاز است')
    if waste:
        c.execute('UPDATE orders SET stock_applied=1,waste_cost_minor=?,wasted_at=? WHERE id=?',(cost_minor,now(),order['id']))
    else:
        stamp=now()
        c.execute('UPDATE orders SET cost_total=?,cost_total_minor=?,stock_applied=1,completed_at=? WHERE id=?',(cost_minor/100,cost_minor,stamp,order['id']))
        c.execute('UPDATE refunds SET recognized_at=? WHERE payment_id IN (SELECT id FROM payments WHERE order_id=?) AND recognized_at IS NULL',(stamp,order['id']))


def apply_payment(c,oid,body,user):
    if body.method not in ('cash','pos'):api_error(422,'روش پرداخت معتبر نیست')
    order=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
    if not order:api_error(404,'سفارش پیدا نشد')
    if order['status']=='cancelled':api_error(409,'سفارش لغوشده قابل تسویه نیست')
    if c.execute('SELECT 1 FROM payments WHERE order_id=?',(oid,)).fetchone():api_error(409,'این سفارش قبلاً تسویه شده است')
    amount=money_minor(order['total'])
    context=financial_context(c,user,body.authorization,'payment',oid)
    pid=c.execute('INSERT INTO payments(order_id,amount,method,reference,created_at,actor_id,shift_id,exception_id) VALUES(?,?,?,?,?,?,?,?)',
        (oid,order['total'],body.method,body.reference.strip(),now(),user['id'],*context)).lastrowid
    movement(c,'payment',pid,amount,body.method,context,user)
    c.execute('UPDATE orders SET payment_method=? WHERE id=?',(body.method,oid))
    return {'ok':True,'paid':order['total'],'method':body.method,'payment_id':pid}


def apply_correction(c,pid,body,user):
    if body.method not in ('cash','pos') or len(body.reason.strip())<3:api_error(422,'روش و دلیل اصلاح معتبر نیست')
    pay=c.execute('SELECT * FROM payments WHERE id=?',(pid,)).fetchone()
    if not pay:api_error(404,'پرداخت پیدا نشد')
    if pay['revision']!=body.expected_revision:api_error(409,'پرداخت تغییر کرده است؛ رسید را تازه کنید')
    reference=body.reference.strip()
    if pay['method']==body.method and pay['reference']==reference:api_error(409,'تغییری وجود ندارد')
    context=financial_context(c,user,body.authorization,'correction',pid)
    aid=c.execute('INSERT INTO payment_corrections(payment_id,old_method,new_method,old_reference,new_reference,reason,actor_id,created_at,shift_id,exception_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (pid,pay['method'],body.method,pay['reference'],reference,body.reason.strip(),user['id'],now(),*context)).lastrowid
    # Closed/legacy shifts are immutable. Reclassification does not move today's cash.
    if context[0] is not None and pay['shift_id']==context[0] and pay['method']!=body.method:
        movement(c,'correction_from',aid,-money_minor(pay['amount']),pay['method'],context,user)
        movement(c,'correction_to',aid,money_minor(pay['amount']),body.method,context,user)
    c.execute('UPDATE payments SET method=?,reference=?,revision=revision+1 WHERE id=?',(body.method,reference,pid))
    c.execute('UPDATE orders SET payment_method=? WHERE id=?',(body.method,pay['order_id']))
    return {'id':aid,'revision':body.expected_revision+1}


def apply_refund(c,pid,body,user):
    amount=money_minor(body.amount,strict=True)
    if amount<=0:api_error(422,'مبلغ استرداد باید مثبت باشد')
    fingerprint={'amount_minor':amount,'method':body.method,'reason':body.reason,'reference':body.reference.strip(),'expected_revision':body.expected_revision,'idempotency_key':body.idempotency_key}
    digest=hashlib.sha256(json.dumps(fingerprint,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    old=c.execute('SELECT * FROM refunds WHERE idempotency_key=?',(body.idempotency_key,)).fetchone()
    if old:
        if old['request_hash']!=digest or old['payment_id']!=pid:api_error(409,'کلید تکرار متفاوت است')
        return {'id':old['id'],'amount_minor':old['amount_minor'],'duplicate':True}
    pay=c.execute('SELECT * FROM payments WHERE id=?',(pid,)).fetchone()
    if not pay:api_error(404,'پرداخت پیدا نشد')
    returned=c.execute('SELECT COALESCE(SUM(amount_minor),0) FROM refunds WHERE payment_id=?',(pid,)).fetchone()[0]
    remaining=money_minor(pay['amount'])-returned
    if amount>remaining:api_error(409,'مبلغ از مانده قابل استرداد بیشتر است')
    if pay['revision']!=body.expected_revision:api_error(409,'پرداخت تغییر کرده است؛ رسید را تازه کنید')
    context=financial_context(c,user,body.authorization,'refund',pid)
    order=c.execute('SELECT * FROM orders WHERE id=?',(pay['order_id'],)).fetchone()
    if order['status']=='cancelled':api_error(409,'سفارش قبلاً لغو شده است')
    stamp=now()
    rid=c.execute('INSERT INTO refunds(payment_id,amount_minor,method,reason,reference,actor_id,created_at,recognized_at,shift_id,exception_id,idempotency_key,request_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (pid,amount,body.method,body.reason.strip(),body.reference.strip(),user['id'],stamp,stamp if order['status']=='completed' else None,*context,body.idempotency_key,digest)).lastrowid
    movement(c,'refund',rid,-amount,body.method,context,user)
    c.execute('UPDATE payments SET revision=revision+1 WHERE id=?',(pid,))
    if amount==remaining and order['status']!='completed':
        if order['status'] in ('preparing','ready'):consume_materials(c,order,waste=True)
        c.execute("UPDATE orders SET status='cancelled' WHERE id=?",(order['id'],))
        c.execute('INSERT INTO order_events(order_id,old_status,new_status,actor,created_at) VALUES(?,?,?,?,?)',(order['id'],order['status'],'cancelled',user['username'],stamp))
    return {'id':rid,'amount_minor':amount,'duplicate':False}


def shift_data(c,row):
    result=dict(row)
    result['opened_by_name']=c.execute('SELECT username FROM users WHERE id=?',(row['opened_by'],)).fetchone()[0]
    result['closed_by_name']=c.execute('SELECT username FROM users WHERE id=?',(row['closed_by'],)).fetchone()[0] if row['closed_by'] else None
    totals={r['method']:r['total'] for r in c.execute('SELECT method,SUM(amount_minor) AS total FROM cash_movements WHERE shift_id=? GROUP BY method',(row['id'],))}
    result['cash_net_minor']=totals.get('cash',0);result['pos_net_minor']=totals.get('pos',0)
    if row['closed_at'] is None:result['expected_minor']=row['opening_minor']+totals.get('cash',0)
    return result


def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, telegram_user_id TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, position INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', price INTEGER NOT NULL CHECK(price>=0), emoji TEXT DEFAULT '☕', category_id INTEGER NOT NULL REFERENCES categories(id), active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS addons (id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE, name TEXT NOT NULL, price INTEGER NOT NULL CHECK(price>=0));
        CREATE TABLE IF NOT EXISTS addon_recipes (addon_id INTEGER NOT NULL REFERENCES addons(id) ON DELETE CASCADE, ingredient_id INTEGER NOT NULL REFERENCES ingredients(id), delta REAL NOT NULL, PRIMARY KEY(addon_id,ingredient_id));
        CREATE TABLE IF NOT EXISTS cafe_tables (number INTEGER PRIMARY KEY, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS ingredients (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, unit TEXT NOT NULL, stock REAL NOT NULL DEFAULT 0, unit_cost REAL NOT NULL DEFAULT 0, reorder REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS recipes (product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE, ingredient_id INTEGER NOT NULL REFERENCES ingredients(id), quantity REAL NOT NULL CHECK(quantity>0), PRIMARY KEY(product_id,ingredient_id));
        CREATE TABLE IF NOT EXISTS customers (id INTEGER PRIMARY KEY, name TEXT DEFAULT '', phone TEXT NOT NULL UNIQUE, consent INTEGER NOT NULL DEFAULT 0, notes TEXT DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, idempotency_key TEXT NOT NULL UNIQUE, table_num INTEGER REFERENCES cafe_tables(number), fulfillment TEXT NOT NULL, customer_id INTEGER REFERENCES customers(id), customer_name TEXT DEFAULT '', customer_phone TEXT DEFAULT '', note TEXT DEFAULT '', allergy_note TEXT DEFAULT '', allergy_ack INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, payment_method TEXT NOT NULL, total INTEGER NOT NULL, cost_total REAL NOT NULL DEFAULT 0, stock_applied INTEGER NOT NULL DEFAULT 0, customer_token TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT);
        CREATE TABLE IF NOT EXISTS order_items (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE, product_id INTEGER REFERENCES products(id), name TEXT NOT NULL, qty INTEGER NOT NULL, base_price INTEGER NOT NULL, addon_name TEXT DEFAULT '', addon_id INTEGER, addon_price INTEGER NOT NULL DEFAULT 0, line_total INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS order_events (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), old_status TEXT, new_status TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id), amount INTEGER NOT NULL, method TEXT NOT NULL, reference TEXT DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS expenses (id INTEGER PRIMARY KEY, title TEXT NOT NULL, amount REAL NOT NULL CHECK(amount>=0), kind TEXT NOT NULL DEFAULT 'operating', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS inventory_moves (id INTEGER PRIMARY KEY, ingredient_id INTEGER NOT NULL REFERENCES ingredients(id), delta REAL NOT NULL, reason TEXT NOT NULL, order_id INTEGER REFERENCES orders(id), created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS attendance (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), action TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS telegram_links (code TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS bot_runtime (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS payment_corrections (id INTEGER PRIMARY KEY, payment_id INTEGER NOT NULL REFERENCES payments(id), old_method TEXT NOT NULL, new_method TEXT NOT NULL, old_reference TEXT NOT NULL, new_reference TEXT NOT NULL, reason TEXT NOT NULL, actor_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS payment_discrepancies (id INTEGER PRIMARY KEY, payment_id INTEGER NOT NULL REFERENCES payments(id), expected_amount INTEGER NOT NULL, reported_amount INTEGER NOT NULL, reason TEXT NOT NULL, actor_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')), resolution TEXT, resolved_by INTEGER REFERENCES users(id), resolved_at TEXT, idempotency_key TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS cashier_shifts (id INTEGER PRIMARY KEY, opening_minor INTEGER NOT NULL CHECK(opening_minor>=0), opened_by INTEGER NOT NULL REFERENCES users(id), opened_at TEXT NOT NULL, closed_by INTEGER REFERENCES users(id), closed_at TEXT, idempotency_key TEXT NOT NULL UNIQUE, counted_minor INTEGER, expected_minor INTEGER, difference_minor INTEGER, note TEXT NOT NULL DEFAULT '');
        CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_shift ON cashier_shifts((1)) WHERE closed_at IS NULL;
        CREATE TABLE IF NOT EXISTS financial_exceptions (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, target_id INTEGER NOT NULL, coordination TEXT NOT NULL CHECK(coordination IN ('in_person','ticket')), reference TEXT NOT NULL, reason TEXT NOT NULL, actor_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS refunds (id INTEGER PRIMARY KEY, payment_id INTEGER NOT NULL REFERENCES payments(id), amount_minor INTEGER NOT NULL CHECK(amount_minor>0), method TEXT NOT NULL CHECK(method IN ('cash','pos')), reason TEXT NOT NULL, reference TEXT NOT NULL DEFAULT '', actor_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, recognized_at TEXT, shift_id INTEGER REFERENCES cashier_shifts(id), exception_id INTEGER REFERENCES financial_exceptions(id), idempotency_key TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS cash_movements (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, source_id INTEGER NOT NULL, amount_minor INTEGER NOT NULL, method TEXT NOT NULL CHECK(method IN ('cash','pos')), shift_id INTEGER REFERENCES cashier_shifts(id), exception_id INTEGER REFERENCES financial_exceptions(id), actor_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, UNIQUE(kind,source_id), CHECK(shift_id IS NOT NULL OR exception_id IS NOT NULL));
        CREATE TABLE IF NOT EXISTS financial_operation_keys (kind TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL, actor_id INTEGER NOT NULL REFERENCES users(id), response TEXT NOT NULL, PRIMARY KEY(kind,key));
        CREATE TABLE IF NOT EXISTS financial_requests (id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('payment','refund','correction')), target_id INTEGER NOT NULL, payload TEXT NOT NULL, coordination TEXT NOT NULL CHECK(coordination IN ('in_person','ticket')), reference TEXT NOT NULL, reason TEXT NOT NULL, requester_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')), reviewer_id INTEGER REFERENCES users(id), reviewed_at TEXT, resolution TEXT, result TEXT);
        CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS order_materials (order_id INTEGER NOT NULL REFERENCES orders(id), ingredient_id INTEGER NOT NULL REFERENCES ingredients(id), quantity REAL NOT NULL CHECK(quantity>0), PRIMARY KEY(order_id,ingredient_id));
        CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items(order_id);
        CREATE INDEX IF NOT EXISTS idx_refunds_payment_id ON refunds(payment_id);
        CREATE INDEX IF NOT EXISTS idx_cash_movements_shift ON cash_movements(shift_id);
        CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
        ''')
        # Small, additive schema migration for demos created by earlier builds.
        columns={x['name'] for x in c.execute('PRAGMA table_info(order_items)')}
        if 'addon_id' not in columns:
            c.execute('ALTER TABLE order_items ADD COLUMN addon_id INTEGER')
        # Seed default add-on ingredient deltas only on a fresh demo database.
        # For plant milk, replace the recipe's regular milk rather than counting both.
        mapping={
            (1,'شات اضافه'):[(1,18)],
            (2,'شات اضافه'):[(1,18)],
            (2,'شیر گیاهی'):[(2,-220),(6,220)],
            (3,'شات اضافه'):[(1,18)],
            (5,'شیر گیاهی'):[(2,-240),(6,240)],
        }
        migrate(c)
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT COUNT(*) FROM users').fetchone()[0]:
            validate_existing_passwords(c)
            c.execute('COMMIT')
            return
        users = [('مدیر کافه','admin','admin'),('صندوق‌دار','cashier','cashier'),('باریستا','staff','barista')]
        passwords={username:os.getenv('CAFE_PASSWORD_'+username.upper(),'') for _,_,username in users}
        if any(len(pwd)<12 for pwd in passwords.values()):
            raise RuntimeError('Set CAFE_PASSWORD_ADMIN, CAFE_PASSWORD_CASHIER and CAFE_PASSWORD_BARISTA (12+ characters), or run python manage.py init for hidden local setup')
        for name, role, username in users:
            c.execute('INSERT INTO users(name,role,username,password_hash) VALUES(?,?,?,?)', (name, role, username, hpass(passwords[username])))
        if not DEMO_MODE:
            c.execute('COMMIT')
            return
        for i, category in enumerate(['قهوه گرم','قهوه سرد','چای و دمنوش','کیک و دسر'], 1):
            c.execute('INSERT INTO categories(id,name,position) VALUES(?,?,?)', (i, category, i))
        products = [
            ('اسپرسو','شات اسپرسوی تازه، عطر عمیق و کرمای لطیف',85000,'☕',1),
            ('لاته','اسپرسو با شیر مخملی و فوم نرم',135000,'🥛',1),
            ('کاپوچینو','ترکیب متعادل اسپرسو و فوم شیر',125000,'☕',1),
            ('آمریکانو','اسپرسو با آب داغ و طعمی ملایم',95000,'♨️',1),
            ('آیس لاته','اسپرسو، شیر خنک و یخ',145000,'🧊',2),
            ('آیس آمریکانو','قهوه پرانرژی و خنک',110000,'🥤',2),
            ('چای ایرانی','چای تازه‌دم به همراه نبات',70000,'🫖',3),
            ('دمنوش بهارنارنج','دمنوش آرامش‌بخش با رایحه دلنشین',90000,'🌿',3),
            ('چیزکیک','برشی از چیزکیک خامه‌ای روز',175000,'🍰',4),
            ('براونی','براونی شکلاتی با بافت نرم',155000,'🍫',4),
        ]
        for p in products:
            c.execute('INSERT INTO products(name,description,price,emoji,category_id) VALUES(?,?,?,?,?)',p)
        for p, opts in {1:[('شات اضافه',35000)],2:[('شات اضافه',35000),('شیر گیاهی',30000)],3:[('شات اضافه',35000)],5:[('شیر گیاهی',30000)]}.items():
            for name, price in opts:
                c.execute('INSERT INTO addons(product_id,name,price) VALUES(?,?,?)', (p,name,price))
        for n in range(1,13):
            c.execute('INSERT INTO cafe_tables(number) VALUES(?)',(n,))
        ingredients=[('دانه قهوه','گرم',8600,1900,2000),('شیر','میلی‌لیتر',25000,120,7000),('چای','گرم',1200,850,250),('کیک','برش',34,68000,10),('شکلات','گرم',900,1300,300),('شیر گیاهی','میلی‌لیتر',6000,190,2000)]
        for t in ingredients:
            c.execute('INSERT INTO ingredients(name,unit,stock,unit_cost,reorder) VALUES(?,?,?,?,?)', t)
        for (pid,addon_name),items in mapping.items():
            ar=c.execute('SELECT id FROM addons WHERE product_id=? AND name=?',(pid,addon_name)).fetchone()
            if ar:
                for iid,delta in items:
                    c.execute('INSERT INTO addon_recipes(addon_id,ingredient_id,delta) VALUES(?,?,?)',(ar['id'],iid,delta))
        recipe={1:[(1,18)],2:[(1,18),(2,220)],3:[(1,18),(2,160)],4:[(1,18)],5:[(1,18),(2,240)],6:[(1,18)],7:[(3,6)],8:[(3,7)],9:[(4,1)],10:[(5,55)]}
        for pid, items in recipe.items():
            for ing, amount in items:
                c.execute('INSERT INTO recipes(product_id,ingredient_id,quantity) VALUES(?,?,?)', (pid,ing,amount))
        for name,phone in [('مهسا','09120000001'),('کیوان','09120000002'),('ترانه','09120000003'),('سام','09120000004')]:
            c.execute('INSERT INTO customers(name,phone,consent,created_at) VALUES(?,?,1,?)',(name,phone,now()))
        # Demo order history is seeded as already accounted for; initial stock represents post-history inventory.
        for i in range(24):
            day=(datetime.now().astimezone()-timedelta(days=i%9,hours=(i*3)%10)).isoformat(timespec='seconds')
            pid=1+(i*7)%10
            product=c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
            qt=1+(i%3==0)
            cost=c.execute('SELECT COALESCE(SUM(r.quantity*g.unit_cost),0) FROM recipes r JOIN ingredients g ON g.id=r.ingredient_id WHERE r.product_id=?',(pid,)).fetchone()[0]*qt
            amount=product['price']*qt
            cust=1+(i%4)
            cr=c.execute('INSERT INTO orders(code,idempotency_key,table_num,fulfillment,customer_id,customer_name,status,payment_method,total,cost_total,stock_applied,customer_token,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (f'CN-H{i+1:03d}',f'seed-{i}',(i%12)+1,'table',cust,['مهسا','کیوان','ترانه','سام'][i%4],'completed','pos' if i%2 else 'cash',amount,cost,1,secrets.token_urlsafe(20),day,day))
            oid=cr.lastrowid
            c.execute('INSERT INTO order_items(order_id,product_id,name,qty,base_price,line_total) VALUES(?,?,?,?,?,?)',(oid,pid,product['name'],qt,product['price'],amount))
            c.execute('INSERT INTO payments(order_id,amount,method,created_at) VALUES(?,?,?,?)',(oid,amount,'pos' if i%2 else 'cash',day))
        for i, (pid,status,tab) in enumerate([(2,'new',3),(5,'accepted',7),(9,'preparing',2)]):
            p=c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
            oid=c.execute('INSERT INTO orders(code,idempotency_key,table_num,fulfillment,customer_name,note,allergy_note,status,payment_method,total,customer_token,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                 (f'CN-L{i+1:03d}',f'live-{i}',tab,'table',['نیلوفر','نیما','سارا'][i],'بدون شکر' if i==1 else '', 'حساسیت به شیر' if i==0 else '',status,'pos',p['price'],secrets.token_urlsafe(20),now())).lastrowid
            c.execute('INSERT INTO order_items(order_id,product_id,name,qty,base_price,line_total) VALUES(?,?,?,?,?,?)',(oid,pid,p['name'],1,p['price'],p['price']))
        c.execute("INSERT INTO expenses(title,amount,kind,created_at) VALUES('هزینه خدمات و نظافت',350000,'operating',?)",(now(),))
        _migrate_financial(c)
        for row in c.execute('SELECT id FROM orders WHERE stock_applied=0').fetchall():
            snapshot_recipe(c,row['id'])
        c.execute('COMMIT')


@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app = FastAPI(title='Cafe Yar 360 Management', version='1.4.1', lifespan=lifespan)
@app.exception_handler(RequestValidationError)
def invalid_input(request: Request, exc: RequestValidationError):
    # Return field locations without reflecting passwords or non-finite JSON values.
    return JSONResponse(status_code=422,content={'detail':[
        {'loc':list(e['loc']),'msg':e['msg'],'type':e['type']} for e in exc.errors()
    ]})


@app.exception_handler(sqlite3.IntegrityError)
def integrity_error(request: Request, exc: sqlite3.IntegrityError):
    return JSONResponse(status_code=409,content={'detail':'عملیات با اطلاعات مرتبط یا محدودیت دیتابیس ناسازگار است'})


@app.exception_handler(sqlite3.OperationalError)
def database_unavailable(request: Request, exc: sqlite3.OperationalError):
    return JSONResponse(status_code=503,content={'detail':'دیتابیس موقتاً در دسترس نیست؛ دوباره تلاش کنید'})


app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')


@app.get('/', response_class=HTMLResponse)
@app.get('/{page}', response_class=HTMLResponse)
def main_page(page: str = 'menu'):
    if page not in {'menu','staff','cashier','admin','inventory','finance','analytics','crm','attendance','presentation'}:
        api_error(404,'صفحه یافت نشد')
    return FileResponse(ROOT/'static'/'index.html')


def current_user(request: Request):
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        api_error(401, 'برای مشاهده این بخش وارد شوید')
    digest=hashlib.sha256(raw.encode()).hexdigest()
    with db() as c:
        user = c.execute('SELECT users.id,users.name,users.role,users.username FROM sessions JOIN users ON users.id=sessions.user_id WHERE sessions.token_hash=? AND expires>?',(digest,time.time())).fetchone()
    if not user:
        api_error(401,'نشست منقضی شده است')
    return asdict(user)


def allowed(*roles):
    def dependency(user=Depends(current_user)):
        if user['role'] not in roles:
            api_error(403,'دسترسی کافی نیست')
        return user
    return dependency


class InputModel(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)


class Login(InputModel):
    username: str
    password: str


@app.post('/api/auth/login')
def login(body: Login):
    with db() as c:
        u=c.execute('SELECT * FROM users WHERE username=?',(body.username.strip(),)).fetchone()
        if not u or not verify(body.password,u['password_hash']):
            api_error(401,'نام کاربری یا رمز عبور نادرست است')
        raw=secrets.token_urlsafe(32)
        c.execute('INSERT INTO sessions(token_hash,user_id,expires) VALUES(?,?,?)',(hashlib.sha256(raw.encode()).hexdigest(),u['id'],time.time()+12*3600))
    resp=Response(content=json.dumps({'id':u['id'],'name':u['name'],'role':u['role'],'username':u['username']},ensure_ascii=False),media_type='application/json')
    resp.set_cookie(COOKIE_NAME,raw,httponly=True,samesite='lax',secure=COOKIE_SECURE,max_age=12*3600,path='/')
    return resp


@app.get('/api/auth/me')
def me(user=Depends(current_user)):
    return user


@app.post('/api/auth/logout')
def logout(request: Request):
    raw=request.cookies.get(COOKIE_NAME)
    if raw:
        with db() as c:
            c.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(raw.encode()).hexdigest(),))
    resp=Response(content='{"ok":true}',media_type='application/json')
    resp.delete_cookie(COOKIE_NAME,path='/')
    return resp


@app.get('/api/public/config')
def config():
    with db() as c:
        tables=c.execute('SELECT COUNT(*) FROM cafe_tables WHERE active=1').fetchone()[0]
    return {'name':PRODUCT_NAME,'cafe_name':CAFE_NAME,'product_name_en':'Cafe Yar 360','currency':'تومان','online_enabled':False,'demo':DEMO_MODE,'tables':tables}


@app.get('/api/public/menu')
def public_menu():
    with db() as c:
        categories=[asdict(r) for r in c.execute('SELECT * FROM categories ORDER BY position,id')]
        products=[asdict(r) for r in c.execute('SELECT * FROM products WHERE active=1 ORDER BY id')]
        addons=[asdict(r) for r in c.execute('SELECT * FROM addons ORDER BY id')]
        tables=[r['number'] for r in c.execute('SELECT number FROM cafe_tables WHERE active=1 ORDER BY number')]
    for p in products:
        p['addons']=[a for a in addons if a['product_id']==p['id']]
    return {'categories':categories,'products':products,'tables':tables}


class CartItem(InputModel):
    product_id: int
    qty: int = Field(ge=1,le=30)
    addon_id: int | None = None


class OrderCreate(InputModel):
    items: list[CartItem] = Field(min_length=1,max_length=50)
    table: int | None = None
    fulfillment: str
    customer_name: str = Field(default='',max_length=80)
    phone: str = Field(default='',max_length=20)
    consent: bool = False
    note: str = Field(default='',max_length=500)
    allergy_note: str = Field(default='',max_length=300)
    payment_method: str
    idempotency_key: str = Field(min_length=8,max_length=100)


@app.post('/api/orders',status_code=201)
def create_order(body: OrderCreate):
    request_hash=hashlib.sha256(json.dumps(body.model_dump(exclude={'idempotency_key'}),sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    if body.fulfillment not in ('table','takeaway'):
        api_error(422,'روش دریافت نامعتبر است')
    if body.payment_method not in ('cash','pos'):
        api_error(422,'پرداخت آنلاین هنوز متصل نیست')
    if body.fulfillment=='table' and body.table is None:
        api_error(422,'شماره میز ضروری است')
    if body.fulfillment=='takeaway' and body.table is not None:
        api_error(422,'برای بیرون‌بر شماره میز ارسال نکنید')
    if body.phone and (not body.phone.isascii() or not body.phone.isdigit() or not 10<=len(body.phone)<=15):
        api_error(422,'شماره تماس باید ۱۰ تا ۱۵ رقم انگلیسی باشد')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT * FROM orders WHERE idempotency_key=?',(body.idempotency_key,)).fetchone()
        if old:
            if not old['request_hash'] or old['request_hash']!=request_hash:
                api_error(409,'کلید تکرار متعلق به درخواست دیگری است؛ سفارش قبلی را بررسی کنید')
            c.execute('COMMIT')
            return {'id':old['id'],'code':old['code'],'token':old['customer_token'],'total':old['total'],'status':old['status'],'duplicate':True}
        if body.table is not None and not c.execute('SELECT 1 FROM cafe_tables WHERE number=? AND active=1',(body.table,)).fetchone():
            api_error(422,'میز معتبر نیست')
        lines=[]; total=0
        for item in body.items:
            p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(item.product_id,)).fetchone()
            if not p:
                api_error(422,'یک محصول از منو حذف یا غیرفعال شده است')
            addon=None
            if item.addon_id is not None:
                addon=c.execute('SELECT * FROM addons WHERE id=? AND product_id=?',(item.addon_id,item.product_id)).fetchone()
                if addon is None:
                    api_error(422,'افزودنی با محصول مطابقت ندارد')
            price=p['price']+(addon['price'] if addon else 0)
            total+=price*item.qty
            lines.append((p['id'],p['name'],item.qty,p['price'],addon['name'] if addon else '',addon['id'] if addon else None,addon['price'] if addon else 0,price*item.qty))
        customer_id=None
        phone=body.phone.strip()
        if phone and body.consent:
            cust=c.execute('SELECT * FROM customers WHERE phone=?',(phone,)).fetchone()
            if cust:
                customer_id=cust['id']
                c.execute('UPDATE customers SET name=CASE WHEN ?<>\'\' THEN ? ELSE name END,consent=1 WHERE id=?',(body.customer_name.strip(),body.customer_name.strip(),customer_id))
            else:
                customer_id=c.execute('INSERT INTO customers(name,phone,consent,created_at) VALUES(?,?,1,?)',(body.customer_name.strip(),phone,now())).lastrowid
        code='CN-'+datetime.now().strftime('%m%d')+'-'+secrets.token_hex(3).upper()
        token=secrets.token_urlsafe(24)
        oid=c.execute('INSERT INTO orders(code,idempotency_key,table_num,fulfillment,customer_id,customer_name,customer_phone,note,allergy_note,status,payment_method,total,customer_token,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
          (code,body.idempotency_key,body.table,body.fulfillment,customer_id,body.customer_name.strip(),phone,body.note.strip(),body.allergy_note.strip(),'new',body.payment_method,total,token,now())).lastrowid
        c.execute('UPDATE orders SET request_hash=? WHERE id=?',(request_hash,oid))
        for p in lines:
            c.execute('INSERT INTO order_items(order_id,product_id,name,qty,base_price,addon_name,addon_id,addon_price,line_total) VALUES(?,?,?,?,?,?,?,?,?)',(oid,*p))
        snapshot_recipe(c,oid)
        c.execute('INSERT INTO order_events(order_id,old_status,new_status,actor,created_at) VALUES(?,?,?,?,?)',(oid,None,'new','customer',now()))
        c.execute('COMMIT')
    return {'id':oid,'code':code,'token':token,'total':total,'status':'new','duplicate':False}


@app.get('/api/orders/{code}/status')
def order_status(code: str,token: str):
    with db() as c:
        o=c.execute('SELECT o.id,o.code,o.status,o.total,o.table_num,o.fulfillment,o.payment_method,o.created_at,p.id AS payment_id FROM orders o LEFT JOIN payments p ON p.order_id=o.id WHERE o.code=? AND o.customer_token=?',(code,token)).fetchone()
        if not o:
            api_error(404,'سفارش یافت نشد')
        return asdict(o)


def order_full(c,o):
    r=asdict(o)
    for internal in ('request_hash','customer_token','recipe_snapshot'):
        r.pop(internal,None)
    r['items']=[asdict(x) for x in c.execute('SELECT * FROM order_items WHERE order_id=?',(o['id'],))]
    pay=c.execute('SELECT * FROM payments WHERE order_id=?',(o['id'],)).fetchone()
    r['payment']=asdict(pay)
    refunded=c.execute('SELECT COALESCE(SUM(amount_minor),0) FROM refunds WHERE payment_id=?',(pay['id'] if pay else -1,)).fetchone()[0]
    r['refunded_minor']=refunded
    r['payment_status']='unpaid' if not pay else ('paid' if not refunded else ('refunded' if refunded==money_minor(pay['amount']) else 'partially_refunded'))
    return r


@app.get('/api/orders')
def get_orders(status: str='all', queue: str='history', limit: int=Query(100,ge=1,le=500), offset: int=Query(0,ge=0), user=Depends(allowed('staff','cashier','admin'))):
    if queue not in ('history','active','unpaid'):
        api_error(422,'صف نامعتبر است')
    with db() as c:
        if queue=='active':
            rows=c.execute("SELECT * FROM orders WHERE status NOT IN ('completed','cancelled') ORDER BY created_at,id").fetchall()
        elif queue=='unpaid':
            rows=c.execute("SELECT o.* FROM orders o LEFT JOIN payments p ON p.order_id=o.id WHERE p.id IS NULL AND o.status<>'cancelled' ORDER BY o.created_at,o.id").fetchall()
        elif status=='all':
            rows=c.execute('SELECT * FROM orders ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',(limit,offset)).fetchall()
        else:
            if status not in STATUS_FLOW:
                api_error(422,'وضعیت نامعتبر است')
            rows=c.execute('SELECT * FROM orders WHERE status=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',(status,limit,offset)).fetchall()
        return [order_full(c,o) for o in rows]


class StatusUpdate(InputModel):
    status: str
    allergy_ack: bool=False


@app.patch('/api/orders/{oid}/status')
def update_status(oid: int,body: StatusUpdate,user=Depends(allowed('admin','staff'))):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        o=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
        if not o:
            api_error(404,'سفارش یافت نشد')
        if body.status not in STATUS_FLOW[o['status']]:
            api_error(409,'تغییر وضعیت مجاز نیست')
        if body.status=='accepted' and o['allergy_note'] and not body.allergy_ack:
            api_error(409,'تأیید مشاهده حساسیت غذایی ضروری است')
        if body.status=='cancelled' and c.execute('SELECT 1 FROM payments WHERE order_id=?',(oid,)).fetchone():
            api_error(409,'سفارش تسویه‌شده بدون فرآیند استرداد قابل لغو نیست')
        if body.status=='completed' and not o['stock_applied']:
            consume_materials(c,o)
        c.execute('UPDATE orders SET status=?,allergy_ack=CASE WHEN ? THEN 1 ELSE allergy_ack END WHERE id=?',(body.status, int(body.allergy_ack),oid))
        c.execute('INSERT INTO order_events(order_id,old_status,new_status,actor,created_at) VALUES(?,?,?,?,?)',(oid,o['status'],body.status,user['username'],now()))
        c.execute('COMMIT')
    return {'ok':True,'status':body.status}


class ExceptionAuthorization(InputModel):
    coordination: Literal['in_person','ticket']
    reference: str=Field(min_length=3,max_length=120)
    reason: str=Field(min_length=3,max_length=500)

    @field_validator('reference','reason')
    @classmethod
    def not_blank(cls,value):
        if len(value.strip())<3:raise ValueError('توضیح کافی ضروری است')
        return value.strip()


class PayInput(InputModel):
    method: str
    reference: str=Field(default='',max_length=80)
    authorization: ExceptionAuthorization | None=None


class PaymentCorrection(InputModel):
    method: str
    reference: str=Field(default='',max_length=80)
    reason: str=Field(min_length=3,max_length=500)
    expected_revision: int=Field(ge=0)
    authorization: ExceptionAuthorization | None=None


class RefundInput(InputModel):
    amount: Decimal=Field(gt=0,le=9000000000000,max_digits=15,decimal_places=2)
    method: Literal['cash','pos']
    reason: str=Field(min_length=3,max_length=500)
    reference: str=Field(default='',max_length=80)
    expected_revision: int=Field(ge=0)
    idempotency_key: str=Field(min_length=8,max_length=100)
    authorization: ExceptionAuthorization | None=None

    @field_validator('reason')
    @classmethod
    def not_blank(cls,value):
        if len(value.strip())<3:raise ValueError('دلیل استرداد ضروری است')
        return value.strip()


@app.post('/api/orders/{oid}/pay')
def pay_order(oid:int,body:PayInput,user=Depends(allowed('admin','cashier'))):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        result=apply_payment(c,oid,body,user)
        c.execute('COMMIT')
    return result


@app.patch('/api/payments/{pid}/correction')
def correct_payment(pid:int,body:PaymentCorrection,user=Depends(allowed('admin'))):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        result=apply_correction(c,pid,body,user)
        c.execute('COMMIT')
    return result


@app.post('/api/payments/{pid}/refunds',status_code=201)
def refund_payment(pid:int,body:RefundInput,user=Depends(allowed('admin'))):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        result=apply_refund(c,pid,body,user)
        c.execute('COMMIT')
    return result


class ShiftOpen(InputModel):
    opening_amount: Decimal=Field(ge=0,le=9000000000000,max_digits=15,decimal_places=2)
    idempotency_key: str=Field(min_length=8,max_length=100)


class ShiftClose(InputModel):
    counted_amount: Decimal=Field(ge=0,le=9000000000000,max_digits=15,decimal_places=2)
    note: str=Field(default='',max_length=500)


@app.post('/api/cashier/shifts',status_code=201)
def open_shift(body:ShiftOpen,user=Depends(allowed('admin','cashier'))):
    opening=money_minor(body.opening_amount,strict=True)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT * FROM cashier_shifts WHERE idempotency_key=?',(body.idempotency_key,)).fetchone()
        if old:
            if old['opening_minor']!=opening or old['opened_by']!=user['id']:api_error(409,'کلید تکرار شیفت متفاوت است')
            result=shift_data(c,old)
        else:
            if c.execute('SELECT 1 FROM cashier_shifts WHERE closed_at IS NULL').fetchone():api_error(409,'یک شیفت صندوق از قبل باز است')
            sid=c.execute('INSERT INTO cashier_shifts(opening_minor,opened_by,opened_at,idempotency_key) VALUES(?,?,?,?)',(opening,user['id'],now(),body.idempotency_key)).lastrowid
            result=shift_data(c,c.execute('SELECT * FROM cashier_shifts WHERE id=?',(sid,)).fetchone())
        c.execute('COMMIT')
    return result


@app.get('/api/cashier/shifts')
def shifts(limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0),user=Depends(allowed('admin','cashier'))):
    with db() as c:
        c.execute('BEGIN')
        active=c.execute('SELECT * FROM cashier_shifts WHERE closed_at IS NULL').fetchone()
        history=[shift_data(c,r) for r in c.execute('SELECT * FROM cashier_shifts ORDER BY id DESC LIMIT ? OFFSET ?',(limit,offset)).fetchall()]
        return {'active':shift_data(c,active) if active else None,'history':history}


@app.post('/api/cashier/shifts/{sid}/close')
def close_shift(sid:int,body:ShiftClose,user=Depends(allowed('admin','cashier'))):
    counted=money_minor(body.counted_amount,strict=True)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM cashier_shifts WHERE id=?',(sid,)).fetchone()
        if not row:api_error(404,'شیفت پیدا نشد')
        if user['role']!='admin' and row['opened_by']!=user['id']:api_error(403,'بستن شیفت فقط توسط مسئول شیفت یا مدیر مجاز است')
        if row['closed_at']:
            if row['counted_minor']!=counted or row['note']!=body.note.strip():api_error(409,'شیفت بسته شده و قابل بازنویسی نیست')
        else:
            expected=shift_data(c,row)['expected_minor']
            if counted!=expected and len(body.note.strip())<3:api_error(422,'برای اختلاف شمارش صندوق دلیل ثبت کنید')
            c.execute('UPDATE cashier_shifts SET closed_at=?,closed_by=?,counted_minor=?,expected_minor=?,difference_minor=?,note=? WHERE id=?',
                (now(),user['id'],counted,expected,counted-expected,body.note.strip(),sid))
        result=shift_data(c,c.execute('SELECT * FROM cashier_shifts WHERE id=?',(sid,)).fetchone())
        c.execute('COMMIT')
    return result


class FinancialRequestInput(InputModel):
    kind: Literal['payment','refund','correction']
    target_id: int=Field(gt=0)
    payload: dict
    coordination: Literal['in_person','ticket']
    reference: str=Field(min_length=3,max_length=120)
    reason: str=Field(min_length=3,max_length=500)
    idempotency_key: str=Field(min_length=8,max_length=100)


class FinancialReview(InputModel):
    decision: Literal['approved','rejected']
    resolution: str=Field(min_length=3,max_length=500)


def request_body(kind,payload):
    try:
        body={'payment':PayInput,'refund':RefundInput,'correction':PaymentCorrection}[kind].model_validate(payload)
        body.authorization=None
        if kind in ('payment','correction') and body.method not in ('cash','pos'):api_error(422,'روش پرداخت معتبر نیست')
        return body
    except ValidationError:api_error(422,'اطلاعات درخواست مالی معتبر نیست')


@app.post('/api/cashier/requests',status_code=201)
def create_financial_request(body:FinancialRequestInput,user=Depends(allowed('admin','cashier'))):
    if len(body.reference.strip())<3 or len(body.reason.strip())<3:api_error(422,'مستند هماهنگی و دلیل ضروری است')
    coordination=ExceptionAuthorization(coordination=body.coordination,reference=body.reference,reason=body.reason)
    payload=request_body(body.kind,body.payload).model_dump_json(exclude={'authorization'})
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT * FROM financial_requests WHERE idempotency_key=?',(body.idempotency_key,)).fetchone()
        if old:
            if (old['kind'],old['target_id'],old['payload'],old['requester_id'],old['coordination'],old['reference'],old['reason'])!=(body.kind,body.target_id,payload,user['id'],body.coordination,coordination.reference,coordination.reason):api_error(409,'کلید تکرار درخواست متفاوت است')
            rid=old['id']
        else:
            table='orders' if body.kind=='payment' else 'payments'
            if not c.execute('SELECT 1 FROM '+table+' WHERE id=?',(body.target_id,)).fetchone():api_error(404,'سفارش یا پرداخت پیدا نشد')
            rid=c.execute('INSERT INTO financial_requests(kind,target_id,payload,coordination,reference,reason,requester_id,created_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?)',
                (body.kind,body.target_id,payload,body.coordination,coordination.reference,coordination.reason,user['id'],now(),body.idempotency_key)).lastrowid
        result=dict(c.execute('SELECT * FROM financial_requests WHERE id=?',(rid,)).fetchone())
        c.execute('COMMIT')
    return result


@app.get('/api/cashier/requests')
def financial_requests(limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0),user=Depends(allowed('admin','cashier'))):
    with db() as c:
        c.execute('BEGIN')
        uid=None if user['role']=='admin' else user['id']
        rows=[dict(r) for r in c.execute('SELECT r.*,u.username AS requester,v.username AS reviewer FROM financial_requests r JOIN users u ON u.id=r.requester_id LEFT JOIN users v ON v.id=r.reviewer_id WHERE (? IS NULL OR requester_id=?) ORDER BY r.id DESC LIMIT ? OFFSET ?',(uid,uid,limit,offset))]
        for row in rows:
            payload=json.loads(row['payload'])
            if row['kind']=='payment':order=c.execute('SELECT id,code,total FROM orders WHERE id=?',(row['target_id'],)).fetchone()
            else:order=c.execute('SELECT o.id,o.code,o.total FROM orders o JOIN payments p ON p.order_id=o.id WHERE p.id=?',(row['target_id'],)).fetchone()
            row['order_code']=order['code'];row['order_id']=order['id']
            row['requested_amount_minor']=money_minor(payload['amount']) if row['kind']=='refund' else money_minor(order['total'])
            row['requested_method']=payload['method']
            row['requested_reference']=payload.get('reference','')
            row['operation_reason']=payload.get('reason','')
        return rows


@app.patch('/api/cashier/requests/{rid}/review')
def review_financial_request(rid:int,body:FinancialReview,user=Depends(allowed('admin'))):
    if len(body.resolution.strip())<3:api_error(422,'نتیجه بررسی را بنویسید')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        request=c.execute('SELECT * FROM financial_requests WHERE id=?',(rid,)).fetchone()
        if not request:api_error(404,'درخواست پیدا نشد')
        if request['status']!='pending':
            if request['status']!=body.decision or request['resolution']!=body.resolution.strip():api_error(409,'این درخواست قبلاً بررسی شده است')
            result=json.loads(request['result']) if request['result'] else None
        else:
            result=None
            if body.decision=='approved':
                action=request_body(request['kind'],json.loads(request['payload']))
                action.authorization=ExceptionAuthorization(coordination=request['coordination'],reference=request['reference'],reason=request['reason'])
                if request['kind']=='payment':result=apply_payment(c,request['target_id'],action,user)
                elif request['kind']=='refund':result=apply_refund(c,request['target_id'],action,user)
                else:result=apply_correction(c,request['target_id'],action,user)
            c.execute('UPDATE financial_requests SET status=?,reviewer_id=?,reviewed_at=?,resolution=?,result=? WHERE id=?',
                (body.decision,user['id'],now(),body.resolution.strip(),json.dumps(result),rid))
        c.execute('COMMIT')
    return {'id':rid,'status':body.decision,'result':result}


@app.get('/api/cashier/exceptions')
def financial_exceptions(limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0),user=Depends(allowed('admin'))):
    with db() as c:
        return [asdict(r) for r in c.execute('SELECT e.*,u.username AS actor FROM financial_exceptions e JOIN users u ON u.id=e.actor_id ORDER BY e.id DESC LIMIT ? OFFSET ?',(limit,offset))]


@app.get('/api/cashier/payments')
def cashier_payments(payment_status:Literal['all','paid','partially_refunded','refunded']='all',limit:int=Query(20,ge=1,le=100),offset:int=Query(0,ge=0),user=Depends(allowed('admin','cashier'))):
    with db() as c:
        c.execute('BEGIN')
        condition=""
        if payment_status=='paid':condition=' AND COALESCE(r.returned,0)=0'
        elif payment_status=='partially_refunded':condition=' AND COALESCE(r.returned,0)>0 AND r.returned<p.amount*100'
        elif payment_status=='refunded':condition=' AND r.returned=p.amount*100'
        join=' FROM orders o JOIN payments p ON p.order_id=o.id LEFT JOIN (SELECT payment_id,SUM(amount_minor) AS returned FROM refunds GROUP BY payment_id) r ON r.payment_id=p.id WHERE 1=1'+condition
        total=c.execute('SELECT COUNT(*)'+join).fetchone()[0]
        rows=c.execute('SELECT o.*'+join+' ORDER BY p.created_at DESC,p.id DESC LIMIT ? OFFSET ?',(limit,offset)).fetchall()
        return {'items':[order_full(c,row) for row in rows],'total':total,'limit':limit,'offset':offset}


@app.get('/api/payments/{pid}/refunds')
def payment_refunds(pid:int,user=Depends(allowed('admin','cashier'))):
    with db() as c:
        if not c.execute('SELECT 1 FROM payments WHERE id=?',(pid,)).fetchone():api_error(404,'پرداخت پیدا نشد')
        return [asdict(r) for r in c.execute('SELECT r.*,u.username AS actor FROM refunds r JOIN users u ON u.id=r.actor_id WHERE r.payment_id=? ORDER BY r.id',(pid,))]


class DiscrepancyInput(InputModel):
    reported_amount: int=Field(ge=0,le=1000000000000)
    reason: str=Field(min_length=3,max_length=500)
    idempotency_key: str=Field(min_length=8,max_length=100)


@app.post('/api/payments/{pid}/discrepancies',status_code=201)
def record_discrepancy(pid:int,body:DiscrepancyInput,user=Depends(allowed('admin','cashier'))):
    if len(body.reason.strip())<3:api_error(422,'شرح مغایرت ضروری است')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT * FROM payment_discrepancies WHERE idempotency_key=?',(body.idempotency_key,)).fetchone()
        if old:
            if old['payment_id']!=pid or old['reported_amount']!=body.reported_amount or old['reason']!=body.reason.strip() or old['actor_id']!=user['id']:
                api_error(409,'کلید تکرار برای گزارش دیگری استفاده شده است')
            c.execute('COMMIT')
            return {'id':old['id'],'duplicate':True}
        pay=c.execute('SELECT amount FROM payments WHERE id=?',(pid,)).fetchone()
        if not pay:api_error(404,'پرداخت پیدا نشد')
        if body.reported_amount==pay['amount']:api_error(422,'مبلغ مشاهده‌شده با مبلغ ثبت‌شده یکسان است')
        did=c.execute('INSERT INTO payment_discrepancies(payment_id,expected_amount,reported_amount,reason,actor_id,created_at,idempotency_key) VALUES(?,?,?,?,?,?,?)',
            (pid,pay['amount'],body.reported_amount,body.reason.strip(),user['id'],now(),body.idempotency_key)).lastrowid
        c.execute('COMMIT')
    return {'id':did,'duplicate':False}


class DiscrepancyResolution(InputModel):
    resolution: str=Field(min_length=3,max_length=500)


@app.patch('/api/cashier/discrepancies/{did}/resolve')
def resolve_discrepancy(did:int,body:DiscrepancyResolution,user=Depends(allowed('admin'))):
    if len(body.resolution.strip())<3:api_error(422,'شرح نتیجه بررسی ضروری است')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT status FROM payment_discrepancies WHERE id=?',(did,)).fetchone()
        if not row:api_error(404,'گزارش پیدا نشد')
        if row['status']!='open':api_error(409,'گزارش قبلاً بسته شده است')
        c.execute("UPDATE payment_discrepancies SET status='resolved',resolution=?,resolved_by=?,resolved_at=? WHERE id=?",(body.resolution.strip(),user['id'],now(),did))
        c.execute('COMMIT')
    return {'ok':True}


@app.get('/api/cashier/discrepancies')
def discrepancies(limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0),user=Depends(allowed('admin','cashier'))):
    with db() as c:
        return [asdict(r) for r in c.execute('''SELECT d.id,d.payment_id,d.expected_amount,d.reported_amount,d.reason,d.status,d.resolution,d.created_at,d.resolved_at,
            o.code,u.username AS actor,ru.username AS resolved_by FROM payment_discrepancies d JOIN payments p ON p.id=d.payment_id
            JOIN orders o ON o.id=p.order_id JOIN users u ON u.id=d.actor_id LEFT JOIN users ru ON ru.id=d.resolved_by ORDER BY d.id DESC LIMIT ? OFFSET ?''',(limit,offset))]


@app.get('/api/cashier/summary')
def cashier_summary(user=Depends(allowed('admin','cashier'))):
    today=local_datetime(now()).date()
    with db() as c:
        c.execute('BEGIN')
        r=financial_summary(c,today,today)
        open_count=c.execute("SELECT COUNT(*) FROM payment_discrepancies WHERE status='open'").fetchone()[0]
        return {'day':today.isoformat(),'received_today':r['cash_received'],'refunded_today':r['cash_refunded'],'net_received_today':r['net_cash_received'],'payments_today':r['paid_orders'],'open_discrepancies':open_count}


def receipt_data(c,oid):
    if not c.in_transaction:c.execute('BEGIN')
    order=c.execute('SELECT id,code,table_num,fulfillment,total,created_at,status FROM orders WHERE id=?',(oid,)).fetchone()
    if not order:api_error(404,'سفارش پیدا نشد')
    pay=c.execute('SELECT p.*,u.username AS actor FROM payments p LEFT JOIN users u ON u.id=p.actor_id WHERE order_id=?',(oid,)).fetchone()
    if not pay:api_error(409,'رسید تسویه پس از ثبت پرداخت صادر می‌شود')
    items=[asdict(r) for r in c.execute('SELECT name,qty,base_price,addon_name,addon_price,line_total FROM order_items WHERE order_id=? ORDER BY id',(oid,))]
    changes=[asdict(r) for r in c.execute('SELECT a.*,u.username AS actor FROM payment_corrections a JOIN users u ON u.id=a.actor_id WHERE payment_id=? ORDER BY a.id',(pay['id'],))]
    refunds=[dict(r) for r in c.execute('SELECT r.*,u.username AS actor FROM refunds r JOIN users u ON u.id=r.actor_id WHERE payment_id=? ORDER BY r.id',(pay['id'],))]
    returned=sum(r['amount_minor'] for r in refunds)
    return {'cafe_name':CAFE_NAME,'refunds':refunds,'refunded_minor':returned,'remaining_refundable_minor':money_minor(pay['amount'])-returned,'product_name':PRODUCT_NAME,'receipt_number':f"CY360-{pay['id']:08d}",'currency':'تومان','order':asdict(order),'payment':asdict(pay),'items':items,'corrections':changes}


@app.get('/api/orders/{oid}/receipt')
def receipt_json(oid:int,response:Response,user=Depends(allowed('admin','cashier'))):
    response.headers['Cache-Control']='no-store'
    with db() as c:return receipt_data(c,oid)


@app.get('/receipts/{oid}',response_class=HTMLResponse)
def receipt_html(oid:int,refund_id:int|None=None,user=Depends(allowed('admin','cashier'))):
    with db() as c:data=receipt_data(c,oid)
    pay=data['payment'];o=data['order']
    receipt_title='رسید استرداد' if refund_id is not None else 'رسید تسویه'
    receipt_number=f'CY360-R{refund_id:08d}' if refund_id is not None else data['receipt_number']
    chosen=[r for r in data['refunds'] if refund_id is None or r['id']==refund_id]
    if refund_id is not None and not chosen:api_error(404,'رسید استرداد پیدا نشد')
    def e(value):return escape(str(value))
    def stamp(value):return jdatetime.datetime.fromgregorian(datetime=local_datetime(value)).strftime('%Y/%m/%d %H:%M:%S')
    rows=''.join(f"<tr><td>{e(i['name'])} {e(i['addon_name'])}</td><td>{i['qty']}</td><td>{i['base_price']+i['addon_price']:,}</td><td>{i['line_total']:,}</td></tr>" for i in data['items'])
    labels={'cash':'نقدی','pos':'کارتخوان'}
    changes=''.join(f"<li><bdi dir='ltr'>{e(stamp(x['created_at']))}</bdi> — {e(x['actor'])}: روش {e(labels[x['old_method']])} به {e(labels[x['new_method']])}؛ {e(x['reason'])}</li>" for x in data['corrections'])
    returned=''.join(f"<section><h3>رسید استرداد CY360-R{r['id']:08d}</h3><p>مبلغ: {r['amount_minor']/100:,.2f} تومان | روش: {e(labels[r['method']])}</p><p>دلیل: {e(r['reason'])} | ثبت‌کننده: {e(r['actor'])}</p><p><bdi dir='ltr'>{e(stamp(r['created_at']))}</bdi> | پیگیری: {e(r['reference'])}</p></section>" for r in chosen)
    page=f'''<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(receipt_title)} {e(receipt_number)}</title><link rel="stylesheet" href="/static/styles.css"><link rel="stylesheet" href="/static/receipt.css"><script src="/static/receipt.js" defer></script></head><body><main class="receipt panel"><h1>{e(CAFE_NAME)}</h1><p>{e(PRODUCT_NAME)} · Cafe Yar 360</p><h2>{e(receipt_title)} {e(receipt_number)}</h2><p>رسید پرداخت مرجع: {e(data['receipt_number'])}</p><p>سفارش <bdi dir="ltr">{e(o['code'])}</bdi> | میز: {e(o['table_num'] or 'بیرون‌بر')}</p><p>زمان پرداخت: <bdi dir="ltr">{e(stamp(pay['created_at']))}</bdi> تهران | صندوق‌دار: {e(pay['actor'] or 'ثبت قدیمی')}</p><div class="table-wrap"><table class="data-table"><thead><tr><th>شرح</th><th>تعداد</th><th>واحد</th><th>مبلغ</th></tr></thead><tbody>{rows}</tbody></table></div><h2>جمع سفارش مرجع: {o['total']:,} تومان</h2><p>روش: {'کارتخوان (ثبت دستی)' if pay['method']=='pos' else 'نقدی'} | شماره پیگیری: <bdi dir="ltr">{e(pay['reference'] or '—')}</bdi></p><p>نسخه رسید: {pay['revision']} | پرداخت ثبت شده: {pay['amount']:,} تومان</p><ul>{changes}</ul>{returned}<p>خالص وجه دریافت‌شده: {(money_minor(pay['amount'])-data['refunded_minor'])/100:,.2f} تومان</p><p>رسید داخلی؛ فاکتور رسمی مالیاتی نیست.</p><button id="print-receipt" class="btn btn-primary">چاپ رسید</button></main></body></html>'''
    return HTMLResponse(page,headers={'Cache-Control':'no-store','Content-Security-Policy':"default-src 'none'; style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'"})


@app.get('/api/qr/{num}')
def table_qr(num:int,request:Request):
    with db() as c:
        if not c.execute('SELECT 1 FROM cafe_tables WHERE number=? AND active=1',(num,)).fetchone():
            api_error(404,'شماره میز یافت نشد')
    base=PUBLIC_BASE_URL or str(request.base_url).rstrip('/')
    url=f'{base}/menu?table={num}'
    im=qrcode.make(url)
    buf=io.BytesIO();im.save(buf,format='PNG')
    return Response(content=buf.getvalue(),media_type='image/png',headers={'Cache-Control':'no-store'})


@app.get('/api/admin/catalog')
def catalog(user=Depends(allowed('admin'))):
    with db() as c:
        return {'categories':[asdict(x) for x in c.execute('SELECT * FROM categories ORDER BY position')],
                'products':[dict(asdict(x),addons=[asdict(z) for z in c.execute('SELECT * FROM addons WHERE product_id=?',(x['id'],))]) for x in c.execute('SELECT * FROM products ORDER BY id DESC')],
                'tables':[asdict(x) for x in c.execute('SELECT * FROM cafe_tables ORDER BY number')],
                'addon_recipes':[asdict(x) for x in c.execute('SELECT * FROM addon_recipes')]} 


class ProductInput(InputModel):
    name: str=Field(min_length=2,max_length=80)
    description: str=Field(default='',max_length=300)
    price: int=Field(ge=0,le=100000000)
    emoji: str=Field(default='☕',max_length=12)
    category_id: int
    active: bool=True


@app.post('/api/admin/products')
def product_create(body: ProductInput,user=Depends(allowed('admin'))):
    with db() as c:
        try:
            oid=c.execute('INSERT INTO products(name,description,price,emoji,category_id,active) VALUES(?,?,?,?,?,?)', (body.name,body.description,body.price,body.emoji,body.category_id,int(body.active))).lastrowid
        except sqlite3.IntegrityError:
            api_error(422,'دسته‌بندی معتبر نیست')
        return {'id':oid}


@app.put('/api/admin/products/{pid}')
def product_update(pid:int,body:ProductInput,user=Depends(allowed('admin'))):
    with db() as c:
        result=c.execute('UPDATE products SET name=?,description=?,price=?,emoji=?,category_id=?,active=? WHERE id=?', (body.name,body.description,body.price,body.emoji,body.category_id,int(body.active),pid))
        if not result.rowcount:api_error(404,'محصول پیدا نشد')
    return {'ok':True}


class AddonInput(InputModel):
    name: str=Field(min_length=2,max_length=80)
    price: int=Field(ge=0,le=10000000)


@app.post('/api/admin/products/{pid}/addons')
def addon_create(pid:int,body:AddonInput,user=Depends(allowed('admin'))):
    with db() as c:
        if not c.execute('SELECT 1 FROM products WHERE id=?',(pid,)).fetchone():api_error(404,'محصول پیدا نشد')
        aid=c.execute('INSERT INTO addons(product_id,name,price) VALUES(?,?,?)',(pid,body.name,body.price)).lastrowid
    return {'id':aid}


@app.get('/api/admin/addons/{aid}/recipe')
def get_addon_recipe(aid:int,user=Depends(allowed('admin'))):
    with db() as c:
        if not c.execute('SELECT 1 FROM addons WHERE id=?',(aid,)).fetchone():api_error(404,'افزودنی پیدا نشد')
        return [asdict(x) for x in c.execute('SELECT * FROM addon_recipes WHERE addon_id=?',(aid,))]


@app.put('/api/admin/addons/{aid}/recipe')
def set_addon_recipe(aid:int,body:dict,user=Depends(allowed('admin'))):
    parts=body.get('parts',[])
    if not isinstance(parts,list) or len(parts)>40:api_error(422,'لیست مواد نامعتبر است')
    used=set()
    with db() as c:
        if not c.execute('SELECT 1 FROM addons WHERE id=?',(aid,)).fetchone():api_error(404,'افزودنی پیدا نشد')
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM addon_recipes WHERE addon_id=?',(aid,))
        for part in parts:
            if not isinstance(part,dict):api_error(422,'فرمت مواد نامعتبر است')
            try: iid=int(part['ingredient_id']);delta=float(part['delta'])
            except (KeyError,TypeError,ValueError):api_error(422,'مقدار ماده نامعتبر است')
            if not math.isfinite(delta) or iid in used or iid<=0 or abs(delta)>100000 or not c.execute('SELECT 1 FROM ingredients WHERE id=?',(iid,)).fetchone():api_error(422,'ماده نامعتبر یا تکراری است')
            quantity_decimal(delta)
            used.add(iid)
            if delta!=0:c.execute('INSERT INTO addon_recipes(addon_id,ingredient_id,delta) VALUES(?,?,?)',(aid,iid,delta))
        c.execute('COMMIT')
    return {'ok':True}


@app.delete('/api/admin/addons/{aid}')
def addon_delete(aid:int,user=Depends(allowed('admin'))):
    with db() as c:
        c.execute('DELETE FROM addons WHERE id=?',(aid,))
    return {'ok':True}


class CategoryInput(InputModel):
    name: str=Field(min_length=2,max_length=60)


@app.post('/api/admin/categories')
def category_create(body:CategoryInput,user=Depends(allowed('admin'))):
    with db() as c:
        try:
            oid=c.execute('INSERT INTO categories(name,position) VALUES(?,(SELECT COALESCE(MAX(position),0)+1 FROM categories))',(body.name,)).lastrowid
        except sqlite3.IntegrityError:api_error(409,'نام دسته‌بندی تکراری است')
        return {'id':oid}


@app.get('/api/inventory')
def inventory(user=Depends(allowed('admin'))):
    with db() as c:
        return {'ingredients':[asdict(x) for x in c.execute('SELECT * FROM ingredients ORDER BY id')],
                'recipes':[asdict(x) for x in c.execute('SELECT * FROM recipes ORDER BY product_id,ingredient_id')],
                'moves':[asdict(x) for x in c.execute('SELECT m.*,i.name FROM inventory_moves m JOIN ingredients i ON i.id=m.ingredient_id ORDER BY m.id DESC LIMIT 30')]}


class IngredientInput(InputModel):
    name: str=Field(min_length=2,max_length=80)
    unit: str=Field(min_length=1,max_length=30)
    stock: float=Field(ge=0)
    unit_cost: Decimal=Field(ge=0,le=9000000000000,max_digits=15,decimal_places=2)
    reorder: float=Field(ge=0)


@app.post('/api/inventory/ingredients')
def add_ingredient(body:IngredientInput,user=Depends(allowed('admin'))):
    unit=money_minor(body.unit_cost,strict=True)
    for value in (body.stock,body.reorder):quantity_decimal(value)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            oid=c.execute('INSERT INTO ingredients(name,unit,stock,unit_cost,reorder) VALUES(?,?,?,?,?)',(body.name,body.unit,body.stock,float(body.unit_cost),body.reorder)).lastrowid
        except sqlite3.IntegrityError:api_error(409,'مواد اولیه تکراری است')
        c.execute('UPDATE ingredients SET unit_cost_minor=? WHERE id=?',(unit,oid))
        c.execute('COMMIT')
        return {'id':oid}


@app.put('/api/inventory/ingredients/{iid}')
def edit_ingredient(iid:int,body:IngredientInput,user=Depends(allowed('admin'))):
    unit=money_minor(body.unit_cost,strict=True)
    for value in (body.stock,body.reorder):quantity_decimal(value)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT stock FROM ingredients WHERE id=?',(iid,)).fetchone()
        if not old:api_error(404,'ماده اولیه پیدا نشد')
        c.execute('UPDATE ingredients SET name=?,unit=?,stock=?,unit_cost=?,reorder=? WHERE id=?',(body.name,body.unit,body.stock,float(body.unit_cost),body.reorder,iid))
        c.execute('UPDATE ingredients SET unit_cost_minor=? WHERE id=?',(unit,iid))
        if body.stock!=old['stock']:
            c.execute('INSERT INTO inventory_moves(ingredient_id,delta,reason,created_at) VALUES(?,?,?,?)',(iid,body.stock-old['stock'],'manual_adjustment',now()))
        c.execute('COMMIT')
    return {'ok':True}


class RecipePart(InputModel):
    ingredient_id: int
    quantity: float=Field(gt=0)


class RecipeBody(InputModel):
    parts: list[RecipePart]


@app.put('/api/inventory/recipes/{pid}')
def set_recipe(pid:int,body:RecipeBody,user=Depends(allowed('admin'))):
    if len(set(p.ingredient_id for p in body.parts))!=len(body.parts):api_error(422,'مواد اولیه تکراری در دستور')
    with db() as c:
        if not c.execute('SELECT 1 FROM products WHERE id=?',(pid,)).fetchone():api_error(404,'محصول پیدا نشد')
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM recipes WHERE product_id=?',(pid,))
        for part in body.parts:
            quantity_decimal(part.quantity)
            if not c.execute('SELECT 1 FROM ingredients WHERE id=?',(part.ingredient_id,)).fetchone():api_error(422,'ماده اولیه نامعتبر است')
            c.execute('INSERT INTO recipes(product_id,ingredient_id,quantity) VALUES(?,?,?)',(pid,part.ingredient_id,part.quantity))
        c.execute('COMMIT')
    return {'ok':True}


class Restock(InputModel):
    quantity: float=Field(gt=0)
    unit_cost: Decimal=Field(ge=0,le=9000000000000,max_digits=15,decimal_places=2)
    from_drawer: bool=False
    authorization: ExceptionAuthorization | None=None
    idempotency_key: str|None=Field(default=None,min_length=8,max_length=100)


@app.post('/api/inventory/{iid}/restock')
def restock(iid:int,body:Restock,user=Depends(allowed('admin'))):
    qty=quantity_decimal(body.quantity);unit=money_minor(body.unit_cost,strict=True)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        fingerprint,old=cached_operation(c,'purchase',body.idempotency_key,{'ingredient_id':iid,'quantity':str(qty.normalize()),'unit_cost_minor':unit,'from_drawer':body.from_drawer},user)
        if old is not None:
            c.execute('COMMIT')
            return old
        ing=c.execute('SELECT * FROM ingredients WHERE id=?',(iid,)).fetchone()
        if not ing:api_error(404,'ماده اولیه پیدا نشد')
        cost_minor=money_minor(qty*Decimal(unit)/100)
        cost=cost_minor/100
        c.execute('UPDATE ingredients SET stock=stock+?,unit_cost=? WHERE id=?',(body.quantity,float(body.unit_cost),iid))
        c.execute('UPDATE ingredients SET stock=?,unit_cost_minor=? WHERE id=?',(float(quantity_decimal(quantity_decimal(ing['stock'])+qty)),unit,iid))
        c.execute('INSERT INTO inventory_moves(ingredient_id,delta,reason,created_at) VALUES(?,?,?,?)',(iid,body.quantity,'restock',now()))
        eid=c.execute('INSERT INTO expenses(title,amount,amount_minor,kind,created_at) VALUES(?,?,?,?,?)',(f'خرید {ing["name"]}',cost,cost_minor,'purchase',now())).lastrowid
        if body.from_drawer:movement(c,'expense',eid,-cost_minor,'cash',financial_context(c,user,body.authorization,'expense',eid),user)
        save_operation(c,'purchase',body.idempotency_key,fingerprint,user,{'ok':True,'expense':cost})
        c.execute('COMMIT')
    return {'ok':True,'expense':cost}


class ExpenseInput(InputModel):
    title: str=Field(min_length=2,max_length=120)
    amount: Decimal=Field(gt=0,le=9000000000000,max_digits=15,decimal_places=2)
    kind: str='operating'
    from_drawer: bool=False
    authorization: ExceptionAuthorization | None=None
    idempotency_key: str|None=Field(default=None,min_length=8,max_length=100)


@app.post('/api/finance/expenses')
def expense(body:ExpenseInput,user=Depends(allowed('admin'))):
    if body.kind not in ('operating','purchase'):api_error(422,'نوع هزینه نامعتبر است')
    amount=money_minor(body.amount,strict=True)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        fingerprint,old=cached_operation(c,'expense',body.idempotency_key,{'title':body.title,'amount_minor':amount,'kind':body.kind,'from_drawer':body.from_drawer},user)
        if old is not None:
            c.execute('COMMIT')
            return old
        oid=c.execute('INSERT INTO expenses(title,amount,amount_minor,kind,created_at) VALUES(?,?,?,?,?)',(body.title,float(body.amount),amount,body.kind,now())).lastrowid
        if body.from_drawer:movement(c,'expense',oid,-amount,'cash',financial_context(c,user,body.authorization,'expense',oid),user)
        save_operation(c,'expense',body.idempotency_key,fingerprint,user,{'id':oid})
        c.execute('COMMIT')
    return {'id':oid}


def report_period(start: date | None, end: date | None):
    if start and end and start>end:api_error(422,'شروع بازه باید قبل از پایان باشد')
    return (start or date.min,end or date.max)


def financial_report(c,start=None,end=None):
    first,last=report_period(start,end)
    def inside(stamp):return bool(stamp) and first<=local_datetime(stamp).date()<=last
    summary={key:0 for key in ('gross_sales','sales_returns','revenue','cash_received','cash_refunded','cogs','waste_cost','operating_expenses','inventory_purchases_cash_out','receivables','advances','total_orders','paid_orders','completed_orders')}
    daily={};weekly={};monthly={};receipts={};methods={};top={};hours={}
    states={'fulfillment':{key:0 for key in STATUS_FLOW},'payment':{key:0 for key in ('unpaid','paid','partially_refunded','refunded')}}
    completed_ids=set()
    payments={r['order_id']:dict(r) for r in c.execute('SELECT * FROM payments')}
    refunded={}
    for row in c.execute('SELECT * FROM refunds'):
        refunded[row['payment_id']]=refunded.get(row['payment_id'],0)+row['amount_minor']
    def sale_event(stamp,gross=0,credit=0,cost=0,count=0):
        day=local_datetime(stamp).date();jalali=jdatetime.date.fromgregorian(date=day)
        week=day-timedelta(days=(day.weekday()+2)%7)
        week_label=jdatetime.date.fromgregorian(date=week).strftime('%Y/%m/%d')
        keys=[(daily,day.isoformat(),jalali.strftime('%Y/%m/%d')),(weekly,week.isoformat(),week_label),(monthly,jalali.strftime('%Y-%m'),jalali.strftime('%Y/%m'))]
        for series,key,label in keys:
            r=series.setdefault(key,{'label':label,'gross_sales_minor':0,'sales_returns_minor':0,'cogs_minor':0,'orders':0})
            r['gross_sales_minor']+=gross;r['sales_returns_minor']+=credit;r['cogs_minor']+=cost;r['orders']+=count
    for order in c.execute('SELECT * FROM orders'):
        amount=money_minor(order['total']);pay=payments.get(order['id'])
        if inside(order['created_at']):
            summary['total_orders']+=1
            states['fulfillment'][order['status']]+=1
            state='unpaid' if not pay else ('paid' if not refunded.get(pay['id'],0) else ('refunded' if refunded[pay['id']]==money_minor(pay['amount']) else 'partially_refunded'))
            states['payment'][state]+=1
        if order['status']=='completed' and inside(order['completed_at']):
            cost=order['cost_total_minor'] if order['cost_total_minor'] is not None else money_minor(order['cost_total'])
            completed_ids.add(order['id'])
            summary['gross_sales']+=amount;summary['cogs']+=cost;summary['completed_orders']+=1
            sale_event(order['completed_at'],gross=amount,cost=cost,count=1)
            if not pay:summary['receivables']+=amount
            hour=local_datetime(order['created_at']).strftime('%H');hours[hour]=hours.get(hour,0)+1
        if order['wasted_at'] and inside(order['wasted_at']):summary['waste_cost']+=order['waste_cost_minor']
        if pay and order['status'] not in ('completed','cancelled') and inside(pay['created_at']):
            summary['advances']+=max(0,money_minor(pay['amount'])-refunded.get(pay['id'],0))
    for item in c.execute('SELECT order_id,name,qty,line_total FROM order_items'):
        if item['order_id'] in completed_ids:
            r=top.setdefault(item['name'],{'name':item['name'],'qty':0,'revenue':0})
            r['qty']+=item['qty'];r['revenue']+=item['line_total']
    for pay in payments.values():
        if inside(pay['created_at']):
            amount=money_minor(pay['amount']);summary['cash_received']+=amount;summary['paid_orders']+=1
            day=local_datetime(pay['created_at']).date().isoformat()
            d=receipts.setdefault(day,{'day':day,'revenue_minor':0,'payments':0})
            d['revenue_minor']+=amount;d['payments']+=1
            method=methods.setdefault(pay['method'],{'method':pay['method'],'cnt':0,'amount_minor':0,'refund_minor':0})
            method['amount_minor']+=amount;method['cnt']+=1
    for row in c.execute('SELECT * FROM refunds'):
        if inside(row['created_at']):
            summary['cash_refunded']+=row['amount_minor']
            methods.setdefault(row['method'],{'method':row['method'],'cnt':0,'amount_minor':0,'refund_minor':0})['refund_minor']+=row['amount_minor']
        if inside(row['recognized_at']):
            summary['sales_returns']+=row['amount_minor'];sale_event(row['recognized_at'],credit=row['amount_minor'])
    for row in c.execute('SELECT * FROM expenses'):
        if inside(row['created_at']):
            amount=row['amount_minor'] if row['amount_minor'] is not None else money_minor(row['amount'])
            summary['operating_expenses' if row['kind']=='operating' else 'inventory_purchases_cash_out']+=amount
    summary['revenue']=summary['gross_sales']-summary['sales_returns']
    summary['gross_profit']=summary['revenue']-summary['cogs']
    summary['net_operating_profit']=summary['gross_profit']-summary['operating_expenses']-summary['waste_cost']
    summary['net_cash_received']=summary['cash_received']-summary['cash_refunded']
    summary['cash_flow_estimate']=summary['net_cash_received']-summary['operating_expenses']-summary['inventory_purchases_cash_out']
    counts={'total_orders','paid_orders','completed_orders'}
    summary.update({key+'_minor':value for key,value in list(summary.items()) if key not in counts})
    for key in list(summary):
        if key not in counts and not key.endswith('_minor'):summary[key]=summary[key]/100
    average=int((Decimal(summary['revenue_minor'])/summary['completed_orders']).quantize(Decimal('1'),rounding=ROUND_HALF_UP)) if summary['completed_orders'] else 0
    summary['average_order_value_minor']=average
    summary['average_order_value']=average/100
    def output(series,keyname):
        result=[]
        for key,row in sorted(series.items()):
            net=row['gross_sales_minor']-row['sales_returns_minor']
            result.append({keyname:key,**row,'sales_minor':net,'sales':net/100,'cogs':row['cogs_minor']/100})
        return result
    return {'order_states':states,'summary':summary,'sales_daily':output(daily,'day'),'weekly':output(weekly,'week'),'monthly':output(monthly,'month'),
        'daily':[{**r,'revenue':r['revenue_minor']/100,'label':jdatetime.date.fromgregorian(date=date.fromisoformat(day)).strftime('%Y/%m/%d')} for day,r in sorted(receipts.items(),reverse=True)],
        'methods':[{**r,'amount':r['amount_minor']/100} for r in methods.values()],
        'top':sorted(top.values(),key=lambda r:r['qty'],reverse=True)[:7],
        'hours':[{'hour':hour,'orders':count} for hour,count in sorted(hours.items(),key=lambda x:x[1],reverse=True)[:5]]}


def financial_summary(c,start=None,end=None):
    return financial_report(c,start,end)['summary']


@app.get('/api/finance')
def finance(start:date|None=None,end:date|None=None,user=Depends(allowed('admin'))):
    report_period(start,end)
    with db() as c:
        c.execute('BEGIN')
        result=financial_report(c,start,end)
        result['expenses']=[dict(r) for r in c.execute('SELECT * FROM expenses ORDER BY id DESC') if (start or date.min)<=local_datetime(r['created_at']).date()<=(end or date.max)][:80]
        result.update(period={'start':start,'end':end},basis='sales_on_completion; refunds_on_recognition; cash_on_operation',calendar='jalali',timezone='Asia/Tehran')
        result['daily']=result['daily'][:30]
        return result


@app.get('/api/analytics')
def analytics(start:date|None=None,end:date|None=None,user=Depends(allowed('admin'))):
    report_period(start,end)
    with db() as c:
        c.execute('BEGIN');r=financial_report(c,start,end)
        low=[asdict(x) for x in c.execute('SELECT * FROM ingredients WHERE stock<=reorder ORDER BY stock/NULLIF(reorder,0)')]
        custs=c.execute('SELECT COUNT(*) FROM customers').fetchone()[0]
        repeat={}
        for row in c.execute("SELECT customer_id,completed_at FROM orders WHERE customer_id IS NOT NULL AND status='completed'"):
            if (start or date.min)<=local_datetime(row['completed_at']).date()<=(end or date.max):repeat[row['customer_id']]=repeat.get(row['customer_id'],0)+1
    tips=['سود از فروش خالص تحویل‌شده منهای بهای مصرف محاسبه می‌شود؛ استرداد مواد را برنمی‌گرداند.',f'{len(low)} ماده به نقطه سفارش رسیده‌اند.' if low else 'موجودی مواد بالاتر از نقطه سفارش است.']
    return {**r,'trend':[{'day':x['day'],'sales':x['sales'],'orders':x['orders']} for x in r['sales_daily'][-14:]],'low_stock':low,'customer_count':custs,
        'repeat_customers':sum(v>1 for v in repeat.values()),'insights':tips,'engine':'تحلیل قاعده‌محور، بدون مدل AI خارجی','calendar':'jalali','timezone':'Asia/Tehran','period':{'start':start,'end':end}}


@app.get('/api/crm')
def crm(user=Depends(allowed('admin'))):
    with db() as c:
        rows=c.execute('''SELECT c.*,COUNT(o.id) AS visits,COALESCE(SUM(CASE WHEN pay.id IS NOT NULL THEN pay.amount*100-COALESCE((SELECT SUM(r.amount_minor) FROM refunds r WHERE r.payment_id=pay.id),0) ELSE 0 END),0) AS spend_minor,
          MAX(o.created_at) AS last_order FROM customers c LEFT JOIN orders o ON o.customer_id=c.id LEFT JOIN payments pay ON pay.order_id=o.id
          GROUP BY c.id ORDER BY visits DESC,c.id DESC''').fetchall()
        return [{**dict(x),'spend':x['spend_minor']/100} for x in rows]


class CustomerNote(InputModel):
    notes:str=Field(default='',max_length=500)


@app.patch('/api/crm/{cid}')
def crm_note(cid:int,body:CustomerNote,user=Depends(allowed('admin'))):
    with db() as c:
        result=c.execute('UPDATE customers SET notes=? WHERE id=?',(body.notes,cid))
        if not result.rowcount:api_error(404,'مشتری پیدا نشد')
    return {'ok':True}


@app.get('/api/attendance')
def attendance(user=Depends(allowed('admin','staff','cashier'))):
    with db() as c:
        uid=None if user['role']=='admin' else user['id']
        users=[asdict(u) for u in c.execute('SELECT id,name,role,username,telegram_user_id FROM users WHERE (? IS NULL OR id=?) ORDER BY id',(uid,uid))]
        rows=[asdict(r) for r in c.execute('''SELECT a.*,u.name FROM attendance a JOIN users u ON a.user_id=u.id
                 WHERE (? IS NULL OR a.user_id=?) ORDER BY a.id DESC LIMIT 80''',(uid,uid))]
        running=c.execute("SELECT value,updated_at FROM bot_runtime WHERE key='online'").fetchone()
        username=c.execute("SELECT value FROM bot_runtime WHERE key='username'").fetchone()
        connected=bool(running and running['value']=='1' and time.time()-running['updated_at']<75)
        return {'users':users,'punches':rows,'telegram_connected':connected,
                'telegram_configured':bool(os.getenv('TELEGRAM_BOT_TOKEN')),
                'telegram_username':username['value'] if username else None}


class PunchInput(InputModel):
    action:str


def punch_for_user(uid:int, action:str, source:str):
    if action not in ('in','out'):api_error(422,'نوع تردد نامعتبر است')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        last=c.execute('SELECT action FROM attendance WHERE user_id=? ORDER BY id DESC LIMIT 1',(uid,)).fetchone()
        if not last and action=='out':api_error(409,'ابتدا ورود خود را ثبت کنید')
        if last and last['action']==action:api_error(409,'آخرین تردد شما همین وضعیت را دارد')
        oid=c.execute('INSERT INTO attendance(user_id,action,source,created_at) VALUES(?,?,?,?)',(uid,action,source,now())).lastrowid
        c.execute('COMMIT')
        return {'id':oid,'action':action,'created_at':now()}


@app.post('/api/attendance/punch')
def attendance_punch(body:PunchInput,user=Depends(allowed('admin','staff','cashier'))):
    return punch_for_user(user['id'],body.action,'panel')


@app.post('/api/attendance/telegram-link')
def attendance_link(user=Depends(allowed('admin','staff','cashier'))):
    code=secrets.token_urlsafe(6).replace('-','').replace('_','')[:8].upper()
    with db() as c:
        c.execute('DELETE FROM telegram_links WHERE user_id=?',(user['id'],))
        c.execute('INSERT INTO telegram_links(code,user_id,expires) VALUES(?,?,?)',(code,user['id'],time.time()+600))
    return {'code':code,'expires_seconds':600,'instruction':f'/link {code}'}


@app.get('/api/demo/health')
def health():
    return {'ok':True,'database':str(DB_PATH.name),'mode':'demo' if DEMO_MODE else 'local','online_payments':False}
