# Design

This document grows with each phase. So far: the university database, the database layer and the
SQL guard.

## Database

Six tables: `teachers`, `students`, `courses`, `semesters`, `course_offerings`, `enrollments`.

- **Offerings are their own table.** "A course is taught by a teacher in a semester" is a
  three-way fact. Storing it as one row per (course, teacher, semester) lets the same course have
  different teachers in different semesters, or several teachers in the same semester.
- **Enrollments point at offerings, not courses.** A grade belongs to one student in one offering,
  so the teacher and semester of every grade are known. A retake is simply a second enrollment in
  a different offering of the same course.
- **Semesters have a `start_date`.** "Latest semester" is answered by sorting on the date. Sorting
  on the name is wrong ('Spring 2026' sorts after 'Fall 2026'); a test demonstrates this.
- **Grades are numeric 0–100 and NULL while in progress.** SQL aggregates skip NULLs, so averages
  are correct without extra filtering. There is no separate enrollment status column, which would
  be a second source of truth for the same fact.
- **Student names are not unique; emails are.** The seed contains two students named Daniel Kim.
  Grouping by name silently merges them, which is the kind of ambiguity the agent must handle.

The seed data is hand-written and small, so every test expectation can be checked by hand. Its
deliberate edge cases are marked `EDGE` in `university/seed.sql`.

## Database layer (`src/sqlqa/db.py`)

One class, `Database`, built from a URL. It does three things:

- `table_names()` lists the tables and views it finds.
- `describe_schema()` turns the schema into text for the model: columns, types, keys, foreign keys,
  and up to three sample rows per table.
- `execute(sql)` runs a query and returns columns, rows, a truncation flag, or an error message.

Nothing in it names a table. Tests run the same class against an unrelated bookstore schema, and
one test fails if a university table name ever appears in `src/`.

### Schema notes

Introspection tells the model what the columns are, not what they mean. It cannot know that a NULL
grade means "in progress". That knowledge goes in an optional plain-text notes file
(`university/notes.md`, six lines) that is appended to the schema text. Moving to another database
means pointing at another URL and, optionally, another notes file. No code changes.

### Row limit

`execute` fetches at most `MAX_ROWS + 1` rows (default 50 + 1). The extra row only tells us whether
there were more, so the answer can say "showing the first 50". The limit also bounds how much data
is sent to the model.

## SQL guard (`src/sqlqa/sql_guard.py`)

One pure function, `check_sql(sql, known_tables, dialect)`. It parses the SQL with sqlglot and
returns `None` if the query may run, or a sentence explaining why not. That sentence is sent back
to the model so it can correct itself. A query passes only if all of these hold:

1. It parses, using the dialect of the connected database.
2. It is exactly one statement.
3. The statement is a SELECT (CTEs, subqueries, UNION / INTERSECT / EXCEPT are fine).
4. No write or command appears anywhere inside it, for example a DELETE inside a CTE or
   `SELECT ... INTO`.
5. Every table it reads is one we introspected. This rejects invented tables before they reach the
   database, and it rejects system tables such as `sqlite_master` with no special case.

Columns are not checked. An unknown column gives a clear database error, which goes back to the
model the same way. Checking columns through aliases and subqueries would need much more code.

## Read-only: three layers

| Layer | Where | Portable? |
|---|---|---|
| SQL guard | `sql_guard.py` | Yes, any dialect sqlglot knows |
| Never commit: every query runs in a transaction that is rolled back | `db.py` | Yes |
| The connection itself refuses writes | `db.py` (`PRAGMA query_only = ON`) | SQLite only |

### Trade-off: we run the model's SQL text, not sqlglot's rewrite of it

After the guard parses a query we have two versions: the text the model wrote, and the text
sqlglot can regenerate from its parse tree. We run the model's text.

- **Why:** the trace shows exactly what the model wrote and exactly what the database ran, and
  they are the same string. Regenerating SQL can change it in small ways, and a bug there would
  change the meaning of a query with nobody having written that change.
- **The cost:** the guard approves what *sqlglot* understood, but the database runs what *it*
  understands. If the two ever read the same string differently, something could pass the guard
  that is not what it looked like.
- **Why that is acceptable:** the guard is not the only layer. The connection refuses writes on
  its own, and a test proves it by sending DELETE, INSERT, UPDATE, DROP and CREATE straight to
  `execute`, skipping the guard. All five fail with "attempt to write a readonly database".
- The same reasoning is why the row limit is applied when fetching, not by inserting `LIMIT` into
  the query.

### Trade-off: other databases are allowed, but the third layer is SQLite-only

The agent starts against any database SQLAlchemy can connect to. The guard and the rollback work
everywhere. The connection-level switch is only implemented for SQLite.

- **Why allow it:** refusing to start on anything but SQLite would make the "database-agnostic"
  claim untrue for the one thing that matters, which is running.
- **The cost:** on PostgreSQL or MySQL, as shipped, there are two layers, not three.
- **What closes the gap:** connect with a database user that only has SELECT rights. That is
  stronger than anything the application can do, because the database enforces it. It is a
  deployment step, not code, and `PRODUCTION.md` lists it as a requirement.
- **What I have not done:** I have only run this against SQLite. Other dialects are supported by
  design (SQLAlchemy and sqlglot both handle them) but are untested here.

## Assumptions

Small choices made without discussion, simplest option taken.

- The schema text is a compact format built from SQLAlchemy's inspector, not the database's own
  `CREATE TABLE` text, so it looks the same on every database.
- Sample rows are the first rows `SELECT *` returns. Values longer than 80 characters are cut.
- Views are introspected and can be queried like tables.
- Only the default schema is introspected. There is no table include/exclude list yet.
- Table names are compared case-insensitively. A schema prefix (`main.books`) is ignored.
- `SELECT ... FOR UPDATE` and table-valued functions are rejected.
- A dialect sqlglot does not know falls back to its generic parser.
- Query results are converted to JSON-safe values: decimals to floats, dates and others to text.
- Database errors are returned as the driver's own message, without SQLAlchemy's wrapper.
- There is no query timeout (SQLite has no setting for one). See `PRODUCTION.md`.

- Teachers and students have a single `name` column.
- Courses have `code`, `title` and `credits`. There is no departments table.
- Terms are limited to `Spring` and `Fall`.
- Teachers have no email column; only student names collide in the seed.
- Primary keys are integer surrogates. Foreign keys are enforced.
- `build_db.py` uses the standard library `sqlite3` module, because it can run a multi-statement
  SQL script directly. All querying uses SQLAlchemy Core.
- `build_db.py` deletes and recreates the database on every run. The `.db` file is not committed.
- `university` is installed as a second package next to `sqlqa`, so tests can import the builder.
  `sqlqa` never imports it.
