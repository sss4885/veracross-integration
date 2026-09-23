# Veracross integration design

Public config-entry version: 1. Examples use synthetic data.

## 1. Goals

1. One Veracross **parent login** → any number of **students** (kids), each added from the integration page.
2. Setup discovers kids and their classes after login; the user ticks kids, then classes.
3. Per student: class **order**, **display name** override, **show/hide**, **late handling**, **PIN**.
4. Optional **parent PIN** that unlocks every student with a switcher.
5. The integration, not the card, decides what "needs attention" means, so every count
   (summary sensor, badges, automations) agrees.
6. Full teacher notes and teacher comments, no truncation.
7. A small **SQLite** database keeps the current data plus the **history of every change**; refreshes only
   write and push what changed, and fire HA events for changes.


Hard rules : only `GET` requests after login; no links or navigation in the card;
credentials only in the config entry; big data never goes into HA state attributes or the recorder.

## 2. Config model

```
Config entry  (one per parent login)       domain: veracross, VERSION 1
  data:    username, password, school
  options: parent_pin (str|None), pin_lockout (bool, default True)
  subentries (type "student")               one per kid, added via "Add student"
    unique_id: "<student_id>"
    title:     "<student name>"
    data:
      student_id:   "900001"
      student_name: "Student 1"
      classes: [                            ordered = display order everywhere
        {"id": "51001", "portal_name": "6th Grade English", "name": "English", "show": true},
        ...
      ]
      late_mode:    "separate" | "listed" | "counted"      default "separate"
      pin:          None  # set through Reconfigure
      upcoming_days: 7
```

- **Config flow (new account):** step `user` = username / password / school → log in, discover kids →
  step `students` = tick kids (multi-select) → one `classes` step per ticked kid (tick classes;
  default = classes that currently have a numeric grade on the overview) → create entry + one subentry per kid.
- **Subentry flow `student`** ("Add student" button): discover kids not yet added → pick one →
  tick classes → create the subentry with default display names and student settings.
- **Reconfigure settings step:** `classes_order` is a multiple entity selector with drag reordering,
  limited to this student's class sensors. Defaults to shown classes in display order. Select at least
  one class; selected classes become shown in the chosen order, followed by hidden classes in their
  previous relative order. Also includes `late_mode`, optional `pin`, and `upcoming_days`.
- **Reconfigure names step:** one text box per shown class in display order, labelled
  `<class_id>: <portal_name>`. Defaults to the effective display name. Submitting writes stripped
  values to the entity registry `name`; empty or default display names clear the override.
- **Display names:** entity registry `name` is authoritative when set; otherwise use the legacy
  subentry class `name`, then `default_display_name(portal_name)` (leading grade prefix stripped).
  At setup, carry a nondefault legacy name into an unset registry name once per class. Names-step
  submission resets the legacy name to the default, so clearing the override takes effect everywhere.
- **Options flow** (entry "Configure"): `parent_pin`, `pin_lockout`, and a "re-discover classes" toggle
  that re-reads each kid's class list (new classes appear hidden).
- PIN validation: digits only, 4–8 long, **unique across all students and the parent PIN** of the entry.

## 3. Entities (per student; `<s>` = slug of student_name)

