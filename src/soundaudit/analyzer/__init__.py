"""Audio analysis modules: duplicates, transcodes, quality."""

from soundaudit.analyzer.acoustid import (
    AcoustidDuplicateAnalyzer,
    AcoustidGroupVerdict,
    DupType,
    analyze_acoustid_keepers,
    find_acoustid_groups,
    write_acoustid_groups,
)
from soundaudit.analyzer.duplicates import (
    DuplicateAnalyzer,
    DuplicateGroupResult,
    find_duplicate_groups,
    write_duplicate_groups,
)

__all__ = [
    "AcoustidDuplicateAnalyzer",
    "AcoustidGroupVerdict",
    "DupType",
    "analyze_acoustid_keepers",
    "find_acoustid_groups",
    "write_acoustid_groups",
    "DuplicateAnalyzer",
    "DuplicateGroupResult",
    "find_duplicate_groups",
    "write_duplicate_groups",
]
