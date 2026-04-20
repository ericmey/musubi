import re

with open("src/musubi/retrieve/orchestration.py") as f:
    text = f.read()

# Fix Err and Ok constructors
text = re.sub(r"return Err\((.*?)\)", r"return Err(error=\1)", text)
text = re.sub(r"return Ok\((.*?)\)", r"return Ok(value=\1)", text)

# Fix res assignments and typing
text = text.replace("res = await asyncio.wait_for(", "blended_res = await asyncio.wait_for(")
text = text.replace("if isinstance(res, Err):", "if isinstance(blended_res, Err):")
text = text.replace(
    'return Err(error=RetrievalError(kind="internal", detail=res.error.detail))',
    'return Err(error=RetrievalError(kind="internal", detail=blended_res.error.detail))',
)
text = text.replace(
    "warnings.extend(res.value.warnings)", "warnings.extend(blended_res.value.warnings)"
)
text = text.replace(
    'return Ok(value=_pack_scored_hits(res.value.results, warnings, include_payload=not getattr(query, "brief", False)))',
    'return Ok(value=_pack_scored_hits(blended_res.value.results, warnings, include_payload=not getattr(query, "brief", False)))',
)

text = text.replace(
    "res = await asyncio.wait_for(\n                run_deep_retrieve(",
    "deep_res = await asyncio.wait_for(\n                run_deep_retrieve(",
)
text = text.replace("if isinstance(deep_res, Err):", "if isinstance(deep_res, Err):")
text = text.replace(
    'return Err(error=RetrievalError(kind="internal", detail=deep_res.error.detail))',
    'return Err(error=RetrievalError(kind="internal", detail=deep_res.error.detail))',
)
text = text.replace(
    'return Ok(value=_pack_scored_hits(deep_res.value, warnings, include_payload=not getattr(query, "brief", False)))',
    'return Ok(value=_pack_scored_hits(deep_res.value, warnings, include_payload=not getattr(query, "brief", False)))',
)
text = text.replace(
    "if isinstance(res, Err):", "if isinstance(deep_res, Err):"
)  # second pass for deep

text = text.replace(
    "res = await asyncio.wait_for(\n                run_fast_retrieve(",
    "fast_res = await asyncio.wait_for(\n                run_fast_retrieve(",
)
text = text.replace(
    "if isinstance(deep_res, Err):", "if isinstance(fast_res, Err):"
)  # wait, this is replacing too many.
