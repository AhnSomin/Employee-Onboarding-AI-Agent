"""Korean tokenization for keyword search (kiwipiepy morphemes).

Article references stay one token: "제15조", "제15조의2" (also written with
spaces, "제 15 조의 2"). Content morphemes are kept (nouns, verb and adjective
stems, roots, numbers, foreign words, hanja); particles and endings are not.
"""

from __future__ import annotations

import re
import threading
from functools import lru_cache

ARTICLE = re.compile(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?")
KEEP_TAGS = ("NNG", "NNP", "NNB", "NR", "SN", "SL", "SH", "XR", "VV", "VA")
_lock = threading.Lock()


@lru_cache(maxsize=1)
def _kiwi():
    from kiwipiepy import Kiwi

    return Kiwi()


def article_tokens(text: str) -> list[str]:
    return [f"제{m.group(1)}조" + (f"의{m.group(2)}" if m.group(2) else "") for m in ARTICLE.finditer(text)]


def tokenize(text: str) -> list[str]:
    """Search terms of a text, in order, article references first-class."""
    terms = article_tokens(text)
    rest = ARTICLE.sub(" ", text)
    with _lock:  # Kiwi is not documented as thread-safe; Streamlit runs sessions in threads
        tokens = _kiwi().tokenize(rest)
    for token in tokens:
        tag = token.tag.split("-")[0]
        if tag in KEEP_TAGS:
            form = token.form.lower()
            if tag in ("VV", "VA") and len(form) < 2:
                continue  # one-syllable stems (하, 되, 있) carry no meaning on their own
            terms.append(form)
    return terms


def content_terms(text: str) -> set[str]:
    """Distinct terms worth matching on: no bare numbers or one-character nouns."""
    return {t for t in tokenize(text) if len(t) >= 2 and not t.isdigit()}
