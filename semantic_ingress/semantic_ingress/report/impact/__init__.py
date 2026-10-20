"""
Business-impact translation of a stored Temporal report, on demand.

    models.py     response models of the model calls
    mapping.py    knowledge/business-impact.yaml; signals and categories from a card
    context.py    the customer context, numbered C1..Cn
    prompts.py    system and user prompts
    validate.py   checks on the answers (V1-V12) and the clean-up
    cost.py       cost ranges, computed in code
    translate.py  prepare -> translate_process -> synthesize; build_business_impact
    render.py     the exec, champion and discovery views as Markdown

Not part of the indexing pipeline or the report build: it only reads the stored
report, and writes its own document and cache entries.
"""
