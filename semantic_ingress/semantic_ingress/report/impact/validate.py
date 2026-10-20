"""
Checks on the model's business-impact answers (V1-V12 of the design).

As in writeup.py: `*_problems` lists what is wrong, for one retry with the
problems spelled out; `clean_*` then keeps what holds and drops the rest, each
drop recorded in `ImpactGrounding.dropped` (shown to readers as "what was
filtered out"). Content problems never raise.

The business-case cap (V8) needs the Temporal verdict and is applied in
translate.py.
"""
import re
from dataclasses import dataclass, field

import structlog

from semantic_ingress.report.impact.mapping import ImpactMapping, ProcessSignals
from semantic_ingress.report.impact.models import (
    Driver,
    FailureStory,
    ImpactSummaryDraft,
    ProcessImpactDraft,
    Workaround,
)

logger = structlog.get_logger()

MAX_STORIES = 3
MAX_DRIVERS = 5
MAX_QUESTIONS = 5
MAX_WHO = 3
WORDS = {"scenario": 45, "workaround": 25, "driver": 25, "case_reason": 40, "question": 30, "why": 20,
         "headline": 40, "exec_summary": 120}

# V5: figures in prose. A heuristic: it misses numbers written as words, so the prompt asks too.
_PROSE_RULES = [
    ("currency", re.compile(r"[$€£¥₹]|\b(?:USD|EUR|GBP|JPY|INR|CAD|AUD|CHF)\b|\b(?:dollars?|euros?|bucks)\b", re.I)),
    ("percentage", re.compile(r"\d\s*%|\bper\s?cent\b|\bpercentage\b", re.I)),
    ("rate", re.compile(r"\b\d[\d,.]*\s*(?:k|m|bn)?\s*(?:/|per\b|an?\b|each\b)\s*"
                        r"(?:second|minute|hour|day|week|month|year|run|call|request|incident)s?\b", re.I)),
    ("magnitude", re.compile(r"\b\d+(?:\.\d+)?\s*(?:k|m|bn|thousand|million|billion)\b|\b\d{1,3}(?:,\d{3})+\b"
                             r"|\b\d{3,}\b|\b\d+x\b", re.I)),
]
_NUMBER = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|m|bn|thousand|million|billion|%)?(?![a-z])", re.I)
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "bn": 1e9, "billion": 1e9}


def words(text: str) -> int:
    return len(text.split())


def prose_problem(text: str) -> str:
    """The V5 rule `text` breaks ("currency", "percentage", "rate", "magnitude"), or ""."""
    for name, pattern in _PROSE_RULES:
        if pattern.search(text):
            return name
    return ""


def numbers_in(text: str) -> set[float]:
    """Numbers a text states, scaled by their suffix; a percentage also as a fraction."""
    found = set()
    for digits, suffix in _NUMBER.findall(text):
        value = float(digits.replace(",", ""))
        suffix = suffix.lower()
        if suffix == "%":
            found |= {value, value / 100}
        else:
            found.add(value * _SCALE.get(suffix, 1))
    return found


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


@dataclass
class ImpactGrounding:
    """What one process's answer may cite and choose, and what was dropped from it."""

    process: str
    sig: ProcessSignals
    eligible: list[str]
    items: dict[str, str]               # context id -> text
    mapping: ImpactMapping
    dropped: list[str] = field(default_factory=list)

    @property
    def context_ids(self) -> set[str]:
        return set(self.items)

    def drop(self, text: str) -> None:
        self.dropped.append(f"{self.process}: {text}")

    def prose(self, where: str, text: str) -> str:
        """V5 and the rule's name for logging (never the text: it may carry customer context)."""
        rule = prose_problem(text)
        if rule:
            logger.info("impact_prose_rejected", process=self.process, field=where, rule=rule)
        return rule

    def cost_model_ok(self, model_id: str, categories: list[str]) -> bool:
        return model_id == "" or model_id in self.mapping.cost_models_for(categories)

    def question_fills(self) -> set[str]:
        return set(self.mapping.driver_kinds) | self.mapping.input_names()


# --- per-item checks (shared by problems and clean) -------------------------------------

