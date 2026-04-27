"""
Deterministic interface manifest extractor for Phase 3 output directories.

Scans all source files after each task batch and writes docs/INTERFACE.json.
This gives the orchestrator and sub-agents an accurate, always-current picture
of what is actually exported by each module — not what was planned.

No LLM involved. Pure regex + AST-free text scanning.
"""

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_SOURCE_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".py", ".svelte", ".vue"}
_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", "coverage", ".pytest_cache",
}

MANIFEST_PATH = "docs/INTERFACE.json"


# ---------------------------------------------------------------------------
# JavaScript / TypeScript / JSX / TSX
# ---------------------------------------------------------------------------

def _js_exports(content: str) -> dict:
    named: list[str] = []
    default_export: str | None = None
    props: list[str] = []

    # export function/class/const/let/var/async function Name
    for m in re.finditer(
        r"export\s+(?:async\s+)?(?:function\s*\*?\s*|class\s+|const\s+|let\s+|var\s+)(\w+)",
        content,
    ):
        named.append(m.group(1))

    # export { foo, bar as baz } — keep the exported (alias) name
    for m in re.finditer(r"export\s+\{([^}]+)\}", content):
        for item in m.group(1).split(","):
            parts = item.strip().split()
            if parts:
                named.append(parts[-1])

    # export default function/class Name  OR  export default Name
    m = re.search(
        r"export\s+default\s+(?:async\s+)?(?:function\s*\*?\s*|class\s+)?(\w+)",
        content,
    )
    if m:
        default_export = m.group(1)
    elif re.search(r"export\s+default\s+", content):
        default_export = "(anonymous)"

    # React props from destructured function params: ({ prop1, prop2 }) or ({ prop1, prop2 }: T)
    for m in re.finditer(
        r"(?:function|const|let|var)\s+\w+\s*(?::\s*\w+)?\s*=?\s*\(\s*\{([^}]+)\}",
        content,
    ):
        raw = m.group(1)
        for pm in re.finditer(r"\b([a-zA-Z_]\w*)\b(?:\s*[?:])?", raw):
            name = pm.group(1)
            if name not in {"true", "false", "null", "undefined", "typeof"}:
                props.append(name)

    # TypeScript interface/type *Props* { prop?: Type }
    for m in re.finditer(
        r"(?:interface|type)\s+\w*[Pp]rops\w*\s*(?:extends[^{]*)?\{([^}]+)\}",
        content,
    ):
        for line in m.group(1).splitlines():
            pm = re.match(r"\s*([a-zA-Z_]\w*)\??:", line)
            if pm:
                props.append(pm.group(1))

    return {
        "named": sorted(set(named)),
        "default": default_export,
        "props": sorted(set(props)) or None,
    }


def _js_imports(content: str) -> list[dict]:
    results: list[dict] = []
    # import { X, Y } from '...'  /  import Default from '...'  /  import * as NS from '...'
    for m in re.finditer(
        r"import\s+"
        r"(?:\*\s+as\s+(\w+)|\{([^}]+)\}|(\w+))?"
        r"(?:\s*,\s*\{([^}]+)\})?"
        r"\s+from\s+['\"]([^'\"]+)['\"]",
        content,
    ):
        namespace = m.group(1)
        named_raw = (m.group(2) or "") + "," + (m.group(4) or "")
        default_ = m.group(3)
        source = m.group(5)
        named = [
            p.strip().split()[-1]
            for p in named_raw.split(",")
            if p.strip() and p.strip() != ","
        ]
        results.append(
            {"source": source, "named": named, "default": default_, "namespace": namespace}
        )
    return results


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

def _py_exports(content: str) -> dict:
    named: list[str] = []

    # Honour __all__ if present
    all_m = re.search(r"^__all__\s*=\s*\[([^\]]+)\]", content, re.MULTILINE)
    if all_m:
        for item in re.finditer(r"['\"](\w+)['\"]", all_m.group(1)):
            named.append(item.group(1))
        return {"named": sorted(set(named)), "default": None, "props": None}

    # Top-level public defs and classes (not indented, not private)
    for m in re.finditer(
        r"^(?:async\s+)?def\s+([a-zA-Z][a-zA-Z0-9_]*)|^class\s+([a-zA-Z][a-zA-Z0-9_]*)",
        content,
        re.MULTILINE,
    ):
        name = m.group(1) or m.group(2)
        if not name.startswith("_"):
            named.append(name)

    return {"named": sorted(set(named)), "default": None, "props": None}


