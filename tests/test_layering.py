"""Layering rules: which layer may import which."""

import ast
import re
import sys
from pathlib import Path

_DOMAIN_DIR = Path(__file__).resolve().parent.parent / "app" / "domain"

# The domain imports the standard library, Pydantic, and itself — nothing else. An
# allow-list, so a new outer layer or framework is caught without anyone listing it.
_DOMAIN_ALLOWED_THIRD_PARTY = ("pydantic", "pydantic_core")


def _is_forbidden(module: str) -> bool:
    """Is ``module`` (absolute, dotted) off-limits to the domain?"""
    top = module.split(".", 1)[0]
    if module == "app" or module == "app.domain" or module.startswith("app.domain."):
        return False  # bare "app" (``from app import x``): ``app.x`` is checked on its own
    if top == "app":
        return True  # every other part of the app is an outer layer
    return top not in sys.stdlib_module_names and top not in _DOMAIN_ALLOWED_THIRD_PARTY


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


def test_domain_check__anything_off_the_allow_list__is_caught(tmp_path):
    """Absolute imports the old deny-list let through: other app packages, any framework."""
    domain = tmp_path / "app" / "domain"
    domain.mkdir(parents=True)
    (domain / "entities.py").write_text(
        "import app.schemas.context\n"
        "from app.db import supabase_py\n"
        "import langgraph\n"
        "from httpx import AsyncClient\n"
        "from __future__ import annotations\n"
        "import datetime, enum, typing\n"
        "from collections.abc import Iterable\n"
        "from pydantic import BaseModel\n"
        "from pydantic_core import PydanticCustomError\n"
        "from app.domain.errors import JarvisError\n"
    )

    assert _domain_violations(domain) == [
        "entities.py: app.schemas.context",
        "entities.py: app.db",
        "entities.py: app.db.supabase_py",
        "entities.py: langgraph",
        "entities.py: httpx",
        "entities.py: httpx.AsyncClient",
    ]


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
        "nested/rules.py: app.utils",
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

# Files under app/ (not the composition roots) that may import app.infrastructure, and
# why. The list can only shrink: an entry that no longer imports it is a failure too.
_INFRASTRUCTURE_IMPORTS_ALLOWED: dict[str, str] = {
    "core/model_router.py": (
        "route_llm_call drives the provider chain the container installs (set_chain,"
        " current_chain) and its transport helpers"
    ),
}


def _imports_under(path: Path, prefix: str) -> list[str]:
    return [m for m in _resolved_imports(path) if m == prefix or m.startswith(prefix + ".")]


def _layer_report(
    dirs: list[str], prefix: str, allowed: dict[str, object]
) -> tuple[list[str], list[str]]:
    """(imports of ``prefix`` under ``dirs`` outside ``allowed``, stale ``allowed`` entries).

    ``allowed`` maps a file under app/ to a reason (any module under ``prefix`` is
    allowed there) or to a set of the modules it may import.
    """
    files = sorted(p for d in dirs for p in (_APP_DIR / d).rglob("*.py"))
    assert files, f"nothing under {dirs}"
    unexpected, used = [], set()
    for path in files:
        where = path.relative_to(_APP_DIR).as_posix()
        for module in _imports_under(path, prefix):
            permitted = allowed.get(where)
            ok = isinstance(permitted, str) or (
                isinstance(permitted, (set, frozenset))
                and any(module == m or module.startswith(m + ".") for m in permitted)
            )
            if ok:
                used.add(where)
            else:
                unexpected.append(f"{where}: {module}")
    in_scope = {
        where for where in allowed if any(where.startswith(d.rstrip("/") + "/") for d in dirs)
    }
    return unexpected, sorted(in_scope - used)


def test_layering__services_orchestrator_modules_core_api__do_not_import_infrastructure():
    unexpected, stale = _layer_report(
        ["services", "orchestrator", "modules", "core", "api"],
        "app.infrastructure",
        _INFRASTRUCTURE_IMPORTS_ALLOWED,
    )

    assert unexpected == [], "take the concrete from the container instead"
    assert stale == [], "no longer imports infrastructure: drop it from the allow-list"


