"""Tests for v1c-B -- per-conversation state on the session entry.

Covers the additive ``SessionMap`` backing store that will replace the
``slack/handler.py`` module-global thread-state dicts:

* boolean flags (``temporary`` / ``incognito``)
* agent + project overrides

Key properties asserted:
* state survives a reload (it is the point of moving off in-memory globals);
* setting per-conversation state never clobbers ``sid`` / Slack-link fields;
* bare ``thread_ts`` and ``slack:`` keys resolve to the SAME entry
  (canonicalization), so a not-yet-migrated caller and a migrated one agree.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.session_map import SessionMap


def _make_kiro_session(kiro_dir, sid: str) -> None:
    kiro_dir.mkdir(parents=True, exist_ok=True)
    (kiro_dir / f"{sid}.json").write_text("{}", encoding="utf-8")
    (kiro_dir / f"{sid}.jsonl").write_text('{"x":1}\n{"y":2}\n', encoding="utf-8")


@pytest.fixture()
def patched(tmp_path, monkeypatch):
    kiro = tmp_path / "kiro"
    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", kiro)
    return tmp_path, kiro


class TestFlags:
    def test_flag_defaults_false(self, patched):
        sm = SessionMap()
        assert sm.get_flag("slack:1.2", "temporary") is False
        assert sm.get_flag("missing", "incognito") is False

    def test_set_and_get_flag(self, patched):
        sm = SessionMap()
        sm.set_flag("slack:1.2", "temporary", True)
        assert sm.get_flag("slack:1.2", "temporary") is True
        # other flags remain independent
        assert sm.get_flag("slack:1.2", "incognito") is False

    def test_flag_persists_across_reload(self, patched):
        sm = SessionMap()
        sm.set_flag("slack:1.2", "incognito", True)
        # fresh instance == reload from disk
        assert SessionMap().get_flag("slack:1.2", "incognito") is True

    def test_clear_flag_removes_it(self, patched):
        sm = SessionMap()
        sm.set_flag("slack:1.2", "temporary", True)
        sm.set_flag("slack:1.2", "temporary", False)
        assert sm.get_flag("slack:1.2", "temporary") is False
        # clearing the last flag drops the sub-dict entirely (no accretion)
        raw = json.loads((patched[0] / "session_map.json").read_text(encoding="utf-8"))
        assert "flags" not in raw["slack:1.2"]

    def test_clear_flag_on_missing_key_creates_no_entry(self, patched):
        # Clearing a flag on a key that was never stored is a no-op and must
        # NOT materialize a blank entry on disk (phantom-entry accretion).
        sm = SessionMap()
        sm.set_flag("slack:never.stored", "temporary", False)
        assert sm.get_flag("slack:never.stored", "temporary") is False
        path = patched[0] / "session_map.json"
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            assert "slack:never.stored" not in raw

    def test_two_flags_coexist(self, patched):
        sm = SessionMap()
        sm.set_flag("slack:1.2", "temporary", True)
        sm.set_flag("slack:1.2", "incognito", True)
        sm.set_flag("slack:1.2", "temporary", False)
        # clearing one leaves the other
        assert sm.get_flag("slack:1.2", "temporary") is False
        assert sm.get_flag("slack:1.2", "incognito") is True

    def test_bare_and_namespaced_key_same_entry(self, patched):
        sm = SessionMap()
        sm.set_flag("1.2", "temporary", True)  # bare thread_ts
        assert sm.get_flag("slack:1.2", "temporary") is True  # namespaced read


class TestPrivacyFlagsAndPrune:
    """A ``temporary`` / ``incognito`` flag keeps its entry through both stale
    paths; the entry goes only once the transcript header records the mode, and
    that decision is taken off the event loop.

    The flag protects the conversation's transcript, which outlives the provider
    session whose reclaimed file makes the entry stale; a memory reader that finds
    neither the entry nor a header mode reads the thread as persistent. ``prune()``
    and the per-read repair keep such an entry WITHOUT reading a transcript (they
    run under the map lock, on the loop); ``collect_recorded_privacy_entries()``
    probes -- and for a legacy row, stamps -- the header on a worker thread and
    removes the rows the header now covers. Immortality stays opt-in. Mutations:
    make ``_header_records_privacy_mode`` return True unconditionally -- the
    ``kept`` cases go red; return False -- the ``collected`` cases go red.
    """

    KEY = "telegram:kirocrew:direct:4242"

    def test_flag_names_are_the_privacy_mode_names_strictest_first(self):
        from kiro_crew.messaging import privacy_mode
        from kiro_crew.session_map import _PRIVACY_FLAGS, _PRIVACY_STRICTNESS

        assert _PRIVACY_FLAGS == {privacy_mode.MODE_TEMPORARY, privacy_mode.MODE_INCOGNITO}
        # The collection rule compares strictness the way ``privacy_mode.strictest`` does.
        assert list(_PRIVACY_STRICTNESS) == [
            m for m in privacy_mode._STRICTNESS if m in _PRIVACY_FLAGS
        ]

    @staticmethod
    def _log(*, seed: bool, mode: str | None = None):
        """The thread's default-directory transcript: seeded with a turn, optionally stamped."""
        from kiro_crew import history as history_mod
        from kiro_crew.history import ConversationLog

        log = ConversationLog()
        log.init()
        if seed:
            with history_mod.allow_on_loop_persist():
                log.append(TestPrivacyFlagsAndPrune.KEY, "user", "a pre-modifier turn")
        if mode is not None:
            log.update_metadata_if(
                TestPrivacyFlagsAndPrune.KEY, {"memory_mode": mode}, lambda _m: True
            )
        return log

    @pytest.mark.parametrize("flag", ["temporary", "incognito"])
    def test_startup_prune_keeps_a_stale_flagged_entry_and_reads_no_transcript(
        self, patched, monkeypatch, flag
    ):
        """The loop-side half: sid cleared, flag kept, on disk -- and no header read."""
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr(
            ConversationLog,
            "get_metadata",
            lambda self, key: pytest.fail("prune read a transcript"),
        )
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")  # no such file under the kiro dir
        sm.set_flag(self.KEY, flag, True)
        assert sm.prune() == 0
        assert sm.get_flag(self.KEY, flag) is True
        assert (sm._data.get(self.KEY) or {}).get("sid") == ""
        assert SessionMap().get_flag(self.KEY, flag) is True
        # The candidate travels with the sid prune left it (cleared), so the
        # worker thread can stat it and the lock-held removal can compare it.
        assert sm.stale_privacy_entries() == {self.KEY: ("", [flag], True)}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("flag", ["temporary", "incognito"])
    async def test_the_off_loop_step_collects_an_entry_whose_header_carries_the_mode(
        self, patched, flag
    ):
        """The header is the durable record; the row has nothing left to protect."""
        self._log(seed=True, mode=flag)
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")
        sm.set_flag(self.KEY, flag, True)
        assert sm.prune() == 0
        assert await sm.collect_recorded_privacy_entries() == 1
        assert sm.get_flag(self.KEY, flag) is False
        assert self.KEY not in sm._data
        assert SessionMap().get_flag(self.KEY, flag) is False

    @pytest.mark.asyncio
    async def test_the_off_loop_step_stamps_a_legacy_rows_mode_into_the_header_then_collects(
        self, patched
    ):
        """A row flagged before headers were stamped: the mode moves into the
        existing transcript's header first, so the memory readers keep refusing
        after the row is gone."""
        log = self._log(seed=True)
        assert "memory_mode" not in log.get_metadata(self.KEY), "premise: legacy header"
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")
        sm.set_flag(self.KEY, "incognito", True)
        assert sm.prune() == 0
        assert await sm.collect_recorded_privacy_entries() == 1
        assert log.get_metadata(self.KEY).get("memory_mode") == "incognito"
        assert self.KEY not in sm._data

    @pytest.mark.asyncio
    async def test_a_stricter_flag_tightens_the_header_before_the_row_goes(self, patched):
        """``!incognito`` then ``!temporary`` with the header still saying incognito:
        the header is tightened to temporary, then the row is collected."""
        log = self._log(seed=True, mode="incognito")
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")
        sm.set_flag(self.KEY, "temporary", True)
        sm.prune()
        assert await sm.collect_recorded_privacy_entries() == 1
        assert log.get_metadata(self.KEY).get("memory_mode") == "temporary"

    @pytest.mark.asyncio
    async def test_a_flagged_entry_with_no_transcript_is_kept(self, patched):
        """``!incognito`` as the very first message: no sid, no transcript. Nothing to
        stamp (a refusal must never create a transcript), so the row stays."""
        sm = SessionMap()
        sm.set_flag(self.KEY, "incognito", True)
        assert sm.prune() == 0
        assert await sm.collect_recorded_privacy_entries() == 0
        assert sm.get_flag(self.KEY, "incognito") is True

    @pytest.mark.asyncio
    async def test_an_unwritable_or_unreadable_header_keeps_the_entry(self, patched, monkeypatch):
        """Unreadable is not "already recorded": keeping is the safe direction."""
        from kiro_crew.history import ConversationLog

        self._log(seed=True, mode="incognito")
        monkeypatch.setattr(
            ConversationLog,
            "update_metadata_if",
            lambda self, *a, **kw: (_ for _ in ()).throw(OSError("read-only")),
        )
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")
        sm.set_flag(self.KEY, "incognito", True)
        sm.prune()
        assert await sm.collect_recorded_privacy_entries() == 0
        assert sm.get_flag(self.KEY, "incognito") is True

    @pytest.mark.asyncio
    async def test_a_row_that_came_back_to_life_during_the_probe_is_not_collected(self, patched):
        """Re-checked under the lock: a live sid regained while the probe ran off
        the loop makes the row current again."""
        self._log(seed=True, mode="incognito")
        tmp, kiro = patched
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")
        sm.set_flag(self.KEY, "incognito", True)
        sm.prune()
        _make_kiro_session(kiro, "sid-alive")
        sm.set(self.KEY, "sid-alive")  # the thread resumed before the collection landed
        assert await sm.collect_recorded_privacy_entries() == 0
        assert sm.get_flag(self.KEY, "incognito") is True

    def test_the_per_read_repair_keeps_the_flag_and_reads_no_transcript(self, patched, monkeypatch):
        """``get()`` on a stale sid keeps the entry whatever the header says; collection
        belongs to the startup step, off the loop."""
        from kiro_crew.history import ConversationLog

        self._log(seed=True, mode="temporary")
        monkeypatch.setattr(
            ConversationLog,
            "get_metadata",
            lambda self, key: pytest.fail("the repair read a transcript"),
        )
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")
        sm.set_flag(self.KEY, "temporary", True)
        assert sm.get(self.KEY) is None
        assert sm.get_flag(self.KEY, "temporary") is True

    @pytest.mark.asyncio
    async def test_the_header_probe_never_runs_on_the_loop_thread(self, patched, monkeypatch):
        """The whole point of the split: the transcript I/O is a worker thread's."""
        import threading

        from kiro_crew.history import ConversationLog

        loop_thread = threading.current_thread()
        seen: list[threading.Thread] = []
        real_get = ConversationLog.get_metadata
        real_update = ConversationLog.update_metadata_if

        def _recording_get(self, key):
            seen.append(threading.current_thread())
            return real_get(self, key)

        def _recording_update(self, *a, **kw):
            seen.append(threading.current_thread())
            return real_update(self, *a, **kw)

        self._log(seed=True, mode="incognito")
        monkeypatch.setattr(ConversationLog, "get_metadata", _recording_get)
        monkeypatch.setattr(ConversationLog, "update_metadata_if", _recording_update)
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")
        sm.set_flag(self.KEY, "incognito", True)
        sm.prune()
        assert await sm.collect_recorded_privacy_entries() == 1
        assert seen, "premise: the header was probed"
        assert [t for t in seen if t is loop_thread] == []

    @pytest.mark.asyncio
    async def test_the_sid_stat_never_runs_on_the_loop_thread_either(self, patched, monkeypatch):
        """Naming the candidates and removing the confirmed rows are entry-state
        work under the lock; whether a candidate's ``sid`` file still exists is
        answered on the worker thread beside the header probe, so a map with many
        flagged rows costs the loop no filesystem call. A row whose session is
        live is still kept -- judged off the loop, not skipped on it."""
        import pathlib
        import threading

        from kiro_crew import session_map as session_map_mod

        tmp, kiro = patched
        loop_thread = threading.current_thread()
        stats: list[threading.Thread] = []

        class _RecordingPath(type(pathlib.Path())):
            def exists(self, *a, **kw):
                stats.append(threading.current_thread())
                return super().exists(*a, **kw)

        live = "telegram:kirocrew:direct:7"
        self._log(seed=True, mode="incognito")
        _make_kiro_session(kiro, "sid-alive")
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")
        sm.set_flag(self.KEY, "incognito", True)
        sm.set(live, "sid-alive")
        sm.set_flag(live, "incognito", True)
        sm.prune()  # its own stats are the pre-existing loop-side pass
        monkeypatch.setattr(session_map_mod, "_KIRO_SESSIONS_DIR", _RecordingPath(kiro))
        assert sm.stale_privacy_entries() == {
            self.KEY: ("", ["incognito"], True),
            live: ("sid-alive", ["incognito"], True),
        }
        assert stats == [], "naming the candidates stat-ed a session file on the loop"
        assert await sm.collect_recorded_privacy_entries() == 1
        assert stats, "premise: the live row's sid file was stat-ed"
        assert [t for t in stats if t is loop_thread] == []
        assert self.KEY not in sm._data
        assert sm.get_flag(live, "incognito") is True and sm.get(live) == "sid-alive"

    @pytest.mark.asyncio
    async def test_a_flag_tightened_during_the_probe_keeps_the_row_for_the_next_pass(
        self, patched, monkeypatch
    ):
        """``!temporary`` lands while the worker probes an ``incognito`` row.

        The worker stamped and judged the header on the flags it saw; the
        tightened flag is a mode the header does not yet record and the channel
        gate hydrates from the map alone, so collecting the row would erase the
        only record of it. The lock-held re-check compares the flags exactly as
        it compares the sid: the row stays, the gate reads ``temporary``, and the
        next collection re-stamps the header with the tightened mode before
        collecting. The stamp the first pass left is the mode it saw -- never a
        looser header than before -- which is why the window costs nothing.
        Mutation: drop the flags comparison in ``_collect_privacy_entries`` --
        the first pass collects the row (``assert 1 == 0``) and the tightened
        mode is gone from every map-sourced reader.
        """
        from types import SimpleNamespace

        from kiro_crew import session_map as session_map_mod
        from kiro_crew.messaging import privacy_mode

        log = self._log(seed=True, mode="incognito")
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")
        sm.set_flag(self.KEY, "incognito", True)
        sm.prune()
        real_probe = session_map_mod._header_records_privacy_mode
        seen_by_worker: list[list[str]] = []

        def _probe_then_tighten(key, flagged):
            seen_by_worker.append(list(flagged))
            recorded = real_probe(key, flagged)
            # The modifier arrives while the probe runs off the loop (a live
            # reload, not only boot): the row tightens under the worker.
            sm.set_flag(key, "temporary", True)
            return recorded

        monkeypatch.setattr(session_map_mod, "_header_records_privacy_mode", _probe_then_tighten)
        assert await sm.collect_recorded_privacy_entries() == 0
        assert seen_by_worker == [["incognito"]], "premise: the worker judged the incognito row"
        # The row survives with BOTH flags, on disk too.
        assert self.KEY in sm._data
        assert sm.get_flag(self.KEY, "temporary") is True
        assert sm.get_flag(self.KEY, "incognito") is True
        assert SessionMap().get_flag(self.KEY, "temporary") is True
        # The stamp is the mode the worker saw: not loosened, not yet tightened.
        assert log.get_metadata(self.KEY).get("memory_mode") == "incognito"
        # The channel gate, hydrating from the map alone, reads the tightened mode.
        privacy_mode.reset()
        try:
            privacy_mode.hydrate(SimpleNamespace(_session_map=sm), self.KEY)
            assert privacy_mode.is_temporary(self.KEY) is True
        finally:
            privacy_mode.reset()
        # The next pass re-probes on the tightened flags: header tightened, row collected.
        monkeypatch.setattr(session_map_mod, "_header_records_privacy_mode", real_probe)
        assert await sm.collect_recorded_privacy_entries() == 1
        assert log.get_metadata(self.KEY).get("memory_mode") == "temporary"
        assert self.KEY not in sm._data

    def test_an_unflagged_stale_entry_is_still_collected_by_prune(self, patched):
        """The two-step rule is exactly the two privacy flags, not every entry."""
        sm = SessionMap()
        sm.set(self.KEY, "sid-reclaimed-by-kiro-cli")
        assert sm.prune() == 1


