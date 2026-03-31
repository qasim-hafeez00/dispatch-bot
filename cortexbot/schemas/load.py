from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class LoadBase(BaseModel):
    origin: str
    destination: str
    rate: float

class LoadCreate(LoadBase):
    pass

class LoadSchema(LoadBase):
    id: int
    status: str
    carrier_id: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True
