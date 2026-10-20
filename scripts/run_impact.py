#!/usr/bin/env python3
"""
Run business-impact translation in-process on a stored Temporal report and print
the views (exec, champion, discovery). Nothing is stored except the model-call
cache, so a rerun with the same inputs makes no model calls.

Against an index in MongoDB (inside the compose network; needs an image built
with this checkout, `docker compose build`):

    docker compose run --rm -v "$PWD/scripts:/app/scripts:ro" --entrypoint python \\
        indexer /app/scripts/run_impact.py --repo my-service --text "Finance reruns failed batches by hand"

Against an exported report document (from a checkout; the cache is a JSON file):

    python scripts/run_impact.py --report-json report.json --cache impact-cache.json --view exec

Needs ANTHROPIC_API_KEY. Uses INDEX_MODEL and the other INDEX_* settings, as the report does.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "semantic_ingress"))

from semantic_ingress.config import LLMConfig, MongoConfig  # noqa: E402
from semantic_ingress.core.llm_client import LLMClient  # noqa: E402
from semantic_ingress.report.impact.context import ImpactContext  # noqa: E402
from semantic_ingress.report.impact.mapping import load_mapping  # noqa: E402
from semantic_ingress.report.impact.render import VIEWS, render  # noqa: E402
from semantic_ingress.report.impact.translate import ImpactUnavailable, build_business_impact  # noqa: E402


class FileStore:
    """A stored report from a JSON file, and the report cache in another JSON file."""

    def __init__(self, report: Path, cache: Path | None):
        self.report = json.loads(report.read_text())
        self.cache_path = cache
        self.cache = json.loads(cache.read_text()) if cache and cache.exists() else {}

    def get_report(self, repo_id):
        return self.report

    def get_cached(self, repo_id, key):
        return self.cache.get(key)

    def put_cached(self, repo_id, key, value):
        self.cache[key] = {"_id": key, **value}
        if self.cache_path:
            self.cache_path.write_text(json.dumps(self.cache))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo", help="index name (reads the report from MongoDB, MONGO_URL)")
    source.add_argument("--report-json", type=Path, help="an exported report document (JSON)")
    parser.add_argument("--cache", type=Path, help="with --report-json: JSON file for the model-call cache")
    parser.add_argument("--prefix", default="semv2", help="database prefix (DB_PREFIX)")
    parser.add_argument("--context-json", type=Path, help="the context as JSON (ImpactContext fields)")
    parser.add_argument("--text", default="", help="call notes, pain points (free text)")
    parser.add_argument("--industry", default="")
    parser.add_argument("--volumes", default="")
    parser.add_argument("--pain-points", default="")
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--hourly-rate", type=float, default=0)
    parser.add_argument("--view", choices=[*VIEWS, "all"], default="all")
    parser.add_argument("--fresh", action="store_true", help="skip cached answers (still writes them)")
    parser.add_argument("--out-json", type=Path, help="also write the impact data here")
    args = parser.parse_args()

    if args.context_json:
        ctx = ImpactContext.model_validate_json(args.context_json.read_text())
    else:
        ctx = ImpactContext(text=args.text, industry=args.industry, volumes=args.volumes,
                            pain_points=args.pain_points, currency=args.currency, hourly_rate=args.hourly_rate)
    if args.repo:
        from semantic_ingress.storage import MongoStore

        store, repo_id = MongoStore(MongoConfig(), args.prefix), args.repo
    else:
        store = FileStore(args.report_json, args.cache)
        repo_id = store.report.get("repo_id") or "repo"

    llm = LLMClient(LLMConfig())
    try:
        result = build_business_impact(llm, store, repo_id, ctx, load_mapping(), fresh=args.fresh)
    except ImpactUnavailable as e:
        print(f"Cannot translate this report: {e}", file=sys.stderr)
        return 2
    data = result.data
    data.setdefault("repo_id", repo_id)
    if args.out_json:
        args.out_json.write_text(json.dumps(data, indent=2))
    views = render(data)
    for name in VIEWS if args.view == "all" else [args.view]:
        print(f"\n{'=' * 30} {name.upper()} {'=' * 30}\n")
        print(views[name])
    print(f"Model calls: {result.calls}; cost ${data['cost_usd']:.4f}; "
          f"{len(data['processes'])} translated, {len(data['skipped'])} skipped, {len(data['dropped'])} items filtered",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
