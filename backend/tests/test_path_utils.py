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
