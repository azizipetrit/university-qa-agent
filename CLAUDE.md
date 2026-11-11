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
  `docs/`, `chore/`), push it, open a PR, and stop. The owner reviews on GitHub and merges to `main`
  manually.
- **Authorship:** commits are under the owner's name only (no `Co-Authored-By` line) and PR
  descriptions carry no "Generated with" footer.
- **`.env.example` stays current:** any change to a setting (new, renamed, new default) updates
  `.env.example` and the Configuration section below in the same commit.
- **CI:** `.github/workflows/ci.yml` runs `ruff check`, `ruff format --check` and the offline tests
  on every PR and on `main`. It must stay green and must never need an API key.
- No abstraction without a second use. No dependency that isn't listed below without asking.

## Stack

- Python 3.13, managed with `uv` (`uv sync`, `uv run pytest`, `uv run sqlqa ...`).
- SQLite for the demo database; SQLAlchemy **Core** only (no ORM).
- LangGraph with an explicit `StateGraph`. No prebuilt agents (`create_react_agent` etc.).
- LLM created with langchain's `init_chat_model` from the `LLM_MODEL` env var. No provider-specific code.
  `langchain-anthropic` is the one provider package installed.
- `sqlglot` for SQL validation. `python-dotenv` loads `.env` in the CLI. `pytest` and `ruff` for dev.

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

```text
START → load_schema → generate_sql → validate_sql → execute_sql → answer → END
```

| Node | LLM | Responsibility |
| --- | --- | --- |
| `load_schema` | no | Introspect tables, columns, types, PKs, FKs; up to 3 sample rows per table (flag). |
| `generate_sql` | yes | Structured output: `status` (`sql` / `clarify` / `unanswerable`), `plan`, `sql` or question. |
| `validate_sql` | no | sqlglot guard (see constraint 2). |
| `execute_sql` | no | Run read-only, apply row limit, record rows / truncation / error. |
| `answer` | yes | Structured output: a final answer, **or** a request for another query with its reason. At the query cap it gets a schema that can only answer. |
| `give_up` | no | Honest failure message with the last error. |

Routing:

- **Repair loop:** a validation or DB error routes back to `generate_sql` with the error text, up to
  `MAX_ATTEMPTS` (3) per query; then `give_up`.
- **Empty result:** reviewed by `answer`, which runs after every query. Its prompt flags 0 rows and
  all-NULL results; it either answers "nothing matched" or requests a corrected query (which counts
  towards `MAX_QUERIES`). There is no separate review node.
- **Overall cap:** `MAX_LLM_CALLS` (10) per run, on top of the two caps above. When hit, the run goes
  to `give_up`.
- **Attempts reset** to 0 after each query that executes, so each query gets its own 3 attempts.
- **Bad model output** (does not fit the schema) counts as a failed attempt in `generate_sql`, and
  ends the run as `failed` in `answer`.
- **Multi-step loop:** `answer` may request another query (with a reason), which routes to
  `generate_sql` with all previous queries and results. Cap: `MAX_QUERIES` (3) executed queries per
  question. At the cap, `answer` must answer with what is available and say the cap was hit.
  The single-query path makes no extra LLM call.
- **Clarification:** `clarify` / `unanswerable` end the run with the question or explanation. No
  interrupt or checkpointer (described in `DESIGN.md` as the upgrade path).

## Layout

```text
src/sqlqa/            generic agent, no university knowledge
  config.py           env settings
  db.py               engine, read-only setup, introspection, execute
  sql_guard.py        sqlglot validation (pure function)
  llm.py              init_chat_model, structured output models, call_llm (the only LLM call site)
  prompts.py          templates; placeholders only, no schema names
  state.py            TypedDict state
  nodes.py            Nodes class: one method per node
  graph.py            build_graph(llm, db, tracer, settings), routers, run(question, ...)
  tracing.py          JSON tracer, Markdown renderer
  cli.py              ask, render-trace
university/           schema.sql, seed.sql, notes.md, golden_questions.json, build_db.py
tests/                fakes.py holds the scripted FakeLLM and the ask() helper
docs/                 DESIGN.md, PRODUCTION.md, EXAMPLES.md, traces/
README.md             quick start, diagram, requirement map, AI usage
```

