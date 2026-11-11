"""Local administration. Secrets are read interactively, never passed as CLI arguments."""
import argparse
import getpass
import sqlite3
from pathlib import Path

import app as server


def change_password(username, password):
    if len(password) < 12:
        raise ValueError('Password must contain at least 12 characters')
    # Do not call init_db: this command must work on legacy demo databases.
    if not server.DB_PATH.exists():
        raise ValueError('Database does not exist; run init first')
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


def main():
    parser=argparse.ArgumentParser(description='CafeYar360 local administration')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('init',help='Create or migrate the database; DEMO_MODE=1 opts into sample data')
    pwd=sub.add_parser('password',help='Change a password and revoke all sessions for that user')
    pwd.add_argument('username')
    save=sub.add_parser('backup',help='Online SQLite backup to a new file')
    save.add_argument('destination')
    args=parser.parse_args()
    try:
        if args.command=='init':
            server.init_db()
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
