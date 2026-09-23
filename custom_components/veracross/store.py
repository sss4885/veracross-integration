"""Synchronous, thread-serialized SQLite storage; no Home Assistant dependencies.

Events are returned as {"event_type": name, "data": the documented event payload}.
Callers dispatch them after this transaction has committed.
"""
from __future__ import annotations

import json
import sqlite3
import threading

if __package__:
    from .parse import class_fingerprint, fingerprint
else:  # Standalone imports from the canonical directory for stdlib tests.
    from parse import class_fingerprint, fingerprint


_SCHEMA = """
CREATE TABLE IF NOT EXISTS students(
 student_id TEXT PRIMARY KEY, entry_id TEXT, name TEXT, school_year INT);
CREATE TABLE IF NOT EXISTS classes(
 student_id TEXT, class_id TEXT, portal_name TEXT, teacher TEXT,
 grade REAL, letter TEXT, grade_source TEXT, period TEXT, weighting TEXT,
 categories_json TEXT, fingerprint TEXT, updated_at TEXT,
 PRIMARY KEY(student_id, class_id));
CREATE TABLE IF NOT EXISTS assignments(
 student_id TEXT, class_id TEXT, assignment_id INT, period TEXT,
 title TEXT, type TEXT, assigned TEXT, due TEXT, status TEXT, status_id INT,
 problem INT, score REAL, max REAL, points REAL, pct REAL, counts INT, extra INT,
 notes TEXT, num_feedback INT, fingerprint TEXT, first_seen TEXT, updated_at TEXT,
 removed_at TEXT, PRIMARY KEY(student_id, class_id, assignment_id));
CREATE TABLE IF NOT EXISTS assignment_history(
 id INTEGER PRIMARY KEY, student_id TEXT, class_id TEXT, assignment_id INT,
 changed_at TEXT, change TEXT, old_json TEXT, new_json TEXT);
CREATE TABLE IF NOT EXISTS grade_history(
 id INTEGER PRIMARY KEY, student_id TEXT, class_id TEXT, changed_at TEXT,
 grade REAL, letter TEXT);
CREATE TABLE IF NOT EXISTS feedback(
 student_id TEXT, class_id TEXT, feedback_id INT, assignment_id INT, person TEXT,
 date TEXT, message TEXT, first_seen TEXT,
 PRIMARY KEY(student_id, class_id, feedback_id));
CREATE TABLE IF NOT EXISTS refresh_log(
 id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, requests INT,
 changed INT, error TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
INSERT OR IGNORE INTO meta VALUES ('schema_version', '1');
"""
_ITEM_FIELDS = (
    "period", "title", "type", "assigned", "due", "status", "status_id", "problem",
    "score", "max", "points", "pct", "counts", "extra", "notes", "num_feedback",
)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _change(old, new):
    if "score" in new:
        if old["score"] is None and new["score"] is not None:
            return "graded"
        return "regraded"
    for kind, fields in (("status", ("status", "status_id", "problem")),
                         ("notes", ("notes",)), ("due", ("due",))):
        if any(field in new for field in fields):
            return kind
    return "other"


