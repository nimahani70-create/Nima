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

from app import init_db, db
import workforce

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
    """Compatibility helper for direct message tests; polling uses durable process_update."""
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        response,_=workforce.telegram_message(c,message)
        c.execute('COMMIT')
    if response:
        telegram('sendMessage',{'chat_id':message['chat']['id'],'text':response})


def process_update(update,bot_id):
    return workforce.telegram_update(bot_id,update)


def flush_replies(bot_id):
    # At-least-once replies are safe; attendance itself is exactly once per update.
    with db() as c:
        rows=c.execute('SELECT * FROM telegram_inbox WHERE bot_id=? AND delivered=0 ORDER BY update_id LIMIT 100',(bot_id,)).fetchall()
    for row in rows:
        if row['response']:
            result=telegram('sendMessage',{'chat_id':row['chat_id'],'text':row['response']})
            if not result or not result.get('ok'):
                raise ValueError('Telegram reply not acknowledged')
        with db() as c:
            c.execute('UPDATE telegram_inbox SET delivered=1 WHERE bot_id=? AND update_id=?',(bot_id,row['update_id']))


def durable_offset(bot_id):
    with db() as c:
        row=c.execute('SELECT next_offset FROM telegram_offsets WHERE bot_id=?',(bot_id,)).fetchone()
        return row['next_offset'] if row else 0


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
        bot_id=str(profile['result']['id'])
        offset=durable_offset(bot_id)
        while True:
            try:
                events=telegram('getUpdates',{'offset':offset,'timeout':25,'allowed_updates':'["message"]'})
                if not events.get('ok'):
                    raise ValueError('Telegram polling failed')
                runtime_state('online','1')
                for update in events.get('result',[]):
                    process_update(update,bot_id)
                    offset=durable_offset(bot_id)
                flush_replies(bot_id)
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
