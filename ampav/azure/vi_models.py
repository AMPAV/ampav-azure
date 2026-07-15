from pydantic import BaseModel, Field, ConfigDict
from typing import Any
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


class Instance(AmpAVBaseModel):
    start: str
    end: str
    confidence: float | None = None
    instanceSource: str | None = None
    brandType: str | None = None
    thumbnailId: str | None = None


class Block(AmpAVBaseModel):
    id: int
    instances: list[Instance]


class Insights(AmpAVBaseModel):

    class AudioEffect(Block):
        type: str

    audioEffects: list["Insights.AudioEffect"]
    blocks: list[Block]
    
    class Brand(Block):
        confidence: float
        description: str
        isCustom: bool
        name: str
        referenceId: str
        referenceType: str | None
        referenceUrl: str
        tags: list[str]

    brands: list["Insights.Brand"]

    class DetectedObject(Block):
        displayName: str
        thumbnailId: str
        type: str
        wikiDataId: str

    detectedObjects: list["Insights.DetectedObject"]
    duration: str
    
    class FramePattern(Block):
        confidence: float
        displayName: str | None
        patternType: str
    
    framePatterns: list["Insights.FramePattern"]

    class Keyword(Block):
        confidence: float
        language: str
        text: str
    
    keywords: list["Insights.Keyword"]

    class Label(Block):
        language: str
        name: str
        referenceId: str | None = None

    labels: list["Insights.Label"]
    language: str
    languages: list[str]

    class NamedLocation(Block):
        confidence: float
        description: str | None
        isCustom: bool
        name: str
        referenceId: str | None = None
        refernceUrl: str | None = None
        tags: list[str]

    namedLocations: list["Insights.NamedLocation"]

    class Ocr(Block):
        angle: float
        confidence: float
        height: int
        language: str
        left: int
        text: str
        top: int
        width: int

    ocr: list["Insights.Ocr"]
    ocrAnalyzedTokenCount: int
    ocrMaxTokenCount: int
    scenes: list[Block]

    class Sentiment(Block):
        averageScore: float
        sentimentType: str

    sentiments: list["Insights.Sentiment"]

    class Shot(Block):
        keyFrames: list[Block]
        tags: list[str] | None = None

    shots: list[Shot]
    sourceLanguage: str
    sourceLanguages: list[str]

    class Speaker(Block):
        name: str

    speakers: list["Insights.Speaker"]
    
    class Statistics(AmpAVBaseModel):
        correspondenceCount: int
        speakerLongestMonolog: dict[int, int]
        speakerNumberOfFragments: dict[int, int]
        speakerTalkToListenRatio: dict[int, float]
        speakerWordCount: dict[int, int]
    
    statistics: "Insights.Statistics"

    class TextualContentModeration(Block):
        bannedWordsCount: int
        bannedWordsRatio: float

    textualContentModeration: "Insights.TextualContentModeration"

    class Topic(Block):
        confidence: float
        iptcName: str
        language: str
        referenceId: str | None
        referenceUrl: str | None = None
        referenceType: str | None

    topics: list[Topic]

    class Transcript(Block):
        confidence: float
        language: str
        speakerId: int
        text: str

    transcript: list[Transcript]
    version: str
    

class Video(AmpAVBaseModel):
    detectSourceLanguage: bool
    excludedAIs: list[str]
    externalId: str | None
    externalUrl: str | None
    failureMessage: str
    height: int
    id: str
    indexingPreset: str
    insights: Insights
    isAdult: bool
    isSearchable: bool
    language: str
    languageAutoDetectMode: str | None
    languages: list[str]
    linguisticModelId: str
    logoGroupId: str | None
    metadata: str | None
    moderationState: str
    personModelId: str
    privacyMode: str
    processingProgress: str
    publishedProxyUrl: str | None
    publishedUrl: str | None
    reviewState: str
    sourceLanguage: str
    sourceLanguages: list[str]
    state: str
    streamingPreset: str
    thumbnailId: str    
    width: int


class VideoIndexer(AmpAVBaseModel):
    created: datetime
    description: str    
    durationInSeconds: float
    id: str
    name: str
    partition: str | None
    privacyMode: str
    state: str    
    summarizedInsights: Any
    videos: list[Video]
    videosRanges: list[Any]

    

class OCR(AmpAVBaseModel):
    Fps: float

    class FrameResult(AmpAVBaseModel):
        apiVersion: str
        content: str
        #languages

    class OcrResult(AmpAVBaseModel):
        FrameIndex: int
        Ocr: "OCR.FrameResult"

    Results: list["OCR.OcrResult"]




class RawVideoIndexer(AmpAVBaseModel):
    data: VideoIndexer
    ocr: dict | None = None
    faces: dict | None = None

