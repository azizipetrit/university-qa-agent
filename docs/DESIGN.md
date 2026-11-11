# Design

Why the system is built the way it is. Each section names a decision, the alternative, and the
cost. Small choices that were not worth a discussion are listed under Assumptions at the end.

Related: [EXAMPLES.md](EXAMPLES.md) for runs and traces, [PRODUCTION.md](PRODUCTION.md) for what a
production version needs.

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
(`university/notes.md`, seven lines) that is appended to the schema text. Moving to another database
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
| --- | --- | --- |
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

An explicit LangGraph `StateGraph` with seven nodes. Two call the model; five are plain code.

```text
load_schema -> generate_sql -> validate_sql -> execute_sql -----------------> answer -> END
                    ^               |               |    \                  ^   |
                    |               |               |     +-> check_filters -+   |
                    +---------------+---------------+----------------------------+
                     guard rejects    database        (only when the result      answer asks for
                     the SQL          rejects it       is empty)                 another query
```

| Node | Model? | What it does |
| --- | --- | --- |
| `load_schema` | no | Reads the schema from the live database. |
| `generate_sql` | yes | Returns SQL with a short plan, or a clarifying question, or "cannot answer". |
| `validate_sql` | no | Runs the SQL guard. |
| `execute_sql` | no | Runs the query read-only and records the result. |
| `check_filters` | no | Only after an empty result: asks the database whether the query's filter values exist. |
| `answer` | yes | Returns the answer, or asks for one more query and says why. |
| `give_up` | no | Ends the run with an honest failure message when a limit is hit. |

The model never chooses which node runs next. It returns a small structured object, and plain
Python functions in `graph.py` (the routers) read the state and pick the next node. That keeps the
control flow readable in one file and testable without a model.

### Three loops, three limits

| What sends the run back to `generate_sql` | Limit | Default |
| --- | --- | --- |
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

How often the real model uses it: rarely for compound questions. Six were tried on default
settings with `anthropic:claude-haiku-5-5`. Five were answered correctly with a single query,
including "which student improved most on a retake, and what are they taking now". One open-ended
question used a second query in about half of its runs, which was too unreliable to keep as an
example or a golden question.

The dependable case is a corrected filter. When a query returns nothing and the filter check
shows that the value exists in another form, `answer` asks for a second query with the real
value. Example 12 in [EXAMPLES.md](EXAMPLES.md) is such a run on default settings, and it is a
golden question.

### Empty results: evidence, not a guess (`src/sqlqa/filter_check.py`)

An empty result has two very different causes. Either the data really has no such rows, or the
query asked for a value the column does not hold: a term that does not exist, or a code written
in a different case. From the result alone the two look the same.

**What was tried first, and why it was not enough.** The answer prompt used to say "judge whether
nothing matches is the true answer or the query was probably wrong". The model then guessed. In
repeated runs of one question it sometimes asked for a second query and sometimes did not, and a
stronger prompt rule only moved the odds. A prompt cannot make this dependable, because the model
has no evidence to judge with.

**What it does now.** When a result has 0 rows or only NULLs, `check_filters` runs before
`answer`. It makes no model call.

1. It reads the simple literal comparisons out of the SQL with sqlglot: `column = value`,
   `column IN (...)`, `column LIKE pattern`, each also with `LOWER()` or `UPPER()` around the
   column.
2. For each one it asks the database whether any row in that table has that value.
3. If none has, it reports the same value in a different case if there is one. Otherwise it lists
   the values the column holds, when that is a short list.

The finding is stored on the query, shown to `answer` under the result, and written to the trace.
`answer` is told to rely on it:

| The check says | `answer` does |
| --- | --- |
| A value does not exist | Says so and names the values that do. No further query is needed. |
| The value exists in another form, such as a different case | Asks for a second query with the real value. |
| Every checked value exists | Treats the empty result as real: nothing matches that combination. |