# Nothing below the API layer reaches up into it: what is shared lives below it
# (run_schedule in app/services/planning/scheduling.py, the schemas in app/schemas).
_API_IMPORTS_ALLOWED: dict[str, frozenset[str]] = {}
_BELOW_THE_API = [
    "services", "domain", "infrastructure", "modules", "orchestrator", "core", "utils", "schemas",
]


def test_layering__nothing_below_the_api__imports_the_api():
    unexpected, stale = _layer_report(_BELOW_THE_API, "app.api", _API_IMPORTS_ALLOWED)

    assert unexpected == [], "move what is shared below the API (e.g. app/schemas)"
    assert stale == [], "no longer imports the API: drop it from the allow-list"


# No service talks to the Supabase driver: clients come from app.db.supabase_py (one
# configured transport) and repositories from the container. Legacy exceptions would be
# listed here; there are none left.
_SUPABASE_DRIVER_ALLOWED: dict[str, str] = {}


def test_layering__services__do_not_import_the_supabase_driver():
    unexpected, stale = _layer_report(["services"], "supabase", _SUPABASE_DRIVER_ALLOWED)

    assert unexpected == [], "use app.db.supabase_py.build_supabase_client or a repository"
    assert stale == []


# What Plan 2 and Plan 3 built is held to every rule: none of it may sit on an allow-list.
_PLAN_2 = (
    "services/anticipation/",
    "services/memory/",  # MemoryService, PEARL, conversation memory, "since we last spoke"
    "domain/anticipation.py",
    "infrastructure/embedding.py",
    "infrastructure/llm/messages.py",
    "services/user_model_service.py",
)
_PLAN_3 = (
    "services/chat_turn/",  # ChatTurnService: the turn, out of the router
    "services/planning/scheduling.py",  # run_schedule's home
    "services/planning/services.py",  # PlanningServices
    "core/settings.py",  # the settings records the composition root builds
    "api/sse.py",
    "api/request_limits.py",
    "schemas/chat_stream.py",
    "schemas/errors.py",
    "schemas/schedule.py",
)


def test_layering__plan_2_and_plan_3_code__is_on_no_allow_list():
    listed = [
        *_INFRASTRUCTURE_IMPORTS_ALLOWED,
        *_API_IMPORTS_ALLOWED,
        *_SUPABASE_DRIVER_ALLOWED,
        *_RAW_TABLE_CALLS_ALLOWED,
        *(file for file, _owner in _LEGACY_LLM_CALLS_ALLOWED),
        *(file for file, _owner in _WEB_SEARCH_CALLS_ALLOWED),
    ]

    assert [f for f in listed if f.startswith(_PLAN_2 + _PLAN_3)] == []
    # Each guarded path is where the code is: a move would leave the guard guarding nothing.
    assert [p for p in _PLAN_2 + _PLAN_3 if not (_APP_DIR / p).exists()] == []
    assert (_APP_DIR / "services" / "memory" / "since_last_spoke.py").exists()


def test_layer_report__an_import_off_the_list__is_caught_and_a_stale_entry_named(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sys.modules[__name__], "_APP_DIR", tmp_path / "app")
    services = tmp_path / "app" / "services"
    services.mkdir(parents=True)
    (services / "fresh.py").write_text("from app.api.v1.endpoints import schedule\n")
    (services / "legacy.py").write_text("import supabase\n")

    unexpected, stale = _layer_report(
        ["services"], "app.api", {"services/gone.py": frozenset({"app.api"})}
    )

    assert unexpected == [
        "services/fresh.py: app.api.v1.endpoints",
        "services/fresh.py: app.api.v1.endpoints.schedule",
    ]
    assert stale == ["services/gone.py"]


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
    builds_concrete_allowed = {_APP_DIR / "container.py"}

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

# (file under app/, enclosing top-level function) -> why it may still call the transport.
_LEGACY_LLM_CALLS_ALLOWED: dict[tuple[str, str], str] = {}


