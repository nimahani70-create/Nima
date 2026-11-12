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

## Canonical repository and permanent delivery workflow

- The source of truth is https://github.com/nimahani70-create/Nima, branch `main`, project directory `cafeyar360/`. Read current remote history before each sprint; a previously recorded SHA is historical, not a pinned starting point.
- Preserve unfinished work and local history. Never use destructive reset, delete unrelated files, or force push. Work on `feature/sprint-N`; compare/synchronize against current main before publishing.
- Restrict changes to `cafeyar360/`. The sole approved root exception is `.github/workflows/cafeyar360-tests.yml`, which runs only this project's tests. Do not change the Mintlify site or its configuration.
- Keep existing filenames, technical paths, SQLite filename `cafe_nima.sqlite3`, FastAPI architecture and existing CSS/JavaScript. The product is Cafe Yar 360 / کافه یار ۳۶۰; Cafe Nima / کافه نیما is the sample cafe.
- Run baseline tests before development. Run unit, integration and browser checks for edited behavior; distinguish local results from GitHub Actions results. Commit/push only reviewed changes, open a PR and inspect checks on its actual HEAD.
- Do not merge without explicit owner approval of the concrete version/PR. Specifically, publish and obtain approval/merge of sprint 2 before starting sprint 3 implementation. Financial sprint-2 behavior must remain covered by regression tests.
- Never publish real passwords, Telegram tokens, `.env`, operational DBs or real customer data. There are no executable default demo passwords. Synthetic test-only values are permitted only in tests; runtime credentials come from environment or hidden local setup.
- Back up an existing DB before changing it. Test using isolated synthetic databases; do not point test scripts at the operational database. Automatic CLI snapshots stay in ignored `backups/`.
- Before approval, publish only clearly labeled candidate review outputs to https://drive.google.com/drive/folders/1XQacIaCup98FmarhKyJZjrow_A_Fr9Md . No candidate is an approved main release.
- After owner approval and merge, export the approved Git commit: source ZIP, history bundle, synthetic DB, Persian report, test results and SHA256 to https://drive.google.com/drive/folders/1NArzX1JX6zGgmn3oxnisgBdX-yPSbR0S . Put demo/review assets in the review folder. Preserve older versions.
- Verify uploaded files by readback and compare ZIP file contents to the declared commit, not merely a local working tree. Report actual commit/PR links, successful/failed tests, Drive links, limitations and next sprint.
- A failed Push, workflow permission or Drive upload must be reported with its actual error; never claim a remote operation succeeded without readback.

## Approved sprint-3 decisions (implementation waits for sprint-2 approval/merge)

- Employee roles: admin/cashier/barista/staff; barista and staff have equal order-operation permissions and no financial/admin access. Preserve existing role records; add the distinct role without rewriting financial behavior.
- Employee schedules are dated, non-overlapping for the same person and may span midnight. Both ordinary in and out punches must be inside the assigned shift. Late/missing punches require a manager correction with reason and an immutable audit trail.
- Work hours use actual completed in/out intervals, no automatic meal deduction. Open presence is provisional and separate; wage/overtime calculation is outside scope.
- Support creating a new table or renumbering an existing table with stable identity. Preserve original FK references and printed QR ownership; old numbers cannot be reassigned to a different table. Disabled tables reject new orders but do not cancel old ones.
- Telegram uses verified message-send time, durable bot/update deduplication and offset. Conflicting delayed/out-of-order messages require manager review rather than silently inventing work hours. Deactivation invalidates all sessions and Telegram linkage.
- Add rate limiting, session management and CSRF without disabling them in tests or bypassing financial endpoints. Existing finance tests are mandatory regressions. No real Telegram test unless valid environment credentials and an authorized test conversation exist.
