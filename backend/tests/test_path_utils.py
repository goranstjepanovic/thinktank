from app.tools.path_utils import normalize_project_relative_path


def test_strips_duplicate_project_root_segment() -> None:
    base = r"C:\out\idea\modern-snake-game"

    assert (
        normalize_project_relative_path(base, "modern-snake-game/src/App.tsx")
        == "src/App.tsx"
    )


def test_preserves_already_relative_path() -> None:
    base = r"C:\out\idea\modern-snake-game"

    assert normalize_project_relative_path(base, "src/App.tsx") == "src/App.tsx"


def test_handles_slashes_dot_prefix_and_case() -> None:
    base = r"C:\out\idea\modern-snake-game"

    assert (
        normalize_project_relative_path(base, "./Modern-Snake-Game/frontend/package.json")
        == "frontend/package.json"
    )


def test_strips_human_name_with_spaces() -> None:
    # idea.name = "Modern Snake Game" → project_root = "modern-snake-game"
    # sub-agent uses the human-readable name as a prefix
    base = r"C:\out\idea\modern-snake-game"

    assert (
        normalize_project_relative_path(base, "Modern Snake Game/src/App.tsx")
        == "src/App.tsx"
    )


def test_strips_name_with_underscores() -> None:
    base = r"C:\out\idea\my-project"

    assert (
        normalize_project_relative_path(base, "my_project/src/main.py")
        == "src/main.py"
    )
