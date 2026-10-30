"""문항 JSON 스키마 (seed / 일괄 업로드 / LLM 초안 공통 검증)."""
from pydantic import BaseModel, Field, field_validator, model_validator

from app.models import Architecture, Difficulty, ItemType

DOMAIN_CODES = {"ARCH", "INSTALL", "EXTRACT", "REPLICAT", "MAPPING", "OPS", "TROUBLE"}
OPTION_KEYS = ("A", "B", "C", "D", "E")


class ItemIn(BaseModel):
    domain: str
    difficulty: Difficulty
    item_type: ItemType
    ogg_version: str = Field(default="19c", max_length=20)
    architecture: Architecture = Architecture.BOTH
    stem: str = Field(max_length=4000)
    options: dict[str, str]
    correct_keys: list[str]
    explanation: str = Field(max_length=4000)
    reference: str | None = Field(default=None, max_length=300)
    variant_group: str = Field(max_length=100)

    @field_validator("domain")
    @classmethod
    def _domain(cls, v: str) -> str:
        if v not in DOMAIN_CODES:
            raise ValueError(f"알 수 없는 영역: {v}")
        return v

    @field_validator("stem", "explanation", "variant_group")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("빈 값은 허용되지 않습니다")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> "ItemIn":
        keys = list(self.options)
        if not 2 <= len(keys) <= len(OPTION_KEYS) or any(k not in OPTION_KEYS for k in keys):
            raise ValueError("options 키는 A~E 중 2~5개여야 합니다")
        if any(not t.strip() for t in self.options.values()):
            raise ValueError("빈 보기가 있습니다")
        if any(len(t) > 500 for t in self.options.values()):
            raise ValueError("보기는 500자를 넘을 수 없습니다")
        if len(set(self.options.values())) != len(keys):
            raise ValueError("중복된 보기가 있습니다")
        if not self.correct_keys or len(set(self.correct_keys)) != len(self.correct_keys):
            raise ValueError("correct_keys 가 비었거나 중복입니다")
        if not set(self.correct_keys) <= set(keys):
            raise ValueError("correct_keys 가 options 에 없는 키를 포함합니다")
        if self.item_type != ItemType.MULTI and len(self.correct_keys) != 1:
            raise ValueError("SINGLE/SCENARIO 는 정답이 정확히 1개여야 합니다")
        if self.item_type == ItemType.MULTI and len(self.correct_keys) < 2:
            raise ValueError("MULTI 는 정답이 2개 이상이어야 합니다")
        return self
