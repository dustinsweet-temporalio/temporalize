from semantic_ingress.core.llm_schema import to_llm_schema
from semantic_ingress.lens.models import FileLens, FileSummaryWithLens, FolderSummaryWithLens, RepoLens
from semantic_ingress.lens.serialize import compact_file_lens


def test_compact_line_matches_calibrated_format():
    rec = FileLens(
        rel=3, cov="full", kind=["ORCH", "ACT"],
        sig=["fanout.parallel:thread pool@L202-245", "effect.llm:summaries@L762", "trigger.cli@L90-173"],
        sym=[{"n": "GenerationOrchestrator.sync", "k": "ORCH"}],
        c=["X-THREAD-FANOUT", "X-CHECKPOINT"], f=[],
        n="Runs the index.",
    )
    assert compact_file_lens(rec) == (
        "rel3 ORCH+ACT | sig fanout.parallel:thread_pool effect.llm:summaries trigger.cli"
        " | sym sync:ORCH | c X-THREAD-FANOUT X-CHECKPOINT | Runs the index."
    )


def test_compact_line_lists_each_finding_once_without_anchors():
    rec = FileLens(rel=3, cov="full", kind=["TWF"], sig=[], sym=[], c=[],
                   f=["E-AO-STC@L29", "E-RP-MAX1@L48"], n="x")
    assert "| f E-AO-STC E-RP-MAX1 |" in compact_file_lens(rec)


def _objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            yield node
        for v in node.values():
            yield from _objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from _objects(v)


def test_llm_schemas_meet_structured_output_rules():
    for model in [FileSummaryWithLens, FolderSummaryWithLens, RepoLens]:
        schema = to_llm_schema(model)
        text = str(schema)
        for banned in ["minLength", "maximum", "minimum", "'default'"]:
            assert banned not in text, (model.__name__, banned)
        for obj in _objects(schema):
            assert obj["additionalProperties"] is False
            assert set(obj["required"]) == set(obj["properties"])


def test_folder_schema_excludes_computed_subtree():
    schema = to_llm_schema(FolderSummaryWithLens)
    assert "SubtreeStats" not in schema.get("$defs", {})
    assert all("subtree" not in obj["properties"] for obj in _objects(schema))
    # No free-form maps survive (the API cannot take them).
    assert all(obj["properties"] for obj in _objects(schema))


def test_fields_named_like_schema_keywords_are_kept():
    from pydantic import BaseModel

    class Item(BaseModel):
        title: str
        default: str
        pattern: str

    class Outer(BaseModel):
        items: list[Item]
        title: str

    schema = to_llm_schema(Outer)
    assert schema["required"] == ["items", "title"] and "title" in schema["properties"]
    item = schema["$defs"]["Item"]
    assert item["required"] == ["title", "default", "pattern"]
    assert "title" not in item["properties"]["title"]  # the keyword is still stripped from each field's schema
