"""Audio analysis modules: duplicates, transcodes, quality."""

from soundaudit.analyzer.duplicates import (
    DuplicateAnalyzer,
    DuplicateGroupResult,
    find_duplicate_groups,
    write_duplicate_groups,
)

__all__ = [
    "DuplicateAnalyzer",
    "DuplicateGroupResult",
    "find_duplicate_groups",
    "write_duplicate_groups",
]
