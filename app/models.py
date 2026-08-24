from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class GPUOwner(str, Enum):
    none = "none"
    gemma = "gemma"
    comfyui = "comfyui"
    facefusion = "facefusion"


class SwitchPhase(str, Enum):
    stopped = "STOPPED"
    draining = "DRAINING"
    releasing = "RELEASING"
    starting = "STARTING"
    warming_up = "WARMING_UP"
    ready = "READY"
    failed = "FAILED"


class GPUState(BaseModel):
    owner: GPUOwner = GPUOwner.none
    desired_owner: GPUOwner = GPUOwner.none
    phase: SwitchPhase = SwitchPhase.stopped
    message: str = "尚未激活 GPU 工作区"
    error: str | None = None
    revision: int = 0
    active_tasks: dict[str, int] = Field(
        default_factory=lambda: {"gemma": 0, "comfyui": 0, "facefusion": 0}
    )
    memory_used_mb: int | None = None
    memory_free_mb: int | None = None
    memory_total_mb: int | None = None


class TaskStatus(str, Enum):
    queued = "queued"
    switching_gpu = "switching_gpu"
    running = "running"
    completed = "completed"
    failed = "failed"


class TaskRecord(BaseModel):
    task_id: str
    module: str
    operation: str
    title: str
    status: TaskStatus = TaskStatus.queued
    progress: int = 0
    message: str = "等待执行"
    input_files: list[str] = Field(default_factory=list)
    output_files: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    logs: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: str
    updated_at: str


class PromptRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200000)
    mode: str = "optimize"
    width: int = Field(default=1024, ge=256, le=2048)
    height: int = Field(default=1024, ge=256, le=2048)


class PromptResponse(BaseModel):
    positive_prompt: str
    negative_prompt: str
    notes: str = ""


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=200000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=80)
    system_prompt: str = Field(default="", max_length=50000)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, ge=32, le=8192)


class ChatResponse(BaseModel):
    content: str
    model: str
    multimodal: bool = False
