import csv
import io
import sqlite3
from datetime import date, timedelta

import pytest

import app as medicine_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    database = tmp_path / "test-medicines.db"
    monkeypatch.setattr(medicine_app, "DATABASE", database)
    medicine_app.app.config.update(TESTING=True, SECRET_KEY="test-only-secret-key")
    medicine_app.initialize_database()
    with medicine_app.app.test_client() as test_client:
        setup = test_client.post(
            "/api/auth/setup",
            json={"email": "main-admin@example.org", "password": "main-admin-test-password"},
        )
        assert setup.status_code == 201
        assert test_client.post(
            "/api/admin/branch-access",
            json={
                "branch_id": 1,
                "email": "main-head@example.org",
                "password": "main-branch-test-password",
            },
        ).status_code == 201
        assert test_client.post(
            "/api/admin/branch-access",
            json={
                "branch_name": "North Hospital",
                "email": "north-head@example.org",
                "password": "north-branch-test-password",
            },
        ).status_code == 201
        test_client.post("/api/auth/logout")
        assert test_client.post(
            "/api/auth/login",
            json={
                "email": "main-head@example.org",
                "password": "main-branch-test-password",
                "role": "branch_admin",
                "branch_id": 1,
            },
        ).status_code == 200
        yield test_client


def payload(**overrides):
    medicine = {
        "name": "Test medicine",
        "batch_number": "TEST-001",
        "quantity": 20,
        "manufacture_date": (date.today() - timedelta(days=100)).isoformat(),
        "expiry_date": (date.today() + timedelta(days=40)).isoformat(),
    }
    medicine.update(overrides)
    return medicine


def test_first_run_seeds_realistic_inventory(client):
    response = client.get("/api/medicines")
    assert response.status_code == 200
    medicines = response.get_json()
    assert len(medicines) >= 5
    assert all(medicine["manufacture_date"] for medicine in medicines)
    assert {"expired", "urgent", "soon", "upcoming", "safe"} <= {
        medicine["status"] for medicine in medicines
    }
    expired = next(medicine for medicine in medicines if medicine["status"] == "expired")
    assert expired["availability"] == "expired_stock"


def test_setup_login_and_role_protection(client):
    assert client.get("/api/me").get_json()["user"]["role"] == "branch_admin"
    with medicine_app.get_db() as connection:
        users = connection.execute("SELECT username, role, password_hash FROM users").fetchall()
    assert {user["role"] for user in users} == {"branch_admin", "main_admin"}
    assert all(user["password_hash"] != "main-branch-test-password" for user in users)
    assert client.get("/api/admin/branches").status_code == 403

    client.post("/api/auth/logout")
    assert client.get("/api/medicines").status_code == 401
    login = client.post(
        "/api/auth/login",
        json={
            "email": "main-head@example.org",
            "password": "wrong-password",
            "role": "branch_admin",
            "branch_id": 1,
        },
    )
    assert login.status_code == 401

    system_login = client.post(
        "/api/auth/login",
        json={"email": "admin@example.org", "password": "system-admin-test-password", "role": "system_admin"},
    )
    assert system_login.status_code == 400


def test_branch_admins_are_isolated(client):
    client.post("/api/auth/logout")
    login = client.post(
        "/api/auth/login",
        json={
            "email": "north-head@example.org",
            "password": "north-branch-test-password",
            "role": "branch_admin",
            "branch_id": 2,
        },
    )
    assert login.status_code == 200
    north_medicines = client.get("/api/medicines").get_json()
    assert north_medicines == []
    created = client.post(
        "/api/medicines",
        json=payload(name="Paracetamol 500 mg", batch_number="PCM-24107"),
    )
    assert created.status_code == 201
    medicine_id = created.get_json()["id"]
    private_medicine = client.post("/api/medicines", json=payload(name="North-only medicine"))
    assert private_medicine.status_code == 201
    assert client.get("/api/stats").get_json()["total"] == 2
    north_csv = client.get("/api/export.csv").get_data(as_text=True)
    assert "North-only medicine" in north_csv
    assert north_csv.count("Paracetamol 500 mg") == 1

    client.post("/api/auth/logout")
    client.post(
        "/api/auth/login",
        json={
            "email": "main-head@example.org",
            "password": "main-branch-test-password",
            "role": "branch_admin",
            "branch_id": 1,
        },
    )
    assert all(item["name"] != "North-only medicine" for item in client.get("/api/medicines").get_json())
    assert client.get("/api/stats").get_json()["total"] > 2
    assert "North-only medicine" not in client.get("/api/export.csv").get_data(as_text=True)
    assert client.put(f"/api/medicines/{medicine_id}", json=payload()).status_code == 404
    assert client.delete(f"/api/medicines/{medicine_id}").status_code == 404


