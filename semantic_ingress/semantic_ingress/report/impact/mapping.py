"""
The business-impact mapping (knowledge/business-impact.yaml): loading, validation,
and what code derives from a process card before any model call.

`derive_signals` reads the card's facts (numbered as card_facts numbers them), and
`eligible_categories` decides which consequence categories the card supports. The
model may only choose from those, and validate.py checks the same rule, so
"derived from the code" is enforced in code, not only asked for in a prompt.
"""
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args

import yaml

from semantic_ingress.config import _knowledge_file
from semantic_ingress.report.impact.models import DriverKind, ImpactCategory, Stakeholder
from semantic_ingress.report.investigate import NOT_ESTABLISHED, card_facts

ANCHOR_KINDS = {"crash_loss", "waits", "trigger"}
REQUIRE_KEYS = {"effects_any", "crash_loss", "waits", "distinct_effects_min", "path"}
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


class MappingError(Exception):
    """The mapping file is missing or invalid."""


def default_mapping_path() -> Path:
    override = os.environ.get("IMPACT_MAPPING_PATH", "").strip()
    return Path(override) if override else _knowledge_file("business-impact.yaml")


def formula_terms(formula: str) -> list[str] | None:
    """The input names of a product formula ("a * b * c"), or None if it is not a plain product."""
    terms = [t.strip() for t in formula.split("*")]
    return terms if terms and all(_IDENT.match(t) for t in terms) else None


@dataclass
class ImpactMapping:
    path: Path
    data: dict[str, Any]
    hash: str

    @property
    def id(self) -> str:
        return self.data["meta"]["id"]

    @property
    def version(self) -> str:
        return str(self.data["meta"]["version"])

    @property
    def categories(self) -> dict[str, dict]:
        return self.data["categories"]

    @property
    def stakeholders(self) -> dict[str, str]:
        return self.data["stakeholders"]

    @property
    def driver_kinds(self) -> dict[str, str]:
        return self.data["driver_kinds"]

    @property
    def cost_models(self) -> dict[str, dict]:
        return self.data["cost_models"]

    @property
    def high_severity_effects(self) -> set[str]:
        rules = self.data["severity_rules"]["high_requires_any_of"]
        return {e for r in rules for e in r.get("cited_step_effects_any", [])}

    @property
    def high_severity_from_context(self) -> bool:
        return any(r.get("basis") == "context" for r in self.data["severity_rules"]["high_requires_any_of"])

    def cost_models_for(self, categories: list[str]) -> list[str]:
        """Cost model ids that apply to any of `categories`, in file order."""
        wanted = set(categories)
        return [m for m, spec in self.cost_models.items() if wanted & set(spec["applies_to"])]

    def input_names(self) -> set[str]:
        return {i for spec in self.cost_models.values() for i in spec["inputs"]}


def validate(data: dict) -> list[str]:
    problems = []
    for key in ("id", "version"):
        if not (data.get("meta") or {}).get(key):
            problems.append(f"meta.{key} is missing")
    known_categories = set(get_args(ImpactCategory))
    known_stakeholders = set(get_args(Stakeholder))
    stakeholders = data.get("stakeholders") or {}
    if set(stakeholders) != known_stakeholders:
        problems.append(f"stakeholders must be exactly {sorted(known_stakeholders)}")
    if set(data.get("driver_kinds") or {}) != set(get_args(DriverKind)):
        problems.append(f"driver_kinds must be exactly {sorted(get_args(DriverKind))}")
    categories = data.get("categories") or {}
    if set(categories) != known_categories:
        problems.append(f"categories must be exactly {sorted(known_categories)}")
    for cid, spec in categories.items():
        if not spec.get("t"):
            problems.append(f"categories.{cid}: missing t")
        unknown = set(spec.get("requires") or {}) - REQUIRE_KEYS
        if unknown:
            problems.append(f"categories.{cid}: unknown requires keys {sorted(unknown)}")
        anchors = spec.get("anchors") or []
        if not anchors or set(anchors) - ANCHOR_KINDS:
            problems.append(f"categories.{cid}: anchors must be a non-empty subset of {sorted(ANCHOR_KINDS)}")
        bad = set(spec.get("stakeholders") or []) - known_stakeholders
        if bad:
            problems.append(f"categories.{cid}: unknown stakeholders {sorted(bad)}")
    for path, cats in (data.get("path_emphasis") or {}).items():
        bad = set(cats) - set(categories)
        if bad:
            problems.append(f"path_emphasis.{path}: unknown categories {sorted(bad)}")
    rules = (data.get("severity_rules") or {}).get("high_requires_any_of")
    if not rules:
        problems.append("severity_rules.high_requires_any_of is missing")
    for mid, spec in (data.get("cost_models") or {}).items():
        where = f"cost_models.{mid}"
        bad = set(spec.get("applies_to") or []) - set(categories)
        if bad or not spec.get("applies_to"):
            problems.append(f"{where}: applies_to must list known categories (unknown: {sorted(bad)})")
        inputs = spec.get("inputs") or {}
        terms = formula_terms(str(spec.get("formula", "")))
        if terms is None:
            problems.append(f"{where}: formula must be a product of input names (a * b * c)")
        elif sorted(terms) != sorted(inputs):
            problems.append(f"{where}: formula terms {terms} must be exactly its inputs {sorted(inputs)}")
        for name, inp in inputs.items():
            if not inp.get("unit") or not inp.get("question") or inp.get("ask") not in known_stakeholders:
                problems.append(f"{where}.inputs.{name}: needs unit, question and a known ask stakeholder")
            rng = inp.get("default_range")
            if rng is not None:
                if (not isinstance(rng, list) or len(rng) != 2 or not all(isinstance(v, (int, float)) for v in rng)
                        or rng[0] < 0 or rng[0] > rng[1]):
                    problems.append(f"{where}.inputs.{name}: default_range must be [low, high] with 0 <= low <= high")
                elif inp["unit"] == "fraction" and rng[1] > 1:
                    problems.append(f"{where}.inputs.{name}: a fraction's default_range must be within 0-1")
    return problems


