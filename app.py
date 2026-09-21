from flask import Flask, render_template, request, redirect, url_for, session
from database import get_connection
from werkzeug.security import check_password_hash

from algorithm import (
    create_recommendations,
    find_candidates,
    is_teacher_available,
    get_affected_periods,
    calculate_score,
    build_reason,
)


app = Flask(__name__)
app.secret_key = "smart-faculty-substitute-system-secret-key"


# ============================================================
# HELPERS
# ============================================================

DAY_CODES = {
    "Monday": "MON",
    "Tuesday": "TUE",
    "Wednesday": "WED",
    "Thursday": "THU",
    "Friday": "FRI",
    "Saturday": "SAT",
}


def get_day_code(assignment_date):
    """Return the timetable day code for Monday-Saturday.

    Sunday is outside the college timetable, so return None instead of
    raising KeyError when the dashboard is opened on Sunday.
    """
    return DAY_CODES.get(assignment_date.strftime("%A"))


def admin_only():
    return session.get("role") == "ADMIN"


def teacher_only():
    # Admin/HOD users who are also teaching faculty can access teacher features.
    return session.get("role") in ("TEACHER", "ADMIN")


# ============================================================
# LAB TIMETABLE HELPERS
# ============================================================

LAB_SLOTS = {
    "PST LAB": (1, 2),
    "CA LAB": (4, 5),
    "DBMS LAB": (1, 2),
    "AI LAB": (5, 6),
    "ML LAB": (5, 6),
    "DIP LAB": (5, 6),
    "OAT LAB": None,  # Main Campus P1-P3; Women Campus P2-P3
}


def _lab_periods_for_row(row):
    """Return all occupied periods for a lab row."""
    label = str(row.get("display_subject") or row.get("subject_name") or "").strip().upper()
    if not label:
        return []

    if label == "OAT LAB":
        campus = str(row.get("display_campus") or row.get("campus") or "").strip().lower()
        return [2, 3] if "women" in campus else [1, 2, 3]

    slots = LAB_SLOTS.get(label)
    return list(slots) if slots else []


def _expand_lab_rows(rows):
    """Expand multi-period lab rows so every occupied period is visible.

    This is display/availability normalization only; it does not change the
    underlying timetable database. Existing occupied rows always win.
    """
    normalized = [dict(row) for row in rows]
    occupied = {(row.get("day_of_week"), int(row.get("period_no"))) for row in normalized}

    additions = []
    for row in normalized:
        if str(row.get("period_type") or "").upper() != "LAB" and "LAB" not in str(row.get("display_subject") or row.get("subject_name") or "").upper():
            continue

        for period in _lab_periods_for_row(row):
            key = (row.get("day_of_week"), period)
            if key in occupied:
                continue

            copy_row = dict(row)
            copy_row["period_no"] = period
            copy_row["lab_expanded"] = True
            additions.append(copy_row)
            occupied.add(key)

    normalized.extend(additions)
    normalized.sort(
        key=lambda r: (
            ["MON", "TUE", "WED", "THU", "FRI", "SAT"].index(r.get("day_of_week"))
            if r.get("day_of_week") in ["MON", "TUE", "WED", "THU", "FRI", "SAT"]
            else 99,
            int(r.get("period_no") or 0),
        )
    )
    return normalized


# ============================================================
# LOGIN
# ============================================================

@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    if not username or not password:
        return render_template(
            "login.html",
            error="Please enter username and password."
        )

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT
                teacher_id,
                teacher_name,
                username,
                password_hash,
                role,
                is_active
            FROM teachers
            WHERE username = %s
            LIMIT 1
            """,
            (username,),
        )
        user = cursor.fetchone()
    finally:
        cursor.close()
        connection.close()

    if not user:
        return render_template(
            "login.html",
            error="Invalid username or password."
        )

    if not user["is_active"]:
        return render_template(
            "login.html",
            error="This account is inactive."
        )

    if not user["password_hash"]:
        return render_template(
            "login.html",
            error="Password is not configured for this account."
        )

    try:
        password_correct = check_password_hash(
            user["password_hash"],
            password
        )
    except Exception:
        password_correct = False

    if not password_correct:
        return render_template(
            "login.html",
            error="Invalid username or password."
        )

    session.clear()
    session["teacher_id"] = user["teacher_id"]
    session["teacher_name"] = user["teacher_name"]
    session["username"] = user["username"]
    session["role"] = user["role"]

    if user["role"] == "ADMIN":
        return redirect(url_for("admin_dashboard"))

    return redirect(url_for("teacher_dashboard"))


# ============================================================
# LOGOUT
# ============================================================

@app.route("/about-us")
def about_us():
    return render_template("about_us.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ============================================================
# ADMIN DASHBOARD
# ============================================================

@app.route("/admin")
def admin_dashboard():
    if not admin_only():
        return redirect(url_for("login"))

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT
                sa.assignment_id,
                sa.assignment_date,
                sa.period_no,
                c.class_name,
                COALESCE(s.subject_name, sa.reason) AS subject_name,
                absent.teacher_name AS absent_teacher,
                substitute.teacher_name AS substitute_teacher,
                sa.score,
                sa.status,
                sa.reason
            FROM substitute_assignments sa
            LEFT JOIN classes c
                ON sa.class_id = c.class_id
            LEFT JOIN subjects s
                ON sa.subject_id = s.subject_id
            LEFT JOIN teachers absent
                ON sa.absent_teacher_id = absent.teacher_id
            LEFT JOIN teachers substitute
                ON sa.substitute_teacher_id = substitute.teacher_id
            WHERE sa.status = 'PENDING'
            ORDER BY sa.assignment_date, sa.period_no, sa.assignment_id
            """
        )
        pending = cursor.fetchall()

        cursor.execute(
            """
            SELECT
                sa.assignment_id,
                sa.assignment_date,
                sa.period_no,
                c.class_name,
                COALESCE(s.subject_name, 'Subject') AS subject_name,
                absent.teacher_name AS absent_teacher,
                sa.reason,
                sa.status
            FROM substitute_assignments sa
            LEFT JOIN classes c
                ON sa.class_id = c.class_id
            LEFT JOIN subjects s
                ON sa.subject_id = s.subject_id
            LEFT JOIN teachers absent
                ON sa.absent_teacher_id = absent.teacher_id
            WHERE sa.status = 'ADMIN_ACTION'
            ORDER BY sa.assignment_date, sa.period_no, sa.assignment_id
            """
        )
        admin_actions = cursor.fetchall()

        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM substitute_assignments
            WHERE status = 'ASSIGNED'
            """
        )
        assigned_count = cursor.fetchone()["total"]


        cursor.execute(
            """
            SELECT
                sa.assignment_id,
                sa.assignment_date,
                sa.period_no,
                c.class_name,
                COALESCE(s.subject_name, 'Subject') AS subject_name,
                absent.teacher_name AS absent_teacher,
                substitute.teacher_name AS substitute_teacher,
                sa.status,
                sa.reason
            FROM substitute_assignments sa
            LEFT JOIN classes c
                ON sa.class_id = c.class_id
            LEFT JOIN subjects s
                ON sa.subject_id = s.subject_id
            LEFT JOIN teachers absent
                ON sa.absent_teacher_id = absent.teacher_id
            LEFT JOIN teachers substitute
                ON sa.substitute_teacher_id = substitute.teacher_id
            WHERE sa.status IN ('ASSIGNED', 'APPROVED')
            ORDER BY sa.assignment_date DESC, sa.period_no, sa.assignment_id
            """
        )
        assigned = cursor.fetchall()

        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM substitute_assignments
            WHERE status = 'REJECTED'
            """
        )
        rejected_count = cursor.fetchone()["total"]

    finally:
        cursor.close()
        connection.close()

    return render_template(
        "admin_dashboard.html",
        pending=pending,
        admin_actions=admin_actions,
        assigned_count=assigned_count,
        rejected_count=rejected_count,
        assigned=assigned,
    )



