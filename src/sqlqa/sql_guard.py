"""Decide whether a SQL string is safe to run: one read-only SELECT over known tables.

Pure function, no database access. The error messages are written for the LLM,
because they are sent back to it when a query is rejected.
"""

from collections.abc import Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

# SQLAlchemy dialect names that sqlglot spells differently.
SQLGLOT_NAMES = {"postgresql": "postgres", "mssql": "tsql"}

# Node types that must not appear anywhere in the statement, even nested
# (e.g. a DELETE inside a CTE, or SELECT ... INTO new_table).
FORBIDDEN = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Into,
    exp.Lock,
    exp.Pragma,
    exp.Command,  # anything sqlglot does not model: VACUUM, EXPLAIN, ...
)


def sqlglot_dialect(sqlalchemy_dialect: str) -> str | None:
    """Translate a SQLAlchemy dialect name; None means sqlglot's generic parser."""
    name = SQLGLOT_NAMES.get(sqlalchemy_dialect, sqlalchemy_dialect)
    return name if name in sqlglot.Dialect.classes else None


def check_sql(sql: str, known_tables: Iterable[str], dialect: str) -> str | None:
    """Return None if `sql` may be executed, otherwise the reason it may not."""
    try:
        statements = [s for s in sqlglot.parse(sql, dialect=sqlglot_dialect(dialect)) if s]
    except SqlglotError as exc:
        return f"The SQL could not be parsed: {exc}"

    if len(statements) != 1:
        return f"Expected exactly one SQL statement, found {len(statements)}."
    statement = statements[0]

    # A CTE query parses as a Select; UNION / INTERSECT / EXCEPT as a SetOperation.
    if not isinstance(statement, exp.Select | exp.SetOperation):
        return f"Only SELECT statements are allowed, not {statement.key.upper()}."

    forbidden = statement.find(*FORBIDDEN)
    if forbidden is not None:
        return f"Only read-only SELECT statements are allowed, found {forbidden.key.upper()}."

    known = {name.lower() for name in known_tables}
    cte_names = {cte.alias.lower() for cte in statement.find_all(exp.CTE)}
    for table in statement.find_all(exp.Table):
        if not table.name:  # e.g. FROM some_function(...)
            return "Table-valued functions are not allowed; select from tables only."
        if table.name.lower() not in known | cte_names:
            return f"Unknown table '{table.name}'. Available tables: {', '.join(sorted(known))}."
    return None