**Why "only NULLs" counts as empty.** An aggregate over no matching rows returns one row holding
NULL, not zero rows. `AVG(grade)` for a course code that does not exist comes back as a single
NULL, which reads exactly like "not graded yet". A row such as `Fall 2026, NULL` is not empty in
this sense, because the query did find the semester.

**How it stays safe and database-agnostic.**

- Probes are built with SQLAlchemy Core and bound parameters. No text from the model's query is
  executed again, so the trade-off above about running only the model's own SQL still holds, and
  a quote inside a value is just a character.
- Table and column names come from the introspected schema. Nothing is specific to one database.
- A comparison is checked only if its column resolves to exactly one real table. Anything else is
  skipped and counted, and the count is shown: ranges, negations, expressions, columns of a CTE,
  an unqualified column that two tables share.
- At most `MAX_FILTER_PROBES` (5) comparisons are checked per query.

**What it reveals, and the switch for it.** Values listed by the check are sent to the model
provider, like sample rows. Three rules limit that, controlled by `FILTER_VALUE_LIMIT` (10):

- a wrongly cased value gets only its one real spelling;
- other values are listed only if the column has at most 10 distinct values, so a column of names
  or emails is never listed;
- set to 0, no values are shown at all: the finding says only found or not found.

**What it costs.** Nothing on a normal run. On an empty result, a few small queries and no model
call. A wrongly cased value still costs a second query, which counts as one of the three.

**What it does not do.** It checks each value on its own, not the combination. If every value
exists and the result is still empty, it cannot say whether the rows matched and hold NULLs or
the combination matched nothing. Telling those apart would need a rewritten `COUNT(*)` version of
the model's query, which means executing SQL that sqlglot generated.

### Ambiguous questions

- If the question cannot be mapped to the schema without guessing ("how did they do?"),
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

## Testing and evaluation

Two kinds of test answer two different questions.

- **Does the code do what it should?** 169 offline tests. The model is replaced by a scripted
  stand-in (`tests/fakes.py`) that returns prepared replies in order and records every prompt. The
  database, the guard, the graph and the tracer are all real. These tests are exact and free, and
  they cannot say anything about the quality of a real model's SQL.
- **Does the real model answer correctly?** 16 golden questions in
  `university/golden_questions.json`, run with `uv run pytest -m live`. Each lists the values a
  correct run must have fetched (for example `73.75`), and the expected status. Whole result sets
  are not compared, because a correct query that selects one extra column would fail. Two of
  them name a value that is not in the data (a course code that does not exist, and one written in
  the wrong case); for those the run must also contain a filter check that reported it.

The live evaluation is not run by default, because it needs a key and costs money.

### Scores

| Model | Schema notes | Result | Runs |
| --- | --- | --- | --- |
| `anthropic:claude-opus-5-5` | Before the "latest semester" line | 13 of 14 | 1 |
| `anthropic:claude-opus-5-5` | Current | 14 of 14 | 1 |
| `anthropic:claude-haiku-5-5` (the default) | Current | 14 of 14 | 3 |
| `anthropic:claude-haiku-5-5` (the default) | Current, with the open-ended question added | 15 of 15 | 1 |
| `anthropic:claude-haiku-5-5` (the default) | Current, with `check_filters` and two more questions | 16 of 17, then 17 of 17 | 2 |
| `anthropic:claude-haiku-5-5` (the default) | Current, after the two question changes below | 16 of 16 | 1 |

