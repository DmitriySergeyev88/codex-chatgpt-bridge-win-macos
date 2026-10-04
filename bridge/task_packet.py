"""Shared task limits for MCP discovery and durable queue validation."""
from typing import Annotated

from pydantic import BaseModel, Field, ValidationError

TaskTitle = Annotated[str, Field(strict=True, min_length=1, max_length=200)]
TaskInstructions = Annotated[str, Field(strict=True, min_length=1, max_length=262144,
    description="Complete plain-text task specification, 1..262144 Unicode characters. Preserved verbatim; gzip/xz/base64 is not decoded.")]
AcceptanceCriterion = Annotated[str, Field(strict=True, min_length=1, max_length=8192)]
TaskAcceptance = Annotated[list[AcceptanceCriterion], Field(min_length=1, max_length=100,
    description="All acceptance criteria: 1..100 items, each 1..8192 Unicode characters. Do not summarize or group to fit transport limits.")]
TaskKey = Annotated[str, Field(strict=True, min_length=8, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")]


class TaskPacket(BaseModel):
    title: TaskTitle
    instructions: TaskInstructions
    acceptance: TaskAcceptance
    idempotency_key: TaskKey


def validate_task_packet(title, instructions, acceptance, key):
    values=dict(title=title, instructions=instructions, acceptance=acceptance, idempotency_key=key)
    try:
        TaskPacket.model_validate(values)
    except ValidationError as error:
        problems=[]
        for item in error.errors(include_input=False, include_url=False):
            location=item["loc"]
            field=".".join(map(str,location))
            actual=values.get(location[0])
            if len(location)>1 and isinstance(actual,list): actual=actual[location[1]]
            length=f" (actual length {len(actual)})" if isinstance(actual,(str,list)) else ""
            problems.append(f"{field}: {item['msg']}{length}")
        raise ValueError("Invalid task packet: "+"; ".join(problems)) from None
