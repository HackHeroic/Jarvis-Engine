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