# ============================================================
# ADMIN - FACULTY TIMETABLES
# ============================================================

def get_faculty_timetables(cursor):
    """Return one Monday-Saturday/P1-P6 grid for every active teacher."""
    cursor.execute(
        """
        SELECT
            t.teacher_id,
            t.teacher_name,
            t.department,
            tt.day_of_week,
            tt.period_no,
            COALESCE(
                NULLIF(tt.class_label, ''),
                c.class_name,
                '—'
            ) AS display_class,
            COALESCE(
                NULLIF(tt.period_label, ''),
                s.subject_name,
                CASE
                    WHEN tt.period_type = 'GAMES' THEN 'PET'
                    ELSE 'Free / Other'
                END
            ) AS display_subject,
            COALESCE(tt.campus, '—') AS display_campus,
            COALESCE(tt.period_type, 'ACADEMIC') AS period_type
        FROM teachers t
        LEFT JOIN timetable tt
            ON t.teacher_id = tt.teacher_id
        LEFT JOIN classes c
            ON tt.class_id = c.class_id
        LEFT JOIN subjects s
            ON tt.subject_id = s.subject_id
        WHERE t.is_active = 1
        ORDER BY
            t.teacher_name,
            FIELD(
                tt.day_of_week,
                'MON','TUE','WED','THU','FRI','SAT'
            ),
            tt.period_no
        """
    )
    faculty_rows = cursor.fetchall()

    faculty_map = {}
    for row in faculty_rows:
        teacher_id = row["teacher_id"]
        if teacher_id not in faculty_map:
            faculty_map[teacher_id] = {
                "teacher_id": teacher_id,
                "teacher_name": row["teacher_name"],
                "department": row["department"],
                "rows": [],
            }

        if row["day_of_week"] is not None and row["period_no"] is not None:
            faculty_map[teacher_id]["rows"].append(row)

    faculty_timetables = []
    for faculty in faculty_map.values():
        expanded_rows = _expand_lab_rows(faculty["rows"])
        grid = {}
        for row in expanded_rows:
            day = row.get("day_of_week")
            period = row.get("period_no")
            if day in DAY_CODES.values() and period is not None:
                grid.setdefault(day, {})[int(period)] = row

        faculty["grid"] = grid
        faculty["display_rows"] = expanded_rows
        faculty_timetables.append(faculty)

    faculty_timetables.sort(
        key=lambda faculty: str(faculty.get("teacher_name") or "").lower()
    )
    return faculty_timetables


@app.route("/admin/faculty-timetables")
def admin_faculty_timetables():
    if not admin_only():
        return redirect(url_for("login"))

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        faculty_timetables = get_faculty_timetables(cursor)
    finally:
        cursor.close()
        connection.close()

    return render_template(
        "faculty_timetables.html",
        faculty_timetables=faculty_timetables,
    )


# ============================================================
# APPROVE SUBSTITUTE RECOMMENDATION
# ============================================================

@app.route("/admin/approve/<int:assignment_id>", methods=["GET", "POST"])
def approve_assignment(assignment_id):
    if not admin_only():
        return redirect(url_for("login"))

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT *
            FROM substitute_assignments
            WHERE assignment_id = %s
              AND status = 'PENDING'
            LIMIT 1
            """,
            (assignment_id,),
        )
        assignment = cursor.fetchone()
    finally:
        cursor.close()
        connection.close()

    if not assignment:
        return redirect(url_for("admin_dashboard"))

    teacher_id = assignment["substitute_teacher_id"]

    available, _ = is_teacher_available(
        teacher_id=teacher_id,
        assignment_date=assignment["assignment_date"],
        period_no=assignment["period_no"],
        absent_teacher_id=assignment["absent_teacher_id"],
        exclude_assignment_id=assignment_id,
    )

    if not available:
        # The teacher became unavailable after the recommendation was made.
        # Reuse the rejection/reallocation flow.
        return redirect(url_for("reject_assignment", assignment_id=assignment_id))

    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            UPDATE substitute_assignments
            SET
                status = 'ASSIGNED',
                reason = CONCAT(
                    COALESCE(reason, ''),
                    ' | Approved by HOD'
                )
            WHERE assignment_id = %s
              AND status = 'PENDING'
            """,
            (assignment_id,),
        )
        connection.commit()
    finally:
        cursor.close()
        connection.close()

    return redirect(url_for("admin_dashboard"))


# ============================================================
# REJECT SUBSTITUTE RECOMMENDATION
# ============================================================

@app.route("/admin/reject/<int:assignment_id>", methods=["GET", "POST"])
def reject_assignment(assignment_id):
    if not admin_only():
        return redirect(url_for("login"))

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT *
            FROM substitute_assignments
            WHERE assignment_id = %s
              AND status = 'PENDING'
            LIMIT 1
            """,
            (assignment_id,),
        )
        assignment = cursor.fetchone()
    finally:
        cursor.close()
        connection.close()

    if not assignment:
        return redirect(url_for("admin_dashboard"))

    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            UPDATE substitute_assignments
            SET
                status = 'REJECTED',
                reason = CONCAT(
                    COALESCE(reason, ''),
                    ' | Rejected by HOD'
                )
            WHERE assignment_id = %s
              AND status = 'PENDING'
            """,
            (assignment_id,),
        )
        connection.commit()
    finally:
        cursor.close()
        connection.close()

    # Find the next best available teacher. The algorithm also ignores all
    # previously rejected teachers for the same period.
    candidates = find_candidates(assignment)

    connection = get_connection()
    cursor = connection.cursor()

    try:
        if candidates:
            candidate = candidates[0]

            score = calculate_score(candidate)
            eligibility = "YES"

            reason = (
                "Next recommendation after rejection. "
                "Subject knowledge is treated equally for all substitute teachers. "
                f"Free periods today: {candidate.get('free_periods', 0)}. "
                f"Daily classes: {candidate['daily_classes']}. "
                f"Substitutions today: {candidate['substitutions_today']}. "
                "Higher free-period availability receives higher priority. "
                "Requires HOD approval."
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
                    assignment["assignment_date"],
                    assignment["period_no"],
                    assignment["class_id"],
                    assignment["subject_id"],
                    assignment["absent_teacher_id"],
                    candidate["teacher_id"],
                    score,
                    reason,
                ),
            )
        else:
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
                    assignment["assignment_date"],
                    assignment["period_no"],
                    assignment["class_id"],
                    assignment["subject_id"],
                    assignment["absent_teacher_id"],
                    "No available teacher after rejection. "
                    "Admin must manually arrange this period.",
                ),
            )

        connection.commit()
    finally:
        cursor.close()
        connection.close()

    return redirect(url_for("admin_dashboard"))


