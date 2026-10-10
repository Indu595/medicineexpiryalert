# Medicine Expiry Alert System

A responsive, multi-branch Medicine Expiry Alert System built with Flask, SQLite, HTML, CSS, and vanilla JavaScript. It tracks medicine batch expiry dates and notifies each branch head when medicines are expired or will expire within 15 days.

## Overview

The main administrator creates hospital branches and grants each branch head access using an email address and a specified password. Branch heads select their branch when signing in and can manage medicines only within that branch. The administrator shares credentials directly and securely; the application does not send email invitations.

The system stores medicine name, batch number, optional manufacturing date, expiry date, and quantity. It automatically calculates expiry status and stock availability. Expired medicines are clearly marked “do not use.”

## Features

- Main administrator setup and protected branch provisioning.
- Assign or reset a branch head's email and password for a branch.
- Branch-scoped logins, medicine records, expiry statistics, CSV reports, and email/browser notifications.
- Add, edit, search, filter, and delete medicine records.
- Color-coded expired, urgent, soon, and upcoming expiry alerts.
- Automatic email digest to each branch head for newly-alerting expired medicines and those expiring within 15 days.
- Optional branch-scoped browser notifications for expired medicines and those expiring within 15 days.
- Spoken expiry summary naming each expired or soon-to-expire medicine and batch when a branch head opens the dashboard, with a **Read alerts** button to replay it.
- Dashboard statistics for expiry windows of 7, 30, and 90 days.
- Low-stock flag for quantities of 10 units or fewer.
- CSV export, responsive desktop/mobile dashboard, and realistic sample medicines.
- Existing SQLite databases migrate while retaining medicine rows and existing accounts where possible.

Expiry status windows are inclusive: 0–7 days is urgent, 8–30 days is soon, and 31–90 days is upcoming. Medicines that expired before today are marked expired. Branch-head browser notifications use the requested 15-day threshold, independently of those dashboard buckets.

## Requirements

- Python 3.9+
- pip

## Setup

1. Open a terminal in this project folder.
2. Create and activate a virtual environment:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

4. Start the application:

   ```powershell
   python app.py
   ```

5. Visit [http://127.0.0.1:5000](http://127.0.0.1:5000).

On first visit, create a main administrator with a valid email and a password of at least 12 characters. Setup signs you in. Use the **Branch access** page to create hospital branches and grant each head access by setting their email and password. For an existing branch, save updated credentials to replace its current branch-head access. Share login credentials directly and securely; this app does not email invitations. Branch heads sign in by choosing their hospital branch and using their assigned email and password.

### Configure expiry alert email

The app checks for due expiry alerts at startup and hourly while running. It sends one email digest per branch head, containing newly-alerting medicine batches for that branch. An alert is recorded as sent only after the SMTP server accepts the message. It is not repeated for the same medicine batch and expiry date. Email is not sent until an SMTP server is configured. Set these environment variables before starting the app:

For Gmail, turn on 2-Step Verification for the sender's Google account, then create a Google **App Password** (use that generated password, not the normal Gmail password). Copy `.env.example` to `.env`, replace the sender and App Password placeholders in `.env`, then restart Flask. Use the same Gmail address for `SMTP_USERNAME`, `SMTP_FROM`, and the branch head email if the sender and recipient should be the same. Remove any spaces from the Google App Password before putting it in `.env`.

```powershell
Copy-Item .env.example .env
python app.py
```

Edit `.env` locally with the real sender account and its App Password. Do not paste the App Password into chat or commit `.env`; it is excluded by `.gitignore`. The application loads `.env` on startup. For other SMTP providers, set their SMTP host, port, username, password, sender address, and TLS options in `.env`:

```dotenv
SMTP_HOST=smtp.example.org
SMTP_PORT=587
SMTP_USERNAME=your-smtp-login
SMTP_PASSWORD=your-smtp-password
SMTP_FROM=medicine-alerts@example.org
SMTP_USE_TLS=1
```

For an SSL-wrapped SMTP connection (commonly port 465), set `SMTP_USE_SSL=1` and `SMTP_USE_TLS=0`. The scheduler runs inside the `python app.py` process; for production, keep one scheduler-enabled app process running or run an equivalent single scheduler process alongside the web workers. SMTP connection failures are logged and unrecorded alerts are retried on the next hourly check.

The SQLite database (`medicines.db`) is created in the project directory. A fresh database includes realistic sample medicines in the initial branch. A migrated database preserves medicine records and branch accounts. Legacy system administrator accounts are retained as main administrator accounts where possible.

Complete initial setup from a trusted private machine before exposing the application. Setup is available only until the first main administrator is created. For a shared or networked deployment, set a stable, secret `SECRET_KEY`, use persistent database storage, serve over HTTPS, and set `SESSION_COOKIE_SECURE=1`. The `python app.py` entry point runs Waitress bound to `127.0.0.1` and starts the hourly expiry-email scheduler. Do not expose Flask's development server directly to the public internet. For temporary public previews, use ngrok to tunnel to the Waitress listener; for a permanent hospital deployment, use a cloud host, persistent database storage, HTTPS, and access controls.

## Browser notifications

Email digests are sent by the running app independently of whether the branch head is signed in or has the dashboard open. The optional bell notification is browser-only: while signed in as a branch head, select the bell and allow notifications when prompted. The browser checks for expired medicines and medicines expiring within 15 days when the dashboard loads, once per hour while open, and when returning to the tab. On opening the branch dashboard, supported browsers speak the name, batch number, and expiry timing of each expired medicine and each medicine due within 15 days; the **Read alerts** button replays the summary. Browser autoplay policies may block speech until the user interacts with the page, so the replay button is always available. Browser notification permission is per device, and notifications are not delivered while the browser is closed. Browsers generally require `localhost` or HTTPS for notification permissions.

## Tests

```powershell
pip install pytest
pytest
```

Tests use a temporary SQLite database and do not modify the sample database.

## API overview

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/medicines?q=...&status=...&low_stock=true` | List/search/filter medicines (branch head) |
| `POST` | `/api/medicines` | Add a medicine (branch head) |
| `PUT` | `/api/medicines/<id>` | Update a medicine (branch head) |
| `DELETE` | `/api/medicines/<id>` | Delete a medicine (branch head) |
| `GET` | `/api/stats` | Branch dashboard statistics |
| `GET` | `/api/export.csv` | Download branch expiry-alert report |
| `GET` | `/api/auth/setup-status` | Get setup status and public branch choices |
| `POST` | `/api/auth/setup` | One-time main administrator creation |
| `POST` | `/api/auth/login` | Sign in as main administrator or branch head |
| `GET` | `/api/me` | Get the signed-in account |
| `GET` | `/api/admin/branches` | List branches and branch-head emails (main administrator) |
| `POST` | `/api/admin/branch-access` | Create a branch or grant/reset branch-head credentials |
| `POST` | `/api/auth/logout` | Sign out |

Setup accepts `email` and `password`. Branch access accepts `branch_name` for a new branch or `branch_id` for an existing branch, plus the branch head's `email` and `password`. Login accepts `email`, `password`, and `role` (`main_admin` or `branch_admin`); branch-head logins also include `branch_id`. Medicine JSON fields are `name`, `batch_number`, optional `manufacture_date` (`YYYY-MM-DD`), non-negative integer `quantity`, and `expiry_date` (`YYYY-MM-DD`). Passwords are stored as hashes and must be at least 12 characters. Branch-only endpoints enforce the signed-in account's branch on the server.