Dependencies (LLM, engine, tracer, settings) are passed into `build_graph`; nodes never read globals
or env vars.

## Configuration (env)

Read once in `config.py`; `.env.example` documents every one.

- Required: `DATABASE_URL`, `LLM_MODEL` (`provider:model`), plus the provider's API key.
- Optional: `SCHEMA_NOTES_FILE`, `STRUCTURED_OUTPUT_METHOD` (empty = provider default; `json_schema`
  for Anthropic), `SAMPLE_ROWS` (3; 0 disables), `MAX_ROWS` (50), `MAX_ATTEMPTS` (3),
  `MAX_QUERIES` (3), `MAX_LLM_CALLS` (10), `TRACE_DIR` (`traces`).
- LangSmith uses its own standard env vars.

## Database (university/)

`teachers`, `students` (unique `email`; names may repeat), `courses`, `semesters`,
`course_offerings` (unique course + teacher + semester), `enrollments` (unique student + offering,
`grade`). Grade is numeric 0–100, **NULL while in progress**. The seed is hand-written; edge cases
are marked `EDGE` in `seed.sql` (duplicate student name, retakes, NULL grades, an entirely ungraded
latest semester, a student with no enrollments, a course never offered).
Build with `uv run python -m university.build_db`.

## Tracing

- Nodes are wrapped in one place in `graph.py` (`tracer.wrap`): name, state update, duration, error.
  Node inputs are not stored; the state at any point is the earlier updates applied in order.
- All LLM calls go through `call_llm`, which records full prompt, parsed and raw reply, token usage.
- Each query in a run is recorded with its SQL, the reason it was requested, and its result.
- One file per run: `traces/<timestamp>-<id>.json` (gitignored), written even if the run crashes.
- The JSON stores everything. `render-trace` produces Markdown that shows the schema once at the top
  and replaces it with a placeholder inside each prompt.

## Tests

- `test_db`: joins and aggregations over the seed data with known answers; averages ignore NULL grades.
- `test_sql_guard`: accepts CTEs/unions; rejects writes, multi-statement, unknown tables, DML in CTEs.
- `test_database`: introspection, execution, row limit, read-only without the guard.
- `test_generation`: prompt contains the introspected schema; output handling; feedback on retry.
- `test_agent`: happy path, repair, blocked write, clarify, empty and NULL results, attempts
  exhausted, multi-query, query cap, LLM-call cap, crash.
- `test_tracing`: JSON contents, Markdown view, `render-trace`, settings.
- `test_live` (marker `live`, excluded by default; run with `uv run pytest -m live`): the golden
  questions in `university/golden_questions.json` against the real model. Run it after any change
  to prompts, notes or the model. A new wrong answer becomes a new golden question.
- Agnosticism: the same graph runs against an unrelated toy schema; `src/` is grepped for university
  table names.

## Docs

- `DESIGN.md`: decisions and trade-offs, an "Assumptions" section, upgrade paths.
- `PRODUCTION.md`: reliability, scalability, monitoring, security, deployment; includes the privacy
  implication of sending sample rows to the LLM provider.
- `EXAMPLES.md` and `docs/traces/`: example questions, outputs, committed traces (JSON and
  Markdown), and the debugging walkthrough. A scripted trace must say so in its header.
- `README.md`: keep the requirement map and the test counts in step with the code.
- Schema meaning goes in `university/notes.md`, never in `src/` or the prompts. Keep it short and
  factual.

## Phases

1. Project scaffold, university schema + seed, `test_db`.
2. `db.py` introspection/execution and `sql_guard.py`, with tests.
3. Agent and tracing together: state, prompts, nodes, graph, tracer, CLI, fake LLM, tests.
4. Docs, example runs, committed traces, live evaluation set.

All four phases are done. Later changes follow the same rules: questions first, tests shown before
committing, one branch and one PR per change.