# ============================================================
# MANUAL ADMIN ASSIGNMENT
# ============================================================

@app.route("/admin/manual/<int:assignment_id>", methods=["GET", "POST"])
def manual_assignment(assignment_id):
    if not admin_only():
        return redirect(url_for("login"))

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT
                sa.*,
                c.class_name,
                COALESCE(s.subject_name, 'Subject') AS subject_name,
                absent.teacher_name AS absent_teacher
            FROM substitute_assignments sa
            LEFT JOIN classes c
                ON sa.class_id = c.class_id
            LEFT JOIN subjects s
                ON sa.subject_id = s.subject_id
            LEFT JOIN teachers absent
                ON sa.absent_teacher_id = absent.teacher_id
            WHERE sa.assignment_id = %s
              AND sa.status = 'ADMIN_ACTION'
            LIMIT 1
            """,
            (assignment_id,),
        )
        assignment = cursor.fetchone()
    finally:
        cursor.close()
        connection.close()

    if not assignment:
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        selected_teacher_id = request.form.get("teacher_id")

        if not selected_teacher_id:
            return redirect(
                url_for("manual_assignment", assignment_id=assignment_id)
            )

        available, _ = is_teacher_available(
            teacher_id=int(selected_teacher_id),
            assignment_date=assignment["assignment_date"],
            period_no=assignment["period_no"],
            absent_teacher_id=assignment["absent_teacher_id"],
            exclude_assignment_id=assignment_id,
        )

        if not available:
            return redirect(
                url_for("manual_assignment", assignment_id=assignment_id)
            )

        connection = get_connection()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                UPDATE substitute_assignments
                SET
                    substitute_teacher_id = %s,
                    status = 'ASSIGNED',
                    score = 0,
                    reason = 'Manually assigned and approved by HOD'
                WHERE assignment_id = %s
                  AND status = 'ADMIN_ACTION'
                """,
                (int(selected_teacher_id), assignment_id),
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()

        return redirect(url_for("admin_dashboard"))

    available_teachers = find_candidates(assignment)

    return render_template(
        "manual_assignment.html",
        assignment=assignment,
        available_teachers=available_teachers,
    )


# ============================================================
# ADMIN - TEACHER ABSENCE
# ============================================================

@app.route("/admin/absence", methods=["GET", "POST"])
def admin_absence():
    if not admin_only():
        return redirect(url_for("login"))

    if request.method == "POST":
        teacher_id = request.form.get("teacher_id")
        absence_date = request.form.get("absence_date")
        reason = request.form.get("reason", "").strip()

        if not teacher_id or not absence_date:
            return redirect(url_for("admin_absence"))

        connection = get_connection()
        cursor = connection.cursor(dictionary=True)

        try:
            cursor.execute(
                """
                SELECT absence_id
                FROM teacher_absence
                WHERE teacher_id = %s
                  AND absence_date = %s
                  AND status IN ('PENDING','APPROVED')
                LIMIT 1
                """,
                (teacher_id, absence_date),
            )
            existing = cursor.fetchone()

            if not existing:
                cursor.execute(
                    """
                    INSERT INTO teacher_absence
                    (teacher_id, absence_date, reason, status)
                    VALUES (%s,%s,%s,'APPROVED')
                    """,
                    (teacher_id, absence_date, reason),
                )
                connection.commit()
        finally:
            cursor.close()
            connection.close()

        # Generate all affected-period recommendations after saving absence.
        try:
            result = create_recommendations(int(teacher_id), absence_date)
            print("Recommendation result:", result)
        except Exception as error:
            print("Recommendation generation error:", error)

        return redirect(url_for("admin_absence"))

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT teacher_id, teacher_name, department
            FROM teachers
            WHERE is_active = TRUE
            ORDER BY teacher_name
            """
        )
        teachers = cursor.fetchall()

        cursor.execute(
            """
            SELECT
                a.absence_id,
                t.teacher_name,
                a.absence_date,
                a.reason,
                a.status
            FROM teacher_absence a
            JOIN teachers t
              ON a.teacher_id = t.teacher_id
            ORDER BY a.absence_date DESC, a.absence_id DESC
            """
        )
        absences = cursor.fetchall()
    finally:
        cursor.close()
        connection.close()

    return render_template(
        "absence.html",
        teachers=teachers,
        absences=absences,
    )


# ============================================================
# TEACHER - PERIOD UNAVAILABILITY
# ============================================================

@app.route("/teacher/unavailability", methods=["GET", "POST"])
def teacher_unavailability():
    if not teacher_only():
        return redirect(url_for("login"))

    teacher_id = session.get("teacher_id")

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        if request.method == "POST":
            unavailable_date = request.form.get("unavailable_date")
            period_no = request.form.get("period_no")
            reason = request.form.get("reason", "").strip()

            if unavailable_date and period_no:
                cursor.execute(
                    """
                    SELECT unavailability_id
                    FROM teacher_unavailability
                    WHERE teacher_id = %s
                      AND unavailable_date = %s
                      AND period_no = %s
                      AND status IN ('PENDING','APPROVED')
                    LIMIT 1
                    """,
                    (teacher_id, unavailable_date, period_no),
                )
                existing = cursor.fetchone()

                if not existing:
                    cursor.execute(
                        """
                        INSERT INTO teacher_unavailability
                        (
                            teacher_id,
                            unavailable_date,
                            period_no,
                            reason,
                            status
                        )
                        VALUES (%s,%s,%s,%s,'PENDING')
                        """,
                        (
                            teacher_id,
                            unavailable_date,
                            period_no,
                            reason,
                        ),
                    )
                    connection.commit()

            return redirect(url_for("teacher_unavailability"))

        cursor.execute(
            """
            SELECT
                unavailable_date,
                period_no,
                reason,
                status
            FROM teacher_unavailability
            WHERE teacher_id = %s
            ORDER BY unavailable_date DESC, period_no
            """,
            (teacher_id,),
        )
        requests_list = cursor.fetchall()
    finally:
        cursor.close()
        connection.close()

    return render_template(
        "unavailability.html",
        requests=requests_list,
    )


# ============================================================
# ADMIN - UNAVAILABILITY REQUESTS
# ============================================================

@app.route("/admin/unavailability")
def admin_unavailability():
    if not admin_only():
        return redirect(url_for("login"))

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT
                tu.unavailability_id,
                tu.unavailable_date,
                tu.period_no,
                tu.reason,
                tu.status,
                t.teacher_name,
                t.department
            FROM teacher_unavailability tu
            JOIN teachers t
              ON tu.teacher_id = t.teacher_id
            WHERE tu.status = 'PENDING'
            ORDER BY tu.unavailable_date, tu.period_no, tu.unavailability_id
            """
        )
        requests_list = cursor.fetchall()
    finally:
        cursor.close()
        connection.close()

    return render_template(
        "admin_unavailability.html",
        requests=requests_list,
    )


# ============================================================
# APPROVE / REJECT UNAVAILABILITY
# ============================================================