def _story_issues(s: FailureStory, g: ImpactGrounding) -> list[str]:
    issues = []
    if s.category not in g.eligible:
        issues.append(f"category {s.category!r} is not allowed for this process (allowed: {', '.join(g.eligible)})")
        return issues
    anchors = g.mapping.categories[s.category]["anchors"]
    fids = [c for c in s.cites if not c.startswith("C")]
    bad = [c for c in fids if c not in g.sig.fact_ids] + [c for c in s.cites if c.startswith("C") and c not in g.context_ids]
    if bad:
        issues.append(f"cites {bad} are not card fact ids or context ids")
    anchor_ids = g.sig.anchor_ids(anchors)
    if not (set(fids) & anchor_ids):
        kinds = " or ".join(a.replace("_", "-") for a in anchors)
        issues.append(f"must cite at least one {kinds} fact ({', '.join(sorted(anchor_ids, key=_fid)) or 'none on the card'})")
    stage_ok = s.stage in g.sig.crash_stages or (s.stage == "" and _stage_optional(anchors))
    if not stage_ok:
        issues.append(f"stage {s.stage!r} is not one of the crash stages")
    if not 1 <= len(s.who_feels_it) <= MAX_WHO:
        issues.append(f"who_feels_it needs 1 to {MAX_WHO} stakeholders")
    if words(s.scenario) > WORDS["scenario"]:
        issues.append(f"scenario has {words(s.scenario)} words (at most {WORDS['scenario']})")
    rule = prose_problem(s.scenario)
    if rule:
        issues.append(f"scenario contains a figure ({rule}); no numbers, currency or percentages in prose")
    if s.severity == "high" and not _high_ok(s, g):
        issues.append("severity high needs a cited step with a paid_call or irreversible effect, or a cited context item")
    return issues


def _stage_optional(anchors: list[str]) -> bool:
    """A story led by a wait or schedule fact may have no crash stage; crash-loss stories need one."""
    return anchors[0] != "crash_loss"


def _fid(f: str) -> int:
    return int(f[1:]) if f[1:].isdigit() else 0


def _high_ok(s: FailureStory, g: ImpactGrounding) -> bool:
    cited = set(s.cites)
    if any(cited & g.sig.step_fact_ids_by_effect.get(e, set()) for e in g.mapping.high_severity_effects):
        return True
    return g.mapping.high_severity_from_context and bool(cited & g.context_ids)


def _basis_issues(basis: str, cites: list[str], g: ImpactGrounding) -> list[str]:
    """V3."""
    if basis == "assumption":
        return [] if not cites else ["an assumption has no citations"]
    if not cites:
        return [f"basis {basis} needs citations"]
    valid = g.sig.fact_ids if basis == "code" else g.context_ids
    bad = [c for c in cites if c not in valid]
    kind = "card fact ids (F...)" if basis == "code" else "context ids (C...)"
    return [f"basis {basis} must cite {kind}; invalid: {bad}"] if bad else []


def _input_issues(name: str, low: float, high: float, cite: str, model_id: str, g: ImpactGrounding) -> list[str]:
    """V9, one cost input."""
    from semantic_ingress.report.impact.cost import value_ok

    spec = g.mapping.cost_models.get(model_id)
    if spec is None or name not in spec["inputs"]:
        return [f"cost input {name!r} is not an input of cost model {model_id!r}"]
    if cite not in g.context_ids:
        return [f"cost input {name!r} cites {cite!r}, which is not a context id"]
    if not value_ok(spec["inputs"][name]["unit"], low, high):
        return [f"cost input {name!r}: values must be non-negative, low <= high, and within 0-1 for a fraction"]
    stated = numbers_in(g.items[cite])
    if not stated:
        return [f"cost input {name!r}: {cite} states no number"]
    if not all(any(_same(v, n) for n in stated) for v in (low, high)):
        return [f"cost input {name!r}: {low}-{high} is not a number {cite} states; copy numbers, do not compute them"]
    return []


def _question_issues(question: str, why: str, fills: str, g: ImpactGrounding) -> list[str]:
    issues = []
    if fills not in g.question_fills():
        issues.append(f"fills {fills!r} is not a driver kind or a cost input name")
    if words(question) > WORDS["question"]:
        issues.append(f"question has {words(question)} words (at most {WORDS['question']})")
    if words(why) > WORDS["why"]:
        issues.append(f"why has {words(why)} words (at most {WORDS['why']})")
    if prose_problem(question) or prose_problem(why):
        issues.append("contains a figure; no numbers, currency or percentages in prose")
    return issues


# --- process answers --------------------------------------------------------------