def test_setup_is_one_time_and_branch_login_requires_matching_branch(client):
    setup_again = client.post(
        "/api/auth/setup",
        json={"email": "another-admin@example.org", "password": "another-admin-password"},
    )
    assert setup_again.status_code == 409
    client.post("/api/auth/logout")
    mismatch = client.post(
        "/api/auth/login",
        json={
            "email": "main-head@example.org",
            "password": "main-branch-test-password",
            "role": "branch_admin",
            "branch_id": 999,
        },
    )
    assert mismatch.status_code == 401


def test_setup_and_branch_access_validate_credentials(client):
    client.post("/api/auth/logout")
    assert client.post(
        "/api/auth/login",
        json={
            "email": "main-admin@example.org",
            "password": "main-admin-test-password",
            "role": "main_admin",
        },
    ).status_code == 200
    assert client.post(
        "/api/admin/branch-access",
        json={"branch_name": "East Hospital", "email": "bad-email", "password": "a-long-enough-password"},
    ).status_code == 400
    assert client.post(
        "/api/admin/branch-access",
        json={"branch_name": "East Hospital", "email": "east@example.org", "password": "short"},
    ).status_code == 400
    assert client.post(
        "/api/admin/branch-access",
        json={"branch_name": "Main Hospital", "email": "east@example.org", "password": "east-branch-password"},
    ).status_code == 409
    assert client.post(
        "/api/auth/setup",
        json={"email": "invalid", "password": "a-long-enough-password"},
    ).status_code == 400


def test_main_admin_provisions_and_branch_head_signs_in(tmp_path, monkeypatch):
    database = tmp_path / "custom-hospital-name.db"
    monkeypatch.setattr(medicine_app, "DATABASE", database)
    medicine_app.app.config.update(TESTING=True, SECRET_KEY="test-only-secret-key")
    medicine_app.initialize_database()
    with medicine_app.app.test_client() as test_client:
        setup = test_client.post(
            "/api/auth/setup",
            json={"email": "central-admin@example.org", "password": "central-main-admin-password"},
        )
        assert setup.status_code == 201
        assert setup.get_json()["user"]["role"] == "main_admin"
        provisioned = test_client.post(
            "/api/admin/branch-access",
            json={
                "branch_name": "Central Hospital",
                "email": "central-head@example.org",
                "password": "central-branch-admin-password",
            },
        )
        assert provisioned.status_code == 201
        assert test_client.get("/api/admin/branches").get_json()["branches"][0]["name"] == "Central Hospital"
        branch_id = provisioned.get_json()["branch"]["id"]
        test_client.post("/api/auth/logout")
        login = test_client.post(
            "/api/auth/login",
            json={
                "email": "central-head@example.org",
                "password": "central-branch-admin-password",
                "role": "branch_admin",
                "branch_id": branch_id,
            },
        )
        assert login.status_code == 200
        assert login.get_json()["user"]["branch_name"] == "Central Hospital"


def test_create_update_search_and_filter_medicine(client):
    created = client.post("/api/medicines", json=payload())
    assert created.status_code == 201
    medicine_id = created.get_json()["id"]
    assert created.get_json()["status"] == "upcoming"
    assert created.get_json()["manufacture_date"] == payload()["manufacture_date"]
    assert created.get_json()["availability"] == "in_stock"

    updated = client.put(
        f"/api/medicines/{medicine_id}",
        json=payload(name="Updated medicine", quantity=0),
    )
    assert updated.status_code == 200
    assert updated.get_json()["low_stock"] is True
    assert updated.get_json()["availability"] == "out_of_stock"

    searched = client.get("/api/medicines?q=Updated")
    assert [item["id"] for item in searched.get_json()] == [medicine_id]
    manufactured = client.get(f"/api/medicines?q={payload()['manufacture_date']}")
    assert [item["id"] for item in manufactured.get_json()] == [medicine_id]
    low_stock = client.get("/api/medicines?low_stock=true")
    assert medicine_id in [item["id"] for item in low_stock.get_json()]


