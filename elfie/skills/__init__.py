"""
Elfie's learned skills — the self-extension system.

Each skill is one self-contained Python file in this folder exposing
`TOOLS = [...]` (async functions decorated with @function_tool). The agent
loads them fresh at every session start, so a newly learned skill works on
the next connect — no restart.

Skills get here through the learn_skill flow:
  voice request -> Claude Code researches + writes the file into staging
  -> validate_skill() gate (syntax, banned operations, isolated import)
  -> install_skill() copies it here and git-commits it (one-command rollback)

A broken skill file is skipped with a warning — it can never take Elfie down.
"""
import ast
import logging
import re
import shutil
import subprocess
import sys
from importlib import util as importlib_util
from pathlib import Path

logger = logging.getLogger("elfie.skills")

SKILLS_DIR = Path(__file__).parent
REPO_DIR = SKILLS_DIR.parent.parent

# Operations a skill may never contain — keeps learned code low-risk.
# Skills wrap APIs over httpx; anything below has no business in one.
BANNED_CALLS = {
    "eval", "exec", "compile", "__import__",
    "os.system", "os.popen", "os.remove", "os.rmdir", "os.unlink",
    "shutil.rmtree", "shutil.move",
}
BANNED_IMPORTS = {"subprocess", "ctypes", "socket", "pty", "pickle", "marshal"}


def load_skills() -> list:
    """Import every skill module and collect its TOOLS. Broken files skip."""
    tools = []
    for path in sorted(SKILLS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib_util.spec_from_file_location(f"elfie.skills.{path.stem}", path)
            module = importlib_util.module_from_spec(spec)
            spec.loader.exec_module(module)
            skill_tools = list(getattr(module, "TOOLS", []))
            tools.extend(skill_tools)
            logger.info(f"[Skills] Loaded {path.stem} ({len(skill_tools)} tools)")
        except Exception as e:
            logger.warning(f"[Skills] Skipped broken skill {path.name}: {e}")
    return tools


def validate_skill(path: Path) -> str | None:
    """
    Safety gate for a candidate skill file. Returns None when it passes,
    or a human-readable rejection reason.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as e:
        return f"unreadable file: {e}"

    # 1. Must parse
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return f"syntax error: {e}"

    # 2. No banned imports or calls
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name.split(".")[0] for a in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module.split(".")[0])
            banned = BANNED_IMPORTS.intersection(names)
            if banned:
                return f"banned import: {', '.join(banned)}"
        if isinstance(node, ast.Call):
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                name = f"{node.func.value.id}.{node.func.attr}"
            if name in BANNED_CALLS:
                return f"banned call: {name}"

    # 3. Must declare TOOLS with at least one entry
    has_tools = any(
        isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "TOOLS" for t in n.targets
        )
        for n in ast.walk(tree)
    )
    if not has_tools:
        return "missing module-level TOOLS = [...] list"

    # 4. Must import cleanly in an isolated interpreter (catches missing deps,
    #    top-level crashes) — never inside the running agent.
    check = (
        "import importlib.util as u; "
        f"spec = u.spec_from_file_location('candidate', {str(path)!r}); "
        "m = u.module_from_spec(spec); spec.loader.exec_module(m); "
        "assert getattr(m, 'TOOLS', None), 'TOOLS empty'"
    )
    proc = subprocess.run(
        [sys.executable, "-c", check],
        capture_output=True, text=True, timeout=30, cwd=REPO_DIR,
    )
    if proc.returncode != 0:
        return f"import test failed: {(proc.stderr or proc.stdout).strip()[-300:]}"

    return None


def install_skill(staging_path: Path, name: str) -> Path:
    """Copy a validated skill into the live folder and git-commit it."""
    safe = re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_") or "skill"
    target = SKILLS_DIR / f"{safe}.py"
    shutil.copy(staging_path, target)
    try:
        subprocess.run(["git", "-C", str(REPO_DIR), "add", str(target)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(REPO_DIR), "commit", "-q", "-m", f"Elfie learned skill: {safe}"],
            check=True, capture_output=True,
        )
        logger.info(f"[Skills] Installed and committed {safe}")
    except subprocess.CalledProcessError as e:
        logger.warning(f"[Skills] Installed {safe} but git commit failed: {e.stderr.decode()[:120]}")
    return target
