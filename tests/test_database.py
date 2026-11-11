"""The Database class: introspection, read-only execution, row limit."""

import json
import re
from pathlib import Path

from sqlqa.db import Database
from sqlqa.sql_guard import check_sql

SRC = Path(__file__).parent.parent / "src" / "sqlqa"


def url(path):
    return f"sqlite:///{path.as_posix()}"


# --- introspection -----------------------------------------------------------


def test_table_names_are_discovered(db):
    assert db.table_names() == [
        "course_offerings",
        "courses",
        "enrollments",
        "semesters",
        "students",
        "teachers",
    ]


def test_schema_text_has_columns_keys_and_foreign_keys(db):
    schema = db.describe_schema()
    assert schema.startswith("SQL dialect: sqlite")
    assert "Table enrollments" in schema
    assert "  id: INTEGER, primary key" in schema
    assert "  student_id: INTEGER, not null, references students.id" in schema
    assert "  offering_id: INTEGER, not null, references course_offerings.id" in schema
    assert "  grade: REAL\n" in schema  # nullable: no 'not null'


def test_schema_text_has_at_most_three_sample_rows_per_table(db):
    schema = db.describe_schema()
    assert "Sample rows (id, name, email):" in schema
    assert "[1, 'Anna Keller', 'anna.keller@uni.example']" in schema
    assert "Ben Okafor" in schema and "Clara Rossi" in schema
    assert "daniel.kim@uni.example" not in schema  # the fourth student


def test_sample_rows_can_be_switched_off(university_db_path):
    schema = Database(url(university_db_path), sample_rows=0).describe_schema()
    assert "Table students" in schema
    assert "Sample rows" not in schema
    assert "Anna Keller" not in schema


def test_notes_file_is_appended_when_configured(db, university_db_path):
    assert "A NULL grade means the enrollment is in progress" in db.describe_schema()
    assert "Notes:" not in Database(url(university_db_path)).describe_schema()


def test_long_sample_values_are_shortened(tmp_path):
    import sqlite3

    path = tmp_path / "long.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE posts (body TEXT)")
    connection.execute("INSERT INTO posts VALUES (?)", ("x" * 500,))
    connection.commit()
    connection.close()

    schema = Database(url(path)).describe_schema()
    assert "x" * 80 + "..." in schema
    assert "x" * 81 not in schema


# --- execution ---------------------------------------------------------------


def test_execute_returns_columns_and_rows(db):
    result = db.execute("SELECT id, name FROM teachers ORDER BY id LIMIT 2")
    assert result.error is None
    assert result.columns == ["id", "name"]
    assert result.rows == [[1, "Elena Markovic"], [2, "David Chen"]]
    assert result.truncated is False


def test_empty_result_is_not_an_error(db):
    result = db.execute("SELECT * FROM students WHERE name = 'Nobody'")
    assert result.error is None
    assert result.columns == ["id", "name", "email"]
    assert result.rows == []


def test_row_limit_truncates_and_says_so(university_db_path):
    database = Database(url(university_db_path), max_rows=5)
    result = database.execute("SELECT * FROM enrollments")  # 31 rows
    assert len(result.rows) == 5
    assert result.truncated is True


def test_exactly_max_rows_is_not_truncated(university_db_path):
    database = Database(url(university_db_path), max_rows=4)
    result = database.execute("SELECT * FROM teachers")  # 4 rows
    assert len(result.rows) == 4
    assert result.truncated is False


def test_database_error_is_returned_not_raised(db):
    result = db.execute("SELECT nickname FROM students")
    assert result.rows == []
    assert result.error == "no such column: nickname"


def test_colon_in_sql_is_not_treated_as_a_parameter(db):
    result = db.execute("SELECT ' :name' AS value")
    assert result.rows == [[" :name"]]


def test_rows_are_json_serialisable(bookstore_db):
    result = bookstore_db.execute("SELECT title, price, published FROM books ORDER BY id")
    assert json.loads(json.dumps(result.rows)) == result.rows


# --- read-only ---------------------------------------------------------------


def test_writes_are_refused_by_the_connection_itself(db):
    """The second safety layer: even SQL that skipped the guard cannot write."""
    for statement in (
        "DELETE FROM enrollments",
        "INSERT INTO teachers (name) VALUES ('Mallory')",
        "UPDATE enrollments SET grade = 100",
        "DROP TABLE students",
        "CREATE TABLE loot (x)",
    ):
        result = db.execute(statement)
        assert "readonly database" in result.error, statement

    assert db.execute("SELECT COUNT(*) FROM enrollments").rows == [[31]]
    assert db.execute("SELECT COUNT(*) FROM teachers").rows == [[4]]


# --- database-agnostic -------------------------------------------------------


def test_same_code_describes_an_unrelated_schema(bookstore_db):
    assert bookstore_db.table_names() == ["authors", "books", "cheap_books"]  # view included
    schema = bookstore_db.describe_schema()
    assert "  author_id: INTEGER, not null, references authors.id" in schema
    assert "Ursula Le Guin" in schema


def test_guard_and_database_work_together_on_an_unrelated_schema(bookstore_db):
    tables, dialect = bookstore_db.table_names(), bookstore_db.dialect
    sql = "SELECT a.name FROM cheap_books b JOIN authors a ON a.id = b.author_id"
    assert check_sql(sql, tables, dialect) is None
    assert bookstore_db.execute(sql).rows == [["Italo Calvino"]]
    # university tables are unknown here
    assert "Unknown table 'students'" in check_sql("SELECT * FROM students", tables, dialect)


def test_agent_code_never_mentions_university_tables(db):
    source = "\n".join(path.read_text(encoding="utf-8") for path in SRC.rglob("*.py")).lower()
    for name in db.table_names():
        # 'students' also catches 'student'; table names are plural in this schema.
        assert not re.search(rf"\b{name.rstrip('s')}", source), name
