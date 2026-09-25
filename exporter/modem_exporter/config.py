"""Configuration loaded from environment variables (typically via a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(*names: str, default: str | None = None) -> str | None:
    """Return the first non-empty environment variable among `names`."""
    for name in names:
        value = os.environ.get(name)
        if value not in (None, ""):
            return value
    return default


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader so the exporter also runs outside Docker without extra deps.

    Existing environment variables win over values from the file.
    """
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)


@dataclass
class Config:
    url: str
    username: str
    password: str
    name: str = ""
    verify_tls: bool = False
    timeout: float = 10.0
    listen_address: str = "0.0.0.0"
    listen_port: int = 9877
    cache_seconds: float = 30.0
    # Explicit list of pages to scrape. Empty => auto-discover by crawling the web UI.
    pages: list[str] = field(default_factory=list)
    max_pages: int = 80
    rediscover_seconds: float = 3600.0
    # Log out after every scrape so the web UI stays usable from a browser
    # (many ISP modems allow only one admin session at a time).
    logout_after_scrape: bool = True
    # Login overrides for firmwares whose login form can't be auto-detected.
    login_path: str = ""
    login_user_field: str = ""
    login_pass_field: str = ""
    login_extra_fields: str = ""
    password_encoding: str = "plain"  # plain | base64 | md5 | sha256
    extra_exclude: list[str] = field(default_factory=list)
    # e.g. socks5://user:pass@host:1080 (socks5h:// resolves DNS through the proxy)
    proxy: str = ""
    driver: str = "auto"  # auto | dzs | html
    # Wait this long after a failed login before trying again (modems lock accounts after a few failures).
    login_backoff_seconds: float = 300.0
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Config:
        url = _env("MODEM_URL", "URL")
        username = _env("MODEM_USERNAME", "USERNAME")
        password = _env("MODEM_PASSWORD", "PASSWORD")
        missing = [n for n, v in (("URL", url), ("USERNAME", username), ("PASSWORD", password)) if v is None]
        if missing:
            raise SystemExit(f"Missing required environment variable(s): {', '.join(missing)}")
        url = url.rstrip("/")
        if "://" not in url:
            url = "http://" + url
        return cls(
            url=url,
            username=username,
            password=password,
            name=_env("MODEM_NAME", default="") or "",
            verify_tls=_bool(_env("VERIFY_TLS"), False),
            timeout=float(_env("SCRAPE_TIMEOUT", default="10")),
            listen_address=_env("LISTEN_ADDRESS", default="0.0.0.0"),
            listen_port=int(_env("LISTEN_PORT", default="9877")),
            cache_seconds=float(_env("CACHE_SECONDS", default="30")),
            pages=_list(_env("PAGES")),
            max_pages=int(_env("MAX_PAGES", default="80")),
            rediscover_seconds=float(_env("REDISCOVER_SECONDS", default="3600")),
            logout_after_scrape=_bool(_env("LOGOUT_AFTER_SCRAPE"), True),
            login_path=_env("LOGIN_PATH", default="") or "",
            login_user_field=_env("LOGIN_USER_FIELD", default="") or "",
            login_pass_field=_env("LOGIN_PASS_FIELD", default="") or "",
            login_extra_fields=_env("LOGIN_EXTRA_FIELDS", default="") or "",
            password_encoding=(_env("PASSWORD_ENCODING", default="plain") or "plain").lower(),
            extra_exclude=_list(_env("EXCLUDE_PATTERNS")),
            proxy=_env("MODEM_PROXY", "PROXY", default="") or "",
            driver=(_env("DRIVER", default="auto") or "auto").lower(),
            login_backoff_seconds=float(_env("LOGIN_BACKOFF_SECONDS", default="300")),
            log_level=(_env("LOG_LEVEL", default="INFO") or "INFO").upper(),
        )
