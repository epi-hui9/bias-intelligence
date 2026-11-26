"""Build the JSON that GET /api/rankings returns: one result per model, no answers or judgements.

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
    """What the website's ranking needs from one scored run."""

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


def build_rankings(run: PublishedRun) -> dict:
    by_category = defaultdict(lambda: defaultdict(list))
    for row in run.question_scores:
        if row["category"]:
            by_category[row["model"]][slug(row["category"], "_")].append(row["score"])

    models = []
    for row in run.scores:
        overall = round(row["score"], 4)
        level = score_level(overall)
        models.append(
            {
                "rank": row["rank"],
                "modelId": slug(row["model"], "-"),
                "modelName": row["model"],
                "provider": row["provider"],
                "effort": None if row["effort"] == "default" else row["effort"],
                "overallScore": overall,
                "level": level,
                "label": LEVELS[level - 1],
                "categoryScores": {key: round(mean(scores), 4) for key, scores in by_category[row["model"]].items()},
            }
        )

    return {
        "run": {
            "runId": str(run.run_id),
            "runDate": run.published_at.isoformat() if run.published_at else None,
            # "mock" means the results are not from real models.
            "llmMode": run.settings.get("llm_mode", "azure"),
            "rubricVersion": ", ".join(run.rubric_versions),
            "modelCount": len(models),
            "questionCount": len({row["question_id"] for row in run.question_scores}),
            "runsPerQuestion": run.settings.get("n_answers"),
        },
        "models": models,
    }
