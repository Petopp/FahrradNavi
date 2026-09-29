"""Zugriffsschutz: Passwort-Login, Sperre, IP-Freigabe, Proxy-Header, Header-Härtung."""

import time

import pytest
from fastapi.testclient import TestClient

from fahrradnavi import auth
from fahrradnavi.api import create_app
from fahrradnavi.auth import AuthConfig, LoginLimiter

from fixture import POINTS

PW = "correct-horse-battery"
BODY = {"points": [{"lat": POINTS["S1"][0], "lon": POINTS["S1"][1]}, {"lat": POINTS["T1"][0], "lon": POINTS["T1"][1]}]}


@pytest.fixture(autouse=True)
def fast_fail(monkeypatch):
    monkeypatch.setattr(auth, "FAIL_DELAY", 0)


def client(graph, cfg, ip="203.0.113.7", **kw):
    return TestClient(create_app(graph=graph, auth=cfg), client=(ip, 5000), follow_redirects=False, **kw)


def cfg_pw(**kw):
    return AuthConfig.with_password(PW, **kw)


# --- Passwörter / Token ------------------------------------------------------


def test_password_hash_roundtrip():
    h = auth.hash_password("geheim-geheim", iterations=1000)
    assert h.startswith("pbkdf2_sha256$1000$") and "geheim" not in h
    assert auth.verify_password("geheim-geheim", h)
    assert not auth.verify_password("falsch", h)
    assert not auth.verify_password("x", "kaputt") and not auth.verify_password("x", "md5$1$aa$bb")
    assert auth.hash_password("geheim-geheim", 1000) != h  # zufälliges Salz


def test_token_valid_expired_tampered():
    cfg = cfg_pw()
    t = auth.make_token(cfg)
    assert auth.check_token(cfg, t)
    assert not auth.check_token(cfg, auth.make_token(cfg, now=time.time() - cfg.session_ttl - 10))
    v, exp, sig = t.split(".")
    assert not auth.check_token(cfg, f"{v}.{int(exp) + 99999}.{sig}")  # Ablaufzeit verlängert
    assert not auth.check_token(cfg, f"{v}.{exp}.{'0' * len(sig)}")
    assert not auth.check_token(cfg_pw(), t)  # anderes Passwort/Salz -> anderer Schlüssel
    assert not auth.check_token(cfg, None) and not auth.check_token(cfg, "quatsch")


# --- Konfiguration aus Umgebungsvariablen -------------------------------------


def test_default_config_generates_random_password(caplog):
    with caplog.at_level("WARNING"):
        cfg = AuthConfig.from_env({})
    assert cfg.enabled and cfg.generated_password and len(cfg.generated_password) >= 15
    assert cfg.generated_password in caplog.text  # einmalig im Log sichtbar
    assert auth.verify_password(cfg.generated_password, cfg.password_hash)
    assert AuthConfig.from_env({}).generated_password != cfg.generated_password


def test_env_password_hash_and_validation():
    assert AuthConfig.from_env({"FAHRRADNAVI_PASSWORD": PW}).generated_password is None
    h = auth.hash_password(PW, 1000)
    assert AuthConfig.from_env({"FAHRRADNAVI_PASSWORD_HASH": h}).password_hash == h
    with pytest.raises(ValueError, match="zu kurz"):
        AuthConfig.from_env({"FAHRRADNAVI_PASSWORD": "kurz"})
    with pytest.raises(ValueError, match="Format"):
        AuthConfig.from_env({"FAHRRADNAVI_PASSWORD_HASH": "nicht-gehasht"})
    with pytest.raises(ValueError, match="API_TOKEN"):
        AuthConfig.from_env({"FAHRRADNAVI_PASSWORD": PW, "FAHRRADNAVI_API_TOKEN": "zu-kurz"})
    assert not AuthConfig.from_env({"FAHRRADNAVI_AUTH": "off"}).enabled


def test_sessions_survive_restart_with_fixed_password():
    """Gleiches Passwort in der Umgebung -> Cookies bleiben nach einem Neustart gültig (Salz ändert den Schlüssel nicht)."""
    env = {"FAHRRADNAVI_PASSWORD": PW}
    a, b = AuthConfig.from_env(env), AuthConfig.from_env(env)
    assert a.password_hash != b.password_hash  # neues Salz je Start
    assert auth.check_token(b, auth.make_token(a))
    assert not auth.check_token(AuthConfig.from_env({"FAHRRADNAVI_PASSWORD": PW + "x"}), auth.make_token(a))  # Passwortwechsel meldet ab
    h = auth.hash_password(PW, 1000)
    assert auth.check_token(AuthConfig.from_env({"FAHRRADNAVI_PASSWORD_HASH": h}), auth.make_token(AuthConfig.from_env({"FAHRRADNAVI_PASSWORD_HASH": h})))
    # Zufallspasswort: bewusst nicht stabil
    assert not auth.check_token(AuthConfig.from_env({}), auth.make_token(AuthConfig.from_env({})))
    assert auth.check_token(AuthConfig.from_env({"FAHRRADNAVI_PASSWORD": PW, "FAHRRADNAVI_SECRET_KEY": "k"}),
                            auth.make_token(AuthConfig.from_env({"FAHRRADNAVI_PASSWORD": "anderes-passwort", "FAHRRADNAVI_SECRET_KEY": "k"})))


