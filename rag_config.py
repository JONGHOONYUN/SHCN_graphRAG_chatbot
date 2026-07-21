"""Single source of truth for vector-index configuration.

Historically both `tools/vector.py` and `text_rag.py` defined their own
`INDEX_BY_LANG` dict. Any change had to be applied twice, and drift was
inevitable. Phase 6 consolidates the mapping here — both modules re-export
`INDEX_BY_LANG` from this module, so a new language or a renamed index is
edited exactly once.

Fields:
  index_name         — the Neo4j vector index to query
  text_property      — the Entry property whose text is stored in the index
  embedding_property — the Entry property that stores the vector

textRAG uses a lightweight retrieval query focused on citation metadata;
graphRAG uses an enriched retrieval query pulling contained poems, mentioned
entities, and Poetry Talks links. The retrieval-query BUILDERS are kept in
their respective modules (they diverge intentionally), but the INDEX config
is shared here.
"""

from __future__ import annotations

from typing import Dict, Mapping


INDEX_BY_LANG: Dict[str, Mapping[str, str]] = {
    "ko": {
        "index_name":         "EntryTextsKor",
        "text_property":      "textKor",
        "embedding_property": "textEmbedding_Kor",
    },
    "en": {
        "index_name":         "EntryTextsEng",
        "text_property":      "textEng",
        "embedding_property": "textEmbedding_Eng",
    },
    "zh": {
        "index_name":         "EntryTextsChi",
        "text_property":      "textChi",
        "embedding_property": "textEmbedding_Chi",
    },
}


def index_config_for(lang: str) -> Mapping[str, str]:
    """Return the index config for `lang`, falling back to Korean.

    Callers used to inline the fallback (`INDEX_BY_LANG.get(lang, INDEX_BY_LANG["ko"])`)
    at several sites; consolidating it here keeps the fallback policy
    consistent."""
    return INDEX_BY_LANG.get(lang) or INDEX_BY_LANG["ko"]
