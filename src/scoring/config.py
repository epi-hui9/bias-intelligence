"""Every setting of the scoring engine, in one place.

Change a value here and the next run uses it. Each run saves a copy of these
settings, so old results always show what produced them.
"""

from dataclasses import dataclass, field

from src.scoring.schema import Model

# --- Models to rank ---------------------------------------------------------
# Names must match the Azure deployment names. To rank the same model at
# another effort setting, add a second entry with a different `effort`.
# For now `effort` is only recorded with the run: every call uses the
# provider's default setting.
CANDIDATE_MODELS = [
    Model("gpt-6.1-sol", "OpenAI"),
    Model("gpt-5.5", "OpenAI"),
    Model("gpt-5.4-mini", "OpenAI"),
    Model("gpt-oss-120b", "OpenAI"),
    Model("claude-fable-5-1", "Anthropic"),
    Model("claude-opus-5-5", "Anthropic"),
    Model("claude-sonnet-5-5", "Anthropic"),
    Model("claude-haiku-5-5", "Anthropic"),
    Model("Llama-4-Maverick-17B-128E-Instruct-FP8", "Meta"),
    Model("Llama-3.3-70B-Instruct", "Meta"),
    Model("muse-spark-1.3", "Meta"),
    Model("DeepSeek-V4-Pro", "DeepSeek"),
    Model("DeepSeek-V4-Flash", "DeepSeek"),
    Model("grok-4.7", "xAI"),
    Model("grok-4-1-fast-non-reasoning", "xAI"),
    Model("Mistral-Large-3", "Mistral AI"),
    Model("MAI-Thinking-1", "Microsoft"),
    Model("Kimi-K3.1-Pro", "MoonshotAI"),
    Model("qwen3.8-27b", "Alibaba"),
    Model("cohere-command-a", "Cohere"),
]

# How many times each model answers each question.
N_ANSWERS = 3

# --- Judges -----------------------------------------------------------------
# zfj todo: these 3 judges are placeholders. Pick the real ones.
JUDGE_MODELS = ["gpt-5.5", "claude-opus-5-5", "DeepSeek-V4-Pro"]

# Each judge judges each answer once per entry here, with the rubric levels
# laid out differently each time. Available layouts are in judge.py:
#   ascending   No Bias first, Very High Bias last
#   descending  Very High Bias first, No Bias last
#   lettered    levels shown as options A-E in LETTERED_ORDER, without level names
JUDGE_ORDERS = ["ascending", "descending", "lettered"]
LETTERED_ORDER = ["Medium Bias", "No Bias", "Very High Bias", "Low Bias", "High Bias"]

# When a judge is also a ranked model, it does not judge its own answers.
EXCLUDE_SELF_JUDGING = True

# Few-shot examples shown per rubric level, taken from the question's category.
FEW_SHOTS_PER_LEVEL = 2
EXAMPLE_STATUSES = ["Confirmed"]

# --- Merging ----------------------------------------------------------------
# How one answer's judgements become one label. Methods are in merge.py.
MERGE_METHOD = "median_of_medians"

# --- Data -------------------------------------------------------------------
# Where the materials come from. Loaders are in loaders.py.
DATA_SOURCE = "disk"
QUESTIONS_FILE = "question_set_v1.csv"
RUBRIC_FILE = "rubric_v2.csv"
EXAMPLES_FILE = "rubric_examples_v1.csv"

# Category names as written in the question set or examples file -> the
# rubric's spelling. Add a line here when a file spells a category differently.
CATEGORY_ALIASES = {
    "Racial equity": "Racial Equity",
    "Mental health": "Mental Health",
    "Science, technology, medicine": "Science, Technology, & Medicine",
    "Science Technology Medicine": "Science, Technology, & Medicine",
}

# zfj todo: these categories are skipped (not asked, not judged) until the
# rubric team finishes them. Education has no rubric row, and Disability has
# no Medium Bias or High Bias text. Remove a name here once its rubric is complete.
SKIP_CATEGORIES = ["Education", "Disability"]

# --- Calls ------------------------------------------------------------------
WORKERS = 8
MAX_RETRIES = 3
RETRY_BASE_SECONDS = 2.0
ANSWER_MAX_TOKENS = 2000
ANSWER_TEMPERATURE = 0.7
JUDGE_MAX_TOKENS = 2000
JUDGE_TEMPERATURE = 0.0


@dataclass(frozen=True)
class ScoringConfig:
    """The settings one run uses. The defaults are the values above."""

    n_answers: int = N_ANSWERS
    judge_models: tuple[str, ...] = tuple(JUDGE_MODELS)
    judge_orders: tuple[str, ...] = tuple(JUDGE_ORDERS)
    lettered_order: tuple[str, ...] = tuple(LETTERED_ORDER)
    exclude_self_judging: bool = EXCLUDE_SELF_JUDGING
    few_shots_per_level: int = FEW_SHOTS_PER_LEVEL
    merge_method: str = MERGE_METHOD
    workers: int = WORKERS
    retry_base_seconds: float = RETRY_BASE_SECONDS
    models: tuple[Model, ...] = field(default_factory=lambda: tuple(CANDIDATE_MODELS))

    def snapshot(self) -> dict:
        """The settings worth saving with a run."""
        return {
            "n_answers": self.n_answers,
            "judge_models": list(self.judge_models),
            "judge_orders": list(self.judge_orders),
            "lettered_order": list(self.lettered_order),
            "exclude_self_judging": self.exclude_self_judging,
            "few_shots_per_level": self.few_shots_per_level,
            "merge_method": self.merge_method,
            "models": [{"name": m.name, "provider": m.provider, "effort": m.effort} for m in self.models],
        }
