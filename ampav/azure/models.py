from pydantic import BaseModel, Field, ConfigDict
from typing import Any, Literal
from datetime import datetime
from ampav.core.schema.basemodel import AmpAVBaseModel
from enum import StrEnum

class JobState(StrEnum):
    UPLOADED = "Uploaded"
    PROCESSING = "Processing"
    PROCESSED = "Processed"
    FAILED = "Failed"


class JobStatus(AmpAVBaseModel):
    id: str
    externalId: None | str = Field(None, documentation="User-supplied external ID")
    metadata: None | str = Field(None, description="User-supplied metadata(?)")
    description: None | str = Field(None, description="Job description")
    created: datetime
    lastModified: datetime
    lastIndexed: datetime | None
    processingProgress: str 
    state: JobState
    durationInSeconds: float
    sourceLanguage: str | None
    sourceLanguages: list[str]


class ViRawData(AmpAVBaseModel):
    """This is a really thin wrapper over a full VI data dump"""
    format: Literal['viraw'] = 'viraw'
    data: dict
    thumbnails: dict = Field(default_factory=dict)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    
