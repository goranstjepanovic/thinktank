"""
Context reduction utilities — keep model inputs lean without losing meaning.

Two responsibilities:
  reduce_prd   — extract only the PRD sections relevant to a task instruction
  prune_stale_reads — replace read_file results with stubs once the same file
                      has been successfully rewritten, freeing context for the
                      remaining rounds of a long tool-calling loop
"""
import json
import logging
import re

logger = logging.getLogger(__name__)

_STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "to", "in", "for", "with",
    "on", "at", "from", "by", "is", "are", "that", "this", "it", "as",
    "be", "will", "your", "we", "you", "they", "all", "new", "not",
    "any", "each", "per", "its", "their", "our", "can", "should",
    "must", "use", "used", "using", "make", "create", "add", "into",
    "also", "which", "where", "when", "how", "what", "who",
    "then", "than", "but", "have", "has", "file", "files", "code",
})

# Only reduce PRDs longer than this — shorter ones are cheap enough to pass whole
_MIN_CHARS_TO_REDUCE = 2000


def reduce_prd(prd: str, task: str) -> str:
    """Return the PRD sections most relevant to task, plus the intro.

    Splits by markdown headings (up to ####), scores each section by keyword
    overlap with the task instruction, and keeps only matching sections.
    Falls back to the top-3 sections when nothing scores.
    Short PRDs (< _MIN_CHARS_TO_REDUCE) are returned unchanged.
    """
    if not prd or len(prd) < _MIN_CHARS_TO_REDUCE:
        return prd

    # Split into (heading | None, body_lines) segments
    segments: list[tuple[str | None, list[str]]] = []
    current_heading: str | None = None
    current_body: list[str] = []

    for line in prd.splitlines():
        if re.match(r"^#{1,4}\s", line):
            segments.append((current_heading, current_body))
            current_heading = line
            current_body = []
        else:
            current_body.append(line)
    segments.append((current_heading, current_body))

    # First segment (heading=None) is the intro — always kept
    intro_body = segments[0][1] if segments and segments[0][0] is None else []
    heading_segs = [(h, b) for h, b in segments if h is not None]

    if not heading_segs:
        return prd  # flat document, nothing to trim

    # Score sections by keyword overlap with the task instruction
    task_words = frozenset(re.findall(r"\b[a-z]{3,}\b", task.lower())) - _STOP_WORDS

    scored: list[tuple[int, str, list[str]]] = []
    for heading, body in heading_segs:
        text = (heading + " " + " ".join(body)).lower()
        score = sum(1 for w in task_words if w in text)
        scored.append((score, heading, body))

    scored.sort(key=lambda x: -x[0])
    relevant = [(h, b) for s, h, b in scored if s > 0]
    if not relevant:
        relevant = [(h, b) for _, h, b in scored[:3]]

    parts: list[str] = []
    intro_text = "\n".join(intro_body).strip()
    if intro_text:
        parts.append(intro_text)
    for heading, body in relevant:
        section = (heading + "\n" + "\n".join(body)).strip()
        if section:
            parts.append(section)

    result = "\n\n".join(parts)

    if len(result) < len(prd) * 0.85:
        logger.info(
            "context_reducer: PRD %d→%d chars (%.0f%%) for task: %.80s",
            len(prd), len(result), len(result) / len(prd) * 100, task,
        )

    return result


def prune_stale_reads(messages: list) -> list:
    """Stub out read_file results whose path was subsequently written successfully.

    Walks working_messages positionally to pair each assistant tool call with
    its tool result. Any read_file result whose path was later rewritten is
    replaced with a small stub, freeing those tokens for the model's remaining
    context window.

    Returns a new list — does not mutate the input.
    """
    _READ_TOOLS = {"read_file"}
    _WRITE_TOOLS = {"file_edit"}

    # Pass 1: pair (tool_name, path, result_msg_idx) and collect written paths.
    # Invariant: after an assistant turn with n tool_calls, the next n messages
    # are the corresponding tool results (same order).
    pairs: list[tuple[str, str, int]] = []  # (tool_name, path, result_idx)
    written_paths: set[str] = set()

    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.role == "assistant" and msg.tool_calls:
            for j, tc in enumerate(msg.tool_calls):
                result_idx = i + 1 + j
                if result_idx >= len(messages) or messages[result_idx].role != "tool":
                    continue
                path = (tc.arguments.get("path") or "").strip()
                pairs.append((tc.name, path, result_idx))
                if tc.name in _WRITE_TOOLS and path:
                    try:
                        res = json.loads(messages[result_idx].content)
                        if res.get("success"):
                            written_paths.add(path)
                    except (json.JSONDecodeError, AttributeError):
                        pass
            i += 1 + len(msg.tool_calls)
        else:
            i += 1

    if not written_paths:
        return messages

    # Pass 2: find stale read indices
    stale: set[int] = {
        result_idx
        for tool_name, path, result_idx in pairs
        if tool_name in _READ_TOOLS and path in written_paths
    }

    if not stale:
        return messages

    logger.info(
        "context_reducer: pruning %d stale read result(s) — paths: %s",
        len(stale),
        ", ".join(sorted(written_paths)),
    )

    # Pass 3: rebuild with stubs
    from app.inference.base import Message

    result_msgs = []
    for idx, msg in enumerate(messages):
        if idx in stale:
            try:
                orig = json.loads(msg.content)
                path = orig.get("path", "?")
                total_lines = orig.get("total_lines")
            except (json.JSONDecodeError, AttributeError):
                path, total_lines = "?", None
            stub: dict = {
                "pruned": True,
                "path": path,
                "reason": "file was read then rewritten — no longer needed in context",
            }
            if total_lines is not None:
                stub["total_lines"] = total_lines
            result_msgs.append(Message(role="tool", content=json.dumps(stub)))
        else:
            result_msgs.append(msg)

    return result_msgs
