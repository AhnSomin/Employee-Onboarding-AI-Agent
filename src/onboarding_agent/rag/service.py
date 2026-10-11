"""Wire the regulation Q&A pieces from settings: index, embedder, retriever, model client, budget.

Paths (all git-ignored except the config files):
- REG_INDEX_DIR (default data/index/regulations): index versions and the embedding cache
- data/regulations/usage.jsonl: every Gemini request made for regulation Q&A
- data/regulations/pending_escalations.jsonl: inquiry drafts (never sent)
- data/regulations/glossary.yaml, targets.yaml: configuration (committed)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import REPO_ROOT, Settings
from ..llm.client import LLMClient
from .answer import QAService
from .embed import EmbeddingCache, EmbeddingConfig, GeminiEmbedder
from .index import LoadedIndex, load_index
from .retrieve import DEFAULT_MIN_SCORE, DEFAULT_MODE, Retriever, load_glossary
from .usage import CountingGenai, UsageMeter

REG_DATA = REPO_ROOT / "data" / "regulations"
USAGE_LOG = REG_DATA / "usage.jsonl"
ESCALATIONS = REG_DATA / "pending_escalations.jsonl"
GLOSSARY = REG_DATA / "glossary.yaml"
TOOL_LOG = REG_DATA / "logs" / "tool_mode.jsonl"
BUDGET = REG_DATA / "budget.yaml"


def embedding_config(settings: Settings) -> EmbeddingConfig:
    return EmbeddingConfig(
        model=settings.gemini_embed_model,
        dim=settings.gemini_embed_dim,
        query_task=settings.gemini_embed_query_task,
    )


def real_genai_client(settings: Settings) -> Any:
    from google import genai
    from google.genai import types

    if settings.gemini_api_key is None:
        raise RuntimeError("GEMINI_API_KEY가 설정되지 않았습니다.")
    return genai.Client(
        api_key=settings.gemini_api_key.get_secret_value(),
        http_options=types.HttpOptions(timeout=int(settings.llm_timeout_sec * 1000)),
    )


class BudgetedLLMClient(LLMClient):
    """The shared client with one attempt per model: under a small call budget a transient
    error moves to the next model instead of retrying, and a 429 stops everything (usage.py)."""

    def _with_retries(self, fn):  # noqa: ANN001, ANN202 - same contract as the parent
        return fn()


@dataclass
class Runtime:
    settings: Settings
    meter: UsageMeter
    counter: CountingGenai | None
    embedder: GeminiEmbedder | None
    index: LoadedIndex
    retriever: Retriever
    service: QAService


def counting_client(settings: Settings, meter: UsageMeter, purpose: str = "spare") -> CountingGenai | None:
    try:
        return CountingGenai(real_genai_client(settings), meter, purpose)
    except RuntimeError:
        return None


def make_embedder(settings: Settings, counter: CountingGenai | None, root: Path | None = None) -> GeminiEmbedder | None:
    if counter is None:
        return None
    config = embedding_config(settings)
    return GeminiEmbedder(counter, config, EmbeddingCache((root or settings.reg_index_dir) / "cache", config))


def build_runtime(settings: Settings, *, meter: UsageMeter | None = None) -> Runtime:
    """Load the current index (IndexUnavailable if there is none) and everything that answers from it."""
    meter = meter or UsageMeter.from_budget_file(USAGE_LOG, BUDGET, enforce=settings.rag_enforce_budget)
    config = embedding_config(settings)
    index = load_index(settings.reg_index_dir, config)
    counter = counting_client(settings, meter)
    embedder = make_embedder(settings, counter)
    retriever = Retriever(
        index,
        embedder,
        glossary=load_glossary(GLOSSARY),
        min_score=settings.rag_min_score if settings.rag_min_score is not None else DEFAULT_MIN_SCORE,
    )
    llm = BudgetedLLMClient(settings, genai_client=counter) if counter is not None else LLMClient(settings)
    service = QAService(
        index,
        retriever,
        llm,
        escalations=ESCALATIONS,
        counter=counter,
        primary_model=settings.gemini_model_primary,
        top_k=settings.rag_top_k,
        mode=settings.rag_retrieval_mode or DEFAULT_MODE,
        max_agent_steps=settings.rag_max_agent_steps,
        tool_log=TOOL_LOG,
    )
    return Runtime(settings, meter, counter, embedder, index, retriever, service)
