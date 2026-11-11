"""check_filters on its own: which comparisons it reads from SQL, and what it reports."""

import sqlite3

import pytest

from sqlqa.db import Database
from sqlqa.filter_check import Comparison, check_filters, find_comparisons

COLUMNS = {
    "authors": ["id", "name"],
    "books": ["id", "title", "price", "published", "author_id"],
}


def found(sql):
    comparisons, skipped = find_comparisons(sql, COLUMNS, "sqlite")
    return [c.describe() for c in comparisons], skipped


# --- reading comparisons out of SQL ------------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM books WHERE title = 'Dune'", ["books.title = 'Dune'"]),
        ("SELECT * FROM books WHERE 'Dune' = title", ["books.title = 'Dune'"]),
        ("SELECT * FROM books WHERE price = 9", ["books.price = 9"]),
        ("SELECT * FROM books WHERE price = 9.5", ["books.price = 9.5"]),
        ("SELECT * FROM books WHERE title LIKE '%dune%'", ["books.title LIKE '%dune%'"]),
        ("SELECT * FROM books WHERE LOWER(title) = 'dune'", ["LOWER(books.title) = 'dune'"]),
        ("SELECT * FROM books WHERE UPPER(title) LIKE 'D%'", ["UPPER(books.title) LIKE 'D%'"]),
        (
            "SELECT * FROM books WHERE title IN ('Dune', 'Emma')",
            ["books.title = 'Dune'", "books.title = 'Emma'"],
        ),
        # aliases, and an unqualified column that only one table has
        (
            "SELECT * FROM books b JOIN authors a ON a.id = b.author_id "
            "WHERE a.name = 'Ann' AND title = 'Dune'",
            ["books.title = 'Dune'", "authors.name = 'Ann'"],
        ),
        # inside a CTE and a subquery, when the columns say which table they mean
        (
            "WITH cheap AS (SELECT * FROM books b WHERE b.price = 1) SELECT * FROM cheap "
            "WHERE author_id IN (SELECT a.id FROM authors a WHERE a.name = 'Ann')",
            ["books.price = 1", "authors.name = 'Ann'"],
        ),
    ],
)
def test_reads_simple_literal_comparisons(sql, expected):
    comparisons, skipped = found(sql)
    assert sorted(comparisons) == sorted(expected)
    assert skipped == 0


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM books",
        "SELECT * FROM books b JOIN authors a ON a.id = b.author_id",  # column = column
        "SELECT * FROM books WHERE price > 5 AND price BETWEEN 1 AND 9",  # ranges
        "SELECT * FROM books WHERE title != 'Dune'",
        "SELECT * FROM books WHERE author_id IN (SELECT id FROM authors)",
        "SELECT * FROM books WHERE price = -1",  # not a plain literal
        "this is not SQL at all (",
    ],
)
def test_ignores_what_is_not_a_literal_comparison(sql):
    assert found(sql) == ([], 0)


@pytest.mark.parametrize(
    "sql",
    [
        # 'id' is in both tables and not qualified: ambiguous
        "SELECT * FROM books b JOIN authors a ON a.id = b.author_id WHERE id = 1",
        # the column belongs to a CTE, not to a real table
        "WITH t AS (SELECT title AS label FROM books) SELECT * FROM t WHERE label = 'Dune'",
        "SELECT * FROM books WHERE no_such_column = 1",
        "SELECT * FROM books WHERE NOT title = 'Dune'",
        "SELECT * FROM books WHERE SUBSTR(title, 1, 1) = 'D'",
    ],
)
def test_counts_what_it_cannot_resolve_instead_of_guessing(sql):
    assert found(sql) == ([], 1)


@pytest.mark.parametrize(
    "sql",
    [
        # a CTE: 'title' could be the CTE's column or the table's
        "WITH t AS (SELECT * FROM books) SELECT * FROM t WHERE title = 'Dune'",
        "WITH cheap AS (SELECT * FROM books WHERE price = 1) SELECT * FROM cheap",
        # a subquery: 'name' is not qualified, so its scope is not certain
        "SELECT * FROM books WHERE author_id IN (SELECT id FROM authors WHERE name = 'Ann')",
        "SELECT * FROM (SELECT * FROM books) AS inner_books WHERE title = 'Dune'",
        "SELECT * FROM books WHERE title = 'Dune' UNION SELECT * FROM books WHERE price > 5",
    ],
)
def test_unqualified_columns_are_skipped_when_the_query_is_nested(sql):
    """One table would be enough to resolve the column, but with a CTE or subquery in
    the statement the column may belong to another scope, so it is not trusted."""
    assert found(sql) == ([], 1)


def test_qualified_columns_are_still_checked_when_the_query_is_nested():
    sql = "SELECT * FROM books b WHERE b.title = 'Dune' AND b.author_id IN (SELECT id FROM authors)"
    assert found(sql) == (["books.title = 'Dune'"], 0)


