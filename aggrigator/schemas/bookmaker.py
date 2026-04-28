from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class BookmakerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    active: bool