class TestStartPoolPrunesOffTheLoop:
    """``start_pool()`` prunes on the loop and awaits the header probe off it."""

    @pytest.mark.asyncio
    async def test_start_pool_reads_no_transcript_header_on_the_loop_thread(
        self, patched, monkeypatch
    ):
        import threading
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew import history as history_mod
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.history import ConversationLog
        from kiro_crew.session import SessionManager

        key = TestPrivacyFlagsAndPrune.KEY
        log = ConversationLog()
        log.init()
        with history_mod.allow_on_loop_persist():
            log.append(key, "user", "a pre-modifier turn")
        log.update_metadata_if(key, {"memory_mode": "incognito"}, lambda _m: True)
        seeded = SessionMap()
        seeded.set(key, "sid-reclaimed-by-kiro-cli")
        seeded.set_flag(key, "incognito", True)
        seeded.flush()

        loop_thread = threading.current_thread()
        seen: list[threading.Thread] = []
        real_get = ConversationLog.get_metadata

        def _recording_get(self, k):
            seen.append(threading.current_thread())
            return real_get(self, k)

        monkeypatch.setattr(ConversationLog, "get_metadata", _recording_get)

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.is_process_alive = lambda: True
            m.context_usage_pct = lambda: 0.0
            m.context_usage_unknown = lambda: False
            m.context_window_tokens = lambda: 0
            m.has_active_turn = lambda: False
            m.runtime_info = lambda: (None, None)
            m.stream_command = MagicMock()
            return m

        cfg = KiroCrewConfig()
        cfg.session.timeout_secs = 2
        mgr = SessionManager(cfg, provider_factory=factory)
        try:
            await mgr.start_pool()
            assert seen, "premise: start_pool probed the flagged row's header"
            assert [t for t in seen if t is loop_thread] == []
            assert mgr._session_map.get_flag(key, "incognito") is False
        finally:
            await mgr.close_all()


