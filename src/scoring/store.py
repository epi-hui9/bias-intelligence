"""Storage for scoring runs: one append-only log of rows.

The log lives in one of two places. Both hold the same rows, and the same
logic in `LogStore` reads and writes them:

    SqlLog  the `log` table in PostgreSQL. Runs on Azure use it.
    CsvLog  one CSV file per run on this computer, exports/run_<id>.csv.
            `--debug` runs use it, so a run on a laptop never writes to the database.

The last step of a run writes the ranking, one row per model, to the
`rankings` table (or exports/run_<id>_rankings.csv). GET /api/rank_db reads it.

Every step of a run adds rows, and nothing is changed or deleted. Each row has a kind:

    run        the run's settings and the versions of the materials it uses
    answer     one model's answer to one question, one attempt
    judgement  one judge's level and reason for one answer, in one prompt order
    label      one answer's merged level, from one calculation
    score      one model's score and rank, from one calculation

When a call is retried, or a run is re-judged or re-merged, the newest row counts.
"""

import csv
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from src.scoring.merge import AnswerOutcome, level_score, merge, rank_models
from src.scoring.report import RANKING_COLUMNS, PublishedRun, ranking_response, ranking_rows
from src.scoring.schema import Judgement

KINDS = ("run", "answer", "judgement", "label", "score")

