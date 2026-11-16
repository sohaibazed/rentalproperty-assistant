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
    monkeypatch.setenv("RENTAL_SCHEDULER", "off")
    monkeypatch.setenv("RENTAL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RENTAL_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.delenv("RENTAL_DB_URL", raising=False)
    (tmp_path / "artifacts").mkdir()
    return tmp_path, key


@pytest.fixture
def client(env):
    with TestClient(create_app()) as c:
        c.post("/login", data={"password": "pw"}, headers=ORIGIN)
        yield c


def test_requires_login(env):
    with TestClient(create_app()) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        c.post("/login", data={"password": "wrong"}, headers=ORIGIN)
        assert c.get("/", follow_redirects=False).status_code == 303
        c.post("/login", data={"password": "pw"}, headers=ORIGIN)
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
    for path in ("/", "/rent", "/expenses", "/research", "/leases", "/runs"):
        r = client.get(path)
        assert r.status_code == 200
    assert "RENTAL_DB_URL is not set" in client.get("/rent").text


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
