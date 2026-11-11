"""The SQL guard: what may run and what may not. No database is needed."""

import pytest

from sqlqa.sql_guard import check_sql, sqlglot_dialect

TABLES = ["authors", "books"]


def check(sql, dialect="sqlite"):
    return check_sql(sql, TABLES, dialect)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM books",
        "SELECT * FROM books;",
        "SELECT 1",
        "select b.title, a.name from books b join authors a on a.id = b.author_id",
        "SELECT author_id, AVG(price) FROM books GROUP BY author_id HAVING COUNT(*) > 1",
        "SELECT * FROM books WHERE author_id IN (SELECT id FROM authors WHERE name LIKE 'U%')",
        "WITH pricey AS (SELECT * FROM books WHERE price > 10) SELECT COUNT(*) FROM pricey",
        "SELECT name FROM authors UNION SELECT title FROM books",
        "SELECT name FROM authors EXCEPT SELECT title FROM books",
        'SELECT * FROM "Books"',  # table names are matched case-insensitively
        "SELECT * FROM main.books",
        "-- a comment\nSELECT * FROM books /* another */",
        "SELECT * FROM books WHERE title = 'DROP TABLE books; --'",  # keywords inside a string
    ],
)
def test_accepts_read_only_selects(sql):
    assert check(sql) is None


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        # writes and schema changes
        ("INSERT INTO books (title) VALUES ('x')", "not INSERT"),
        ("UPDATE books SET price = 0", "not UPDATE"),
        ("DELETE FROM books", "not DELETE"),
        ("DROP TABLE books", "not DROP"),
        ("CREATE TABLE t (id INTEGER)", "not CREATE"),
        ("ALTER TABLE books ADD COLUMN x TEXT", "not ALTER"),
        ("INSERT INTO books SELECT * FROM books", "not INSERT"),
        # writes hidden inside a SELECT
        ("WITH gone AS (DELETE FROM books RETURNING *) SELECT * FROM gone", "found DELETE"),
        ("SELECT * INTO backup FROM books", "found INTO"),
        ("SELECT * FROM books FOR UPDATE", "found LOCK"),
        # more than one statement
        ("SELECT 1; DROP TABLE books", "exactly one"),
        ("SELECT 1; SELECT 2", "exactly one"),
        ("", "exactly one"),
        ("   ", "exactly one"),
        # database commands
        ("PRAGMA table_info(books)", "not PRAGMA"),
        ("ATTACH DATABASE 'other.db' AS other", "not ATTACH"),
        ("VACUUM", "not COMMAND"),
        ("EXPLAIN SELECT * FROM books", "not COMMAND"),
        # tables outside the introspected schema
        ("SELECT * FROM customers", "Unknown table 'customers'"),
        ("SELECT * FROM sqlite_master", "Unknown table 'sqlite_master'"),
        ("SELECT * FROM books b JOIN orders o ON o.book_id = b.id", "Unknown table 'orders'"),
        ("SELECT * FROM pragma_table_info('books')", "Table-valued functions"),
        # not SQL
        ("SELEC title FROM books", "could not be parsed"),
        ("What is the cheapest book?", "could not be parsed"),
    ],
)
def test_rejects_everything_else(sql, expected):
    error = check(sql)
    assert error is not None
    assert expected in error


def test_unknown_table_error_lists_the_available_tables():
    assert check("SELECT * FROM customers").endswith("Available tables: authors, books.")


def test_cte_name_is_not_mistaken_for_an_unknown_table():
    assert check("WITH recent AS (SELECT * FROM books) SELECT * FROM recent") is None
    # ...but a CTE does not make an unknown table inside it acceptable
    assert "Unknown table 'orders'" in check(
        "WITH recent AS (SELECT * FROM orders) SELECT * FROM recent"
    )


def test_dialect_names_are_translated_for_sqlglot():
    assert sqlglot_dialect("sqlite") == "sqlite"
    assert sqlglot_dialect("postgresql") == "postgres"
    assert sqlglot_dialect("mssql") == "tsql"
    assert sqlglot_dialect("some-new-database") is None  # generic parser


def test_guard_uses_the_dialect_it_is_given():
    # A data-modifying CTE is PostgreSQL syntax; it must be caught under that dialect too.
    sql = "WITH gone AS (DELETE FROM books RETURNING *) SELECT * FROM gone"
    assert "found DELETE" in check(sql, dialect="postgresql")
