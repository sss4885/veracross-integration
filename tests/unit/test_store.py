"""Fixture-backed, network-free tests for the standalone data layer."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1] / "fixtures"
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components" / "veracross"))
from parse import (attention, class_fingerprint, default_display_name, fingerprint,
                   normalize, parse_children, parse_feedback, parse_grade_detail,
                   parse_overview, parse_student_ids)
from store import VeracrossStore


def fixture(name):
    return (ROOT / name).read_text()


class DiscoveryTests(unittest.TestCase):
    def test_children(self):
        page = fixture("fixture_grade_detail_tab.html")
        children = parse_children(page)
        self.assertEqual([(c["name"], c["student_id"], len(c["classes"])) for c in children],
                         [("Student 1", "900001", 14), ("Student 2", "900002", 10)])
        self.assertIn({"id": "51001", "code": "ENG6", "portal_name": "6th Grade English"},
                      children[0]["classes"])
        # Removing the school segment exercises variable URL prefixes without inventing one.
        self.assertEqual(parse_children(page.replace("/demo/", "/")), children)

    def test_student_ids(self):
        page = fixture("fixture_overview.html")
        self.assertEqual(parse_student_ids(page)[:2], ["900001", "900002"])
        self.assertEqual(parse_student_ids(page + page), parse_student_ids(page))
        self.assertEqual(parse_student_ids(""), [])

    def test_display_name(self):
        for name, expected in (("6th Grade English", "English"),
                               ("6th Grade Advisory", "Advisory"),
                               ("Grade 6 English", "English"),
                               ("6TH GRADE English", "English"),
                               ("Homeroom", "Homeroom"), ("", "")):
            with self.subTest(name=name):
                self.assertEqual(default_display_name(name), expected)

    def test_attention(self):
        items = normalize(json.loads(fixture("fixture_la.json")))
        for mode, needs, late, count in (("separate", 1, 1, 1), ("listed", 2, 0, 1), ("counted", 2, 0, 2)):
            with self.subTest(mode=mode):
                result = attention(items, mode)
                self.assertEqual(len(result["needs_attention"]), needs)
                self.assertEqual(len(result["late"]), late)
                self.assertEqual(result["needs_attention_count"], count)
                self.assertEqual(result["late_count"], 1)
                self.assertEqual(result["needs_attention"][0]["title"],
                                 "Reading Response 5")
        self.assertTrue(attention(items, "separate")["late"][0]["title"].startswith("Reading Response 9"))
        with self.assertRaises(ValueError):
            attention(items, "")

    def test_fingerprints(self):
        items = normalize(json.loads(fixture("fixture_la.json")))
        official = parse_overview(fixture("fixture_overview.html"))["51001"]
        self.assertEqual(class_fingerprint(official, items), class_fingerprint(official, items[::-1]))
        self.assertEqual(fingerprint(items[0]), fingerprint(dict(reversed(list(items[0].items())))))
        modified = {**items[0], "num_feedback": items[0]["num_feedback"] + 1}
        self.assertNotEqual(fingerprint(items[0]), fingerprint(modified))
        self.assertNotEqual(class_fingerprint(official, items), class_fingerprint(None, items))
        self.assertEqual(len(fingerprint(items[0])), 40)

    def test_feedback_empty_shape(self):
        row = dict.fromkeys(("id", "feedback_id", "assignment_id", "person_record_type",
                             "feedback_person", "feedback_date", "feedback_message"))
        self.assertEqual(parse_feedback({"feedback": [row], "submissions": []}), [
            dict.fromkeys(("feedback_id", "assignment_id", "person", "date", "message"))])
        self.assertEqual(parse_feedback({}), [])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "entry-one.db"
        self.store = VeracrossStore(self.path)
        self.addCleanup(self.store.close)
        self.raw = json.loads(fixture("fixture_la.json"))
        self.items = normalize(self.raw)
        self.official = parse_overview(fixture("fixture_overview.html"))["51001"]
        self.detail = parse_grade_detail(fixture("fixture_grade_detail_doc.html"))
        self.store.upsert_student("900001", None, "Student 1", 2026)
        self.kwargs = dict(portal_name="6th Grade English", teacher=self.official["teacher"],
                           official=self.official, detail=self.detail, grade_source="official",
                           period=self.detail["period"], items=self.items,
                           now_iso="2030-01-18T00:00:00", emit_events=False)

    def apply(self, **kwargs):
        return self.store.apply_class("900001", "51001", **{**self.kwargs, **kwargs})

    def rows(self, table):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(f"SELECT * FROM {table}")]

    def test_first_and_identical_apply(self):
        result = self.apply()
        self.assertTrue(result["changed"])
        self.assertEqual(result["events"], [])
        self.assertEqual(len(self.rows("assignments")), 13)
        self.assertEqual(self.store.data_version("900001"), 1)
        before = {t: self.rows(t) for t in ("assignments", "classes", "assignment_history", "grade_history")}
        result = self.apply(now_iso="2030-01-18T01:00:00", emit_events=True)
        self.assertFalse(result["changed"])
        self.assertEqual(result["events"], [])
        self.assertEqual(self.store.data_version("900001"), 1)
        self.assertEqual(before, {t: self.rows(t) for t in before})
        self.assertEqual(self.store.get_class_fingerprint("900001", "51001"), result["class_fingerprint"])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "1")

    def test_graded_diff_and_event(self):
        self.apply()
        items = copy.deepcopy(self.items)
        item = next(i for i in items if i["score"] is None)
        item["score"] = 8
        item["pct"] = round(8 / item["max"] * 100, 1)
        before = len(self.rows("assignment_history"))
        result = self.apply(items=items, emit_events=True)
        history = self.rows("assignment_history")
        self.assertEqual(len(history), before + 1)
        self.assertEqual(history[-1]["change"], "graded")
        self.assertEqual(json.loads(history[-1]["old_json"]), {"score": None, "pct": None})
        self.assertEqual(json.loads(history[-1]["new_json"]), {"score": 8, "pct": item["pct"]})
        self.assertEqual(len(result["events"]), 1)
        event = result["events"][0]
        self.assertEqual(event["event_type"], "veracross_assignment_changed")
        self.assertEqual(event["data"], {"student_id": "900001", "student": "Student 1", "class_id": "51001",
            "class": self.kwargs["portal_name"], "assignment_id": item["id"], "title": item["title"],
            "change": "graded", "old": {"score": None, "pct": None}, "new": {"score": 8, "pct": item["pct"]}})
        self.assertEqual(self.store.data_version("900001"), 2)

    def test_removed_and_reappearing(self):
        self.apply()
        self.apply(items=self.items[1:])
        row = next(r for r in self.rows("assignments") if r["assignment_id"] == self.items[0]["id"])
        self.assertEqual(row["removed_at"], self.kwargs["now_iso"])
        self.assertEqual(self.rows("assignment_history")[-1]["change"], "removed")
        self.assertEqual(len(self.store.load_student("900001")["assignments"]["51001"]), 12)
        self.assertFalse(self.apply(items=self.items[1:])["changed"])
        self.assertTrue(self.apply()["changed"])
        self.assertEqual(len(self.store.load_student("900001")["assignments"]["51001"]), 13)
        self.assertEqual(json.loads(self.rows("assignment_history")[-1]["new_json"]), {"removed_at": None})

    def test_grade_change(self):
        self.apply()
        # Use another synthetic grade for the simulated change.
        official = parse_overview(fixture("fixture_overview.html"))["51002"]
        before = len(self.rows("grade_history"))
        result = self.apply(official=official, emit_events=True)
        self.assertEqual(len(self.rows("grade_history")), before + 1)
        self.assertEqual(len(result["events"]), 1)
        event = result["events"][0]
        self.assertEqual(event["event_type"], "veracross_grade_changed")
        self.assertEqual(event["data"]["old_grade"], self.official["grade"])
        self.assertEqual(event["data"]["new_grade"], official["grade"])
        self.assertEqual(len(self.rows("assignment_history")), 13)

    def test_full_notes_and_normalized_round_trip(self):
        self.apply()
        loaded = self.store.load_student("900001")
        self.assertEqual(sorted(loaded["assignments"]["51001"], key=lambda i: i["id"]),
                         sorted(self.items, key=lambda i: i["id"]))
        note = max((a["assignment_notes"].strip() for a in self.raw["assignments"]), key=len)
        self.assertGreater(len(note), 300)
        self.assertIn(note, [i["notes"] for i in loaded["assignments"]["51001"]])
        self.assertTrue(all(isinstance(i["num_feedback"], int) for i in self.items))
        self.assertEqual(self.store.get_feedback_counts("900001", "51001"),
                         {i["id"]: i["num_feedback"] for i in self.items})
        self.assertTrue(self.store.has_categories("900001", "51001"))
        self.assertFalse(self.apply(detail=None)["changed"])
        self.assertEqual(self.store.load_student("900001")["classes"]["51001"]["categories"], self.detail["categories"])

    def test_synthetic_feedback_round_trip(self):
        feedback = parse_feedback(json.loads(fixture("fixture_feedback.json")))
        self.assertEqual([row["feedback_id"] for row in feedback], [81001, 81002])
        self.apply()
        self.assertTrue(self.store.apply_feedback("900001", "51001", feedback, self.kwargs["now_iso"]))
        self.assertFalse(self.store.apply_feedback("900001", "51001", feedback, self.kwargs["now_iso"]))
        loaded = self.store.load_student("900001")["feedback"]["51001"]
        self.assertEqual([row["message"] for row in loaded], [row["message"] for row in feedback])

    def test_period_isolation(self):
        self.apply()
        # A new period must not remove assignments from Semester 1.
        self.apply(items=[], period="Semester 2", detail=None)
        self.assertEqual(self.store.load_student("900001")["assignments"]["51001"], [])
        self.assertEqual(len(self.store.load_student("900001", self.detail["period"])["assignments"]["51001"]), 13)
        self.assertTrue(all(r["removed_at"] is None for r in self.rows("assignments")))

    def test_change_classification(self):
        for field, value, expected in (("score", 8, "regraded"), ("status_id", 1, "status"),
                                       ("notes", "", "notes"), ("due", None, "due"),
                                       ("num_feedback", 1, "other")):
            with self.subTest(field=field):
                self.apply()
                items = copy.deepcopy(self.items)
                item = next(i for i in items if i["score"] is not None and i["notes"])
                item[field] = value
                self.apply(items=items)
                self.assertEqual(self.rows("assignment_history")[-1]["change"], expected)

    def test_atomic_rollback(self):
        with self.assertRaises(ValueError):
            self.apply(items=[*self.items, self.items[0]])
        self.assertEqual(self.rows("assignments"), [])
        self.assertEqual(self.rows("assignment_history"), [])
        self.assertEqual(self.store.data_version("900001"), 0)

    def test_thread_serialization(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.apply(), range(4)))
        self.assertEqual(sum(r["changed"] for r in results), 1)
        self.assertEqual(self.store.data_version("900001"), 1)

    def test_purge_feedback_and_removed_assignment_cutoffs(self):
        self.apply()
        self.apply(items=self.items[1:], now_iso="2030-01-17T12:00:00")
        self.apply(items=self.items[2:], now_iso="2030-01-18T00:00:00")
        feedback = [
            {"feedback_id": 1, "date": "2030-01-17"},
            {"feedback_id": 2, "date": "2030-01-18"},
            {"feedback_id": 3, "date": None},
        ]
        self.store.apply_feedback("900001", "51001", feedback, "2030-01-17T12:00:00")
        self.store.apply_feedback("900001", "51001", [{"feedback_id": 4, "date": None}],
                                  "2030-01-18T00:00:00")
        version = self.store.data_version("900001")
        self.assertEqual(self.store.purge_history("2030-01-18"), 4)
        self.assertEqual([r["feedback_id"] for r in self.rows("feedback")], [2, 4])
        remaining = {r["assignment_id"] for r in self.rows("assignments")}
        self.assertNotIn(self.items[0]["id"], remaining)
        self.assertIn(self.items[1]["id"], remaining)
        self.assertEqual(len(remaining), 12)
        self.assertEqual(self.store.data_version("900001"), version + 1)
        self.assertEqual(len(self.rows("classes")), 1)
        self.assertEqual(len(self.rows("assignment_history")), 14)
        self.assertEqual(self.store.purge_history("2030-01-18"), 0)

    def test_log_purge_and_missing_student(self):
        self.assertEqual(self.store.load_student("900002"),
                         {"classes": {}, "assignments": {}, "feedback": {}, "data_version": 0})
        self.assertIsNone(self.store.get_class_fingerprint("900002", "51001"))
        self.assertFalse(self.store.has_categories("900002", "51001"))
        self.assertFalse(self.store.apply_feedback("900001", "51001", [], self.kwargs["now_iso"]))
        self.apply()
        self.store.log_refresh(self.kwargs["now_iso"], self.kwargs["now_iso"], 1, True)
        self.assertEqual(len(self.rows("refresh_log")), 1)
        self.assertEqual(self.store.purge_history(self.kwargs["now_iso"]), 0)
        self.assertEqual(self.store.purge_history("2030-01-19"), 14)
        self.assertEqual(len(self.rows("assignments")), 13)
        self.assertEqual(self.store.data_version("900001"), 1)


if __name__ == "__main__":
    unittest.main()


class StudentRemovalTests(unittest.TestCase):
    def test_delete_student_removes_only_that_student(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = VeracrossStore(os.path.join(tmp, "entry.db"))
            items = normalize(json.loads(fixture("fixture_la.json")))
            for sid in ("900001", "900002"):
                store.upsert_student(sid, "entry", f"Student {sid[-1]}", 2030)
                store.apply_class(sid, "51001", portal_name="English", teacher="Teacher A", official=None,
                                  detail=None, grade_source="calculated", period=items[0]["period"],
                                  items=items, now_iso="2030-01-01T00:00:00+00:00", emit_events=False)
            store.delete_student("900001")
            self.assertEqual(store.list_students(), ["900002"])
            self.assertEqual(store.load_student("900001")["classes"], {})
            self.assertEqual(store.data_version("900001"), 0)
            self.assertTrue(store.load_student("900002")["assignments"]["51001"])
            store.close()
