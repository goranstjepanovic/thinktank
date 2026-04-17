from app.pipeline.context import BranchContext
from app.pipeline.stages.base_stage import BaseStage, StageOutput

# ---------------------------------------------------------------------------
# Few-shot examples — keep the model anchored to the exact output schema
# and prevent it from omitting fields or returning prose instead of JSON.
# ---------------------------------------------------------------------------
_FEW_SHOT_EXAMPLES = """
Example 1
Input:
  Idea: Local voice assistant
  Description: A voice assistant that runs fully offline on a Raspberry Pi 5.
  Requirements: Wake-word detection, speech-to-text, LLM response, text-to-speech. All offline.
  Constraints: Must run on Raspberry Pi 5 (8 GB RAM). No cloud APIs. Latency under 3 seconds.

Output:
{
  "parsed_name": "Local Voice Assistant",
  "parsed_description": "An offline voice assistant running entirely on a Raspberry Pi 5, covering wake-word detection through text-to-speech without any cloud dependency.",
  "goals": [
    "Provide a fully offline conversational voice interface",
    "Keep end-to-end response latency under 3 seconds"
  ],
  "requirements": [
    "Wake-word detection",
    "Speech-to-text transcription",
    "LLM-based response generation",
    "Text-to-speech synthesis"
  ],
  "constraints": [
    "Must run on Raspberry Pi 5 with 8 GB RAM",
    "No cloud APIs or network calls permitted",
    "End-to-end latency must stay below 3 seconds"
  ],
  "domain": "hardware",
  "complexity_estimate": "high",
  "notes": "Latency constraint is tight given available RAM; model quantization and component pipeline overlap will be critical."
}

Example 2
Input:
  Idea: Budget tracker CLI
  Description: A command-line tool to track personal expenses and produce monthly summaries.
  Requirements: Add expenses with category and date, list by month, export to CSV.
  Constraints: Single Python file. No external dependencies beyond stdlib.

Output:
{
  "parsed_name": "Budget Tracker CLI",
  "parsed_description": "A single-file Python command-line tool for logging personal expenses and generating monthly summaries, with CSV export, using only the standard library.",
  "goals": [
    "Allow users to record expenses with category and date from the terminal",
    "Summarise spending by month",
    "Export data to CSV"
  ],
  "requirements": [
    "Add expense entries with category and date",
    "List expenses filtered by month",
    "Export to CSV"
  ],
  "constraints": [
    "Implemented as a single Python file",
    "No third-party dependencies — stdlib only"
  ],
  "domain": "software",
  "complexity_estimate": "low",
  "notes": "sqlite3 and csv modules from stdlib are sufficient. argparse handles the CLI."
}
"""


class IntakeStage(BaseStage):
    stage_key = "intake"
    stage_index = 0
    stage_name = "intake"

    async def run(self, ctx: BranchContext, session) -> StageOutput:
        system_prompt = (
            "You are a requirements analyst. Parse the provided idea into a structured JSON object. "
            "Return JSON with exactly these keys:\n"
            "  parsed_name        — cleaned-up idea name (string)\n"
            "  parsed_description — one-paragraph description (string)\n"
            "  goals              — list of strings, one per distinct goal\n"
            "  requirements       — list of strings, one per functional requirement\n"
            "  constraints        — list of strings, one per hard constraint\n"
            "  domain             — one of: software | hardware | mixed | other\n"
            "  complexity_estimate — one of: low | medium | high\n"
            "  notes              — any ambiguities or important observations (string, may be empty)\n\n"
            "Rules:\n"
            "- Every key must be present. Use an empty list [] if a field has no entries.\n"
            "- Do not add extra keys.\n"
            "- Be specific: extract only what is stated; do not invent requirements.\n\n"
            f"Examples:{_FEW_SHOT_EXAMPLES}"
        )
        user_prompt = self._base_context_str(ctx)

        try:
            output = await self._call(self._build_messages(system_prompt, user_prompt), session, ctx)
            return StageOutput(stage_name=self.stage_name, output=output)
        except Exception as e:
            return StageOutput(stage_name=self.stage_name, output={}, failed=True, failure_reason=str(e))
