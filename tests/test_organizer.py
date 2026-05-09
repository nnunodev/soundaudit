"""Tests for organizer featured-artist stripping and album_artist normalization."""

from pathlib import Path

import pytest

from soundaudit.organizer import (
    _apply_template,
    _is_compilation_group,
    _normalize_album_artist,
    _strip_featured_artists,
    plan_organization,
    OrganizePlan,
)
from soundaudit.models import TrackTags


class TestStripFeaturedArtists:
    def test_feat_suffix(self):
        assert _strip_featured_artists("Kendrick Lamar feat. Drake") == "Kendrick Lamar"

    def test_ft_suffix(self):
        assert _strip_featured_artists("Kendrick Lamar ft. Drake") == "Kendrick Lamar"

    def test_featuring_suffix(self):
        assert _strip_featured_artists("Kendrick Lamar featuring Drake") == "Kendrick Lamar"

    def test_parenthesized_feat(self):
        assert _strip_featured_artists("Kendrick Lamar (feat. Drake)") == "Kendrick Lamar"

    def test_bracketed_ft(self):
        assert _strip_featured_artists("Kendrick Lamar [ft. Drake]") == "Kendrick Lamar"

    def test_no_feature(self):
        assert _strip_featured_artists("Kendrick Lamar") == "Kendrick Lamar"

    def test_empty_string(self):
        assert _strip_featured_artists("") == ""

    def test_multiple_spaces_collapsed(self):
        assert _strip_featured_artists("Artist  feat.  Guest") == "Artist"


class TestNormalizeAlbumArtist:
    def test_explicit_album_artist_unchanged(self):
        assert _normalize_album_artist("Artist A", None) == "Artist A"

    def test_compilation_alias_normalized(self):
        assert _normalize_album_artist("VA", None) == "Various Artists"
        assert _normalize_album_artist("V.A.", None) == "Various Artists"
        assert _normalize_album_artist("Various", None) == "Various Artists"

    def test_fallback_strips_featured(self):
        assert _normalize_album_artist(None, "Artist A feat. Guest") == "Artist A"

    def test_fallback_no_strip_when_disabled(self):
        assert (
            _normalize_album_artist(None, "Artist A feat. Guest", strip_featured=False)
            == "Artist A feat. Guest"
        )

    def test_unknown_when_both_missing(self):
        assert _normalize_album_artist(None, None) == "Unknown Artist"

    def test_custom_compilation_names(self):
        names = {"custom comp"}
        assert _normalize_album_artist("Custom Comp", None, compilation_names=names) == "Various Artists"

    def test_empty_whitespace_treated_as_none(self):
        assert _normalize_album_artist("", None) == "Unknown Artist"
        assert _normalize_album_artist("   ", None) == "Unknown Artist"
        assert _normalize_album_artist("\t", "Artist A") == "Artist A"


class TestIsCompilationGroup:
    def test_explicit_compilation_flag(self):
        plan = OrganizePlan(source=Path("a.flac"), proposed=Path("out"))
        plan.tags.compilation = True
        assert _is_compilation_group([plan]) is True

    def test_single_artist_not_compilation(self):
        plans = []
        for name in ["track1.flac", "track2.flac", "track3.flac"]:
            p = OrganizePlan(source=Path(name), proposed=Path("out"))
            p.tags.artist = "Artist A"
            plans.append(p)
        assert _is_compilation_group(plans) is False

    def test_three_distinct_artists_is_compilation(self):
        plans = []
        for artist in ["Artist A", "Artist B", "Artist C"]:
            p = OrganizePlan(source=Path(f"{artist}.flac"), proposed=Path("out"))
            p.tags.artist = artist
            plans.append(p)
        assert _is_compilation_group(plans) is True

    def test_two_artists_split_ep_above_threshold(self):
        plans = []
        for artist in ["Artist A", "Artist B", "Artist A", "Artist B"]:
            p = OrganizePlan(source=Path(f"{artist}.flac"), proposed=Path("out"))
            p.tags.artist = artist
            plans.append(p)
        assert _is_compilation_group(plans) is True

    def test_two_artists_dominated_not_compilation(self):
        plans = []
        for artist in ["Artist A", "Artist A", "Artist A", "Artist B"]:
            p = OrganizePlan(source=Path(f"{artist}.flac"), proposed=Path("out"))
            p.tags.artist = artist
            plans.append(p)
        assert _is_compilation_group(plans) is False

    def test_two_track_single_not_compilation(self):
        plans = []
        for artist in ["Artist A", "Artist B"]:
            p = OrganizePlan(source=Path(f"{artist}.flac"), proposed=Path("out"))
            p.tags.artist = artist
            plans.append(p)
        assert _is_compilation_group(plans) is False

    def test_strips_featured_before_counting(self):
        plans = []
        for artist in [
            "Artist A feat. Guest",
            "Artist A",
            "Artist B",
        ]:
            p = OrganizePlan(source=Path("a.flac"), proposed=Path("out"))
            p.tags.artist = artist
            plans.append(p)
        # "Artist A feat. Guest" strips to "Artist A", so only 2 distinct → not compilation
        assert _is_compilation_group(plans, strip_featured=True) is False


