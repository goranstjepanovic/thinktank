import asyncio
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class BranchContext:
    idea_id: str
    branch_id: str
    branch_index: int

    # Raw idea fields — set by orchestrator before runner starts
    idea_name: str = ""
    idea_description: str = ""
    idea_requirements: str = ""
    idea_constraints: str = ""

    # Accumulated stage outputs — keyed by stage name
    stage_outputs: dict[str, dict] = field(default_factory=dict)

    # Failure context inherited from parent branch (if this branch was spawned from a failure)
    inherited_context: dict = field(default_factory=dict)

    # Set by runner before each stage.run() so model calls link to the correct StageResult row
    current_stage_result_id: str | None = None

    # Pause/cancel control
    pause_event: asyncio.Event = field(default_factory=lambda: asyncio.Event())
    cancel_event: asyncio.Event = field(default_factory=lambda: asyncio.Event())

    def __post_init__(self):
        # Start unpaused (event set = not paused)
        self.pause_event.set()
