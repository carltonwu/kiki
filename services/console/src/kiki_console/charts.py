"""Server-rendered SVG charts: no client-side code, so a strict CSP holds.

Colors are roles (CSS custom properties in static/style.css); each segment has
a <title> for hover, and every chart sits next to a table with the same data.
"""

from html import escape

SERIES = (("chat", "Chat"), ("decide", "Decide"), ("console", "Console"))


def usd(amount: float | None) -> str:
    if amount is None:
        return "–"
    if amount == 0:
        return "$0"
    return f"${amount:.4f}" if amount < 1 else f"${amount:,.2f}"


def daily_cost_svg(days: list[dict], width: int = 720, height: int = 180) -> str:
    """Stacked daily bars, one hue per cost kind, baseline at the bottom."""
    left, bottom, top = 60, 20, 8
    plot_w, plot_h = width - left - 4, height - bottom - top
    peak = max((sum(float(d[k]) for k, _ in SERIES) for d in days), default=0) or 1
    slot = plot_w / max(len(days), 1)
    bar = max(slot - 2, 1)  # 2px gap between adjacent bars
    parts = [
        (
            f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="Daily LLM cost, last {len(days)} days">'
        )
    ]
    for frac in (0, 0.5, 1):
        y = top + plot_h * (1 - frac)
        parts.append(f'<line class="grid" x1="{left}" x2="{width}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(
            f'<text class="axis" x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">'
            f"{escape(usd(peak * frac))}</text>"
        )
    for i, d in enumerate(days):
        x = left + i * slot + 1
        y = top + plot_h
        for n, (key, label) in enumerate(SERIES, 1):
            value = float(d[key])
            if value <= 0:
                continue
            h = plot_h * value / peak
            # 2px surface gap between stacked segments, when there's room.
            drawn = max(h - 2, 1) if h > 3 else h
            y -= h
            parts.append(
                f'<rect class="s{n}" x="{x:.1f}" y="{y + (h - drawn):.1f}" width="{bar:.1f}" '
                f'height="{drawn:.1f}" rx="2"><title>{escape(str(d["day"]))} · {label}: '
                f"{escape(usd(value))}</title></rect>"
            )
        last = i == len(days) - 1
        if last or (i % 7 == 0 and len(days) - 1 - i > 3):
            # The last label ends at the bar's right edge so it stays inside the chart.
            anchor, at = ("end", x + bar) if last else ("middle", x + bar / 2)
            parts.append(
                f'<text class="axis" x="{at:.1f}" y="{height - 4}" text-anchor="{anchor}">'
                f"{escape(d['day'].strftime('%b %-d'))}</text>"
            )
    parts.append("</svg>")
    return "".join(parts)
