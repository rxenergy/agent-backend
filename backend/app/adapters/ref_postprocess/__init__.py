"""ref_postprocess — chunking.postprocess 이식본.

agent-backend 내부에서 자족적으로 동작한다 (외부 chunking 패키지 의존 없음).
"""

from .ref_catalog import (
    RefCatalog,
    build_case_reference_index_from_catalog,
    build_report_number_index_from_catalog,
    load_or_build_catalog,
)
from .ref_extractor_rule import (
    FollowUpQuery,
    RawRef,
    extract_refs_with_follow_up,
    resolve_text_with_follow_up,
)
from .ref_resolver import (
    RefResolver,
    build_source_id_filter,
)
from .settings import RefSettings

__all__ = [
    "RefCatalog",
    "RefResolver",
    "RefSettings",
    "RawRef",
    "FollowUpQuery",
    "build_case_reference_index_from_catalog",
    "build_report_number_index_from_catalog",
    "build_source_id_filter",
    "extract_refs_with_follow_up",
    "load_or_build_catalog",
    "resolve_text_with_follow_up",
]
