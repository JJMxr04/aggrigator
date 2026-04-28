from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    short_name: str
    active: bool
