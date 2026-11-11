"""Queries and joins over the seeded university database.

Every expected value can be verified by hand against university/seed.sql.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

# Reused join: one row per enrollment with its student, course, teacher and semester.
ENROLLMENT_JOIN = """
    FROM enrollments e
    JOIN students s          ON s.id = e.student_id
    JOIN course_offerings o  ON o.id = e.offering_id
    JOIN courses c           ON c.id = o.course_id
    JOIN teachers t          ON t.id = o.teacher_id
    JOIN semesters sem       ON sem.id = o.semester_id
"""


def rows(conn, sql, **params):
    return [tuple(row) for row in conn.execute(text(sql), params)]


def scalar(conn, sql, **params):
    return conn.execute(text(sql), params).scalar()


# --- seed sanity -------------------------------------------------------------


def test_row_counts(conn):
    counts = {
        table: scalar(conn, f"SELECT COUNT(*) FROM {table}")
        for table in (
            "teachers",
            "students",
            "courses",
            "semesters",
            "course_offerings",
            "enrollments",
        )
    }
    assert counts == {
        "teachers": 4,
        "students": 8,
        "courses": 6,
        "semesters": 3,
        "course_offerings": 10,
        "enrollments": 31,
    }


# --- joins and filters -------------------------------------------------------


def test_course_can_have_two_teachers_in_one_semester(conn):
    result = rows(
        conn,
        """
        SELECT t.name
        FROM course_offerings o
        JOIN courses c     ON c.id = o.course_id
        JOIN teachers t    ON t.id = o.teacher_id
        JOIN semesters sem ON sem.id = o.semester_id
        WHERE c.code = :code AND sem.name = :semester
        ORDER BY t.name
        """,
        code="CS101",
        semester="Fall 2025",
    )
    assert result == [("David Chen",), ("Elena Markovic",)]


def test_course_teacher_changes_between_semesters(conn):
    result = rows(
        conn,
        """
        SELECT sem.name, t.name
        FROM course_offerings o
        JOIN courses c     ON c.id = o.course_id
        JOIN teachers t    ON t.id = o.teacher_id
        JOIN semesters sem ON sem.id = o.semester_id
        WHERE c.code = 'MATH101'
        ORDER BY sem.start_date
        """,
    )
    assert result == [("Fall 2025", "Sara Lindqvist"), ("Spring 2026", "Omar Haddad")]


def test_students_of_a_teacher_in_a_semester(conn):
    result = rows(
        conn,
        f"""
        SELECT s.name, c.code, e.grade
        {ENROLLMENT_JOIN}
        WHERE t.name = 'Elena Markovic' AND sem.name = 'Spring 2026'
        ORDER BY s.name
        """,
    )
    assert result == [
        ("Anna Keller", "CS201", 92.0),
        ("Ben Okafor", "CS201", 64.0),
        ("Clara Rossi", "CS201", 78.0),
        ("Eva Novak", "CS201", None),
    ]


def test_retake_gives_two_grades_for_one_course(conn):
    result = rows(
        conn,
        f"""
        SELECT sem.name, e.grade
        {ENROLLMENT_JOIN}
        WHERE s.email = 'farid.rahimi@uni.example' AND c.code = 'CS101'
        ORDER BY sem.start_date
        """,
    )
    assert result == [("Fall 2025", 45.0), ("Spring 2026", 72.0)]


def test_student_without_enrollments(conn):
    result = rows(
        conn,
        """
        SELECT s.name
        FROM students s
        LEFT JOIN enrollments e ON e.student_id = s.id
        WHERE e.id IS NULL
        """,
    )
    assert result == [("Greta Olsen",)]


def test_course_never_offered(conn):
    result = rows(
        conn,
        """
        SELECT c.code
        FROM courses c
        LEFT JOIN course_offerings o ON o.course_id = c.id
        WHERE o.id IS NULL
        """,
    )
    assert result == [("PHYS101",)]


# --- aggregations ------------------------------------------------------------


def test_enrollment_count_per_semester(conn):
    result = rows(
        conn,
        f"""
        SELECT sem.name, COUNT(*)
        {ENROLLMENT_JOIN}
        GROUP BY sem.id
        ORDER BY sem.start_date
        """,
    )
    assert result == [("Fall 2025", 10), ("Spring 2026", 12), ("Fall 2026", 9)]


def test_average_grade_per_course_across_semesters(conn):
    average = scalar(conn, f"SELECT AVG(e.grade) {ENROLLMENT_JOIN} WHERE c.code = 'CS101'")
    # 90, 70, 80, 60, 85, 45, 72, 88
    assert average == pytest.approx(73.75)


def test_average_grade_per_teacher(conn):
    result = rows(
        conn,
        f"""
        SELECT t.name, AVG(e.grade)
        {ENROLLMENT_JOIN}
        GROUP BY t.id
        ORDER BY t.name
        """,
    )
    assert [name for name, _ in result] == [
        "David Chen",
        "Elena Markovic",
        "Omar Haddad",
        "Sara Lindqvist",
    ]
    assert [avg for _, avg in result] == pytest.approx([70.0, 79.0, 224 / 3, 77.0])


def test_top_student_by_average_grade(conn):
    result = rows(
        conn,
        """
        SELECT s.email, AVG(e.grade) AS average
        FROM students s
        JOIN enrollments e ON e.student_id = s.id
        GROUP BY s.id
        HAVING AVG(e.grade) IS NOT NULL
        ORDER BY average DESC
        LIMIT 1
        """,
    )
    assert result[0][0] == "daniel.kim2@uni.example"
    assert result[0][1] == pytest.approx(274 / 3)  # 95, 88, 91


# --- NULL grades (in-progress enrollments) -----------------------------------


def test_average_ignores_null_grades(conn):
    total, graded, average = rows(
        conn,
        f"""
        SELECT COUNT(*), COUNT(e.grade), AVG(e.grade)
        {ENROLLMENT_JOIN}
        WHERE c.code = 'CS201' AND sem.name = 'Spring 2026'
        """,
    )[0]
    assert (total, graded) == (4, 3)
    # (92 + 78 + 64) / 3. Treating the NULL as 0 would give 58.5.
    assert average == pytest.approx(78.0)


def test_latest_semester_is_found_by_date_not_by_name(conn):
    latest = scalar(conn, "SELECT name FROM semesters ORDER BY start_date DESC LIMIT 1")
    assert latest == "Fall 2026"
    # Alphabetical order would wrongly pick 'Spring 2026'.
    assert scalar(conn, "SELECT MAX(name) FROM semesters") == "Spring 2026"


def test_average_for_ungraded_latest_semester_is_null(conn):
    total, average = rows(
        conn,
        f"""
        SELECT COUNT(*), AVG(e.grade)
        {ENROLLMENT_JOIN}
        WHERE sem.name = 'Fall 2026'
        """,
    )[0]
    assert total == 9  # students are enrolled...
    assert average is None  # ...but nothing is graded: all-NULL result, not zero


def test_grades_for_ungraded_latest_semester_are_empty(conn):
    result = rows(
        conn,
        f"""
        SELECT s.name, e.grade
        {ENROLLMENT_JOIN}
        WHERE sem.name = 'Fall 2026' AND e.grade IS NOT NULL
        """,
    )
    assert result == []


def test_per_course_averages_for_ungraded_semester_are_all_null(conn):
    result = rows(
        conn,
        f"""
        SELECT c.code, AVG(e.grade)
        {ENROLLMENT_JOIN}
        WHERE sem.name = 'Fall 2026'
        GROUP BY c.id
        ORDER BY c.code
        """,
    )
    assert result == [("CS201", None), ("CS301", None), ("MATH201", None)]


# --- duplicate names ---------------------------------------------------------


def test_two_students_share_a_name_but_not_an_email(conn):
    result = rows(conn, "SELECT id, email FROM students WHERE name = 'Daniel Kim' ORDER BY id")
    assert result == [(4, "daniel.kim@uni.example"), (6, "daniel.kim2@uni.example")]


def test_grouping_by_name_merges_two_different_students(conn):
    by_id = rows(
        conn,
        """
        SELECT s.id, AVG(e.grade)
        FROM students s JOIN enrollments e ON e.student_id = s.id
        WHERE s.name = 'Daniel Kim'
        GROUP BY s.id
        ORDER BY s.id
        """,
    )
    assert [avg for _, avg in by_id] == pytest.approx([62.5, 274 / 3])

    by_name = scalar(
        conn,
        """
        SELECT AVG(e.grade)
        FROM students s JOIN enrollments e ON e.student_id = s.id
        WHERE s.name = 'Daniel Kim'
        """,
    )
    assert by_name == pytest.approx(79.8)  # a number that describes neither student


# --- constraints -------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        # duplicate email
        "INSERT INTO students (name, email) VALUES ('X', 'anna.keller@uni.example')",
        # missing name
        "INSERT INTO teachers (name) VALUES (NULL)",
        # grade out of range
        "INSERT INTO enrollments (student_id, offering_id, grade) VALUES (8, 1, 101)",
        # same student enrolled twice in one offering
        "INSERT INTO enrollments (student_id, offering_id, grade) VALUES (1, 1, 50)",
        # same course, teacher and semester offered twice
        "INSERT INTO course_offerings (course_id, teacher_id, semester_id) VALUES (1, 1, 1)",
        # foreign key to a student that does not exist
        "INSERT INTO enrollments (student_id, offering_id, grade) VALUES (999, 1, 50)",
    ],
)
def test_constraints_reject_invalid_rows(conn, statement):
    with pytest.raises(IntegrityError):
        conn.execute(text(statement))