def _provider_factory(session_key=None, agent=None, channel_id=None, **kwargs):
    """A ``SessionManager`` provider double that never spawns anything."""
    from unittest.mock import AsyncMock, MagicMock

    m = AsyncMock()
    m.start = AsyncMock()
    m.shutdown = AsyncMock()
    m.is_process_alive = lambda: True
    m.context_usage_pct = lambda: 0.0
    m.context_usage_unknown = lambda: False
    m.context_window_tokens = lambda: 0
    m.has_active_turn = lambda: False
    m.runtime_info = lambda: (None, None)
    m.stream_command = MagicMock()
    return m


class TestSidLessPrivacyRowsAcrossARestart:
    """A privacy-flagged channel row that NEVER owned a sid, across a restart.

    ``!incognito`` as a thread's first message writes the map flag (and, since
    the modifier stamps headers, a metadata-only transcript header) before the
    thread has run a turn, so the row has no ``sid`` and no ``discarded_sid``.
    A restart then runs the real startup path -- ``SessionManager.start_pool()``:
    ``prune()`` on the loop, ``collect_recorded_privacy_entries()`` off it -- and
    the question is what the thread's readers resolve AFTERWARDS, in three
    transcript states: (a) no transcript on disk, (b) a transcript whose header
    lacks the mode, (c) a transcript whose header carries it.

    Two readers, two records. The channel's inbound gate
    (``upload_gate.session_is_restricted`` for a non-``dashboard:`` key -- the
    same ``privacy_mode.hydrate`` the Slack and Telegram handlers call per
    message, and ``_is_restricted_session`` behind every memory-mutation route)
    reads the SESSION MAP through the process-local trackers and never the
    header. ``execution_context.capture_session_execution`` (the hooks handler,
    the MCP control tool, the task runner, workflow memory) and the consolidation
    resolver read the HEADER when the map has nothing. Observed before the fix,
    states (b) and (c): the worker stamped the header and collected the row, the
    header and the consolidator read ``incognito``, and the channel gate read the
    thread as PERSISTENT -- the modifier undone by a restart between the flag and
    the first turn. Now a never-owned row is stamped but never collected.
    Mutation: drop the ``collectable`` guard in ``collect_recorded_privacy_entries``
    -- (b) and (c) go red on the gate assertion with ``channel_gate_restricted:
    False``.
    """

    KEY = "telegram:kirocrew:direct:5150"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["no-transcript", "header-lacks-mode", "header-carries-mode"])
    async def test_the_row_is_kept_and_the_thread_stays_restricted_after_a_restart(
        self, patched, state
    ):
        from types import SimpleNamespace

        from kiro_crew import execution_context
        from kiro_crew import history as history_mod
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.history import ConversationLog
        from kiro_crew.history_consolidation import resolve_consolidation_target
        from kiro_crew.messaging import privacy_mode, upload_gate
        from kiro_crew.session import SessionManager

        key = self.KEY
        log = ConversationLog()
        log.init()
        if state != "no-transcript":
            with history_mod.allow_on_loop_persist():
                log.append(key, "user", "a turn written before the modifier")
        if state == "header-carries-mode":
            log.update_metadata_if(key, {"memory_mode": "incognito"}, lambda _m: True)
        # The flag alone: no sid was ever bound (no provider session ran yet).
        seeded = SessionMap()
        seeded.set_flag(key, "incognito", True)
        seeded.flush()
        entry = SessionMap()._data[key]
        assert not entry.get("sid") and not entry.get("discarded_sid"), "premise: never owned a sid"

        # The restart: empty trackers, then the real startup path.
        privacy_mode.reset()
        cfg = KiroCrewConfig()
        cfg.session.timeout_secs = 2
        mgr = SessionManager(cfg, provider_factory=_provider_factory)
        try:
            await mgr.start_pool()
            target = await resolve_consolidation_target(key, log=ConversationLog(), sessions=mgr)
            observed = {
                "row_in_map": key in mgr._session_map._data,
                "flag": mgr._session_map.get_flag(key, "incognito"),
                "header_mode": ConversationLog().get_metadata(key).get("memory_mode"),
                # The channel gate, exactly as an inbound message asks it.
                "channel_gate_restricted": await upload_gate.session_is_restricted(
                    SimpleNamespace(sessions=mgr),
                    key,
                    persisted_probe=lambda _k: (False, None),
                ),
                # The header reader (execution_context.py, capture_session_execution).
                "execution_mode": execution_context.capture_session_execution(key).memory_mode,
                # The memory readers' verdict.
                "consolidation": (
                    None
                    if target.restricted is None
                    else (target.restricted.mode, target.restricted.source)
                ),
            }
        finally:
            await mgr.close_all()
            privacy_mode.reset()

        # The row's fate: kept, flag intact, on disk -- never a collection
        # candidate, so no restart can undo the modifier.
        assert observed["row_in_map"] is True, observed
        assert observed["flag"] is True, observed
        assert SessionMap().get_flag(key, "incognito") is True, observed
        # The thread's readers after the restart: the channel gate off the map,
        # the memory readers off the map or the header.
        assert observed["channel_gate_restricted"] is True, observed
        assert observed["consolidation"] is not None, observed
        # The header is ensured wherever a transcript exists (the worker stamps a
        # header that lacks the mode), and never created where none does.
        assert observed["header_mode"] == (
            None if state == "no-transcript" else "incognito"
        ), observed
        assert observed["execution_mode"] == (
            "persistent" if state == "no-transcript" else "incognito"
        ), observed


