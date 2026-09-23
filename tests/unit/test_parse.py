import json
import unittest
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components" / "veracross"))

from parse import (
    parse_overview,
    parse_grade_detail,
    build_login_payload,
    normalize,
    parse_html,
    pick_login_form,
    summarize,
)


TODAY = date(2030, 1, 18)


class ParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "fixture_la.json"
        cls.summary = summarize(normalize(json.loads(fixture.read_text())), TODAY)

    def test_english_summary(self):
        summary = self.summary
        self.assertEqual(summary["period"], "Semester 1")
        # Earned: 28/32*12 + 30/40*28 + 35/56*36 = 54; possible: 12+16+28+36.
        self.assertEqual(summary["grade"], 58.7)
        self.assertEqual(summary["earned"], 54.0)
        self.assertEqual(summary["possible"], 92.0)
        self.assertEqual(summary["categories"]["Mastery"]["avg"], 67.97)
        self.assertEqual(summary["categories"]["Practice"]["avg"], 37.5)
        self.assertEqual(summary["attention_count"], 2)
        self.assertEqual(summary["upcoming_count"], 3)
        self.assertIsNone(summary["letter"])
        self.assertEqual(summary["grade_source"], "calculated")

    def test_waiting_is_newest_first(self):
        waiting = self.summary["waiting"]
        self.assertEqual(
            [item["id"] for item in waiting],
            [61004, 61006, 61007, 61008, 61010],
        )
        self.assertEqual(
            [item["status"] for item in waiting],
            [
                "Pending",
                "Pending",
                "Turned In/Not Graded",
                "Pending",
                "Turned In/Not Graded",
            ],
        )

    def test_login_form_helpers_and_csrf(self):
        html = (
            '<meta name="csrf-token" content="csrf-value">'
            '<form id="login-form" action="/demo/portals/login/user_lookup" method="post">'
            '<input type="hidden" name="authenticity_token" value="x">'
            '<input type="text" name="username">'
            '<input type="submit" name="commit" value="Next">'
            "</form>"
        )
        parsed = parse_html(html)
        form = pick_login_form(parsed.forms)
        self.assertIsNotNone(form)
        self.assertEqual(form["action"], "/demo/portals/login/user_lookup")
        payload, has_user, has_pw = build_login_payload(form, "test-user", "test-password")
        self.assertEqual(payload["username"], "test-user")
        self.assertEqual(payload["authenticity_token"], "x")
        self.assertTrue(has_user)
        self.assertFalse(has_pw)
        self.assertEqual(parsed.csrf, "csrf-value")


if __name__ == "__main__":
    unittest.main()


class OfficialGradeTests(unittest.TestCase):
    def test_overview_official_grades(self):
        from pathlib import Path
        ov = parse_overview((Path(__file__).resolve().parents[1] / "fixtures" / "fixture_overview.html").read_text())
        self.assertEqual(ov["51001"], {"grade": 91.25, "letter": "A", "teacher": "Teacher A"})
        self.assertEqual(ov["51002"]["grade"], 78.4)
        self.assertEqual(ov["51004"]["letter"], "C")
        self.assertEqual(ov["51007"]["grade"], 103.5)
        self.assertIsNone(ov["51011"]["grade"])
        self.assertIsNone(ov["51009"]["letter"])  # Synthetic numeric grade with blank letter

    def test_grade_detail_report(self):
        page = (Path(__file__).resolve().parents[1] / "fixtures" / "fixture_grade_detail_doc.html").read_text()
        d = parse_grade_detail(page)
        self.assertEqual(d["weighting"], "Weighting by Assignment Points")
        self.assertEqual((d["grade"], d["letter"], d["period"]), (58.7, "F", "Semester 1"))
        self.assertEqual(d["categories"], [
            {"type": "Mastery", "n": 2, "earned": 43.5, "possible": 64.0, "avg": 67.97},
            {"type": "Practice", "n": 2, "earned": 10.5, "possible": 28.0, "avg": 37.5},
        ])

    def test_grade_detail_weight_column_and_empty(self):
        page = """<p class="assignment_grading_method">Weighting by
          Assignment Type</p><div id="assignment_type_summary"><table>
        <tr><th>Assignment Type</th><th>Weight</th><th>Points Earned</th><th>Points Possible</th><th>Average</th></tr>
        <tr><td>Tests <span>(3 assignments)</span></td><td>60%</td><td>40</td><td>100</td><td>40.00</td></tr>
        </table></div><div id="assignments"></div>"""
        d = parse_grade_detail(page)
        self.assertEqual(d["weighting"], "Weighting by Assignment Type")
        self.assertEqual(d["categories"], [{"type": "Tests", "n": 3, "weight": 60.0, "earned": 40.0, "possible": 100.0, "avg": 40.0}])
        self.assertEqual(parse_grade_detail("<html>nothing</html>")["categories"], [])

class GradeDetailLinkTests(unittest.TestCase):
    def test_iframe_url(self):
        from parse import grade_detail_document_url
        page = (Path(__file__).resolve().parents[1] / "fixtures" / "fixture_grade_detail_tab.html").read_text()
        self.assertEqual(grade_detail_document_url(page),
                         "https://documents.veracross.com/demo/grade_detail/51001?grading_period=1&key=_")
