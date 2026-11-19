from datetime import date

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from rental_agent.web.app import create_app
from rental_agent.web.jobs import next_sync_date
from rental_agent.web.secrets import SecretStore

ORIGIN = {"origin": "http://testserver"}
SECRET = "sk-ant-super-secret-value-1234"


@pytest.fixture
def env(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("RENTAL_SECRET_KEY", key)
    monkeypatch.setenv("RENTAL_UI_PASSWORD", "pw")
    monkeypatch.setenv("RENTAL_UI_USERNAME", "owner@example.com")
    monkeypatch.setenv("RENTAL_SCHEDULER", "off")
    monkeypatch.setenv("RENTAL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RENTAL_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("RENTAL_REFERENCE_DIR", str(tmp_path / "reference"))
    monkeypatch.delenv("RENTAL_DB_URL", raising=False)
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "reference").mkdir()
    return tmp_path, key


@pytest.fixture
def client(env):
    with TestClient(create_app()) as c:
        c.post("/login", data={"email": "Owner@Example.com ", "password": "pw"}, headers=ORIGIN)
        yield c


def test_session_cookie_survives_the_return_from_google(env):
    """Google redirects back from another site; a strict cookie would be left out and the
    Gmail callback would land on the login page."""
    with TestClient(create_app()) as c:
        r = c.post("/login", data={"email": "owner@example.com", "password": "pw"},
                   headers=ORIGIN, follow_redirects=False)
        assert "samesite=lax" in r.headers["set-cookie"].lower()
        # Cross-site POSTs are still refused, with or without the cookie.
        assert c.post("/logout", headers={"origin": "https://evil.example"}).status_code == 403


def test_requires_login(env):
    with TestClient(create_app()) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        r = c.post("/login", data={"email": "owner@example.com", "password": "wrong"}, headers=ORIGIN)
        assert "Wrong email or password." in r.text
        assert c.get("/", follow_redirects=False).status_code == 303
        r = c.post("/login", data={"email": "someone@else.com", "password": "pw"}, headers=ORIGIN)
        assert "Wrong email or password." in r.text
        assert c.get("/", follow_redirects=False).status_code == 303
        c.post("/login", data={"email": "Owner@Example.com ", "password": "pw"}, headers=ORIGIN)
        assert c.get("/").status_code == 200


def test_cross_site_posts_are_blocked(client):
    assert client.post("/settings/secret", data={"name": "ANTHROPIC_API_KEY", "value": "x"}).status_code == 403
    r = client.post("/settings/secret", data={"name": "ANTHROPIC_API_KEY", "value": "x"},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_secret_saved_encrypted_and_never_shown(client, env):
    tmp_path, key = env
    client.post("/settings/secret", data={"name": "ANTHROPIC_API_KEY", "value": SECRET}, headers=ORIGIN)
    page = client.get("/settings").text
    assert SECRET not in page and "ends …1234" in page
    raw = (tmp_path / "data" / "secrets.enc").read_bytes()
    assert SECRET.encode() not in raw
    assert oct((tmp_path / "data" / "secrets.enc").stat().st_mode)[-3:] == "600"
    assert SecretStore(tmp_path / "data" / "secrets.enc", key).load()["ANTHROPIC_API_KEY"] == SECRET


def test_bad_gmail_json_rejected(client, env):
    tmp_path, key = env
    r = client.post("/settings/secret", data={"name": "GMAIL_CLIENT_CONFIG", "value": "not json"},
                    headers=ORIGIN)
    assert "Not saved" in r.text
    assert "GMAIL_CLIENT_CONFIG" not in SecretStore(tmp_path / "data" / "secrets.enc", key).load()


def test_unknown_secret_name_rejected(env):
    tmp_path, key = env
    with pytest.raises(ValueError):
        SecretStore(tmp_path / "s.enc", key).set("PATH", "/evil")


def test_wrong_key_cannot_read_secrets(env):
    tmp_path, key = env
    SecretStore(tmp_path / "s.enc", key).set("ANTHROPIC_API_KEY", SECRET)
    with pytest.raises(RuntimeError, match="changed"):
        SecretStore(tmp_path / "s.enc", Fernet.generate_key().decode()).load()


def test_pages_explain_missing_database(client):
    for path in ("/", "/rent", "/expenses", "/research", "/leases", "/jobs"):
        r = client.get(path)
        assert r.status_code == 200
    assert "RENTAL_DB_URL is not set" in client.get("/rent").text


def test_jobs_page_lists_runs_above_the_job_forms(client):
    page = client.get("/jobs").text
    assert 0 < page.index("Recent runs") < page.index('id="run-a-job"') < page.index("Run sync-email")
    assert 'href="/runs"' not in page and client.get("/runs").status_code == 404


def test_starting_a_job_without_database_is_a_clear_error(client):
    r = client.post("/jobs/tax-report", data={"year": "2025"}, headers=ORIGIN)
    assert "RENTAL_DB_URL is not set" in r.text


def test_bad_job_input_rejected(client):
    r = client.post("/jobs/generate-lease", data={"property_id": "", "rent": "-5"}, headers=ORIGIN)
    assert "Property ID is required" in r.text


def test_downloads_stay_inside_artifacts(client, env):
    tmp_path, _ = env
    (tmp_path / "secret.txt").write_text("nope")
    (tmp_path / "artifacts" / "ok.txt").write_text("fine")
    assert client.get("/files/ok.txt").text == "fine"
    assert client.get("/files/../secret.txt").status_code == 404
    assert client.get("/files/%2e%2e/secret.txt").status_code == 404


def test_downloads_fall_back_to_reference(client, env):
    tmp_path, _ = env
    (tmp_path / "reference" / "leases").mkdir()
    (tmp_path / "reference" / "leases" / "orig.pdf").write_text("original")
    (tmp_path / "artifacts" / "same.txt").write_text("artifact")
    (tmp_path / "reference" / "same.txt").write_text("reference")
    assert client.get("/files/leases/orig.pdf").text == "original"   # an original lease
    assert client.get("/files/same.txt").text == "artifact"          # artifacts/ wins


def test_pdfs_open_in_the_browser_other_files_download(client, env):
    tmp_path, _ = env
    (tmp_path / "artifacts" / "lease.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "artifacts" / "report.xlsx").write_bytes(b"x")
    pdf = client.get("/files/lease.pdf").headers
    assert pdf["content-disposition"].startswith("inline")
    assert pdf["content-security-policy"] == "frame-ancestors 'none'"   # lets the viewer run
    assert client.get("/files/report.xlsx").headers["content-disposition"].startswith("attachment")
    assert client.get("/files/../secret.txt").status_code == 404


def test_report_html_is_escaped(client, env):
    tmp_path, _ = env
    (tmp_path / "artifacts" / "r.md").write_text("# Report\n\n| a |\n|---|\n| <script>alert(1)</script> |\n")
    page = client.get("/report/r.md").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert "<table>" in page


def test_security_headers(client):
    r = client.get("/settings")
    assert "default-src 'none'" in r.headers["content-security-policy"]
    csp = r.headers["content-security-policy"]
    assert "script-src 'self';" in csp                    # only this app's own script files
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp


def test_next_sync_date():
    assert next_sync_date(date(2026, 11, 16)) == date(2026, 11, 21)
    assert next_sync_date(date(2026, 11, 21)) == date(2026, 11, 21)
    assert next_sync_date(date(2026, 12, 25)) == date(2027, 1, 1)


def test_dashboard_shows_month_claude_cost(client, monkeypatch):
    from contextlib import nullcontext
    from decimal import Decimal

    from rental_agent import attention, db
    monkeypatch.setenv("RENTAL_DB_URL", "postgresql://unused")
    monkeypatch.setenv("RENTAL_CLAUDE_MONTHLY_BUDGET", "20")
    monkeypatch.setattr(attention, "items", lambda _c, _today: [])
    monkeypatch.setattr(db, "connect", lambda _url: nullcontext())
    monkeypatch.setattr(db, "year_totals", lambda _c, _y: {"rent": 0, "expenses": 0, "needs_review": 0})
    monkeypatch.setattr(db, "job_runs", lambda _c, limit: [])
    monkeypatch.setattr(db, "month_claude_cost",
                        lambda _c, _d: {"cost": Decimal("3.456789"), "calls": 12})
    r = client.get("/dashboard")
    assert "Claude cost, " + date.today().strftime("%B") in r.text
    assert "$3.46" in r.text and "12 calls this month" in r.text
    assert 'max="20"' in r.text and "$3.46 of $20.00 budget" in r.text
