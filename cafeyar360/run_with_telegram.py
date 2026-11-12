"""Launch the local web server and attendance bot with an ephemeral secret.
The token is never written to a file or printed to the terminal.
"""
import getpass
import os
import subprocess
import sys
import time

from manage import initialize


def main():
    os.environ.setdefault('DEMO_MODE','1')
    initialize()
    env=os.environ.copy()
    token=env.get('TELEGRAM_BOT_TOKEN')
    if not token:
        print('Paste your CURRENT BotFather token. Input is hidden; nothing will be saved.')
        token=getpass.getpass('Telegram bot token: ').strip()
    if not token or ':' not in token:
        raise SystemExit('A valid BotFather token is required. No servers were started.')
    env['TELEGRAM_BOT_TOKEN']=token
    env.setdefault('DEMO_MODE','1')
    token=None
    commands=[
        [sys.executable,'-m','uvicorn','app:app','--host','0.0.0.0','--port','8000'],
        [sys.executable,'telegram_bot.py'],
    ]
    processes=[]
    try:
        for cmd in commands:
            processes.append(subprocess.Popen(cmd,env=env))
        print('Local demo: http://127.0.0.1:8000/presentation')
        print('Attendance panel: http://127.0.0.1:8000/attendance')
        print('Both services must keep running. Press Ctrl+C to stop.')
        while True:
            for process in processes:
                status=process.poll()
                if status is not None:
                    raise SystemExit('A service exited with status '+str(status)+'. See its error above.')
            time.sleep(1)
    except KeyboardInterrupt:
        print('Stopping local demo and Telegram bot...')
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:process.wait(timeout=8)
            except subprocess.TimeoutExpired:process.kill()


if __name__=='__main__':
    main()
