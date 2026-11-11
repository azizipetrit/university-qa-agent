"""The graph's nodes. Each takes the state and returns only the keys it changes."""

from sqlqa import prompts
from sqlqa.answer_check import check_answer
from sqlqa.config import Settings
from sqlqa.db import Database
from sqlqa.filter_check import check_filters
from sqlqa.llm import AnswerDecision, FinalAnswer, SqlDecision, call_llm
from sqlqa.sql_guard import check_sql
from sqlqa.state import Query, State, is_empty

BAD_FORMAT = "The reply did not match the required format."
NOTE = "\n\nNote: "  # starts every sentence this code appends to a model-written answer


class Nodes:
    def __init__(self, llm, db: Database, tracer, settings: Settings):
        self.llm = llm
        self.db = db
        self.tracer = tracer
        self.settings = settings

    def _ask(self, schema, messages):
        return call_llm(
            self.llm, schema, messages, self.tracer, self.settings.structured_output_method
        )

    def load_schema(self, state: State) -> dict:
        """Read the schema from the live database. No model call."""
        return {
            "schema": self.db.describe_schema(),
            "tables": self.db.table_names(),
            "dialect": self.db.dialect,
        }

    def generate_sql(self, state: State) -> dict:
        """Ask the model for a query, a clarifying question, or 'cannot answer'."""
        decision = self._ask(SqlDecision, prompts.sql_messages(state))
        update = {
            "llm_calls": state["llm_calls"] + 1,
            "attempts": state["attempts"] + 1,
            "sql": None,
            "error": None,
        }
        if decision is None:
            update["error"] = BAD_FORMAT
        elif decision.status != "sql":
            update["status"] = decision.status
            update["answer"] = decision.message or ""
        elif not (decision.sql or "").strip():
            update["error"] = "The status was 'sql' but no SQL was given."
        else:
            update["plan"] = decision.plan
            update["sql"] = decision.sql.strip()
        return update

    def validate_sql(self, state: State) -> dict:
        """Check the query is a single read-only SELECT over known tables. No model call."""
        return {"error": check_sql(state["sql"], state["tables"], state["dialect"])}

    def execute_sql(self, state: State) -> dict:
        """Run the query. A database error goes back to the model; a result is recorded."""
        result = self.db.execute(state["sql"])
        if result.error:
            return {"error": f"The database rejected the query: {result.error}"}
        query = Query(
            reason=state["reason"],
            plan=state["plan"],
            sql=state["sql"],
            columns=result.columns,
            rows=result.rows,
            truncated=result.truncated,
            filter_check=None,
        )
        if is_empty(result.rows):
            # Not recorded yet: check_filters adds its finding first.
            return {"pending": query, "attempts": 0, "error": None}
        return {"queries": [query], "attempts": 0, "error": None}

    def check_filters(self, state: State) -> dict:
        """After an empty result: do the query's filter values exist at all? No model call."""
        query = state["pending"]
        finding = check_filters(
            query["sql"],
            self.db,
            max_probes=self.settings.max_filter_probes,
            value_limit=self.settings.filter_value_limit,
        )
        return {"queries": [query | {"filter_check": finding}], "pending": None}

    def answer(self, state: State) -> dict:
        """Ask the model to answer from the results, or to request one more query."""
        limit = self.settings.max_queries
        at_limit = len(state["queries"]) >= limit
        decision = self._ask(
            FinalAnswer if at_limit else AnswerDecision, prompts.answer_messages(state, limit)
        )
        update = {"llm_calls": state["llm_calls"] + 1}

        if decision is None:
            return update | {"status": "failed", "answer": f"Could not answer. {BAD_FORMAT}"}

        if at_limit:
            text = decision.answer
            if not decision.complete:
                text += (
                    f"{NOTE}this answer may be incomplete. "
                    f"The limit of {limit} queries per question was reached."
                )
            return update | {"status": "answered", "answer": text}

        if decision.status == "need_query":
            # No status is set, so the graph loops back to generate_sql with this reason.
            return update | {"reason": decision.reason or "", "sql": None, "error": None}

        return update | {"status": "answered", "answer": decision.answer or ""}

    def verify_answer(self, state: State) -> dict:
        """Check every figure in the answer against what the run fetched. No model call."""
        written = state["answer"].split(NOTE)[0]  # not the notes this code added itself
        finding = check_answer(written, state["queries"], state["question"], notes=self.db.notes())
        update = {"verification": finding}
        if finding["unsupported"]:
            figures = ", ".join(finding["unsupported"])
            update["answer"] = (
                f"{state['answer']}{NOTE}these figures were not found in the query results "
                f"and could not be verified: {figures}."
            )
        return update

    def give_up(self, state: State) -> dict:
        """End the run honestly when a limit is hit. No model call."""
        if state["llm_calls"] >= self.settings.max_llm_calls:
            why = f"the limit of {self.settings.max_llm_calls} model calls per question was reached"
        else:
            why = f"no valid query was produced in {self.settings.max_attempts} attempts"
        lines = [f"I could not answer this question: {why}."]
        if state.get("error"):
            lines.append(f"Last error: {state['error']}")
        if state.get("sql"):
            lines.append(f"Last SQL: {state['sql']}")
        if state["queries"]:
            count = len(state["queries"])
            lines.append(f"{count} earlier query(ies) did run; their results are in the trace.")
        return {"status": "failed", "answer": "\n".join(lines)}
