# University QA Agent

Interview take-home (spec: `docs/HW Task.pdf`). A LangGraph agent that answers natural-language
questions over a SQL database. The owner must present and defend every line, so
**small and explainable beats clever**.

## Working rules

- **Before starting each phase**, list any edge cases or design choices that would change behaviour and
  ask about them in one batch, with a recommendation for each. Wait for the answer.
- **Small choices: don't ask.** Pick the simplest option and record it under "Assumptions" in
  `docs/DESIGN.md`.
- Do not start a phase without approval of the previous one.
- **Git:** never commit to `main`. After a phase's tests pass, show the results and wait for approval;
  then commit on a new branch named `<type>/<short-kebab-description>` (`feat/`, `fix/`, `test/`,
  `docs/`, `chore/`), push it, and stop. The owner reviews on GitHub and merges to `main` manually.
- No abstraction without a second use. No dependency that isn't listed below without asking.

## Stack

- Python 3.13, managed with `uv` (`uv sync`, `uv run pytest`, `uv run sqlqa ...`).
- SQLite for the demo database; SQLAlchemy **Core** only (no ORM).
- LangGraph with an explicit `StateGraph`. No prebuilt agents (`create_react_agent` etc.).
- LLM created with langchain's `init_chat_model` from the `LLM_MODEL` env var. No provider-specific code.
- `sqlglot` for SQL validation. `pytest` for tests.

## Hard constraints

1. **DB-agnostic.** The schema is introspected at runtime from `DATABASE_URL`. No table or column
   names from the university schema in `src/` or in prompts. The sqlglot dialect comes from the engine.
   Everything university-specific lives in `university/`.
2. **Read-only execution**, three layers: sqlglot validation (single statement, SELECT root, no DML/DDL
   nodes anywhere, only tables present in the introspected schema); row limit (`MAX_ROWS`, fetch
   `MAX_ROWS + 1` to detect truncation); read-only connection (SQLite: `PRAGMA query_only=ON`).
3. **Tests run offline** with a scripted fake LLM. Tests needing a real model are marked `live` and
   skipped without an API key.
4. **Every run is traced** to one local JSON file, renderable to Markdown. LangSmith is optional and
   enabled by env vars only.

## Graph

```
START → load_schema → generate_sql → validate_sql → execute_sql → answer → END
```

| Node | LLM | Responsibility |
|---|---|---|
| `load_schema` | no | Introspect tables, columns, types, PKs, FKs; up to 3 sample rows per table (flag). |
| `generate_sql` | yes | Structured output: `status` (`sql` / `clarify` / `unanswerable`), `plan`, `sql` or question. |
| `validate_sql` | no | sqlglot guard (see constraint 2). |
| `execute_sql` | no | Run read-only, apply row limit, record rows / truncation / error. |
| `answer` | yes | Structured output: a final answer, **or** a request for another query with its reason. |
| `give_up` | no | Honest failure message with the last error. |

Routing:

- **Repair loop:** a validation or DB error routes back to `generate_sql` with the error text, up to
  `MAX_ATTEMPTS` (3) per query; then `give_up`.
- **Empty result:** one review pass after every query (not only the last), where the model confirms
  empty is correct or fixes the SQL.
- **Overall cap:** one limit on LLM calls per run (`MAX_LLM_CALLS`), on top of the two caps above.
- **Multi-step loop:** `answer` may request another query (with a reason), which routes to
  `generate_sql` with all previous queries and results. Cap: `MAX_QUERIES` (3) executed queries per
  question. At the cap, `answer` must answer with what is available and say the cap was hit.
  The single-query path makes no extra LLM call.
- **Clarification:** `clarify` / `unanswerable` end the run with the question or explanation. No
  interrupt or checkpointer (described in `DESIGN.md` as the upgrade path).

## Layout

```
src/sqlqa/            generic agent, no university knowledge
  config.py           env settings
  db.py               engine, read-only setup, introspection, execute
  sql_guard.py        sqlglot validation (pure function)
  llm.py              init_chat_model, structured output models
  prompts.py          templates; placeholders only, no schema names
  state.py            TypedDict state
  nodes.py            node functions
  graph.py            build_graph(llm, db, tracer, settings), routers
  tracing.py          JSON tracer, Markdown renderer
  cli.py              ask, render-trace
university/           schema.sql, seed.sql, build_db.py
tests/
docs/                 DESIGN.md, PRODUCTION.md, EXAMPLES.md, traces/
```

Dependencies (LLM, engine, tracer, settings) are passed into `build_graph`; nodes never read globals
or env vars.

## Configuration (env)

`DATABASE_URL`, `LLM_MODEL`, `MAX_ROWS`, `MAX_ATTEMPTS` (3), `MAX_QUERIES` (3), `MAX_LLM_CALLS`,
`SAMPLE_ROWS` (3; 0 disables), `SCHEMA_NOTES_FILE` (optional), `TRACE_DIR`. `MAX_ROWS` defaults to 50. LangSmith uses its own standard env vars.

## Database (university/)

`teachers`, `students` (unique `email`; names may repeat), `courses`, `semesters`,
`course_offerings` (unique course + teacher + semester), `enrollments` (unique student + offering,
`grade`). Grade is numeric 0–100, **NULL while in progress**. The seed is hand-written; edge cases
are marked `EDGE` in `seed.sql` (duplicate student name, retakes, NULL grades, an entirely ungraded
latest semester, a student with no enrollments, a course never offered).
Build with `uv run python -m university.build_db`.

## Tracing

- Nodes are wrapped in one place in `graph.py`: name, input, state update, duration, error.
- All LLM calls go through one helper that records prompt, raw response, and token usage.
- Each query in a run is recorded with its SQL, the reason it was requested, and its result.
- One file per run: `traces/<timestamp>_<run_id>.json`. `render-trace` produces Markdown showing
  question → nodes → SQL → rows → answer.

## Tests

- `test_db`: joins and aggregations over the seed data with known answers; averages ignore NULL grades.
- `test_sql_guard`: accepts CTEs/unions; rejects writes, multi-statement, unknown tables, DML in CTEs.
- `test_generation`: prompt contains the introspected schema; output parsing; feedback on retry.
- `test_agent`: happy path, repair, blocked write, clarify, empty result, attempts exhausted,
  multi-query, query cap hit, trace contents.
- Agnosticism: the same graph runs against an unrelated toy schema; `src/` is grepped for university
  table names.

## Docs

- `DESIGN.md`: decisions and trade-offs, an "Assumptions" section, upgrade paths.
- `PRODUCTION.md`: reliability, scalability, monitoring, security, deployment; includes the privacy
  implication of sending sample rows to the LLM provider.
- `EXAMPLES.md` and `docs/traces/`: example questions, outputs, and committed traces.

## Phases

1. Project scaffold, university schema + seed, `test_db`.
2. `db.py` introspection/execution and `sql_guard.py`, with tests.
3. State, prompts, nodes, graph, fake LLM, `test_generation` and `test_agent`.
4. Tracing and CLI.
5. Docs, example runs, committed traces, live evaluation set.
