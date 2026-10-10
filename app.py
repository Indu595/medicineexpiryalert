import csv
import functools
import hashlib
import io
import logging
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote, urlsplit

from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DATABASE = Path(os.environ.get("MEDICINE_DB", BASE_DIR / "medicines.db"))
LOW_STOCK_THRESHOLD = 10
EXPIRY_EMAIL_CHECK_INTERVAL_SECONDS = 60 * 60
logger = logging.getLogger(__name__)
expiry_email_check_lock = threading.Lock()

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE") == "1"
app.config["PUBLIC_BASE_URL"] = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")


@contextmanager
def get_db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def login_required(role=None):
    def decorate(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                return jsonify({"error": "Please sign in to continue."}), 401
            if role and session.get("role") != role:
                return jsonify({"error": "You are not authorized to access this resource."}), 403
            return view(*args, **kwargs)

        return wrapped

    return decorate


def valid_email(value):
    return (
        isinstance(value, str)
        and len(value) <= 254
        and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value.strip()) is not None
    )


def reset_main_admin_password(email, password):
    if not valid_email(email):
        raise ValueError("Enter a valid main administrator email address.")
    if not isinstance(password, str) or len(password) < 12:
        raise ValueError("Main administrator password must be at least 12 characters.")

    with get_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            UPDATE users
            SET password_hash = ?
            WHERE username = ? AND role = 'main_admin'
            """,
            (generate_password_hash(password), email.strip().lower()),
        )
        if cursor.rowcount == 0:
            raise LookupError("No main administrator account exists with that email.")


def expiry_status(expiry_date, today=None):
    today = today or date.today()
    days_remaining = (date.fromisoformat(expiry_date) - today).days
    if days_remaining < 0:
        status = "expired"
    elif days_remaining <= 7:
        status = "urgent"
    elif days_remaining <= 30:
        status = "soon"
    elif days_remaining <= 90:
        status = "upcoming"
    else:
        status = "safe"
    return status, days_remaining


def serialize_medicine(row, today=None):
    medicine = dict(row)
    status, days_remaining = expiry_status(medicine["expiry_date"], today)
    medicine["status"] = status
    medicine["days_remaining"] = days_remaining
    medicine["notification_due"] = status == "expired" or 0 <= days_remaining <= 15
    medicine["low_stock"] = medicine["quantity"] <= LOW_STOCK_THRESHOLD
    if medicine["quantity"] == 0:
        medicine["availability"] = "out_of_stock"
    elif status == "expired":
        medicine["availability"] = "expired_stock"
    else:
        medicine["availability"] = "in_stock"
    return medicine


def send_expiry_email(recipient, branch_name, medicines):
    message = EmailMessage()
    message["Subject"] = f"Medicine expiry alert: {branch_name}"
    message["To"] = recipient
    lines = [
        f"Expiry alert for {branch_name}",
        "",
        "The following medicine batches are expired or will expire within 15 days:",
        "",
    ]
    for medicine in medicines:
        days_remaining = medicine["days_remaining"]
        status = "already expired" if days_remaining < 0 else (
            "expires today" if days_remaining == 0 else
            f"expires in {days_remaining} day{'s' if days_remaining != 1 else ''}"
        )
        lines.append(
            f"- {medicine['name']} (batch {medicine['batch_number']}): "
            f"{status}; expiry date {medicine['expiry_date']}; "
            f"quantity {medicine['quantity']}."
        )
    lines.extend(["", "Please review these medicines and take appropriate action."])
    message.set_content("\n".join(lines))

    send_email_message(message)


def send_email_message(message):
    host = os.environ.get("SMTP_HOST")
    sender = os.environ.get("SMTP_FROM")
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    if not host or not sender:
        raise RuntimeError("SMTP_HOST and SMTP_FROM must be configured to send emails.")
    if bool(username) != bool(password):
        raise RuntimeError("Configure both SMTP_USERNAME and SMTP_PASSWORD, or neither.")
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
    except ValueError as error:
        raise RuntimeError("SMTP_PORT must be a valid port number.") from error

    message["From"] = sender
    use_ssl = os.environ.get("SMTP_USE_SSL") == "1"
    use_starttls = os.environ.get("SMTP_USE_TLS", "1") == "1"
    smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_class(host, port, timeout=20) as server:
        if not use_ssl and use_starttls:
            server.starttls(context=ssl.create_default_context())
        if username and password:
            server.login(username, password)
        server.send_message(message)


def send_pending_expiry_emails(today=None):
    today = today or date.today()
    cutoff = today + timedelta(days=15)
    with get_db() as connection:
        rows = connection.execute(
            """
            SELECT medicines.id, medicines.branch_id, medicines.name,
                   medicines.batch_number, medicines.quantity, medicines.expiry_date,
                   branches.name AS branch_name, users.username AS head_email
            FROM medicines
            JOIN branches ON branches.id = medicines.branch_id
            JOIN users ON users.branch_id = medicines.branch_id
                       AND users.role = 'branch_admin'
            LEFT JOIN expiry_email_notifications AS sent
                   ON sent.medicine_id = medicines.id
                  AND sent.expiry_date = medicines.expiry_date
            WHERE medicines.expiry_date <= ?
              AND sent.medicine_id IS NULL
            ORDER BY medicines.branch_id, medicines.expiry_date, medicines.name
            """,
            (cutoff.isoformat(),),
        ).fetchall()

    by_branch = {}
    for row in rows:
        medicine = dict(row)
        status, days_remaining = expiry_status(medicine["expiry_date"], today)
        medicine["status"] = status
        medicine["days_remaining"] = days_remaining
        group = by_branch.setdefault(
            medicine["branch_id"],
            {
                "branch_name": medicine["branch_name"],
                "head_email": medicine["head_email"],
                "medicines": [],
            },
        )
        group["medicines"].append(medicine)

    sent_count = 0
    for branch_id, alert in by_branch.items():
        try:
            send_expiry_email(
                alert["head_email"], alert["branch_name"], alert["medicines"]
            )
        except (OSError, ValueError, smtplib.SMTPException, RuntimeError):
            logger.exception(
                "Could not email expiry alerts for branch %s to %s",
                branch_id,
                alert["head_email"],
            )
            continue

        with get_db() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO expiry_email_notifications
                    (medicine_id, expiry_date, sent_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                """,
                [(medicine["id"], medicine["expiry_date"]) for medicine in alert["medicines"]],
            )
        sent_count += len(alert["medicines"])
        logger.info(
            "Sent %d expiry alerts to branch head for branch %s",
            len(alert["medicines"]),
            branch_id,
        )
    return sent_count