# Web search: Claude's search tool, then Gemini's grounding, outside the provider chain
# (its request has no place for a search tool). One helper calls it — after the PII gate, metered into the usage ledger,
# failing honestly — and nothing else may.
_WEB_SEARCH_CALLS = frozenset({"claude_web_search", "gemini_web_search", "run_deep_research"})

_WEB_SEARCH_CALLS_ALLOWED: dict[tuple[str, str], str] = {
    ("services/analytical/workspace_builder.py", "_search_web"): (
        "the shared search helper: PII gate first, metered, WebSearchUnavailableError"
    ),
}


def _calls_to(path: Path, names: frozenset[str]) -> list[tuple[str, int]]:
    """(enclosing top-level definition, line) of each call in ``path`` to one of ``names``."""
    found: list[tuple[str, int]] = []
    for top in ast.parse(path.read_text(), filename=str(path)).body:
        owner = getattr(top, "name", "<module>")
        for node in ast.walk(top):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in names:
                found.append((owner, node.lineno))
    return found


def _legacy_llm_calls(path: Path) -> list[tuple[str, int]]:
    """(enclosing top-level definition, line) of each call to a transport function."""
    return _calls_to(path, _LEGACY_LLM_CALLS)


def _guard_report(
    names: frozenset[str], allowed: dict[tuple[str, str], str], app_dir: Path = _APP_DIR
) -> tuple[list[str], list[str]]:
    """(calls outside the allow-list, allow-list entries that no longer call) under ``app_dir``."""
    calls: dict[tuple[str, str], list[int]] = {}
    for path in sorted(app_dir.rglob("*.py")):
        if path in _LLM_TRANSPORT_FILES:
            continue
        for owner, line in _calls_to(path, names):
            calls.setdefault((path.relative_to(app_dir).as_posix(), owner), []).append(line)
    unexpected = [
        f"{file}:{lines} in {owner}"
        for (file, owner), lines in sorted(calls.items())
        if (file, owner) not in allowed
    ]
    stale = [f"{file} {owner}" for file, owner in allowed if (file, owner) not in calls]
    return unexpected, stale


def test_llm_calls__outside_the_transport__go_through_route_llm_call():
    unexpected, stale = _guard_report(_LEGACY_LLM_CALLS, _LEGACY_LLM_CALLS_ALLOWED)

    assert not unexpected, "call route_llm_call instead:\n" + "\n".join(unexpected)
    assert not stale, "no longer call the transport, drop from the allow-list:\n" + "\n".join(stale)


def test_web_search__only_the_shared_search_helper__calls_it():
    unexpected, stale = _guard_report(_WEB_SEARCH_CALLS, _WEB_SEARCH_CALLS_ALLOWED)

    assert not unexpected, "go through workspace_builder._search_web:\n" + "\n".join(unexpected)
    assert not stale, "no longer calls web search, drop from the allow-list:\n" + "\n".join(stale)


def test_web_search_guard__a_direct_call_elsewhere__is_caught(tmp_path):
    (tmp_path / "modules").mkdir()
    (tmp_path / "modules" / "research.py").write_text(
        "from app.models.brain import litellm_conf\n"
        "async def execute_search(q):\n"
        "    return await litellm_conf.gemini_web_search(user_prompt=q)\n"
        "async def deep(qs):\n"
        "    return await run_deep_research(qs)\n"
    )

    unexpected, _ = _guard_report(_WEB_SEARCH_CALLS, {}, app_dir=tmp_path)

    assert unexpected == ["modules/research.py:[5] in deep", "modules/research.py:[3] in execute_search"]


def test_routes__no_raw_model_proxy__the_lm_studio_route_is_gone():
    """/lmstudio proxied straight to LM Studio over httpx: no chain, no ledger. Nothing
    called it (the frontend talks to /chat/stream), so it was deleted, not allowed."""
    from app.main import app

    assert [r.path for r in app.routes if "/lmstudio" in getattr(r, "path", "")] == []


# --- raw table calls --------------------------------------------------------------

