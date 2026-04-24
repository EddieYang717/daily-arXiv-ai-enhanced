from typing import List

from pydantic import BaseModel, Field, field_validator

class Structure(BaseModel):
    tldr: str = Field(description="generate a too long; didn't read summary")
    motivation: str = Field(description="describe the motivation in this paper")
    method: str = Field(description="method of this paper")
    result: str = Field(description="result of this paper")
    conclusion: str = Field(description="conclusion of this paper")
    relevance_score: int = Field(
        default=0,
        ge=0,
        le=5,
        description="integer score from 0 to 5 measuring how well the paper matches the research profile",
    )
    relevance_reason: str = Field(
        default="",
        description="brief reason explaining the relevance score against the research profile",
    )
    relevance_topics: List[str] = Field(
        default_factory=list,
        description="short matched topics from the research profile",
    )

    @field_validator("relevance_score", mode="before")
    @classmethod
    def clamp_relevance_score(cls, value):
        try:
            score = int(value)
        except (TypeError, ValueError):
            return 0
        return min(5, max(0, score))

    @field_validator("relevance_topics", mode="before")
    @classmethod
    def normalize_relevance_topics(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return value