def check_pending_expiry_emails():
    with expiry_email_check_lock:
        try:
            send_pending_expiry_emails()
        except (OSError, sqlite3.Error):
            logger.exception("Expiry email check failed.")


def trigger_pending_expiry_email_check():
    if not os.environ.get("SMTP_HOST") or not os.environ.get("SMTP_FROM"):
        logger.warning(
            "Immediate expiry email alert was not sent: configure SMTP_HOST and "
            "SMTP_FROM in .env, then restart."
        )
        return
    threading.Thread(
        target=check_pending_expiry_emails,
        name="immediate-expiry-email-check",
        daemon=True,
    ).start()


def run_expiry_email_scheduler():
    if not os.environ.get("SMTP_HOST") or not os.environ.get("SMTP_FROM"):
        logger.warning(
            "Expiry email alerts are disabled: configure SMTP_HOST and SMTP_FROM in .env, then restart."
        )
        return
    while True:
        check_pending_expiry_emails()
        threading.Event().wait(EXPIRY_EMAIL_CHECK_INTERVAL_SECONDS)


def initialize_database():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS branches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        users_table = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        ).fetchone()
        if users_table and "main_admin" not in users_table["sql"]:
            connection.execute("DROP INDEX IF EXISTS one_admin_per_branch")
            connection.execute("ALTER TABLE users RENAME TO users_before_branch_only")
            connection.execute(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('main_admin', 'branch_admin')),
                    branch_id INTEGER REFERENCES branches(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK (
                        (role = 'main_admin' AND branch_id IS NULL)
                        OR (role = 'branch_admin' AND branch_id IS NOT NULL)
                    )
                )
                """
            )
            connection.execute(
                """
                INSERT INTO users (id, username, password_hash, role, branch_id, created_at)
                SELECT id, username, password_hash,
                       CASE WHEN role = 'system_admin' THEN 'main_admin' ELSE 'branch_admin' END,
                       branch_id, created_at
                FROM users_before_branch_only
                WHERE (role = 'branch_admin' AND branch_id IS NOT NULL)
                   OR (role IN ('system_admin', 'main_admin') AND branch_id IS NULL)
                """
            )
            connection.execute("DROP TABLE users_before_branch_only")
        else:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('main_admin', 'branch_admin')),
                    branch_id INTEGER REFERENCES branches(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK (
                        (role = 'main_admin' AND branch_id IS NULL)
                        OR (role = 'branch_admin' AND branch_id IS NOT NULL)
                    )
                )
                """
            )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_branch_head_per_branch "
            "ON users(branch_id) WHERE role = 'branch_admin'"
        )
        if connection.execute("SELECT COUNT(*) FROM branches").fetchone()[0] == 0:
            connection.execute("INSERT INTO branches (name) VALUES (?)", ("Main Hospital",))
        default_branch_id = connection.execute(
            "SELECT id FROM branches ORDER BY id LIMIT 1"
        ).fetchone()["id"]
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS medicines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                batch_number TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity >= 0),
                manufacture_date TEXT,
                expiry_date TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(branch_id, name, batch_number)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS expiry_email_notifications (
                medicine_id INTEGER NOT NULL REFERENCES medicines(id) ON DELETE CASCADE,
                expiry_date TEXT NOT NULL,
                sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (medicine_id, expiry_date)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL
            )
            """
        )
        columns = {
            column["name"]
            for column in connection.execute("PRAGMA table_info(medicines)").fetchall()
        }
        if "branch_id" not in columns:
            connection.execute("ALTER TABLE medicines RENAME TO medicines_before_branches")
            connection.execute(
                """
                CREATE TABLE medicines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    batch_number TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity >= 0),
                    manufacture_date TEXT,
                    expiry_date TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(branch_id, name, batch_number)
                )
                """
            )
            old_columns = {
                column["name"]
                for column in connection.execute(
                    "PRAGMA table_info(medicines_before_branches)"
                ).fetchall()
            }
            manufacture_column = "manufacture_date" if "manufacture_date" in old_columns else "NULL"
            connection.execute(
                f"""
                INSERT INTO medicines
                    (id, branch_id, name, batch_number, quantity, manufacture_date,
                     expiry_date, created_at, updated_at)
                SELECT id, ?, name, batch_number, quantity, {manufacture_column},
                       expiry_date, created_at, updated_at
                FROM medicines_before_branches
                """,
                (default_branch_id,),
            )
            connection.execute("DROP TABLE medicines_before_branches")
        if connection.execute("SELECT COUNT(*) FROM medicines").fetchone()[0] == 0:
            today = date.today()
            samples = [
                ("Amoxicillin 500 mg", "AMX-24018", 8, today - timedelta(days=180), today + timedelta(days=5)),
                ("Paracetamol 500 mg", "PCM-24107", 124, today - timedelta(days=120), today + timedelta(days=24)),
                ("Cetirizine 10 mg", "CTZ-23092", 42, today - timedelta(days=240), today + timedelta(days=68)),
                ("Omeprazole 20 mg", "OMP-24044", 6, today - timedelta(days=80), today + timedelta(days=130)),
                ("Vitamin C 500 mg", "VTC-23156", 73, today - timedelta(days=400), today - timedelta(days=12)),
                ("Ibuprofen 200 mg", "IBU-24063", 18, today - timedelta(days=150), today + timedelta(days=9)),
                ("Metformin 500 mg", "MET-24031", 95, today - timedelta(days=60), today + timedelta(days=210)),
            ]
            connection.executemany(
                "INSERT INTO medicines "
                "(branch_id, name, batch_number, quantity, manufacture_date, expiry_date) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (default_branch_id, name, batch, quantity, manufacture.isoformat(), expiry.isoformat())
                    for name, batch, quantity, manufacture, expiry in samples
                ],
            )


def validate_medicine(payload):
    if not isinstance(payload, dict):
        return None, "Request body must be a JSON object."
    name = payload.get("name")
    batch_number = payload.get("batch_number")
    manufacture_date = payload.get("manufacture_date") or None
    expiry_date = payload.get("expiry_date")
    quantity = payload.get("quantity")
    if not isinstance(name, str) or not name.strip():
        return None, "Medicine name is required."
    if len(name.strip()) > 120:
        return None, "Medicine name must be 120 characters or fewer."
    if not isinstance(batch_number, str) or not batch_number.strip():
        return None, "Batch number is required."
    if len(batch_number.strip()) > 80:
        return None, "Batch number must be 80 characters or fewer."
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
        return None, "Quantity must be a whole number of zero or more."
    try:
        parsed_expiry = date.fromisoformat(expiry_date)
    except (TypeError, ValueError):
        return None, "Enter a valid expiry date."
    if manufacture_date is not None:
        try:
            parsed_manufacture = date.fromisoformat(manufacture_date)
        except (TypeError, ValueError):
            return None, "Enter a valid manufacturing date."
        if parsed_manufacture > date.today():
            return None, "Manufacturing date cannot be in the future."
        if parsed_manufacture >= parsed_expiry:
            return None, "Manufacturing date must be before the expiry date."
        manufacture_date = parsed_manufacture.isoformat()
    return {
        "name": name.strip(),
        "batch_number": batch_number.strip(),
        "quantity": quantity,
        "manufacture_date": manufacture_date,
        "expiry_date": parsed_expiry.isoformat(),
    }, None


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/auth/setup-status")
def setup_status():
    with get_db() as connection:
        needs_setup = (
            connection.execute(
                "SELECT 1 FROM users WHERE role = 'main_admin' LIMIT 1"
            ).fetchone()
            is None
        )
        branches = connection.execute(
            "SELECT id, name FROM branches ORDER BY id"
        ).fetchall()
    return jsonify(
        {
            "needs_setup": needs_setup,
            "branches": [dict(branch) for branch in branches],
        }
    )


@app.post("/api/auth/setup")
def setup_main_admin():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    email = payload.get("email")
    password = payload.get("password")
    if not valid_email(email):
        return jsonify({"error": "Enter a valid main administrator email address."}), 400
    email = email.strip().lower()
    if not isinstance(password, str) or len(password) < 12:
        return jsonify({"error": "Main administrator password must be at least 12 characters."}), 400
    try:
        with get_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM users WHERE role = 'main_admin' LIMIT 1"
            ).fetchone():
                return jsonify({"error": "Initial setup has already been completed."}), 409
            cursor = connection.execute(
                """
                INSERT INTO users (username, password_hash, role, branch_id)
                VALUES (?, ?, 'main_admin', NULL)
                """,
                (email, generate_password_hash(password)),
            )
            user_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        return jsonify({"error": "That email address is already in use."}), 409
    session.clear()
    session.update(
        {
            "user_id": user_id,
            "email": email,
            "role": "main_admin",
            "branch_id": None,
            "branch_name": None,
        }
    )
    return jsonify({"user": current_user(), "message": "Main administrator is ready."}), 201


@app.post("/api/auth/login")
def login():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    email = payload.get("email")
    password = payload.get("password")
    branch_id = payload.get("branch_id")
    if not valid_email(email):
        return jsonify({"error": "Enter a valid email address."}), 400
    if not isinstance(password, str) or not password:
        return jsonify({"error": "Password is required."}), 400
    role = payload.get("role")
    if role not in {"main_admin", "branch_admin"}:
        return jsonify({"error": "Select a valid account type."}), 400
    with get_db() as connection:
        if role == "branch_admin":
            try:
                parsed_branch_id = int(branch_id)
            except (TypeError, ValueError):
                return jsonify({"error": "Select your hospital branch."}), 400
            user = connection.execute(
                """
                SELECT users.*, branches.name AS branch_name
                FROM users JOIN branches ON branches.id = users.branch_id
                WHERE users.username = ? AND users.branch_id = ?
                  AND users.role = 'branch_admin'
                """,
                (email.strip().lower(), parsed_branch_id),
            ).fetchone()
        else:
            user = connection.execute(
                "SELECT * FROM users WHERE username = ? AND role = 'main_admin'",
                (email.strip().lower(),),
            ).fetchone()
    if user is None or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Incorrect email, password, account type, or branch."}), 401
    session.clear()
    session.update(
        {
            "user_id": user["id"],
            "email": user["username"],
            "role": user["role"],
            "branch_id": user["branch_id"],
            "branch_name": user["branch_name"] if user["role"] == "branch_admin" else None,
        }
    )
    return jsonify({"user": current_user()})


@app.post("/api/auth/password-reset/request")
def request_main_admin_password_reset():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    email = payload.get("email")
    if not valid_email(email):
        return jsonify({"error": "Enter a valid email address."}), 400

    public_base_url = app.config["PUBLIC_BASE_URL"]
    try:
        parsed_base_url = urlsplit(public_base_url)
        hostname = parsed_base_url.hostname
    except ValueError:
        parsed_base_url = None
        hostname = None
    if parsed_base_url is None:
        return jsonify(
            {"error": "Password recovery needs a valid HTTPS PUBLIC_BASE_URL setting."}
        ), 503
    local_http = parsed_base_url.scheme == "http" and hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }
    if (
        not parsed_base_url.netloc
        or parsed_base_url.username
        or parsed_base_url.password
        or parsed_base_url.query
        or parsed_base_url.fragment
        or (parsed_base_url.scheme != "https" and not local_http)
    ):
        return jsonify(
            {"error": "Password recovery needs a valid HTTPS PUBLIC_BASE_URL setting."}
        ), 503
    if not os.environ.get("SMTP_HOST") or not os.environ.get("SMTP_FROM"):
        return jsonify(
            {"error": "Password recovery email is not configured. Contact the app administrator."}
        ), 503

    with get_db() as connection:
        user = connection.execute(
            "SELECT id, username FROM users WHERE username = ? AND role = 'main_admin'",
            (email.strip().lower(),),
        ).fetchone()
        if user is None:
            return jsonify(
                {"message": "If a main administrator account exists for that email, a reset link has been sent."}
            )

        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        connection.execute(
            """
            INSERT INTO password_reset_tokens (user_id, token_hash, expires_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                token_hash = excluded.token_hash,
                expires_at = excluded.expires_at
            """,
            (user["id"], token_hash, expires_at),
        )

    reset_url = f"{public_base_url}/?reset_token={quote(token, safe='')}"
    message = EmailMessage()
    message["To"] = user["username"]
    message["Subject"] = "Reset your ExpiryCare administrator password"
    message.set_content(
        "A password reset was requested for your ExpiryCare main administrator account.\n\n"
        f"Use this one-time link within 30 minutes:\n{reset_url}\n\n"
        "If you did not request this reset, you can ignore this email."
    )
    try:
        send_email_message(message)
    except (OSError, RuntimeError, smtplib.SMTPException, ValueError):
        with get_db() as connection:
            connection.execute(
                "DELETE FROM password_reset_tokens WHERE user_id = ? AND token_hash = ?",
                (user["id"], token_hash),
            )
        logger.exception("Could not send a main administrator password reset email.")
        return jsonify(
            {"error": "The reset email could not be sent. Check the email settings or use local recovery."}
        ), 503

    return jsonify(
        {"message": "If a main administrator account exists for that email, a reset link has been sent."}
    )


@app.post("/api/auth/password-reset/confirm")
def reset_main_admin_password_with_token():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    token = payload.get("token")
    password = payload.get("password")
    if not isinstance(token, str) or not token:
        return jsonify({"error": "The reset link is invalid or has expired."}), 400
    if not isinstance(password, str) or len(password) < 12:
        return jsonify({"error": "Password must be at least 12 characters."}), 400

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        reset = connection.execute(
            """
            SELECT user_id FROM password_reset_tokens
            WHERE token_hash = ? AND expires_at > ?
            """,
            (token_hash, now),
        ).fetchone()
        if reset is None:
            return jsonify({"error": "The reset link is invalid or has expired."}), 400
        connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ? AND role = 'main_admin'",
            (generate_password_hash(password), reset["user_id"]),
        )
        connection.execute(
            "DELETE FROM password_reset_tokens WHERE user_id = ?", (reset["user_id"],)
        )
    return jsonify({"message": "Password reset successfully. Sign in with your new password."})


def current_user():
    return {
        "id": session.get("user_id"),
        "email": session.get("email"),
        "role": session.get("role"),
        "branch_id": session.get("branch_id"),
        "branch_name": session.get("branch_name"),
    }


@app.get("/api/me")
@login_required()
def get_current_user():
    return jsonify({"user": current_user()})


@app.get("/api/admin/branches")
@login_required(role="main_admin")
def admin_list_branches():
    with get_db() as connection:
        branches = connection.execute(
            """
            SELECT branches.id, branches.name, users.username AS head_email
            FROM branches
            LEFT JOIN users ON users.branch_id = branches.id
                          AND users.role = 'branch_admin'
            ORDER BY branches.name COLLATE NOCASE
            """
        ).fetchall()
    return jsonify({"branches": [dict(branch) for branch in branches]})


@app.post("/api/admin/branch-access")
@login_required(role="main_admin")
def grant_branch_access():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    email = payload.get("email")
    password = payload.get("password")
    if not valid_email(email):
        return jsonify({"error": "Enter a valid branch head email address."}), 400
    email = email.strip().lower()
    if not isinstance(password, str) or len(password) < 12:
        return jsonify({"error": "Branch head password must be at least 12 characters."}), 400

    branch_id = payload.get("branch_id")
    branch_name = payload.get("branch_name")
    try:
        with get_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if branch_id in (None, ""):
                if not isinstance(branch_name, str) or not branch_name.strip() or len(branch_name.strip()) > 120:
                    return jsonify({"error": "Enter a branch name of 1 to 120 characters."}), 400
                cursor = connection.execute(
                    "INSERT INTO branches (name) VALUES (?)", (branch_name.strip(),)
                )
                branch_id = cursor.lastrowid
            else:
                try:
                    branch_id = int(branch_id)
                except (TypeError, ValueError):
                    return jsonify({"error": "Select a valid hospital branch."}), 400
                if connection.execute(
                    "SELECT 1 FROM branches WHERE id = ?", (branch_id,)
                ).fetchone() is None:
                    return jsonify({"error": "Selected hospital branch does not exist."}), 404

            existing_head = connection.execute(
                "SELECT id FROM users WHERE branch_id = ? AND role = 'branch_admin'",
                (branch_id,),
            ).fetchone()
            if existing_head:
                connection.execute(
                    "UPDATE users SET username = ?, password_hash = ? WHERE id = ?",
                    (email, generate_password_hash(password), existing_head["id"]),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO users (username, password_hash, role, branch_id)
                    VALUES (?, ?, 'branch_admin', ?)
                    """,
                    (email, generate_password_hash(password), branch_id),
                )
            branch = connection.execute(
                "SELECT id, name FROM branches WHERE id = ?", (branch_id,)
            ).fetchone()
    except sqlite3.IntegrityError:
        return jsonify(
            {"error": "That branch name or email address is already assigned."}
        ), 409
    return jsonify(
        {"branch": {**dict(branch), "head_email": email},
         "message": "Branch head access has been saved."}
    ), 201