class TestPlanOrganizationNormalization:
    def test_propagates_album_artist_within_same_folder(self, tmp_path):
        # Create two files in the same folder, same album
        src = tmp_path / "source"
        src.mkdir()
        out = tmp_path / "output"
        out.mkdir()

        # We can't easily create real audio files, so we mock _get_tags by
        # passing paths and relying on _get_tags to fail (not ideal).
        # Better: monkeypatch _get_tags.
        from soundaudit import organizer

        original_get_tags = organizer._get_tags

        def mock_get_tags(path: Path) -> TrackTags:
            tags = TrackTags()
            tags.album = "Test Album"
            tags.year = 2023
            if path.name == "track1.flac":
                tags.album_artist = "Main Artist"
                tags.artist = "Main Artist"
            elif path.name == "track2.flac":
                tags.album_artist = None
                tags.artist = "Main Artist feat. Guest"
            else:
                tags.album_artist = None
                tags.artist = "Main Artist"
            return tags

        organizer._get_tags = mock_get_tags
        try:
            (src / "track1.flac").write_text("fake")
            (src / "track2.flac").write_text("fake")
            (src / "track3.flac").write_text("fake")

            plans = plan_organization(
                [src / "track1.flac", src / "track2.flac", src / "track3.flac"],
                out,
            )
            # All should end up under "Main Artist"
            for plan in plans:
                assert "Main Artist" in str(plan.proposed)
                assert "feat." not in str(plan.proposed)
        finally:
            organizer._get_tags = original_get_tags

    def test_compilation_alias_normalized(self, tmp_path):
        from soundaudit import organizer

        original_get_tags = organizer._get_tags

        def mock_get_tags(path: Path) -> TrackTags:
            tags = TrackTags()
            tags.album = "Comp Album"
            tags.year = 2020
            tags.album_artist = "VA"
            tags.artist = "Random Artist"
            return tags

        organizer._get_tags = mock_get_tags
        try:
            src = tmp_path / "source"
            src.mkdir()
            out = tmp_path / "output"
            out.mkdir()
            (src / "track.flac").write_text("fake")

            plans = plan_organization([src / "track.flac"], out)
            assert "Various Artists" in str(plans[0].proposed)
        finally:
            organizer._get_tags = original_get_tags

    def test_infers_various_artists_when_no_album_artist_and_diverse_artists(self, tmp_path):
        from soundaudit import organizer

        original_get_tags = organizer._get_tags

        def mock_get_tags(path: Path) -> TrackTags:
            tags = TrackTags()
            tags.album = "Summer Hits"
            tags.year = 2024
            tags.album_artist = None
            tags.artist = path.stem.replace("_", " ")
            return tags

        organizer._get_tags = mock_get_tags
        try:
            src = tmp_path / "source"
            src.mkdir()
            out = tmp_path / "output"
            out.mkdir()
            files = [src / "Artist_A.flac", src / "Artist_B.flac", src / "Artist_C.flac"]
            for f in files:
                f.write_text("fake")

            plans = plan_organization(files, out)
            for plan in plans:
                assert "Various Artists" in str(plan.proposed)
        finally:
            organizer._get_tags = original_get_tags

    def test_prefers_compilation_name_in_conflict(self, tmp_path):
        from soundaudit import organizer

        original_get_tags = organizer._get_tags

        def mock_get_tags(path: Path) -> TrackTags:
            tags = TrackTags()
            tags.album = "Mixed Bag"
            tags.year = 2024
            # Three tracks: two say "Artist A", one says "Various Artists"
            if "track1" in path.name or "track2" in path.name:
                tags.album_artist = "Artist A"
                tags.artist = "Artist A"
            else:
                tags.album_artist = "Various Artists"
                tags.artist = "Artist B"
            return tags

        organizer._get_tags = mock_get_tags
        try:
            src = tmp_path / "source"
            src.mkdir()
            out = tmp_path / "output"
            out.mkdir()
            files = [src / "track1.flac", src / "track2.flac", src / "track3.flac"]
            for f in files:
                f.write_text("fake")

            plans = plan_organization(files, out)
            for plan in plans:
                assert "Various Artists" in str(plan.proposed)
        finally:
            organizer._get_tags = original_get_tags

    def test_empty_album_artist_treated_as_missing(self, tmp_path):
        from soundaudit import organizer

        original_get_tags = organizer._get_tags

        def mock_get_tags(path: Path) -> TrackTags:
            tags = TrackTags()
            tags.album = "Empty AA"
            tags.year = 2024
            tags.album_artist = ""
            tags.artist = "Artist A"
            return tags

        organizer._get_tags = mock_get_tags
        try:
            src = tmp_path / "source"
            src.mkdir()
            out = tmp_path / "output"
            out.mkdir()
            (src / "track.flac").write_text("fake")

            plans = plan_organization([src / "track.flac"], out)
            assert "Artist A" in str(plans[0].proposed)
            assert "Unknown Artist" not in str(plans[0].proposed)
        finally:
            organizer._get_tags = original_get_tags


