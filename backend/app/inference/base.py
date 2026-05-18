from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolCall:
    """A single tool invocation requested by the model."""
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolDefinition:
    """Description of a tool exposed to the model."""
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema object


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str = ""
    # Present on assistant messages when the model called a tool instead of responding directly
    tool_calls: list[ToolCall] | None = None


@dataclass
class InferenceRequest:
    model: str
    messages: list[Message]
    format: str = "json"
    temperature: float = 0.2
    max_tokens: int | None = None
    num_ctx: int = 8192          # Ollama context window; default 8k (Ollama's own default is 2048)
    tools: list[ToolDefinition] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int | None = None  # overrides backend default when set
    on_token: Callable[[str], None] | None = None  # optional token-level streaming callback
    think: bool = False  # enable extended thinking (Ollama: adds think=True to payload)


@dataclass
class InferenceResponse:
    content: str  # raw response string — valid JSON when no tool_calls
    model: str
    tokens_prompt: int | None
    tokens_completion: int | None
    duration_ms: int | None
    raw_response: dict[str, Any]
    tool_calls: list[ToolCall] = field(default_factory=list)


class InferenceBackendError(Exception):
    pass


class InferenceBackend(ABC):
    """
    Stateless transport adapter contract.
    No logging, retry, or prompt building — those live in InferenceClient.
    """

    @abstractmethod
    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        """Submit a completion request. Raises InferenceBackendError on failure."""
        ...

    async def stream_complete(self, request: InferenceRequest):
        """
        Stream completion chunks as an async generator.
        Default: yields the full response as a single chunk.
        Override in drivers that support native streaming.
        """
        response = await self.complete(request)
        yield response.content

    @abstractmethod
    async def health_check(self) -> bool:
        """Returns True if the backend is reachable and ready."""
        ...

    @abstractmethod
    async def list_available_models(self) -> list[str]:
        """Returns model names currently available in this backend."""
        ...
