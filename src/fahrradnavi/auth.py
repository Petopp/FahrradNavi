"""Zugriffsschutz für den öffentlichen Betrieb.

* **Standardmäßig aktiv:** Ohne Konfiguration wird beim Start ein zufälliges Passwort erzeugt und im Log ausgegeben –
  der Server läuft nie versehentlich offen. Abschalten nur ausdrücklich (``FAHRRADNAVI_AUTH=off``).
* Ein gemeinsames Passwort, Login-Seite, signiertes Session-Cookie (HttpOnly, SameSite=Strict, Secure hinter HTTPS).
* Schutz gegen Passwort-Raten: Sperre je IP nach mehreren Fehlversuchen (mit wachsender Dauer).
* Optionale IP-Freigabeliste (``FAHRRADNAVI_ALLOWED_NETS``): Zugriff nur aus bestimmten Netzen.
* Korrekte Client-IP hinter Reverse-Proxy nur von ausdrücklich vertrauenswürdigen Proxys (``FAHRRADNAVI_TRUSTED_PROXIES``).
* Optionaler API-Token (``FAHRRADNAVI_API_TOKEN``) für Skripte.

Alle Einstellungen kommen aus Umgebungsvariablen, siehe ``AuthConfig.from_env``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import ipaddress
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

log = logging.getLogger(__name__)

PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD_LEN = 8
COOKIE_NAME = "fn_session"
FAIL_DELAY = 0.5  # Sekunden Verzögerung nach falschem Passwort
PUBLIC_PATHS = ("/login", "/api/health")  # Health-Check liefert nichts Sensibles

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


# ---------------------------------------------------------------------------
# Passwörter
# ---------------------------------------------------------------------------


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """PBKDF2-SHA256-Hash im Format ``pbkdf2_sha256$Iterationen$Salt(hex)$Hash(hex)``."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, it, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


def is_password_hash(value: str) -> bool:
    return value.startswith("pbkdf2_sha256$") and value.count("$") == 3


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------


def _nets(value: str | None, default: str = "") -> list[Network]:
    out: list[Network] = []
    for part in (value if value is not None else default).split(","):
        part = part.strip()
        if part:
            out.append(ipaddress.ip_network(part, strict=False))
    return out


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on", "ja")