class TestAlbumArtistPropagation:
    def test_unifies_real_album_under_majority_artist_when_no_album_artist(self, tmp_path):
        """If a real album has no album_artist but mostly one artist, all
        tracks should go to that artist's folder — NOT split up."""
        from soundaudit import organizer

        original_get_tags = organizer._get_tags

        def mock_get_tags(path: Path) -> TrackTags:
            tags = TrackTags()
            tags.album = "Sehnsucht"
            tags.year = 2021
            tags.album_artist = None
            if path.stem == "outlier":
                tags.artist = "Hansi Hinterseer"
            else:
                tags.artist = "Rammstein"
            return tags

        organizer._get_tags = mock_get_tags
        try:
            src = tmp_path / "source"
            src.mkdir()
            out = tmp_path / "output"
            out.mkdir()
            files = [src / f"track_{i}.flac" for i in range(1, 10)] + [src / "outlier.flac"]
            for f in files:
                f.write_text("fake")

            plans = plan_organization(files, out)
            for plan in plans:
                assert "Rammstein" in str(plan.proposed)
                assert "Hansi Hinterseer" not in str(plan.proposed)
                assert "Various Artists" not in str(plan.proposed)
        finally:
            organizer._get_tags = original_get_tags

    def test_loose_tracks_without_album_do_not_clump(self, tmp_path):
        """Tracks with no album tag (Unknown Album) and different artists
        should not be forced into Various Artists — each stays in its own
        artist folder."""
        from soundaudit import organizer

        original_get_tags = organizer._get_tags

        def mock_get_tags(path: Path) -> TrackTags:
            tags = TrackTags()
            tags.album = None
            tags.album_artist = None
            tags.artist = path.stem.replace("_", " ")
            return tags

        organizer._get_tags = mock_get_tags
        try:
            src = tmp_path / "source"
            src.mkdir()
            out = tmp_path / "output"
            out.mkdir()
            files = [src / "Artist_A.flac", src / "Artist_B.flac", src / "Artist_C.flac"]
            for f in files:
                f.write_text("fake")

            plans = plan_organization(files, out)
            assert "Artist_A" in str(plans[0].proposed) or "Artist A" in str(plans[0].proposed)
            assert "Artist_B" in str(plans[1].proposed) or "Artist B" in str(plans[1].proposed)
            assert "Artist_C" in str(plans[2].proposed) or "Artist C" in str(plans[2].proposed)
            for plan in plans:
                assert "Various Artists" not in str(plan.proposed)
        finally:
            organizer._get_tags = original_get_tags