def _py_imports(content: str) -> list[dict]:
    results: list[dict] = []
    for m in re.finditer(
        r"^from\s+([\w.]+)\s+import\s+(.+)$", content, re.MULTILINE
    ):
        source = m.group(1)
        raw = m.group(2).strip().strip("()")
        named = [n.strip().rstrip(",").split()[0] for n in raw.split(",") if n.strip()]
        results.append({"source": source, "named": named, "default": None, "namespace": None})
    for m in re.finditer(
        r"^import\s+([\w.]+)(?:\s+as\s+(\w+))?$", content, re.MULTILINE
    ):
        results.append(
            {"source": m.group(1), "named": [], "default": m.group(1), "namespace": m.group(2)}
        )
    return results


# ---------------------------------------------------------------------------
# Main extraction + manifest writing
# ---------------------------------------------------------------------------

def extract_interface(output_dir: str) -> dict:
    """
    Scan all source files in output_dir and return a structured manifest.

    Shape::

        {
            "updated_at": "<ISO timestamp>",
            "files": {
                "src/hooks/useGameLogic.js": {
                    "exports": {"named": ["useGameLogic"], "default": null, "props": null},
                    "imports": [{"source": "../assets/cards", "named": ["getCardValues"], ...}]
                },
                ...
            },
            "imported_by": {
                "src/hooks/useGameLogic.js": ["src/App.jsx"]
            }
        }
    """
    root = Path(output_dir)
    files_data: dict[str, dict] = {}

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in _SOURCE_EXTENSIONS:
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue

        rel = path.relative_to(root).as_posix()
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        if path.suffix in {".js", ".jsx", ".ts", ".tsx", ".svelte", ".vue"}:
            exports = _js_exports(content)
            imports = _js_imports(content)
        elif path.suffix == ".py":
            exports = _py_exports(content)
            imports = _py_imports(content)
        else:
            continue

        files_data[rel] = {"exports": exports, "imports": imports}

    # Build reverse "imported_by" map for relative imports
    imported_by: dict[str, list[str]] = {}
    for caller_rel, data in files_data.items():
        for imp in data.get("imports", []):
            source = imp.get("source", "")
            if not source.startswith("."):
                continue
            caller_dir = Path(caller_rel).parent
            resolved = (caller_dir / source).as_posix()
            # Try appending common extensions when none present
            if "." not in Path(resolved).name:
                for ext in (".js", ".jsx", ".ts", ".tsx", ".py"):
                    if resolved + ext in files_data:
                        resolved = resolved + ext
                        break
                # Also try /index variants
                if resolved not in files_data:
                    for ext in (".js", ".jsx", ".ts", ".tsx"):
                        candidate = resolved + "/index" + ext
                        if candidate in files_data:
                            resolved = candidate
                            break
            if resolved in files_data:
                imported_by.setdefault(resolved, [])
                if caller_rel not in imported_by[resolved]:
                    imported_by[resolved].append(caller_rel)

    return {
        "updated_at": datetime.now(UTC).isoformat(),
        "files": files_data,
        "imported_by": imported_by,
    }


def write_interface_manifest(output_dir: str) -> str | None:
    """
    Extract the manifest and write it to docs/INTERFACE.json.
    Returns the relative path on success, None on failure.
    """
    try:
        manifest = extract_interface(output_dir)
        out_path = Path(output_dir) / MANIFEST_PATH
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        logger.info(
            "interface_extractor: wrote manifest for %d files → %s",
            len(manifest["files"]),
            out_path,
        )
        return MANIFEST_PATH
    except Exception as exc:
        logger.warning("interface_extractor: failed: %s", exc)
        return None


def format_manifest_summary(manifest: dict, max_files: int = 40) -> str:
    """
    Compact text summary for inclusion in orchestrator context.

    Each line: ``  path: exports [X, Y] | default: Z  ← imported by A, B``
    Flags files that export nothing (likely stubs).
    """
    files = manifest.get("files", {})
    imported_by = manifest.get("imported_by", {})
    lines = [f"## Live Module Interface ({len(files)} source files)\n"]

    for rel, data in sorted(files.items())[:max_files]:
        exp = data.get("exports", {})
        named = exp.get("named") or []
        default_ = exp.get("default")
        props = exp.get("props")

        parts: list[str] = []
        if default_:
            parts.append(f"default:{default_}")
        if named:
            parts.append("{" + ", ".join(named) + "}")
        export_str = ", ".join(parts) if parts else "⚠ NO EXPORTS"

        callers = imported_by.get(rel, [])
        caller_str = f"  ← {', '.join(callers[:3])}" if callers else ""
        if len(callers) > 3:
            caller_str += f" (+{len(callers) - 3})"

        lines.append(f"  {rel}: [{export_str}]{caller_str}")
        if props:
            lines.append(f"    props: {{{', '.join(props)}}}")

    if len(files) > max_files:
        lines.append(f"  … {len(files) - max_files} more files omitted")

    return "\n".join(lines)
