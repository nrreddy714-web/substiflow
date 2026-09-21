from datetime import datetime, date
from database import get_connection


# ============================================================
# DAY / PERIOD HELPERS
# ============================================================

DAY_CODES = {
    "Monday": "MON",
    "Tuesday": "TUE",
    "Wednesday": "WED",
    "Thursday": "THU",
    "Friday": "FRI",
    "Saturday": "SAT",
}


def normalize_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def get_day_code(assignment_date):
    return DAY_CODES[normalize_date(assignment_date).strftime("%A")]


def period_block(period_no):
    return "MORNING" if int(period_no) <= 3 else "AFTERNOON"


# ============================================================
# LAB / OCCUPANCY HELPERS
# ============================================================


LAB_LABELS = {
    "CA LAB",
    "PST LAB",
    "OAT LAB",
    "DBMS LAB",
    "AI LAB",
    "ML LAB",
    "DIP LAB",
}


def _normalize_campus(value):
    return str(value or "").strip().lower()


def is_lab_row(period_type, period_label):
    period_type = str(period_type or "").strip().upper()
    period_label = str(period_label or "").strip().upper()
    return period_type == "LAB" or period_label in LAB_LABELS or period_label.endswith(" LAB")


def get_lab_duration(cursor, class_id, day_code, start_period, campus, period_label):
    """Return the actual number of periods occupied by a lab.

    OAT LAB is campus-specific: Main Campus = 3 periods (P1-P3),
    Women's Campus = 2 periods (P2-P3). Other known BCA labs are 2 periods.
    The class timetable is used as a fallback for any future lab label.
    """
    label = str(period_label or "").strip().upper()
    campus_text = _normalize_campus(campus)

    if label == "OAT LAB":
        return 2 if "women" in campus_text else 3

    if label in LAB_LABELS:
        return 2

    cursor.execute(
        """
        SELECT duration_hours
        FROM class_timetable
        WHERE class_id = %s
          AND day_of_week = %s
          AND period_no = %s
        LIMIT 1
        """,
        (class_id, day_code, start_period),
    )
    row = cursor.fetchone()
    if row and row.get("duration_hours"):
        try:
            return max(1, int(round(float(row["duration_hours"]))))
        except (TypeError, ValueError):
            pass

    return 2


def _get_teacher_day_occupancy(cursor, teacher_id, day_code):
    """Return occupied periods for a teacher, expanding merged lab rows.

    A lab may be stored as one row (for example P5 with duration 2) or as
    multiple consecutive rows (for example P1 and P2). Both representations
    are treated as the same occupied time block and never double-counted.
    """
    cursor.execute(
        """
        SELECT
            timetable_id,
            class_id,
            day_of_week,
            period_no,
            campus,
            period_type,
            period_label
        FROM timetable
        WHERE teacher_id = %s
          AND day_of_week = %s
        ORDER BY period_no, timetable_id
        """,
        (teacher_id, day_code),
    )
    rows = cursor.fetchall()

    occupancy = {}
    lab_groups = {}

    for row in rows:
        period_no = int(row["period_no"])
        if not is_lab_row(row["period_type"], row["period_label"]):
            occupancy.setdefault(
                period_no,
                {
                    "campus": _normalize_campus(row["campus"]),
                    "timetable_id": row["timetable_id"],
                },
            )
            continue

        key = (
            int(row["class_id"]),
            _normalize_campus(row["campus"]),
            str(row["period_label"] or "").strip().upper(),
        )
        lab_groups.setdefault(key, []).append(row)

    for rows_in_lab in lab_groups.values():
        start_period = min(int(r["period_no"]) for r in rows_in_lab)
        first = min(rows_in_lab, key=lambda r: int(r["period_no"]))
        duration = get_lab_duration(
            cursor,
            int(first["class_id"]),
            day_code,
            start_period,
            first["campus"],
            first["period_label"],
        )

        for occupied_period in range(start_period, min(6, start_period + duration - 1) + 1):
            occupancy.setdefault(
                occupied_period,
                {
                    "campus": _normalize_campus(first["campus"]),
                    "timetable_id": first["timetable_id"],
                },
            )

    return occupancy


def _teacher_occupies_period(cursor, teacher_id, day_code, period_no):
    occupancy = _get_teacher_day_occupancy(cursor, teacher_id, day_code)
    return int(period_no) in occupancy