def process_problems(d: ProcessImpactDraft, g: ImpactGrounding) -> list[str]:
    """Everything wrong with one process's answer, for the retry."""
    problems = []
    if d.process != g.process:
        problems.append(f"process must be {g.process!r} exactly (V1)")
    if not d.stories:
        problems.append("at least one story is required")
    if len(d.stories) > MAX_STORIES:
        problems.append(f"at most {MAX_STORIES} stories")
    for i, s in enumerate(d.stories, 1):
        problems += [f"story {i}: {x}" for x in _story_issues(s, g)]
    w = d.workaround
    problems += [f"workaround: {x}" for x in _basis_issues(w.basis, w.cites, g)]
    if words(w.text) > WORDS["workaround"]:
        problems.append(f"workaround has {words(w.text)} words (at most {WORDS['workaround']})")
    if prose_problem(w.text):
        problems.append("workaround contains a figure; no numbers, currency or percentages in prose")
    if len(d.drivers) > MAX_DRIVERS:
        problems.append(f"at most {MAX_DRIVERS} drivers")
    for i, dr in enumerate(d.drivers, 1):
        problems += [f"driver {i}: {x}" for x in _basis_issues(dr.basis, dr.cites, g)]
        if words(dr.text) > WORDS["driver"]:
            problems.append(f"driver {i} has {words(dr.text)} words (at most {WORDS['driver']})")
        if prose_problem(dr.text):
            problems.append(f"driver {i} contains a figure; no numbers, currency or percentages in prose")
    categories = [s.category for s in d.stories if s.category in g.eligible]
    if not g.cost_model_ok(d.cost_model, categories):
        allowed = ", ".join(g.mapping.cost_models_for(categories)) or "none"
        problems.append(f"cost_model {d.cost_model!r} does not apply to the stories' categories (allowed: {allowed})")
    else:
        for c in d.cost_inputs:
            problems += _input_issues(c.name, c.low, c.high, c.cite, d.cost_model, g)
    if words(d.case_reason) > WORDS["case_reason"]:
        problems.append(f"case_reason has {words(d.case_reason)} words (at most {WORDS['case_reason']})")
    if prose_problem(d.case_reason):
        problems.append("case_reason contains a figure; no numbers, currency or percentages in prose")
    if len(d.questions) > MAX_QUESTIONS:
        problems.append(f"at most {MAX_QUESTIONS} questions")
    for i, q in enumerate(d.questions, 1):
        problems += [f"question {i}: {x}" for x in _question_issues(q.question, q.why, q.fills, g)]
    return problems


def _clean_story(s: FailureStory, g: ImpactGrounding) -> FailureStory | None:
    where = f"story {s.scenario[:60]!r}"
    if s.category not in g.eligible:
        g.drop(f"{where} dropped (category {s.category} is not supported by the card)")
        return None
    s = s.model_copy(deep=True)
    valid = [c for c in dict.fromkeys(s.cites)
             if (c in g.context_ids if c.startswith("C") else c in g.sig.fact_ids)]
    if len(valid) != len(set(s.cites)):
        g.drop(f"{where}: citations dropped ({', '.join(c for c in s.cites if c not in valid)})")
    s.cites = valid
    anchors = g.mapping.categories[s.category]["anchors"]
    if not (set(valid) & g.sig.anchor_ids(anchors)):
        g.drop(f"{where} dropped (no valid citation of a {' or '.join(anchors)} fact)")
        return None
    if s.stage not in g.sig.crash_stages:
        match = [c for c in g.sig.crash_stages if _norm(c) == _norm(s.stage)]
        if match:
            s.stage = match[0]
        elif not (s.stage == "" and _stage_optional(anchors)):
            g.drop(f"{where} dropped (stage {s.stage!r} is not a crash stage on the card)")
            return None
    if words(s.scenario) > WORDS["scenario"]:
        g.drop(f"{where} dropped (over {WORDS['scenario']} words)")
        return None
    rule = g.prose("scenario", s.scenario)
    if rule:
        g.drop(f"{where} dropped (states a figure: {rule})")
        return None
    who = list(dict.fromkeys(s.who_feels_it))
    if not who:
        who = list(g.mapping.categories[s.category]["stakeholders"])
        g.drop(f"{where}: no stakeholders given; the category's defaults are used")
    if len(who) > MAX_WHO:
        g.drop(f"{where}: stakeholders beyond {MAX_WHO} dropped")
    s.who_feels_it = who[:MAX_WHO]
    if s.severity == "high" and not _high_ok(s, g):
        s.severity = "medium"
        g.drop(f"{where}: severity high lowered to medium (no cited paid or irreversible step, and no context)")
    return s


