"""Plotly figures for the per-candidate dashboard."""

import plotly.graph_objects as go

import config
import gaps as gaps_mod
from schemas import ResumeResult

_KIND_COLOR = {"overall": "#6366f1", "must_have": "#ef4444", "nice_to_have": "#10b981"}


def radar_chart(result: ResumeResult) -> go.Figure | None:
    """Skill-match radar: one axis per criterion, value = met (0-1)."""
    scores = [s for s in result.criterion_scores]
    if len(scores) < 3:
        return None  # a radar needs 3+ axes to be readable
    labels = [_short(s.criterion_text) for s in scores]
    values = [s.met for s in scores]
    fig = go.Figure(go.Scatterpolar(
        r=values + values[:1],
        theta=labels + labels[:1],
        fill="toself",
        line_color="#6366f1",
        fillcolor="rgba(99,102,241,0.25)",
    ))
    fig.update_layout(
        polar={"radialaxis": {"range": [0, 1], "showticklabels": True}},
        showlegend=False, height=340, margin=dict(l=60, r=60, t=30, b=30),
    )
    return fig


def breakdown_chart(result: ResumeResult, criteria_weights: dict) -> go.Figure:
    """Horizontal bars: points earned vs points available per criterion."""
    rows = sorted(result.criterion_scores,
                  key=lambda s: ({"overall": 0, "must_have": 1}.get(s.kind, 2), -s.met))
    labels, earned, available, colors = [], [], [], []
    for s in rows:
        w = (config.OVERALL_FIT_WEIGHT if s.kind == "overall"
             else criteria_weights.get(s.criterion_id, 0))
        labels.append(_short(s.criterion_text))
        earned.append(round(s.met * w, 2))
        available.append(w)
        colors.append(_KIND_COLOR.get(s.kind, "#94a3b8"))

    fig = go.Figure()
    fig.add_bar(y=labels, x=available, orientation="h", name="available",
                marker_color="rgba(148,163,184,0.3)")
    fig.add_bar(y=labels, x=earned, orientation="h", name="earned",
                marker_color=colors)
    fig.update_layout(
        barmode="overlay", height=90 + 45 * len(labels), showlegend=False,
        margin=dict(l=10, r=10, t=10, b=10),
        xaxis_title="points", yaxis=dict(autorange="reversed"),
    )
    return fig


def timeline_chart(result: ResumeResult) -> go.Figure | None:
    """Experience timeline built from the parsed start/end dates."""
    if not result.structured:
        return None
    rows = []
    for e in result.structured.experience:
        start = gaps_mod.parse_date(e.start_date)
        end = gaps_mod.parse_date(e.end_date)
        if start and end and end >= start:
            label = f"{e.title or 'Role'} @ {e.company or '?'}"
            rows.append((label, start, end))
    if not rows:
        return None

    rows.sort(key=lambda r: r[1])
    fig = go.Figure()
    for label, start, end in rows:
        fig.add_trace(go.Scatter(
            x=[start, end], y=[label, label],
            mode="lines+markers",
            line=dict(width=12, color="#6366f1"),
            marker=dict(size=8),
            hovertemplate=f"{label}<br>%{{x|%b %Y}}<extra></extra>",
            showlegend=False,
        ))
    fig.update_layout(
        height=110 + 45 * len(rows),
        margin=dict(l=10, r=10, t=10, b=10),
        yaxis=dict(autorange="reversed"),
    )
    return fig


def _short(text: str, n: int = 34) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"
