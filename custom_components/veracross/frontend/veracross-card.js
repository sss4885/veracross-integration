const LOCK_PATH = "M12,17A2,2 0 0,0 14,15C14,13.89 13.1,13 12,13A2,2 0 0,0 10,15A2,2 0 0,0 12,17M18,8A2,2 0 0,1 20,10V20A2,2 0 0,1 18,22H6A2,2 0 0,1 4,20V10C4,8.89 4.9,8 6,8H7V6A5,5 0 0,1 12,1A5,5 0 0,1 17,6V8H18M12,3A3,3 0 0,0 9,6V8H15V6A3,3 0 0,0 12,3Z";
const EYE_PATH = "M12,9A3,3 0 0,0 9,12A3,3 0 0,0 12,15A3,3 0 0,0 15,12A3,3 0 0,0 12,9M12,17A5,5 0 0,1 7,12A5,5 0 0,1 12,7A5,5 0 0,1 17,12A5,5 0 0,1 12,17M12,4.5C7,4.5 2.73,7.61 1,12C2.73,16.39 7,19.5 12,19.5C17,19.5 21.27,16.39 23,12C21.27,7.61 17,4.5 12,4.5Z";
const EYE_OFF_PATH = "M11.83,9L15,12.16C15,12.11 15,12.05 15,12A3,3 0 0,0 12,9C11.94,9 11.89,9 11.83,9M7.53,9.8L9.08,11.35C9.03,11.56 9,11.77 9,12A3,3 0 0,0 12,15C12.22,15 12.44,14.97 12.65,14.92L14.2,16.47C13.53,16.8 12.79,17 12,17A5,5 0 0,1 7,12C7,11.21 7.2,10.47 7.53,9.8M2,4.27L4.28,6.55L4.73,7C3.08,8.3 1.78,10 1,12C2.73,16.39 7,19.5 12,19.5C13.55,19.5 15.03,19.2 16.38,18.66L16.81,19.08L19.73,22L21,20.73L3.27,3M12,7A5,5 0 0,1 17,12C17,12.64 16.87,13.26 16.64,13.82L19.57,16.75C21.07,15.5 22.27,13.86 23,12C21.27,7.61 17,4.5 12,4.5C10.6,4.5 9.26,4.75 8,5.2L10.17,7.35C10.74,7.13 11.35,7 12,7Z";
const REFRESH_PATH = "M17.65,6.35C16.2,4.9 14.21,4 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20C15.73,20 18.84,17.45 19.73,14H17.65C16.83,16.33 14.61,18 12,18A6,6 0 0,1 6,12A6,6 0 0,1 12,6C13.66,6 15.14,6.69 16.22,7.78L13,11H20V4L17.65,6.35Z";

const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#39;");

const numberOrNull = (value) => {
  if (value === null || value === undefined || value === "" || value === "unknown" || value === "unavailable") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};

const localDate = (value) => {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value ?? ""));
  if (!match) return null;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return Number.isNaN(date.getTime()) ? null : date;
};

const isLate = (assignment) => Number(assignment?.status_id) === 7;

