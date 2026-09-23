"""Pure parsing / grade math for Veracross assignment JSON.

No Home Assistant imports here so it can be tested standalone.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import html
import hashlib
import json
import re
from html.parser import HTMLParser

# completion_status_id values seen in the portal
STATUS_NOT_TURNED_IN = 1


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _iso(mdy: str | None) -> str | None:
    if not mdy:
        return None
    try:
        return datetime.strptime(mdy, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def normalize(raw: dict) -> list[dict]:
    """Shrink the portal's assignment JSON to what the card needs."""
    out = []
    for a in raw.get("assignments", []):
        score = _num(a.get("raw_score"))
        max_score = _num(a.get("maximum_score"))
        pp = _num(a.get("points_possible"))
        notes = (a.get("assignment_notes") or "").strip()
        pct = None
        if score is not None and max_score:
            pct = round(score / max_score * 100, 1)
        out.append(
            {
                "id": a.get("assignment_id") or a.get("id"),
                "due": _iso(a.get("_date")),
                "assigned": a.get("assignment_date_long") or "",
                "type": a.get("assignment_type") or "",
                "title": (a.get("assignment_description") or "").strip(),
                "status": a.get("completion_status") or "",
                "status_id": a.get("completion_status_id"),
                "problem": bool(a.get("is_problem")),
                "score": score,
                "max": max_score,
                "points": pp,
                "pct": pct,
                "counts": bool(a.get("include_in_calculated_grade")),
                "extra": bool(a.get("extra_credit")),
                "period": a.get("grading_period") or "",
                "notes": notes,
                "num_feedback": int(a.get("num_feedback") or 0),
            }
        )
    out.sort(key=lambda x: x["due"] or "", reverse=True)
    return out


def current_period(items: list[dict], today: date | None = None) -> str:
    """Grading period of the assignments closest to today."""
    today = today or date.today()
    iso = today.isoformat()
    past = [i for i in items if i["due"] and i["due"] <= iso and i["period"]]
    pool = past or [i for i in items if i["period"]]
    if not pool:
        return ""
    # most recent due date wins; ties broken by frequency
    latest = max(i["due"] or "" for i in pool)
    periods = Counter(i["period"] for i in pool if i["due"] == latest)
    return periods.most_common(1)[0][0]


def grade(items: list[dict], period: str) -> dict:
    """Points-weighted grade, matching Veracross 'Weighting by Assignment Points'.

    Earned points = raw/max * points_possible. 'Not Turned In' with no score
    counts as zero. Pending / not-required / turned-in-not-graded are skipped.
    """
    cats: dict[str, dict] = {}
    tot_e = tot_p = 0.0
    graded = 0
    for i in items:
        if i["period"] != period or not i["counts"] or not i["points"]:
            continue
        if i["score"] is not None and i["max"]:
            earned = i["score"] / i["max"] * i["points"]
        elif i["score"] is None and i["status_id"] == STATUS_NOT_TURNED_IN:
            earned = 0.0
        else:
            continue
        possible = 0.0 if i["extra"] else i["points"]
        c = cats.setdefault(i["type"] or "Other", {"earned": 0.0, "possible": 0.0, "n": 0})
        c["earned"] += earned
        c["possible"] += possible
        c["n"] += 1
        tot_e += earned
        tot_p += possible
        graded += 1
    for c in cats.values():
        c["avg"] = round(c["earned"] / c["possible"] * 100, 2) if c["possible"] else None
        c["earned"] = round(c["earned"], 2)
        c["possible"] = round(c["possible"], 2)
    return {
        "percent": round(tot_e / tot_p * 100, 2) if tot_p else None,
        "earned": round(tot_e, 2),
        "possible": round(tot_p, 2),
        "graded": graded,
        "categories": cats,
    }


def summarize(items: list[dict], today: date | None = None) -> dict:
    today = today or date.today()
    iso = today.isoformat()
    period = current_period(items, today)
    cur = [i for i in items if i["period"] == period] if period else items
    g = grade(items, period)
    attention = [i for i in cur if i["problem"]]
    upcoming = [i for i in cur if i["due"] and i["due"] >= iso and i["score"] is None]
    waiting = [
        i
        for i in cur
        if i["score"] is None and i["status_id"] in (0, 6) and i["due"] and i["due"] < iso
    ]
    waiting.sort(key=lambda x: x["due"], reverse=True)
    return {
        "period": period,
        "grade": g["percent"],
        "earned": g["earned"],
        "possible": g["possible"],
        "graded_count": g["graded"],
        "categories": g["categories"],
        "attention_count": len(attention),
        "upcoming_count": len(upcoming),
        "waiting": waiting,
        "letter": None,
        "grade_source": "calculated",
        "assignments": cur,
    }


# ---------------------------------------------------------------- HTML forms