def test_filter_medicines_expiring_within_15_days(client):
    within_id = client.post(
        "/api/medicines",
        json=payload(
            name="15-day filter medicine",
            batch_number="FIFTEEN-DAY",
            expiry_date=(date.today() + timedelta(days=15)).isoformat(),
        ),
    ).get_json()["id"]
    outside_id = client.post(
        "/api/medicines",
        json=payload(
            name="16-day filter medicine",
            batch_number="SIXTEEN-DAY",
            expiry_date=(date.today() + timedelta(days=16)).isoformat(),
        ),
    ).get_json()["id"]
    expired_id = client.post(
        "/api/medicines",
        json=payload(
            name="Expired filter medicine",
            batch_number="EXPIRED-FILTER",
            expiry_date=(date.today() - timedelta(days=1)).isoformat(),
        ),
    ).get_json()["id"]

    filtered_ids = {
        medicine["id"]
        for medicine in client.get("/api/medicines?status=expiring_15_days").get_json()
    }

    assert within_id in filtered_ids
    assert outside_id not in filtered_ids
    assert expired_id not in filtered_ids


def test_validates_invalid_and_duplicate_records(client):
    assert client.post("/api/medicines", json=payload(quantity=-1)).status_code == 400
    assert client.post("/api/medicines", json=payload(expiry_date="not-a-date")).status_code == 400
    assert client.post(
        "/api/medicines",
        json=payload(manufacture_date=payload()["expiry_date"]),
    ).status_code == 400
    assert client.post("/api/medicines", json=payload(manufacture_date="not-a-date")).status_code == 400
    assert client.post(
        "/api/medicines",
        json=payload(manufacture_date=(date.today() + timedelta(days=1)).isoformat()),
    ).status_code == 400
    assert client.post("/api/medicines", json=payload()).status_code == 201
    duplicate = client.post("/api/medicines", json=payload())
    assert duplicate.status_code == 409
    assert "already exists" in duplicate.get_json()["error"]


def test_delete_and_not_found_responses(client):
    medicine_id = client.post("/api/medicines", json=payload()).get_json()["id"]
    assert client.delete(f"/api/medicines/{medicine_id}").status_code == 200
    assert client.delete(f"/api/medicines/{medicine_id}").status_code == 404
    assert client.put("/api/medicines/99999", json=payload()).status_code == 404


def test_stats_and_csv_export(client):
    stats = client.get("/api/stats").get_json()
    assert stats["total"] >= 5
    assert stats["expired"] >= 1
    assert stats["low_stock"] >= 1
    assert stats["out_of_stock"] == 0
    assert stats["expired_stock"] >= 1

    response = client.get("/api/export.csv")
    assert response.status_code == 200
    assert "text/csv" in response.content_type
    data = list(csv.reader(io.StringIO(response.get_data(as_text=True))))
    assert data[0] == [
        "Medicine", "Batch number", "Manufacturing date", "Expiry date",
        "Quantity", "Availability", "Expiry status", "Days remaining",
    ]
    assert len(data) == stats["total"] + 1


