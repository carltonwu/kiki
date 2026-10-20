"""What a bot shows in place of `content`: rich embeds and Components V2 cards.

A quote bot's post, a game bot's result or a starboard entry has no `content`;
without this, Kiki reads it as an empty message. Link previews on people's
messages aren't cards (they're `link`, `article`, `image` or `video` embeds), and
a message with text keeps its text: cards only stand in for a blank one."""

from collections.abc import Iterator

TEXT_DISPLAY = 10  # A Components V2 text display.


def card_text(embeds: list | None, components: list | None) -> str:
    parts: list[str] = []
    for embed in embeds or []:
        if not isinstance(embed, dict) or embed.get("type", "rich") != "rich":
            continue
        parts.append((embed.get("author") or {}).get("name") or "")
        parts += [embed.get("title") or "", embed.get("description") or ""]
        for field in embed.get("fields") or []:
            name, value = field.get("name") or "", field.get("value") or ""
            parts.append(f"{name}: {value}" if name and value else name or value)
        parts.append((embed.get("footer") or {}).get("text") or "")
    parts += texts(components or [])
    return "\n".join(p.strip() for p in parts if p and p.strip())


def texts(components: list) -> Iterator[str]:
    """Text displays at any depth: containers and sections nest them."""
    for component in components:
        if not isinstance(component, dict):
            continue
        if component.get("type") == TEXT_DISPLAY and isinstance(component.get("content"), str):
            yield component["content"]
        yield from texts(component.get("components") or [])
        if isinstance(accessory := component.get("accessory"), dict):
            yield from texts([accessory])


def shown_text(m: dict) -> str:
    """A row's text as Kiki reads it: its content, or its card when it has none."""
    if m["content"].strip():
        return m["content"]
    return card_text(m.get("embeds"), m.get("components"))
