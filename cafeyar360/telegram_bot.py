"""Optional Telegram attendance integration (polling).
Set TELEGRAM_BOT_TOKEN and run 'python telegram_bot.py'.
Employee links Telegram via /link CODE from an authenticated dashboard.
Never expose the bot token in a browser or commit it.
"""
import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error

from app import init_db, db, punch_for_user

TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','')


def telegram(method, values):
    req=urllib.request.Request(
        f'https://api.telegram.org/bot{TOKEN}/{method}',
        data=urllib.parse.urlencode(values).encode('utf-8'),
        headers={'Content-Type':'application/x-www-form-urlencoded'},
        method='POST')
    with urllib.request.urlopen(req,timeout=42) as resp:
        return json.loads(resp.read().decode())


def dispatch(message):
    # Attendance and one-time linking are permitted only in private chats.
    if message.get('chat',{}).get('type')!='private':
        return
    sender=message.get('from',{}).get('id')
    chat=message.get('chat',{}).get('id')
    cmd=message.get('text','').strip().split()
    if not sender or not chat or not cmd:return
    action=cmd[0].split('@')[0].lower()
    if action=='/start':
        response='ربات حضور و غیاب کافه یار ۳۶۰. ابتدا کد اتصال پنل را با /link CODE وارد کنید. سپس /in یا /out را ارسال کنید.'
    elif action=='/link':
        if len(cmd)!=2:response='کد را به صورت /link CODE وارد کنید.'
        else:
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                row=c.execute('SELECT user_id FROM telegram_links WHERE code=? AND expires>?',(cmd[1].upper(),time.time())).fetchone()
                if row:
                    used=c.execute('SELECT id FROM users WHERE telegram_user_id=? AND id<>?',(str(sender),row['user_id'])).fetchone()
                    if used:response='این حساب تلگرام قبلاً به کاربر دیگری متصل شده است.'
                    else:
                        c.execute('UPDATE users SET telegram_user_id=? WHERE id=?',(str(sender),row['user_id']))
                        c.execute('DELETE FROM telegram_links WHERE code=?',(cmd[1].upper(),))
                        response='حساب تلگرام متصل شد. دستورهای /in و /out فعال هستند.'
                    c.execute('COMMIT')
                else:response='کد اتصال نامعتبر یا منقضی شده است.'
    elif action in ('/in','/out','/status'):
        with db() as c:
            user=c.execute('SELECT id,name FROM users WHERE telegram_user_id=?',(str(sender),)).fetchone()
        if not user:response='ابتدا از پنل حضور و غیاب، کد اتصال دریافت و /link CODE را ارسال کنید.'
        elif action=='/status':
            with db() as c:
                row=c.execute('SELECT action,created_at FROM attendance WHERE user_id=? ORDER BY id DESC LIMIT 1',(user['id'],)).fetchone()
            response='آخرین ثبت: '+(('ورود' if row['action']=='in' else 'خروج')+' | '+row['created_at'] if row else 'هیچ ترددی ثبت نشده')
        else:
            try:
                item=punch_for_user(user['id'],'in' if action=='/in' else 'out','telegram')
                response=('ورود' if action=='/in' else 'خروج')+' شما در کافه یار ۳۶۰ ثبت شد. '+item['created_at']
            except Exception as exc:
                response='ثبت انجام نشد: '+getattr(exc,'detail','خطای داخلی')
    else:response='دستورهای معتبر: /start، /link CODE، /in، /out، /status'
    telegram('sendMessage',{'chat_id':chat,'text':response})


def runtime_state(key, value):
    with db() as c:
        c.execute("INSERT INTO bot_runtime(key,value,updated_at) VALUES(?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                  (key,str(value),time.time()))


def main():
    if not TOKEN:
        raise SystemExit('TELEGRAM_BOT_TOKEN is not configured. Use the secure launcher.')
    init_db()
    runtime_state('online','0')
    try:
        try:
            profile=telegram('getMe',{})
            if not profile.get('ok'):
                raise ValueError('Telegram rejected bot credentials')
            username=profile.get('result',{}).get('username','')
            runtime_state('username',username)
            hook=telegram('getWebhookInfo',{}).get('result',{}).get('url','')
            if hook:
                raise SystemExit('A webhook is already configured for this bot. Polling cannot run concurrently. Remove the existing webhook deliberately, only if this bot is dedicated to cafe attendance.')
        except (urllib.error.HTTPError,urllib.error.URLError,ValueError) as exc:
            # Never print the exception URL: Telegram's API URL contains the bot token.
            raise SystemExit('Cannot validate the bot with Telegram. Check the NEW token and network access. '+type(exc).__name__)
        print('Cafe Nima Telegram attendance ready: @'+username,flush=True)
        offset=0
        while True:
            try:
                events=telegram('getUpdates',{'offset':offset,'timeout':25,'allowed_updates':'["message"]'})
                if not events.get('ok'):
                    raise ValueError('Telegram polling failed')
                runtime_state('online','1')
                for update in events.get('result',[]):
                    offset=max(offset,update['update_id']+1)
                    if 'message' in update:
                        try:dispatch(update['message'])
                        except Exception as exc:
                            print('Telegram message processing issue:',type(exc).__name__,flush=True)
            except KeyboardInterrupt:
                break
            except Exception as exc:
                runtime_state('online','0')
                # Avoid logging request URLs, which may embed the secret.
                print('Telegram temporarily unavailable:',type(exc).__name__,flush=True)
                time.sleep(6)
    finally:
        runtime_state('online','0')


if __name__=='__main__':main()