@app.route("/admin/unavailability/approve/<int:request_id>", methods=["POST"])
def approve_unavailability(request_id):
    if not admin_only():
        return redirect(url_for("login"))

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            UPDATE teacher_unavailability
            SET status = 'APPROVED'
            WHERE unavailability_id = %s
              AND status = 'PENDING'
            """,
            (request_id,),
        )
        connection.commit()
    finally:
        cursor.close()
        connection.close()

    return redirect(url_for("admin_unavailability"))


@app.route("/admin/unavailability/reject/<int:request_id>", methods=["POST"])
def reject_unavailability(request_id):
    if not admin_only():
        return redirect(url_for("login"))

    connection = get_connection()
    cursor = connection.cursor()

    try:
        cursor.execute(
            """
            UPDATE teacher_unavailability
            SET status = 'REJECTED'
            WHERE unavailability_id = %s
              AND status = 'PENDING'
            """,
            (request_id,),
        )
        connection.commit()
    finally:
        cursor.close()
        connection.close()

    return redirect(url_for("admin_unavailability"))


# ============================================================
# TEACHER DASHBOARD
# ============================================================

@app.route("/teacher")
def teacher_dashboard():
    if not teacher_only():
        return redirect(url_for("login"))

    teacher_id = session.get("teacher_id")

    from datetime import date

    today = date.today()
    today_code = get_day_code(today)

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT
                teacher_id,
                teacher_name,
                username,
                department,
                designation,
                campus,
                is_substitute_eligible,
                is_active,
                role
            FROM teachers
            WHERE teacher_id = %s
            LIMIT 1
            """,
            (teacher_id,),
        )
        teacher = cursor.fetchone()

        cursor.execute(
            """
            SELECT
                sa.assignment_id,
                sa.assignment_date,
                sa.period_no,
                COALESCE(c.class_name, 'Class') AS class_name,
                COALESCE(s.subject_name, 'Subject') AS subject_name,
                absent.teacher_name AS absent_teacher,
                COALESCE(tt.campus, 'Main Campus') AS campus,
                sa.status,
                sa.reason
            FROM substitute_assignments sa
            LEFT JOIN classes c
              ON sa.class_id = c.class_id
            LEFT JOIN subjects s
              ON sa.subject_id = s.subject_id
            LEFT JOIN teachers absent
              ON sa.absent_teacher_id = absent.teacher_id
            LEFT JOIN timetable tt
              ON tt.teacher_id = sa.absent_teacher_id
             AND tt.class_id = sa.class_id
             AND tt.period_no = sa.period_no
             AND tt.day_of_week = %s
            WHERE sa.substitute_teacher_id = %s
              AND sa.assignment_date = %s
              AND sa.status = 'ASSIGNED'
            ORDER BY sa.period_no
            """,
            (today_code, teacher_id, today),
        )
        today_substitutions = cursor.fetchall()

        cursor.execute(
            """
            SELECT
                sa.assignment_id,
                sa.assignment_date,
                sa.period_no,
                COALESCE(c.class_name, 'Class') AS class_name,
                COALESCE(s.subject_name, 'Subject') AS subject_name,
                absent.teacher_name AS absent_teacher,
                COALESCE(tt.campus, 'Main Campus') AS campus,
                sa.status,
                sa.reason
            FROM substitute_assignments sa
            LEFT JOIN classes c
              ON sa.class_id = c.class_id
            LEFT JOIN subjects s
              ON sa.subject_id = s.subject_id
            LEFT JOIN teachers absent
              ON sa.absent_teacher_id = absent.teacher_id
            LEFT JOIN timetable tt
              ON tt.teacher_id = sa.absent_teacher_id
             AND tt.class_id = sa.class_id
             AND tt.period_no = sa.period_no
             AND tt.day_of_week = CASE DAYOFWEEK(sa.assignment_date)
                    WHEN 2 THEN 'MON'
                    WHEN 3 THEN 'TUE'
                    WHEN 4 THEN 'WED'
                    WHEN 5 THEN 'THU'
                    WHEN 6 THEN 'FRI'
                    WHEN 7 THEN 'SAT'
                    ELSE NULL
                 END
            WHERE sa.substitute_teacher_id = %s
              AND sa.assignment_date > %s
              AND sa.status = 'ASSIGNED'
            ORDER BY sa.assignment_date, sa.period_no
            LIMIT 30
            """,
            (teacher_id, today),
        )
        upcoming_substitutions = cursor.fetchall()

        # Keep the original substitutions collection for compatibility with
        # older templates or code that may still use this variable.
        cursor.execute(
            """
            SELECT
                sa.assignment_date,
                sa.period_no,
                c.class_name,
                COALESCE(s.subject_name, 'Subject') AS subject_name,
                sa.status,
                sa.reason
            FROM substitute_assignments sa
            LEFT JOIN classes c
              ON sa.class_id = c.class_id
            LEFT JOIN subjects s
              ON sa.subject_id = s.subject_id
            WHERE sa.substitute_teacher_id = %s
              AND sa.status = 'ASSIGNED'
            ORDER BY sa.assignment_date, sa.period_no
            """,
            (teacher_id,),
        )
        substitutions = cursor.fetchall()
    finally:
        cursor.close()
        connection.close()

    if not teacher:
        session.clear()
        return redirect(url_for("login"))

    return render_template(
        "teacher_dashboard.html",
        teacher=teacher,
        substitutions=substitutions,
        today_substitutions=today_substitutions,
        upcoming_substitutions=upcoming_substitutions,
    )


# ============================================================
# TEACHER - TODAY'S OPERATIONS
# ============================================================