def test_initialize_database_migrates_existing_records(tmp_path, monkeypatch):
    database = tmp_path / "existing-medicines.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE branches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            "INSERT INTO branches (id, name) VALUES (1, 'Main Hospital')"
        )
        connection.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('system_admin', 'branch_admin')),
                branch_id INTEGER REFERENCES branches(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (
                    (role = 'system_admin' AND branch_id IS NULL)
                    OR (role = 'branch_admin' AND branch_id IS NOT NULL)
                )
            )
            """
        )
        connection.execute(
            "CREATE UNIQUE INDEX one_admin_per_branch ON users(branch_id) "
            "WHERE role = 'branch_admin'"
        )
        connection.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'system_admin')",
            ("old-system-admin@example.org", "old-system-admin-hash"),
        )
        connection.execute(
            """
            INSERT INTO users (username, password_hash, role, branch_id)
            VALUES (?, ?, 'branch_admin', 1)
            """,
            ("preserved-branch-admin@example.org", medicine_app.generate_password_hash("branch-password")),
        )
        connection.execute(
            """
            CREATE TABLE medicines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                batch_number TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                expiry_date TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(name, batch_number)
            )
            """
        )
        connection.execute(
            "INSERT INTO medicines (name, batch_number, quantity, expiry_date) "
            "VALUES (?, ?, ?, ?)",
            ("Existing medicine", "OLD-001", 4, (date.today() + timedelta(days=12)).isoformat()),
        )
    monkeypatch.setattr(medicine_app, "DATABASE", database)

    medicine_app.initialize_database()
    with medicine_app.app.test_client() as test_client:
        assert test_client.get("/api/auth/setup-status").get_json()["needs_setup"] is False
        assert test_client.post(
            "/api/auth/login",
            json={
                "email": "preserved-branch-admin@example.org",
                "password": "branch-password",
                "role": "branch_admin",
                "branch_id": 1,
            },
        ).status_code == 200
        medicines = test_client.get("/api/medicines").get_json()
        with sqlite3.connect(database) as connection:
            usernames = [row[0] for row in connection.execute("SELECT username FROM users ORDER BY role")]
            roles = [row[0] for row in connection.execute("SELECT role FROM users ORDER BY role")]

    assert len(medicines) == 1
    assert medicines[0]["name"] == "Existing medicine"
    assert medicines[0]["manufacture_date"] is None
    assert medicines[0]["availability"] == "in_stock"
    assert usernames == ["preserved-branch-admin@example.org", "old-system-admin@example.org"]
    assert roles == ["branch_admin", "main_admin"]


def test_expiry_status_boundaries():
    today = date(2030, 1, 1)
    assert medicine_app.expiry_status("2029-12-31", today)[0] == "expired"
    assert medicine_app.expiry_status("2030-01-08", today) == ("urgent", 7)
    medicine = {"expiry_date": "2030-01-16", "quantity": 4}
    assert medicine_app.serialize_medicine(medicine, today)["notification_due"] is True
    medicine["expiry_date"] = "2030-01-17"
    assert medicine_app.serialize_medicine(medicine, today)["notification_due"] is False
    medicine["expiry_date"] = "2029-12-31"
    assert medicine_app.serialize_medicine(medicine, today)["notification_due"] is True
    assert medicine_app.expiry_status("2030-01-31", today) == ("soon", 30)
    assert medicine_app.expiry_status("2030-04-01", today) == ("upcoming", 90)
    assert medicine_app.expiry_status("2030-04-02", today)[0] == "safe"


def test_expiry_email_digest_targets_branch_head_and_sends_once(client, monkeypatch):
    today = date.today()
    monkeypatch.setattr(medicine_app, "send_expiry_email", lambda *args: sent_emails.append(args))
    sent_emails = []

    sent_count = medicine_app.send_pending_expiry_emails(today)

    assert sent_count > 0
    assert len(sent_emails) == 1
    recipient, branch_name, medicines = sent_emails[0]
    assert recipient == "main-head@example.org"
    assert branch_name == "Main Hospital"
    assert all(medicine["days_remaining"] <= 15 for medicine in medicines)
    assert medicine_app.send_pending_expiry_emails(today) == 0
    assert len(sent_emails) == 1


def test_expiry_email_uses_configured_smtp(monkeypatch):
    smtp_calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            smtp_calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, context):
            smtp_calls.append(("tls", context))

        def login(self, username, password):
            smtp_calls.append(("login", username, password))

        def send_message(self, message):
            smtp_calls.append(("message", message))

    monkeypatch.setattr(medicine_app.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.org")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USERNAME", "smtp-user")
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-secret")
    monkeypatch.setenv("SMTP_FROM", "alerts@example.org")
    monkeypatch.setenv("SMTP_USE_TLS", "1")
    monkeypatch.delenv("SMTP_USE_SSL", raising=False)

    medicine_app.send_expiry_email(
        "head@example.org",
        "North Hospital",
        [{
            "name": "Test medicine",
            "batch_number": "BATCH-001",
            "expiry_date": "2030-01-10",
            "quantity": 8,
            "days_remaining": 3,
        }],
    )

    email = smtp_calls[-1][1]
    assert smtp_calls[0] == ("connect", "smtp.example.org", 587, 20)
    assert ("login", "smtp-user", "smtp-secret") in smtp_calls
    assert email["To"] == "head@example.org"
    assert "Test medicine" in email.get_content()
    assert "expires in 3 days" in email.get_content()
