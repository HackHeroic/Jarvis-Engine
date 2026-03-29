import pytest
from app.core.registry import BaseRegistry, RegistryEntry


async def _dummy_handler(**kwargs):
    return {"handled": True}


def test_register_and_get():
    registry = BaseRegistry(name="test", fallback_key="DEFAULT")
    entry = RegistryEntry(
        name="GREET",
        description="Handle greetings",
        handler=_dummy_handler,
        examples=["hello", "hi"],
    )
    registry.register(entry)
    result = registry.get("GREET")
    assert result is not None
    assert result.name == "GREET"
    assert result.handler is _dummy_handler


def test_get_returns_none_for_unknown():
    registry = BaseRegistry(name="test")
    assert registry.get("NONEXISTENT") is None


def test_get_or_fallback_returns_fallback():
    registry = BaseRegistry(name="test", fallback_key="DEFAULT")
    default_entry = RegistryEntry(
        name="DEFAULT",
        description="Default handler",
        handler=_dummy_handler,
    )
    registry.register(default_entry)
    result = registry.get_or_fallback("NONEXISTENT")
    assert result.name == "DEFAULT"


def test_get_or_fallback_raises_without_fallback():
    registry = BaseRegistry(name="test")
    with pytest.raises(KeyError, match="No entry"):
        registry.get_or_fallback("NONEXISTENT")


def test_classification_prompt_includes_all_entries():
    registry = BaseRegistry(name="test")
    registry.register(RegistryEntry(
        name="A", description="Do A", handler=_dummy_handler, examples=["do a"],
    ))
    registry.register(RegistryEntry(
        name="B", description="Do B", handler=_dummy_handler, examples=["do b"],
    ))
    prompt = registry.classification_prompt()
    assert "A: Do A" in prompt
    assert "B: Do B" in prompt
    assert "do a" in prompt
    assert "do b" in prompt


def test_registered_names():
    registry = BaseRegistry(name="test")
    registry.register(RegistryEntry(name="X", description="x", handler=_dummy_handler))
    registry.register(RegistryEntry(name="Y", description="y", handler=_dummy_handler))
    names = registry.registered_names()
    assert set(names) == {"X", "Y"}


def test_register_overwrites_existing():
    registry = BaseRegistry(name="test")
    handler_a = _dummy_handler

    async def handler_b(**kwargs):
        return {"new": True}

    registry.register(RegistryEntry(name="X", description="old", handler=handler_a))
    registry.register(RegistryEntry(name="X", description="new", handler=handler_b))
    assert registry.get("X").description == "new"
    assert registry.get("X").handler is handler_b
