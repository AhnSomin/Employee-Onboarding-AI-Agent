"""requirements-batch.txt covers every third-party import the reminder batch loads.

GitHub Actions installs only that file, so a missing package fails the batch at import time
(e.g. roster.py → yaml). Imports under `try` or `if TYPE_CHECKING:` are optional and not counted
(config.py imports streamlit that way on purpose).
"""

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

from onboarding_agent.config import REPO_ROOT

# requirements-batch.txt package name → top-level import name
IMPORT_NAMES = {
    "pydantic": "pydantic",
    "python-dotenv": "dotenv",
    "gspread": "gspread",
    "google-auth": "google",
    "slack-sdk": "slack_sdk",
    "pyyaml": "yaml",
}
FIRST_PARTY = {"onboarding_agent", "__future__"}


def loaded_engine_files() -> list[Path]:
    code = (
        "import json, sys; "
        f"sys.path[:0] = [{str(REPO_ROOT / 'src')!r}, {str(REPO_ROOT / 'scripts')!r}]; "
        "import run_reminders, onboarding_agent.store.sheets_store; "
        "print(json.dumps(sorted(m.__file__ for n, m in list(sys.modules.items()) "
        "if (n == 'onboarding_agent' or n.startswith('onboarding_agent.')) and getattr(m, '__file__', None))))"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    return [Path(p) for p in json.loads(result.stdout)]


def is_type_checking(node: ast.If) -> bool:
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def required_imports(tree: ast.AST) -> set[str]:
    """Top-level names of absolute imports, skipping anything under `try` or `if TYPE_CHECKING:`."""
    found: set[str] = set()

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.Try | ast.TryStar):
            return
        if isinstance(node, ast.If) and is_type_checking(node):
            for child in node.orelse:
                visit(child)
            return
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def batch_requirements() -> set[str]:
    names = set()
    for line in (REPO_ROOT / "requirements-batch.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            names.add(re.split(r"[\s\[<>=!~;]", line, maxsplit=1)[0].lower().replace("_", "-"))
    return names


def test_batch_requirements_cover_third_party_imports():
    packages = batch_requirements()
    unmapped = packages - IMPORT_NAMES.keys()
    assert not unmapped, f"IMPORT_NAMES에 import 이름을 추가하세요: {sorted(unmapped)}"
    installed = {IMPORT_NAMES[p] for p in packages}

    files = [*loaded_engine_files(), REPO_ROOT / "scripts" / "run_reminders.py"]
    assert any(f.name == "sheets_store.py" for f in files)
    missing: dict[str, list[str]] = {}
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name in required_imports(tree) - set(sys.stdlib_module_names) - FIRST_PARTY - installed:
            missing.setdefault(name, []).append(path.relative_to(REPO_ROOT).as_posix())
    assert not missing, f"requirements-batch.txt에 없는 패키지: {missing}"