@dataclass
class AuthConfig:
    enabled: bool = True
    password_hash: str | None = None
    generated_password: str | None = None  # nur für die einmalige Log-Ausgabe
    api_token: str | None = None
    secret: bytes = b""
    session_ttl: int = 7 * 24 * 3600
    allowed_nets: list = field(default_factory=list)  # leer = alle Netze erlaubt
    trusted_proxies: list = field(default_factory=lambda: _nets("127.0.0.1,::1"))
    cookie_secure: str = "auto"  # "auto" = Secure, wenn die Verbindung HTTPS ist; "1" immer; "0" nie
    max_failures: int = 5
    lockout_seconds: int = 900

    @classmethod
    def disabled(cls) -> "AuthConfig":
        return cls(enabled=False)

    @classmethod
    def with_password(cls, password: str, **kw) -> "AuthConfig":
        cfg = cls(password_hash=hash_password(password, iterations=1000), **kw)
        cfg.secret = hashlib.sha256(b"fahrradnavi-session-v1|" + cfg.password_hash.encode()).digest()
        return cfg

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "AuthConfig":
        env = os.environ if env is None else env
        mode = (env.get("FAHRRADNAVI_AUTH") or "on").strip().lower()
        if mode in ("off", "0", "false", "no", "aus"):
            log.warning("ZUGRIFFSSCHUTZ ABGESCHALTET (FAHRRADNAVI_AUTH=off) – nur im vertrauenswürdigen Netz verwenden!")
            return cls.disabled()

        pw_hash = (env.get("FAHRRADNAVI_PASSWORD_HASH") or "").strip()
        pw_hash_given = bool(pw_hash)
        password = env.get("FAHRRADNAVI_PASSWORD") or ""
        generated = None
        if pw_hash:
            if not is_password_hash(pw_hash):
                raise ValueError("FAHRRADNAVI_PASSWORD_HASH hat kein gültiges Format (erzeugen mit: fahrradnavi hash-password)")
        elif password:
            if len(password) < MIN_PASSWORD_LEN:
                raise ValueError(f"FAHRRADNAVI_PASSWORD ist zu kurz (mindestens {MIN_PASSWORD_LEN} Zeichen)")
            pw_hash = hash_password(password)
        else:
            generated = secrets.token_urlsafe(15)
            pw_hash = hash_password(generated)

        token = (env.get("FAHRRADNAVI_API_TOKEN") or "").strip() or None
        if token and len(token) < 20:
            raise ValueError("FAHRRADNAVI_API_TOKEN ist zu kurz (mindestens 20 Zeichen, z. B. `openssl rand -hex 24`)")

        # Schlüssel für die Session-Cookies: stabil über Neustarts (sonst würden alle abgemeldet), ändert sich mit dem
        # Passwort. Bei festem Klartext-Passwort daraus, bei Hash aus dem Hash; beim Zufallspasswort neu bei jedem Start.
        secret_src = ((env.get("FAHRRADNAVI_SECRET_KEY") or "") or (password if not generated and not pw_hash_given else "")
                      ).encode() or pw_hash.encode()
        cfg = cls(
            enabled=True,
            password_hash=pw_hash,
            generated_password=generated,
            api_token=token,
            secret=hashlib.sha256(b"fahrradnavi-session-v1|" + secret_src).digest(),
            session_ttl=int(float(env.get("FAHRRADNAVI_SESSION_HOURS") or "168") * 3600),
            allowed_nets=_nets(env.get("FAHRRADNAVI_ALLOWED_NETS")),
            trusted_proxies=_nets(env.get("FAHRRADNAVI_TRUSTED_PROXIES"), "127.0.0.1,::1"),
            cookie_secure=(env.get("FAHRRADNAVI_COOKIE_SECURE") or "auto").strip().lower(),
            max_failures=int(env.get("FAHRRADNAVI_MAX_LOGIN_FAILURES") or "5"),
            lockout_seconds=int(env.get("FAHRRADNAVI_LOCKOUT_SECONDS") or "900"),
        )
        if generated:
            bar = "=" * 64
            log.warning(
                "\n%s\nKein Passwort konfiguriert – für diesen Start wurde ein zufälliges erzeugt:\n\n    %s\n\n"
                "Dauerhaft setzen mit FAHRRADNAVI_PASSWORD=... (oder FAHRRADNAVI_PASSWORD_HASH).\n%s",
                bar, generated, bar,
            )
        if not cfg.allowed_nets:
            log.info("Keine IP-Freigabeliste gesetzt (FAHRRADNAVI_ALLOWED_NETS): Login von überall, nur mit Passwort.")
        return cfg


# ---------------------------------------------------------------------------
# Session-Token (zustandslos, HMAC-signiert)
# ---------------------------------------------------------------------------


def make_token(cfg: AuthConfig, now: float | None = None) -> str:
    exp = int((time.time() if now is None else now) + cfg.session_ttl)
    msg = f"v1.{exp}"
    sig = hmac.new(cfg.secret, msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}.{sig}"


