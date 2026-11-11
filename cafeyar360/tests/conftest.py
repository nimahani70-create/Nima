import pytest
import app as server

@pytest.fixture(autouse=True)
def explicit_demo(monkeypatch):
    monkeypatch.setattr(server, 'DEMO_MODE', True)
    for name in ('ADMIN','CASHIER','BARISTA'):
        monkeypatch.delenv('CAFE_PASSWORD_'+name, raising=False)