class _FormParser(HTMLParser):
    """Collect <form>s with their inputs, and <meta name=csrf-token>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: list[dict] = []
        self._cur: dict | None = None
        self.csrf: str | None = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta" and a.get("name", "").lower() == "csrf-token":
            self.csrf = a.get("content")
        elif tag == "form":
            self._cur = {"action": a.get("action", ""), "method": a.get("method", "get").lower(), "inputs": []}
            self.forms.append(self._cur)
        elif tag in ("input", "button") and self._cur is not None:
            if a.get("name"):
                self._cur["inputs"].append(
                    {"name": a["name"], "type": a.get("type", "text").lower(), "value": a.get("value", "")}
                )

    def handle_endtag(self, tag):
        if tag == "form":
            self._cur = None


def parse_html(html: str) -> _FormParser:
    p = _FormParser()
    p.feed(html)
    return p


USER_HINTS = ("username", "user", "login", "email")


def build_login_payload(form: dict, username: str, password: str) -> tuple[dict, bool, bool]:
    """Fill a login form. Returns (payload, has_user_field, has_password_field)."""
    data: dict[str, str] = {}
    has_user = has_pw = False
    for inp in form["inputs"]:
        n, t = inp["name"], inp["type"]
        if t == "password":
            data[n] = password
            has_pw = True
        elif t in ("text", "email") and any(h in n.lower() for h in USER_HINTS):
            data[n] = username
            has_user = True
        elif t in ("submit", "button"):
            continue
        elif t in ("checkbox", "radio"):
            if "remember" in n.lower():
                data[n] = inp["value"] or "1"
        else:
            data[n] = inp["value"]
    return data, has_user, has_pw


def pick_login_form(forms: list[dict]) -> dict | None:
    best, best_score = None, 0
    for f in forms:
        types = {i["type"] for i in f["inputs"]}
        names = " ".join(i["name"].lower() for i in f["inputs"])
        score = (3 if "password" in types else 0) + (2 if any(h in names for h in USER_HINTS) else 0)
        if score > best_score:
            best, best_score = f, score
    return best


# ------------------------------------------------------------ official grades

_OV_ITEM = re.compile(r"course-list-item")
_OV_CLASS = re.compile(r"/classes/(\d+)/assignments")
_OV_TEACHER = re.compile(r'course-teacher">\s*<small[^>]*>(.*?)</small>', re.S)
_OV_LETTER = re.compile(r'course-letter-grade">(.*?)</span>', re.S)
_OV_NUMERIC = re.compile(r'course-numeric-grade">\s*([-\d.]+)\s*%?\s*</span>', re.S)


def parse_overview(page: str) -> dict[str, dict]:
    """Official grade per class from the parent-portal student overview page.

    Returns {class_id: {"grade": float|None, "letter": str|None, "teacher": str|None}}.
    Classes the school hasn't graded show "-" and come back with grade None.
    """
    out: dict[str, dict] = {}
    for item in _OV_ITEM.split(page)[1:]:
        cid = _OV_CLASS.search(item)
        if not cid:
            continue
        teacher = _OV_TEACHER.search(item)
        letter = _OV_LETTER.search(item)
        numeric = _OV_NUMERIC.search(item)
        out[cid.group(1)] = {
            "grade": _num(numeric.group(1)) if numeric else None,
            "letter": (html.unescape(letter.group(1)).strip() or None) if letter else None,
            "teacher": html.unescape(re.sub(r"\s+", " ", teacher.group(1))).strip() if teacher else None,
        }
    return out


class _TableParser(HTMLParser):
    """Collect text of every <tr> as a list of cell strings."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        self.text.append(data)


def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _first(pattern: str, page: str) -> str | None:
    m = re.search(pattern, page, re.S)
    return _clean(m.group(1)) if m else None


def parse_grade_detail(page: str) -> dict:
    """Parse a documents.veracross.com Grade Detail report.

    Returns {"weighting", "grade", "letter", "period", "categories": [{type, n, earned,
    possible, avg[, weight]}]}. Summary columns are mapped by header text, so a
    category-weighted report with an extra Weight column parses too.
    """
    out: dict = {
        "weighting": _first(r'class="assignment_grading_method"[^>]*>(.*?)</p>', page),
        "grade": _num(_first(r'class="ptd_grade"[^>]*>(.*?)</span>', page)),
        "letter": _first(r'class="letter_grade"[^>]*>(.*?)</span>', page) or None,
        "period": (_first(r"<h1[^>]*>\s*Grade Detail\s*-\s*(.*?)</h1>", page) or None),
        "categories": [],
    }
    i = page.find('id="assignment_type_summary"')
    if i < 0:
        return out
    j = page.find('id="assignments"', i)
    section = page[i : j if j > 0 else len(page)]
    p = _TableParser()
    p.feed(section)
    if not p.rows:
        return out
    cols: dict[str, int] = {}
    for idx, h in enumerate(p.rows[0]):
        h = h.lower().replace(" ", "")
        for key, word in (("earned", "earned"), ("possible", "possible"), ("avg", "average"), ("weight", "weight")):
            if word in h and key not in cols:
                cols[key] = idx
    for row in p.rows[1:]:
        if not row or not row[0]:
            continue
        m = re.match(r"(.*?)\s*\((\d+) assignments?\)\s*$", row[0])
        cat = {"type": m.group(1) if m else row[0], "n": int(m.group(2)) if m else None}
        for key, idx in cols.items():
            cat[key] = _num(row[idx].replace("%", "").replace(",", "")) if idx < len(row) else None
        out["categories"].append(cat)
    return out