class TestMinAlbumTracks:
    def test_skips_incomplete_album(self, tmp_path):
        from soundaudit import organizer

        original_get_tags = organizer._get_tags

        def mock_get_tags(path: Path) -> TrackTags:
            tags = TrackTags()
            tags.album = "Solo Album"
            tags.year = 2024
            tags.album_artist = "Artist A"
            tags.artist = "Artist A"
            return tags

        organizer._get_tags = mock_get_tags
        try:
            src = tmp_path / "source"
            src.mkdir()
            out = tmp_path / "output"
            out.mkdir()
            (src / "track.flac").write_text("fake")

            plans = plan_organization([src / "track.flac"], out, min_album_tracks=2)
            assert plans[0].status == "skipped"
            assert plans[0].proposed == plans[0].source
        finally:
            organizer._get_tags = original_get_tags

    def test_organizes_complete_album(self, tmp_path):
        from soundaudit import organizer

        original_get_tags = organizer._get_tags

        def mock_get_tags(path: Path) -> TrackTags:
            tags = TrackTags()
            tags.album = "Full Album"
            tags.year = 2024
            tags.album_artist = "Artist A"
            tags.artist = "Artist A"
            tags.track_number = int(path.stem.split("_")[1])
            return tags

        organizer._get_tags = mock_get_tags
        try:
            src = tmp_path / "source"
            src.mkdir()
            out = tmp_path / "output"
            out.mkdir()
            files = [src / f"track_{i}.flac" for i in range(1, 4)]
            for f in files:
                f.write_text("fake")

            plans = plan_organization(files, out, min_album_tracks=3)
            for plan in plans:
                assert plan.status == "pending"
                assert "Artist A" in str(plan.proposed)
        finally:
            organizer._get_tags = original_get_tags

    def test_counts_existing_destination_tracks(self, tmp_path):
        """Incremental organization: destination already has most of the album."""
        from soundaudit import organizer

        original_get_tags = organizer._get_tags

        def mock_get_tags(path: Path) -> TrackTags:
            tags = TrackTags()
            tags.album = "Test Album"
            tags.year = 2024
            tags.album_artist = "Test Artist"
            tags.artist = "Test Artist"
            tags.track_number = int(path.stem.split("_")[1])
            return tags

        organizer._get_tags = mock_get_tags
        try:
            src = tmp_path / "source"
            src.mkdir()
            out = tmp_path / "output"
            out.mkdir()

            # Simulate already-organized destination: 2 tracks present
            dest_album = out / "Test Artist" / "Test Album [2024]"
            dest_album.mkdir(parents=True)
            (dest_album / "01. Old Track.flac").write_text("old")
            (dest_album / "02. Old Track.flac").write_text("old")

            # One new track in source
            new_file = src / "track_3.flac"
            new_file.write_text("new")

            # With min=3, the single source track (1) + 2 existing = 3 → allowed
            plans = plan_organization([new_file], out, min_album_tracks=3)
            assert plans[0].status == "pending"
            assert dest_album in plans[0].proposed.parents
        finally:
            organizer._get_tags = original_get_tags

    def test_mixed_batch_some_skipped(self, tmp_path):
        from soundaudit import organizer

        original_get_tags = organizer._get_tags

        def mock_get_tags(path: Path) -> TrackTags:
            tags = TrackTags()
            tags.year = 2024
            tags.artist = path.stem
            if "album" in path.name:
                tags.album = "Album"
                tags.album_artist = "Album Artist"
            else:
                tags.album = "Single"
                tags.album_artist = "Single Artist"
            return tags

        organizer._get_tags = mock_get_tags
        try:
            src = tmp_path / "source"
            src.mkdir()
            out = tmp_path / "output"
            out.mkdir()
            album_files = [src / f"album_{i}.flac" for i in range(1, 3)]
            single_file = src / "single.flac"
            for f in album_files + [single_file]:
                f.write_text("fake")

            plans = plan_organization(album_files + [single_file], out, min_album_tracks=2)
            statuses = {p.source.name: p.status for p in plans}
            assert statuses["album_1.flac"] == "pending"
            assert statuses["album_2.flac"] == "pending"
            assert statuses["single.flac"] == "skipped"
        finally:
            organizer._get_tags = original_get_tags