COLUMNS = (
    "id", "run_id", "kind", "model", "question_id", "category", "attempt", "judge",
    "prompt_order", "level", "score", "status", "text", "error", "created_at",
)
# Keys of `details` that get their own CSV column. Anything else, such as a
# run's settings or a published ranking, stays as JSON text in `details`.
DETAIL_COLUMNS = (
    "rank", "scored", "blocked", "failed", "provider", "effort", "merge_method",
    "judgement_count", "rubric_version", "examples_version",
)
CSV_COLUMNS = COLUMNS + DETAIL_COLUMNS + ("details",)
INT_COLUMNS = {"id", "run_id", "attempt", "rank", "scored", "blocked", "failed", "judgement_count"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS log (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('run', 'answer', 'judgement', 'label', 'score')),
    model TEXT,
    question_id TEXT,
    category TEXT,
    attempt SMALLINT CHECK (attempt >= 1),
    judge TEXT,
    prompt_order TEXT,
    level TEXT,
    score DOUBLE PRECISION CHECK (score BETWEEN 0 AND 1),
    status TEXT CHECK (status IN ('ok', 'blocked', 'failed')),
    text TEXT,
    error TEXT,
    details JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Each kind must fill the columns it is about.
    CHECK (kind NOT IN ('answer', 'judgement', 'label')
           OR (model IS NOT NULL AND question_id IS NOT NULL AND attempt IS NOT NULL)),
    CHECK (kind NOT IN ('answer', 'judgement') OR status IS NOT NULL),
    CHECK (kind <> 'judgement' OR (judge IS NOT NULL AND prompt_order IS NOT NULL AND (status = 'ok') = (level IS NOT NULL))),
    CHECK (kind <> 'label' OR (level IS NOT NULL AND score IS NOT NULL)),
    CHECK (kind <> 'score' OR (model IS NOT NULL AND score IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS log_run_idx ON log (run_id, kind, id);
-- One run row per run, and an answer succeeds at most once per attempt.
CREATE UNIQUE INDEX IF NOT EXISTS log_one_run_row ON log (run_id) WHERE kind = 'run';
CREATE UNIQUE INDEX IF NOT EXISTS log_one_answer ON log (run_id, model, question_id, attempt)
    WHERE kind = 'answer' AND status <> 'failed';

-- The ranking GET /api/rank_db returns: one row per model. Every write adds a set
-- of rows sharing one published_at, and the newest set is the current ranking.
CREATE TABLE IF NOT EXISTS rankings (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    rank INTEGER NOT NULL CHECK (rank >= 1),
    model_id TEXT NOT NULL,
    model_name TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    effort TEXT,
    overall_score DOUBLE PRECISION NOT NULL CHECK (overall_score BETWEEN 0 AND 1),
    level SMALLINT NOT NULL CHECK (level BETWEEN 1 AND 5),
    label TEXT NOT NULL,
    category_scores JSONB NOT NULL DEFAULT '{}',
    llm_mode TEXT NOT NULL,
    rubric_version TEXT NOT NULL DEFAULT '',
    question_count INTEGER NOT NULL,
    runs_per_question INTEGER,
    UNIQUE (published_at, model_name)
);
CREATE INDEX IF NOT EXISTS rankings_newest_idx ON rankings (published_at DESC);
"""
RANKING_INT_COLUMNS = {"run_id", "rank", "level", "question_count", "runs_per_question"}


def _row(**fields) -> dict:
    """A log row with every column, unset ones as None."""
    row = {column: None for column in COLUMNS}
    row["details"] = {}
    row.update(fields)
    return row


def _answer_key(row: dict) -> tuple[str, str, int]:
    return (row["model"], row["question_id"], row["attempt"])


def _newest(rows: list[dict], key) -> dict:
    """The newest row for each key. Rows come oldest first."""
    newest = {}
    for row in rows:
        newest[key(row)] = row
    return newest


def _newest_calculation(rows: list[dict]) -> list[dict]:
    """Rows from the newest calculation: they share one created_at."""
    if not rows:
        return []
    latest = max(row["created_at"] for row in rows)
    return [row for row in rows if row["created_at"] == latest]


def to_csv(row: dict) -> list:
    """One log row as CSV values, in CSV_COLUMNS order."""
    details = dict(row["details"] or {})
    values = [row[column] for column in COLUMNS]
    values[COLUMNS.index("created_at")] = row["created_at"].isoformat() if row["created_at"] else None
    values += [details.pop(key, None) for key in DETAIL_COLUMNS]
    values.append(json.dumps(details, ensure_ascii=False) if details else "")
    return values


def from_csv(record: dict[str, str]) -> dict:
    """The inverse of to_csv. Empty cells become None."""
    def value(column: str):
        text = record.get(column, "")
        if text == "":
            return None
        if column in INT_COLUMNS:
            return int(text)
        if column == "score":
            return float(text)
        if column == "created_at":
            return datetime.fromisoformat(text)
        return text

    row = _row(**{column: value(column) for column in COLUMNS})
    row["details"] = json.loads(record["details"]) if record.get("details") else {}
    row["details"].update({key: value(key) for key in DETAIL_COLUMNS if value(key) is not None})
    return row


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig, so Excel shows the text correctly.
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        writer.writerows(to_csv(row) for row in rows)


class SqlLog:
    """The `log` table in PostgreSQL."""

    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("DATABASE_URL must be set")
        self._database_url = database_url

    def _connect(self):
        return psycopg.connect(self._database_url)

    def init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(SCHEMA)

    def new_run(self, details: dict) -> int:
        """Add a run row and return its ID: one more than the highest so far."""
        with self._connect() as connection:
            # Stops two runs that start at once from taking the same ID.
            connection.execute("LOCK TABLE log IN SHARE ROW EXCLUSIVE MODE")
            return connection.execute(
                "INSERT INTO log (run_id, kind, details) "
                "SELECT COALESCE(MAX(run_id), 0) + 1, 'run', %s FROM log RETURNING run_id",
                (Jsonb(details),),
            ).fetchone()[0]

    def append(self, rows: list[dict]) -> None:
        """Add rows in one transaction. A second success for the same answer is skipped."""
        columns = [c for c in COLUMNS if c not in ("id", "created_at")]
        with self._connect() as connection:
            connection.cursor().executemany(
                f"INSERT INTO log ({', '.join(columns)}, details, created_at) "
                f"VALUES ({', '.join(['%s'] * len(columns))}, %s, COALESCE(%s, now())) ON CONFLICT DO NOTHING",
                [[row[c] for c in columns] + [Jsonb(row["details"]), row["created_at"]] for row in rows],
            )

    def read(self, run_id: int | None = None, kinds=None, text: bool = True) -> list[dict]:
        """Rows oldest first, for one run or all, of the given kinds or all."""
        selected = [c if c != "text" or text else "NULL" for c in COLUMNS]
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {', '.join(selected)}, details FROM log "
                "WHERE (%(run_id)s::bigint IS NULL OR run_id = %(run_id)s) "
                "AND (%(kinds)s::text[] IS NULL OR kind = ANY(%(kinds)s)) ORDER BY id",
                {"run_id": run_id, "kinds": list(kinds) if kinds else None},
            ).fetchall()
        return [_row(**dict(zip(COLUMNS, values)), details=details) for *values, details in rows]

    def save_rankings(self, rows: list[dict]) -> None:
        with self._connect() as connection:
            connection.cursor().executemany(
                f"INSERT INTO rankings ({', '.join(RANKING_COLUMNS)}) VALUES ({', '.join(['%s'] * len(RANKING_COLUMNS))})",
                [[Jsonb(r[c]) if c == "category_scores" else r[c] for c in RANKING_COLUMNS] for r in rows],
            )

    def latest_rankings(self) -> list[dict]:
        """The newest written ranking, one row per model."""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {', '.join(RANKING_COLUMNS)} FROM rankings "
                "WHERE published_at = (SELECT MAX(published_at) FROM rankings) ORDER BY rank, model_name"
            ).fetchall()
        return [dict(zip(RANKING_COLUMNS, row)) for row in rows]

    def has_rankings(self, run_id: int) -> bool:
        with self._connect() as connection:
            return connection.execute("SELECT EXISTS (SELECT 1 FROM rankings WHERE run_id = %s)", (run_id,)).fetchone()[0]


class CsvLog:
    """One CSV file per run in a folder on this computer, with the same rows as SqlLog."""

    FILE = re.compile(r"run_(\d+)\.csv")

    def __init__(self, directory: Path = Path("exports")):
        self.directory = Path(directory)
        self._lock = threading.Lock()
        self._rows: dict[int, list[dict]] = {}

    def path(self, run_id: int) -> Path:
        return self.directory / f"run_{run_id}.csv"

    def rankings_path(self, run_id: int) -> Path:
        return self.directory / f"run_{run_id}_rankings.csv"

    def init_schema(self) -> None:
        """Nothing to create: a run's file is created when the run starts."""

    def _run_ids(self) -> list[int]:
        if not self.directory.exists():
            return []
        return sorted(int(m.group(1)) for p in self.directory.iterdir() if (m := self.FILE.fullmatch(p.name)))

    def _load(self, run_id: int) -> list[dict]:
        if run_id not in self._rows:
            path = self.path(run_id)
            if path.exists():
                with path.open(newline="", encoding="utf-8-sig") as handle:
                    self._rows[run_id] = [from_csv(record) for record in csv.DictReader(handle)]
            else:
                self._rows[run_id] = []
        return self._rows[run_id]

    def new_run(self, details: dict) -> int:
        with self._lock:
            run_id = max(self._run_ids(), default=0) + 1
            self.directory.mkdir(parents=True, exist_ok=True)
            write_csv([], self.path(run_id))
            self._rows[run_id] = []
            self._append(run_id, [_row(run_id=run_id, kind="run", details=details)])
        return run_id

    def append(self, rows: list[dict]) -> None:
        with self._lock:
            for run_id in dict.fromkeys(row["run_id"] for row in rows):
                self._append(run_id, [row for row in rows if row["run_id"] == run_id])

    def _append(self, run_id: int, rows: list[dict]) -> None:
        saved = self._load(run_id)
        if not self.path(run_id).exists():
            raise ValueError(f"Run {run_id} has no local log at {self.path(run_id)}")
        succeeded = {_answer_key(r) for r in saved if r["kind"] == "answer" and r["status"] != "failed"}
        now = datetime.now(timezone.utc)
        added = []
        for row in rows:
            # The same rule as the database: an answer succeeds at most once per attempt.
            if row["kind"] == "answer" and row["status"] != "failed":
                if _answer_key(row) in succeeded:
                    continue
                succeeded.add(_answer_key(row))
            added.append({**row, "id": len(saved) + len(added) + 1, "created_at": row["created_at"] or now})
        with self.path(run_id).open("a", newline="", encoding="utf-8-sig") as handle:
            csv.writer(handle).writerows(to_csv(row) for row in added)
        saved.extend(added)

    def read(self, run_id: int | None = None, kinds=None, text: bool = True) -> list[dict]:
        with self._lock:
            run_ids = [run_id] if run_id is not None else self._run_ids()
            rows = [row for rid in run_ids for row in self._load(rid)]
        return [row for row in rows if not kinds or row["kind"] in kinds]

    def save_rankings(self, rows: list[dict]) -> None:
        with self._lock:
            for run_id in dict.fromkeys(row["run_id"] for row in rows):
                path = self.rankings_path(run_id)
                new = not path.exists()
                with path.open("a", newline="", encoding="utf-8-sig") as handle:
                    writer = csv.writer(handle)
                    if new:
                        writer.writerow(RANKING_COLUMNS)
                    for row in rows:
                        if row["run_id"] == run_id:
                            writer.writerow([_ranking_cell(c, row[c]) for c in RANKING_COLUMNS])

    def _rankings(self) -> list[dict]:
        rows = []
        for path in self.directory.glob("run_*_rankings.csv") if self.directory.exists() else []:
            with path.open(newline="", encoding="utf-8-sig") as handle:
                rows += [_ranking_from_csv(record) for record in csv.DictReader(handle)]
        return rows

    def latest_rankings(self) -> list[dict]:
        rows = self._rankings()
        if not rows:
            return []
        latest = max(row["published_at"] for row in rows)
        return sorted((r for r in rows if r["published_at"] == latest), key=lambda r: (r["rank"], r["model_name"]))

    def has_rankings(self, run_id: int) -> bool:
        return self.rankings_path(run_id).exists()


def _ranking_cell(column: str, value):
    if column == "category_scores":
        return json.dumps(value, ensure_ascii=False)
    if column == "published_at":
        return value.isoformat()
    return value


def _ranking_from_csv(record: dict[str, str]) -> dict:
    row = {}
    for column in RANKING_COLUMNS:
        text = record.get(column, "")
        if column == "category_scores":
            row[column] = json.loads(text) if text else {}
        elif text == "":
            row[column] = None
        elif column in RANKING_INT_COLUMNS:
            row[column] = int(text)
        elif column == "overall_score":
            row[column] = float(text)
        elif column == "published_at":
            row[column] = datetime.fromisoformat(text)
        else:
            row[column] = text
    return row


@dataclass(frozen=True)
class AnswerToJudge:
    model_name: str
    question_id: str
    attempt: int
    category: str
    answer_text: str

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.model_name, self.question_id, self.attempt)


class LogStore:
    """What the engine asks of the log. `log` is a SqlLog or a CsvLog."""

    def __init__(self, log: SqlLog | CsvLog):
        self.log = log

    def init_schema(self) -> None:
        self.log.init_schema()

    def create_run(self, question_set_version: str, settings: dict | None = None) -> int:
        return self.log.new_run({**(settings or {}), "question_set_version": question_set_version})

    def run_status(self, run_id: int) -> str | None:
        """'running', 'scored' or 'published', or None if the run does not exist."""
        kinds = {row["kind"] for row in self.log.read(run_id, ("run", "score"))}
        if "run" not in kinds:
            return None
        return "published" if self.log.has_rankings(run_id) else "scored" if "score" in kinds else "running"

    def _answers(self, run_id: int, text: bool = False) -> dict:
        return _newest(self.log.read(run_id, ("answer",), text), _answer_key)

    def _judgements(self, run_id: int) -> list[dict]:
        return self.log.read(run_id, ("judgement",), text=False)

    def failed_calls(self, run_id: int) -> dict[str, int]:
        """How many answer and judgement calls failed (timeouts, errors) and were not yet redone."""
        newest = _newest(self._judgements(run_id), lambda r: (_answer_key(r), r["judge"], r["prompt_order"]))
        return {
            "answers": sum(row["status"] == "failed" for row in self._answers(run_id).values()),
            "judgements": sum(row["status"] == "failed" for row in newest.values()),
        }

    def finished_answers(self, run_id: int) -> set[tuple[str, str, int]]:
        """Return answer calls that need no retry when a run restarts."""
        return {key for key, row in self._answers(run_id).items() if row["status"] != "failed"}

    def save_answer(
        self,
        run_id: int,
        model_name: str,
        question_id: str,
        attempt: int,
        status: str,
        answer_text: str | None = None,
        error: str | None = None,
        category: str | None = None,
        call_settings: dict | None = None,
    ) -> None:
        self.log.append([_row(
            run_id=run_id, kind="answer", model=model_name, question_id=question_id, category=category,
            attempt=attempt, status=status, text=answer_text, error=error,
            details={"call_settings": call_settings} if call_settings else {},
        )])

    def answers_to_judge(self, run_id: int) -> list[AnswerToJudge]:
        """Return every answer in the run that has text to judge."""
        return [
            AnswerToJudge(row["model"], row["question_id"], row["attempt"], row["category"], row["text"])
            for row in sorted(self._answers(run_id, text=True).values(), key=lambda r: r["id"])
            if row["status"] == "ok"
        ]

    def finished_judgements(self, run_id: int) -> set[tuple[tuple[str, str, int], str, str]]:
        """Return (answer key, judge, prompt order) calls that need no retry when a run restarts."""
        newest = _newest(self._judgements(run_id), lambda r: (_answer_key(r), r["judge"], r["prompt_order"]))
        return {key for key, row in newest.items() if row["status"] != "failed"}

    def add_judgement(
        self,
        run_id: int,
        answer: AnswerToJudge,
        judge: str,
        prompt_order: str,
        rubric_version: str,
        examples_version: str,
        status: str,
        level: str | None = None,
        reason: str | None = None,
        error: str | None = None,
        call_settings: dict | None = None,
    ) -> None:
        details = {"rubric_version": rubric_version, "examples_version": examples_version}
        if call_settings:
            details["call_settings"] = call_settings
        self.log.append([_row(
            run_id=run_id, kind="judgement", model=answer.model_name, question_id=answer.question_id,
            category=answer.category, attempt=answer.attempt, judge=judge, prompt_order=prompt_order,
            level=level, status=status, text=reason, error=error, details=details,
        )])

    def calculate(self, run_id: int, merge_method: str, judges, orders) -> None:
        """Merge each answer's judgements, and add label and score rows for this calculation.

        Only judgements from the given judges and prompt orders count, so a
        judge removed from the config stops affecting the result.
        """
        judges, orders = set(judges), set(orders)
        counted = [r for r in self._judgements(run_id) if r["judge"] in judges and r["prompt_order"] in orders]
        call = lambda r: (_answer_key(r), r["judge"], r["prompt_order"])
        current: dict[tuple, list[Judgement]] = {}
        for row in _newest([r for r in counted if r["status"] == "ok"], call).values():
            current.setdefault(_answer_key(row), []).append(Judgement(row["judge"], row["prompt_order"], row["level"]))
        blocked_by_judge = {answer for (answer, _, _), r in _newest(counted, call).items() if r["status"] == "blocked"}

        # One created_at marks every row of this calculation.
        now = datetime.now(timezone.utc)
        outcomes, rows = [], []
        for key, answer in self._answers(run_id).items():
            judgements = current.get(key, [])
            level = merge(judgements, merge_method)
            blocked = answer["status"] == "blocked" or key in blocked_by_judge
            outcomes.append(AnswerOutcome(answer["model"], answer["question_id"], level, blocked))
            if level is not None:
                rows.append(_row(
                    run_id=run_id, kind="label", model=answer["model"], question_id=answer["question_id"],
                    category=answer["category"], attempt=answer["attempt"], level=level, score=level_score(level),
                    created_at=now, details={"merge_method": merge_method, "judgement_count": len(judgements)},
                ))
        listed = {m["name"]: m for m in self._settings(run_id).get("models", [])}
        for result in rank_models(outcomes):
            model = listed.get(result["model"], {})
            rows.append(_row(
                run_id=run_id, kind="score", model=result["model"], score=result["score"], created_at=now,
                details={
                    "rank": result["rank"], "scored": result["scored"], "blocked": result["blocked"],
                    "failed": result["failed"], "merge_method": merge_method,
                    "provider": model.get("provider", ""), "effort": model.get("effort", "default"),
                },
            ))
        self.log.append(rows)

    def _settings(self, run_id: int) -> dict:
        rows = self.log.read(run_id, ("run",))
        return rows[0]["details"] if rows else {}

    def _newest_scores(self, run_id: int) -> list[dict]:
        rows = _newest_calculation(self.log.read(run_id, ("score",)))
        return sorted(rows, key=lambda r: (r["details"]["rank"], r["model"]))

    def run_rankings(self, run_id: int) -> list[dict]:
        """Return a run's newest ranking, with its answer counts, for review."""
        return [
            {"rank": r["details"]["rank"], "model": r["model"], "score": r["score"], "scored": r["details"]["scored"],
             "blocked": r["details"]["blocked"], "failed": r["details"]["failed"]}
            for r in self._newest_scores(run_id)
        ]

    def publish(self, run_id: int) -> list[dict]:
        """Write the run's newest ranking to the rankings table (or file), and return its rows."""
        scores = self._newest_scores(run_id)
        if not scores:
            raise ValueError(f"Run {run_id} does not exist or has not been scored")
        by_question: dict[tuple, list[float]] = {}
        for row in _newest_calculation(self.log.read(run_id, ("label",))):
            by_question.setdefault((row["model"], row["question_id"], row["category"]), []).append(row["score"])
        rubric_versions = {
            r["details"].get("rubric_version") for r in self._judgements(run_id) if r["status"] == "ok"
        }
        published = PublishedRun(
            run_id=run_id,
            settings=self._settings(run_id),
            published_at=datetime.now(timezone.utc),
            rubric_versions=sorted(v for v in rubric_versions if v),
            scores=[
                {"rank": r["details"]["rank"], "model": r["model"], "provider": r["details"]["provider"],
                 "effort": r["details"]["effort"], "score": r["score"]}
                for r in scores
            ],
            question_scores=[
                {"model": m, "question_id": q, "category": c, "score": sum(s) / len(s)}
                for (m, q, c), s in by_question.items()
            ],
        )
        rows = ranking_rows(published)
        self.log.save_rankings(rows)
        return rows

    def published_ranking(self) -> dict | None:
        """Return the newest written ranking as the API's JSON, or None if there is none."""
        return ranking_response(self.log.latest_rankings())

    def export(self, run_id: int, path: Path, kind: str | None = None) -> int:
        """Write a run's rows, oldest first, to a CSV file and return how many were written."""
        if kind is not None and kind not in KINDS:
            raise ValueError(f"Unknown kind {kind!r}. Choose from: {', '.join(KINDS)}")
        rows = self.log.read(run_id, (kind,) if kind else None)
        write_csv(rows, path)
        return len(rows)
