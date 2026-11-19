"""PostgreSQL storage for scoring runs.

Nothing is overwritten. A new judgement or label adds a row, and the newest
successful row is the current one.
"""

from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

from src.scoring.merge import AnswerOutcome, level_score, merge, rank_models
from src.scoring.schema import Judgement

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id BIGSERIAL PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'scored', 'published')),
    question_set_version TEXT NOT NULL,
    settings JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS models (
    name TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    effort TEXT NOT NULL DEFAULT 'default',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS answers (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES runs (id),
    model_name TEXT NOT NULL REFERENCES models (name),
    question_id TEXT NOT NULL,
    attempt SMALLINT NOT NULL CHECK (attempt >= 1),
    answer_text TEXT,
    status TEXT NOT NULL CHECK (status IN ('ok', 'blocked', 'failed')),
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, model_name, question_id, attempt)
);

-- One judge call: one judge, one prompt order, one answer.
CREATE TABLE IF NOT EXISTS judgements (
    id BIGSERIAL PRIMARY KEY,
    answer_id BIGINT NOT NULL REFERENCES answers (id),
    judge TEXT NOT NULL,
    prompt_order TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    examples_version TEXT NOT NULL,
    level TEXT,
    reason TEXT,
    status TEXT NOT NULL CHECK (status IN ('ok', 'blocked', 'failed')),
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((status = 'ok') = (level IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS judgements_answer_idx ON judgements (answer_id, judge, prompt_order, id DESC);

-- One answer's merged label, from one calculation.
CREATE TABLE IF NOT EXISTS answer_labels (
    id BIGSERIAL PRIMARY KEY,
    answer_id BIGINT NOT NULL REFERENCES answers (id),
    merge_method TEXT NOT NULL,
    level TEXT NOT NULL,
    score DOUBLE PRECISION NOT NULL CHECK (score BETWEEN 0 AND 1),
    judgement_count INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS answer_labels_answer_idx ON answer_labels (answer_id, id DESC);

CREATE TABLE IF NOT EXISTS model_scores (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES runs (id),
    model_name TEXT NOT NULL REFERENCES models (name),
    score DOUBLE PRECISION NOT NULL,
    rank INTEGER NOT NULL,
    scored_answers INTEGER NOT NULL,
    blocked_answers INTEGER NOT NULL,
    failed_answers INTEGER NOT NULL,
    merge_method TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS model_scores_run_idx ON model_scores (run_id, created_at DESC);
"""

# The newest row of each (answer, judge, prompt order), whatever its status.
NEWEST_JUDGEMENTS_SQL = """
SELECT DISTINCT ON (j.answer_id, j.judge, j.prompt_order)
       j.answer_id, j.judge, j.prompt_order, j.status
FROM judgements j
JOIN answers a ON a.id = j.answer_id
WHERE a.run_id = %s
ORDER BY j.answer_id, j.judge, j.prompt_order, j.id DESC
"""

# The newest successful row of each (answer, judge, prompt order).
CURRENT_JUDGEMENTS_SQL = """
SELECT DISTINCT ON (j.answer_id, j.judge, j.prompt_order)
       j.answer_id, j.judge, j.prompt_order, j.level, j.reason
FROM judgements j
JOIN answers a ON a.id = j.answer_id
WHERE a.run_id = %s AND j.status = 'ok'
ORDER BY j.answer_id, j.judge, j.prompt_order, j.id DESC
"""

LATEST_RANKINGS_SQL = """
WITH latest_run AS (
    SELECT id FROM runs
    WHERE status = 'published'
    ORDER BY published_at DESC, id DESC
    LIMIT 1
)
SELECT rank, model_name, score
FROM model_scores
WHERE run_id = (SELECT id FROM latest_run)
  AND created_at = (
      SELECT MAX(created_at) FROM model_scores WHERE run_id = (SELECT id FROM latest_run)
  )
ORDER BY rank, model_name
"""


@dataclass(frozen=True)
class AnswerToJudge:
    answer_id: int
    model_name: str
    question_id: str
    answer_text: str


class ScoringStore:
    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("DATABASE_URL must be set")
        self._database_url = database_url

    def _connect(self):
        return psycopg.connect(self._database_url)

    def init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(SCHEMA)

    def create_run(self, question_set_version: str, settings: dict | None = None) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "INSERT INTO runs (question_set_version, settings) VALUES (%s, %s) RETURNING id",
                (question_set_version, Jsonb(settings or {})),
            ).fetchone()
        return row[0]

    def run_status(self, run_id: int) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT status FROM runs WHERE id = %s", (run_id,)).fetchone()
        return row[0] if row else None

    def save_models(self, models) -> None:
        with self._connect() as connection:
            for model in models:
                connection.execute(
                    "INSERT INTO models (name, provider, effort) VALUES (%s, %s, %s) "
                    "ON CONFLICT (name) DO UPDATE SET provider = EXCLUDED.provider, effort = EXCLUDED.effort",
                    (model.name, model.provider, model.effort),
                )

    def finished_answers(self, run_id: int) -> set[tuple[str, str, int]]:
        """Return answer calls that need no retry when a run restarts."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT model_name, question_id, attempt FROM answers "
                "WHERE run_id = %s AND status <> 'failed'",
                (run_id,),
            ).fetchall()
        return {(row[0], row[1], row[2]) for row in rows}

    def save_answer(
        self,
        run_id: int,
        model_name: str,
        question_id: str,
        attempt: int,
        status: str,
        answer_text: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO answers (run_id, model_name, question_id, attempt, answer_text, status, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id, model_name, question_id, attempt) DO UPDATE
                SET answer_text = EXCLUDED.answer_text, status = EXCLUDED.status,
                    error = EXCLUDED.error, created_at = now()
                WHERE answers.status = 'failed'
                """,
                (run_id, model_name, question_id, attempt, answer_text, status, error),
            )

    def answers_to_judge(self, run_id: int) -> list[AnswerToJudge]:
        """Return every answer in the run that has text to judge."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, model_name, question_id, answer_text FROM answers "
                "WHERE run_id = %s AND status = 'ok' ORDER BY id",
                (run_id,),
            ).fetchall()
        return [AnswerToJudge(*row) for row in rows]

    def finished_judgements(self, run_id: int) -> set[tuple[int, str, str]]:
        """Return (answer, judge, prompt order) calls that need no retry when a run restarts."""
        with self._connect() as connection:
            rows = connection.execute(NEWEST_JUDGEMENTS_SQL, (run_id,)).fetchall()
        return {(row[0], row[1], row[2]) for row in rows if row[3] != "failed"}

    def add_judgement(
        self,
        answer_id: int,
        judge: str,
        prompt_order: str,
        rubric_version: str,
        examples_version: str,
        status: str,
        level: str | None = None,
        reason: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO judgements
                    (answer_id, judge, prompt_order, rubric_version, examples_version, level, reason, status, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (answer_id, judge, prompt_order, rubric_version, examples_version, level, reason, status, error),
            )

    def calculate(self, run_id: int, merge_method: str, judges, orders) -> None:
        """Merge each answer's judgements, save new model scores and ranks, and mark the run as scored.

        Only judgements from the given judges and prompt orders count, so a
        judge removed from the config stops affecting the result.
        """
        judges, orders = set(judges), set(orders)
        with self._connect() as connection:
            answers = connection.execute(
                "SELECT id, model_name, question_id, status FROM answers WHERE run_id = %s", (run_id,)
            ).fetchall()
            current: dict[int, list[Judgement]] = {}
            for answer_id, judge, order, level, reason in connection.execute(CURRENT_JUDGEMENTS_SQL, (run_id,)):
                if judge in judges and order in orders:
                    current.setdefault(answer_id, []).append(Judgement(judge, order, level, reason))
            blocked_by_judge = {
                answer_id
                for answer_id, judge, order, status in connection.execute(NEWEST_JUDGEMENTS_SQL, (run_id,))
                if status == "blocked" and judge in judges and order in orders
            }

            outcomes, labels = [], []
            for answer_id, model_name, question_id, status in answers:
                judgements = current.get(answer_id, [])
                level = merge(judgements, merge_method)
                blocked = status == "blocked" or answer_id in blocked_by_judge
                outcomes.append(AnswerOutcome(model_name, question_id, level, blocked))
                if level is not None:
                    labels.append((answer_id, merge_method, level, level_score(level), len(judgements)))
            connection.cursor().executemany(
                "INSERT INTO answer_labels (answer_id, merge_method, level, score, judgement_count) "
                "VALUES (%s, %s, %s, %s, %s)",
                labels,
            )

            # One transaction, so every row of this calculation shares one created_at.
            for row in rank_models(outcomes):
                connection.execute(
                    """
                    INSERT INTO model_scores
                        (run_id, model_name, score, rank, scored_answers, blocked_answers, failed_answers, merge_method)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (run_id, row["model"], row["score"], row["rank"], row["scored"], row["blocked"], row["failed"], merge_method),
                )
            connection.execute(
                "UPDATE runs SET status = 'scored' WHERE id = %s AND status = 'running'",
                (run_id,),
            )

    def run_rankings(self, run_id: int) -> list[dict]:
        """Return a run's newest ranking, with its answer counts, for review."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT rank, model_name, score, scored_answers, blocked_answers, failed_answers
                FROM model_scores
                WHERE run_id = %(run_id)s
                  AND created_at = (SELECT MAX(created_at) FROM model_scores WHERE run_id = %(run_id)s)
                ORDER BY rank, model_name
                """,
                {"run_id": run_id},
            ).fetchall()
        return [
            {"rank": r[0], "model": r[1], "score": r[2], "scored": r[3], "blocked": r[4], "failed": r[5]}
            for r in rows
        ]

    def publish(self, run_id: int) -> None:
        """Make a scored run the one the website shows."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET status = 'published', published_at = now() "
                "WHERE id = %s AND status IN ('scored', 'published')",
                (run_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Run {run_id} does not exist or has not been scored")

    def latest_rankings(self) -> list[dict]:
        """Return the newest published ranking, or an empty list if there is none."""
        with self._connect() as connection:
            rows = connection.execute(LATEST_RANKINGS_SQL).fetchall()
        return [{"rank": row[0], "model": row[1], "score": round(row[2], 4)} for row in rows]