def load_mapping(path: Path | None = None) -> ImpactMapping:
    path = path or default_mapping_path()
    if not path.exists():
        raise MappingError(f"business-impact mapping not found at {path}")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise MappingError(f"business-impact mapping {path} is not valid YAML: {e}") from e
    problems = validate(data)
    if problems:
        raise MappingError(f"business-impact mapping {path} is invalid:\n  " + "\n  ".join(problems[:20]))
    return ImpactMapping(path, data, hashlib.sha256(path.read_bytes()).hexdigest()[:16])


# --- signals from the process card ---------------------------------------------------

@dataclass
class ProcessSignals:
    effects: set[str]
    distinct_effects: int
    has_crash_loss: bool
    crash_stages: list[str]
    has_waits: bool
    best_path: str
    fact_ids: set[str]
    crash_fact_ids: set[str]
    wait_fact_ids: set[str]
    trigger_fact_ids: set[str]
    step_fact_ids_by_effect: dict[str, set[str]]
    facts: dict[str, dict] = field(default_factory=dict)  # fact id -> {label, text, refs}

    def anchor_ids(self, anchors: list[str]) -> set[str]:
        by_kind = {"crash_loss": self.crash_fact_ids, "waits": self.wait_fact_ids, "trigger": self.trigger_fact_ids}
        return set().union(*(by_kind[a] for a in anchors)) if anchors else set()


def derive_signals(process: dict) -> ProcessSignals:
    """What the card shows, for one process of the Temporal report's conversion section."""
    card = process["card"]
    effects = {e for s in card["steps"] for e in s["effects"]}
    by_effect: dict[str, set[str]] = {}
    crash_ids, wait_ids, trigger_ids, facts = set(), set(), set(), {}
    for fid, field_name, index, label, text, refs in card_facts(card):
        facts[fid] = {"label": label, "text": text, "refs": list(refs)}
        if field_name == "steps":
            for e in card["steps"][index]["effects"]:
                by_effect.setdefault(e, set()).add(fid)
        elif field_name == "crash_loss":
            crash_ids.add(fid)
        elif field_name == "waits" and text.strip() != NOT_ESTABLISHED:
            wait_ids.add(fid)
        elif field_name == "trigger" and text.strip() != NOT_ESTABLISHED:
            trigger_ids.add(fid)
    return ProcessSignals(
        effects=effects,
        distinct_effects=len(effects),
        has_crash_loss=bool(card["crash_loss"]),
        crash_stages=[c["stage"] for c in card["crash_loss"]],
        has_waits=bool(wait_ids),
        best_path=process.get("best_path", ""),
        fact_ids=set(facts),
        crash_fact_ids=crash_ids,
        wait_fact_ids=wait_ids,
        trigger_fact_ids=trigger_ids,
        step_fact_ids_by_effect=by_effect,
        facts=facts,
    )


def _requires_hold(req: dict, sig: ProcessSignals) -> bool:
    if "effects_any" in req and not (sig.effects & set(req["effects_any"])):
        return False
    if req.get("crash_loss") and not sig.has_crash_loss:
        return False
    if req.get("waits") and not sig.has_waits:
        return False
    if sig.distinct_effects < req.get("distinct_effects_min", 0):
        return False
    if "path" in req and sig.best_path != req["path"]:
        return False
    return True


def eligible_categories(sig: ProcessSignals, mapping: ImpactMapping) -> list[str]:
    """Categories the card supports: the best path's emphasis first, then file order."""
    ok = [c for c, spec in mapping.categories.items() if _requires_hold(spec.get("requires") or {}, sig)]
    lead = [c for c in mapping.data.get("path_emphasis", {}).get(sig.best_path, []) if c in ok]
    return lead + [c for c in ok if c not in lead]
