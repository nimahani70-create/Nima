"""Local administration. Secrets are read interactively, never passed as CLI arguments."""
import argparse
import getpass
import sqlite3
import os
import sys
import secrets
from datetime import datetime, timezone
from pathlib import Path

import app as server


def change_password(username, password):
    if len(password) < 12:
        raise ValueError('Password must contain at least 12 characters')
    # Do not call init_db: this command must work on legacy demo databases.
    if not server.DB_PATH.exists():
        raise ValueError('Database does not exist; run init first')
    automatic_backup('password')
    with server.db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT id FROM users WHERE username=?',(username,)).fetchone()
        if not row:
            raise ValueError('User does not exist')
        c.execute('UPDATE users SET password_hash=? WHERE id=?',(server.hpass(password),row['id']))
        c.execute('DELETE FROM sessions WHERE user_id=?',(row['id'],))
        c.execute('COMMIT')


def backup(destination):
    if not server.DB_PATH.exists():
        raise ValueError('Source database does not exist')
    target=Path(destination)
    if target.exists() or target.resolve()==server.DB_PATH.resolve():
        raise ValueError('Backup destination must be a new file')
    target.parent.mkdir(parents=True,exist_ok=True)
    try:
        with server.db() as source:
            output=sqlite3.connect(target)
            try:
                source.backup(output)
            finally:
                output.close()
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def automatic_backup(operation):
    if not server.DB_PATH.exists():return None
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target=server.DB_PATH.parent/'backups'/f'before-{operation}-{stamp}-{secrets.token_hex(4)}.sqlite3'
    backup(target)
    return target


def initialize():
    fresh=True
    if server.DB_PATH.exists():
        c=sqlite3.connect(server.DB_PATH.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            has_users=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
            fresh=not has_users or c.execute('SELECT COUNT(*) FROM users').fetchone()[0]==0
        finally:c.close()
    if fresh:
        for username in ('admin','cashier','barista'):
            key='CAFE_PASSWORD_'+username.upper()
            password=os.getenv(key,'')
            if not password:
                if not sys.stdin.isatty():
                    raise ValueError('Non-interactive setup requires CAFE_PASSWORD_ADMIN, CAFE_PASSWORD_CASHIER and CAFE_PASSWORD_BARISTA; no password is read or printed')
                password=getpass.getpass('New local password for '+username+' (12+ characters): ')
                if password!=getpass.getpass('Confirm password for '+username+': '):
                    raise ValueError('Passwords do not match')
            if len(password)<12:raise ValueError('Every local password must contain at least 12 characters')
            os.environ[key]=password
    automatic_backup('init')
    server.init_db()


def main():
    parser=argparse.ArgumentParser(description='Cafe Yar 360 local administration')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('init',help='Create or migrate the database; DEMO_MODE=1 opts into sample data')
    pwd=sub.add_parser('password',help='Change a password and revoke all sessions for that user')
    pwd.add_argument('username')
    save=sub.add_parser('backup',help='Online SQLite backup to a new file')
    save.add_argument('destination')
    args=parser.parse_args()
    try:
        if args.command=='init':
            initialize()
        elif args.command=='password':
            password=getpass.getpass('New password (12+ characters): ')
            if password != getpass.getpass('Confirm password: '):
                raise ValueError('Passwords do not match')
            change_password(args.username,password)
        else:
            backup(args.destination)
    except (ValueError,RuntimeError,sqlite3.Error) as exc:
        # DB errors are kept generic: do not log SQL data or secrets.
        parser.exit(1, ('Database operation failed' if isinstance(exc,sqlite3.Error) else str(exc))+'\n')
    print('Operation completed.')


if __name__=='__main__':
    main()
