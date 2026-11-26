"""Build the ranking: one row per model for the rankings table, and the JSON GET /api/rank_db returns.

No answers or judgements are included.

The shape follows Zeli's Website API Contract (RankingResponse). Category
scores group question scores by the category saved with each answer.
"""

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from statistics import mean

from src.scoring.schema import LEVELS


@dataclass(frozen=True)
class PublishedRun:
    """What the ranking needs from one scored run."""

    run_id: int
    settings: dict
    published_at: datetime
    rubric_versions: list[str]
    scores: list[dict]
    question_scores: list[dict]


def slug(text: str, sep: str) -> str:
    """'Racial Equity' -> 'racial_equity'; 'gpt-5.5' -> 'gpt-5-5'."""
    return re.sub(r"[^a-z0-9]+", sep, text.lower()).strip(sep)


def score_level(score: float) -> int:
    """The level whose score is nearest: 0 -> 1, 0.25 -> 2, ... 1 -> 5."""
    return int(score * (len(LEVELS) - 1) + 0.5) + 1


# The columns of the rankings table, one row per model.
RANKING_COLUMNS = (
    "run_id", "published_at", "rank", "model_id", "model_name", "provider", "effort", "overall_score",
    "level", "label", "category_scores", "llm_mode", "rubric_version", "question_count", "runs_per_question",
)


def ranking_rows(run: PublishedRun) -> list[dict]:
    """One row per model, with everything GET /api/rank_db returns."""
    by_category = defaultdict(lambda: defaultdict(list))
    for row in run.question_scores:
        if row["category"]:
            by_category[row["model"]][slug(row["category"], "_")].append(row["score"])

    rows = []
    for row in run.scores:
        overall = round(row["score"], 4)
        level = score_level(overall)
        rows.append(
            {
                "run_id": run.run_id,
                "published_at": run.published_at,
                "rank": row["rank"],
                "model_id": slug(row["model"], "-"),
                "model_name": row["model"],
                "provider": row["provider"],
                "effort": None if row["effort"] == "default" else row["effort"],
                "overall_score": overall,
                "level": level,
                "label": LEVELS[level - 1],
                "category_scores": {key: round(mean(s), 4) for key, s in by_category[row["model"]].items()},
                # "mock" means the results are not from real models.
                "llm_mode": run.settings.get("llm_mode", "azure"),
                "rubric_version": ", ".join(run.rubric_versions),
                "question_count": len({q["question_id"] for q in run.question_scores}),
                "runs_per_question": run.settings.get("n_answers"),
            }
        )
    return rows


def ranking_response(rows: list[dict]) -> dict | None:
    """Turn one written ranking back into the API's JSON (Zeli's RankingResponse), or None if empty."""
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: (r["rank"], r["model_name"]))
    first = rows[0]
    return {
        "run": {
            "runId": str(first["run_id"]),
            "runDate": first["published_at"].isoformat(),
            "llmMode": first["llm_mode"],
            "rubricVersion": first["rubric_version"],
            "modelCount": len(rows),
            "questionCount": first["question_count"],
            "runsPerQuestion": first["runs_per_question"],
        },
        "models": [
            {
                "rank": r["rank"],
                "modelId": r["model_id"],
                "modelName": r["model_name"],
                "provider": r["provider"],
                "effort": r["effort"],
                "overallScore": r["overall_score"],
                "level": r["level"],
                "label": r["label"],
                "categoryScores": r["category_scores"],
            }
            for r in rows
        ],
    }