# ============================================================
# COMMON FACULTY / CAMPUS MOVEMENT
# ============================================================

def _teacher_occupied_periods(cursor, teacher_id, day_code):
    """Return every period occupied by a teacher, expanding lab duration."""
    cursor.execute(
        """
        SELECT
            tt.period_no,
            COALESCE(ct.duration_hours, 1) AS duration_hours
        FROM timetable tt
        LEFT JOIN class_timetable ct
          ON ct.class_id = tt.class_id
         AND ct.day_of_week = tt.day_of_week
         AND ct.period_no = tt.period_no
        WHERE tt.teacher_id = %s
          AND tt.day_of_week = %s
        """,
        (teacher_id, day_code),
    )

    occupied = set()

    for row in cursor.fetchall():
        start_period = int(row["period_no"])

        try:
            duration = max(
                1,
                int(round(float(row["duration_hours"] or 1)))
            )
        except (TypeError, ValueError):
            duration = 1

        for period in range(
            start_period,
            start_period + duration
        ):
            if 1 <= period <= 6:
                occupied.add(period)

    return occupied


def common_teacher_has_womens_class_in_block(
    cursor,
    teacher_id,
    day_code,
    period_no
):
    """
    Common faculty cannot switch between Women's Campus
    and Main Campus inside the same morning/afternoon block.

    Lab duration is expanded so merged lab rows protect
    every period occupied by that lab.
    """

    block_start = 1 if int(period_no) <= 3 else 4
    block_end = 3 if int(period_no) <= 3 else 6

    cursor.execute(
        """
        SELECT
            tt.period_no,
            COALESCE(ct.duration_hours, 1) AS duration_hours
        FROM timetable tt
        LEFT JOIN class_timetable ct
          ON ct.class_id = tt.class_id
         AND ct.day_of_week = tt.day_of_week
         AND ct.period_no = tt.period_no
        WHERE tt.teacher_id = %s
          AND tt.day_of_week = %s
          AND LOWER(COALESCE(tt.campus, '')) LIKE 'women%%'
        """,
        (teacher_id, day_code),
    )

    for row in cursor.fetchall():
        start_period = int(row["period_no"])

        try:
            duration = max(
                1,
                int(round(float(row["duration_hours"] or 1)))
            )
        except (TypeError, ValueError):
            duration = 1

        end_period = start_period + duration - 1

        if start_period <= block_end and end_period >= block_start:
            return True

    return False
# ============================================================
# AFFECTED PERIODS
# ============================================================

def get_affected_periods(absent_teacher_id, assignment_date):
    """Get all affected Main Campus BCA periods, expanding lab durations."""
    assignment_date = normalize_date(assignment_date)
    day_code = get_day_code(assignment_date)

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT
                tt.timetable_id,
                tt.teacher_id AS absent_teacher_id,
                tt.class_id,
                c.class_name,
                COALESCE(tt.subject_id, ct.subject_id) AS subject_id,
                s.subject_name,
                tt.day_of_week,
                tt.period_no,
                tt.campus,
                tt.period_type,
                tt.period_label
            FROM timetable tt
            JOIN classes c
              ON tt.class_id = c.class_id
            LEFT JOIN class_timetable ct
              ON ct.class_id = tt.class_id
             AND ct.day_of_week = tt.day_of_week
             AND ct.period_no = tt.period_no
            LEFT JOIN subjects s
              ON s.subject_id = COALESCE(tt.subject_id, ct.subject_id)
            WHERE tt.teacher_id = %s
              AND tt.day_of_week = %s
              AND LOWER(COALESCE(tt.campus, '')) LIKE 'main%%'
              AND LOWER(COALESCE(c.class_name, '')) LIKE '%%bca%%'
            ORDER BY tt.period_no, tt.timetable_id
            """,
            (absent_teacher_id, day_code),
        )
        rows = cursor.fetchall()

        affected = {}
        lab_groups = {}

        for row in rows:
            period_no = int(row["period_no"])
            if not is_lab_row(row["period_type"], row["period_label"]):
                affected[(int(row["class_id"]), period_no)] = dict(row)
                continue

            key = (
                int(row["class_id"]),
                _normalize_campus(row["campus"]),
                str(row["period_label"] or "").strip().upper(),
            )
            lab_groups.setdefault(key, []).append(row)

        for rows_in_lab in lab_groups.values():
            start_row = min(rows_in_lab, key=lambda r: int(r["period_no"]))
            start_period = int(start_row["period_no"])
            duration = get_lab_duration(
                cursor,
                int(start_row["class_id"]),
                day_code,
                start_period,
                start_row["campus"],
                start_row["period_label"],
            )

            for occupied_period in range(
                start_period, min(6, start_period + duration - 1) + 1
            ):
                affected[(int(start_row["class_id"]), occupied_period)] = dict(start_row)
                affected[(int(start_row["class_id"]), occupied_period)]["period_no"] = occupied_period

        return sorted(affected.values(), key=lambda r: (r["period_no"], r["class_id"]))
    finally:
        cursor.close()
        connection.close()


# ============================================================
# CANDIDATES
# ============================================================

def get_rejected_teacher_ids(assignment):
    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT substitute_teacher_id
            FROM substitute_assignments
            WHERE assignment_date = %s
              AND period_no = %s
              AND class_id = %s
              AND absent_teacher_id = %s
              AND status = 'REJECTED'
              AND substitute_teacher_id IS NOT NULL
            """,
            (
                assignment["assignment_date"],
                assignment["period_no"],
                assignment["class_id"],
                assignment["absent_teacher_id"],
            ),
        )
        return {row["substitute_teacher_id"] for row in cursor.fetchall()}
    finally:
        cursor.close()
        connection.close()