class TestOverrides:
    def test_agent_override_round_trip(self, patched):
        sm = SessionMap()
        sm.set_agent_override("slack:1.2", "researcher")
        assert sm.get_agent_override("slack:1.2") == "researcher"
        assert SessionMap().get_agent_override("slack:1.2") == "researcher"

    def test_agent_override_clear(self, patched):
        sm = SessionMap()
        sm.set_agent_override("slack:1.2", "researcher")
        sm.set_agent_override("slack:1.2", None)
        assert sm.get_agent_override("slack:1.2") is None

    def test_project_override_round_trip(self, patched):
        sm = SessionMap()
        sm.set_project_override("slack:1.2", "/home/u/proj")
        assert sm.get_project_override("slack:1.2") == "/home/u/proj"
        assert SessionMap().get_project_override("slack:1.2") == "/home/u/proj"

    def test_missing_override_is_none(self, patched):
        sm = SessionMap()
        assert sm.get_agent_override("nope") is None
        assert sm.get_project_override("nope") is None


class TestNoClobber:
    def test_flag_preserves_sid(self, patched):
        tmp, kiro = patched
        _make_kiro_session(kiro, "sid-abc")
        sm = SessionMap()
        sm.set("slack:1.2", "sid-abc")
        sm.set_flag("slack:1.2", "temporary", True)
        # reload: both the live sid and the flag survive together
        sm2 = SessionMap()
        assert sm2.get("slack:1.2") == "sid-abc"
        assert sm2.get_flag("slack:1.2", "temporary") is True

    def test_flag_preserves_slack_link(self, patched):
        sm = SessionMap()
        sm.set_slack_link("slack:1.2", "1.2", "C1")
        sm.set_agent_override("slack:1.2", "researcher")
        sm2 = SessionMap()
        assert sm2.get_slack_link("slack:1.2") == ("1.2", "C1")
        assert sm2.get_agent_override("slack:1.2") == "researcher"
        # reverse index for challenge-redirect resume is intact
        assert sm2.get_session_for_thread("1.2") == "slack:1.2"