class VeracrossStore:
    def __init__(self, path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)

    def _bump(self, student_id):
        self._db.execute(
            "INSERT INTO meta(key,value) VALUES (?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1",
            (f"data_version:{student_id}",))

    def list_students(self):
        with self._lock:
            return [row[0] for row in self._db.execute("SELECT student_id FROM students")]

    def delete_student(self, student_id):
        """Remove every stored row for a student who was removed from the integration."""
        with self._lock, self._db:
            for table in ("assignments", "assignment_history", "grade_history", "feedback", "classes", "students"):
                self._db.execute(f"DELETE FROM {table} WHERE student_id=?", (student_id,))
            self._db.execute("DELETE FROM meta WHERE key LIKE ?", (f"%:{student_id}",))

    def upsert_student(self, student_id, entry_id, name, school_year):
        with self._lock, self._db:
            old = self._db.execute("SELECT * FROM students WHERE student_id=?", (student_id,)).fetchone()
            values = (str(student_id), entry_id, name, school_year)
            if old is not None and tuple(old) == values:
                return
            self._db.execute("INSERT INTO students VALUES (?,?,?,?) ON CONFLICT(student_id) "
                             "DO UPDATE SET entry_id=excluded.entry_id, name=excluded.name, "
                             "school_year=excluded.school_year", values)
            # Registration precedes the first data snapshot (version 1).
            if old is not None:
                self._bump(student_id)

    def apply_class(self, student_id, class_id, *, portal_name, teacher, official,
                    detail, grade_source, period, items, now_iso, emit_events):
        with self._lock, self._db:
            key = (str(student_id), str(class_id))
            previous = self._db.execute("SELECT * FROM classes WHERE student_id=? AND class_id=?", key).fetchone()
            old_class = dict(previous) if previous else {}
            student = self._db.execute("SELECT name FROM students WHERE student_id=?", key[:1]).fetchone()
            base = {"student_id": key[0], "student": student[0] if student else None,
                    "class_id": key[1], "class": portal_name}
            events = []
            changed = False

            def history(assignment_id, title, change, old, new):
                nonlocal changed
                changed = True
                self._db.execute("INSERT INTO assignment_history "
                                 "(student_id,class_id,assignment_id,changed_at,change,old_json,new_json) "
                                 "VALUES (?,?,?,?,?,?,?)", (*key, assignment_id, now_iso, change, _json(old), _json(new)))
                if emit_events:
                    events.append({"event_type": "veracross_assignment_changed", "data": {
                        **base, "assignment_id": assignment_id, "title": title,
                        "change": change, "old": old, "new": new}})

            stored = {row["assignment_id"]: dict(row) for row in self._db.execute(
                "SELECT * FROM assignments WHERE student_id=? AND class_id=?", key)}
            seen = set()
            for item in items:
                aid = item["id"]
                if aid is None or aid in seen:
                    raise ValueError("Assignments must have unique, non-null ids")
                seen.add(aid)
                old = stored.get(aid)
                digest = fingerprint(item)
                if old and old["fingerprint"] == digest and old["removed_at"] is None:
                    continue
                values = {field: item.get(field) for field in _ITEM_FIELDS}
                if old is None:
                    history(aid, item["title"], "new", {}, values)
                    fields = ("student_id", "class_id", "assignment_id", *_ITEM_FIELDS,
                              "fingerprint", "first_seen", "updated_at")
                    self._db.execute(f"INSERT INTO assignments ({','.join(fields)}) VALUES "
                                     f"({','.join('?' for _ in fields)})",
                                     (*key, aid, *values.values(), digest, now_iso, now_iso))
                else:
                    new_diff = {field: value for field, value in values.items() if old[field] != value}
                    if old["removed_at"] is not None:
                        new_diff["removed_at"] = None
                    old_diff = {field: old[field] for field in new_diff}
                    history(aid, item["title"], _change(old_diff, new_diff), old_diff, new_diff)
                    self._db.execute(f"UPDATE assignments SET {','.join(f'{field}=?' for field in values)},"
                                     "fingerprint=?,updated_at=?,removed_at=NULL "
                                     "WHERE student_id=? AND class_id=? AND assignment_id=?",
                                     (*values.values(), digest, now_iso, *key, aid))
            for aid, old in stored.items():
                if aid not in seen and old["period"] == period and old["removed_at"] is None:
                    history(aid, old["title"], "removed", {"removed_at": None}, {"removed_at": now_iso})
                    self._db.execute("UPDATE assignments SET removed_at=?,updated_at=? "
                                     "WHERE student_id=? AND class_id=? AND assignment_id=?",
                                     (now_iso, now_iso, *key, aid))

            digest = class_fingerprint(official, items)
            grade_data = official if official is not None else (detail or {})
            current = {"portal_name": portal_name, "teacher": teacher,
                       "grade": grade_data.get("grade"), "letter": grade_data.get("letter"),
                       "grade_source": grade_source, "period": period,
                       "weighting": detail.get("weighting") if detail is not None else old_class.get("weighting"),
                       "categories_json": _json(detail.get("categories", [])) if detail is not None
                       else old_class.get("categories_json", "[]"), "fingerprint": digest}
            if any(old_class.get(field) != current[field] for field in ("grade", "letter")):
                changed = True
                self._db.execute("INSERT INTO grade_history(student_id,class_id,changed_at,grade,letter) "
                                 "VALUES (?,?,?,?,?)", (*key, now_iso, current["grade"], current["letter"]))
                if emit_events:
                    events.append({"event_type": "veracross_grade_changed", "data": {
                        **base, "old_grade": old_class.get("grade"), "new_grade": current["grade"],
                        "old_letter": old_class.get("letter"), "new_letter": current["letter"]}})
            if not previous or any(old_class[field] != value for field, value in current.items()):
                changed = True
                fields = ("student_id", "class_id", *current, "updated_at")
                self._db.execute(f"INSERT INTO classes ({','.join(fields)}) VALUES "
                                 f"({','.join('?' for _ in fields)}) ON CONFLICT(student_id,class_id) "
                                 f"DO UPDATE SET {','.join(f'{f}=excluded.{f}' for f in (*current, 'updated_at'))}",
                                 (*key, *current.values(), now_iso))
            if changed:
                self._bump(student_id)
            return {"changed": changed, "events": events, "class_fingerprint": digest}

    def apply_feedback(self, student_id, class_id, feedback, now_iso):
        with self._lock, self._db:
            changed = False
            for row in feedback:
                if row.get("feedback_id") is None:
                    raise ValueError("Feedback must have a non-null feedback_id")
                cursor = self._db.execute("INSERT OR IGNORE INTO feedback VALUES (?,?,?,?,?,?,?,?)",
                    (student_id, class_id, row["feedback_id"], row.get("assignment_id"),
                     row.get("person"), row.get("date"), row.get("message"), now_iso))
                changed = changed or cursor.rowcount > 0
            if changed:
                self._bump(student_id)
            return changed

    def get_class_fingerprint(self, student_id, class_id):
        with self._lock:
            row = self._db.execute("SELECT fingerprint FROM classes WHERE student_id=? AND class_id=?",
                                   (student_id, class_id)).fetchone()
            return row[0] if row else None

    def get_feedback_counts(self, student_id, class_id):
        """Last observed portal counts, used to decide whether to fetch feedback."""
        with self._lock:
            return dict(self._db.execute("SELECT assignment_id,num_feedback FROM assignments "
                        "WHERE student_id=? AND class_id=? AND removed_at IS NULL", (student_id, class_id)))

    def has_categories(self, student_id, class_id):
        with self._lock:
            row = self._db.execute("SELECT categories_json FROM classes WHERE student_id=? AND class_id=?",
                                  (student_id, class_id)).fetchone()
            return bool(row and json.loads(row[0] or "[]"))

    def _data_version(self, student_id):
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (f"data_version:{student_id}",)).fetchone()
        return int(row[0]) if row else 0

    def data_version(self, student_id):
        with self._lock:
            return self._data_version(student_id)

    def load_student(self, student_id, period=None):
        with self._lock:
            classes, assignments, feedback = {}, {}, {}
            for row in self._db.execute("SELECT * FROM classes WHERE student_id=? ORDER BY class_id", (student_id,)):
                cls = dict(row)
                cid = cls["class_id"]
                cls["categories"] = json.loads(cls.pop("categories_json") or "[]")
                classes[cid] = cls
                assignments[cid] = []
                for assignment in self._db.execute("SELECT * FROM assignments WHERE student_id=? AND class_id=? "
                        "AND period=? AND removed_at IS NULL ORDER BY due DESC, assignment_id",
                        (student_id, cid, period if period is not None else cls["period"])):
                    item = {field: assignment[field] for field in _ITEM_FIELDS}
                    item["id"] = assignment["assignment_id"]
                    for field in ("problem", "counts", "extra"):
                        item[field] = bool(item[field])
                    assignments[cid].append(item)
                feedback[cid] = [dict(f) for f in self._db.execute(
                    "SELECT * FROM feedback WHERE student_id=? AND class_id=? ORDER BY date,feedback_id", (student_id, cid))]
            return {"classes": classes, "assignments": assignments, "feedback": feedback,
                    "data_version": self._data_version(student_id)}

    def purge_history(self, before_iso):
        with self._lock, self._db:
            affected = {row[0] for row in self._db.execute(
                "SELECT student_id FROM feedback WHERE date < ? OR (date IS NULL AND first_seen < ?) "
                "UNION SELECT student_id FROM assignments WHERE removed_at < ?",
                (before_iso, before_iso, before_iso))}
            deleted = sum(self._db.execute(f"DELETE FROM {table} WHERE changed_at < ?", (before_iso,)).rowcount
                          for table in ("assignment_history", "grade_history"))
            deleted += self._db.execute(
                "DELETE FROM feedback WHERE date < ? OR (date IS NULL AND first_seen < ?)",
                (before_iso, before_iso)).rowcount
            deleted += self._db.execute("DELETE FROM assignments WHERE removed_at < ?", (before_iso,)).rowcount
            for student_id in affected:
                self._bump(student_id)
            return deleted

    def log_refresh(self, started_at, finished_at, requests, changed, error=None):
        with self._lock, self._db:
            self._db.execute("INSERT INTO refresh_log(started_at,finished_at,requests,changed,error) "
                             "VALUES (?,?,?,?,?)", (started_at, finished_at, requests, changed, error))

    def get_meta(self, key):
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def set_meta(self, key, value):
        with self._lock, self._db:
            self._db.execute("INSERT INTO meta(key,value) VALUES (?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def close(self):
        with self._lock:
            self._db.close()