def test_comparison_keeps_table_column_and_wrapper():
    (comparison,), _ = find_comparisons(
        "SELECT * FROM authors a WHERE LOWER(a.name) = 'ann'", COLUMNS, "sqlite"
    )
    assert comparison == Comparison("authors", "name", "=", "ann", "lower")


# --- asking the database -----------------------------------------------------


def details(db, sql, **options):
    return {item["filter"]: item for item in check_filters(sql, db, **options)["checked"]}


def test_existing_value_is_found(bookstore_db):
    result = check_filters("SELECT * FROM authors WHERE name = 'Italo Calvino'", bookstore_db)
    assert result == {
        "checked": [{"filter": "authors.name = 'Italo Calvino'", "found": True, "detail": "found"}],
        "not_checked": 0,
    }


def test_missing_value_lists_what_the_column_holds(bookstore_db):
    (item,) = details(bookstore_db, "SELECT * FROM authors WHERE name = 'Ann'").values()
    assert item["found"] is False
    assert item["detail"] == (
        "no row has this value; the column holds only: 'Ursula Le Guin', 'Italo Calvino'"
    )


def test_wrong_case_reports_the_real_spelling_only(bookstore_db):
    (item,) = details(bookstore_db, "SELECT * FROM authors WHERE name = 'italo calvino'").values()
    assert item["detail"] == "no row has this value, but 'Italo Calvino' exists (different case)"
    assert "Ursula" not in item["detail"]


def test_like_and_lower_are_probed_as_written(bookstore_db):
    checks = details(
        bookstore_db,
        "SELECT * FROM books WHERE LOWER(title) = 'invisible cities' AND title LIKE '%Winter%'",
    )
    assert checks["LOWER(books.title) = 'invisible cities'"]["found"] is True
    assert checks["books.title LIKE '%Winter%'"]["found"] is False


def test_number_values_are_checked_too(bookstore_db):
    checks = details(bookstore_db, "SELECT * FROM books WHERE author_id = 2 OR author_id = 7")
    assert checks["books.author_id = 2"]["found"] is True
    assert checks["books.author_id = 7"]["detail"].endswith("the column holds only: 1, 2")


def test_each_value_of_an_in_list_is_checked(bookstore_db):
    checks = details(bookstore_db, "SELECT * FROM authors WHERE name IN ('Italo Calvino', 'Ann')")
    assert [item["found"] for item in checks.values()] == [True, False]


def test_values_are_not_listed_for_a_column_with_many_distinct_values(tmp_path):
    path = tmp_path / "people.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE people (email TEXT)")
    connection.executemany(
        "INSERT INTO people VALUES (?)", [(f"p{n}@x.example",) for n in range(50)]
    )
    connection.commit()
    connection.close()
    db = Database(f"sqlite:///{path.as_posix()}")

    (item,) = details(db, "SELECT * FROM people WHERE email = 'nobody@x.example'").values()
    assert item["detail"] == ("no row has this value; the column has more than 10 distinct values")
    assert "p1@" not in item["detail"]

    # ...but a wrongly cased value still gets its one real spelling
    (item,) = details(db, "SELECT * FROM people WHERE email = 'P7@X.example'").values()
    assert item["detail"] == "no row has this value, but 'p7@x.example' exists (different case)"


def test_value_limit_zero_reveals_no_values_at_all(bookstore_db):
    for sql in (
        "SELECT * FROM authors WHERE name = 'Ann'",
        "SELECT * FROM authors WHERE name = 'italo calvino'",
    ):
        (item,) = details(bookstore_db, sql, value_limit=0).values()
        assert item["detail"] == "no row has this value"


def test_probes_are_capped_and_the_rest_counted(bookstore_db):
    sql = "SELECT * FROM books WHERE title IN ('a', 'b', 'c', 'd') AND SUBSTR(title, 1, 1) = 'x'"
    result = check_filters(sql, bookstore_db, max_probes=2)
    assert len(result["checked"]) == 2
    assert result["not_checked"] == 3  # two over the cap, one that could not be resolved


def test_value_in_the_sql_cannot_inject_anything(bookstore_db):
    """Values are bound parameters, so a quote in one is just a character."""
    sql = "SELECT * FROM authors WHERE name = 'x''; DROP TABLE authors; --'"
    (item,) = details(bookstore_db, sql).values()
    assert item["found"] is False
    assert bookstore_db.execute("SELECT COUNT(*) FROM authors").rows == [[2]]


def test_nothing_to_check_is_reported_as_such(bookstore_db):
    assert check_filters("SELECT * FROM books WHERE price > 100", bookstore_db) == {
        "checked": [],
        "not_checked": 0,
    }
