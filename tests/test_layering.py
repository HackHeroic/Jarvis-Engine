"""Layering rules: which layer may import which."""

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


# --- services and the composition root --------------------------------------------

# Files that must reach infrastructure only through ports. The list grows as more of
# app/services and app/orchestrator move onto the ports.
_PORT_ONLY = (
    _APP_DIR / "services" / "draft_service.py",
    _APP_DIR / "services" / "usage_ledger.py",
    *sorted(_DOMAIN_DIR.glob("*.py")),
)


def test_layering__services_and_orchestrator__do_not_import_infrastructure():
    missing = [str(path.relative_to(_APP_DIR)) for path in _PORT_ONLY if not path.exists()]
    assert missing == []

    violations = [
        f"{path.relative_to(_APP_DIR)}: {module}"
        for path in _PORT_ONLY
        for module in _resolved_imports(path)
        if module == "app.infrastructure" or module.startswith("app.infrastructure.")
    ]

    assert violations == []


def test_layering__only_container_builds_repositories():
    """Dependency inversion, literally: one place chooses the repository family."""
    defined_in = _INFRASTRUCTURE_DIR / "persistence"
    allowed = {_APP_DIR / "container.py"}

    referencing = [
        str(path.relative_to(_APP_DIR))
        for path in sorted(_APP_DIR.rglob("*.py"))
        if "RepositoryFactory" in path.read_text()
        and path not in allowed
        and defined_in not in path.parents
    ]

    assert referencing == []
    assert "RepositoryFactory" in (_APP_DIR / "container.py").read_text()


# --- language-model calls ---------------------------------------------------------

# The LiteLLM transport functions. Called directly they skip the provider chain: no
# Claude fallback, no PII redaction of prompt, system prompt and history, no usage
# ledger. Everything else reaches a model through ``route_llm_call``.
_LEGACY_LLM_CALLS = frozenset({"hybrid_route_query", "gemini_primary_route", "local_primary_route"})

# The transport itself, and the provider adapters built on it.
_LLM_TRANSPORT_FILES = frozenset(
    {
        _APP_DIR / "models" / "brain" / "litellm_conf.py",
        *(_INFRASTRUCTURE_DIR / "llm").rglob("*.py"),
    }
)

_V1_ONLY = "v1 control-policy flow, reached only from the deprecated /chat and /chat/stream"

# (file under app/, enclosing top-level function) -> why it may still call the transport.
_LEGACY_LLM_CALLS_ALLOWED: dict[tuple[str, str], str] = {
    ("api/v1/endpoints/chat.py", "chat_stream"): "inside the deprecated v1 /chat/stream route",
    ("api/v1/endpoints/chat.py", "_stream_direct_27b"): "called only by the deprecated /chat/stream",
    ("services/analytical/control_policy.py", "_extract_and_stage_inline_habits"): _V1_ONLY,
    ("services/analytical/control_policy.py", "_run_brain_dump_extraction"): _V1_ONLY,
    ("services/analytical/control_policy.py", "_direct_qa_response"): _V1_ONLY,
    ("services/analytical/control_policy.py", "_fallback_single_intent"): _V1_ONLY,
    ("services/analytical/control_policy.py", "_run_plan_day_flow"): _V1_ONLY,
}


def _legacy_llm_calls(path: Path) -> list[tuple[str, int]]:
    """(enclosing top-level definition, line) of each call to a transport function."""
    found: list[tuple[str, int]] = []
    for top in ast.parse(path.read_text(), filename=str(path)).body:
        owner = getattr(top, "name", "<module>")
        for node in ast.walk(top):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in _LEGACY_LLM_CALLS:
                found.append((owner, node.lineno))
    return found


def test_llm_calls__outside_the_transport__go_through_route_llm_call():
    calls: dict[tuple[str, str], list[int]] = {}
    for path in sorted(_APP_DIR.rglob("*.py")):
        if path in _LLM_TRANSPORT_FILES:
            continue
        for owner, line in _legacy_llm_calls(path):
            calls.setdefault((path.relative_to(_APP_DIR).as_posix(), owner), []).append(line)

    unexpected = [
        f"{file}:{lines} in {owner}"
        for (file, owner), lines in sorted(calls.items())
        if (file, owner) not in _LEGACY_LLM_CALLS_ALLOWED
    ]
    stale = [f"{file} {owner}" for file, owner in _LEGACY_LLM_CALLS_ALLOWED if (file, owner) not in calls]

    assert not unexpected, "call route_llm_call instead:\n" + "\n".join(unexpected)
    assert not stale, "no longer call the transport, drop from the allow-list:\n" + "\n".join(stale)