@app.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify({"message": "You have been signed out."})


@app.get("/api/medicines")
@login_required(role="branch_admin")
def list_medicines():
    search = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "").strip()
    low_stock = request.args.get("low_stock") == "true"
    with get_db() as connection:
        rows = connection.execute(
            """
            SELECT * FROM medicines
            WHERE branch_id = ?
              AND (? = '' OR name LIKE ? OR batch_number LIKE ?
                   OR COALESCE(manufacture_date, '') LIKE ? OR expiry_date LIKE ?)
            ORDER BY expiry_date ASC, name COLLATE NOCASE ASC
            """,
            (
                session["branch_id"], search, f"%{search}%", f"%{search}%",
                f"%{search}%", f"%{search}%",
            ),
        ).fetchall()
    medicines = [serialize_medicine(row) for row in rows]
    if status_filter == "expiring_15_days":
        medicines = [
            medicine
            for medicine in medicines
            if 0 <= medicine["days_remaining"] <= 15
        ]
    elif status_filter in {"expired", "urgent", "soon", "upcoming", "safe"}:
        medicines = [medicine for medicine in medicines if medicine["status"] == status_filter]
    if low_stock:
        medicines = [medicine for medicine in medicines if medicine["low_stock"]]
    return jsonify(medicines)


@app.get("/api/stats")
@login_required(role="branch_admin")
def dashboard_stats():
    with get_db() as connection:
        rows = connection.execute(
            "SELECT * FROM medicines WHERE branch_id = ?", (session["branch_id"],)
        ).fetchall()
    medicines = [serialize_medicine(row) for row in rows]
    return jsonify(
        {
            "total": len(medicines),
            "expired": sum(item["status"] == "expired" for item in medicines),
            "urgent": sum(item["status"] == "urgent" for item in medicines),
            "soon": sum(item["status"] == "soon" for item in medicines),
            "upcoming": sum(item["status"] == "upcoming" for item in medicines),
            "low_stock": sum(item["low_stock"] for item in medicines),
            "out_of_stock": sum(item["availability"] == "out_of_stock" for item in medicines),
            "expired_stock": sum(item["availability"] == "expired_stock" for item in medicines),
        }
    )