class TestGenerationFloor:
    def test_explicit_generation_survives_reload_and_prune(self, patched):
        tmp, _ = patched
        bucket = "discord:kirocrew:direct:u1"
        sm = SessionMap()

        sm.reserve_generation(f"{bucket}:gen4")

        reloaded = SessionMap()
        assert reloaded.max_generation(bucket) == 4
        assert reloaded.prune() == 0
        assert reloaded.max_generation(bucket) == 4
        raw = json.loads((tmp / "session_map.json").read_text(encoding="utf-8"))
        assert raw[bucket]["generation_floor"] == 4
        assert f"{bucket}:gen4" not in raw

    def test_generation_floor_is_monotonic_and_supports_unified_keys(self, patched):
        sm = SessionMap()
        sm.reserve_generation("discord:kirocrew:direct:u1:gen5")
        sm.reserve_generation("discord:kirocrew:direct:u1:gen2")
        sm.reserve_generation("unified:kirocrew:gen3")

        assert sm.max_generation("discord:kirocrew:direct:u1") == 5
        assert sm.max_generation("unified:kirocrew") == 3

    def test_non_dm_key_is_rejected(self, patched):
        with pytest.raises(ValueError, match="not a canonical DM session key"):
            SessionMap().reserve_generation("dashboard:chat-1")
