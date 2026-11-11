"""Settings, read once from environment variables."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    llm_model: str  # passed to init_chat_model, e.g. "provider:model-name"
    structured_output_method: str | None = None  # None = the provider's default
    schema_notes_file: str | None = None
    sample_rows: int = 3  # per table in the schema text; 0 sends no data rows
    max_rows: int = 50  # rows returned per query
    max_attempts: int = 3  # tries to get one valid query
    max_queries: int = 3  # queries executed per question
    max_llm_calls: int = 10  # hard stop for the whole run
    trace_dir: str = "traces"

    @classmethod
    def from_env(cls) -> "Settings":
        missing = [name for name in ("DATABASE_URL", "LLM_MODEL") if not os.environ.get(name)]
        if missing:
            raise ValueError(
                f"Missing setting(s): {', '.join(missing)}. "
                "Copy .env.example to .env and fill it in."
            )

        def number(name: str, default: int) -> int:
            return int(os.environ.get(name) or default)

        return cls(
            database_url=os.environ["DATABASE_URL"],
            llm_model=os.environ["LLM_MODEL"],
            structured_output_method=os.environ.get("STRUCTURED_OUTPUT_METHOD") or None,
            schema_notes_file=os.environ.get("SCHEMA_NOTES_FILE") or None,
            sample_rows=number("SAMPLE_ROWS", cls.sample_rows),
            max_rows=number("MAX_ROWS", cls.max_rows),
            max_attempts=number("MAX_ATTEMPTS", cls.max_attempts),
            max_queries=number("MAX_QUERIES", cls.max_queries),
            max_llm_calls=number("MAX_LLM_CALLS", cls.max_llm_calls),
            trace_dir=os.environ.get("TRACE_DIR") or cls.trace_dir,
        )
