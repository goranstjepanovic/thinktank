from pathlib import Path

from app.agents.orchestrator_agent import (
    _expand_inspection_paths,
    _parse_orchestrator_result,
)


def test_parse_orchestrator_result_prefers_tasks_over_done_flag() -> None:
    user_message, done, next_tasks = _parse_orchestrator_result(
        {
            "analysis": "Looks finished, but here is one remaining task.",
            "done": True,
            "next_tasks": [
                {
                    "id": "add_tailwind_css_imports",
                    "title": "Add Tailwind CSS directives",
                    "instruction": "Edit src/styles/index.css and add the required directives.",
                }
            ],
            "user_message": None,
        }
    )

    assert user_message is None
    assert done is False
    assert len(next_tasks) == 1
    assert next_tasks[0]["id"] == "add_tailwind_css_imports"


def test_parse_orchestrator_result_accepts_legacy_next_task() -> None:
    _, done, next_tasks = _parse_orchestrator_result(
        {
            "done": False,
            "next_task": {
                "id": "single_task",
                "title": "Single task",
                "instruction": "Do the work.",
            },
        }
    )

    assert done is False
    assert len(next_tasks) == 1
    assert next_tasks[0]["id"] == "single_task"


def test_expand_inspection_paths_flattens_directories(tmp_path: Path) -> None:
    (tmp_path / "src" / "components").mkdir(parents=True)
    (tmp_path / "backend" / "routes").mkdir(parents=True)
    (tmp_path / "src" / "App.js").write_text("export default function App() {}", encoding="utf-8")
    (tmp_path / "src" / "components" / "Board.js").write_text("export default function Board() {}", encoding="utf-8")
    (tmp_path / "backend" / "server.js").write_text("console.log('server')", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"name":"demo"}', encoding="utf-8")

    paths = _expand_inspection_paths(
        str(tmp_path),
        ["package.json", "src", "backend"],
        max_files=10,
    )

    assert "package.json" in paths
    assert "src/App.js" in paths
    assert "src/components/Board.js" in paths
    assert "backend/server.js" in paths
