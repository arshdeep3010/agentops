"""What each SDLC phase must return. Model output that doesn't fit is retried with the error."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from engine.workflow import schema
from sdlc import repo


class Story(BaseModel):
    id: str = Field(pattern=r"^US-\d+$")
    as_a: str = Field(min_length=2)
    i_want: str = Field(min_length=3)
    so_that: str = Field(min_length=3)


class Criterion(BaseModel):
    id: str = Field(pattern=r"^AC-\d+$")
    story_id: str = Field(pattern=r"^US-\d+$")
    given: str = Field(min_length=2)
    when: str = Field(min_length=2)
    then: str = Field(min_length=2)


@schema("Requirements")
class Requirements(BaseModel):
    title: str = Field(min_length=5, max_length=72, description="short pull-request title, imperative mood")
    summary: str = Field(min_length=10, max_length=600)
    user_stories: list[Story] = Field(min_length=1, max_length=8)
    acceptance_criteria: list[Criterion] = Field(min_length=1, max_length=20)
    open_questions: list[str] = Field(default_factory=list, max_length=8)
    out_of_scope: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def consistent(self):
        stories = [s.id for s in self.user_stories]
        acs = [a.id for a in self.acceptance_criteria]
        if len(set(stories)) != len(stories) or len(set(acs)) != len(acs):
            raise ValueError("story and criterion ids must be unique")
        for a in self.acceptance_criteria:
            if a.story_id not in stories:
                raise ValueError(f"{a.id} points at unknown story {a.story_id}")
        for s in stories:
            if not any(a.story_id == s for a in self.acceptance_criteria):
                raise ValueError(f"{s} has no acceptance criteria")
        return self


class FilePlan(BaseModel):
    path: str
    change: Literal["create", "modify"]
    purpose: str = Field(min_length=3)

    @field_validator("path")
    @classmethod
    def safe(cls, v: str) -> str:
        return repo.safe_path(v)


class Interface(BaseModel):
    name: str
    signature: str
    description: str


class ADR(BaseModel):
    title: str
    decision: str
    alternatives: list[str] = Field(default_factory=list)
    consequences: str = ""


@schema("Design")
class Design(BaseModel):
    approach: str = Field(min_length=20, max_length=1500)
    files: list[FilePlan] = Field(min_length=1, max_length=10)
    test_files: list[str] = Field(min_length=1, max_length=5)
    interfaces: list[Interface] = Field(default_factory=list, max_length=12)
    risks: list[str] = Field(default_factory=list, max_length=8)
    adr: ADR

    @field_validator("test_files")
    @classmethod
    def tests_folder(cls, v: list[str]) -> list[str]:
        out = []
        for p in v:
            p = repo.safe_path(p)
            if not (repo.is_test_path(p) and p.endswith(".py") and p.split("/")[-1].startswith("test_")):
                raise ValueError(f"test files must be tests/test_*.py, got {p}")
            out.append(p)
        return out

    @model_validator(mode="after")
    def no_tests_or_protected_in_code(self):
        for f in self.files:
            if repo.is_test_path(f.path):
                raise ValueError(f"{f.path}: list test files under test_files, not files")
            if repo.is_protected(f.path):
                raise ValueError(f"{f.path} is a protected file and can't be changed")
        return self


class SourceFile(BaseModel):
    path: str
    content: str = Field(max_length=60_000)

    @field_validator("path")
    @classmethod
    def safe(cls, v: str) -> str:
        return repo.safe_path(v)


class CoverageEntry(BaseModel):
    ac_id: str = Field(pattern=r"^AC-\d+$")
    tests: list[str] = Field(min_length=1)


@schema("TestFiles")
class GeneratedTests(BaseModel):
    files: list[SourceFile] = Field(min_length=1, max_length=5)
    coverage_map: list[CoverageEntry] = Field(min_length=1)


@schema("CodeFiles")
class CodeFiles(BaseModel):
    files: list[SourceFile] = Field(min_length=1, max_length=10)
    notes: str = Field(default="", max_length=1500)


class Comment(BaseModel):
    file: str
    line: int = Field(ge=1)
    severity: Literal["blocking", "suggestion"]
    comment: str = Field(min_length=5, max_length=1500)
    # Required for blocking comments: a concrete input and the wrong result it produces.
    scenario: str = Field(default="", max_length=800)

    @model_validator(mode="after")
    def blocking_needs_evidence(self):
        if self.severity == "blocking" and len(self.scenario.strip()) < 15:
            raise ValueError("a blocking comment needs a scenario: a concrete input and the wrong result it "
                             "gives; otherwise make it a suggestion")
        return self


@schema("Review")
class Review(BaseModel):
    comments: list[Comment] = Field(default_factory=list, max_length=20)
    verdict: Literal["approve", "changes"]
