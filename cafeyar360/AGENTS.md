# Cafe Nima - instructions for Codex

## Current scope
This is a LOCAL demo for a cafe management suite. The Python FastAPI backend, SQLite persistence, and frontend in `static/` must remain integrated. The customer menu, orders, staff, cashier (manual cash/POS settlement), admin catalog, inventory, recipes, finance, analytics, CRM, attendance, and optional Telegram bot must continue working.

Online payment gateway is explicitly OUT OF SCOPE and must not be represented as connected. Telegram polling requires an actual token set in the environment and must never store the token in files or logs.

## How to work
1. Read `README.md`, inspect `app.py`, `static/`, and `tests/` before changing architecture.
2. Start each coding task by identifying affected endpoints, schema and permissions; avoid whole-project rewrites.
3. Use Python 3.11+, install requirements, and run `python -m pytest -q` after each change.
4. Make focused commits; describe changed behavior, tested scenario, risks and rollback.
5. Avoid committing any SQLite database, `.env`, live credentials, phone/customer data, access keys, or Telegram token.
6. Never expose this demo directly to the public Internet without reviewing authentication, authorization, rate limiting, CSRF, backups, and legal/data requirements.
7. Preserve Persian RTL UI, mobile QR ordering flow, and offline-first operation.
8. Avoid claiming real online payment, direct POS hardware integration, production accounting compliance or AI API integration without real implementation and tests.

## Suggested priority
- Security hardening for LAN deployment (no default passwords, secure sessions).
- Integration/end-to-end tests for order -> staff -> cashier -> stock -> finance.
- Robust backup/restore, permissions, logs, actual receipt and POS integration.
- CRM, analytics, attendance workflows, and a professional sales/demo experience.
