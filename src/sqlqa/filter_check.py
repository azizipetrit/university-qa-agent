"""When a query returns nothing, find out whether its filter values exist at all.

An empty result has two very different causes: the data really has no such rows, or the
query asked for a value that is not in the column (a term that does not exist, or one
written in a different case). This module tells them apart with evidence:

1. read the simple literal comparisons out of the SQL with sqlglot;
2. ask the database, for each one, whether any row in that table has that value;
3. if not, report a differently cased match, or the values that do exist.

Probes are built with SQLAlchemy Core and bound parameters. No text from the model's query
is executed again. Anything that cannot be resolved with certainty is skipped and counted.
"""

from dataclasses import dataclass

import sqlglot
from sqlalchemy import column, func, literal, select, table
from sqlglot import exp
from sqlglot.errors import SqlglotError

from sqlqa.db import Database
from sqlqa.sql_guard import sqlglot_dialect


@dataclass(frozen=True)
class Comparison:
    """One `column <op> literal` condition found in a query."""

    table: str
    column: str
    op: str  # "=" or "LIKE"
    value: str | int | float
    wrapper: str | None = None  # "lower" or "upper" around the column

    def describe(self) -> str:
        target = f"{self.table}.{self.column}"
        if self.wrapper:
            target = f"{self.wrapper.upper()}({target})"
        return f"{target} {self.op} {self.value!r}"


def check_filters(sql: str, db: Database, max_probes: int = 5, value_limit: int = 10) -> dict:
    """Probe the literal comparisons in `sql`. Returns what was found, as plain data."""
    comparisons, skipped = find_comparisons(sql, db.table_columns(), db.dialect)
    skipped += max(0, len(comparisons) - max_probes)
    checked = [probe(db, comparison, value_limit) for comparison in comparisons[:max_probes]]
    return {"checked": checked, "not_checked": skipped}


def find_comparisons(
    sql: str, columns_by_table: dict[str, list[str]], dialect: str
) -> tuple[list[Comparison], int]:
    """The comparisons that can be checked, and how many had a literal but could not be."""
    try:
        tree = sqlglot.parse_one(sql, dialect=sqlglot_dialect(dialect))
    except SqlglotError:
        return [], 0

    known = {name.lower(): name for name in columns_by_table}
    # alias (or bare table name) -> real table name, for the real tables this query reads
    aliases = {
        node.alias_or_name.lower(): known[node.name.lower()]
        for node in tree.find_all(exp.Table)
        if node.name.lower() in known
    }

    def resolve(col: exp.Column) -> tuple[str, str] | None:
        """The (table, column) a column reference certainly points at, or None."""
        if col.table:
            candidates = [aliases[col.table.lower()]] if col.table.lower() in aliases else []
        else:
            candidates = sorted(set(aliases.values()))
        matches = [
            (name, real)
            for name in candidates
            for real in columns_by_table[name]
            if real.lower() == col.name.lower()
        ]
        return matches[0] if len(matches) == 1 else None

    found: list[Comparison] = []
    skipped = 0
    for node in tree.find_all(exp.EQ, exp.In, exp.Like):
        if isinstance(node, exp.In):
            left, values = node.this, node.expressions  # a subquery has no expressions
        else:
            left, values = node.this, [node.expression]
            if isinstance(left, exp.Literal):  # written as 'value' = column
                left, values = node.expression, [node.this]
        if not values or not all(isinstance(value, exp.Literal) for value in values):
            continue  # a join condition, a subquery, or something that is not a plain value

        wrapper = None
        if isinstance(left, exp.Lower | exp.Upper):
            wrapper, left = left.key, left.this
        target = resolve(left) if isinstance(left, exp.Column) else None
        if target is None or isinstance(node.parent, exp.Not):
            skipped += len(values)
            continue
        op = "LIKE" if isinstance(node, exp.Like) else "="
        found += [Comparison(*target, op, _python_value(value), wrapper) for value in values]
    return found, skipped


def probe(db: Database, comparison: Comparison, value_limit: int) -> dict:
    """Does any row have this value? If not, what is there instead?"""
    source = table(comparison.table)
    col = column(comparison.column)
    target = getattr(func, comparison.wrapper)(col) if comparison.wrapper else col
    condition = (
        target.like(comparison.value) if comparison.op == "LIKE" else target == comparison.value
    )

    exists = db.execute(select(literal(1)).select_from(source).where(condition).limit(1))
    finding = {"filter": comparison.describe(), "found": bool(exists.rows)}
    if exists.error:
        return finding | {"found": None, "detail": f"could not be checked: {exists.error}"}
    if exists.rows:
        return finding | {"detail": "found"}
    if value_limit <= 0:
        return finding | {"detail": "no row has this value"}

    # The same value in a different case?
    if comparison.op == "=" and not comparison.wrapper and isinstance(comparison.value, str):
        same = db.execute(
            select(col)
            .distinct()
            .select_from(source)
            .where(func.lower(col) == comparison.value.lower())
            .limit(value_limit)
        )
        if same.rows:
            values = ", ".join(repr(row[0]) for row in same.rows)
            return finding | {
                "detail": f"no row has this value, but {values} exists (different case)"
            }

    # Otherwise list what the column holds, if that is a short list.
    distinct = db.execute(select(col).distinct().select_from(source).limit(value_limit + 1))
    if len(distinct.rows) > value_limit:
        detail = f"no row has this value; the column has more than {value_limit} distinct values"
    else:
        values = ", ".join(repr(row[0]) for row in distinct.rows)
        detail = f"no row has this value; the column holds only: {values}"
    return finding | {"detail": detail}


def _python_value(node: exp.Literal) -> str | int | float:
    if node.is_string:
        return node.this
    return float(node.this) if "." in node.this else int(node.this)
