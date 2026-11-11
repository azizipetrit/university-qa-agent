# Production considerations

This project is a working prototype. This document says what exists today, what is missing for
production, and what to do first, for each of the five topics in the assignment.

## Reliability

**Today**

- Rejected SQL is repaired, with limits on attempts, queries and model calls, so a run always ends.
- A run that fails says so, with the last error. A run that crashes still writes its trace.
- 234 offline tests, and a 17-question live evaluation that caught a real wrong answer (see
  [EXAMPLES.md](EXAMPLES.md#debugging-a-wrong-answer)).

**Missing**

- **Query timeout.** A slow query blocks the run. Set a statement timeout on the database role
  (`statement_timeout` in PostgreSQL).
- **Model outages.** The provider's client retries a few times; beyond that the run crashes. Add a
  request timeout, a fallback model, and a clear message to the user.
- **Evaluation in CI.** The live evaluation is run by hand. Run it on every prompt or model change
  and nightly, and grow it from real failed questions.
- **Deeper answer checking.** `verify_answer` confirms that every number and date in the answer
  was fetched by the run. It does not check names, or that a figure is attached to the right
  thing, or arithmetic. A second, cheap model call could check those for high-stakes use.

**First step:** statement timeout and model request timeout. Both are configuration.

## Scalability

**Today**

- The agent keeps no state between questions, so several copies can run side by side.
- The schema is read from the database on every run, and the whole schema goes into each prompt.

**Missing**

- **Large schemas.** Hundreds of tables will not fit in a prompt, and would cost too much if they
  did. Add a step before `generate_sql` that selects the relevant tables, by keyword or embedding
  search over table and column descriptions. This is a new node, not a redesign.
- **Schema caching.** Cache the introspected schema and refresh it on a schedule or on a schema
  change.
- **Database load.** Point the agent at a read replica, and use a connection pool sized for the
  expected traffic.
- **Concurrency.** The graph is called synchronously. LangGraph supports async invocation, which
  matters behind a web API.
- **Cost.** Cache the schema part of the prompt with the provider's prompt caching, and use a
  smaller model for the answer step if the evaluation shows it holds up.

**First step:** schema caching and a read replica. Table selection only when the schema needs it.

## Monitoring and tracing

**Today**

- One JSON trace per run with every node, prompt, reply, SQL statement, guard verdict, row, timing
  and token count, and a Markdown view of it.
- LangSmith can be switched on with environment variables.

**Missing**

- **Central storage.** Traces are local files. Send them to a store that can be searched:
  LangSmith, or an OpenTelemetry backend the organisation already runs.
- **Metrics and alerts.** Track the share of runs by status (`answered`, `clarify`, `unanswerable`,
  `failed`, `crashed`), repair attempts per run, queries per run, latency, and tokens per run.
  Alert when the failed share or the cost rises.
- **User feedback.** A thumbs up or down attached to the run id, so bad answers can be found and
  added to the evaluation set.
- **Redaction.** Traces contain database rows and user questions. Decide what may be stored and
  for how long before sending them anywhere.

**First step:** ship traces to one searchable place with a retention policy.

## Security

**Today**

- Three read-only layers: the SQL guard, a transaction that is always rolled back, and a
  connection that refuses writes (SQLite only).
- Only introspected tables can be queried, which also blocks system tables.
- A row limit on every query.
- The API key is read from the environment and `.env` is not committed.

**Missing**

- **A SELECT-only database user. This is required, not optional.** On any database other than
  SQLite, the connection-level protection in this code does not apply. The database user the agent
  connects with must have SELECT rights only, granted on only the tables it should see. This is
  stronger than anything the application does, because the database enforces it.
- **Data sent to the model provider.** Three kinds of data leave the system: the schema, up to
  three sample rows per table, and the rows returned by each query. The sample rows are sent on
  every run, whatever the question. For sensitive data, set `SAMPLE_ROWS=0` (the agent still works,
  as example 3 in [EXAMPLES.md](EXAMPLES.md) shows), hide sensitive tables and columns from
  introspection, and check the provider's data retention terms. Query results still go to the
  model, so column-level restrictions belong in the database role.
- **Values shown by the filter check.** When a query returns nothing, the agent tells the model
  which values a filtered column holds, if that is a short list (at most `FILTER_VALUE_LIMIT`
  distinct values), or the real spelling of a wrongly cased value. Columns with many distinct
  values, such as names or emails, are never listed. Set `FILTER_VALUE_LIMIT=0` to send none.
- **Per-user access.** Every user currently sees everything the database user can see. Real use
  needs the user's identity passed through, with row-level security or per-role views in the
  database.
- **Prompt injection through data.** Returned rows are placed in the answer prompt. Text stored in
  the database could try to instruct the model. It cannot cause a write, because of the layers
  above, but it could distort an answer. Treat answers over untrusted text columns with care.
- **Abuse and cost.** Add authentication, rate limits per user, and a spending cap.
- **Secrets.** Move the API key and database credentials to a secrets manager.

**First step:** the SELECT-only database user with a table allowlist.

## Deployment

**Today**

- A command-line tool, configured entirely by environment variables, with pinned dependencies
  (`uv.lock`).
- GitHub Actions runs the lint and the offline tests on every pull request and on `main`.

**Missing**

- **A service.** Wrap `run()` in a small HTTP API (for example FastAPI) with one endpoint that
  takes a question and returns the answer, the SQL and the run id.
- **A container.** One image, with configuration and secrets injected at start.
- **A release pipeline.** The live evaluation should run before a prompt or model change is
  released, with the key held as a CI secret, followed by a build and deploy step.
- **Versioning.** Record the prompt version and model name in each trace (the model name already
  is), so a change in behaviour can be tied to a change in configuration.
- **Conversations.** For a chat interface, add a LangGraph checkpointer and `interrupt()` so a
  clarifying question pauses the run instead of ending it. See [DESIGN.md](DESIGN.md).

**First step:** the HTTP wrapper and the container. The agent code does not need to change.
