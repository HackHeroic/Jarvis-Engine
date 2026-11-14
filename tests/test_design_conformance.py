"""The class diagrams in docs/capstone/03-class-diagrams.md, checked against the code.

Every Mermaid ``classDiagram`` block in that document is parsed, and each claim it makes
is checked on the real classes:

* every class named exists, at the module the document's class index gives;
* every ``<<interface>>`` is an ``abc.ABC`` whose public methods are all abstract, and,
  where members are drawn, every abstract method is drawn;
* every ``<<abstract>>`` class is abstract, every ``<<protocol>>`` is a ``Protocol``;
* every ``<<enumeration>>`` lists exactly the enum's members, in order;
* every inheritance (``<|--``) and realization (``<|..``) arrow is a real ``issubclass``,
  and a realization points at an interface;
* every method drawn exists, an abstract (``*``) one is abstract, a static (``$``) one is
  a static or class method, and its parameters are the real ones: every required
  positional parameter, then any of the rest, in code order;
* every attribute drawn exists, and every composition, aggregation or association arrow
  is labelled with a real attribute of the class it starts from.

A line the parser does not understand fails the test too, so nothing in a diagram goes
unchecked. Offline: the code is imported, never run.
"""

import abc
import ast
import dataclasses
import enum
import functools
import importlib
import inspect
import re
from dataclasses import dataclass, field
from pathlib import Path

_DOC = Path(__file__).resolve().parent.parent / "docs" / "capstone" / "03-class-diagrams.md"

_ARROW_KINDS = {
    "<|--": "inheritance",
    "<|..": "realization",
    "*--": "composition",
    "o--": "aggregation",
    "-->": "association",
    "..>": "dependency",
}
_LABELLED_BY_ATTRIBUTE = {"composition", "aggregation", "association"}

_IDENT = r"[A-Za-z_]\w*"
_CLASS_OPEN = re.compile(rf"^class\s+({_IDENT})\s*\{{$")
_CLASS_BARE = re.compile(rf"^class\s+({_IDENT})$")
_STEREOTYPE = re.compile(r"^<<\s*([\w ]+?)\s*>>$")
_ARROW = re.compile(
    rf'^({_IDENT})\s*(?:"([^"]*)"\s*)?(<\|--|<\|\.\.|\*--|o--|-->|\.\.>)\s*(?:"([^"]*)"\s*)?'
    rf"({_IDENT})\s*(?::\s*(.+))?$"
)
_METHOD = re.compile(rf"^([+\-#~])?({_IDENT})\((.*?)\)(.*)$")
_ATTRIBUTE = re.compile(rf"^([+\-#~])?(?:(.+?)\s+)?({_IDENT})$")
_INDEX_ROW = re.compile(rf"^\|\s*`({_IDENT})`\s*\|\s*`([\w.]+)`\s*\|\s*$")


# --- parsing ------------------------------------------------------------------------


@dataclass
class Member:
    name: str
    is_method: bool
    params: list[str] = field(default_factory=list)
    classifier: str | None = None  # "*" abstract, "$" static or class method


@dataclass
class ClassBox:
    name: str
    stereotypes: set[str] = field(default_factory=set)
    members: list[Member] = field(default_factory=list)

    @property
    def methods(self) -> list[Member]:
        return [m for m in self.members if m.is_method]

    @property
    def attributes(self) -> list[Member]:
        return [m for m in self.members if not m.is_method]


@dataclass
class Arrow:
    left: str
    kind: str
    right: str
    label: str | None


@dataclass
class Diagram:
    number: int
    classes: dict[str, ClassBox] = field(default_factory=dict)
    arrows: list[Arrow] = field(default_factory=list)

    def box(self, name: str) -> ClassBox:
        return self.classes.setdefault(name, ClassBox(name))


def _parse_member(line: str) -> Member:
    method = _METHOD.match(line)
    if method:
        _visibility, name, params, rest = method.groups()
        rest, classifier = rest.strip(), None
        if rest[:1] in ("*", "$"):  # the classifier written right after the parentheses
            classifier, rest = rest[0], rest[1:].strip()
        if rest[-1:] in ("*", "$"):  # or at the very end, after the return type
            classifier = rest[-1]
        names = [p.strip() for p in params.split(",") if p.strip()]
        bad = [p for p in names if not re.fullmatch(_IDENT, p)]
        if bad:
            raise ValueError(f"parameters must be bare names, got {bad} in {line!r}")
        return Member(name, True, names, classifier)
    attribute = _ATTRIBUTE.match(line)
    if attribute:
        return Member(attribute.group(3), False)
    raise ValueError(f"not a member: {line!r}")