# Files that still call a Supabase table directly (``.table(``), and why. Everything
# else reads and writes through a repository port the container built. The list can
# only shrink: an entry that no longer calls a table is a failure too.
_PLAN_3_CUT = "Plan 3 cut (Task 6): no repository yet for"
_RAW_TABLE_CALLS_ALLOWED: dict[str, str] = {
    "services/analytical/workspace_builder.py": (
        "task_workspaces cache (decision 10); its user_tasks reads need topic_keywords and"
        " implementation_intention, which the Task entity does not carry; its chunk lookup"
        " reads task_materials itself (module functions the container does not build)"
    ),
    "services/user_preferences.py": "learning_style read (decision 10)",
    "services/chat_history.py": f"{_PLAN_3_CUT} chat_sessions / chat_messages",
    "api/v1/endpoints/sessions.py": f"{_PLAN_3_CUT} chat_sessions",
    "api/v1/endpoints/ingestion.py": f"{_PLAN_3_CUT} pending_calendar_updates",
    "services/extraction/orchestrator.py": f"{_PLAN_3_CUT} pending_calendar_updates",
}
_RAW_TABLE_DIRS = ["api", "services", "core", "modules", "orchestrator"]


def _raw_table_calls(path: Path) -> list[int]:
    """Lines of every ``<anything>.table(...)`` call in ``path``."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path)))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "table"
    ]


def _raw_table_report(
    allowed: dict[str, str], app_dir: Path | None = None
) -> tuple[list[str], list[str]]:
    """(files calling ``.table(`` outside ``allowed``, stale ``allowed`` entries)."""
    root = app_dir or _APP_DIR
    calling = {
        path.relative_to(root).as_posix(): lines
        for d in _RAW_TABLE_DIRS
        for path in sorted((root / d).rglob("*.py"))
        if (lines := _raw_table_calls(path))
    }
    unexpected = [f"{f}:{lines}" for f, lines in sorted(calling.items()) if f not in allowed]
    stale = sorted(f for f in allowed if f not in calling)
    return unexpected, stale


def test_layering__no_raw_table_calls_outside_the_allow_list():
    unexpected, stale = _raw_table_report(_RAW_TABLE_CALLS_ALLOWED)

    assert unexpected == [], "read and write through a repository port"
    assert stale == [], "no longer calls a table: drop it from the allow-list"


def test_raw_table_report__a_call_off_the_list__is_caught_and_a_stale_entry_named(tmp_path):
    for d in _RAW_TABLE_DIRS:
        (tmp_path / d).mkdir()
    (tmp_path / "core" / "facade.py").write_text(
        "rows = self._db.supabase.table('user_tasks').select('*').execute()\n"
    )
    (tmp_path / "services" / "fine.py").write_text("x = repos.tasks.list_pending(u)\n")

    unexpected, stale = _raw_table_report({"services/gone.py": "legacy"}, app_dir=tmp_path)

    assert unexpected == ["core/facade.py:[1]"]
    assert stale == ["services/gone.py"]


def test_layering__allow_lists__are_exactly_the_documented_leftovers():
    assert set(_INFRASTRUCTURE_IMPORTS_ALLOWED) == {"core/model_router.py"}
    assert _API_IMPORTS_ALLOWED == {}
    assert _SUPABASE_DRIVER_ALLOWED == {}
    assert _LEGACY_LLM_CALLS_ALLOWED == {}
    assert set(_WEB_SEARCH_CALLS_ALLOWED) == {
        ("services/analytical/workspace_builder.py", "_search_web")
    }
    assert set(_RAW_TABLE_CALLS_ALLOWED) == {
        "services/analytical/workspace_builder.py",
        "services/user_preferences.py",
        # Plan 3 cut Task 6 (sessions, messages, pending calendar):
        "services/chat_history.py",
        "api/v1/endpoints/sessions.py",
        "api/v1/endpoints/ingestion.py",
        "services/extraction/orchestrator.py",
    }
    assert all(reason.strip() for reason in _RAW_TABLE_CALLS_ALLOWED.values())