def test_env_networks_and_proxies():
    cfg = AuthConfig.from_env({"FAHRRADNAVI_PASSWORD": PW, "FAHRRADNAVI_ALLOWED_NETS": "192.168.0.0/16, 10.1.2.3",
                               "FAHRRADNAVI_TRUSTED_PROXIES": "172.28.0.0/24"})
    assert [str(n) for n in cfg.allowed_nets] == ["192.168.0.0/16", "10.1.2.3/32"]
    assert [str(n) for n in cfg.trusted_proxies] == ["172.28.0.0/24"]


# --- Zugriff ------------------------------------------------------------------


def test_everything_requires_login_by_default(graph):
    c = client(graph, cfg_pw())
    r = c.get("/")
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
    assert c.get("/static/vendor/leaflet.js").status_code == 303
    r = c.post("/api/route", json=BODY)
    assert r.status_code == 401 and r.json()["detail"] == "Nicht angemeldet"
    assert c.get("/api/config").status_code == 401
    assert c.get("/api/geocode?q=haupt").status_code == 401
    assert c.get("/api/docs").status_code in (401, 404)  # Swagger standardmäßig aus
    assert c.get("/api/openapi.json").status_code in (401, 404)


def test_swagger_docs_off_even_when_logged_in(graph):
    c = client(graph, cfg_pw())
    c.post("/login", data={"password": PW})
    assert c.get("/api/docs").status_code == 404 and c.get("/api/openapi.json").status_code == 404


def test_health_is_public_and_minimal(graph):
    r = client(graph, cfg_pw()).get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_login_page_and_wrong_password(graph):
    c = client(graph, cfg_pw())
    page = c.get("/login")
    assert page.status_code == 200 and 'type="password"' in page.text and "noindex" in page.text
    r = c.post("/login", data={"password": "falsch-falsch"})
    assert r.status_code == 401 and "Falsches Passwort" in r.text
    assert auth.COOKIE_NAME not in r.headers.get("set-cookie", "")