@app.route("/teacher/today")
def teacher_today():
    if not teacher_only():
        return redirect(url_for("login"))

    from datetime import date

    teacher_id = session.get("teacher_id")
    selected_date = date.today()
    day_code = get_day_code(selected_date)

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT
                tt.period_no,
                tt.day_of_week,
                COALESCE(
                    NULLIF(tt.class_label, ''),
                    c.class_name,
                    'Class'
                ) AS class_name,
                COALESCE(
                    NULLIF(tt.period_label, ''),
                    s.subject_name,
                    CASE
                        WHEN tt.period_type = 'GAMES' THEN 'Games / PET'
                        ELSE 'Subject'
                    END
                ) AS subject_name,
                COALESCE(tt.campus, 'Main Campus') AS campus,
                COALESCE(tt.period_type, 'ACADEMIC') AS period_type
            FROM timetable tt
            LEFT JOIN classes c
                ON tt.class_id = c.class_id
            LEFT JOIN subjects s
                ON tt.subject_id = s.subject_id
            WHERE tt.teacher_id = %s
              AND tt.day_of_week = %s
            ORDER BY tt.period_no
            """,
            (teacher_id, day_code),
        )
        regular_classes = cursor.fetchall()

        # Expand lab occupancy for today's teacher view.
        regular_classes = _expand_lab_rows(regular_classes)

        cursor.execute(
            """
            SELECT
                sa.assignment_id,
                sa.period_no,
                COALESCE(c.class_name, 'Class') AS class_name,
                COALESCE(s.subject_name, 'Subject') AS subject_name,
                absent.teacher_name AS absent_teacher,
                COALESCE(tt.campus, 'Main Campus') AS campus,
                sa.status
            FROM substitute_assignments sa
            LEFT JOIN classes c
                ON sa.class_id = c.class_id
            LEFT JOIN subjects s
                ON sa.subject_id = s.subject_id
            LEFT JOIN teachers absent
                ON sa.absent_teacher_id = absent.teacher_id
            LEFT JOIN timetable tt
                ON tt.teacher_id = sa.absent_teacher_id
               AND tt.class_id = sa.class_id
               AND tt.period_no = sa.period_no
               AND tt.day_of_week = %s
            WHERE sa.substitute_teacher_id = %s
              AND sa.assignment_date = %s
              AND sa.status = 'ASSIGNED'
            ORDER BY sa.period_no
            """,
            (day_code, teacher_id, selected_date),
        )
        today_substitutions = cursor.fetchall()
    finally:
        cursor.close()
        connection.close()

    return render_template(
        "teacher_today.html",
        selected_date=selected_date.isoformat(),
        regular_classes=regular_classes,
        today_substitutions=today_substitutions,
    )


# ============================================================
# TEACHER - MY TIMETABLE
# ============================================================

@app.route("/teacher/timetable")
def teacher_timetable():
    if not teacher_only():
        return redirect(url_for("login"))

    teacher_id = session.get("teacher_id")

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT
                tt.day_of_week,
                tt.period_no,

                COALESCE(
                    NULLIF(tt.class_label, ''),
                    c.class_name,
                    '—'
                ) AS display_class,

                COALESCE(
                    NULLIF(tt.period_label, ''),
                    s.subject_name,
                    CASE
                        WHEN tt.period_type = 'GAMES' THEN 'Games / PET'
                        ELSE 'Free / Other'
                    END
                ) AS display_subject,

                COALESCE(tt.campus, 'Main Campus') AS display_campus,
                COALESCE(tt.period_type, 'ACADEMIC') AS period_type

            FROM timetable tt

            LEFT JOIN classes c
                ON tt.class_id = c.class_id

            LEFT JOIN subjects s
                ON tt.subject_id = s.subject_id

            WHERE tt.teacher_id = %s

            ORDER BY
                FIELD(
                    tt.day_of_week,
                    'MON','TUE','WED','THU','FRI','SAT'
                ),
                tt.period_no
            """,
            (teacher_id,),
        )

        timetable_rows = cursor.fetchall()

    finally:
        cursor.close()
        connection.close()

    timetable_rows = _expand_lab_rows(timetable_rows)

    day_names = {
        "MON": "Monday",
        "TUE": "Tuesday",
        "WED": "Wednesday",
        "THU": "Thursday",
        "FRI": "Friday",
        "SAT": "Saturday",
    }

    timetable = {}

    for row in timetable_rows:
        day = row["day_of_week"]
        period = row["period_no"]

        timetable.setdefault(day, {})
        timetable[day][period] = row

    return render_template(
        "timetable.html",
        timetable=timetable,
        day_names=day_names,
        periods=range(1, 7),
        teacher_name=session.get("teacher_name"),
    )


# ============================================================
# ADMIN - SUBSTITUTION HISTORY / AUDIT
# ============================================================

@app.route("/admin/history")
def admin_history():
    if not admin_only():
        return redirect(url_for("login"))

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT
                sa.assignment_id,
                sa.assignment_date,
                sa.period_no,
                COALESCE(c.class_name, '—') AS class_name,
                COALESCE(s.subject_name, 'Subject') AS subject_name,
                COALESCE(absent.teacher_name, '—') AS absent_teacher,
                COALESCE(substitute.teacher_name, 'Admin / Manual') AS substitute_teacher,
                sa.status,
                sa.score,
                sa.reason,
                sa.created_at
            FROM substitute_assignments sa
            LEFT JOIN classes c
                ON sa.class_id = c.class_id
            LEFT JOIN subjects s
                ON sa.subject_id = s.subject_id
            LEFT JOIN teachers absent
                ON sa.absent_teacher_id = absent.teacher_id
            LEFT JOIN teachers substitute
                ON sa.substitute_teacher_id = substitute.teacher_id
            WHERE sa.status IN ('ASSIGNED', 'APPROVED', 'REJECTED', 'ADMIN_ACTION')
            ORDER BY sa.assignment_date DESC, sa.period_no DESC, sa.assignment_id DESC
            """
        )
        history = cursor.fetchall()
    finally:
        cursor.close()
        connection.close()

    return render_template(
        "history.html",
        history=history,
    )




# ============================================================
# ADMIN - REPORTS & ANALYTICS
# ============================================================

@app.route("/admin/reports")
def admin_reports():
    if not admin_only():
        return redirect(url_for("login"))

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT
                COUNT(*) AS total_records,
                COALESCE(SUM(status='PENDING'),0) AS pending_count,
                COALESCE(SUM(status IN ('ASSIGNED','APPROVED')),0) AS assigned_count,
                COALESCE(SUM(status='REJECTED'),0) AS rejected_count,
                COALESCE(SUM(status='ADMIN_ACTION'),0) AS admin_action_count
            FROM substitute_assignments
        """)
        substitution_stats = cursor.fetchone()

        cursor.execute("""
            SELECT COUNT(*) AS total_absences
            FROM teacher_absence
            WHERE status IN ('PENDING','APPROVED')
        """)
        total_absences = cursor.fetchone()["total_absences"]

        cursor.execute("""
            SELECT t.teacher_name, t.department,
                   COUNT(sa.assignment_id) AS substitution_count
            FROM teachers t
            LEFT JOIN substitute_assignments sa
              ON sa.substitute_teacher_id=t.teacher_id
             AND sa.status IN ('ASSIGNED','APPROVED')
            WHERE t.is_active=1
            GROUP BY t.teacher_id,t.teacher_name,t.department
            ORDER BY substitution_count DESC,t.teacher_name
        """)
        teacher_load = cursor.fetchall()

        cursor.execute("""
            SELECT assignment_date,
                   COUNT(*) AS total,
                   COALESCE(SUM(status IN ('ASSIGNED','APPROVED')),0) AS assigned,
                   COALESCE(SUM(status='PENDING'),0) AS pending,
                   COALESCE(SUM(status='REJECTED'),0) AS rejected,
                   COALESCE(SUM(status='ADMIN_ACTION'),0) AS admin_action
            FROM substitute_assignments
            GROUP BY assignment_date
            ORDER BY assignment_date DESC
        """)
        daily_activity = cursor.fetchall()

        cursor.execute("""
            SELECT sa.assignment_date,sa.period_no,
                   COALESCE(c.class_name,'—') AS class_name,
                   COALESCE(s.subject_name,'Subject') AS subject_name,
                   absent.teacher_name AS absent_teacher,
                   substitute.teacher_name AS substitute_teacher
            FROM substitute_assignments sa
            LEFT JOIN classes c ON sa.class_id=c.class_id
            LEFT JOIN subjects s ON sa.subject_id=s.subject_id
            LEFT JOIN teachers absent ON sa.absent_teacher_id=absent.teacher_id
            LEFT JOIN teachers substitute ON sa.substitute_teacher_id=substitute.teacher_id
            WHERE sa.status IN ('ASSIGNED','APPROVED')
            ORDER BY sa.assignment_date DESC,sa.period_no DESC,sa.assignment_id DESC
            LIMIT 20
        """)
        recent_assigned = cursor.fetchall()
    finally:
        cursor.close()
        connection.close()

    return render_template(
        "reports.html",
        substitution_stats=substitution_stats,
        total_absences=total_absences,
        teacher_load=teacher_load,
        daily_activity=daily_activity,
        recent_assigned=recent_assigned,
    )


# ============================================================
# ADMIN - DYNAMIC REALLOCATION
# ============================================================

