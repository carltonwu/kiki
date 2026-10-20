"""Where Discord markdown shows text verbatim or as someone else's, so a check of
Kiki's own words (her emoji) leaves it alone: code, links and quotes. Every scan
is linear in the text: it runs in the event loop.

Code follows CommonMark, which Discord's parser is close to. A fence is a line
of at least three backticks or tildes (a backtick fence's info string has no
backticks), closed by a line of at least as many of the same character, or by
the end of the text. An inline code span opens with a run of backticks and
closes at the next run of exactly the same length; a run with no match is
literal text. A backslash before an opening backtick escapes it; inside a span,
backslashes are literal, so one can't escape the closer.

Links are protected conservatively rather than parsed: from a markdown link's
`](` to the next whitespace (so a destination's parentheses, balanced or not,
never matter), and bare or `<...>` URLs to theirs. A link's text in brackets is
still checked, like any of Kiki's words.
"""

import bisect
import re

FENCE_OPEN = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
LINK = re.compile(r"\]\(\S*|<https?://[^>\s]*>?|https?://\S+")
QUOTE_LINE = re.compile(r"^ *> .*$", re.MULTILINE)
QUOTE_REST = re.compile(r"^ *>>> .*\Z", re.MULTILINE | re.DOTALL)
BACKTICKS = re.compile(r"`+")


def protected(text: str) -> list[bool]:
    """Per character: inside code, a link or a quote."""
    mask = [False] * len(text)

    def mark(start: int, end: int) -> None:
        mask[start:end] = [True] * (end - start)

    for start, end in fences(text):
        mark(start, end)
    for start, end in free(text, mask):
        for span in inline_code(text, start, end):
            mark(*span)
    for pattern in (LINK, QUOTE_LINE, QUOTE_REST):
        for match in pattern.finditer(text):
            if not mask[match.start()]:
                mark(match.start(), match.end())
    return mask


def fences(text: str) -> list[tuple[int, int]]:
    spans, start, opened = [], 0, None
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        if opened is None:
            match = FENCE_OPEN.fullmatch(bare)
            if match and not (match[1][0] == "`" and "`" in match[2]):
                opened = (start, match[1][0], len(match[1]))
        else:
            _, char, length = opened
            close = re.fullmatch(rf" {{0,3}}({re.escape(char)}{{{length},}})[ \t]*", bare)
            if close:
                spans.append((opened[0], start + len(line)))
                opened = None
        start += len(line)
    if opened is not None:
        spans.append((opened[0], len(text)))
    return spans


def fence_lines(text: str) -> list[bool]:
    """Per line of `text.split("\\n")`: inside a fenced code block (its fence
    lines included)."""
    spans = fences(text)
    flags, start, k = [], 0, 0
    for line in text.split("\n"):
        while k < len(spans) and spans[k][1] <= start:
            k += 1
        flags.append(k < len(spans) and spans[k][0] <= start)
        start += len(line) + 1
    return flags


def free(text: str, mask: list[bool]) -> list[tuple[int, int]]:
    """The stretches of `text` not yet masked."""
    spans, start = [], None
    for i, masked in enumerate([*mask, True]):
        if not masked and start is None:
            start = i
        elif masked and start is not None:
            spans.append((start, i))
            start = None
    return spans


def inline_code(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """CommonMark code spans in `text[start:end]`, in one pass over its backtick
    runs: each width's runs are indexed, so finding a closer is a bisect."""
    runs = [(m.start(), m.end()) for m in BACKTICKS.finditer(text, start, end)]
    by_width: dict[int, list[int]] = {}
    for i, (a, b) in enumerate(runs):
        by_width.setdefault(b - a, []).append(i)
    spans, i = [], 0
    while i < len(runs):
        a, b = runs[i]
        if escaped(text, a, start):
            a += 1  # `\` makes the first backtick literal; the rest may still open
        width = b - a
        later = by_width.get(width, [])
        j = bisect.bisect_right(later, i)
        if width == 0 or j == len(later):
            i += 1
            continue
        closer = later[j]
        spans.append((a, runs[closer][1]))
        i = closer + 1
    return spans


def escaped(text: str, at: int, start: int) -> bool:
    """Whether an odd number of backslashes come right before `text[at]`."""
    slashes = 0
    while at - slashes - 1 >= start and text[at - slashes - 1] == "\\":
        slashes += 1
    return slashes % 2 == 1