_GD_IFRAME = re.compile(r'<iframe[^>]*id="grade-detail-document"[^>]*src="([^"]+)"', re.S)
_GD_IFRAME_ALT = re.compile(r'<iframe[^>]*src="([^"]*documents\.veracross\.com[^"]*grade_detail[^"]*)"', re.S)


def grade_detail_document_url(page: str) -> str | None:
    """The report iframe URL (active grading period) from the Grade Detail tab page."""
    m = _GD_IFRAME.search(page) or _GD_IFRAME_ALT.search(page)
    return html.unescape(m.group(1)) if m else None


class _DiscoveryParser(HTMLParser):
    """Read portal links and the scoped enrollment chooser without dependencies."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.children = []
        self.student_ids = []
        self.stack = []
        self.chooser = None
        self.child = None
        self.child_depth = None
        self.anchor = None
        self.strong = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag in ("area", "base", "br", "col", "embed", "hr", "img", "input",
                   "link", "meta", "param", "source", "track", "wbr"):
            return
        self.stack.append(tag)
        if "parent-enrollment-chooser" in classes:
            self.chooser = len(self.stack)
        if self.chooser and "child" in classes:
            self.child = {"student_id": None, "name": "", "classes": []}
            self.child_depth = len(self.stack)
        if tag == "a":
            href = attrs.get("href") or ""
            match = re.search(r"parent/student/(\d+)/", href)
            if match and match[1] not in self.student_ids:
                self.student_ids.append(match[1])
            self.anchor = {"href": href, "menu": "menu-button" in classes,
                           "text": [], "code": []}
        if tag == "strong":
            self.strong = True

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor["text"].append(data)
            if self.strong:
                self.anchor["code"].append(data)

    def handle_endtag(self, tag):
        if tag == "strong":
            self.strong = False
        if tag == "a" and self.anchor is not None:
            a = self.anchor
            if self.child is not None:
                text = "".join(a["text"]).strip()
                if a["menu"]:
                    self.child["name"] = text
                match = re.search(r"/parent/children/(\d+)/classes(?:/(\d+))?(?:[/?#]|$)", a["href"])
                if match:
                    self.child["student_id"] = match[1]
                    if match[2]:
                        code = "".join(a["code"]).strip()
                        self.child["classes"].append({
                            "id": match[2], "code": code,
                            "portal_name": text[len(code):].lstrip().removeprefix(":").strip(),
                        })
            self.anchor = None
        if tag not in self.stack:
            return
        depth = len(self.stack) - self.stack[::-1].index(tag)
        if self.child_depth is not None and depth <= self.child_depth:
            if self.child["student_id"] is not None:
                self.children.append(self.child)
            self.child = self.child_depth = None
        if self.chooser is not None and depth <= self.chooser:
            self.chooser = None
        del self.stack[depth - 1:]


def parse_children(page: str) -> list[dict]:
    parser = _DiscoveryParser()
    parser.feed(page)
    return parser.children


def parse_student_ids(page: str) -> list[str]:
    parser = _DiscoveryParser()
    parser.feed(page)
    return parser.student_ids


def default_display_name(portal_name: str) -> str:
    return re.sub(r"^(?:\d+(?:-\d+)?(?:st|nd|rd|th)\s+Grade|Grade\s+\d+)\s+",
                  "", portal_name, flags=re.I)


def attention(items: list[dict], late_mode: str) -> dict:
    if late_mode not in ("separate", "listed", "counted"):
        raise ValueError(f"Unknown late_mode: {late_mode}")
    problems = sorted((i for i in items if i["problem"]),
                      key=lambda i: (i["status_id"] != 1, i["due"] or ""))
    late = [i for i in problems if i["status_id"] == 7]
    others = [i for i in problems if i["status_id"] != 7]
    return {
        "needs_attention": others if late_mode == "separate" else problems,
        "late": late if late_mode == "separate" else [],
        "needs_attention_count": len(problems) if late_mode == "counted" else len(others),
        "late_count": len(late),
    }


FINGERPRINT_FIELDS = (
    "due", "assigned", "type", "title", "status_id", "problem", "score", "max",
    "points", "counts", "extra", "period", "notes", "num_feedback",
)


def _digest(value) -> str:
    return hashlib.sha1(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False).encode("utf-8")).hexdigest()


def fingerprint(item: dict) -> str:
    return _digest({key: item.get(key) for key in FINGERPRINT_FIELDS})


def class_fingerprint(official: dict | None, items: list) -> str:
    official = official or {}
    return _digest([official.get("grade"), official.get("letter"),
                    sorted(fingerprint(item) for item in items)])


def parse_feedback(payload: dict) -> list[dict]:
    return [{"feedback_id": row.get("feedback_id") if row.get("feedback_id") is not None else row.get("id"),
             "assignment_id": row.get("assignment_id"), "person": row.get("feedback_person"),
             "date": row.get("feedback_date"), "message": row.get("feedback_message")}
            for row in payload.get("feedback", [])]