def _parse_diagram(text: str, number: int) -> Diagram:
    """One ``classDiagram`` block; any line it does not understand raises ValueError."""
    diagram = Diagram(number)
    lines = [line.strip() for line in text.splitlines()]
    if not lines or lines[0] != "classDiagram":
        raise ValueError(f"diagram {number} does not start with classDiagram")
    open_box: ClassBox | None = None
    for line in lines[1:]:
        if not line or line.startswith("%%"):
            continue
        if open_box is not None:
            if line == "}":
                open_box = None
            elif stereotype := _STEREOTYPE.match(line):
                open_box.stereotypes.add(stereotype.group(1).lower())
            else:
                open_box.members.append(_parse_member(line))
            continue
        if line.startswith("direction ") or line.startswith("note "):
            continue
        if opened := _CLASS_OPEN.match(line):
            open_box = diagram.box(opened.group(1))
        elif bare := _CLASS_BARE.match(line):
            diagram.box(bare.group(1))
        elif arrow := _ARROW.match(line):
            left, _m1, symbol, _m2, right, label = arrow.groups()
            diagram.box(left)
            diagram.box(right)
            diagram.arrows.append(
                Arrow(left, _ARROW_KINDS[symbol], right, label.strip() if label else None)
            )
        else:
            raise ValueError(f"diagram {number}: unrecognised line {line!r}")
    if open_box is not None:
        raise ValueError(f"diagram {number}: class {open_box.name} is never closed")
    return diagram


def _parse_document(text: str) -> tuple[list[Diagram], dict[str, str]]:
    """Every class diagram in the document, and its class index (name -> module)."""
    blocks = re.findall(r"```mermaid\n(.*?)```", text, flags=re.DOTALL)
    diagrams = [
        _parse_diagram(block, n)
        for n, block in enumerate(
            (b for b in blocks if b.lstrip().startswith("classDiagram")), start=1
        )
    ]
    index_section = text.split("## 9. Class index", 1)[1].split("\n## ", 1)[0]
    index = {}
    for line in index_section.splitlines():
        if row := _INDEX_ROW.match(line.strip()):
            index[row.group(1)] = row.group(2)
    return diagrams, index


@functools.lru_cache(maxsize=1)
def _document() -> tuple[list[Diagram], dict[str, str]]:
    return _parse_document(_DOC.read_text())


# --- looking things up in the code --------------------------------------------------


def _resolve(index: dict[str, str], name: str) -> type:
    return getattr(importlib.import_module(index[name]), name)


@functools.lru_cache(maxsize=None)
def _module_classes(path: str) -> dict[str, ast.ClassDef]:
    tree = ast.parse(Path(path).read_text(), filename=path)
    return {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}


def _self_assignments(klass: type) -> set[str]:
    """Names assigned as ``self.<name> = ...`` (or annotated) inside ``klass``'s body."""
    try:
        path = inspect.getfile(klass)
    except TypeError:  # a builtin
        return set()
    node = _module_classes(path).get(klass.__qualname__)
    if node is None:
        return set()
    names = set()
    for sub in ast.walk(node):
        targets = sub.targets if isinstance(sub, ast.Assign) else (
            [sub.target] if isinstance(sub, ast.AnnAssign) else []
        )
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                names.add(target.attr)
    return names


def _has_attribute(cls: type, name: str) -> bool:
    """A field, class attribute, property, annotation, or ``self.<name>`` assignment."""
    if name in getattr(cls, "model_fields", {}):
        return True
    if dataclasses.is_dataclass(cls) and name in {f.name for f in dataclasses.fields(cls)}:
        return True
    for klass in inspect.getmro(cls):
        if name in vars(klass) or name in inspect.get_annotations(klass):
            return True
        if name in _self_assignments(klass):
            return True
    return False


def _raw(cls: type, name: str):
    """The attribute as stored in the class dict that defines it (staticmethod etc. intact)."""
    for klass in inspect.getmro(cls):
        if name in vars(klass):
            return vars(klass)[name]
    return None


