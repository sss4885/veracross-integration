"""Account refreshes and small, ordered views backed by SQLite."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from functools import partial
import logging
import json
from pathlib import Path
from typing import Any

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import VeracrossAuthError, VeracrossError
from .const import DOMAIN, CACHE_MAX_AGE, REFRESH_THROTTLE
from .parse import normalize, summarize, attention, class_fingerprint
from .store import VeracrossStore
from .class_names import effective_name

_LOGGER = logging.getLogger(__name__)


def open_store(hass, entry_id):
    """Called only in the executor, including directory creation."""
    path = Path(hass.config.path("veracross", f"{entry_id}.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return VeracrossStore(path)


class VeracrossCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, api):
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=None, config_entry=entry)
        self.entry = entry
        self.api = api
        self.views = {}
        self.hidden_classes = {}
        self.refreshing = False
        self._last_fetch = None
        self._auth_failures = 0
        self._refresh_lock = asyncio.Lock()
        self._refresh_task = None
        self._closing = False
        self._full_classes = set()
        self.active = "none"
        self.student_name = None
        self.unlocked_at = None
        self.locked_out_until = None
        self.wrong_pins = []

    @property
    def students(self):
        return [s for s in self.entry.subentries.values() if s.subentry_type == "student"]

    async def db(self, method, *args, **kwargs):
        return await self.hass.async_add_executor_job(partial(getattr(self.store, method), *args, **kwargs))

    async def async_load_cache(self):
        self.store = await self.hass.async_add_executor_job(open_store, self.hass, self.entry.entry_id)
        needs_refresh = False
        now = dt_util.utcnow()
        current = {sub.data["student_id"] for sub in self.students}
        for stale in set(await self.db("list_students")) - current:
            await self.db("delete_student", stale)
        for sub in self.students:
            sid = sub.data["student_id"]
            await self.db("upsert_student", sid, self.entry.entry_id, sub.data["student_name"], now.year)
            stamp = await self.db("get_meta", f"last_success:{sid}")
            parsed = dt_util.parse_datetime(stamp) if stamp else None
            if parsed is None or now - dt_util.as_utc(parsed) > CACHE_MAX_AGE:
                needs_refresh = True
        await self.async_build_views()
        self.async_set_updated_data(self.views)
        return needs_refresh

    async def async_build_views(self):
        views = {}
        today = dt_util.now().date()
        for sub in self.students:
            cfg = sub.data
            sid = cfg["student_id"]
            stored = await self.db("load_student", sid)
            overview = json.loads(await self.db("get_meta", f"overview:{sid}") or "{}")
            self.hidden_classes[sid] = {}
            mode = cfg.get("late_mode", "separate")
            view = {"student_id": sid, "name": cfg["student_name"], "late_mode": mode,
                    "data_version": stored["data_version"],
                    "last_success": await self.db("get_meta", f"last_success:{sid}"),
                    "classes": [], "assignments": [], "needs_attention": [], "late": [], "feedback": []}
            for cls in cfg["classes"]:
                if not cls.get("show", True):
                    official = overview.get(cls["id"], {})
                    self.hidden_classes[sid][cls["id"]] = {
                        "grade": official.get("grade"), "letter": official.get("letter"),
                        "teacher": official.get("teacher"), "grade_source": "official",
                        "period": None, "weighting": None, "needs_attention_count": 0,
                        "late_count": 0, "upcoming_count": 0}
                    continue
                cid = cls["id"]
                row = stored["classes"].get(cid, {})
                items = stored["assignments"].get(cid, [])
                counts = attention(items, mode)
                upcoming = sum(1 for item in items if item["due"] and item["score"] is None
                               and today.isoformat() <= item["due"] <=
                               (today + timedelta(days=cfg.get("upcoming_days", 7))).isoformat())
                view["classes"].append({"id": cid, "name": effective_name(self.hass, self.entry, sid, cls), "portal_name": cls["portal_name"],
                    **{k: row.get(k) for k in ("teacher", "grade", "letter", "grade_source", "period", "weighting")},
                    "categories": row.get("categories", []), "upcoming_count": upcoming,
                    **{k: counts[k] for k in ("needs_attention_count", "late_count")}})
                view["assignments"].extend({**i, "class_id": cid} for i in items)
                for key in ("needs_attention", "late"):
                    view[key].extend({"class_id": cid, "assignment_id": i["id"]} for i in counts[key])
                view["feedback"].extend(stored["feedback"].get(cid, []))
            views[sid] = view
        self.views = views

    def update_display_names(self):
        """Refresh names without portal or database requests."""
        for sub in self.students:
            sid = sub.data["student_id"]
            classes = {c["id"]: c for c in sub.data["classes"]}
            for row in self.views.get(sid, {}).get("classes", []):
                row["name"] = effective_name(self.hass, self.entry, sid, classes[row["id"]])

    @property
    def throttled_until(self):
        return self._last_fetch + REFRESH_THROTTLE if self._last_fetch else None

    async def async_manual_refresh(self):
        if self.refreshing or (self.throttled_until and dt_util.utcnow() < self.throttled_until):
            self.async_update_listeners()
            return
        await self.async_request_refresh()

    async def async_close(self):
        """Finish cancellation before closing the database and portal session."""
        self._closing = True
        await self.async_shutdown()
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            await asyncio.gather(self._refresh_task, return_exceptions=True)
        await self.api.async_close()
        await self.db("close")

    async def _async_update_data(self):
        async with self._refresh_lock:
            if self._closing:
                raise UpdateFailed("Account is unloading")
            self._refresh_task = asyncio.current_task()
            try:
                return await self._refresh()
            finally:
                self._refresh_task = None

    async def _refresh(self):
        self._last_fetch = dt_util.utcnow()
        started = self._last_fetch.isoformat()
        self.refreshing = True
        self.async_update_listeners()
        self.api.requests = 0
        changed = 0
        errors = []
        auth_failure = False
        try:
            for sub in self.students:
                cfg = sub.data
                sid = cfg["student_id"]
                emit = bool(await self.db("get_meta", f"full_refresh:{sid}"))
                student_ok = True
                try:
                    overview = await self.api.fetch_overview(sid)
                    await self.db("set_meta", f"overview:{sid}", json.dumps(overview))
                    for cls in cfg["classes"]:
                        if not cls.get("show", True):
                            continue
                        cid = cls["id"]
                        items = normalize(await self.api.fetch_assignments(sid, cid))
                        calculated = summarize(items, dt_util.now().date())
                        official = overview.get(cid)
                        merged = self._merge_grade(calculated, official, None, effective_name(self.hass, self.entry, sid, cls), cid)
                        final = {"grade": merged["grade"], "letter": merged["letter"]}
                        previous = await self.db("get_class_fingerprint", sid, cid)
                        detail = None
                        if ((sid, cid) not in self._full_classes or previous != class_fingerprint(final, items)
                                or not await self.db("has_categories", sid, cid)):
                            detail = await self.api.fetch_grade_detail(sid, cid)
                        counts = await self.db("get_feedback_counts", sid, cid)
                        feedback = None
                        if any(i["num_feedback"] != counts.get(i["id"], 0) for i in items):
                            feedback = await self.api.fetch_feedback(sid, cid)
                        result = await self.db("apply_class", sid, cid, portal_name=cls["portal_name"],
                            teacher=(official or {}).get("teacher"), official=final, detail=detail,
                            grade_source=merged["grade_source"], period=calculated["period"], items=items,
                            now_iso=dt_util.utcnow().isoformat(), emit_events=emit)
                        self._full_classes.add((sid, cid))
                        changed += result["changed"]
                        for event in result["events"]:
                            self.hass.bus.async_fire(event["event_type"], {**event["data"], "class": effective_name(self.hass, self.entry, sid, cls)})
                        if feedback is not None:
                            changed += await self.db("apply_feedback", sid, cid, feedback, dt_util.utcnow().isoformat())
                except VeracrossError as err:
                    student_ok = False
                    auth_failure |= isinstance(err, VeracrossAuthError)
                    errors.append(type(err).__name__)
                    _LOGGER.warning("Could not refresh student_id=%s: %s", sid, type(err).__name__)
                if student_ok:
                    await self.db("set_meta", f"last_success:{sid}", dt_util.utcnow().isoformat())
                    await self.db("set_meta", f"full_refresh:{sid}", "1")
            self._auth_failures = self._auth_failures + 1 if auth_failure else 0
            await self.async_build_views()
            if self._auth_failures >= 2:
                raise ConfigEntryAuthFailed("Veracross authentication failed twice")
            if errors:
                raise UpdateFailed("; ".join(errors))
            return self.views
        finally:
            await self.db("log_refresh", started, dt_util.utcnow().isoformat(), self.api.requests,
                          changed, "; ".join(errors) or None)
            self.refreshing = False
            self.async_update_listeners()

    @staticmethod
    def _merge_grade(
        calculated: dict[str, Any],
        official: dict[str, Any] | None,
        detail: dict[str, Any] | None,
        name: str,
        class_id: str,
    ) -> dict[str, Any]:
        result = dict(calculated)
        result.update({"class_name": name, "class_id": class_id})
        if official and official.get("teacher"):
            result["teacher"] = official["teacher"]
        if not official or official.get("grade") is None:
            return result
        official_grade = official.get("grade")
        calculated_grade = calculated.get("grade")
        if (
            official_grade is not None
            and calculated_grade is not None
            and abs(float(official_grade) - float(calculated_grade)) > 0.5
        ):
            _LOGGER.debug("Official and calculated grades differ for class_id=%s", class_id)
        result.update(
            {"grade": official_grade, "letter": official.get("letter"), "grade_source": "official"}
        )
        if detail and detail.get("categories"):
            result["categories"] = detail["categories"]
        if detail and detail.get("weighting"):
            result["weighting"] = detail["weighting"]
        return result