def check_token(cfg: AuthConfig, token: str | None, now: float | None = None) -> bool:
    if not token:
        return False
    try:
        ver, exp, sig = token.split(".")
        if ver != "v1":
            return False
        good = hmac.new(cfg.secret, f"{ver}.{exp}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and int(exp) >= (time.time() if now is None else now)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Brute-Force-Schutz
# ---------------------------------------------------------------------------


class LoginLimiter:
    """Sperrt eine IP nach ``max_failures`` Fehlversuchen; jede weitere Sperre dauert doppelt so lang (max. 24 h)."""

    def __init__(self, max_failures: int = 5, lockout: int = 900):
        self.max_failures = max_failures
        self.lockout = lockout
        self._fails: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._strikes: dict[str, int] = {}

    def retry_after(self, ip: str, now: float | None = None) -> int:
        now = time.time() if now is None else now
        until = self._locked_until.get(ip, 0.0)
        return max(0, int(until - now) + (1 if until > now else 0))

    def failure(self, ip: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        fails = [t for t in self._fails.get(ip, []) if now - t < self.lockout]
        fails.append(now)
        self._fails[ip] = fails
        if len(fails) >= self.max_failures:
            strikes = self._strikes.get(ip, 0) + 1
            self._strikes[ip] = strikes
            self._locked_until[ip] = now + min(self.lockout * 2 ** (strikes - 1), 86_400)
            self._fails[ip] = []
        if len(self._fails) > 10_000:  # Speicher begrenzen
            self._fails.clear()

    def success(self, ip: str) -> None:
        self._fails.pop(ip, None)
        self._strikes.pop(ip, None)
        self._locked_until.pop(ip, None)


# ---------------------------------------------------------------------------
# Client-IP / Schema hinter Proxys
# ---------------------------------------------------------------------------


def _ip(value: str | None):
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def _in(nets: list, ip) -> bool:
    return ip is not None and any(ip in n for n in nets)


def peer_is_trusted(cfg: AuthConfig, request: Request) -> bool:
    return bool(request.client) and _in(cfg.trusted_proxies, _ip(request.client.host))


def client_ip(cfg: AuthConfig, request: Request) -> str:
    """Echte Client-IP. X-Forwarded-For zählt nur, wenn die direkte Gegenstelle ein vertrauenswürdiger Proxy ist;
    dann wird von rechts der erste nicht vertrauenswürdige Eintrag genommen (kann nicht vom Client gefälscht werden)."""
    peer = request.client.host if request.client else ""
    if not peer_is_trusted(cfg, request):
        return peer
    chain = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    for hop in reversed(chain):
        if not _in(cfg.trusted_proxies, _ip(hop)):
            return hop
    return chain[0] if chain else peer


def is_https(cfg: AuthConfig, request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    return peer_is_trusted(cfg, request) and request.headers.get("x-forwarded-proto", "").lower() == "https"


def ip_allowed(cfg: AuthConfig, ip: str) -> bool:
    if not cfg.allowed_nets:
        return True
    return _in(cfg.allowed_nets, _ip(ip))


# ---------------------------------------------------------------------------
# Seiten
# ---------------------------------------------------------------------------

LOGIN_HTML = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>FahrradNavi – Anmeldung</title>
<style>
  :root { --bg:#f6f7f4; --panel:#fff; --ink:#1d2320; --muted:#66706a; --line:#dfe3dc; --accent:#1f7a4d; --err:#d63c3c; }
  @media (prefers-color-scheme: dark) { :root { --bg:#14181a; --panel:#1c2124; --ink:#e8ece9; --muted:#9aa5a0; --line:#2d3437; --accent:#38b26f; } }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--ink);
         font:16px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; padding:16px; }
  form { width:100%; max-width:360px; background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:24px; }
  h1 { font-size:22px; margin:0 0 4px; } p { color:var(--muted); margin:0 0 18px; font-size:14px; }
  label { display:block; font-size:13px; color:var(--muted); margin-bottom:4px; }
  input[type=password] { width:100%; padding:11px 12px; border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--ink); font:inherit; }
  button { margin-top:14px; width:100%; padding:11px; border:0; border-radius:8px; background:var(--accent); color:#fff; font:inherit; font-weight:600; cursor:pointer; }
  .err { margin-top:12px; padding:9px 11px; border:1px solid var(--err); border-radius:8px; font-size:14px; }
</style>
</head>
<body>
<form method="post" action="/login">
  <h1>FahrradNavi</h1>
  <p>Zugang nur mit Passwort.</p>
  <input type="hidden" name="next" value="__NEXT__">
  <label for="pw">Passwort</label>
  <input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>
  <button type="submit">Anmelden</button>
  __ERROR__
</form>
</body>
</html>
"""


def _login_page(next_url: str, error: str | None = None, status: int = 200, headers: dict | None = None) -> HTMLResponse:
    err = f'<div class="err" role="alert">{html.escape(error)}</div>' if error else ""
    body = LOGIN_HTML.replace("__NEXT__", html.escape(safe_next(next_url), quote=True)).replace("__ERROR__", err)
    return HTMLResponse(body, status_code=status, headers=headers)


def safe_next(url: str | None) -> str:
    """Nur relative Pfade innerhalb der App (kein Open Redirect)."""
    if not url or not url.startswith("/") or url.startswith("//") or "\\" in url or "\n" in url or "\r" in url:
        return "/"
    return url


def csp_for(tile_url: str) -> str:
    """Content-Security-Policy: nur eigene Ressourcen + der konfigurierte Kachel-Server."""
    m = re.match(r"^(https?://[^/{]*(?:\{[a-z]\}[^/{]*)*)", tile_url)
    origin = ""
    if m:
        origin = re.sub(r"\{[a-z]\}", "*", m.group(1))
        parts = urlsplit(origin.replace("*", "x"))
        if not parts.netloc:
            origin = ""
    return (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        f"img-src 'self' data: blob: {origin}; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )


# ---------------------------------------------------------------------------
# Einbindung in die FastAPI-App
# ---------------------------------------------------------------------------


def install(app, cfg: AuthConfig, tile_url: str = "") -> LoginLimiter:
    limiter = LoginLimiter(cfg.max_failures, cfg.lockout_seconds)
    csp = csp_for(tile_url)

    def secure_flag(request: Request) -> bool:
        if cfg.cookie_secure in ("1", "true", "yes", "on"):
            return True
        if cfg.cookie_secure in ("0", "false", "no", "off"):
            return False
        return is_https(cfg, request)

    def harden(request: Request, resp: Response) -> Response:
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Content-Security-Policy", csp)
        resp.headers.setdefault("Permissions-Policy", "geolocation=(self), camera=(), microphone=()")
        if cfg.enabled:
            static = request.url.path.startswith("/static/")
            resp.headers.setdefault("Cache-Control", "private, max-age=3600" if static else "no-store")
        if is_https(cfg, request):
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    def authenticated(request: Request) -> bool:
        if check_token(cfg, request.cookies.get(COOKIE_NAME)):
            return True
        if cfg.api_token and request.url.path.startswith("/api/"):
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer ") and hmac.compare_digest(auth[7:].strip(), cfg.api_token):
                return True
        return False

    @app.middleware("http")
    async def guard(request: Request, call_next):
        ip = client_ip(cfg, request)
        if not ip_allowed(cfg, ip):
            log.warning("Zugriff von %s nicht in FAHRRADNAVI_ALLOWED_NETS – abgelehnt", ip)
            return harden(request, Response("Zugriff von dieser Adresse nicht erlaubt.", status_code=403, media_type="text/plain"))
        path = request.url.path
        if cfg.enabled and path not in PUBLIC_PATHS and path != "/logout" and not authenticated(request):
            if path.startswith("/api/"):
                return harden(request, JSONResponse({"detail": "Nicht angemeldet"}, status_code=401))
            target = path + (("?" + request.url.query) if request.url.query else "")
            return harden(request, RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303))
        return harden(request, await call_next(request))

    @app.get("/login", include_in_schema=False)
    async def login_page(request: Request, next: str = "/") -> Response:
        if not cfg.enabled or authenticated(request):
            return RedirectResponse(safe_next(next), status_code=303)
        return _login_page(next)

    @app.post("/login", include_in_schema=False)
    async def login(request: Request) -> Response:
        form = parse_qs((await request.body())[:4096].decode("utf-8", "replace"))
        password = (form.get("password") or [""])[0]
        next_url = safe_next((form.get("next") or ["/"])[0])
        ip = client_ip(cfg, request)

        wait = limiter.retry_after(ip)
        if wait:
            log.warning("Login-Sperre aktiv für %s (noch %d s)", ip, wait)
            return _login_page(next_url, f"Zu viele Fehlversuche. Bitte in {max(1, wait // 60)} Min. erneut versuchen.", 429,
                               {"Retry-After": str(wait)})
        ok = cfg.enabled and await run_in_threadpool(verify_password, password[:512], cfg.password_hash or "")
        if not ok:
            limiter.failure(ip)
            log.warning("Fehlgeschlagener Login von %s", ip)
            await asyncio.sleep(FAIL_DELAY)
            return _login_page(next_url, "Falsches Passwort.", 401)
        limiter.success(ip)
        resp = RedirectResponse(next_url, status_code=303)
        resp.set_cookie(COOKIE_NAME, make_token(cfg), max_age=cfg.session_ttl, httponly=True, samesite="strict",
                        secure=secure_flag(request), path="/")
        return resp

    @app.post("/logout", include_in_schema=False)
    async def logout(request: Request) -> Response:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    return limiter
