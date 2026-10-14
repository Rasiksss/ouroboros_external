"""Dynamic Visualization skill — generates Plotly charts."""
from __future__ import annotations
import json
from typing import Any


def create_chart(query_input: dict[str, Any]) -> dict[str, Any]:
    """
    Create a chart from provided data.
    Input: {chart_type: str, data: dict, title: str, x_label: str, y_label: str}
    chart_type: "bar" | "line" | "waterfall" | "pie" | "table"
    Output: {plotly_json: str, description: str}
    """
    try:
        import plotly.graph_objects as go
        import plotly.express as px

        chart_type = query_input.get("chart_type", "bar")
        data = query_input.get("data", {})
        title = query_input.get("title", "")
        x_label = query_input.get("x_label", "")
        y_label = query_input.get("y_label", "")

        fig = None

        if chart_type == "bar":
            categories = data.get("categories", list(data.keys()))
            values = data.get("values", list(data.values()) if isinstance(data, dict) else [])
            fig = go.Figure(go.Bar(x=categories, y=values, text=values, textposition="auto"))

        elif chart_type == "line":
            x = data.get("x", [])
            y_series = data.get("y_series", {})
            fig = go.Figure()
            if y_series:
                for name, vals in y_series.items():
                    fig.add_trace(go.Scatter(x=x, y=vals, mode="lines+markers", name=name))
            else:
                y = data.get("y", [])
                fig.add_trace(go.Scatter(x=x, y=y, mode="lines+markers"))

        elif chart_type == "waterfall":
            measures = data.get("measures", [])
            x = data.get("x", [])
            y = data.get("y", [])
            text = data.get("text", [str(v) for v in y])
            fig = go.Figure(go.Waterfall(
                measure=measures, x=x, y=y, text=text,
                connector={"line": {"color": "rgb(63, 63, 63)"}},
            ))

        elif chart_type == "pie":
            labels = data.get("labels", [])
            values = data.get("values", [])
            fig = go.Figure(go.Pie(labels=labels, values=values))

        elif chart_type == "table":
            headers = data.get("headers", [])
            rows = data.get("rows", [])
            fig = go.Figure(go.Table(
                header=dict(values=headers, fill_color="lightblue"),
                cells=dict(values=list(zip(*rows)) if rows else [[]]),
            ))

        if fig is None:
            return {"status": "error", "message": f"Unknown chart type: {chart_type}"}

        fig.update_layout(
            title=title,
            xaxis_title=x_label,
            yaxis_title=y_label,
            template="plotly_white",
        )

        return {
            "status": "success",
            "plotly_json": fig.to_json(),
            "description": f"График '{title}' типа '{chart_type}' успешно создан",
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}
