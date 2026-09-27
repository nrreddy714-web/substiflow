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
# COMMON FACULTY / CAMPUS MOVEMENT
# ============================================================

def common_teacher_has_womens_class_in_block(cursor, teacher_id, day_code, period_no):
    """
    Common faculty cannot switch between Women's Campus and Main Campus
    inside the same morning/afternoon block.
    """
    block_start = 1 if int(period_no) <= 3 else 4
    block_end = 3 if int(period_no) <= 3 else 6

    cursor.execute(
        """
        SELECT COUNT(*) AS total
        FROM timetable
        WHERE teacher_id = %s
          AND day_of_week = %s
          AND period_no BETWEEN %s AND %s
          AND LOWER(COALESCE(campus, '')) LIKE 'women%%'
        """,
        (teacher_id, day_code, block_start, block_end),
    )
    return cursor.fetchone()["total"] > 0


def is_teacher_available(
    teacher_id,
    assignment_date,
    period_no,
    absent_teacher_id=None,
    exclude_assignment_id=None,
):
    """Return (True, reason) if a teacher can take the period."""
    assignment_date = normalize_date(assignment_date)
    day_code = get_day_code(assignment_date)

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT teacher_id, teacher_name, campus,
                   is_active, is_substitute_eligible
            FROM teachers
            WHERE teacher_id = %s
            LIMIT 1
            """,
            (teacher_id,),
        )
        teacher = cursor.fetchone()

        if not teacher:
            return False, "Teacher not found."
        if not teacher["is_active"]:
            return False, "Teacher account is inactive."
        if not teacher["is_substitute_eligible"]:
            return False, "Teacher is not eligible for substitution."
        if absent_teacher_id is not None and int(teacher_id) == int(absent_teacher_id):
            return False, "Absent teacher cannot be selected."

        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM timetable
            WHERE teacher_id = %s
              AND day_of_week = %s
              AND period_no = %s
            """,
            (teacher_id, day_code, period_no),
        )
        if cursor.fetchone()["total"] > 0:
            return False, "Teacher already has a timetable class in this period."

        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM teacher_absence
            WHERE teacher_id = %s
              AND absence_date = %s
              AND status IN ('PENDING', 'APPROVED')
            """,
            (teacher_id, assignment_date),
        )
        if cursor.fetchone()["total"] > 0:
            return False, "Teacher is absent on this date."

        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM teacher_unavailability
            WHERE teacher_id = %s
              AND unavailable_date = %s
              AND period_no = %s
              AND status IN ('PENDING', 'APPROVED')
            """,
            (teacher_id, assignment_date, period_no),
        )
        if cursor.fetchone()["total"] > 0:
            return False, "Teacher is unavailable for this period."

        if exclude_assignment_id is None:
            cursor.execute(
                """
                SELECT COUNT(*) AS total
                FROM substitute_assignments
                WHERE substitute_teacher_id = %s
                  AND assignment_date = %s
                  AND period_no = %s
                  AND status IN ('PENDING', 'APPROVED', 'ASSIGNED')
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
                  AND status IN ('PENDING', 'APPROVED', 'ASSIGNED')
                """,
                (teacher_id, assignment_date, period_no, exclude_assignment_id),
            )
        if cursor.fetchone()["total"] > 0:
            return False, "Teacher already has another substitution in this period."

        # Campus movement rule applies to teachers whose timetable shows
        # Women's Campus activity (the COMMON faculty).
        # A teacher whose regular campus is Women's Campus only is not a
        # Main Campus substitute. Common faculty are detected from their
        # timetable because they appear in both campus schedules.
        cursor.execute(
            """
            SELECT
                LOWER(COALESCE(campus, '')) AS campus
            FROM teachers
            WHERE teacher_id = %s
            LIMIT 1
            """,
            (teacher_id,),
        )
        campus_row = cursor.fetchone()
        campus = (campus_row["campus"] if campus_row else "").strip()

        cursor.execute(
            """
            SELECT
                SUM(CASE WHEN LOWER(COALESCE(campus, '')) LIKE 'main%%' THEN 1 ELSE 0 END) AS main_count,
                SUM(CASE WHEN LOWER(COALESCE(campus, '')) LIKE 'women%%' THEN 1 ELSE 0 END) AS women_count
            FROM timetable
            WHERE teacher_id = %s
            """,
            (teacher_id,),
        )
        campus_counts = cursor.fetchone()
        main_count = int(campus_counts["main_count"] or 0)
        women_count = int(campus_counts["women_count"] or 0)

        common_teacher = main_count > 0 and women_count > 0
        women_only = women_count > 0 and main_count == 0

        if women_only or campus == "women campus" and not common_teacher:
            return False, "Teacher belongs to Women's Campus."

        if common_teacher_has_womens_class_in_block(
            cursor, teacher_id, day_code, period_no
        ):
            return False, "Common faculty is at Women's Campus in this block."

        return True, "Available"
    finally:
        cursor.close()
        connection.close()


