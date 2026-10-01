# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
"""Lenient parsing of JSON that a language model was asked to produce.

Models break the "reply with a bare JSON array" contract in a handful of recurring
ways — ```json fences, prose around the payload, an object wrapper, literal newlines
or unescaped quotes inside strings (code in a description). The generation tasks
parse through here so a run isn't failed over formatting; anything that still
doesn't parse raises ``ValueError`` and the task records the raw output.
"""
import json
import re


def json_candidates(text: str):
    """Yield the substrings of a model output that may hold its JSON payload, most
    literal first: the whole text, the body of a wrapping ```json fence, any fenced
    block, the outermost bracketed span (prose around the JSON), and finally the first
    complete JSON value (the payload repeated or followed by another one)."""
    cleaned = (text or '').strip()
    yield cleaned
    if cleaned.startswith('```'):
        # Strip a leading ```json / ``` fence and trailing ```.
        body = cleaned.split('\n', 1)[-1] if '\n' in cleaned else cleaned
        if body.endswith('```'):
            body = body[:-3]
        yield body.strip()
    for fenced in re.finditer(r'```(?:json)?[ \t]*\n(.*?)```', cleaned, re.DOTALL):
        yield fenced.group(1).strip()
    starts = [i for i in (cleaned.find('['), cleaned.find('{')) if i >= 0]
    end = max(cleaned.rfind(']'), cleaned.rfind('}'))
    if starts and end > min(starts):
        yield cleaned[min(starts):end + 1]
    if starts:
        # The payload followed by more JSON (a model that printed the array twice):
        # the outermost span above fails, so take the first complete value.
        try:
            _, stop = json.JSONDecoder(strict=False).raw_decode(cleaned, min(starts))
        except json.JSONDecodeError:
            return
        yield cleaned[min(starts):stop]


def escape_stray_quotes(text: str) -> str:
    """Escape double quotes that sit inside a JSON string without a backslash — the
    most common way a model breaks JSON when a string holds code (``df["Year"]`` in a
    description). A quote closes the string only if the next non-space character may
    legally follow a string there (``,`` or the container's closer; ``:`` after a key).
    Valid JSON passes through unchanged."""
    out: list[str] = []
    stack: list[str] = []
    in_str = expect_key = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if ch == '\\':
                out.append(text[i:i + 2])
                i += 2
                continue
            if ch == '"':
                j = i + 1
                while j < n and text[j] in ' \t\r\n':
                    j += 1
                nxt = text[j] if j < n else ''
                closer = '}' if stack and stack[-1] == '{' else ']'
                legal = {':'} if expect_key else {',', closer, ''}
                if nxt in legal:
                    in_str = False
                    out.append(ch)
                else:
                    out.append('\\"')
                i += 1
                continue
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_str = True
        elif ch in '{[':
            stack.append(ch)
            expect_key = ch == '{'
        elif ch in '}]':
            if stack:
                stack.pop()
            expect_key = False
        elif ch == ':':
            expect_key = False
        elif ch == ',':
            expect_key = bool(stack) and stack[-1] == '{'
        out.append(ch)
        i += 1
    return ''.join(out)


def loads_lenient(candidate: str):
    """``json.loads`` that tolerates literal control characters inside strings and,
    failing that, unescaped double quotes inside strings."""
    try:
        return json.loads(candidate, strict=False)
    except json.JSONDecodeError:
        return json.loads(escape_stray_quotes(candidate), strict=False)


def parse_json_questions(text: str) -> list:
    """Parse a model's JSON array of questions.

    Tolerates the usual ways models break the bare-JSON-array contract: ```json fences,
    prose before/after the JSON, an object wrapper ({"questions": [...]}), literal
    newlines and unescaped quotes inside strings. Raises ``ValueError`` when no
    candidate parses."""
    data = None
    for candidate in json_candidates(text):
        try:
            data = loads_lenient(candidate)
            break
        except json.JSONDecodeError:
            continue
    else:
        raise ValueError('The model output contains no JSON.')
    if isinstance(data, dict):
        # {"questions": [...]} — or any single list-valued key.
        lists = [v for v in data.values() if isinstance(v, list)]
        data = data['questions'] if isinstance(data.get('questions'), list) else (
            lists[0] if len(lists) == 1 else [])
    return data if isinstance(data, list) else []