| Entity | State | Attributes (all small) |
|---|---|---|
| `sensor.veracross_<s>_<class slug>` per **portal** class | official grade % (None → unknown) | class_id, class_name (display), shown, teacher, letter, grade_source, period, weighting, needs_attention_count, late_count, upcoming_count, `data_version` |
| `sensor.veracross_<s>_summary` | **needs-attention total** (same number as the card's Overview badge) | student, student_id, late_mode, last_success, refreshing, throttled_until, upcoming_total, late_total, `classes` [{id, name, entity_id}] in display order, `data_version` |
| `button.veracross_<s>_refresh` | – | refreshes **all** students of the account (one login session) |

Account-level:

| Entity | State | Notes |
|---|---|---|
| `sensor.veracross_active_student` | `none` \| `<student_id>` \| `all` | **not restored** after restart (always `none`). Set only by `veracross.enter_pin`, cleared by `veracross.lock`. Attributes: `student_name`, `unlocked_at`, `locked_out_until`. |

Every discovered class has a sensor. Hidden classes have `shown: false` and are hidden by the
integration in the entity registry; showing them clears only integration-owned hiding, preserving
user-owned hiding. Hidden sensors use only grade, letter, and teacher from the shared overview
(snapshot cached per student); other detail attributes are empty and counts zero. They cause no
assignment, grade-detail, or feedback requests and are excluded from summary counts, websocket
classes/assignments/attention/late/feedback, and therefore the card. Shown sensors have `shown: true`.

Entity-id **slugs are fixed at creation** (unique_id based). Class sensor `_attr_name` always uses
`Veracross <student> <default display name>`; the registry override controls the displayed name.
Effective names are also used in summary classes, websocket classes, and events; registry renames
are reflected without a portal refresh. `_unrecorded_attributes` on every sensor: classes, data_version, throttled_until.

### Needs-attention rules (integration-owned; `parse.attention()`)
An assignment in the current grading period **needs attention** when `problem == true`, with Late
(status_id 7) handled by the student's `late_mode`:

| late_mode | Late rows in Needs attention list | Late counted in badges / summary | Separate Late section |
|---|---|---|---|
| `separate` (default) | no | no | yes |
| `listed` | yes (amber tag) | no | no |
| `counted` | yes | yes | no |

Order of the list: class display order, then Missing (1) before others, then due date.

## 4. Storage: SQLite  `/config/veracross/<entry_id>.db`

Stdlib `sqlite3`, WAL mode, every call through `hass.async_add_executor_job` (never the event loop).
Separate from HA's recorder DB. Size depends on assignment volume and retention.

Each config entry has its own SQLite file, opened with `hass.config.path("veracross", f"{entry.entry_id}.db")`. Accounts with overlapping student or class IDs remain isolated. Removing an integration entry closes its database and deletes its database file and any `-wal` / `-shm` sidecars.

`veracross.purge` removes assignment and grade history whose `changed_at` is strictly before `before`, feedback whose `date` is strictly before it (using `first_seen` when the date is absent), and assignment rows whose `removed_at` is strictly before it. Active assignments, current classes and grades, students, metadata, and refresh logs remain. Rows on the cutoff date remain.

```sql
students(student_id TEXT PRIMARY KEY, entry_id TEXT, name TEXT, school_year INT)
classes(student_id TEXT, class_id TEXT, portal_name TEXT, teacher TEXT,
        grade REAL, letter TEXT, grade_source TEXT, period TEXT, weighting TEXT,
        categories_json TEXT, fingerprint TEXT, updated_at TEXT,
        PRIMARY KEY(student_id, class_id))
assignments(student_id TEXT, class_id TEXT, assignment_id INT, period TEXT,
        title TEXT, type TEXT, assigned TEXT, due TEXT, status TEXT, status_id INT,
        problem INT, score REAL, max REAL, points REAL, pct REAL, counts INT, extra INT,
        notes TEXT, num_feedback INT, fingerprint TEXT, first_seen TEXT, updated_at TEXT,
        removed_at TEXT,
        PRIMARY KEY(student_id, class_id, assignment_id))
assignment_history(id INTEGER PRIMARY KEY, student_id TEXT, class_id TEXT, assignment_id INT,
        changed_at TEXT, change TEXT,           -- new|graded|regraded|status|notes|due|removed
        old_json TEXT, new_json TEXT)           -- only the fields that changed
grade_history(id INTEGER PRIMARY KEY, student_id TEXT, class_id TEXT, changed_at TEXT,
        grade REAL, letter TEXT)
feedback(student_id TEXT, class_id TEXT, feedback_id INT, assignment_id INT, person TEXT,
        date TEXT, message TEXT, first_seen TEXT, PRIMARY KEY(student_id, class_id, feedback_id))
refresh_log(id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, requests INT,
        changed INT, error TEXT)
meta(key TEXT PRIMARY KEY, value TEXT)          -- schema_version, per-student data_version
```

- **Fingerprint** = sha1 of the normalized fields that matter (not `is_unread` etc.). Unchanged
  fingerprint → no write. Changed → update row + one `assignment_history` row with only changed fields.
- Assignments that disappear from the portal get `removed_at` (hard-deleted only by purge).
- `data_version` (per student) increments when anything for that student changed; sensors expose it,
  the card refetches only when it moves.
- Retention: keep everything; `veracross.purge` action (before date) for manual cleanup.

## 5. Refresh (efficient)

Per account, serialized, 2 s between requests, one login session for all students:
1. `GET overview` per student (1 req) → official grade/letter/teacher per class.
2. Per shown class: `GET …/enrollment/<id>/assignments` (JSON). CSRF: reuse the token from the first
   embed page of the session; only fetch a class's embed page if the JSON call is rejected (403/422/HTML).
3. Grade Detail (wrapper + report, 2 req) **only if** the class's overview grade or assignments
   fingerprint changed since last run, or no categories are stored.
4. `GET …/enrollment/<id>/feedback` **only if** any assignment's `num_feedback` changed.
5. Diff → write → bump `data_version` → fire events → update sensors.
First refresh after install is a full one. Scheduling stays in the user's automation.

### Events (for notifications/automations)
`veracross_assignment_changed`: {student_id, student, class_id, class, assignment_id, title, change,
old, new}; `veracross_grade_changed`: {student_id, student, class_id, class, old_grade, new_grade,
old_letter, new_letter}. Not fired on the first full refresh of a student (would flood).

## 6. Actions & websocket API

| Action | Fields | Behaviour |
|---|---|---|
| `veracross.enter_pin` | `pin` | Matches a student PIN → active = that student; parent PIN → `all`. Wrong → `ServiceValidationError("wrong_pin")`. 5 wrong in 60 s → 60 s lockout (if `pin_lockout`). Never says whose PIN was close. Returns `{active, student_name}` as service response. |
| `veracross.lock` | – | active = `none`. |
| `veracross.refresh` | optional `student_id` | Same as the button (5-min throttle). |
| `veracross.purge` | `before` (date) | Deletes old assignment/grade history, feedback, and removed assignments. |

Websocket (for the card; requires an authenticated user, read-only):
- `veracross/student` {student_id} → `{student_id, name, late_mode, data_version, last_success,
  classes: [{id, name, portal_name, teacher, grade, letter, grade_source, period, weighting,
  categories, needs_attention_count, late_count, upcoming_count}],
  assignments: [...current period, full notes...], needs_attention: [{class_id, assignment_id}],
  late: [...ids], feedback: [...]}` — ordered by class display order. Refuses unless the student
  is the active student or active is `all` (the kiosk can't read a locked kid).
- `veracross/students` → [{student_id, name}] **only when active = `all`**, else [].

## 7. Card (`custom:veracross-card`)

- Config: `account_entity: sensor.veracross_active_student` (+ optional `upcoming_days`, `schedule`).
  No student is hard-coded; the card follows the active student.
- Active `none` → built-in PIN keypad (●●○○ display, 1-9 / C / 0 / ⌫) → `veracross.enter_pin`.
  Wrong PIN → shake-free "Wrong PIN" text. Lockout → "Try again in N s".
- Active student → that student's view . Active `all` → student switcher chips above the class chips.
- Lock icon (footer) → `veracross.lock`. Eye toggle, masking, ordering, Late section per `late_mode`,
  two-decimal grades:  lists and counts come from the integration (no rules in the card).
- Fetches `veracross/student` on open and whenever the summary sensor's `data_version` changes.


## Bundled frontend and diagnostics

`async_setup` registers the card through HTTP `StaticPathConfig` and
`async_register_static_paths`, then `frontend.add_extra_js_url` with the manifest
version in the URL. A per-instance guard prevents duplicate registration.

Login failures save only structural diagnostics (counts and boolean flags) in
`veracross_debug`. No raw HTML, server strings, attributes, form values, or URLs
are written. Public config entries begin at version 1; unknown future versions
are rejected. There is no importer for private config formats.