- The one Opus failure was a real misreading of "latest semester". It is written up in
  [EXAMPLES.md](EXAMPLES.md#debugging-a-wrong-answer), and fixed by one line in the schema notes.
- That failure does not reproduce on Haiku. With the line removed, Haiku picked the true latest
  semester in three of three runs. The walkthrough therefore keeps the Opus traces.
- Haiku is the default because it passes the same evaluation at a fraction of the cost. The
  evaluation is 14 questions on a six-table schema, so this says the smaller model is enough here,
  not that it would be on a larger or messier database.
- The third Haiku run was made after one rule was added to the SQL prompt (see Assumptions).
- The 16 of 17 run failed on an open-ended question ("is anything unusual about the grades?").
  The answer was correct, but it derived the number of ungraded enrollments instead of selecting
  it, and the check only sees selected values. That question was then removed: it was the least
  reliable thing in the evaluation and measured the check more than the agent.
- The clarification question was changed from "Who is the best?" to "How did they do?". The first
  has a reasonable reading (highest average grade), so the prompt allows the model to answer it
  with a stated assumption, and on Haiku it did so in two runs out of five. The second has no
  reasonable reading and got a clarifying question in five runs out of five.
- The two `check_filters` questions passed in every run made of them.
- These are counts from single runs, and model output varies between runs. Re-run the evaluation
  after changing the model, the prompts or the notes.

## Known limitations

- **Only SQLite has been run.** Other databases are supported by design and untested.
- **No query timeout.**
- **The whole schema goes into every prompt.** Fine for six tables, not for six hundred.
- **The guard checks tables, not columns or functions.** A wrong column costs one repair attempt.
- **The answer is not verified against the rows.** The answer prompt forbids invented values, and
  the trace lets a person check, but no code does.
- **The filter check covers simple comparisons only.** It reads `=`, `IN` and `LIKE` against a
  literal. A filter written another way (a range, a function other than `LOWER` or `UPPER`, a
  value computed in a subquery) is skipped, and the answer step is back to judging without
  evidence for that part.
- **The filter check does not test combinations.** See "What it does not do" above.
- **Whether a question is clarified or assumed is the model's call.** The prompt tells it to ask
  only when no reasonable reading exists. For a question on the border, such as "Who is the
  best?", Haiku asked in three runs out of five and assumed in two.
- **A gendered pronoun occasionally slips through.** The data does not record gender, and the
  answer prompt says to use a name or "they". It holds in the committed examples and nearly every
  run, but one test run on Haiku wrote "his course offerings" for a teacher. No code checks it.
- **No conversation memory.** Each question stands alone.
- **The live evaluation is small** and tied to the demo database. It shows the method more than it
  measures quality.
- **Model output varies.** No temperature is set, so the same question can produce different SQL
  on different runs. The golden questions passed on the runs recorded here; that is not a
  guarantee for every run.

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
- Live tests are excluded by default through pytest's `addopts`, and selected with `-m live`.
- A golden question passes when each expected value appears in some row of some query the run
  executed. Numbers match within 0.01; text matches case-insensitively as a substring.
- Live test traces go to `traces/live/`.
- `scripts/make_examples.py` produces the committed traces and `EXAMPLES.md`. Model names,
  answers, SQL and counts in `EXAMPLES.md` are read from the trace files, never typed. The
  commentary under each example is hand-written in the script and has to be re-checked after a
  re-run. The scripted trace is labelled in its header.
- SQL in `EXAMPLES.md` is re-laid-out by sqlglot for reading. The traces hold the original text.
- The answer prompt tells the model to refer to people by name or as "they", because an early run
  wrote "she" for a teacher and the data holds no such information.
- The README diagram is the unedited output of `draw_mermaid()`.
- The SQL prompt asks the model to also select the item a figure belongs to when the query has to
  find that item ("the latest", "the highest"). Without it, Haiku answered "the average is NULL"
  for the latest semester without being able to say which semester that was.
- An empty result is held in the state as `pending` and recorded in `queries` by `check_filters`,
  together with its finding. That keeps `queries` append-only: every query appears once in the
  trace, in the step that recorded it.
- An unqualified column in a filter is resolved against every real table the statement reads, not
  only the tables in the same subquery. If more than one has it, the comparison is skipped.
- Each value of an `IN` list is checked as its own comparison and counts towards the probe cap.
- The filter check does not run on a result that has at least one non-NULL value.
- `Database.execute` accepts SQL text or a SQLAlchemy Core statement; the probes use the latter.