def _clean_basis(item: Workaround | Driver, g: ImpactGrounding, where: str) -> bool:
    """Keep an item whose basis holds; an assumption only loses stray citations."""
    if item.basis == "assumption":
        if item.cites:
            g.drop(f"{where}: citations removed from an assumption")
            item.cites = []
        return True
    issues = _basis_issues(item.basis, item.cites, g)
    if issues:
        g.drop(f"{where} dropped ({issues[0]})")
        return False
    item.cites = list(dict.fromkeys(item.cites))
    return True


def clean_process(d: ProcessImpactDraft, g: ImpactGrounding) -> ProcessImpactDraft:
    """The answer with every check applied: what fails is dropped (and recorded) or corrected."""
    d = d.model_copy(deep=True)
    if d.process != g.process:
        g.drop(f"answer named the process {d.process!r}; corrected")
        d.process = g.process

    stories = [s for s in (_clean_story(s, g) for s in d.stories) if s is not None]
    if len(stories) > MAX_STORIES:
        g.drop(f"{len(stories) - MAX_STORIES} stories beyond {MAX_STORIES} dropped")
    d.stories = stories[:MAX_STORIES]

    w, none = d.workaround, Workaround(text="", basis="assumption", cites=[])
    if not w.text.strip():
        d.workaround = none
    elif words(w.text) > WORDS["workaround"] or g.prose("workaround", w.text):
        g.drop(f"workaround dropped (over {WORDS['workaround']} words, or states a figure): {w.text}")
        d.workaround = none
    elif not _clean_basis(w, g, f"workaround {w.text[:60]!r}"):
        d.workaround = none

    drivers = []
    for dr in d.drivers:
        where = f"driver {dr.text[:60]!r}"
        if words(dr.text) > WORDS["driver"]:
            g.drop(f"{where} dropped (over {WORDS['driver']} words)")
        elif g.prose("driver", dr.text):
            g.drop(f"{where} dropped (states a figure)")
        elif _clean_basis(dr, g, where):
            drivers.append(dr)
    if len(drivers) > MAX_DRIVERS:
        g.drop(f"{len(drivers) - MAX_DRIVERS} drivers beyond {MAX_DRIVERS} dropped")
    d.drivers = drivers[:MAX_DRIVERS]

    categories = [s.category for s in d.stories]
    if not g.cost_model_ok(d.cost_model, categories):
        g.drop(f"cost model {d.cost_model!r} dropped (does not apply to the stories)")
        d.cost_model, d.cost_inputs = "", []
    inputs, seen = [], set()
    for c in d.cost_inputs:
        issues = _input_issues(c.name, c.low, c.high, c.cite, d.cost_model, g)
        if c.name in seen:
            issues = [f"cost input {c.name!r} given twice"]
        if issues:
            g.drop(f"{issues[0]}; dropped")
            continue
        seen.add(c.name)
        inputs.append(c)
    d.cost_inputs = inputs

    if words(d.case_reason) > WORDS["case_reason"] or g.prose("case_reason", d.case_reason):
        g.drop(f"case reason dropped: {d.case_reason}")
        d.case_reason = ""

    questions, seen_q = [], set()
    for q in d.questions:
        issues = _question_issues(q.question, q.why, q.fills, g)
        key = _norm(q.question)
        if key in seen_q:
            issues = ["duplicate"]
        if issues:
            g.drop(f"question {q.question[:60]!r} dropped ({issues[0]})")
            continue
        seen_q.add(key)
        questions.append(q)
    if len(questions) > MAX_QUESTIONS:
        g.drop(f"{len(questions) - MAX_QUESTIONS} questions beyond {MAX_QUESTIONS} dropped")
    d.questions = questions[:MAX_QUESTIONS]
    return d


# --- summary (V11) ------------------------------------------------------------------

def summary_problems(s: ImpactSummaryDraft, names: set[str]) -> list[str]:
    problems = [f"mentions {m!r}, which is not a listed process name" for m in s.mentions if m not in names]
    for name in ("headline", "exec_summary"):
        text = getattr(s, name)
        if not text.strip():
            problems.append(f"{name} is empty")
        if words(text) > WORDS[name]:
            problems.append(f"{name} has {words(text)} words (at most {WORDS[name]})")
        if prose_problem(text):
            problems.append(f"{name} contains a figure; no numbers, currency or percentages")
    return problems
