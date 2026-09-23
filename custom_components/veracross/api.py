"""Async client for the Veracross parent portal."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp
from homeassistant.core import HomeAssistant

from .parse import (
    build_login_payload,
    grade_detail_document_url,
    parse_children,
    parse_feedback,
    parse_grade_detail,
    parse_html,
    parse_overview,
    parse_student_ids,
    pick_login_form,
)

_LOGGER = logging.getLogger(__name__)


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


def make_session() -> aiohttp.ClientSession:
    """Private session with its own cookie jar; closed by VeracrossAPI.async_close."""
    return aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(),
        headers={"User-Agent": USER_AGENT},
        timeout=aiohttp.ClientTimeout(total=30),
    )


class VeracrossError(Exception):
    """Base Veracross client error."""


class VeracrossAuthError(VeracrossError):
    """Authentication failed."""


class VeracrossCaptchaError(VeracrossAuthError):
    """A CAPTCHA prevents automated login."""


class VeracrossAPI:
    """Small, serialized client with an isolated cookie jar."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        school: str,
    ) -> None:
        self.hass = hass
        self.session = session
        self.username = username
        self.password = password
        self.school = school
        self._lock = asyncio.Lock()
        self._logged_in = False
        self._last_request: float | None = None
        self._json_lock = asyncio.Lock()
        self._csrf = None
        self.requests = 0
        # Instrument requests without changing the verified transport/login code.
        request = self._request

        async def counted_request(method, url, **kwargs):
            self.requests += 1
            if url == f"https://accounts.veracross.com/{self.school}/portals/login":
                self._csrf = None
            return await request(method, url, **kwargs)

        self._request = counted_request

    async def async_close(self) -> None:
        """Close this integration's private session."""
        if not self.session.closed:
            await self.session.close()

    async def _request(
        self, method: str, url: str, *, redirect_info: dict | None = None, **kwargs: Any
    ) -> tuple[int, str, str, str]:
        """Paced request; redirects are followed only to https *.veracross.com hosts."""
        if "allow_redirects" in kwargs:  # caller (login) validates hops itself
            return await self._request_once(method, url, redirect_info=redirect_info, **kwargs)
        self._validate_login_url(url)
        for _ in range(10):
            hop: dict = {}
            status, final_url, content_type, body = await self._request_once(
                method, url, redirect_info=hop, allow_redirects=False, **kwargs)
            if status not in (301, 302, 303, 307, 308) or not hop.get("location"):
                return status, final_url, content_type, body
            url = urljoin(final_url, hop["location"])
            self._validate_login_url(url)
            if status in (301, 302, 303):
                method = "GET"
                kwargs.pop("data", None)
        raise VeracrossError("Too many redirects")

    async def _request_once(
        self, method: str, url: str, *, redirect_info: dict | None = None, **kwargs: Any
    ) -> tuple[int, str, str, str]:
        """Make one paced request and consume its body."""
        if self._last_request is not None:
            delay = 2.0 - (time.monotonic() - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
        try:
            async with self.session.request(method, url, **kwargs) as response:
                body = await response.text()
                if redirect_info is not None:
                    redirect_info["location"] = response.headers.get("Location")
                return response.status, str(response.url), response.headers.get("Content-Type", ""), body
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise VeracrossError(f"Request failed: {type(err).__name__}") from err
        finally:
            self._last_request = time.monotonic()

    @staticmethod
    def _is_login_url(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.hostname == "accounts.veracross.com" or "/login" in parsed.path.lower()

    @staticmethod
    def _is_portal_url(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.hostname == "portals.veracross.com" and "/login" not in parsed.path.lower()

    @staticmethod
    def _has_captcha(html: str) -> bool:
        lowered = html.lower()
        if "recaptcha" in lowered:
            return True
        return any(
            "captcha" in item["name"].lower()
            for form in parse_html(html).forms
            for item in form["inputs"]
        )

    @staticmethod
    def _validate_login_url(url: str) -> None:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or not host.endswith(".veracross.com")
                or parsed.username is not None or parsed.password is not None):
            raise VeracrossAuthError("Untrusted login destination")

    async def _login_request(self, method: str, url: str, **kwargs: Any):
        """Validate every login hop before sending credentials or following it."""
        self._validate_login_url(url)
        for redirects in range(11):
            redirect_info = {}
            result = await self._request(method, url, allow_redirects=False,
                                         redirect_info=redirect_info, **kwargs)
            status, current_url, _, _ = result
            if status not in (301, 302, 303, 307, 308):
                return result
            location = redirect_info.get("location")
            if not location or redirects == 10:
                raise VeracrossAuthError("Invalid or excessive login redirects")
            target = urljoin(current_url, location)
            self._validate_login_url(target)
            if status in (307, 308) and method == "POST":
                # Never replay credentials at a redirect destination.
                raise VeracrossAuthError("Login redirect would replay credentials")
            if status == 303 or (status in (301, 302) and method == "POST"):
                method = "GET"
                kwargs.pop("data", None)
            url = target
        raise VeracrossAuthError("Excessive login redirects")

    async def login(self) -> None:
        """Perform the two-step accounts login."""
        async with self._lock:
            await self._login_locked()

    async def _login_locked(self) -> None:
        pages: dict[str, str] = {}
        login_url = f"https://accounts.veracross.com/{self.school}/portals/login"
        try:
            status, current_url, _, html = await self._login_request("GET", login_url)
            pages["login_step1.html"] = html
            if status >= 400:
                raise VeracrossAuthError(f"Login page returned HTTP {status}")

            form = pick_login_form(parse_html(html).forms)
            if form is None:
                if self._has_captcha(html):
                    raise VeracrossCaptchaError("The portal requires a CAPTCHA")
                raise VeracrossAuthError("Direct username login form not found; SSO logins are unsupported")
            payload, has_user, has_password = build_login_payload(form, self.username, self.password)
            if not has_user or has_password:
                raise VeracrossAuthError("Unexpected first login form")
            payload["commit"] = "Next"
            action = urljoin(current_url, form["action"])
            status, current_url, _, html = await self._login_request("POST", action, data=payload)
            pages["login_step2.html"] = html
            if status in (401, 403):
                raise VeracrossAuthError("Username was rejected")

            parsed = parse_html(html)
            form = pick_login_form(parsed.forms)
            payload, has_password = ({}, False)
            if form is not None:
                payload, _, has_password = build_login_payload(form, self.username, self.password)
            if not has_password:
                if self._has_captcha(html):
                    raise VeracrossCaptchaError(
                        "Blocked at the username step (CAPTCHA or username rejected); "
                        "see /config/veracross_debug/login_step2.html"
                    )
                raise VeracrossAuthError("Direct password form not found; SSO logins are unsupported")
            action = urljoin(current_url, form["action"])
            status, current_url, _, html = await self._login_request("POST", action, data=payload)
            pages["login_result.html"] = html
            if status in (401, 403):
                raise VeracrossAuthError("Password was rejected")

            parent_url = f"https://portals.veracross.com/{self.school}/parent"
            status, current_url, _, html = await self._login_request("GET", parent_url)
            pages["login_result.html"] = html
            if status >= 400 or not self._is_portal_url(current_url):
                raise VeracrossAuthError("Login did not reach the Veracross parent portal")
            self._logged_in = True
        except VeracrossError:
            self._logged_in = False
            await self._save_debug_pages(pages)
            raise


    async def _save_debug_pages(self, pages: dict[str, str]) -> None:
        if not pages:
            return
        debug_dir = Path(self.hass.config.path("veracross_debug"))

        def write_pages() -> None:
            debug_dir.mkdir(parents=True, exist_ok=True)
            for filename, body in pages.items():
                # Never persist server-controlled text, URLs, attributes, or values:
                # even an error page can echo credentials or contain session secrets.
                parsed = parse_html(body)
                diagnostic = {
                    "form_count": len(parsed.forms),
                    "has_username_input": any(
                        item["name"].lower() == "username"
                        for form in parsed.forms for item in form["inputs"]
                    ),
                    "has_password_input": any(
                        item["type"].lower() == "password"
                        for form in parsed.forms for item in form["inputs"]
                    ),
                    "has_csrf": bool(parsed.csrf),
                    "has_captcha": self._has_captcha(body),
                }
                (debug_dir / filename).write_text(
                    "<!doctype html><pre>" + json.dumps(diagnostic, indent=2) + "</pre>\n",
                    encoding="utf-8",
                )

        try:
            await self.hass.async_add_executor_job(write_pages)
        except OSError as err:
            _LOGGER.debug("Could not save Veracross login diagnostics: %s", type(err).__name__)

    def _embed_url(self, student_id: str, class_id: str) -> str:
        return (
            f"https://portals-embed.veracross.com/{self.school}/parent/children/"
            f"{student_id}/classes/{class_id}"
        )

    async def discover(self) -> list[dict]:
        parent = await self._get_page(f"https://portals.veracross.com/{self.school}/parent")
        overviews = {}
        children = []
        for sid in parse_student_ids(parent):
            overview = overviews[sid] = await self.fetch_overview(sid)
            if overview:
                page = await self._get_page(self._embed_url(sid, next(iter(overview))))
                self._csrf = parse_html(page).csrf
                children = parse_children(page)
                break
        for child in children:
            sid = child["student_id"]
            if sid not in overviews:
                overviews[sid] = await self.fetch_overview(sid)
            child["graded"] = {cid: row.get("grade") is not None
                               for cid, row in overviews[sid].items()}
        return children

    async def _fetch_json(self, student_id: str, class_id: str, endpoint: str) -> dict:
        async with self._json_lock:
            embed = self._embed_url(student_id, class_id)
            if not self._logged_in:
                await self.login()
            for attempt in range(2):
                if not self._csrf:
                    self._csrf = parse_html(await self._get_page(embed)).csrf
                headers = {"Accept": "*/*", "X-Requested-With": "XMLHttpRequest", "Referer": embed}
                if self._csrf:
                    headers["X-CSRF-Token"] = self._csrf
                async with self._lock:
                    status, url, content_type, body = await self._request(
                        "GET", f"https://portals-embed.veracross.com/{self.school}/parent/"
                        f"enrollment/{class_id}/{endpoint}", headers=headers)
                rejected = status in (401, 403, 422) or self._is_login_url(url) or (
                    "html" in content_type.lower() or body.lstrip().startswith("<"))
                if rejected:
                    self._csrf = None
                    if attempt == 0:
                        if status == 401 or self._is_login_url(url):
                            self._logged_in = False
                        continue
                    raise VeracrossAuthError(f"{endpoint} rejected after token refresh")
                if status >= 400:
                    raise VeracrossError(f"{endpoint} returned HTTP {status}")
                try:
                    payload = json.loads(body)
                except ValueError as err:
                    raise VeracrossError(f"{endpoint} was not valid JSON") from err
                if not isinstance(payload, dict):
                    raise VeracrossError(f"{endpoint} was not an object")
                return payload
            raise VeracrossError("JSON retry failed")

    async def fetch_assignments(self, student_id: str, class_id: str) -> dict:
        return await self._fetch_json(student_id, class_id, "assignments")

    async def fetch_feedback(self, student_id: str, class_id: str) -> list[dict]:
        return parse_feedback(await self._fetch_json(student_id, class_id, "feedback"))

    async def _get_page(self, url: str) -> str:
        """GET an HTML page, re-logging in once if the session has expired."""
        async with self._lock:
            if not self._logged_in:
                await self._login_locked()
            for attempt in range(2):
                status, final_url, _, body = await self._request("GET", url)
                if status in (401, 403) or self._is_login_url(final_url):
                    if attempt == 0:
                        self._logged_in = False
                        await self._login_locked()
                        continue
                    raise VeracrossAuthError("Page request was redirected to login")
                if status >= 400:
                    raise VeracrossError(f"{url.rsplit('/', 1)[-1]} returned HTTP {status}")
                return body
            raise VeracrossAuthError("Authentication retry failed")

    async def fetch_overview(self, student_id: str) -> dict[str, dict]:
        """Official grade, letter and teacher for every class (one request)."""
        url = (
            f"https://portals.veracross.com/{self.school}/parent/student/"
            f"{student_id}/overview"
        )
        return parse_overview(await self._get_page(url))

    async def fetch_grade_detail(self, student_id: str, class_id: str) -> dict | None:
        """Weighting line + category table from the class's Grade Detail report."""
        # The tab is a wrapper; the report is an iframe on documents.veracross.com
        # for the currently selected grading period.
        wrapper = await self._get_page(f"{self._embed_url(student_id, class_id)}/grade_detail")
        src = grade_detail_document_url(wrapper)
        if not src:
            raise VeracrossError("Grade Detail report link not found")
        if urlparse(src).hostname != "documents.veracross.com":
            raise VeracrossError("Unexpected Grade Detail report host")
        return parse_grade_detail(await self._get_page(src))
