from typing import Any

from pydantic import BaseModel


class GoldenQuery(BaseModel):
    id: str
    text: str
    relevant: list[Any]
    mode: str
    namespace: str
