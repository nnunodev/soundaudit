"""Metadata resolvers (MusicBrainz, AcoustID, …)."""

from __future__ import annotations

from soundaudit.resolver.musicbrainz import (
    AcoustidLookupClient,
    MusicBrainzClient,
    MusicBrainzResolver,
    ResolvedMetadata,
)

__all__ = [
    "AcoustidLookupClient",
    "MusicBrainzClient",
    "MusicBrainzResolver",
    "ResolvedMetadata",
]