def _parameter_problem(cls: type, member: Member) -> str | None:
    """Why ``member``'s parameters do not fit the real signature, or None."""
    params = list(inspect.signature(getattr(cls, member.name)).parameters.values())
    if params and params[0].name in ("self", "cls"):
        params = params[1:]
    names = [p.name for p in params]
    required = [
        p.name
        for p in params
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty
    ]
    if member.params[: len(required)] != required:
        return f"must list the required parameters {required} first, lists {member.params}"
    remaining = iter(names)
    if not all(any(p == n for n in remaining) for p in member.params):
        return f"lists {member.params}, not names of {names} in code order"
    return None


def _boxes(diagrams: list[Diagram]):
    for diagram in diagrams:
        for box in diagram.classes.values():
            yield diagram, box


def _declared(diagrams: list[Diagram], stereotype: str) -> set[str]:
    return {box.name for _, box in _boxes(diagrams) if stereotype in box.stereotypes}


# --- the checks (each returns a list of problems; empty means the diagrams hold) ----


def _class_problems(diagrams: list[Diagram], index: dict[str, str]) -> list[str]:
    drawn = {box.name for _, box in _boxes(diagrams)}
    problems = [f"{name}: drawn but not in the class index" for name in sorted(drawn - set(index))]
    problems += [f"{name}: in the class index but in no diagram" for name in sorted(set(index) - drawn)]
    for name in sorted(drawn & set(index)):
        try:
            cls = _resolve(index, name)
        except (ImportError, AttributeError) as exc:
            problems.append(f"{name}: not importable from {index[name]} ({exc})")
            continue
        if not inspect.isclass(cls):
            problems.append(f"{name}: {index[name]}.{name} is not a class")
    return problems


def _stereotype_problems(diagrams: list[Diagram], index: dict[str, str]) -> list[str]:
    problems = []
    for diagram, box in _boxes(diagrams):
        where = f"diagram {diagram.number}, {box.name}"
        cls = _resolve(index, box.name)
        if "interface" in box.stereotypes:
            abstract = set(getattr(cls, "__abstractmethods__", ()))
            if not (issubclass(cls, abc.ABC) and abstract):
                problems.append(f"{where}: <<interface>> but not an abc.ABC with abstract methods")
            concrete = [
                n for n, v in vars(cls).items()
                if inspect.isfunction(v) and not n.startswith("_") and n not in abstract
            ]
            if concrete:
                problems.append(f"{where}: <<interface>> with concrete methods {concrete}")
            if box.members and (missing := abstract - {m.name for m in box.methods}):
                problems.append(f"{where}: abstract methods not drawn: {sorted(missing)}")
        if "abstract" in box.stereotypes and not inspect.isabstract(cls):
            problems.append(f"{where}: <<abstract>> but the class is not abstract")
        if "protocol" in box.stereotypes and not getattr(cls, "_is_protocol", False):
            problems.append(f"{where}: <<protocol>> but not a typing.Protocol")
        if "enumeration" in box.stereotypes:
            if not issubclass(cls, enum.Enum):
                problems.append(f"{where}: <<enumeration>> but not an Enum")
                continue
            drawn = [m.name for m in box.members]
            real = [m.name for m in cls]
            if drawn != real:
                problems.append(f"{where}: members {drawn} but the enum has {real}")
    return problems


def _arrow_problems(diagrams: list[Diagram], index: dict[str, str]) -> list[str]:
    interfaces = _declared(_document()[0], "interface") | _declared(diagrams, "interface")
    problems = []
    for diagram in diagrams:
        for arrow in diagram.arrows:
            where = f"diagram {diagram.number}, {arrow.left} {arrow.kind} {arrow.right}"
            left, right = _resolve(index, arrow.left), _resolve(index, arrow.right)
            if arrow.kind in ("inheritance", "realization"):
                if not issubclass(right, left):
                    problems.append(f"{where}: {arrow.right} is not a subclass of {arrow.left}")
                if arrow.kind == "realization" and arrow.left not in interfaces:
                    problems.append(f"{where}: realizes {arrow.left}, which is no <<interface>>")
                if arrow.kind == "inheritance" and arrow.left in interfaces:
                    problems.append(f"{where}: {arrow.left} is an interface; draw <|.. instead")
            elif arrow.kind in _LABELLED_BY_ATTRIBUTE:
                if not arrow.label or not re.fullmatch(_IDENT, arrow.label):
                    problems.append(f"{where}: must be labelled with an attribute name")
                elif not _has_attribute(left, arrow.label):
                    problems.append(f"{where}: {arrow.left} has no attribute {arrow.label!r}")
    return problems


