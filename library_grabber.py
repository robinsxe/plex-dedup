"""
Grab/replacement half of LibraryAnalyzer: searching the Radarr/Sonarr
interactive-search API (which queries the indexers synced from Prowlarr),
grabbing releases with stale-cache recovery, and batch execution. Mixed into
LibraryAnalyzer in library_analyzer.py, which provides the clients, trackers,
and grab-stat state this code reads off ``self``.
"""

import logging
import time
from types import SimpleNamespace

from analysis_models import AnalysisResult, _normalize_release_title
from arr_common import StaleReleaseError

logger = logging.getLogger(__name__)


class GrabOps:

    @staticmethod
    def _empty_grab_stats() -> dict:
        return {
            "grabbed_direct": 0,
            "stale_recovered": 0,
            "push_fallback_used": 0,
            "failed_unrecoverable": 0,
        }

    def reset_grab_stats(self) -> None:
        """
        Reset per-batch grab telemetry counters. ``execute_all`` calls this
        automatically; direct callers of ``execute_replacement`` (e.g. the
        web UI or single-item CLI flows) should invoke this themselves
        before a batch if they want clean counter values.
        """
        self._grab_stats = self._empty_grab_stats()

    # ------------------------------------------------------------------ #
    # Post-grab verification
    # ------------------------------------------------------------------ #

    def verify_grabs(self) -> dict:
        """
        Check earlier grabs against Plex. Grabbing only enqueues the download
        in Radarr/Sonarr — the import happens later, outside this app — so a
        grab is confirmed once the old file was replaced or a Swedish
        subtitle is present. Grabs that never import before the deadline are
        released (grab entry removed) and put on search cooldown, so the next
        scan retries them once the cooldown expires instead of excluding them
        forever.

        Runs at the start of each convert scan. Entries written before the
        verification metadata existed are left untouched.
        """
        now = time.time()
        pending = self.grab_tracker.pending_verification(self._verify_min_age_s)
        summary = {"checked": 0, "verified": 0, "released": 0, "pending": 0}

        for entry in pending:
            summary["checked"] += 1
            ids = {
                "imdb_id": entry.get("imdb_id"),
                "tmdb_id": entry.get("tmdb_id"),
                "rating_key": entry.get("rating_key"),
            }

            if ids["rating_key"]:
                item = self.plex.get_item_media(ids["rating_key"])
                if item is None:
                    # Plex unreachable — can't judge, leave it.
                    summary["pending"] += 1
                    continue
                if item.get("missing"):
                    # Item deleted from Plex. After the deadline, drop the
                    # grab entries so a re-added copy isn't excluded forever.
                    # No cooldown — if it comes back, analyze it right away.
                    if now - entry.get("grabbed_ts", now) >= self._verify_deadline_s:
                        self.grab_tracker.remove(**ids)
                        summary["released"] += 1
                        logger.info(
                            f"Grabbed item gone from Plex — dropped grab "
                            f"entry: {entry.get('title')}"
                        )
                    else:
                        summary["pending"] += 1
                    continue
                old_path = entry.get("file_path") or ""
                paths = item.get("file_paths") or []
                replaced = bool(old_path) and bool(paths) and old_path not in paths
                if item.get("has_swedish_sub") or replaced:
                    self.grab_tracker.mark_verified(**ids)
                    summary["verified"] += 1
                    logger.info(f"Verified grab: {entry.get('title')}")
                    continue

            # Checked but not imported yet (or no rating_key to check).
            if now - entry.get("grabbed_ts", now) >= self._verify_deadline_s:
                self.grab_tracker.remove(**ids)
                shim = SimpleNamespace(
                    display_title=entry.get("title", ""),
                    imdb_id=ids["imdb_id"],
                    tmdb_id=ids["tmdb_id"],
                    rating_key=ids["rating_key"] or "",
                )
                self.search_cooldown.mark_searched(shim)
                summary["released"] += 1
                logger.warning(
                    f"Grab never imported before deadline — released for retry "
                    f"after cooldown: {entry.get('title')}"
                )
            else:
                summary["pending"] += 1

        if summary["checked"]:
            logger.info(
                f"Grab verification: {summary['verified']} verified, "
                f"{summary['released']} released, {summary['pending']} pending"
            )
        return summary

    # ------------------------------------------------------------------ #
    # Indexer search (via Radarr/Sonarr)
    # ------------------------------------------------------------------ #

    def _build_radarr_index(self) -> dict:
        """Build lookup index for Radarr movies by TMDB/IMDB ID."""
        index = {}
        try:
            for movie in self.radarr.get_all_movies():
                tmdb = str(movie.get("tmdbId", ""))
                imdb = movie.get("imdbId", "")
                if tmdb:
                    index[f"tmdb:{tmdb}"] = movie
                if imdb:
                    index[f"imdb:{imdb}"] = movie
        except Exception as e:
            logger.warning(f"Could not fetch Radarr movies: {e}")
        return index

    def _find_in_radarr(self, result: AnalysisResult, index: dict) -> dict | None:
        """Find a movie in Radarr index by TMDB/IMDB ID."""
        if result.tmdb_id:
            entry = index.get(f"tmdb:{result.tmdb_id}")
            if entry:
                return entry
        if result.imdb_id:
            entry = index.get(f"imdb:{result.imdb_id}")
            if entry:
                return entry
        return None

    def _resolve_sonarr_episode_id(self, result: AnalysisResult) -> int | None:
        """
        Resolve a Sonarr internal episode_id for a TV AnalysisResult by
        looking up the series (tvdb/imdb/title) then the episode
        (season/episode number). Returns None if anything is missing.
        """
        if result.season_number is None or result.episode_number is None:
            return None
        series = self.sonarr.find_series(
            tvdb_id=result.tvdb_id,
            imdb_id=result.show_imdb_id or result.imdb_id,
            title=result.show_title or result.title,
        )
        if not series or "id" not in series:
            return None
        episode = self.sonarr.find_episode(
            series["id"], result.season_number, result.episode_number
        )
        if not episode or "id" not in episode:
            return None
        return episode["id"]

    def search_replacements(
        self, results: list[AnalysisResult], limit: int = 0,
        progress_callback=None,
    ) -> list[AnalysisResult]:
        """
        For items that need replacement, search Radarr/Sonarr indexers
        for available releases. Uses the *arr interactive search API
        which queries all configured indexers (synced from Prowlarr).

        Args:
            results: List of AnalysisResult from analyze_library
            limit: Max items to search (0 = all)
            progress_callback: Optional callable(current, total, title)

        Returns:
            The same list with indexer_results populated.
        """
        needs_replacement = [r for r in results if r.status == "needs_replacement"]

        if limit > 0:
            needs_replacement = needs_replacement[:limit]

        total = len(needs_replacement)
        logger.info(f"Searching indexers for {total} replacement releases")

        # Build Radarr index for movie lookups
        logger.info("Building Radarr movie index...")
        radarr_index = self._build_radarr_index()
        logger.info(f"Radarr index: {len(radarr_index)} entries")

        for idx, result in enumerate(needs_replacement, start=1):
            if not result.recommended_release:
                continue

            if progress_callback:
                try:
                    progress_callback(idx, total, result.display_title)
                except Exception:
                    pass

            logger.info(
                f"[{idx}/{total}] "
                f"Searching indexers for: {result.display_title} "
                f"(want: {result.recommended_release})"
            )

            try:
                all_results = []

                if result.media_type == "movie":
                    # Find the movie in Radarr to get its internal ID
                    radarr_movie = self._find_in_radarr(result, radarr_index)
                    if not radarr_movie:
                        logger.info(f"  Not found in Radarr — skipping")
                        result.indexer_results = []
                        continue
                    movie_id = radarr_movie["id"]
                    all_results = self.radarr.search_releases(movie_id)
                else:
                    episode_id = self._resolve_sonarr_episode_id(result)
                    if episode_id is None:
                        logger.info(f"  Not found in Sonarr — skipping")
                        result.indexer_results = []
                        continue
                    result.episode_id = episode_id
                    all_results = self.sonarr.search_releases(episode_id)

                if all_results is None:
                    # The indexer search itself failed (Radarr/Sonarr down or
                    # erroring) — distinct from "no releases found". Leave the
                    # item unmarked so it retries on the next scan instead of
                    # being skip-listed for SKIP_EXPIRY_DAYS after a transient
                    # outage.
                    logger.warning(
                        "  Indexer search failed — leaving for retry "
                        "(not skip-listed)"
                    )
                    result.indexer_results = []
                    continue

                if not all_results:
                    logger.info(f"  No releases found on indexers — adding to skip list")
                    result.indexer_results = []
                    self.skip_tracker.mark_skipped(
                        result, reason="no_indexer_results", defer_save=True)
                    continue

                # Filter out remux, 4K, and oversized releases
                filtered = []
                for r in all_results:
                    title_lower = (r.get("title") or "").lower()
                    size_gb = (r.get("size") or 0) / (1024 ** 3)

                    # Reject by quality keywords
                    if any(kw in title_lower for kw in self._rejected_qualities):
                        continue
                    # Reject by size
                    if self._max_size_gb > 0 and size_gb > self._max_size_gb:
                        continue
                    filtered.append(r)

                if not filtered and all_results:
                    logger.info(
                        f"  {len(all_results)} releases found but all filtered "
                        f"(remux/4K/>{self._max_size_gb}GB) — adding to skip list"
                    )
                    self.skip_tracker.mark_skipped(
                        result, reason="all_filtered", defer_save=True)
                    result.indexer_results = []
                    continue

                all_results = filtered

                # Score results: prefer NORDIC/SWE releases
                recommended_lower = result.recommended_release.lower()
                nordic_results = []
                matching_results = []
                other_results = []

                for r in all_results:
                    title = (r.get("title") or "").lower()
                    if recommended_lower and recommended_lower in title:
                        matching_results.append(r)
                    elif self._is_nordic_release(r.get("title", "")):
                        nordic_results.append(r)
                    else:
                        other_results.append(r)

                # Sort each group by quality preference
                def _quality_score(r):
                    t = (r.get("title") or "").lower()
                    score = 0
                    # Resolution preference
                    if "1080p" in t:
                        score += 100
                    elif "720p" in t:
                        score += 50
                    # Source preference
                    if "bluray" in t or "blu-ray" in t:
                        score += 30
                    elif "web-dl" in t or "webdl" in t:
                        score += 20
                    elif "webrip" in t:
                        score += 15
                    elif "hdtv" in t:
                        score += 10
                    return -score  # negative for ascending sort

                matching_results.sort(key=_quality_score)
                nordic_results.sort(key=_quality_score)
                other_results.sort(key=_quality_score)

                # Priority: exact match > nordic release > everything else
                ranked = matching_results + nordic_results + other_results
                result.indexer_results = ranked

                logger.info(
                    f"  Found {len(ranked)} release(s) "
                    f"({len(matching_results)} matching, "
                    f"{len(nordic_results)} NORDIC)"
                )

            except Exception as e:
                logger.error(
                    f"Search failed for {result.display_title}: {e}"
                )
                result.indexer_results = []

        # Flush deferred skip entries to disk in one write
        self.skip_tracker.flush()

        return results

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #

    def execute_replacement(self, result: AnalysisResult, dry_run: bool = True) -> bool:
        """
        Execute a single replacement by grabbing the release via Radarr/Sonarr.
        Uses the same grab mechanism as clicking "download" in the Radarr/Sonarr UI.

        In dry_run mode, just logs what would happen.
        """
        if result.status != "needs_replacement":
            logger.info(
                f"Skipping {result.display_title}  —  status is {result.status}"
            )
            return False

        if not result.indexer_results:
            logger.warning(
                f"No indexer results for {result.display_title}  —  "
                f"run search_replacements first"
            )
            return False

        best_result = result.indexer_results[0]
        release_title = best_result.get("title", result.recommended_release)
        guid = best_result.get("guid", "")
        indexer_id = best_result.get("indexerId") or best_result.get("indexer_id")

        if not guid or indexer_id is None:
            logger.warning(
                f"Missing guid or indexer_id for {result.display_title} — skipping"
            )
            return False

        if dry_run:
            logger.info(
                f"[DRY RUN] Would grab via {'Radarr' if result.media_type == 'movie' else 'Sonarr'}: "
                f"{release_title} for {result.display_title}"
            )
            return True

        if self._grab_refresh_before:
            best_result = self._refreshed_best_result(result, best_result)

        try:
            success = self._grab_with_retry(result, best_result)

            if not success:
                result.status = "error"
                result.error = "Grab returned failure"
                logger.error(f"Grab failed for {release_title}")
                return False

            result.status = "replaced"
            if not self.grab_tracker.mark_grabbed(result):
                # The grab succeeded but we failed to record it. Surface this —
                # otherwise the item is silently re-grabbed (re-downloaded) on
                # every future scan because is_grabbed never sees it.
                result.error = (
                    "Grabbed, but failed to save the grab record — this item "
                    "may be re-grabbed on the next scan (check /data is writable)"
                )
                logger.error(
                    f"Grab succeeded but grab-tracker save failed for {release_title}"
                )
            logger.info(
                f"Successfully grabbed via {'Radarr' if result.media_type == 'movie' else 'Sonarr'}: "
                f"{release_title}"
            )
            return True
        except Exception as e:
            result.status = "error"
            result.error = f"Grab failed: {e}"
            logger.error(f"Failed to grab {release_title}: {e}")
            return False

    def _grab_with_retry(
        self,
        result: "AnalysisResult",
        best_result: dict,
    ) -> bool:
        """
        Grab with stale-cache recovery.

        Radarr/Sonarr cache search results in memory for a short window. If the
        guid is evicted (HTTP 404 on POST /api/v3/release), re-search to refresh
        the cache, find the same release by title, and retry. As a last resort,
        push by downloadUrl which bypasses the guid cache entirely.

        Unrecoverable failures are added to the skip tracker so subsequent
        scans don't immediately re-attempt the same broken release.
        """
        is_movie = result.media_type == "movie"
        client: "RadarrClient | SonarrClient" = self.radarr if is_movie else self.sonarr
        label = "Radarr" if is_movie else "Sonarr"
        release_title = best_result.get("title") or result.recommended_release or ""
        guid = best_result.get("guid", "")
        indexer_id = best_result.get("indexerId") or best_result.get("indexer_id")

        logger.info(f"Grabbing via {label}: {release_title}")
        try:
            if client.grab_release(guid, indexer_id):
                self._grab_stats["grabbed_direct"] += 1
                return True
        except StaleReleaseError as e:
            logger.warning(f"{label} cache stale for {release_title} ({e}) — refreshing")

        # Refresh cache and retry (symmetric for movies + TV).
        refreshed = (
            self._refresh_radarr_release(result, release_title) if is_movie
            else self._refresh_sonarr_release(result, release_title)
        )
        if refreshed:
            new_guid, new_indexer = refreshed
            logger.info(
                f"Retrying grab via {label} with refreshed guid for {release_title}"
            )
            try:
                if client.grab_release(new_guid, new_indexer):
                    self._grab_stats["stale_recovered"] += 1
                    return True
                logger.warning(
                    f"{label} retry returned failure for {release_title} — falling back to push"
                )
            except StaleReleaseError:
                logger.warning(
                    f"{label} still 404 after refresh for {release_title} — falling back to push"
                )
            except Exception as e:
                logger.warning(
                    f"{label} retry raised {type(e).__name__} for {release_title} ({e}) "
                    f"— falling back to push"
                )

        # Final fallback: push by URL (bypasses guid cache)
        if self._push_fallback(client, label, release_title, best_result):
            self._grab_stats["push_fallback_used"] += 1
            return True

        self._grab_stats["failed_unrecoverable"] += 1
        try:
            self.skip_tracker.mark_skipped(
                result, reason="stale_grab_unrecoverable", defer_save=True
            )
        except Exception as e:
            logger.warning(f"Could not mark {result.display_title} as skipped: {e}")
        return False

    def _refreshed_best_result(
        self, result: "AnalysisResult", best_result: dict
    ) -> dict:
        """
        Proactively refresh the *arr release cache and return a best_result
        with a fresh guid/indexerId. Returns the original dict untouched if
        refresh found no match. Never mutates the input. Triggered by the
        GRAB_REFRESH_BEFORE env flag.
        """
        is_movie = result.media_type == "movie"
        label = "Radarr" if is_movie else "Sonarr"
        release_title = best_result.get("title") or result.recommended_release or ""

        refreshed = (
            self._refresh_radarr_release(result, release_title) if is_movie
            else self._refresh_sonarr_release(result, release_title)
        )
        if not refreshed:
            return best_result

        new_guid, new_indexer = refreshed
        old_indexer = best_result.get("indexerId") or best_result.get("indexer_id")
        if new_guid == best_result.get("guid") and new_indexer == old_indexer:
            return best_result

        logger.info(f"Pre-grab refresh updated guid for {release_title} via {label}")
        updated = dict(best_result)
        updated["guid"] = new_guid
        updated["indexerId"] = new_indexer
        return updated

    def _refresh_sonarr_release(
        self, result: "AnalysisResult", release_title: str
    ) -> tuple[str, int] | None:
        """
        Re-run Sonarr's interactive search and locate the same release by
        normalized title. Returns (guid, indexerId) on match, None otherwise.

        Requires ``result.episode_id`` (populated by search_replacements).
        """
        if not result.episode_id:
            logger.warning(
                f"Skipping Sonarr refresh for {result.display_title}: "
                f"no episode_id"
            )
            return None

        fresh = self.sonarr.search_releases(result.episode_id)
        if not fresh:
            logger.warning(
                f"Sonarr refresh returned no releases for {release_title} "
                f"(indexer transient failure or release no longer listed)"
            )
            return None

        target = _normalize_release_title(release_title)
        for r in fresh:
            if _normalize_release_title(r.get("title") or "") == target:
                guid = r.get("guid")
                indexer_id = r.get("indexerId") or r.get("indexer_id")
                if guid and indexer_id is not None:
                    return guid, indexer_id

        logger.warning(
            f"Original release {release_title!r} not in refreshed Sonarr results — "
            f"refusing to substitute a different release"
        )
        return None

    def _refresh_radarr_release(
        self, result: "AnalysisResult", release_title: str
    ) -> tuple[str, int] | None:
        """
        Re-run Radarr's interactive search and locate the same release by
        normalized title. Returns (guid, indexerId) on match, None otherwise.

        Skips when neither tmdb_id nor imdb_id is known, since the title-only
        fallback in find_movie() pulls the full Radarr catalog per item.
        """
        if not result.tmdb_id and not result.imdb_id:
            logger.warning(
                f"Skipping Radarr refresh for {result.display_title}: "
                f"no tmdb_id or imdb_id — title-only lookup is too expensive"
            )
            return None

        movie = self.radarr.find_movie(
            tmdb_id=result.tmdb_id,
            imdb_id=result.imdb_id,
            title=result.title,
            year=result.year,
        )
        if not movie or "id" not in movie:
            logger.warning(f"Could not re-locate movie in Radarr: {result.display_title}")
            return None

        fresh = self.radarr.search_releases(movie["id"])
        if not fresh:
            logger.warning(
                f"Radarr refresh returned no releases for {release_title} "
                f"(indexer transient failure or release no longer listed)"
            )
            return None

        target = _normalize_release_title(release_title)
        for r in fresh:
            if _normalize_release_title(r.get("title") or "") == target:
                guid = r.get("guid")
                indexer_id = r.get("indexerId") or r.get("indexer_id")
                if guid and indexer_id is not None:
                    return guid, indexer_id

        logger.warning(
            f"Original release {release_title!r} not in refreshed results — "
            f"refusing to substitute a different release"
        )
        return None

    def _push_fallback(
        self,
        client: "RadarrClient | SonarrClient",
        label: str,
        release_title: str,
        best_result: dict,
    ) -> bool:
        """
        Push the release by downloadUrl, bypassing the guid cache. Best-effort:
        skips cleanly if the original result dict lacks required fields.
        """
        download_url = best_result.get("downloadUrl") or best_result.get("download_url")
        protocol = best_result.get("protocol")
        publish_date = best_result.get("publishDate") or best_result.get("publish_date") or ""
        indexer_name = best_result.get("indexer") or ""

        if not download_url or not protocol:
            logger.error(
                f"Push fallback for {release_title} not possible "
                f"(missing downloadUrl or protocol)"
            )
            return False

        logger.info(f"Push fallback via {label}: {release_title}")
        return client.push_release(
            title=release_title,
            download_url=download_url,
            protocol=protocol,
            publish_date=publish_date,
            indexer=indexer_name,
        )

    def execute_all(
        self, results: list[AnalysisResult], dry_run: bool = True
    ) -> dict:
        """Execute all replacements. Returns summary dict."""
        needs_replacement = [r for r in results if r.status == "needs_replacement"]

        logger.info(
            f"Executing {'DRY RUN ' if dry_run else ''}"
            f"replacements for {len(needs_replacement)} items"
        )

        # Reset per-batch grab telemetry.
        self.reset_grab_stats()

        success = 0
        failed = 0
        skipped = 0

        for result in needs_replacement:
            if not result.indexer_results:
                skipped += 1
                continue

            if self.execute_replacement(result, dry_run=dry_run):
                success += 1
            else:
                failed += 1

        # Flush deferred skip-tracker writes from unrecoverable grabs.
        try:
            self.skip_tracker.flush()
        except Exception as e:
            logger.warning(f"Could not flush skip tracker: {e}")

        stats = dict(self._grab_stats)
        summary = {
            "total": len(needs_replacement),
            "success": success,
            "failed": failed,
            "skipped": skipped,
            "dry_run": dry_run,
            "grab_stats": stats,
        }

        logger.info(
            f"Execution complete: {success} succeeded, "
            f"{failed} failed, {skipped} skipped"
        )
        if not dry_run and any(stats.values()):
            logger.info(
                f"Grab telemetry: direct={stats['grabbed_direct']}, "
                f"stale_recovered={stats['stale_recovered']}, "
                f"push_fallback={stats['push_fallback_used']}, "
                f"unrecoverable={stats['failed_unrecoverable']}"
            )
        return summary
