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
    # zfj todo: testing the whole project with 1 candidate (2026-11-25). Uncomment
    # the other 16 for a real run. gpt-5.4-mini is not a judge, so all 3 judges
    # judge its answers (a judge never judges its own).
    # Model("gpt-6.1-sol", "OpenAI"),
    # Model("gpt-5.5", "OpenAI"),
    Model("gpt-5.4-mini", "OpenAI"),
    # Model("gpt-oss-120b", "OpenAI"),
    # Model("claude-fable-5-1", "Anthropic"),
    # Model("claude-opus-5-5", "Anthropic"),
    # Model("claude-sonnet-5-5", "Anthropic"),
    # Model("claude-haiku-5-5", "Anthropic"),
    # Model("Llama-4-Maverick-17B-128E-Instruct-FP8", "Meta"),
    # zfj todo: dropped 2026-11-23, the subscription's shared quota for it was full
    # (2000 of 2000). Uncomment when there is quota.
    # Model("Llama-3.3-70B-Instruct", "Meta"),
    # zfj todo: dropped 2026-11-23, not listed on Azure in northcentralus or eastus2.
    # Model("muse-spark-1.3", "Meta"),
    # Model("DeepSeek-V4-Pro", "DeepSeek"),
    # Model("DeepSeek-V4-Flash", "DeepSeek"),
    # Model("grok-4.7", "xAI"),
    # Model("grok-4-1-fast-non-reasoning", "xAI"),
    # Model("Mistral-Large-3", "Mistral AI"),
    # Model("MAI-Thinking-1", "Microsoft"),
    # Model("Kimi-K3.1-Pro", "MoonshotAI"),
    # zfj todo: dropped 2026-11-23, Azure lists it in northcentralus with no deployment option.
    # Model("qwen3.8-27b", "Alibaba"),
    # Model("cohere-command-a", "Cohere"),
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

# "rotate": each judge judges each answer once, and the judges get different
# orders, rotated per answer, so every answer is still seen in all 3 orders and
# every judge uses each order about equally often. 3 judge calls per answer.
# "all": each judge judges each answer once per order. 9 judge calls per answer.
JUDGE_ORDER_MODE = "rotate"

# When a judge is also a ranked model, it does not judge its own answers.
EXCLUDE_SELF_JUDGING = True

# Few-shot examples shown per rubric level, taken from the question's category.
FEW_SHOTS_PER_LEVEL = 2
EXAMPLE_STATUSES = ["Confirmed"]

# --- Merging ----------------------------------------------------------------
# How one answer's judgements become one label. Methods are in merge.py.
MERGE_METHOD = "median_of_medians"

# --- Data -------------------------------------------------------------------
# Where the questions, rubric and examples come from: "sql" or "disk".
# Loaders are in loaders.py.
DATA_SOURCE = "sql"
# The files `import-materials` copies into SQL. A file's name without .csv is its
# version in SQL, so these also choose which version a run reads.
# Human-written questions are evaluated first. The AI-generated set is
# question_set_v1.csv; switch back by naming it here.
QUESTIONS_FILE = "question_set_human_v1.csv"
RUBRIC_FILE = "rubric_v3.csv"
EXAMPLES_FILE = "rubric_examples_v1.csv"

# Category names as written in the question set or examples file -> the
# rubric's spelling. Add a line here when a file spells a category differently.
CATEGORY_ALIASES = {
    "Racial equity": "Racial Equity",
    "Mental health": "Mental Health",
    "Science, technology, medicine": "Science, Technology, & Medicine",
    "Science Technology Medicine": "Science, Technology, & Medicine",
}

# Categories listed here are not asked or judged. Use it when a category's
# rubric is incomplete. Empty since rubric_v3 (2026-11-22) wrote every level.
SKIP_CATEGORIES = []

# --- Calls ------------------------------------------------------------------
# Who answers and judges: "azure" calls the real models, "mock" uses
# mock_llm.py (a fixed answer, a fixed reason, a seeded random label).
# This is the default. `--llm azure` or `--llm mock` on `run` or `rejudge`
# overrides it for one command, with or without --debug.
# zfj todo: set to "azure" once development is finished.
LLM_MODE = "mock"
# The same seed gives the same mock results every run. Change it for new ones.
MOCK_SEED = 42
# Share of mock answers that act as if a content filter blocked them.
MOCK_BLOCK_RATE = 0.02

# The last step of a run writes the ranking (rankings table, or the local
# rankings file with --debug). If calls failed (timeouts, errors), it is not
# written, so the website keeps the last good ranking; `run --run-id N` retries
# the failed calls and then writes it. True writes it anyway.
PUBLISH_IF_CALLS_FAILED = False

# --debug commands write src/logs/<date>.log, with every prompt and response.
# Today's file and those of the 6 days before are kept; older ones are deleted.
DEBUG_LOG_KEEP_DAYS = 7

WORKERS = 8
MAX_RETRIES = 3
RETRY_BASE_SECONDS = 2.0
# Candidates answer with their provider's default settings: no temperature,
# reasoning effort or thinking is sent. Only this length cap is, the same for
# every model (Claude requires one). It is large because a reasoning model
# spends part of it on reasoning.
ANSWER_MAX_TOKENS = 8000
# Judges are configured. A judge model that rejects a setting (many reasoning
# models reject temperature) is retried without it; every call records the
# settings it really used.
JUDGE_MAX_TOKENS = 2000
JUDGE_TEMPERATURE = 0.0
JUDGE_REASONING_EFFORT = "medium"


@dataclass(frozen=True)
class ScoringConfig:
    """The settings one run uses. The defaults are the values above."""

    n_answers: int = N_ANSWERS
    judge_models: tuple[str, ...] = tuple(JUDGE_MODELS)
    judge_orders: tuple[str, ...] = tuple(JUDGE_ORDERS)
    judge_order_mode: str = JUDGE_ORDER_MODE
    lettered_order: tuple[str, ...] = tuple(LETTERED_ORDER)
    exclude_self_judging: bool = EXCLUDE_SELF_JUDGING
    few_shots_per_level: int = FEW_SHOTS_PER_LEVEL
    merge_method: str = MERGE_METHOD
    llm_mode: str = LLM_MODE
    publish_if_calls_failed: bool = PUBLISH_IF_CALLS_FAILED
    mock_seed: int = MOCK_SEED
    workers: int = WORKERS
    retry_base_seconds: float = RETRY_BASE_SECONDS
    models: tuple[Model, ...] = field(default_factory=lambda: tuple(CANDIDATE_MODELS))

    def snapshot(self) -> dict:
        """The settings worth saving with a run."""
        return {
            "n_answers": self.n_answers,
            "judge_models": list(self.judge_models),
            "judge_orders": list(self.judge_orders),
            "judge_order_mode": self.judge_order_mode,
            "lettered_order": list(self.lettered_order),
            "exclude_self_judging": self.exclude_self_judging,
            "few_shots_per_level": self.few_shots_per_level,
            "merge_method": self.merge_method,
            # A mock run is marked, so its results are never mistaken for real ones.
            "llm_mode": self.llm_mode,
            **({"mock_seed": self.mock_seed} if self.llm_mode == "mock" else {}),
            "models":[{"name": m.name, "provider": m.provider, "effort": m.effort} for m in self.models],
        }