def _get_free_period_count(cursor, teacher_id, day_code):
    """Return the number of free periods (out of P1-P6) on a day.

    Occupancy includes expanded multi-period labs, so a two-period lab
    consumes two periods for availability and priority purposes.
    """
    occupied = _get_teacher_day_occupancy(cursor, teacher_id, day_code)
    return max(0, 6 - len(occupied))


def find_candidates(assignment, extra_rejected=None):
    """Find valid teachers and prioritize those with more free periods."""
    assignment_date = normalize_date(assignment["assignment_date"])
    period_no = int(assignment["period_no"])
    absent_teacher_id = assignment["absent_teacher_id"]
    day_code = get_day_code(assignment_date)

    rejected = get_rejected_teacher_ids(assignment)
    if extra_rejected:
        rejected.update(extra_rejected)

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT
                t.teacher_id,
                t.teacher_name,
                t.department,
                t.designation,
                t.campus,
                1 AS subject_eligible,
                (
                    SELECT COUNT(*)
                    FROM timetable tt2
                    WHERE tt2.teacher_id = t.teacher_id
                      AND tt2.day_of_week = %s
                ) AS daily_classes,
                (
                    SELECT COUNT(*)
                    FROM substitute_assignments sa2
                    WHERE sa2.substitute_teacher_id = t.teacher_id
                      AND sa2.assignment_date = %s
                      AND sa2.status IN ('PENDING','APPROVED','ASSIGNED')
                ) AS substitutions_today,
                (
                    SELECT COUNT(*)
                    FROM substitute_assignments sa3
                    WHERE sa3.substitute_teacher_id = t.teacher_id
                      AND sa3.status IN ('APPROVED','ASSIGNED')
                ) AS total_substitutions
            FROM teachers t
            WHERE t.is_active = TRUE
              AND t.is_substitute_eligible = TRUE
              AND t.teacher_id <> %s
            ORDER BY t.teacher_name
            """,
            (day_code, assignment_date, absent_teacher_id),
        )
        teachers = cursor.fetchall()

        candidates = []
        for teacher in teachers:
            if teacher["teacher_id"] in rejected:
                continue

            ok, _ = _availability_with_cursor(
                cursor,
                teacher["teacher_id"],
                assignment_date,
                period_no,
                absent_teacher_id=absent_teacher_id,
                exclude_assignment_id=assignment.get("assignment_id"),
            )
            if not ok:
                continue

            teacher["free_periods"] = _get_free_period_count(
                cursor, teacher["teacher_id"], day_code
            )
            teacher["priority_score"] = (
                int(teacher["free_periods"]) * 100
                + max(0, 50 - int(teacher["substitutions_today"]) * 10)
                + max(0, 10 - min(int(teacher["total_substitutions"]), 10))
            )
            candidates.append(teacher)

        candidates.sort(
            key=lambda t: (
                -int(t["free_periods"]),
                int(t["substitutions_today"]),
                int(t["total_substitutions"]),
                t["teacher_name"].lower(),
            )
        )
        return candidates
    finally:
        cursor.close()
        connection.close()


def _availability_with_cursor(
    cursor,
    teacher_id,
    assignment_date,
    period_no,
    absent_teacher_id=None,
    exclude_assignment_id=None,
):
    day_code = get_day_code(assignment_date)

    if _teacher_occupies_period(cursor, teacher_id, day_code, period_no):
        return False, "Timetable conflict"

    cursor.execute(
        """
        SELECT COUNT(*) AS total
        FROM teacher_absence
        WHERE teacher_id = %s
          AND absence_date = %s
          AND status IN ('PENDING','APPROVED')
        """,
        (teacher_id, assignment_date),
    )
    if cursor.fetchone()["total"]:
        return False, "Absence conflict"

    cursor.execute(
        """
        SELECT COUNT(*) AS total
        FROM teacher_unavailability
        WHERE teacher_id = %s
          AND unavailable_date = %s
          AND period_no = %s
          AND status IN ('PENDING','APPROVED')
        """,
        (teacher_id, assignment_date, period_no),
    )
    if cursor.fetchone()["total"]:
        return False, "Unavailability conflict"

    if exclude_assignment_id is None:
        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM substitute_assignments
            WHERE substitute_teacher_id = %s
              AND assignment_date = %s
              AND period_no = %s
              AND status IN ('PENDING','APPROVED','ASSIGNED')
            """,
            (teacher_id, assignment_date, period_no),
        )
    else:
        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM substitute_assignments
            WHERE substitute_teacher_id = %s
              AND assignment_date = %s
              AND period_no = %s
              AND assignment_id <> %s
              AND status IN ('PENDING','APPROVED','ASSIGNED')
            """,
            (teacher_id, assignment_date, period_no, exclude_assignment_id),
        )
    if cursor.fetchone()["total"]:
        return False, "Substitution conflict"

    if common_teacher_has_womens_class_in_block(
        cursor, teacher_id, day_code, period_no
    ):
        return False, "Women's Campus block conflict"

    return True, "Available"


def is_teacher_available(
    teacher_id,
    assignment_date,
    period_no,
    absent_teacher_id=None,
    exclude_assignment_id=None,
):
    """Public availability check used by app.py and manual/reallocation flows.

    Reuses the single shared availability implementation above.
    """
    assignment_date = normalize_date(assignment_date)
    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        return _availability_with_cursor(
            cursor,
            teacher_id,
            assignment_date,
            int(period_no),
            absent_teacher_id=absent_teacher_id,
            exclude_assignment_id=exclude_assignment_id,
        )
    finally:
        cursor.close()
        connection.close()


# ============================================================
# SCORE / REASON
# ============================================================

def calculate_score(candidate):
    """Calculate a visible priority score led by free-period availability."""
    if "priority_score" in candidate:
        return float(candidate["priority_score"])

    free_periods = int(candidate.get("free_periods", 0))
    substitutions_today = int(candidate.get("substitutions_today", 0))
    total_substitutions = int(candidate.get("total_substitutions", 0))

    score = free_periods * 100
    score += max(0, 50 - substitutions_today * 10)
    score += max(0, 10 - min(total_substitutions, 10))
    return float(score)


def build_reason(candidate):
    return (
        "Available teacher. Subject knowledge is treated equally for all teachers. "
        f"Free periods today: {candidate.get('free_periods', 0)}. "
        f"Daily classes: {candidate['daily_classes']}. "
        f"Substitutions today: {candidate['substitutions_today']}. "
        "Higher free-period availability receives higher priority. "
        "Recommendation requires HOD approval."
    )


# ============================================================
# CREATE RECOMMENDATIONS
# ============================================================

def create_recommendations(absent_teacher_id, assignment_date):
    """
    Create PENDING recommendations for every affected Main Campus BCA period.

    Rules:
    1. Any available eligible substitute may be selected.
    2. Teachers with more free periods receive higher priority.
    3. Lower current substitution load breaks ties.
    4. If nobody is available, create ADMIN_ACTION.
    5. Recommendations are never automatically final.
    """
    assignment_date = normalize_date(assignment_date)
    affected = get_affected_periods(absent_teacher_id, assignment_date)

    if not affected:
        print("No affected Main Campus BCA periods found.")
        return {"pending": 0, "admin_action": 0, "skipped": 0}

    created_pending = 0
    created_admin_action = 0
    skipped = 0

    # Teachers reserved for a specific date + period while this batch is
    # being generated. This prevents one substitute from being recommended
    # for two different absent teachers in the same period.
    reserved_by_slot = {}

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        for period in affected:
            # Avoid creating duplicate active recommendations for the same
            # absent teacher/date/class/period.
            cursor.execute(
                """
                SELECT assignment_id
                FROM substitute_assignments
                WHERE assignment_date = %s
                  AND period_no = %s
                  AND class_id = %s
                  AND absent_teacher_id = %s
                  AND status IN ('PENDING','APPROVED','ASSIGNED','ADMIN_ACTION')
                LIMIT 1
                """,
                (
                    assignment_date,
                    period["period_no"],
                    period["class_id"],
                    absent_teacher_id,
                ),
            )
            existing = cursor.fetchone()
            if existing:
                # If an active recommendation already exists for this slot,
                # reserve that teacher so another absence at the same period
                # cannot receive the same substitute.
                cursor.execute(
                    """
                    SELECT substitute_teacher_id
                    FROM substitute_assignments
                    WHERE assignment_date = %s
                      AND period_no = %s
                      AND class_id = %s
                      AND absent_teacher_id = %s
                      AND status IN ('PENDING','APPROVED','ASSIGNED')
                    LIMIT 1
                    """,
                    (
                        assignment_date,
                        period["period_no"],
                        period["class_id"],
                        absent_teacher_id,
                    ),
                )
                existing_assignment = cursor.fetchone()
                if existing_assignment and existing_assignment["substitute_teacher_id"] is not None:
                    slot = (assignment_date, int(period["period_no"]))
                    reserved_by_slot.setdefault(slot, set()).add(
                        int(existing_assignment["substitute_teacher_id"])
                    )

                skipped += 1
                continue

            assignment = {
                "assignment_date": assignment_date,
                "period_no": period["period_no"],
                "class_id": period["class_id"],
                "subject_id": period["subject_id"],
                "absent_teacher_id": absent_teacher_id,
            }

            slot = (assignment_date, int(period["period_no"]))
            reserved_teachers = reserved_by_slot.setdefault(slot, set())

            # Exclude teachers already reserved for another class in this
            # exact date/period. find_candidates still applies all normal
            # availability, timetable, absence, unavailability and campus
            # movement rules.
            candidates = find_candidates(
                assignment,
                extra_rejected=reserved_teachers,
            )

            if candidates:
                candidate = candidates[0]
                reserved_teachers.add(int(candidate["teacher_id"]))
                score = calculate_score(candidate)
                reason = build_reason(candidate)

                cursor.execute(
                    """
                    INSERT INTO substitute_assignments
                    (
                        assignment_date,
                        period_no,
                        class_id,
                        subject_id,
                        absent_teacher_id,
                        substitute_teacher_id,
                        score,
                        status,
                        reason
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,'PENDING',%s)
                    """,
                    (
                        assignment_date,
                        period["period_no"],
                        period["class_id"],
                        period["subject_id"],
                        absent_teacher_id,
                        candidate["teacher_id"],
                        score,
                        reason,
                    ),
                )
                created_pending += 1
            else:
                reason = (
                    "No eligible/available teacher found after checking "
                    "timetable, absence, unavailability, existing substitutions "
                    "and campus movement. Admin must manually arrange this period."
                )
                cursor.execute(
                    """
                    INSERT INTO substitute_assignments
                    (
                        assignment_date,
                        period_no,
                        class_id,
                        subject_id,
                        absent_teacher_id,
                        substitute_teacher_id,
                        score,
                        status,
                        reason
                    )
                    VALUES (%s,%s,%s,%s,%s,NULL,0,'ADMIN_ACTION',%s)
                    """,
                    (
                        assignment_date,
                        period["period_no"],
                        period["class_id"],
                        period["subject_id"],
                        absent_teacher_id,
                        reason,
                    ),
                )
                created_admin_action += 1

        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()

    print(
        f"Recommendations: PENDING={created_pending}, "
        f"ADMIN_ACTION={created_admin_action}, SKIPPED={skipped}"
    )

    return {
        "pending": created_pending,
        "admin_action": created_admin_action,
        "skipped": skipped,
    }


# ============================================================
# TEST
# ============================================================

if __name__ == "__main__":
    print("Algorithm module loaded successfully.")
