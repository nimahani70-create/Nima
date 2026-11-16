"""Run bootstrap in a fresh interpreter: app caches mode at import time."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('configured_mode', [None, '0', '1'])
def test_telegram_launcher_bootstrap_mode(tmp_path, configured_mode):
    env = os.environ.copy()
    env.pop('DEMO_MODE', None)
    if configured_mode is not None:
        env['DEMO_MODE'] = configured_mode
    env['CAFE_DB_PATH'] = str(tmp_path / 'cafe_nima.sqlite3')
    env.pop('TELEGRAM_BOT_TOKEN', None)
    for name in ('ADMIN', 'CASHIER', 'BARISTA'):
        env['CAFE_PASSWORD_' + name] = 'test-only-launcher-password-' + name
    code = """
import os
import run_with_telegram as launcher
import app
from fastapi.testclient import TestClient
launcher.initialize()
expected = os.environ['DEMO_MODE'] == '1'
assert app.DEMO_MODE == expected
with TestClient(app.app) as client:
    menu = client.get('/api/public/menu').json()
    if expected:
        assert len(menu['products']) >= 10
        assert len(menu['tables']) == 12
        response = client.get('/api/qr/3')
        assert response.status_code == 200
        assert response.content.startswith(b'\\x89PNG\\r\\n\\x1a\\n')
    else:
        assert menu['products'] == []
        assert menu['tables'] == []
"""
    result = subprocess.run([sys.executable, '-c', code],
                            cwd=Path(__file__).resolve().parents[1], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