# ============================================================
# AFFECTED PERIODS
# ============================================================

def get_affected_periods(absent_teacher_id, assignment_date):
    """Get all Main Campus BCA periods taught by the absent teacher."""
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

                -- Some imported teacher timetable rows may not have a
                -- subject_id. In that case, recover the subject from the
                -- class timetable for the same class/day/period.
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
            ORDER BY tt.period_no
            """,
            (absent_teacher_id, day_code),
        )
        return cursor.fetchall()
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


def find_candidates(assignment, extra_rejected=None):
    """Find every available teacher using availability and workload rules."""
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

            candidates.append(teacher)

        # Balanced ranking: fewer regular classes first, then fewer
        # substitutions today, then lower historical substitution count.
        candidates.sort(
            key=lambda t: (
                int(t["daily_classes"]),
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

    cursor.execute(
        """
        SELECT COUNT(*) AS total
        FROM timetable
        WHERE teacher_id = %s
          AND day_of_week = %s
          AND period_no = %s
        """,
        (teacher_id, day_code, period_no),
    )
    if cursor.fetchone()["total"]:
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


# ============================================================
# SCORE / REASON
# ============================================================

def calculate_score(candidate):
    """Calculate a workload/fairness score for the recommendation."""
    score = 0

    # Fewer regular classes today is better.
    score += max(0, 100 - int(candidate["daily_classes"]) * 10)

    # Fewer substitutions already assigned today is better.
    score += max(0, 50 - int(candidate["substitutions_today"]) * 10)

    # Small fairness bonus for teachers with lower historical workload.
    score += max(0, 10 - min(int(candidate["total_substitutions"]), 10))

    return float(score)


def build_reason(candidate, batch_assignments=1):
    """Build a transparent reason using availability and workload balancing."""
    return (
        "Available teacher selected using availability and workload balancing. "
        f"Daily classes: {candidate['daily_classes']}. "
        f"Substitutions today: {candidate['substitutions_today']}. "
        f"Assignments in this batch: {batch_assignments}. "
        "Recommendation requires HOD approval."
    )


# ============================================================
# CREATE RECOMMENDATIONS
# ============================================================

def create_recommendations(absent_teacher_id, assignment_date):
    """
    Create PENDING recommendations for every affected Main Campus BCA period.

    Rules:
    1. Any active, substitution-eligible and available teacher may be selected.
    2. Workload/fairness is used for ranking.
    3. For every period, prefer the currently available teacher with the fewest
       assignments already given in this absence batch. Reuse occurs only when
       the availability/workload comparison makes that teacher the best current
       choice.
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

    # Number of substitutions already allocated in this absence batch.
    # At every period, the algorithm prefers an available teacher with the
    # lowest batch-assignment count. This guarantees a balanced distribution:
    # if Teacher A and Teacher B are both free, and both can cover the period,
    # the teacher with fewer assignments in this batch is preferred.
    batch_assignment_counts = {}

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

            # Get every teacher who is actually available for this exact slot.
            # We do NOT exclude a teacher just because they were used earlier
            # in this absence batch. Instead, we rank all currently available
            # teachers by how many periods they have already received in THIS
            # batch. This is the key fairness rule.
            candidates = find_candidates(
                assignment,
                extra_rejected=reserved_teachers,
            )

            if candidates:
                # First minimize assignments already given in this batch.
                # Then use the normal workload/fairness criteria. Therefore,
                # when A and B are both free, the one with 0 batch assignments
                # is selected before the one with 1 or more. A teacher is reused
                # only when all better-balanced available choices have already
                # received the same or a higher batch count, or the teacher is
                # the only available choice for that period.
                candidates.sort(
                    key=lambda t: (
                        int(batch_assignment_counts.get(int(t["teacher_id"]), 0)),
                        int(t["daily_classes"]),
                        int(t["substitutions_today"]),
                        int(t["total_substitutions"]),
                        t["teacher_name"].lower(),
                    )
                )

                candidate = candidates[0]
                candidate_id = int(candidate["teacher_id"])
                reserved_teachers.add(candidate_id)
                batch_assignment_counts[candidate_id] = (
                    batch_assignment_counts.get(candidate_id, 0) + 1
                )
                score = calculate_score(candidate)
                reason = build_reason(
                    candidate,
                    batch_assignments=batch_assignment_counts[candidate_id],
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
                    "No available teacher found after checking "
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