@app.post("/api/medicines")
@login_required(role="branch_admin")
def create_medicine():
    medicine, error = validate_medicine(request.get_json(silent=True))
    if error:
        return jsonify({"error": error}), 400
    try:
        with get_db() as connection:
            cursor = connection.execute(
                "INSERT INTO medicines "
                "(branch_id, name, batch_number, quantity, manufacture_date, expiry_date) "
                "VALUES (:branch_id, :name, :batch_number, :quantity, :manufacture_date, :expiry_date)",
                {**medicine, "branch_id": session["branch_id"]},
            )
            row = connection.execute(
                "SELECT * FROM medicines WHERE id = ? AND branch_id = ?",
                (cursor.lastrowid, session["branch_id"]),
            ).fetchone()
    except sqlite3.IntegrityError:
        return jsonify(
            {"error": "This medicine batch already exists in your branch."}
        ), 409
    result = serialize_medicine(row)
    if result["notification_due"]:
        trigger_pending_expiry_email_check()
    return jsonify(result), 201


@app.put("/api/medicines/<int:medicine_id>")
@login_required(role="branch_admin")
def update_medicine(medicine_id):
    medicine, error = validate_medicine(request.get_json(silent=True))
    if error:
        return jsonify({"error": error}), 400
    try:
        with get_db() as connection:
            cursor = connection.execute(
                """
                UPDATE medicines
                SET name = :name, batch_number = :batch_number,
                    quantity = :quantity, manufacture_date = :manufacture_date,
                    expiry_date = :expiry_date,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :id AND branch_id = :branch_id
                """,
                {**medicine, "id": medicine_id, "branch_id": session["branch_id"]},
            )
            if cursor.rowcount == 0:
                return jsonify({"error": "Medicine not found."}), 404
            row = connection.execute(
                "SELECT * FROM medicines WHERE id = ? AND branch_id = ?",
                (medicine_id, session["branch_id"]),
            ).fetchone()
    except sqlite3.IntegrityError:
        return jsonify(
            {"error": "This medicine batch already exists in your branch."}
        ), 409
    result = serialize_medicine(row)
    if result["notification_due"]:
        trigger_pending_expiry_email_check()
    return jsonify(result)


