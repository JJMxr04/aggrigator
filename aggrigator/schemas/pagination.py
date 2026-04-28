"""Generic page envelope used by every paginated read endpoint."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class PageParams(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class Page(BaseModel, Generic[T]):
    items: list[T]
    page: int
    page_size: int
    total: int
    pages: int

    @classmethod
    def build(cls, *, items: list[T], page: int, page_size: int, total: int) -> "Page[T]":
        return cls(
            items=items,
            page=page,
            page_size=page_size,
            total=total,
            pages=max(1, -(-total // page_size)),  # ceil
        )
