# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""
Deterministic error classifier for autograder execution events.

Maps raw executor error output onto the small closed category set stored on
AutograderExecutionEvent, so "most common errors" can be a cheap GROUP BY
instead of clustering unique tracebacks.
"""
import re
from typing import Optional, Tuple

MAX_MESSAGE_LENGTH = 500
MAX_SCAN_LENGTH = 4000

# Ordered, first-match-wins. Each signal is a regex searched against lowercased
# output. Specific categories must come before the greedy runtime_error fallback.
_CATEGORY_SIGNALS = [
    ('timeout', [
        'timed out',
        'timeouterror',
        'execution timeout or incomplete',
        'soft time limit',
        'softtimelimitexceeded',
    ]),
    ('missing_dependency', [
        'modulenotfounderror',
        'importerror: no module',
        'no module named',
        'cannot find module',                      # node
        'there is no package called',              # R
        r'package [\w.]* ?does not exist',         # java
    ]),
    ('compile_error', [
        'syntaxerror',
        'cannot find symbol',
        'javac',
        'error: expected',
        'compilation terminated',
        'compilation failed',
        'undefined reference',
        'unexpected token',
    ]),
    # A category may appear more than once so a specific signal can outrank a
    # generic one that would otherwise match first. The container never ran
    # here (payload pushed through argv, see Executor._put_file), so this is
    # infra even though the surrounding text says "missing markers".
    ('infra', [
        'argument list too long',
        'e2big',
    ]),
    ('marker_extraction', [
        'missing markers',
        'failed to extract results',
    ]),
    ('infra', [
        'no executor',
        'docker',
        'container',
        'image not found',
        'cache save failed',
        'connection refused',
        'doesnotexist',
    ]),
]


def _sample_message(text: str) -> str:
  """Pick the most informative single line of the error output.

  Python tracebacks carry `ExcType: message` on their last line; everything
  else is best represented by its first non-empty line.
  """
  lines = [line.strip() for line in text.splitlines() if line.strip()]
  if not lines:
    return ''
  if 'traceback (most recent call last)' in text.lower():
    return lines[-1][:MAX_MESSAGE_LENGTH]
  return lines[0][:MAX_MESSAGE_LENGTH]


def classify_error(text: Optional[str]) -> Tuple[str, str]:
  """Classify raw error output into (category, truncated sample message)."""
  if not text or not text.strip():
    return ('unknown', '')

  haystack = text[:MAX_SCAN_LENGTH].lower()
  for category, signals in _CATEGORY_SIGNALS:
    if any(re.search(signal, haystack) for signal in signals):
      return (category, _sample_message(text))
  # Any remaining non-empty error output is a plain runtime failure.
  return ('runtime_error', _sample_message(text))
