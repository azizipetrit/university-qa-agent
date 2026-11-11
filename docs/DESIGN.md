# Design

This document grows with each phase. So far: the university database, the database layer, the SQL
guard, the agent graph and tracing.

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

## The agent (`src/sqlqa/graph.py`, `nodes.py`)

An explicit LangGraph `StateGraph` with six nodes. Two call the model; four are plain code.

```text
load_schema -> generate_sql -> validate_sql -> execute_sql -> answer -> END
                    ^               |               |            |
                    +---------------+---------------+------------+
                     guard rejects    database        answer asks for
                     the SQL          rejects it      another query
```

| Node | Model? | What it does |
|---|---|---|
| `load_schema` | no | Reads the schema from the live database. |
| `generate_sql` | yes | Returns SQL with a short plan, or a clarifying question, or "cannot answer". |
| `validate_sql` | no | Runs the SQL guard. |
| `execute_sql` | no | Runs the query read-only and records the result. |
| `answer` | yes | Returns the answer, or asks for one more query and says why. |
| `give_up` | no | Ends the run with an honest failure message when a limit is hit. |

The model never chooses which node runs next. It returns a small structured object, and plain
Python functions in `graph.py` (the routers) read the state and pick the next node. That keeps the
control flow readable in one file and testable without a model.

### Three loops, three limits

| What sends the run back to `generate_sql` | Limit | Default |
|---|---|---|
| The guard or the database rejects the SQL. The error text goes back to the model. | Attempts per query | 3 |
| `answer` asks for another query, with a reason. | Queries per question | 3 |
| Either of the above. | Model calls per run | 10 |

- The attempt counter starts again after each query that runs, so every query gets its own three
  attempts.
- A normal question costs two model calls: one to write SQL, one to answer. There is no planning
  or review call in front of that.
- When the query limit is reached, `answer` is given a different output shape that has no "ask for
  another query" option, so it has to answer. If it marks the answer incomplete, the code appends
  a fixed sentence saying the limit was reached. The model is not trusted to remember to say it.
- When the attempt limit or the model-call limit is reached, `give_up` reports the last SQL and the
  last error. It does not call the model.

### Multi-step questions

Most questions need one query, because SQL can already express several steps with CTEs and
subqueries. The loop is for questions where the second query depends on what the first one
returned. Each query is stored with the reason it was run, and both appear in the trace.

### Empty and NULL results

There is no separate review step. `answer` runs after every query and its prompt states plainly
when a result has 0 rows or only NULLs. It then either answers ("nothing matches", "not graded
yet") or asks for a corrected query, for example when a filter value was written differently from
the data. A corrected query counts as one of the three queries.

### Ambiguous questions

- If the question cannot be mapped to the schema without guessing ("who is the best?"),
  `generate_sql` ends the run with one clarifying question.
- If a reasonable reading exists, the model takes it and states the assumption in its plan, and
  the answer repeats it.
- If the results show several matches for one name, the answer reports each one separately.

**Upgrade path: a real back-and-forth.** Today the run ends and the user asks again with more
detail. LangGraph supports pausing instead: the node calls `interrupt()` with the clarifying
question, a checkpointer saves the state, and the run resumes from the same point when the user
replies. That needs a checkpointer (a database table in production), a thread id per conversation,
and a CLI or API that can resume a run. It is the right next step for a chat interface and too
much machinery for a one-shot command.

### Structured output

Both model nodes use `with_structured_output` with a Pydantic model, so the reply is an object with
named fields, not text to parse. A reply that does not fit is treated like any other failure: in
`generate_sql` it counts as an attempt and the model is told; in `answer` the run ends as failed.

### Swapping the model

`LLM_MODEL` is passed to langchain's `init_chat_model`, so `provider:model` selects any supported
provider. Nothing in `src/` imports a provider. All model calls go through one function,
`call_llm`, which is also where tracing happens.

## Tracing (`src/sqlqa/tracing.py`)

Every run writes one JSON file. It is written by our own code, so it works offline and can be
committed to the repository as evidence.

- **Nodes:** `graph.py` wraps every node with `tracer.wrap` when it builds the graph. The wrapper
  records the node name, what the node changed in the state, how long it took, and any exception.
  No node contains tracing code.
- **Model calls:** `call_llm` records the full prompt, the parsed reply, the raw reply, token
  counts and duration, attached to the node that made the call.
- **Queries:** each one is stored with its reason, plan, SQL, columns, rows and truncation flag.
- **Failures:** if a node raises, the trace is still written, with status `crashed`.

`sqlqa render-trace <file>` turns the JSON into Markdown for reading: a summary, the path through
the graph, the schema once, then each step with its prompt, reply, SQL and result table. The JSON
keeps the schema inside every prompt exactly as it was sent; the Markdown replaces it with a
placeholder so the steps stay short.

LangSmith needs no code. Setting `LANGSMITH_TRACING=true` and a key sends the same run there as
well, because LangGraph and langchain report to it on their own.

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
- Each question is independent. There is no conversation memory, so "and in spring?" will not work.
- The schema is read again on every run, so each trace shows the schema that was actually used.
- Node inputs are not stored in the trace, only what each node changed. The state at any step is
  the earlier changes applied in order.
- Trace files go to `traces/`, which is not committed because they contain database rows. Chosen
  examples will be committed under `docs/traces/`.
- `STRUCTURED_OUTPUT_METHOD` is an optional setting passed to `with_structured_output`. Current
  Anthropic models do not allow forcing a tool call, which is langchain's default method for them,
  so `.env.example` sets `json_schema`. Left empty, the provider's default is used.
- No temperature is set. Current Anthropic models reject the parameter, and leaving it out keeps
  the code the same for every provider.
- The model is not told to round numbers. The answer shows what the database returned.
- The CLI loads `.env` from the directory it is run in. It exits with code 1 when a run fails and
  2 when a required setting is missing.
- LangGraph's own step limit is set to 60, above the worst case the three limits allow.
- The Markdown view shows the first 10 rows of each result; the JSON has all of them.
