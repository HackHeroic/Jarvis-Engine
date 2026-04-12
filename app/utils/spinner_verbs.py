"""Server-side spinner verb selection — 80% context-aware, 20% wildcard.

Jarvis personality: Tony Stark wit + Claude Code silliness.
"""

import random

PHASE_VERBS: dict[str, list[str]] = {
    "loading_context":       ["Recollecting", "Summoning", "Dusting off", "Booting up", "Rehydrating"],
    "brain_dump_extraction":  ["Deciphering", "Noodling on", "Untangling", "Dissecting", "Decoding", "Unpacking"],
    "intent_classified":      ["Sussing out", "Reading between the lines", "Deducing", "Profiling"],
    "planning":               ["Orchestrating", "Choreographing", "Tetris-ing", "Blueprinting", "War-rooming"],
    "habits_fetched":         ["Rounding up", "Herding", "Cataloguing", "Mustering"],
    "translating":            ["Weaving", "Translating", "Mapping out", "Threading"],
    "decomposing":            ["Decomposing", "Socratic-chunking", "Slicing into micro-tasks", "Breaking down"],
    "scheduling":             ["Crunching", "Optimizing", "Number-wrangling", "Clockwork-ing", "Tetrimino-ing"],
    "researching":            ["Spelunking", "Excavating", "Rummaging the web", "Sleuthing"],
    "coaching":               ["Checking in on", "Pep-talking", "Reviewing your wins"],
    "ingesting":              ["Munching on", "Digesting", "Absorbing", "Inhaling"],
    "synthesizing":           ["Crafting", "Weaving", "Distilling", "Bottling up", "Composing"],
    "responding":             ["Composing", "Penning", "Wordsmithing", "Articulating"],
    "learning":               ["Absorbing", "Filing away", "Cerebrating", "Etching into memory", "Jotting down"],
}

WILDCARD_VERBS: list[str] = [
    "Jarvising", "Flambéing", "Moonwalking through", "Discombobulating",
    "Prestidigitating", "Combobulating", "Quantum-tunneling",
    "Vibing with", "Percolating", "Gallivanting through",
    "Shenaniganing", "Sock-hopping through", "Razzle-dazzling",
    "Arc-reactoring", "Hullaballooing over", "Stark-industrializing",
    "Beboppin' through", "Flibbertigibbeting", "Canoodling with",
    "Lollygagging over", "Tomfoolering with",
    "Hyperspacing through", "Wibbling at", "Whatchamacalliting",
]


def get_spinner_verb(phase: str) -> str:
    """Pick a spinner verb: 80% context-aware, 20% wildcard."""
    if random.random() < 0.2:
        return random.choice(WILDCARD_VERBS)
    pool = PHASE_VERBS.get(phase, PHASE_VERBS.get("responding", ["Processing"]))
    return random.choice(pool)
