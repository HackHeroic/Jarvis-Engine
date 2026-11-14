"""Layering rules: which layer may import which."""

import ast
import re
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


def _is_forbidden(module: str) -> bool:
    return any(module == p or module.startswith(p + ".") for p in _FORBIDDEN_PREFIXES)


_APP_DIR = _DOMAIN_DIR.parent
_INFRASTRUCTURE_DIR = _APP_DIR / "infrastructure"
_INFRASTRUCTURE_FORBIDDEN = ("app.api", "app.orchestrator")


def _import_targets(path: Path, root: Path) -> list[tuple[str, bool]]:
    """(absolute dotted module, imported relatively?) for every import in ``path``.

    ``root`` is the directory the top-level package sits in. ``from X import Y``
    yields both ``X`` and ``X.Y`` (so ``from app import orchestrator`` is caught),
    and relative imports are resolved against the file's package.
    """
    package = list(path.relative_to(root).with_suffix("").parts[:-1])
    targets: list[tuple[str, bool]] = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            targets.extend((alias.name, False) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join([*base, *([node.module] if node.module else [])])
            relative = node.level > 0
            targets.append((module, relative))
            targets.extend((f"{module}.{alias.name}", relative) for alias in node.names)
    return targets


def _resolved_imports(path: Path) -> list[str]:
    """Every module ``path`` (a file under app/) imports, as absolute dotted names."""
    return [module for module, _relative in _import_targets(path, _APP_DIR.parent)]


def _domain_violations(domain_dir: Path) -> list[str]:
    """Imports anywhere under ``domain_dir`` that reach an outer layer or a framework.

    Recursive, and every spelling counts: ``import app.core``, ``from app import core``,
    and any relative import that climbs out of ``app.domain`` at all.
    """
    root = domain_dir.parent.parent
    violations = []
    for path in sorted(domain_dir.rglob("*.py")):
        where = path.relative_to(domain_dir)
        for module, relative in _import_targets(path, root):
            if _is_forbidden(module):
                violations.append(f"{where}: {module}")
            elif relative and not (module == "app.domain" or module.startswith("app.domain.")):
                violations.append(f"{where}: relative import of {module}")
    return violations


def test_domain__imports_nothing_from_outer_layers():
    assert sorted(_DOMAIN_DIR.rglob("*.py")), f"no domain modules found under {_DOMAIN_DIR}"

    assert _domain_violations(_DOMAIN_DIR) == []


def test_domain_check__every_spelling_of_an_outer_import__is_caught(tmp_path):
    """The check itself: nested modules, ``from app import x`` and relative climbs."""
    domain = tmp_path / "app" / "domain"
    (domain / "nested").mkdir(parents=True)
    (domain / "entities.py").write_text("from app import core\nfrom . import errors\n")
    (domain / "nested" / "rules.py").write_text(
        "from ...api import deps\nfrom .. import entities\nfrom ... import utils\n"
    )

    assert _domain_violations(domain) == [
        "entities.py: app.core",
        "nested/rules.py: app.api",
        "nested/rules.py: app.api.deps",
        "nested/rules.py: relative import of app",
        "nested/rules.py: relative import of app.utils",
    ]


# --- infrastructure ---------------------------------------------------------------


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

# The one legacy shim: with no container registered (tests, scripts) the draft-accept
# helper builds a DraftService over the Supabase repository itself.
_LEGACY_SHIM = _APP_DIR / "services" / "draft_actions.py"


def test_layering__services_and_orchestrator__do_not_import_infrastructure():
    files = sorted([*(_APP_DIR / "services").rglob("*.py"), *(_APP_DIR / "orchestrator").rglob("*.py")])
    assert files

    importing = {
        path: [m for m in _resolved_imports(path) if m == "app.infrastructure" or m.startswith("app.infrastructure.")]
        for path in files
    }
    violations = [
        f"{path.relative_to(_APP_DIR)}: {module}"
        for path, modules in importing.items()
        if path != _LEGACY_SHIM
        for module in modules
    ]

    assert violations == []
    assert importing[_LEGACY_SHIM], "the shim no longer imports infrastructure: drop the exception"


_CONCRETE_REPOSITORY = re.compile(r"^(Supabase|InMemory)\w*Repository$")


def _repository_constructions(path: Path) -> list[str]:
    """Names of the concrete repository classes ``path`` instantiates directly."""
    built = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if _CONCRETE_REPOSITORY.match(name or ""):
                built.append(name)
    return built


def test_layering__only_container_builds_repositories():
    """Dependency inversion, literally: one place chooses the repository family.

    Neither the factory nor a concrete Supabase*/InMemory* repository class is built
    anywhere else under app/, except in the persistence package that defines them.
    """
    defined_in = _INFRASTRUCTURE_DIR / "persistence"
    builds_concrete_allowed = {_APP_DIR / "container.py", _LEGACY_SHIM}

    referencing = [
        str(path.relative_to(_APP_DIR))
        for path in sorted(_APP_DIR.rglob("*.py"))
        if "RepositoryFactory" in path.read_text()
        and path != _APP_DIR / "container.py"
        and defined_in not in path.parents
    ]
    constructing = [
        f"{path.relative_to(_APP_DIR)}: {name}"
        for path in sorted(_APP_DIR.rglob("*.py"))
        if path not in builds_concrete_allowed and defined_in not in path.parents
        for name in _repository_constructions(path)
    ]

    assert referencing == []
    assert constructing == []
    assert "RepositoryFactory" in (_APP_DIR / "container.py").read_text()


def test_repository_construction_check__direct_construction__is_caught(tmp_path):
    planted = tmp_path / "planner.py"
    planted.write_text(
        "from app.infrastructure.persistence import supabase_repos as repos\n"
        "drafts = repos.SupabaseDraftRepository(client)\n"
        "tasks = InMemoryTaskRepository(store)\n"
        "port = DraftRepository\n"
    )

    assert _repository_constructions(planted) == ["SupabaseDraftRepository", "InMemoryTaskRepository"]


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
