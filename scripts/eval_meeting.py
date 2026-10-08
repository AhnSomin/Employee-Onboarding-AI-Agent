"""Evaluate meeting extraction on the labeled sample minutes and print Markdown tables.

Run with:
    uv run python scripts/eval_meeting.py [--details] [--repeat N] [--force-fallback]

Models come from GEMINI_MODEL_PRIMARY / GEMINI_MODEL_FALLBACKS (environment
first, then .env). Extraction events are not written to the metrics log.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from onboarding_agent import config
from onboarding_agent.meeting.evaluation import load_gold, render_markdown, score_sample
from onboarding_agent.meeting.extract import extract_meeting, load_prompt
from onboarding_agent.meeting.loader import load_meeting
from onboarding_agent.meeting.roster import load_roster

DEFAULT_GOLD = config.REPO_ROOT / "data" / "eval" / "meeting_gold.jsonl"
DEFAULT_SAMPLES = config.REPO_ROOT / "data" / "samples"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES)
    parser.add_argument("--repeat", type=int, default=1, help="run every sample N times")
    parser.add_argument("--details", action="store_true", help="per-label table")
    parser.add_argument("--force-fallback", action="store_true", help="rule-based only")
    parser.add_argument("--env-file", type=Path, help=".env to read instead of the repo root one")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if args.env_file:
        config.DOTENV_PATH = args.env_file.resolve()
        config.reset_settings_cache()
    settings = config.get_settings()
    roster = load_roster(settings.roster_path)

    scores = []
    for gold in load_gold(args.gold):
        loaded = load_meeting(data=(args.samples / gold.sample).read_bytes(), filename=gold.sample)
        for _ in range(args.repeat):
            started = time.perf_counter()
            result = extract_meeting(
                loaded.text,
                title=loaded.title_suggestion,
                meeting_date=gold.meeting_date,
                source_filename=gold.sample,
                roster=roster,
                force_fallback=True if args.force_fallback else None,
                log_metrics=False,
            )
            latency = round((time.perf_counter() - started) * 1000)
            scores.append(score_sample(gold, result, latency))

    chain = ", ".join(settings.gemini_models) or "(모델 미지정)"
    mode = "규칙 기반 강제" if args.force_fallback else f"모델 체인: {chain}"
    print(f"### 회의록 추출 평가 — {mode} · 프롬프트 {load_prompt().version} · 반복 {args.repeat}회\n")
    print(render_markdown(scores, details=args.details))
    return 0


if __name__ == "__main__":
    sys.exit(main())
