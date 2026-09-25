"""HTTP client for the modem web UI: login, page discovery (crawl) and fetching."""
from __future__ import annotations

import base64
import hashlib
import logging
import re
from collections import deque
from dataclasses import dataclass
from urllib.parse import parse_qsl, urldefrag, urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

from .config import Config

log = logging.getLogger(__name__)

# Never GET anything that looks like it changes modem state. Some embedded web UIs
# perform actions (reboot, factory reset, ...) on a plain GET request.
DANGEROUS = re.compile(
    r"reboot|restart|reset|factory|default|restore|upgrade|firmware_?up|update|flash|"
    r"logout|logoff|signout|delete|remove|del_|apply|save|commit|submit|set_|"
    r"backup|download|upload|ping|traceroute|diag|wizard|shutdown|poweroff|kill",
    re.IGNORECASE,
)
LOGOUT_HINT = re.compile(r"logout|logoff|signout|log_out", re.IGNORECASE)
# Page-like resources referenced from HTML/JS (menus in these UIs are often built in JS).
RESOURCE_RE = re.compile(
    r"""["'`]((?:\.{0,2}/)?[\w\-./]*?\.(?:asp|aspx|html?|cgi|php|json|lua|cmd|xml|js))(\?[^"'`\s<>]*)?["'`]""",
    re.IGNORECASE,
)
STATIC_EXT = re.compile(r"\.(?:css|png|jpe?g|gif|svg|ico|woff2?|ttf|eot|map)(?:\?|$)", re.IGNORECASE)


@dataclass
class Page:
    url: str
    path: str
    content_type: str
    text: str


class LoginError(RuntimeError):
    pass


def looks_like_login_page(text: str) -> bool:
    return bool(re.search(r"<input[^>]+type\s*=\s*[\"']?password", text, re.IGNORECASE))


class ModemClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = cfg.url
        self.host = urlparse(cfg.url).netloc
        self.session = self._new_session()
        self.logged_in = False
        self.logout_url: str | None = None
        self.exclude = [re.compile(p, re.IGNORECASE) for p in cfg.extra_exclude]
        if not cfg.verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _new_session(self) -> requests.Session:
        s = requests.Session()
        s.verify = self.cfg.verify_tls
        if self.cfg.proxy:
            s.proxies = {"http": self.cfg.proxy, "https": self.cfg.proxy}
            s.trust_env = False
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) modem-exporter/1.0",
            "Accept": "text/html,application/json,application/xhtml+xml,*/*;q=0.8",
        })
        return s

    # ------------------------------------------------------------------ http
    def get(self, url: str) -> requests.Response:
        url = urljoin(self.base + "/", url)
        return self.session.get(url, timeout=self.cfg.timeout, headers={"Referer": self.base + "/"})

    def is_safe(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.netloc and parsed.netloc != self.host:
            return False
        target = parsed.path + ("?" + parsed.query if parsed.query else "")
        if DANGEROUS.search(target) or STATIC_EXT.search(parsed.path):
            return False
        return not any(p.search(target) for p in self.exclude)

    # ----------------------------------------------------------------- login
    def _encode_password(self) -> str:
        pw = self.cfg.password
        enc = self.cfg.password_encoding
        if enc == "base64":
            return base64.b64encode(pw.encode()).decode()
        if enc == "md5":
            return hashlib.md5(pw.encode()).hexdigest()
        if enc == "sha256":
            return hashlib.sha256(pw.encode()).hexdigest()
        return pw

    def _extra_fields(self) -> dict[str, str]:
        raw = self.cfg.login_extra_fields
        raw = raw.replace("{username}", self.cfg.username).replace("{password}", self._encode_password())
        return dict(parse_qsl(raw, keep_blank_values=True))

    def login(self) -> None:
        """Log in via the web form. Auto-detects the form; LOGIN_* env vars override."""
        self.session = self._new_session()
        self.logged_in = False
        resp = self.get("/")
        page_url = resp.url
        html = resp.text

        # Some UIs put the login form in a frame or redirect via JS; follow once.
        if not looks_like_login_page(html):
            soup = BeautifulSoup(html, "html.parser")
            nxt = None
            frame = soup.find(["frame", "iframe"], src=True)
            if frame:
                nxt = frame["src"]
            else:
                m = re.search(r"""(?:location(?:\.href)?\s*=|location\.replace\()\s*["']([^"']+)["']""", html)
                if m:
                    nxt = m.group(1)
            if nxt:
                r2 = self.get(urljoin(page_url, nxt))
                if looks_like_login_page(r2.text):
                    resp, page_url, html = r2, r2.url, r2.text

        if not looks_like_login_page(html) and not self.cfg.login_path:
            if resp.status_code == 401:
                log.info("Modem uses HTTP auth; switching to basic auth")
                self.session.auth = (self.cfg.username, self.cfg.password)
                if self.get("/").status_code == 401:
                    self.session.auth = requests.auth.HTTPDigestAuth(self.cfg.username, self.cfg.password)
                    if self.get("/").status_code == 401:
                        raise LoginError("HTTP authentication rejected")
            else:
                log.info("No login form found at %s, assuming already authenticated / no auth", page_url)
            self.logged_in = True
            return

        action, method, fields = self._build_login_form(html, page_url)
        log.debug("Submitting login to %s (%s) fields=%s", action, method, sorted(fields))
        if method == "get":
            r = self.session.get(action, params=fields, timeout=self.cfg.timeout, headers={"Referer": page_url})
        else:
            r = self.session.post(action, data=fields, timeout=self.cfg.timeout, headers={"Referer": page_url})

        if r.status_code >= 400:
            raise LoginError(f"Login POST to {action} returned HTTP {r.status_code}")
        # Verify: the landing page must no longer be the login form.
        check = self.get("/")
        if looks_like_login_page(check.text) and looks_like_login_page(r.text):
            raise LoginError(
                "Still on the login page after submitting credentials. Wrong password, or the "
                "firmware encodes the password in JavaScript (try PASSWORD_ENCODING=base64|md5|sha256) "
                "or uses a different login endpoint (see LOGIN_PATH / LOGIN_*_FIELD). "
                "Run `python -m modem_exporter discover` to dump the login page for inspection."
            )
        self.logged_in = True
        log.info("Logged in to %s", self.base)

    def _build_login_form(self, html: str, page_url: str) -> tuple[str, str, dict[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        pw_input = soup.find("input", attrs={"type": re.compile("password", re.IGNORECASE)})
        form = pw_input.find_parent("form") if pw_input else None
        container = form or soup

        fields: dict[str, str] = {}
        user_name = self.cfg.login_user_field
        pass_name = self.cfg.login_pass_field
        for inp in container.find_all(["input", "select"]):
            name = inp.get("name")
            if not name:
                continue
            itype = (inp.get("type") or "text").lower()
            if itype in ("submit", "button", "image", "reset", "file"):
                continue
            if itype in ("checkbox", "radio") and not inp.has_attr("checked"):
                continue
            if itype == "password" and not pass_name:
                pass_name = name
            elif itype in ("text", "email") and not user_name:
                user_name = name
            fields[name] = inp.get("value", "")
        if not user_name:
            user_name = next((n for n in fields if re.search(r"user|login|name|account", n, re.IGNORECASE)), "username")
        if not pass_name:
            pass_name = next((n for n in fields if re.search(r"pass|pwd", n, re.IGNORECASE)), "password")
        fields[user_name] = self.cfg.username
        fields[pass_name] = self._encode_password()
        fields.update(self._extra_fields())

        if self.cfg.login_path:
            action = urljoin(self.base + "/", self.cfg.login_path)
            method = "post"
        else:
            action = urljoin(page_url, form.get("action") or page_url) if form else page_url
            method = (form.get("method") or "post").lower() if form else "post"
        return action, method, fields

    def logout(self) -> None:
        if self.logout_url and self.logged_in:
            try:
                self.session.get(self.logout_url, timeout=self.cfg.timeout)
            except requests.RequestException as exc:
                log.debug("Logout failed: %s", exc)
        self.logged_in = False

    # ------------------------------------------------------------ fetching
    def fetch(self, path: str) -> Page | None:
        """Fetch a page, re-logging in once if the session expired."""
        for attempt in range(2):
            if not self.logged_in:
                self.login()
            r = self.get(path)
            ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if r.status_code in (401, 403) or (ctype != "application/json" and looks_like_login_page(r.text)):
                log.debug("Session expired while fetching %s (attempt %d)", path, attempt + 1)
                self.logged_in = False
                continue
            if r.status_code >= 400:
                log.debug("GET %s -> HTTP %d", path, r.status_code)
                return None
            r.encoding = r.encoding or r.apparent_encoding
            return Page(url=r.url, path=path, content_type=ctype, text=r.text)
        raise LoginError(f"Could not stay logged in while fetching {path}")

    def to_path(self, url: str) -> str:
        p = urlparse(url)
        return p.path + ("?" + p.query if p.query else "")

    def discover(self) -> list[Page]:
        """Breadth-first crawl of the web UI starting at the landing page."""
        if not self.logged_in:
            self.login()
        start = self.get("/").url
        queue: deque[str] = deque([self.to_path(start), "/"])
        seen: set[str] = set()
        pages: list[Page] = []
        while queue and len(seen) < self.cfg.max_pages:
            path = queue.popleft()
            if path in seen:
                continue
            seen.add(path)
            try:
                page = self.fetch(path)
            except requests.RequestException as exc:
                log.debug("Fetch %s failed: %s", path, exc)
                continue
            if page is None:
                continue
            pages.append(page)
            for link in self._extract_links(page):
                if link not in seen:
                    queue.append(link)
        log.info("Discovery crawled %d page(s)", len(pages))
        return pages

    def _extract_links(self, page: Page) -> list[str]:
        candidates: list[str] = []
        if "html" in page.content_type or page.text.lstrip().startswith("<"):
            soup = BeautifulSoup(page.text, "html.parser")
            for tag in soup.find_all(["a", "frame", "iframe", "script"]):
                ref = tag.get("href") or tag.get("src")
                if ref:
                    candidates.append(ref)
        candidates.extend(m.group(1) + (m.group(2) or "") for m in RESOURCE_RE.finditer(page.text))

        out = []
        for ref in candidates:
            ref = ref.strip()
            if not ref or ref.startswith(("javascript:", "mailto:", "#", "data:")):
                continue
            url, _ = urldefrag(urljoin(page.url, ref))
            if not url.startswith(("http://", "https://")):
                continue
            if LOGOUT_HINT.search(url) and urlparse(url).netloc == self.host:
                self.logout_url = self.logout_url or url
            if self.is_safe(url):
                out.append(self.to_path(url))
        return out