def test_login_success_sets_hardened_cookie_and_grants_access(graph):
    c = client(graph, cfg_pw())
    r = c.post("/login", data={"password": PW, "next": "/"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert "secure" not in cookie  # Klartext-HTTP im Test
    assert c.get("/api/config").status_code == 200  # Cookie wird vom TestClient mitgeschickt
    assert c.post("/api/route", json=BODY).status_code == 200
    assert c.get("/").status_code == 200


def test_cookie_is_secure_behind_https_proxy(graph):
    cfg = cfg_pw(trusted_proxies=auth._nets("10.0.0.0/8"))
    c = client(graph, cfg, ip="10.0.0.5")
    r = c.post("/login", data={"password": PW}, headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.9"})
    assert "secure" in r.headers["set-cookie"].lower()
    assert "strict-transport-security" in r.headers
    # von einem NICHT vertrauenswürdigen Client wird X-Forwarded-Proto ignoriert
    c2 = client(graph, cfg, ip="203.0.113.9")
    r2 = c2.post("/login", data={"password": PW}, headers={"X-Forwarded-Proto": "https"})
    assert "secure" not in r2.headers["set-cookie"].lower() and "strict-transport-security" not in r2.headers


def test_logout_clears_cookie(graph):
    c = client(graph, cfg_pw())
    c.post("/login", data={"password": PW})
    r = c.post("/logout")
    assert r.status_code == 200 and "fn_session" in r.headers["set-cookie"] and "max-age=0" in r.headers["set-cookie"].lower()
    assert c.get("/api/config").status_code == 401


def test_open_redirect_is_blocked(graph):
    c = client(graph, cfg_pw())
    for evil in ("https://evil.example/", "//evil.example", "/\\evil.example", "javascript:alert(1)"):
        r = c.post("/login", data={"password": PW, "next": evil})
        assert r.status_code == 303 and r.headers["location"] == "/"
    r = c.post("/login", data={"password": PW, "next": "/?r=1"})
    assert r.headers["location"] == "/?r=1"
    assert auth.safe_next(None) == "/"


def test_next_is_escaped_in_login_form(graph):
    page = client(graph, cfg_pw()).get('/login?next=/"><script>alert(1)</script>')
    assert "<script>alert(1)</script>" not in page.text


def test_api_token(graph):
    tok = "t" * 24
    c = client(graph, cfg_pw(api_token=tok))
    assert c.post("/api/route", json=BODY, headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    assert c.post("/api/route", json=BODY, headers={"Authorization": "Bearer falsch"}).status_code == 401
    assert c.get("/", headers={"Authorization": f"Bearer {tok}"}).status_code == 303  # Token gilt nur für /api/


def test_auth_can_be_disabled_explicitly(graph):
    c = client(graph, AuthConfig.disabled())
    assert c.get("/api/config").json()["auth"] is False
    assert c.post("/api/route", json=BODY).status_code == 200


# --- Brute-Force-Schutz ---------------------------------------------------------


def test_lockout_after_repeated_failures(graph):
    c = client(graph, cfg_pw(max_failures=3, lockout_seconds=600))
    for _ in range(3):
        assert c.post("/login", data={"password": "falsch-falsch"}).status_code == 401
    r = c.post("/login", data={"password": PW})  # auch das richtige Passwort wird jetzt abgewiesen
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 500
    # andere IP ist nicht betroffen
    other = client(graph, cfg_pw(max_failures=3), ip="198.51.100.20")
    assert other.post("/login", data={"password": PW}).status_code == 303


def test_limiter_unit_backoff_and_reset():
    lim = LoginLimiter(max_failures=2, lockout=100)
    lim.failure("a", now=0); assert lim.retry_after("a", now=0) == 0
    lim.failure("a", now=1); assert 95 < lim.retry_after("a", now=1) <= 101
    assert lim.retry_after("a", now=200) == 0  # Sperre abgelaufen
    lim.failure("a", now=200); lim.failure("a", now=201)
    assert lim.retry_after("a", now=201) > 190  # zweite Sperre doppelt so lang
    lim.success("a"); assert lim.retry_after("a", now=201) == 0
    lim.failure("b", now=0); lim.failure("b", now=500)  # Fehlversuche verfallen nach lockout
    assert lim.retry_after("b", now=500) == 0


# --- IP-Freigabeliste / Proxy ---------------------------------------------------


def test_ip_allowlist_blocks_everything_else(graph):
    cfg = cfg_pw(allowed_nets=auth._nets("192.168.0.0/16"))
    assert client(graph, cfg, ip="203.0.113.7").get("/login").status_code == 403
    assert client(graph, cfg, ip="203.0.113.7").post("/login", data={"password": PW}).status_code == 403
    assert client(graph, cfg, ip="203.0.113.7").get("/api/health").status_code == 403
    ok = client(graph, cfg, ip="192.168.1.20")
    assert ok.post("/login", data={"password": PW}).status_code == 303


def test_allowlist_uses_forwarded_ip_only_from_trusted_proxy(graph):
    cfg = cfg_pw(allowed_nets=auth._nets("192.168.0.0/16"), trusted_proxies=auth._nets("10.0.0.0/8"))
    proxy = client(graph, cfg, ip="10.0.0.2")
    assert proxy.get("/login", headers={"X-Forwarded-For": "192.168.5.5"}).status_code == 200
    assert proxy.get("/login", headers={"X-Forwarded-For": "203.0.113.5"}).status_code == 403
    # Fälschungsversuch: Angreifer hängt eine erlaubte IP vorn an, der Proxy ergänzt die echte
    assert proxy.get("/login", headers={"X-Forwarded-For": "192.168.5.5, 203.0.113.5"}).status_code == 403
    # direkter Client (kein vertrauenswürdiger Proxy) kann den Header nicht nutzen
    direct = client(graph, cfg, ip="203.0.113.5")
    assert direct.get("/login", headers={"X-Forwarded-For": "192.168.5.5"}).status_code == 403


def test_lockout_is_per_real_client_behind_proxy(graph):
    cfg = cfg_pw(max_failures=2, trusted_proxies=auth._nets("10.0.0.0/8"))
    c = client(graph, cfg, ip="10.0.0.2")
    h = {"X-Forwarded-For": "198.51.100.1"}
    for _ in range(2):
        c.post("/login", data={"password": "falsch-falsch"}, headers=h)
    assert c.post("/login", data={"password": PW}, headers=h).status_code == 429
    assert c.post("/login", data={"password": PW}, headers={"X-Forwarded-For": "198.51.100.2"}).status_code == 303


# --- Header --------------------------------------------------------------------


def test_security_headers_and_csp(graph, monkeypatch):
    monkeypatch.setenv("FAHRRADNAVI_TILE_URL", "https://{s}.tiles.example.org/{z}/{x}/{y}.png")
    r = client(graph, cfg_pw()).get("/login")
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "img-src 'self' data: blob: https://*.tiles.example.org" in csp
    assert r.headers["cache-control"] == "no-store"


def test_csp_helper():
    assert "img-src 'self' data: blob: https://tile.openstreetmap.org;" in auth.csp_for("https://tile.openstreetmap.org/{z}/{x}/{y}.png")
    assert "img-src 'self' data: blob: ;" in auth.csp_for("/tiles/{z}/{x}/{y}.png")
