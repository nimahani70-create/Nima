import pytest
import app as server

TEST_PASSWORDS={name:'test-only-'+name+'-long-password' for name in ('admin','cashier','barista')}

@pytest.fixture(autouse=True)
def explicit_demo(monkeypatch):
    monkeypatch.setattr(server, 'DEMO_MODE', True)
    for name,password in TEST_PASSWORDS.items():
        monkeypatch.setenv('CAFE_PASSWORD_'+name.upper(), password)
