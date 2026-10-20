"""Where Discord markdown shows text verbatim or as someone else's, so a check of
Kiki's own words (her emoji) leaves it alone: code, links and quotes.

Code follows CommonMark, which Discord's parser is close to. A fence is a line
of at least three backticks or tildes (a backtick fence's info string has no
backticks), closed by a line of at least as many of the same character, or by
the end of the text. An inline code span opens with a run of backticks and
closes at the next run of exactly the same length; a run with no match is
literal text.
"""

import re

FENCE_OPEN = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
URL = re.compile(r"\[[^\]\n]*\]\([^)\s]*\)|<https?://[^>\s]+>|https?://\S+")
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
    for pattern in (URL, QUOTE_LINE, QUOTE_REST):
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
    runs = list(BACKTICKS.finditer(text, start, end))
    spans, i = [], 0
    while i < len(runs):
        opener = runs[i]
        width = len(opener[0])
        closer = next((j for j in range(i + 1, len(runs)) if len(runs[j][0]) == width), None)
        if closer is None:
            i += 1
            continue
        spans.append((opener.start(), runs[closer].end()))
        i = closer + 1
    return spans
