"""PostgreSQL storage for scoring runs.

Scores are never overwritten. An update adds a new row, and the newest
successful row is the answer's current score.
"""

from dataclasses import dataclass

import psycopg

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id BIGSERIAL PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'scored', 'published')),
    question_set_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS models (
    name TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS answers (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES runs (id),
    model_name TEXT NOT NULL REFERENCES models (name),
    question_id TEXT NOT NULL,
    attempt SMALLINT NOT NULL CHECK (attempt BETWEEN 1 AND 3),
    answer_text TEXT,
    status TEXT NOT NULL CHECK (status IN ('ok', 'blocked', 'failed')),
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, model_name, question_id, attempt)
);

CREATE TABLE IF NOT EXISTS answer_scores (
    id BIGSERIAL PRIMARY KEY,
    answer_id BIGINT NOT NULL REFERENCES answers (id),
    evaluator TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    score DOUBLE PRECISION CHECK (score BETWEEN 0 AND 1),
    level TEXT,
    reasoning TEXT,
    status TEXT NOT NULL CHECK (status IN ('ok', 'blocked', 'failed')),
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((status = 'ok') = (score IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS answer_scores_answer_idx ON answer_scores (answer_id, id DESC);

CREATE TABLE IF NOT EXISTS model_scores (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES runs (id),
    model_name TEXT NOT NULL REFERENCES models (name),
    score DOUBLE PRECISION NOT NULL,
    rank INTEGER NOT NULL,
    scored_answers INTEGER NOT NULL,
    blocked_answers INTEGER NOT NULL,
    failed_answers INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS model_scores_run_idx ON model_scores (run_id, created_at DESC);
"""

# Rank 1 is the most biased model. A question's score averages its current
# answer scores, and a model's score averages its question scores.
CALCULATE_SQL = """
WITH newest AS (
    SELECT DISTINCT ON (answer_id) answer_id, status
    FROM answer_scores
    ORDER BY answer_id, id DESC
),
current_scores AS (
    SELECT DISTINCT ON (answer_id) answer_id, score
    FROM answer_scores
    WHERE status = 'ok'
    ORDER BY answer_id, id DESC
),
run_answers AS (
    SELECT a.model_name, a.question_id, a.status AS answer_status,
           n.status AS score_status, c.score
    FROM answers a
    LEFT JOIN newest n ON n.answer_id = a.id
    LEFT JOIN current_scores c ON c.answer_id = a.id
    WHERE a.run_id = %(run_id)s
),
question_scores AS (
    SELECT model_name, question_id, AVG(score) AS score
    FROM run_answers
    WHERE score IS NOT NULL
    GROUP BY model_name, question_id
),
averages AS (
    SELECT model_name, AVG(score) AS score
    FROM question_scores
    GROUP BY model_name
),
counts AS (
    SELECT model_name,
           COUNT(*) FILTER (WHERE score IS NOT NULL) AS scored,
           COUNT(*) FILTER (
               WHERE score IS NULL AND (answer_status = 'blocked' OR score_status = 'blocked')
           ) AS blocked,
           COUNT(*) FILTER (
               WHERE score IS NULL AND answer_status <> 'blocked' AND score_status IS DISTINCT FROM 'blocked'
           ) AS failed
    FROM run_answers
    GROUP BY model_name
)
INSERT INTO model_scores
    (run_id, model_name, score, rank, scored_answers, blocked_answers, failed_answers)
SELECT %(run_id)s, a.model_name, a.score, RANK() OVER (ORDER BY a.score DESC),
       c.scored, c.blocked, c.failed
FROM averages a
JOIN counts c USING (model_name)
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
class AnswerToGrade:
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

    def create_run(self, question_set_version: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "INSERT INTO runs (question_set_version) VALUES (%s) RETURNING id",
                (question_set_version,),
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
                    "INSERT INTO models (name, provider) VALUES (%s, %s) "
                    "ON CONFLICT (name) DO UPDATE SET provider = EXCLUDED.provider",
                    (model.name, model.provider),
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

    def answers_to_grade(self, run_id: int, include_scored: bool = False) -> list[AnswerToGrade]:
        """Return answers needing a score: unscored or last failed, or all of them for a re-score."""
        condition = "" if include_scored else "AND (newest.status IS NULL OR newest.status = 'failed')"
        query = f"""
            SELECT a.id, a.model_name, a.question_id, a.answer_text
            FROM answers a
            LEFT JOIN LATERAL (
                SELECT status FROM answer_scores s
                WHERE s.answer_id = a.id
                ORDER BY s.id DESC
                LIMIT 1
            ) newest ON TRUE
            WHERE a.run_id = %s AND a.status = 'ok' {condition}
            ORDER BY a.id
        """
        with self._connect() as connection:
            rows = connection.execute(query, (run_id,)).fetchall()
        return [AnswerToGrade(*row) for row in rows]

    def add_score(
        self,
        answer_id: int,
        evaluator: str,
        rubric_version: str,
        status: str,
        score: float | None = None,
        level: str | None = None,
        reasoning: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO answer_scores
                    (answer_id, evaluator, rubric_version, score, level, reasoning, status, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (answer_id, evaluator, rubric_version, score, level, reasoning, status, error),
            )

    def calculate(self, run_id: int) -> None:
        """Save a new set of model scores and ranks, and mark the run as scored."""
        with self._connect() as connection:
            connection.execute(CALCULATE_SQL, {"run_id": run_id})
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