const dayStart = (date = new Date()) => new Date(date.getFullYear(), date.getMonth(), date.getDate());
const dayKey = (date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;

class VeracrossCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = null;
    this._hass = null;
    this._signature = null;
    this._selected = "overview";
    this._expanded = new Set();
    this._showAllRecent = false;
    this._waitingOpen = false;
    this._footerInterval = null;
    this._noticeTimer = null;
    this._upToDate = false;
    this._gradesHidden = true;
    this._active = null;
    this._studentId = null;
    this._students = [];
    this._data = null;
    this._requestKey = null;
    this._generation = 0;
    this._pin = "";
    this._pinError = "";
    this._pinBusy = false;
    this._loadError = "";
    this.shadowRoot.addEventListener("click", (event) => this._handleClick(event));
  }

  static getStubConfig() {
    return {
      account_entity: "sensor.veracross_active_student",
      upcoming_days: 7,
      schedule: ["06:00", "15:00"],
    };
  }

  setConfig(config) {
    if (!config || typeof config.account_entity !== "string" || !config.account_entity.trim()) {
      throw new Error("account_entity is required");
    }
    const upcoming = config.upcoming_days === undefined ? 7 : Number(config.upcoming_days);
    if (!Number.isFinite(upcoming) || upcoming < 0) throw new Error("upcoming_days must be a non-negative number");
    const schedule = config.schedule === undefined ? ["06:00", "15:00"] : config.schedule;
    if (!Array.isArray(schedule) || !schedule.length || schedule.some((time) => !/^([01]\d|2[0-3]):[0-5]\d$/.test(String(time)))) {
      throw new Error("schedule must contain HH:MM times");
    }
    this._config = { account_entity: config.account_entity.trim(), upcoming_days: upcoming, schedule: schedule.map(String) };
    this._active = null;
    this._signature = null;
    if (this.isConnected && this._hass) this._sync();
  }

  set hass(hass) {
    const signature = this._stateSignature(hass);
    const changed = signature !== this._signature;
    this._hass = hass;
    if (!changed) return;
    this._signature = signature;
    if (this.isConnected && this._config) this._sync();
  }

  getCardSize() {
    return 8;
  }

  connectedCallback() {
    this._selected = "overview";
    this._gradesHidden = true;
    this._expanded.clear();
    this._showAllRecent = false;
    this._waitingOpen = false;
    this._signature = this._stateSignature(this._hass);
    if (this._config && this._hass) this._sync();
    if (!this._footerInterval) {
      this._footerInterval = setInterval(() => this._updateFooter(), 60000);
    }
  }

  disconnectedCallback() {
    clearInterval(this._footerInterval);
    this._footerInterval = null;
    this._resetView();
    this._generation++;
    this._active = null;
    this._studentId = null;
    this._students = [];
    this._data = null;
    this._requestKey = null;
    this._pin = "";
    this._pinError = "";
    this._pinBusy = false;
  }

  _resetView() {
    this._selected = "overview";
    this._gradesHidden = true;
    this._expanded.clear();
    this._showAllRecent = false;
    this._waitingOpen = false;
    this._upToDate = false;
    clearTimeout(this._noticeTimer);
    this._noticeTimer = null;
  }

  _summary(hass = this._hass) {
    if (!this._studentId) return null;
    return Object.entries(hass?.states ?? {}).find(([id, entity]) =>
      id.startsWith("sensor.") && id.endsWith("_summary") &&
      String(entity.attributes?.student_id) === this._studentId)?.[1] ?? null;
  }

  _stateSignature(hass) {
    if (!hass || !this._config) return null;
    const account = hass.states?.[this._config.account_entity];
    const summary = this._summary(hass);
    const attrs = summary?.attributes;
    return JSON.stringify([account?.state, account?.attributes?.locked_out_until,
      this._studentId, summary?.state, attrs?.data_version, attrs?.last_success,
      attrs?.refreshing, attrs?.throttled_until]);
  }

  _sync() {
    const active = this._hass.states?.[this._config.account_entity]?.state ?? "none";
    if (active !== this._active) {
      this._generation++;
      this._active = active;
      this._students = [];
      this._studentId = null;
      this._data = null;
      this._requestKey = null;
      this._loadError = "";
      this._pin = "";
      this._pinError = "";
      this._pinBusy = false;
      this._resetView();
      if (active === "all") this._fetchStudents();
      else if (!["none", "unknown", "unavailable"].includes(active)) this._studentId = active;
    }
    this._signature = this._stateSignature(this._hass);
    if (this._studentId) this._fetchStudent();
    this._render();
  }

  async _fetchStudents() {
    const generation = this._generation;
    try {
      const students = await this._hass.callWS({ type: "veracross/students" });
      if (!this.isConnected || generation !== this._generation) return;
      this._students = students;
      this._chooseStudent(students[0]?.student_id ?? null);
      if (!students.length) {
        this._loadError = "No students available.";
        this._render();
      }
    } catch (error) {
      if (!this.isConnected || generation !== this._generation) return;
      this._loadError = "Unable to load students.";
      this._render();
    }
  }

  _chooseStudent(id) {
    if (id != null && String(id) === this._studentId) return;
    this._generation++;
    this._studentId = id == null ? null : String(id);
    this._data = null;
    this._requestKey = null;
    this._loadError = "";
    this._resetView();
    this._sync();
  }

  async _fetchStudent() {
    const studentId = this._studentId;
    const key = JSON.stringify([studentId, this._summary()?.attributes?.data_version]);
    if (key === this._requestKey) return;
    this._requestKey = key;
    const generation = ++this._generation;
    try {
      const data = await this._hass.callWS({ type: "veracross/student", student_id: studentId });
      if (!this.isConnected || generation !== this._generation) return;
      this._data = data;
      this._loadError = "";
      this._expanded.clear();
      if (this._selected !== "overview" && !data.classes[this._selected]) this._selected = "overview";
      this._render();
    } catch (error) {
      if (!this.isConnected || generation !== this._generation) return;
      this._loadError = "Unable to load school data.";
      this._render();
    }
  }

  _classes() {
    return (this._data?.classes ?? []).map((reference, index) => {
      const assignments = (this._data.assignments ?? []).filter(item => String(item.class_id) === String(reference.id));
      const waiting = assignments.filter(item => item.score == null && [0, 6].includes(Number(item.status_id)) &&
        localDate(item.due) && localDate(item.due) < dayStart()).sort((a, b) => String(b.due).localeCompare(String(a.due)));
      return { reference, index, entity: { state: reference.grade, attributes: { ...reference, assignments, waiting } } };
    });
  }

  _grade(entity) {
    const value = numberOrNull(entity?.state);
    const letter = entity?.attributes?.letter;
    const approximate = entity?.attributes?.grade_source === "calculated";
    const tone = value === null ? "none" : value >= 90 ? "green" : value >= 80 ? "blue" : value >= 70 ? "amber" : "red";
    // Overall grades always use two decimal places.
    const percent = value === null ? "—" : `${value.toFixed(2)}%`;
    return { value, letter, approximate, tone, percent };
  }

  _gradePill(entity, large = false) {
    // masked pills use the neutral tone so the colour can't give the grade away
    if (this._gradesHidden) return `<span class="grade-pill none masked${large ? " large" : ""}">****</span>`;
    const grade = this._grade(entity);
    const letter = grade.value === null || grade.letter === null || grade.letter === undefined || grade.letter === "" ? "" : ` <span>${escapeHtml(grade.letter)}</span>`;
    return `<span class="grade-pill ${grade.tone}${large ? " large" : ""}">${grade.value !== null && grade.approximate ? '<span class="approx">≈</span>' : ""}${escapeHtml(grade.percent)}${letter}</span>`;
  }

  _allItems(classItem) {
    const attributes = classItem.entity?.attributes ?? {};
    const assignments = Array.isArray(attributes.assignments) ? attributes.assignments : [];
    const waiting = Array.isArray(attributes.waiting) ? attributes.waiting : [];
    const seen = new Set();
    return [...assignments, ...waiting].filter((item) => {
      const key = String(item?.assignment_id ?? item?.id ?? `${item?.due}|${item?.title}`);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  _listedRows(list, classItems) {
    const rows = [];
    for (const ref of this._data?.[list] ?? []) {
      const classItem = classItems.find(item => String(item.reference.id) === String(ref.class_id));
      const assignment = classItem && this._allItems(classItem).find(item =>
        String(item.assignment_id ?? item.id) === String(ref.assignment_id));
      if (assignment) rows.push({ classItem, assignment });
    }
    return rows;
  }

  _needsAttention(classItems = this._classes()) {
    return this._listedRows("needs_attention", classItems);
  }

  _upcoming(classItems = this._classes()) {
    const today = dayStart();
    const through = new Date(today);
    through.setDate(through.getDate() + this._config.upcoming_days);
    const rows = [];
    for (const classItem of classItems) {
      const assignments = classItem.entity?.attributes?.assignments;
      if (!Array.isArray(assignments)) continue;
      for (const assignment of assignments) {
        const due = localDate(assignment?.due);
        if (assignment?.score === null && due && due >= today && due <= through) rows.push({ classItem, assignment, due });
      }
    }
    return rows.sort((a, b) => a.due - b.due || a.classItem.index - b.classItem.index || String(a.assignment?.title ?? "").localeCompare(String(b.assignment?.title ?? "")));
  }

  _className(classItem) {
    const name = classItem.entity?.attributes?.class_name ?? classItem.reference?.name ?? "Class";
    return name;
  }

  _dayLabel(date) {
    const today = dayStart();
    const difference = Math.round((date - today) / 86400000);
    if (difference === 0) return "Today";
    if (difference === 1) return "Tomorrow";
    return date.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  }

  _shortDate(value) {
    const date = localDate(value);
    return date ? date.toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "—";
  }

  _statusTag(assignment) {
    const statusId = Number(assignment?.status_id);
    if (statusId === 1) return '<span class="tag missing">Missing</span>';
    if (statusId === 7) return '<span class="tag late">Late</span>';
    return assignment?.status ? `<span class="tag">${escapeHtml(assignment.status)}</span>` : "";
  }

  _details(assignment, classItem) {
    const extra = assignment?.extra && typeof assignment.extra === "object" ? assignment.extra : {};
    const assigned = assignment?.assigned ?? extra.assigned ?? extra.assigned_date ?? extra.date_assigned;
    const notes = assignment?.notes ? `<div class="notes">${escapeHtml(assignment.notes)}</div>` : "";
    return `<div class="row-details">
      ${notes}
      ${(this._data?.feedback ?? []).filter(item =>
        String(item.class_id) === String(classItem.reference.id) &&
        String(item.assignment_id) === String(assignment.assignment_id ?? assignment.id)
      ).map(item => `<div class="feedback"><div>${escapeHtml(item.person)} · ${escapeHtml(item.date)}</div><div class="notes">${escapeHtml(item.message)}</div></div>`).join("")}
      <div class="detail-grid">
        <span>Assigned <strong>${escapeHtml(assigned ? (localDate(assigned) ? this._shortDate(assigned) : assigned) : "—")}</strong></span>
        <span>Points <strong>${escapeHtml(assignment?.points ?? "—")}</strong></span>
        <span>Status <strong>${escapeHtml(assignment?.status ?? "—")}</strong></span>
      </div>
    </div>`;
  }

  _assignmentRow(row, key, mode) {
    const { classItem, assignment } = row;
    const open = this._expanded.has(key);
    const className = this._className(classItem);
    let meta = "";
    if (mode === "attention") {
      meta = `<span>${escapeHtml(className)} · ${escapeHtml(this._shortDate(assignment?.due))}</span>${this._statusTag(assignment)}`;
    } else if (mode === "upcoming") {
      meta = `<span>${escapeHtml(className)}</span><span class="tag type">${escapeHtml(assignment?.type ?? "Assignment")}</span>`;
    } else {
      const score = assignment?.score === null || assignment?.score === undefined ? "—" : assignment.score;
      const maximum = assignment?.max === null || assignment?.max === undefined ? "—" : assignment.max;
      const pct = numberOrNull(assignment?.pct);
      meta = `<span>${escapeHtml(this._shortDate(assignment?.due))}</span><span class="score">${escapeHtml(score)}/${escapeHtml(maximum)}${pct === null ? "" : ` · ${escapeHtml(`${Number.isInteger(pct) ? pct : pct.toFixed(1)}%`)}`}</span>${this._statusTag(assignment)}`;
    }
    return `<div class="assignment ${open ? "open" : ""}">
      <button type="button" class="assignment-main" data-action="row" data-row="${escapeHtml(key)}" aria-expanded="${open}">
        <span class="assignment-title">${escapeHtml(assignment?.title ?? "Untitled assignment")}</span>
        <span class="assignment-meta">${meta}</span>
      </button>
      ${open ? this._details(assignment, classItem) : ""}
    </div>`;
  }

  _empty(message) {
    return `<div class="empty">${escapeHtml(message)}</div>`;
  }

  _overview() {
    const classes = this._classes();
    const attention = this._needsAttention(classes);
    const upcoming = this._upcoming(classes);
    const attentionRows = attention.length
      ? attention.map((row, index) => this._assignmentRow(row, `oa-${row.classItem.index}-${index}`, "attention")).join("")
      : this._empty("Nothing needs attention.");

    const groups = new Map();
    for (const row of upcoming) {
      const key = dayKey(row.due);
      if (!groups.has(key)) groups.set(key, { date: row.due, rows: [] });
      groups.get(key).rows.push(row);
    }
    const upcomingRows = groups.size
      ? [...groups.values()].map((group) => `<div class="day-group"><h3>${escapeHtml(this._dayLabel(group.date))}</h3>${group.rows.map((row, index) => this._assignmentRow(row, `ou-${row.classItem.index}-${dayKey(group.date)}-${index}`, "upcoming")).join("")}</div>`).join("")
      : this._empty(`Nothing due in the next ${this._config.upcoming_days} days.`);

    const tiles = classes.length ? classes.map((classItem) => {
      const attentionCount = Number(classItem.reference.needs_attention_count) || 0;
      return `<button type="button" class="grade-tile" data-action="select" data-index="${classItem.index}">
        <span class="tile-name">${escapeHtml(this._className(classItem))}</span>
        ${this._gradePill(classItem.entity, true)}
        ${attentionCount > 0 ? `<span class="tile-alert">${escapeHtml(attentionCount)} need attention</span>` : '<span class="tile-clear">All caught up</span>'}
      </button>`;
    }).join("") : this._empty("No classes are available.");

    return `<div class="view overview">
      <section><h2>Needs attention</h2>${attentionRows}</section>
      <section><h2>Coming up <span>next ${escapeHtml(this._config.upcoming_days)} days</span></h2>${upcomingRows}</section>
      <section><h2>Grades</h2><div class="grade-grid">${tiles}</div></section>
    </div>`;
  }

  _categoryBars(entity) {
    const categories = entity?.attributes?.categories;
    if (!Array.isArray(categories) || !categories.length) return "";
    return `<div class="categories">${categories.map((category) => {
      const average = numberOrNull(category?.avg);
      const width = average === null ? 0 : Math.max(0, Math.min(100, average));
      return `<div class="category">
        <div class="category-line"><strong>${escapeHtml(category?.type ?? "Category")}${numberOrNull(category?.weight) === null ? "" : ` <small>(${escapeHtml(category.weight)}% of grade)</small>`}</strong><span>${escapeHtml(average === null ? "—" : `${Number.isInteger(average) ? average : average.toFixed(1)}%`)} · ${escapeHtml(category?.earned ?? "—")}/${escapeHtml(category?.possible ?? "—")}</span></div>
        <div class="bar"><span style="width:${width}%"></span></div>
      </div>`;
    }).join("")}</div>`;
  }

  _classView(index) {
    const classItem = this._classes()[index];
    if (!classItem?.entity) return `<div class="view">${this._empty("Class data is unavailable.")}</div>`;
    const entity = classItem.entity;
    const attributes = entity.attributes ?? {};
    const attention = this._needsAttention([classItem]);
    const upcoming = this._upcoming([classItem]);
    const byDueDesc = (a, b) => (localDate(b?.due)?.getTime() ?? 0) - (localDate(a?.due)?.getTime() ?? 0);
    const late = this._listedRows("late", [classItem]).map(row => row.assignment);
    const assignments = Array.isArray(attributes.assignments) ? attributes.assignments : [];
    const recent = assignments.filter((item) => item?.score !== null && item?.score !== undefined && !isLate(item))
      .sort(byDueDesc);
    const visibleRecent = this._showAllRecent ? recent : recent.slice(0, 6);
    const waiting = Array.isArray(attributes.waiting) ? attributes.waiting : [];
    const teacher = attributes.teacher ? `<span>${escapeHtml(attributes.teacher)}</span>` : "";
    const period = attributes.period === null || attributes.period === undefined || attributes.period === "" ? "" : `<span>Period ${escapeHtml(attributes.period)}</span>`;
    const weighting = attributes.weighting === null || attributes.weighting === undefined || attributes.weighting === "" ? "" : `<div class="weighting">${escapeHtml(attributes.weighting)}</div>`;

    const attentionRows = attention.length ? attention.map((row, rowIndex) => this._assignmentRow(row, `ca-${index}-${rowIndex}`, "attention")).join("") : this._empty("Nothing needs attention.");
    const upcomingRows = upcoming.length ? upcoming.map((row, rowIndex) => this._assignmentRow(row, `cu-${index}-${rowIndex}`, "upcoming")).join("") : this._empty(`Nothing due in the next ${this._config.upcoming_days} days.`);
    const recentRows = visibleRecent.length ? visibleRecent.map((assignment, rowIndex) => this._assignmentRow({ classItem, assignment }, `cr-${index}-${rowIndex}`, "grade")).join("") : this._empty("No grades yet.");
    const showAll = recent.length > 6 ? `<button type="button" class="text-button" data-action="recent">${this._showAllRecent ? "Show fewer" : `Show all ${escapeHtml(recent.length)}`}</button>` : "";
    const lateRows = late.length ? late.map((assignment, rowIndex) => this._assignmentRow({ classItem, assignment }, `cl-${index}-${rowIndex}`, "grade")).join("") : this._empty("No late assignments.");
    const waitingRows = waiting.length ? waiting.map((assignment, rowIndex) => this._assignmentRow({ classItem, assignment }, `cw-${index}-${rowIndex}`, "grade")).join("") : this._empty("No assignments are waiting for a grade.");

    return `<div class="view class-view">
      <header class="class-header">
        <div><h1>${escapeHtml(this._className(classItem))}</h1><div class="class-subtitle">${teacher}${period}</div>${weighting}</div>
        ${this._gradePill(entity, true)}
      </header>
      ${this._categoryBars(entity)}
      <section><h2>Needs attention</h2>${attentionRows}</section>
      <section><h2>Coming up</h2>${upcomingRows}</section>
      <section><h2>Recent grades</h2>${recentRows}${showAll}</section>
      ${this._data.late_mode === "separate" ? `<section><h2>Late</h2>${lateRows}</section>` : ""}
      <section class="waiting-section">
        <button type="button" class="section-toggle" data-action="waiting" aria-expanded="${this._waitingOpen}">
          <span>Waiting for grade</span><span>${escapeHtml(waiting.length)} ${this._waitingOpen ? "−" : "+"}</span>
        </button>
        ${this._waitingOpen ? waitingRows : ""}
      </section>
    </div>`;
  }

  _chips() {
    const attention = this._classes().reduce((total, item) => total + (Number(item.reference.needs_attention_count) || 0), 0);
    const overviewAlert = attention > 0 ? `<span class="alert-dot">${escapeHtml(attention)}</span>` : "";
    const overview = `<button type="button" class="chip ${this._selected === "overview" ? "selected" : ""}" data-action="select" data-index="overview">Overview${overviewAlert}</button>`;
    const classes = this._classes().map((classItem) => {
      const selected = Number(this._selected) === classItem.index;
      const count = Number(classItem.reference.needs_attention_count) || 0;
      const alert = count > 0 ? `<span class="alert-dot">${escapeHtml(count)}</span>` : "";
      return `<button type="button" class="chip ${selected ? "selected" : ""}" data-action="select" data-index="${classItem.index}">
        <span>${escapeHtml(this._className(classItem))}</span>${this._gradePill(classItem.entity)}${alert}
      </button>`;
    }).join("");
    return `<nav class="chips" aria-label="School views">${overview}${classes}</nav>`;
  }

  _footerHtml() {
    const summary = this._summary();
    const refreshing = summary?.attributes?.refreshing === true;
    const label = refreshing ? "Refreshing…" : this._upToDate ? "Up to date" : "Refresh";
    return `<footer>
      <button type="button" class="refresh" data-action="refresh" ${refreshing ? "disabled" : ""}>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="${REFRESH_PATH}"></path></svg><span>${label}</span>
      </button>
      <button type="button" class="lock" data-action="lock" aria-label="Lock School"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="${LOCK_PATH}"></path></svg></button>
      <div class="footer-meta">${this._footerMetaHtml()}</div>
    </footer>`;
  }

  _footerMetaHtml() {
    const lastSuccess = this._summary()?.attributes?.last_success ?? this._data?.last_success;
    const date = lastSuccess ? new Date(lastSuccess) : null;
    const valid = date && !Number.isNaN(date.getTime());
    const updated = valid ? `Updated ${this._formatTime(date)}` : "Not updated yet";
    const stale = valid && Date.now() - date.getTime() > 13 * 60 * 60 * 1000;
    return `<div>${escapeHtml(updated)}</div><div>Next update ${escapeHtml(this._nextUpdate())}</div>${stale ? '<div class="stale">may be out of date</div>' : ""}`;
  }

  _formatTime(date) {
    return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", hour12: true });
  }

  _nextUpdate() {
    const now = new Date();
    const candidates = this._config.schedule.map((time) => {
      const [hours, minutes] = time.split(":").map(Number);
      const candidate = new Date(now.getFullYear(), now.getMonth(), now.getDate(), hours, minutes);
      if (candidate <= now) candidate.setDate(candidate.getDate() + 1);
      return candidate;
    });
    candidates.sort((a, b) => a - b);
    return this._formatTime(candidates[0]);
  }

  _updateFooter() {
    const footer = this.shadowRoot.querySelector(".footer-meta");
    if (footer) footer.innerHTML = this._footerMetaHtml();
  }

  _render() {
    if (!this._config || !this._hass) return;
    if (["none", "unknown", "unavailable", null].includes(this._active)) {
      this.shadowRoot.innerHTML = `${this._styles()}<ha-card><div class="shell"><div class="student">School</div>${this._keypad()}</div></ha-card>`;
      return;
    }
    const switcher = this._active === "all" ? `<div class="chips" aria-label="Students">${this._students.map(item =>
      `<button type="button" class="chip ${String(item.student_id) === this._studentId ? "selected" : ""}" data-action="student" data-student="${escapeHtml(item.student_id)}">${escapeHtml(item.name)}</button>`).join("")}</div>` : "";
    if (!this._data) {
      this.shadowRoot.innerHTML = `${this._styles()}<ha-card><div class="shell"><div class="student">School</div>${switcher}<main><div class="missing-data">${escapeHtml(this._loadError || "Loading school…")}</div>${this._loadError ? '<button class="text-button" data-action="retry">Try again</button>' : ""}</main>${this._footerHtml()}</div></ha-card>`;
      return;
    }
    const student = this._data.name;
    const content = this._selected === "overview" ? this._overview() : this._classView(Number(this._selected));
    this.shadowRoot.innerHTML = `${this._styles()}<ha-card>
      <div class="shell">
        <div class="student">${student ? `${escapeHtml(student)}’s school` : "School"}<button type="button" class="eye" data-action="eye" aria-label="${this._gradesHidden ? "Show grades" : "Hide grades"}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="${this._gradesHidden ? EYE_OFF_PATH : EYE_PATH}"></path></svg></button></div>
        ${switcher}
        ${this._chips()}
        <main>${this._loadError ? this._empty(this._loadError) + '<button class="text-button" data-action="retry">Try again</button>' : ""}${content}</main>
        ${this._footerHtml()}
      </div>
    </ha-card>`;
  }

  _handleClick(event) {
    const target = event.target.closest("[data-action]");
    if (!target || !this.shadowRoot.contains(target)) return;
    const action = target.dataset.action;
    if (action === "pin") {
      if (this._pinBusy) return;
      const key = target.dataset.key;
      if (key === "C") this._pin = "";
      else if (key === "back") this._pin = this._pin.slice(0, -1);
      else if (/^\d$/.test(key) && this._pin.length < 8) this._pin += key;
      this._pinError = "";
      this._render();
    } else if (action === "enter") {
      this._enterPin();
    } else if (action === "student") {
      this._chooseStudent(target.dataset.student);
    } else if (action === "retry") {
      this._loadError = "";
      this._requestKey = null;
      if (this._studentId) this._sync();
      else this._fetchStudents();
    } else if (action === "select") {
      this._selected = target.dataset.index === "overview" ? "overview" : Number(target.dataset.index);
      this._expanded.clear();
      this._showAllRecent = false;
      this._waitingOpen = false;
      this._render();
    } else if (action === "row") {
      const key = target.dataset.row;
      this._expanded.has(key) ? this._expanded.delete(key) : this._expanded.add(key);
      this._render();
    } else if (action === "recent") {
      this._showAllRecent = !this._showAllRecent;
      this._render();
    } else if (action === "waiting") {
      this._waitingOpen = !this._waitingOpen;
      this._render();
    } else if (action === "refresh") {
      this._refresh();
    } else if (action === "eye") {
      this._gradesHidden = !this._gradesHidden;
      this._render();
    } else if (action === "lock") {
      this._hass.callService("veracross", "lock", {}).catch(() => {});
    }
  }

  _refresh() {
    const summary = this._summary();
    if (!this._studentId || summary?.attributes?.refreshing === true) return;
    const throttledUntil = summary?.attributes?.throttled_until;
    const throttleDate = throttledUntil ? new Date(throttledUntil) : null;
    if (throttleDate && !Number.isNaN(throttleDate.getTime()) && throttleDate > new Date()) {
      this._upToDate = true;
      const label = this.shadowRoot.querySelector(".refresh span");
      if (label) label.textContent = "Up to date";
      clearTimeout(this._noticeTimer);
      this._noticeTimer = setTimeout(() => {
        this._upToDate = false;
        const currentLabel = this.shadowRoot.querySelector(".refresh span");
        if (currentLabel && this._summary()?.attributes?.refreshing !== true) currentLabel.textContent = "Refresh";
        this._noticeTimer = null;
      }, 3000);
      return;
    }
    this._hass.callService("veracross", "refresh", { student_id: this._studentId }).catch(() => {});
  }

  _keypad() {
    return `<div class="keypad"><div class="pin-dots" aria-label="${this._pin.length} digits entered">${"●".repeat(this._pin.length)}${"○".repeat(Math.max(4, this._pin.length) - this._pin.length)}</div>
      <div class="pin-error" role="status">${escapeHtml(this._pinError)}</div>
      <div class="pin-keys">${["1", "2", "3", "4", "5", "6", "7", "8", "9", "C", "0", "back"].map(key => `<button type="button" class="chip" data-action="pin" data-key="${key}" ${this._pinBusy ? "disabled" : ""} aria-label="${key === "back" ? "Backspace" : key === "C" ? "Clear" : key}">${key === "back" ? "⌫" : key}</button>`).join("")}</div>
      <button type="button" class="chip pin-enter" data-action="enter" ${this._pinBusy || this._pin.length < 4 ? "disabled" : ""}>${this._pinBusy ? "Checking…" : "Enter"}</button></div>`;
  }

  async _enterPin() {
    if (this._pinBusy || this._pin.length < 4) return;
    const generation = this._generation;
    const pin = this._pin;
    this._pin = "";
    this._pinBusy = true;
    this._pinError = "";
    this._render();
    try {
      await this._hass.callService("veracross", "enter_pin", { pin }, undefined, false, true);
    } catch (error) {
      if (!this.isConnected || generation !== this._generation) return;
      const message = String(error?.message ?? error);
      const until = this._hass.states?.[this._config.account_entity]?.attributes?.locked_out_until;
      const seconds = until ? Math.max(1, Math.ceil((new Date(until).getTime() - Date.now()) / 1000)) : NaN;
      this._pinError = /locked/i.test(message)
        ? `Too many tries — wait ${Number.isFinite(seconds) ? seconds : Number(message.match(/\d+/)?.[0]) || 60}s`
        : "Wrong PIN";
    } finally {
      if (this.isConnected && generation === this._generation) {
        this._pinBusy = false;
        this._render();
      }
    }
  }

  _styles() {
    return `<style>
      :host { display: block; color: var(--primary-text-color); }
      * { box-sizing: border-box; }
      button { font: inherit; }
      ha-card { background: transparent; color: var(--primary-text-color); box-shadow: none; overflow: hidden; }
      .keypad { width: 280px; margin: 24px auto; text-align: center; }
      .pin-dots { font-size: 30px; letter-spacing: 8px; min-height: 42px; }
      .pin-error { min-height: 40px; padding: 8px 0; color: var(--error-color, #db4437); }
      .pin-keys { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
      .pin-keys .chip, .pin-enter { justify-content: center; min-height: 54px; }
      .pin-enter { width: 100%; margin-top: 14px; }
      .keypad button:disabled { opacity: .5; cursor: default; }
      .feedback { margin-top: 12px; border-top: 1px solid var(--divider-color); padding-top: 12px; }
      .shell { min-height: 540px; display: flex; flex-direction: column; background: color-mix(in srgb, var(--card-background-color) 84%, transparent); border: 1px solid var(--divider-color); border-radius: 22px; overflow: hidden; }
      .student { padding: 18px 22px 4px; color: var(--secondary-text-color); font-size: 14px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; }
      .chips { display: flex; gap: 10px; padding: 14px 18px; overflow-x: auto; scrollbar-width: thin; border-bottom: 1px solid var(--divider-color); }
      .chip, .grade-tile, .assignment-main, .section-toggle, .text-button, .refresh { border: 0; color: var(--primary-text-color); cursor: pointer; }
      .chip { flex: 0 0 auto; min-height: 46px; display: inline-flex; align-items: center; gap: 8px; padding: 7px 14px; border-radius: 24px; background: color-mix(in srgb, var(--card-background-color) 88%, var(--primary-color)); border: 1px solid var(--divider-color); }
      .chip.selected { border-color: var(--primary-color); background: color-mix(in srgb, var(--primary-color) 20%, var(--card-background-color)); }
      .alert-dot { min-width: 22px; height: 22px; display: inline-grid; place-items: center; border-radius: 50%; color: var(--card-background-color); background: var(--error-color, #db4437); font-size: 12px; font-weight: 800; }
      main { flex: 1; padding: 4px 22px 22px; }
      .view { max-width: 1500px; margin: 0 auto; }
      section { margin-top: 24px; }
      h1, h2, h3 { margin: 0; }
      h1 { font-size: 30px; }
      h2 { margin-bottom: 12px; font-size: 21px; }
      h2 span { color: var(--secondary-text-color); font-size: 14px; font-weight: 500; }
      h3 { margin: 17px 2px 8px; color: var(--secondary-text-color); font-size: 15px; text-transform: uppercase; letter-spacing: .05em; }
      .assignment { margin: 7px 0; border: 1px solid var(--divider-color); border-radius: 13px; overflow: hidden; background: color-mix(in srgb, var(--card-background-color) 92%, transparent); }
      .assignment-main { width: 100%; min-height: 58px; padding: 10px 14px; display: flex; align-items: center; justify-content: space-between; gap: 18px; text-align: left; background: transparent; }
      .assignment-title { min-width: 0; font-weight: 650; }
      .assignment-meta { flex: 0 0 auto; display: flex; align-items: center; justify-content: flex-end; gap: 9px; color: var(--secondary-text-color); font-size: 14px; }
      .row-details { padding: 0 14px 14px; border-top: 1px solid var(--divider-color); color: var(--secondary-text-color); }
      .notes { padding: 12px 0; color: var(--primary-text-color); white-space: pre-wrap; line-height: 1.45; }
      .detail-grid { display: flex; flex-wrap: wrap; gap: 12px 28px; padding-top: 11px; font-size: 14px; }
      .detail-grid strong { color: var(--primary-text-color); margin-left: 5px; }
      .tag { display: inline-flex; align-items: center; min-height: 25px; padding: 3px 8px; border-radius: 12px; background: color-mix(in srgb, var(--secondary-text-color) 16%, transparent); color: var(--primary-text-color); font-size: 12px; font-weight: 700; }
      .tag.missing { color: var(--error-color, #db4437); background: color-mix(in srgb, var(--error-color, #db4437) 17%, transparent); }
      .tag.late { color: var(--warning-color, #ffa600); background: color-mix(in srgb, var(--warning-color, #ffa600) 17%, transparent); }
      .tag.type { color: var(--primary-color); background: color-mix(in srgb, var(--primary-color) 13%, transparent); }
      .score { color: var(--primary-text-color); font-weight: 700; }
      .empty { padding: 18px; border: 1px dashed var(--divider-color); border-radius: 13px; color: var(--secondary-text-color); }
      .grade-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }
      .grade-tile { min-height: 132px; padding: 16px; display: flex; flex-direction: column; align-items: flex-start; justify-content: space-between; border-radius: 16px; border: 1px solid var(--divider-color); background: color-mix(in srgb, var(--card-background-color) 91%, transparent); text-align: left; }
      .tile-name { font-size: 17px; font-weight: 700; }
      .tile-alert { color: var(--error-color, #db4437); font-size: 13px; font-weight: 700; }
      .tile-clear { color: var(--secondary-text-color); font-size: 13px; }
      .grade-pill { display: inline-flex; align-items: baseline; gap: 4px; min-height: 28px; padding: 4px 9px; border-radius: 16px; border: 1px solid currentColor; font-weight: 800; line-height: 1; white-space: nowrap; }
      .grade-pill.large { min-height: 44px; padding: 8px 13px; border-radius: 22px; font-size: 24px; }
      .grade-pill .approx { font-size: .75em; }
      .grade-pill.green { color: var(--success-color, #43a047); background: color-mix(in srgb, var(--success-color, #43a047) 14%, transparent); }
      .grade-pill.blue { color: var(--info-color, #039be5); background: color-mix(in srgb, var(--info-color, #039be5) 14%, transparent); }
      .grade-pill.amber { color: var(--warning-color, #ffa600); background: color-mix(in srgb, var(--warning-color, #ffa600) 14%, transparent); }
      .grade-pill.red { color: var(--error-color, #db4437); background: color-mix(in srgb, var(--error-color, #db4437) 14%, transparent); }
      .grade-pill.none { color: var(--secondary-text-color); background: transparent; }
      .class-header { margin-top: 22px; display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
      .class-subtitle { display: flex; flex-wrap: wrap; gap: 8px 20px; margin-top: 7px; color: var(--secondary-text-color); }
      .weighting { margin-top: 7px; color: var(--secondary-text-color); font-size: 14px; }
      .categories { margin-top: 18px; display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px 20px; }
      .category-line { display: flex; justify-content: space-between; gap: 12px; font-size: 14px; }
      .category-line span { color: var(--secondary-text-color); }
      .bar { height: 7px; margin-top: 7px; overflow: hidden; border-radius: 6px; background: color-mix(in srgb, var(--divider-color) 70%, transparent); }
      .bar span { display: block; height: 100%; border-radius: inherit; background: var(--primary-color); }
      .text-button { min-height: 44px; margin-top: 6px; padding: 7px 12px; background: transparent; color: var(--primary-color); font-weight: 700; }
      .waiting-section { border-top: 1px solid var(--divider-color); padding-top: 8px; }
      .section-toggle { width: 100%; min-height: 52px; display: flex; justify-content: space-between; align-items: center; padding: 6px 2px; background: transparent; font-size: 21px; font-weight: 700; text-align: left; }
      footer { position: sticky; bottom: 0; min-height: 72px; display: flex; align-items: center; justify-content: space-between; gap: 18px; padding: 12px 20px; border-top: 1px solid var(--divider-color); background: color-mix(in srgb, var(--card-background-color) 94%, transparent); }
      .refresh { min-height: 44px; padding: 8px 14px; display: inline-flex; align-items: center; gap: 9px; border-radius: 23px; background: color-mix(in srgb, var(--primary-color) 16%, var(--card-background-color)); color: var(--primary-color); font-weight: 750; }
      .refresh:disabled { color: var(--secondary-text-color); cursor: default; }
      .refresh svg { width: 22px; height: 22px; fill: currentColor; }
      .lock { width: 44px; height: 44px; padding: 0; display: inline-grid; place-items: center; border: 0; border-radius: 50%; background: transparent; color: var(--secondary-text-color); cursor: pointer; }
      .student { display: flex; align-items: center; gap: 4px; }
      .eye { width: 44px; height: 44px; margin: -12px 0 -12px 2px; padding: 0; display: inline-grid; place-items: center; border: 0; border-radius: 50%; background: transparent; color: var(--secondary-text-color); cursor: pointer; }
      .eye svg { width: 20px; height: 20px; fill: currentColor; }
      .grade-pill.masked { letter-spacing: .12em; }
      .lock svg { width: 20px; height: 20px; fill: currentColor; }
      .footer-meta { margin-left: auto; }
      .footer-meta { text-align: right; color: var(--secondary-text-color); font-size: 13px; line-height: 1.35; }
      .stale { color: var(--warning-color, #ffa600); font-weight: 700; }
      .missing-data { padding: 24px; color: var(--secondary-text-color); }
      @media (max-width: 650px) {
        .shell { border-radius: 16px; }
        main { padding-inline: 13px; }
        .chips { padding-inline: 12px; }
        .assignment-main { align-items: flex-start; flex-direction: column; gap: 6px; }
        .assignment-meta { width: 100%; justify-content: flex-start; flex-wrap: wrap; }
        .class-header { align-items: flex-start; }
        footer { padding-inline: 13px; }
      }
    </style>`;
  }
}

if (!customElements.get("veracross-card")) customElements.define("veracross-card", VeracrossCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "veracross-card",
  name: "Veracross Card",
  description: "School grades, assignments, and upcoming work.",
});