@app.route("/admin/reallocate/<int:assignment_id>", methods=["GET", "POST"])
def reallocate_assignment(assignment_id):
    if not admin_only():
        return redirect(url_for("login"))

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT
                sa.*,
                c.class_name,
                COALESCE(s.subject_name, 'Subject') AS subject_name,
                absent.teacher_name AS absent_teacher,
                substitute.teacher_name AS substitute_teacher
            FROM substitute_assignments sa
            LEFT JOIN classes c ON sa.class_id = c.class_id
            LEFT JOIN subjects s ON sa.subject_id = s.subject_id
            LEFT JOIN teachers absent ON sa.absent_teacher_id = absent.teacher_id
            LEFT JOIN teachers substitute ON sa.substitute_teacher_id = substitute.teacher_id
            WHERE sa.assignment_id = %s
              AND sa.status IN ('ASSIGNED', 'APPROVED')
            LIMIT 1
            """,
            (assignment_id,),
        )
        assignment = cursor.fetchone()
    finally:
        cursor.close()
        connection.close()

    if not assignment:
        return redirect(url_for("admin_dashboard"))

    current_teacher_id = assignment["substitute_teacher_id"]
    current_available, current_reason = is_teacher_available(
        teacher_id=current_teacher_id,
        assignment_date=assignment["assignment_date"],
        period_no=assignment["period_no"],
        absent_teacher_id=assignment["absent_teacher_id"],
        exclude_assignment_id=assignment_id,
    )

    candidates = []
    if not current_available:
        replacement_assignment = {
            "assignment_date": assignment["assignment_date"],
            "period_no": assignment["period_no"],
            "class_id": assignment["class_id"],
            "subject_id": assignment["subject_id"],
            "absent_teacher_id": assignment["absent_teacher_id"],
            "assignment_id": assignment_id,
        }
        candidates = find_candidates(
            replacement_assignment,
            extra_rejected={current_teacher_id},
        )

    if request.method == "POST":
        if current_available:
            return redirect(url_for("reallocate_assignment", assignment_id=assignment_id))

        try:
            selected_teacher_id = int(request.form.get("teacher_id", "0"))
        except ValueError:
            selected_teacher_id = 0

        selected = next(
            (c for c in candidates if int(c["teacher_id"]) == selected_teacher_id),
            None,
        )
        if not selected:
            return redirect(url_for("reallocate_assignment", assignment_id=assignment_id))

        connection = get_connection()
        cursor = connection.cursor()
        try:
            # Re-check before changing anything.
            available, _ = is_teacher_available(
                teacher_id=selected_teacher_id,
                assignment_date=assignment["assignment_date"],
                period_no=assignment["period_no"],
                absent_teacher_id=assignment["absent_teacher_id"],
                exclude_assignment_id=assignment_id,
            )
            if not available:
                connection.rollback()
                return redirect(url_for("reallocate_assignment", assignment_id=assignment_id))

            score = calculate_score(selected)
            reason = (
                "Dynamic reallocation created after the assigned substitute became unavailable. "
                f"Original substitute: {assignment['substitute_teacher']}. "
                "Subject knowledge is treated equally for all substitute teachers. "
                "Requires HOD approval."
            )

            cursor.execute(
                """
                UPDATE substitute_assignments
                SET status = 'CANCELLED',
                    reason = CONCAT(
                        COALESCE(reason, ''),
                        ' | Cancelled for dynamic reallocation: ', %s
                    )
                WHERE assignment_id = %s
                  AND status IN ('ASSIGNED', 'APPROVED')
                """,
                (current_reason, assignment_id),
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
                    assignment["assignment_date"],
                    assignment["period_no"],
                    assignment["class_id"],
                    assignment["subject_id"],
                    assignment["absent_teacher_id"],
                    selected_teacher_id,
                    score,
                    reason,
                ),
            )
            connection.commit()
        finally:
            cursor.close()
            connection.close()

        return redirect(url_for("admin_dashboard"))

    return render_template(
        "reallocate.html",
        assignment=assignment,
        current_available=current_available,
        current_reason=current_reason,
        candidates=candidates,
    )


# ============================================================
# ADMIN - WHAT-IF SIMULATION
# ============================================================

@app.route("/admin/what-if", methods=["GET", "POST"])
def what_if_simulation():
    if not admin_only():
        return redirect(url_for("login"))

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    teachers = []
    try:
        cursor.execute(
            """
            SELECT teacher_id, teacher_name, department
            FROM teachers
            WHERE is_active = TRUE
            ORDER BY teacher_name
            """
        )
        teachers = cursor.fetchall()
    finally:
        cursor.close()
        connection.close()

    simulation = None
    selected_date = request.form.get("assignment_date", "")
    selected_teacher_id = request.form.get("absent_teacher_id", "")

    if request.method == "POST":
        if not selected_date or not selected_teacher_id:
            simulation = {"error": "Please select both a date and an absent teacher."}
        else:
            try:
                from datetime import datetime
                assignment_date = datetime.strptime(selected_date, "%Y-%m-%d").date()
                absent_teacher_id = int(selected_teacher_id)
            except ValueError:
                simulation = {"error": "Please enter a valid date."}
            else:
                affected = get_affected_periods(absent_teacher_id, assignment_date)

                # Every lab period is a separate substitution slot.
                # A teacher cannot be reused in another period of the same lab
                # block, while teachers may still be reused in different labs.
                reserved_by_slot = {}
                reserved_by_lab = {}
                rows = []

                for period in affected:
                    slot = (assignment_date, int(period["period_no"]))
                    reserved = set(reserved_by_slot.setdefault(slot, set()))
                    lab_key = period.get("lab_block_key")
                    if lab_key is not None:
                        reserved.update(reserved_by_lab.setdefault(lab_key, set()))

                    assignment = {
                        "assignment_date": assignment_date,
                        "period_no": period["period_no"],
                        "class_id": period["class_id"],
                        "subject_id": period["subject_id"],
                        "absent_teacher_id": absent_teacher_id,
                    }

                    candidates = find_candidates(
                        assignment,
                        extra_rejected=reserved,
                    )

                    if candidates:
                        candidate = candidates[0]
                        teacher_id = int(candidate["teacher_id"])
                        reserved_by_slot.setdefault(slot, set()).add(teacher_id)
                        if lab_key is not None:
                            reserved_by_lab.setdefault(lab_key, set()).add(teacher_id)
                        score = calculate_score(candidate)
                        eligible = "YES"
                        reason = build_reason(candidate)
                        rows.append({
                            "period_no": period["period_no"],
                            "period_label": period.get("period_label") or f"Period {period['period_no']}",
                            "class_name": period.get("class_name") or "—",
                            "subject_name": period.get("subject_name") or "Subject",
                            "teacher_name": candidate["teacher_name"],
                            "department": candidate.get("department") or "—",
                            "subject_eligible": eligible,
                            "score": score,
                            "daily_classes": int(candidate["daily_classes"]),
                            "free_periods": int(candidate.get("free_periods", 0)),
                            "substitutions_today": int(candidate["substitutions_today"]),
                            "total_substitutions": int(candidate["total_substitutions"]),
                            "reason": reason,
                            "status": "Recommended",
                        })
                    else:
                        rows.append({
                            "period_no": period["period_no"],
                            "period_label": period.get("period_label") or f"Period {period['period_no']}",
                            "class_name": period.get("class_name") or "—",
                            "subject_name": period.get("subject_name") or "Subject",
                            "teacher_name": "—",
                            "department": "—",
                            "subject_eligible": "—",
                            "score": 0,
                            "daily_classes": 0,
                            "free_periods": 0,
                            "substitutions_today": 0,
                            "total_substitutions": 0,
                            "reason": "No available teacher after checking timetable, absence, unavailability, substitutions and campus movement.",
                            "status": "ADMIN ACTION",
                        })

                simulation = {
                    "date": assignment_date,
                    "absent_teacher_id": absent_teacher_id,
                    "affected_count": len(affected),
                    "rows": rows,
                    "recommended_count": sum(1 for r in rows if r["status"] == "Recommended"),
                    "admin_action_count": sum(1 for r in rows if r["status"] == "ADMIN ACTION"),
                    "message": (
                        "Simulation complete. No absence, recommendation or assignment was saved to the database."
                    ) if affected else "No Main Campus BCA timetable periods found for this teacher on the selected date.",
                }

    return render_template(
        "what_if.html",
        teachers=teachers,
        simulation=simulation,
        selected_date=selected_date,
        selected_teacher_id=selected_teacher_id,
    )



# ============================================================
# ADMIN - TIMETABLE VALIDATION
# ============================================================

@app.route("/admin/timetable-validation")
def timetable_validation():
    if not admin_only():
        return redirect(url_for("login"))

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)

    issues = []
    checks = []

    try:
        # 1. Duplicate teacher assignments in the same day/period.
        cursor.execute("""
            SELECT
                tt.day_of_week,
                tt.period_no,
                t.teacher_name,
                COUNT(*) AS total
            FROM timetable tt
            JOIN teachers t ON tt.teacher_id = t.teacher_id
            GROUP BY tt.day_of_week, tt.period_no, tt.teacher_id, t.teacher_name
            HAVING COUNT(*) > 1
            ORDER BY tt.day_of_week, tt.period_no, t.teacher_name
        """)
        teacher_conflicts = cursor.fetchall()
        for r in teacher_conflicts:
            issues.append({
                "type": "Teacher conflict",
                "severity": "ERROR",
                "location": f"{r['day_of_week']} P{r['period_no']}",
                "details": f"{r['teacher_name']} is assigned to {r['total']} timetable entries in the same period."
            })
        checks.append(("Duplicate teacher slots", len(teacher_conflicts)))

        # 2. Duplicate class assignments in the same day/period/campus.
        # Main and Women's Campus can legitimately use the same class_id,
        # so campus must be part of the conflict key.
        cursor.execute("""
            SELECT
                tt.day_of_week,
                tt.period_no,
                LOWER(COALESCE(tt.campus, '')) AS campus,
                COALESCE(c.class_name, tt.class_label, 'Unknown Class') AS class_name,
                COUNT(*) AS total
            FROM timetable tt
            LEFT JOIN classes c ON tt.class_id = c.class_id
            WHERE tt.class_id IS NOT NULL OR tt.class_label IS NOT NULL
            GROUP BY
                tt.day_of_week,
                tt.period_no,
                tt.class_id,
                tt.class_label,
                c.class_name,
                LOWER(COALESCE(tt.campus, ''))
            HAVING COUNT(*) > 1
            ORDER BY tt.day_of_week, tt.period_no, campus, class_name
        """)
        class_conflicts = cursor.fetchall()
        for r in class_conflicts:
            issues.append({
                "type": "Class conflict",
                "severity": "ERROR",
                "location": f"{r['day_of_week']} P{r['period_no']} ({r['campus'] or 'campus not specified'})",
                "details": f"{r['class_name']} has {r['total']} timetable entries in the same period and campus."
            })
        checks.append(("Duplicate class slots", len(class_conflicts)))

        # 3. Occupied Main Campus BCA timetable rows without a teacher.
        # Non-BCA rows may exist in the source timetable without mapped
        # teachers; they are outside the substitution project scope.
        cursor.execute("""
            SELECT
                tt.timetable_id,
                tt.day_of_week,
                tt.period_no,
                COALESCE(c.class_name, tt.class_label, 'Unknown Class') AS class_name,
                COALESCE(s.subject_name, tt.period_label, 'Subject') AS subject_name
            FROM timetable tt
            LEFT JOIN classes c ON tt.class_id = c.class_id
            LEFT JOIN subjects s ON tt.subject_id = s.subject_id
            WHERE tt.teacher_id IS NULL
              AND LOWER(COALESCE(tt.campus, '')) LIKE 'main%'
              AND (
                    LOWER(COALESCE(c.class_name, tt.class_label, '')) LIKE '%bca%'
                    OR UPPER(COALESCE(tt.course_type, '')) = 'BCA'
                  )
              AND (tt.class_id IS NOT NULL OR tt.class_label IS NOT NULL)
              AND UPPER(COALESCE(tt.period_label, '')) NOT LIKE '%PET%'
              AND UPPER(COALESCE(tt.period_label, '')) NOT LIKE '%GAME%'
              AND UPPER(COALESCE(tt.period_type, '')) <> 'GAMES'
        """)
        missing_teachers = cursor.fetchall()
        for r in missing_teachers:
            issues.append({
                "type": "Missing teacher",
                "severity": "ERROR",
                "location": f"{r['day_of_week']} P{r['period_no']}",
                "details": f"{r['class_name']} / {r['subject_name']} has no assigned teacher."
            })
        checks.append(("Occupied rows with missing teacher", len(missing_teachers)))

        # 4. Invalid class references.
        cursor.execute("""
            SELECT tt.timetable_id, tt.day_of_week, tt.period_no, tt.class_id
            FROM timetable tt
            LEFT JOIN classes c ON tt.class_id = c.class_id
            WHERE tt.class_id IS NOT NULL AND c.class_id IS NULL
        """)
        bad_classes = cursor.fetchall()
        for r in bad_classes:
            issues.append({
                "type": "Invalid class reference",
                "severity": "ERROR",
                "location": f"{r['day_of_week']} P{r['period_no']}",
                "details": f"Timetable row {r['timetable_id']} references missing class_id {r['class_id']}."
            })
        checks.append(("Invalid class references", len(bad_classes)))

        # 5. Invalid subject references.
        cursor.execute("""
            SELECT tt.timetable_id, tt.day_of_week, tt.period_no, tt.subject_id
            FROM timetable tt
            LEFT JOIN subjects s ON tt.subject_id = s.subject_id
            WHERE tt.subject_id IS NOT NULL AND s.subject_id IS NULL
        """)
        bad_subjects = cursor.fetchall()
        for r in bad_subjects:
            issues.append({
                "type": "Invalid subject reference",
                "severity": "ERROR",
                "location": f"{r['day_of_week']} P{r['period_no']}",
                "details": f"Timetable row {r['timetable_id']} references missing subject_id {r['subject_id']}."
            })
        checks.append(("Invalid subject references", len(bad_subjects)))

        # 6. Invalid period numbers.
        cursor.execute("""
            SELECT timetable_id, day_of_week, period_no
            FROM timetable
            WHERE period_no NOT BETWEEN 1 AND 6
        """)
        bad_periods = cursor.fetchall()
        for r in bad_periods:
            issues.append({
                "type": "Invalid period",
                "severity": "ERROR",
                "location": str(r['day_of_week']),
                "details": f"Timetable row {r['timetable_id']} uses period {r['period_no']}; valid periods are 1 to 6."
            })
        checks.append(("Invalid period numbers", len(bad_periods)))

        # 7. Invalid day names.
        valid_days = ('MON','TUE','WED','THU','FRI','SAT','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday')
        placeholders = ','.join(['%s'] * len(valid_days))
        cursor.execute(
            f"SELECT timetable_id, day_of_week FROM timetable WHERE day_of_week NOT IN ({placeholders})",
            valid_days,
        )
        bad_days = cursor.fetchall()
        for r in bad_days:
            issues.append({
                "type": "Invalid day",
                "severity": "ERROR",
                "location": f"Row {r['timetable_id']}",
                "details": f"Unknown day_of_week value: {r['day_of_week']}."
            })
        checks.append(("Invalid day values", len(bad_days)))

        # 8. Main Campus BCA timetable sanity count.
        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM timetable
            WHERE LOWER(COALESCE(campus,'')) LIKE 'main%'
              AND COALESCE(course_type,'') = 'BCA'
        """)
        main_bca_rows = cursor.fetchone()['total']
        checks.append(("Main Campus BCA timetable rows", main_bca_rows))

        # 9. Main Campus BCA rows missing both teacher and subject on non-game rows.
        cursor.execute("""
            SELECT timetable_id, day_of_week, period_no,
                   COALESCE(c.class_name, class_label, 'Unknown Class') AS class_name,
                   period_type, period_label
            FROM timetable tt
            LEFT JOIN classes c ON tt.class_id = c.class_id
            WHERE tt.teacher_id IS NULL
              AND tt.subject_id IS NULL
              AND LOWER(COALESCE(tt.campus, '')) LIKE 'main%'
              AND (
                    LOWER(COALESCE(c.class_name, tt.class_label, '')) LIKE '%bca%'
                    OR UPPER(COALESCE(tt.course_type, '')) = 'BCA'
                  )
              AND (tt.class_id IS NOT NULL OR tt.class_label IS NOT NULL)
              AND UPPER(COALESCE(tt.period_label,'')) NOT LIKE '%PET%'
              AND UPPER(COALESCE(tt.period_label,'')) NOT LIKE '%GAME%'
        """)
        incomplete_rows = cursor.fetchall()
        for r in incomplete_rows:
            issues.append({
                "type": "Incomplete timetable row",
                "severity": "WARNING",
                "location": f"{r['day_of_week']} P{r['period_no']}",
                "details": f"{r['class_name']} has no teacher and no subject mapping. Period type: {r['period_type'] or '—'}."
            })
        checks.append(("Incomplete timetable rows", len(incomplete_rows)))

    finally:
        cursor.close()
        connection.close()

    error_count = sum(1 for x in issues if x['severity'] == 'ERROR')
    warning_count = sum(1 for x in issues if x['severity'] == 'WARNING')

    return render_template(
        "timetable_validation.html",
        issues=issues,
        checks=checks,
        error_count=error_count,
        warning_count=warning_count,
        total_issues=len(issues),
        passed=(error_count == 0),
    )



