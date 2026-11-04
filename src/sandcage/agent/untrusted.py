"""Wrapping untrusted text (file contents, command output, scout notes) for a model.

The block is ``<untrusted source="...">`` … ``</untrusted>``. Text inside can never close
it early or open a fake one: every ``<`` that would start an ``untrusted`` tag (any case,
any spacing, opening or closing) is replaced by a single left angle quote (U+2039)
before wrapping. The text is masked and capped first, and the source label is masked
and escaped.
"""

from __future__ import annotations

import re
from typing import Final

from sandcage.masking import mask

UNTRUSTED_LIMIT: Final = 8000
_TAG: Final = re.compile(r"<(?=\s*/?\s*untrusted)", re.IGNORECASE)
_SOURCE_BAD: Final = re.compile(r'["<>\x00-\x1f\x7f]')


def neutralise(text: str) -> str:
    return _TAG.sub("\u2039", text)


def source_label(source: str) -> str:
    return _SOURCE_BAD.sub("_", mask(source))[:200]


def wrap_untrusted(text: str, source: str, *, limit: int = UNTRUSTED_LIMIT) -> str:
    body = mask(text)
    if len(body) > limit:
        body = body[:limit] + f"\n[truncated: {len(body) - limit} more characters]"
    return f'<untrusted source="{source_label(source)}">\n{neutralise(body)}\n</untrusted>'
