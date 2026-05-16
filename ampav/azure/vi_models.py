from pydantic import BaseModel, Field
from typing import Any
from datetime import datetime

class JobStatus(BaseModel):
    id: str
    externalId: None | str = Field(None, documentation="User-supplied external ID")
    metadata: None | str = Field(None, description="User-supplied metadata(?)")
    description: None | str = Field(None, description="Job description")
    created: datetime
    lastModified: datetime
    lastIndexed: datetime | None
    processingProgress: str 
    state: str
    durationInSeconds: float
    sourceLanguage: str | None
    sourceLanguages: list[str]