@app.delete("/api/medicines/<int:medicine_id>")
@login_required(role="branch_admin")
def delete_medicine(medicine_id):
    with get_db() as connection:
        cursor = connection.execute(
            "DELETE FROM medicines WHERE id = ? AND branch_id = ?",
            (medicine_id, session["branch_id"]),
        )
    if cursor.rowcount == 0:
        return jsonify({"error": "Medicine not found."}), 404
    return jsonify({"message": "Medicine deleted."})


@app.get("/api/export.csv")
@login_required(role="branch_admin")
def export_csv():
    with get_db() as connection:
        rows = connection.execute(
            "SELECT name, batch_number, quantity, manufacture_date, expiry_date FROM medicines "
            "WHERE branch_id = ? ORDER BY expiry_date ASC, name COLLATE NOCASE ASC",
            (session["branch_id"],),
        ).fetchall()
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "Medicine", "Batch number", "Manufacturing date", "Expiry date",
            "Quantity", "Availability", "Expiry status", "Days remaining",
        ]
    )
    for row in rows:
        medicine = serialize_medicine(row)
        writer.writerow(
            [
                medicine["name"],
                medicine["batch_number"],
                medicine["manufacture_date"] or "",
                medicine["expiry_date"],
                medicine["quantity"],
                medicine["availability"],
                medicine["status"],
                medicine["days_remaining"],
            ]
        )
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = 'attachment; filename="medicine-expiry-alerts.csv"'
    return response


initialize_database()


if __name__ == "__main__":
    from waitress import serve

    threading.Thread(
        target=run_expiry_email_scheduler,
        name="expiry-email-scheduler",
        daemon=True,
    ).start()
    serve(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
    )