# ============================================================
# ADMIN - TODAY'S OPERATIONS
# ============================================================

@app.route("/admin/today")
def admin_today():
    if not admin_only():
        return redirect(url_for("login"))

    from datetime import date

    selected_date = date.today()
    date_text = request.args.get("date", "").strip()
    if date_text:
        try:
            selected_date = date.fromisoformat(date_text)
        except ValueError:
            selected_date = date.today()

    connection = get_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT
                ta.absence_id,
                ta.teacher_id,
                t.teacher_name,
                t.department,
                ta.status
            FROM teacher_absence ta
            JOIN teachers t
              ON ta.teacher_id = t.teacher_id
            WHERE ta.absence_date = %s
              AND ta.status IN ('PENDING','APPROVED')
            ORDER BY t.teacher_name
            """,
            (selected_date,),
        )
        absences = cursor.fetchall()

        affected_periods = []

        for absence in absences:
            affected = get_affected_periods(
                absence["teacher_id"],
                selected_date,
            )

            for period in affected:
                cursor.execute(
                    """
                    SELECT
                        sa.assignment_id,
                        sa.substitute_teacher_id,
                        COALESCE(st.teacher_name, '—') AS substitute_teacher,
                        sa.status,
                        sa.score,
                        sa.reason
                    FROM substitute_assignments sa
                    LEFT JOIN teachers st
                      ON sa.substitute_teacher_id = st.teacher_id
                    WHERE sa.assignment_date = %s
                      AND sa.period_no = %s
                      AND sa.absent_teacher_id = %s
                      AND sa.class_id = %s
                    ORDER BY CASE sa.status
                        WHEN 'ASSIGNED' THEN 1
                        WHEN 'APPROVED' THEN 1
                        WHEN 'PENDING' THEN 2
                        WHEN 'ADMIN_ACTION' THEN 3
                        WHEN 'REJECTED' THEN 4
                        WHEN 'CANCELLED' THEN 5
                        ELSE 6
                    END,
                    sa.assignment_id DESC
                    LIMIT 1
                    """,
                    (
                        selected_date,
                        period["period_no"],
                        absence["teacher_id"],
                        period["class_id"],
                    ),
                )
                assignment = cursor.fetchone()
                raw_status = assignment["status"] if assignment else None
                overall_status = (
                    "ASSIGNED"
                    if raw_status in ("ASSIGNED", "APPROVED")
                    else raw_status
                    if raw_status
                    else "NOT_PROCESSED"
                )

                affected_periods.append({
                    "absent_teacher": absence["teacher_name"],
                    "period_no": period["period_no"],
                    "class_name": period["class_name"],
                    "subject_name": period.get("subject_name") or period.get("period_label") or "Subject",
                    "period_type": period.get("period_type") or "CLASS",
                    "assignment_id": assignment["assignment_id"] if assignment else None,
                    "substitute_teacher": assignment["substitute_teacher"] if assignment else None,
                    "status": overall_status,
                    "score": assignment["score"] if assignment else None,
                    "reason": assignment["reason"] if assignment else None,
                })

        total_affected = len(affected_periods)
        assigned_count = sum(
            x["status"] == "ASSIGNED" for x in affected_periods
        )
        pending_count = sum(
            x["status"] == "PENDING" for x in affected_periods
        )
        admin_action_count = sum(
            x["status"] == "ADMIN_ACTION" for x in affected_periods
        )
        not_processed_count = sum(
            x["status"] == "NOT_PROCESSED" for x in affected_periods
        )
    finally:
        cursor.close()
        connection.close()

    return render_template(
        "today_operations.html",
        selected_date=selected_date.isoformat(),
        absences=absences,
        affected_periods=affected_periods,
        total_affected=total_affected,
        assigned_count=assigned_count,
        pending_count=pending_count,
        admin_action_count=admin_action_count,
        not_processed_count=not_processed_count,
    )

# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    print("==============================================")
    print("     SMART FACULTY SUBSTITUTE SYSTEM")
    print("==============================================")
    print("Main Campus | BCA | HOD/Admin: Swathy")
    print("Server: http://127.0.0.1:5000")
    print("==============================================")

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True,
    )
