"""Layering rule: the domain layer depends on nothing outside it."""

import ast
from pathlib import Path

_DOMAIN_DIR = Path(__file__).resolve().parent.parent / "app" / "domain"
_FORBIDDEN_PREFIXES = (
    "app.api",
    "app.services",
    "app.orchestrator",
    "app.infrastructure",
    "app.core",
    "fastapi",
    "supabase",
    "litellm",
    "anthropic",
)


def _imported_modules(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _is_forbidden(module: str) -> bool:
    return any(module == p or module.startswith(p + ".") for p in _FORBIDDEN_PREFIXES)


def test_domain__imports_nothing_from_outer_layers():
    files = sorted(_DOMAIN_DIR.glob("*.py"))
    assert files, f"no domain modules found under {_DOMAIN_DIR}"

    violations = [
        f"{path.name}: {module}"
        for path in files
        for module in _imported_modules(ast.parse(path.read_text(), filename=str(path)))
        if _is_forbidden(module)
    ]

    assert violations == []


# --- infrastructure ---------------------------------------------------------------

_APP_DIR = _DOMAIN_DIR.parent
_INFRASTRUCTURE_DIR = _APP_DIR / "infrastructure"
_INFRASTRUCTURE_FORBIDDEN = ("app.api", "app.orchestrator")


def _resolved_imports(path: Path) -> list[str]:
    """Every module ``path`` imports, as absolute dotted names.

    ``from X import Y`` yields both ``X`` and ``X.Y`` (so ``from app import orchestrator``
    is caught), and relative imports are resolved against the file's package.
    """
    package = list(path.relative_to(_APP_DIR.parent).with_suffix("").parts[:-1])
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join([*base, *([node.module] if node.module else [])])
            names.append(module)
            names.extend(f"{module}.{alias.name}" for alias in node.names)
    return names


def test_infrastructure__imports_nothing_from_api_or_orchestrator():
    files = sorted(_INFRASTRUCTURE_DIR.rglob("*.py"))
    assert files, f"no infrastructure modules found under {_INFRASTRUCTURE_DIR}"

    violations = [
        f"{path.relative_to(_APP_DIR)}: {module}"
        for path in files
        for module in _resolved_imports(path)
        if any(module == p or module.startswith(p + ".") for p in _INFRASTRUCTURE_FORBIDDEN)
    ]

    assert violations == []
