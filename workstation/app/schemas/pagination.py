"""Dynamic paginator schema factory."""

from __future__ import annotations

from pydantic import BaseModel, create_model


def generate_paginator_schema(data_schema: type[BaseModel]) -> type[BaseModel]:
    """Build `Paginator<Name>` schema where `data: list[data_schema]`."""
    model_name = f"Paginator{data_schema.__name__}"
    return create_model(
        model_name,
        data=(list[data_schema], ...),  # type: ignore[valid-type]
        current_page=(int, ...),
        total_pages=(int, ...),
        batch_size=(int, ...),
        total_count=(int, ...),
    )