def _method_problems(diagrams: list[Diagram], index: dict[str, str]) -> list[str]:
    problems = []
    for diagram, box in _boxes(diagrams):
        cls = _resolve(index, box.name)
        for member in box.methods:
            where = f"diagram {diagram.number}, {box.name}.{member.name}"
            if not callable(getattr(cls, member.name, None)):
                problems.append(f"{where}: no such method")
                continue
            if member.classifier == "*" and member.name not in getattr(cls, "__abstractmethods__", ()):
                problems.append(f"{where}: drawn abstract (*) but is not abstract")
            if member.classifier == "$" and not isinstance(
                _raw(cls, member.name), (staticmethod, classmethod)
            ):
                problems.append(f"{where}: drawn static ($) but is an instance method")
            if problem := _parameter_problem(cls, member):
                problems.append(f"{where}: {problem}")
    return problems


def _attribute_problems(diagrams: list[Diagram], index: dict[str, str]) -> list[str]:
    problems = []
    for diagram, box in _boxes(diagrams):
        if "enumeration" in box.stereotypes:
            continue  # members, checked with the stereotypes
        cls = _resolve(index, box.name)
        for member in box.attributes:
            if not _has_attribute(cls, member.name):
                problems.append(f"diagram {diagram.number}, {box.name}: no attribute {member.name!r}")
    return problems


# --- tests ---------------------------------------------------------------------------


def test_design_document__class_diagrams__parse_with_an_index():
    diagrams, index = _document()

    assert len(diagrams) >= 10
    assert all(d.classes for d in diagrams)
    assert len(index) >= 100


def test_class_diagrams__every_class__indexed_and_importable():
    diagrams, index = _document()

    assert _class_problems(diagrams, index) == []


def test_class_diagrams__stereotypes__interfaces_abstracts_protocols_and_enums_hold():
    diagrams, index = _document()

    assert _stereotype_problems(diagrams, index) == []


def test_class_diagrams__arrows__real_subclasses_and_real_attributes():
    diagrams, index = _document()

    assert _arrow_problems(diagrams, index) == []


def test_class_diagrams__methods__exist_with_their_real_parameters():
    diagrams, index = _document()

    assert _method_problems(diagrams, index) == []


def test_class_diagrams__attributes__exist_on_the_class():
    diagrams, index = _document()

    assert _attribute_problems(diagrams, index) == []


def test_conformance_check__diagram_that_lies__every_lie_reported():
    """The checker itself: a false arrow, a false method, a false parameter list, a false
    enum, a false attribute and a false whole/part label are each caught."""
    _, index = _document()
    lie = _parse_diagram(
        "classDiagram\n"
        "    class DraftService {\n"
        "        +accept(draft_id, user_id) int\n"
        "        +teleport(user_id) None\n"
        "        -EventBus _wormhole\n"
        "    }\n"
        "    class TaskStatus {\n"
        "        <<enumeration>>\n"
        "        PENDING\n"
        "        DONE\n"
        "    }\n"
        "    TaskRepository <|.. InMemoryDraftRepository\n"
        "    DraftService o-- DraftRepository : _ghost\n",
        99,
    )

    problems = (
        _arrow_problems([lie], index)
        + _method_problems([lie], index)
        + _stereotype_problems([lie], index)
        + _attribute_problems([lie], index)
    )

    assert problems == [
        "diagram 99, TaskRepository realization InMemoryDraftRepository:"
        " InMemoryDraftRepository is not a subclass of TaskRepository",
        "diagram 99, DraftService aggregation DraftRepository: DraftService has no attribute '_ghost'",
        "diagram 99, DraftService.accept: must list the required parameters"
        " ['user_id', 'draft_id'] first, lists ['draft_id', 'user_id']",
        "diagram 99, DraftService.teleport: no such method",
        "diagram 99, TaskStatus: members ['PENDING', 'DONE'] but the enum has"
        " ['PENDING', 'COMPLETED', 'SKIPPED', 'MISSED']",
        "diagram 99, DraftService: no attribute '_wormhole'",
    ]


def test_conformance_parser__unknown_line__raises_rather_than_skips():
    try:
        _parse_diagram("classDiagram\n    DraftService <-> TaskService\n", 1)
    except ValueError as exc:
        assert "unrecognised line" in str(exc)
    else:
        raise AssertionError("an arrow the checker cannot read must fail, not pass unchecked")
