"""
Cost ranges, computed in code from a cost model in the mapping. No model involved.

Each input of the model's formula comes from the first source that has it: a
number the context states (validated in validate.py, basis "context"), the
structured hourly rate (basis "context"), or the mapping's default range
(basis "default"). If any input has none, there is no estimate, and the missing
inputs become discovery questions. A formula is a product of non-negative
terms, so the low end is the product of the lows and the high end of the highs.
"""
from semantic_ingress.report.impact.context import ImpactContext, hourly_rate_id, numbered
from semantic_ingress.report.impact.mapping import ImpactMapping, formula_terms
from semantic_ingress.report.impact.models import CostInput

LABEL = "Illustrative estimate from the stated inputs. Not a prediction."
PERIOD = "per_month"


def value_ok(unit: str, low: float, high: float) -> bool:
    """Non-negative, ordered, and within 0-1 for a fraction."""
    if low < 0 or high < 0 or low > high:
        return False
    return not (unit == "fraction" and high > 1)


def evaluate(formula: str, values: dict[str, float]) -> float:
    """A product formula over `values`. Anything that is not a plain product is refused."""
    terms = formula_terms(formula)
    if terms is None:
        raise ValueError(f"not a product formula: {formula!r}")
    result = 1.0
    for t in terms:
        result *= values[t]
    return result


def compute_cost(model_id: str, supplied: list[CostInput], ctx: ImpactContext,
                 mapping: ImpactMapping) -> tuple[dict | None, list[str]]:
    """(the estimate, or None; the names of the inputs that had no value)."""
    spec = mapping.cost_models[model_id]
    given = {c.name: c for c in supplied}
    rate_cite = hourly_rate_id(numbered(ctx), ctx)
    rows, missing = [], []
    for name, inp in spec["inputs"].items():
        unit = inp["unit"]
        c = given.get(name)
        if c is not None and value_ok(unit, c.low, c.high):
            rows.append({"name": name, "unit": unit, "low": c.low, "high": c.high, "basis": "context", "cite": c.cite})
        elif name == "hourly_rate" and rate_cite:
            rows.append({"name": name, "unit": unit, "low": ctx.hourly_rate, "high": ctx.hourly_rate,
                         "basis": "context", "cite": rate_cite})
        elif inp.get("default_range") is not None:
            low, high = (float(v) for v in inp["default_range"])
            rows.append({"name": name, "unit": unit, "low": low, "high": high, "basis": "default", "cite": ""})
        else:
            missing.append(name)
    if missing:
        return None, missing
    low = evaluate(spec["formula"], {r["name"]: r["low"] for r in rows})
    high = evaluate(spec["formula"], {r["name"]: r["high"] for r in rows})
    return {
        "model": model_id, "t": spec["t"], "formula": spec["formula"], "inputs": rows,
        "low": round(low, 2), "high": round(high, 2), "period": PERIOD, "currency": ctx.currency,
        "label": LABEL,
    }, []
