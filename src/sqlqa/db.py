"""Database access: read-only engine, schema introspection, query execution.

Nothing here knows which tables exist. Everything is discovered from the URL.
"""

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, literal_column, select, table
from sqlalchemy.exc import SQLAlchemyError

MAX_CELL_CHARS = 80  # sample values longer than this are cut in the schema text


@dataclass(frozen=True)
class QueryResult:
    """Outcome of one query. Either `error` is set, or columns/rows are."""

    columns: list[str]
    rows: list[list]
    truncated: bool = False  # True when more than max_rows rows were available
    error: str | None = None


class Database:
    def __init__(
        self,
        url: str,
        max_rows: int = 50,
        sample_rows: int = 3,
        notes_file: str | Path | None = None,
    ):
        self.engine = create_engine(url)
        self.max_rows = max_rows
        self.sample_rows = sample_rows
        self.notes_file = Path(notes_file) if notes_file else None

        if self.dialect == "sqlite":
            # SQLite has no read-only users, so ask each connection to refuse writes.
            # Other databases: connect with a role that only has SELECT rights.
            @event.listens_for(self.engine, "connect")
            def set_read_only(dbapi_connection, _record):
                dbapi_connection.execute("PRAGMA query_only = ON")

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name

    def table_names(self) -> list[str]:
        """Tables and views in the default schema."""
        inspector = inspect(self.engine)
        return sorted(inspector.get_table_names() + inspector.get_view_names())

    def table_columns(self) -> dict[str, list[str]]:
        """Column names for every table and view."""
        inspector = inspect(self.engine)
        return {
            name: [column["name"] for column in inspector.get_columns(name)]
            for name in self.table_names()
        }

    def notes(self) -> str:
        """The optional plain-text notes about what the data means, or ''."""
        return self.notes_file.read_text(encoding="utf-8").strip() if self.notes_file else ""

    def describe_schema(self) -> str:
        """The schema as text for the LLM: columns, keys, sample rows, optional notes."""
        inspector = inspect(self.engine)
        parts = [f"SQL dialect: {self.dialect}"]

        for name in self.table_names():
            primary_key = inspector.get_pk_constraint(name)["constrained_columns"]
            references = {
                fk["constrained_columns"][0]: f"{fk['referred_table']}.{fk['referred_columns'][0]}"
                for fk in inspector.get_foreign_keys(name)
                if len(fk["constrained_columns"]) == 1
            }
            lines = [f"Table {name}"]
            for column in inspector.get_columns(name):
                facts = [str(column["type"])]
                if column["name"] in primary_key:
                    facts.append("primary key")
                elif not column["nullable"]:
                    facts.append("not null")
                if column["name"] in references:
                    facts.append(f"references {references[column['name']]}")
                lines.append(f"  {column['name']}: {', '.join(facts)}")

            if self.sample_rows > 0:
                sample = self._run(
                    select(literal_column("*")).select_from(table(name)).limit(self.sample_rows)
                )
                lines.append(f"  Sample rows ({', '.join(sample.columns)}):")
                lines += [f"    {[_shorten(value) for value in row]}" for row in sample.rows]
            parts.append("\n".join(lines))

        if self.notes():
            parts.append("Notes:\n" + self.notes())
        return "\n\n".join(parts)

    def execute(self, statement) -> QueryResult:
        """Run SQL text exactly as given, or a statement built with SQLAlchemy Core.

        Database errors are returned, not raised.
        """
        try:
            return self._run(statement)
        except SQLAlchemyError as exc:
            # exc.orig is the driver's own message, without SQLAlchemy's wrapping.
            return QueryResult(columns=[], rows=[], error=str(getattr(exc, "orig", None) or exc))

    def _run(self, statement) -> QueryResult:
        with self.engine.connect() as connection:
            try:
                if isinstance(statement, str):
                    # exec_driver_sql sends the text untouched; text() would treat
                    # ':word' inside the SQL as a bind parameter.
                    result = connection.exec_driver_sql(statement)
                else:
                    result = connection.execute(statement)
                fetched = result.fetchmany(self.max_rows + 1)
                return QueryResult(
                    columns=list(result.keys()),
                    rows=[[_json_safe(value) for value in row] for row in fetched[: self.max_rows]],
                    truncated=len(fetched) > self.max_rows,
                )
            finally:
                connection.rollback()  # never commit, whatever was run


def _json_safe(value):
    """Keep JSON-native values; convert the rest so results can go into a trace file."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return float(value)
    return str(value)  # dates, times, bytes, ...


def _shorten(value):
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS] + "..."
    return value
