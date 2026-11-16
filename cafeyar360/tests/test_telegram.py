"""Offline tests for Telegram attendance; no token and no network required."""
import time
import pytest
from fastapi.testclient import TestClient
import app as server
import telegram_bot as bot
from conftest import TEST_PASSWORDS


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'DB_PATH', tmp_path / 'telegram.sqlite3')
    monkeypatch.setattr(bot, 'db', server.db)
    monkeypatch.setattr(bot, 'init_db', server.init_db)
    with TestClient(server.app) as client:
        yield client


def login(c):
    response=c.post('/api/auth/login', json={'username':'barista','password':TEST_PASSWORDS['barista']})
    assert response.status_code==200


def test_link_private_chat_and_telegram_attendance(client, monkeypatch):
    output=[]
    monkeypatch.setattr(bot,'telegram',lambda method, values: output.append((method,values)))
    login(client)
    response=client.post('/api/attendance/telegram-link')
    assert response.status_code==200
    code=response.json()['code']
    def msg(command,kind='private'):
        bot.dispatch({'from':{'id':715551},'chat':{'id':715551,'type':kind},'text':command})
    msg('/link '+code, 'group')
    assert output==[]
    msg('/link '+code)
    assert 'متصل شد' in output[-1][1]['text']
    msg('/in')
    assert 'ثبت شد' in output[-1][1]['text']
    msg('/status')
    assert 'ورود' in output[-1][1]['text']
    msg('/out')
    assert 'ثبت شد' in output[-1][1]['text']
    msg('/out')
    assert 'ثبت انجام نشد' in output[-1][1]['text']
    row=client.get('/api/attendance').json()
    assert [x['action'] for x in row['punches'][:2]]==['out','in']
    assert all(x['source']=='telegram' for x in row['punches'][:2])
    assert next(x for x in row['users'] if x['username']=='barista')['telegram_user_id']=='715551'


def test_runtime_status_reflects_successful_poll_not_just_token(client):
    login(client)
    assert client.get('/api/attendance').json()['telegram_connected'] is False
    with server.db() as c:
        c.execute("INSERT INTO bot_runtime(key,value,updated_at) VALUES('online','1',?)",(time.time(),))
        c.execute("INSERT INTO bot_runtime(key,value,updated_at) VALUES('username','cafe_bot_test',?)",(time.time(),))
    r=client.get('/api/attendance').json()
    assert r['telegram_connected'] is True
    assert r['telegram_username']=='cafe_bot_test'
    with server.db() as c:
        c.execute("UPDATE bot_runtime SET updated_at=? WHERE key='online'",(time.time()-120,))
    assert client.get('/api/attendance').json()['telegram_connected'] is False


def test_unlinked_sender_and_expired_link_cannot_punch(client, monkeypatch):
    output=[]
    monkeypatch.setattr(bot,'telegram',lambda method,values:output.append((method,values)))
    message={'from':{'id':999001},'chat':{'id':999001,'type':'private'},'text':'/in'}
    bot.dispatch(message)
    login(client)
    assert client.get('/api/attendance').json()['punches']==[]
    code=client.post('/api/attendance/telegram-link').json()['code']
    with server.db() as c:
        c.execute('UPDATE telegram_links SET expires=? WHERE code=?',(time.time()-1,code))
    bot.dispatch({**message,'text':'/link '+code})
    bot.dispatch(message)
    assert client.get('/api/attendance').json()['punches']==[]
    assert 'منقضی' in output[-2][1]['text']
