"""One-time startup migration: prune the crewmates an older agent sync generated.

An enrol-on-mount build of the dashboard called ``POST /api/agents/sync`` on
every chat mount, and that sync enrolled EVERY user-authored spec under
``~/.kiro/agents`` as a crewmate --
a ``config.agents`` row with no ``member_id``, on the shared ``default`` memory
store, bound to the spec by name. An existing install therefore carries one
crewmate per custom agent, most of them never opened. This module runs once at
gateway startup and:

* **removes** each such crewmate that was never chatted with as a crewmate --
  no DM thread on the Crewmates page, no session that recorded it as its
  agent, and no entry in its member activity log -- by deleting its
  ``config.agents`` row (:func:`remove_never_chatted`);
* **leaves the chatted ones exactly as they are**: on the shared ``default``
  store, with no ``member_id``. A memory binding is identity and is chosen only
  at creation; an existing member keeps its exact V1 binding (see
  ``memory-skills-hooks.md``, "Member memory experience and lifecycle"), and no
  startup pass rewrites it.

Design:

* **Precise identification.** A row is a candidate only when it is EXACTLY
  what the sync wrote: its name is its ``kiro_agent``, that spec is on disk,
  user-authored (``source == "builtin"``), not the runtime's own and not a
  crew's private copy, its ``description`` is the spec's current description
  (the sync copied it from there; a description the owner rewrote is a
  customization), and every other field sits at its default -- no
  ``member_id``, the shared ``default`` store, no model, effort, triggers,
  colour, star, avatar or workspace, and no key the record does not declare
  (:func:`_is_fresh_sync_shape`, tested on the RAW row as ``config.json`` holds
  it, a missing key reading as its default). Both config
  layers are consulted: a name that ``config.local.json`` touches in its own
  ``agents`` section -- ``kirocrew config set --local agents.<name>.model``,
  the capability writer's overlay binding -- is the owner's and is never a
  candidate, because deleting the base row would leave the overlay leaf as a
  crewmate bound to nothing. A row the owner touched in any of those ways is
  the owner's; a hand-made crewmate has a ``member_id``; a package's spec has
  another source; a row whose spec is gone is left alone.
* **Kept on doubt, and the pass always finishes.** Removal is decided from
  three kinds of evidence, each read STRICTLY by this module -- never through
  the roster's total-by-contract readers, which answer "absent" for a damaged
  directory or file: every session file's metadata line
  (:func:`_agents_named_in_history`), the crewmate's member activity log --
  the per-session pointer ``record_activity`` appends when a chat runs as that
  member, which survives a later agent switch on the same slot
  (:func:`_activity_names_member`) -- and the crewmate's DM binding file
  (:func:`_chatted`). Doubt is per CANDIDATE: a candidate whose own activity
  log or binding cannot be read is kept and listed under ``doubted``; a session
  file that cannot be read or parsed is skipped as evidence -- it is neither
  for nor against anyone -- and listed under ``unreadable_sessions``; no
  conversation log at all keeps every candidate. A candidate that none of the
  three sources names is removed. The pass always completes and writes the
  marker; it never loops boot after boot on one bad file.
* **Agent-writable paths are opened defensively.** The session files and the
  legacy activity files are opened with ``open_file_no_reparse`` (``O_NOFOLLOW``
  / reparse-point refusal settled in the same operation as the open,
  ``O_NONBLOCK`` so a FIFO cannot hang the pass) and read only when ``fstat``
  says regular file -- anything else is "no record". The legacy activity file
  is streamed under ``MAX_LEGACY_ACTIVITY_BYTES``, the same budget the event
  log's own fold applies; over budget is doubt for that candidate.
* **Serialized against every session-agent writer, off the boot path.** The
  gateway clears ``DashboardState.crewmate_prune_settled`` BEFORE the listener
  binds (``_register_crewmate_prune_gate``); the pass itself is kicked as a
  tracked background task right after the bind (``_kick_crewmate_prune``) and
  sets the event in ``finally``, so readiness is not gated by a scan whose cost
  scales with the session count, while the slot restores wait on the event so
  a removed crewmate's DM slot is not rebuilt. That
  function's middleware holds every mutating request on it -- the chat send,
  slot create, slot agent switch, member thread, channel and import routes
  under ``/api/`` and the OpenAI-compatible ``POST /v1/chat/completions`` are
  all such requests -- so no session can bind an agent, and no DM binding can
  appear, between the history snapshot and a candidate's delete. Nothing else
  writes sessions while the pass runs: the cron scheduler, the Slack socket
  and the channel relaunches start after ``start_dashboard`` returns, and the
  pass completes inside it. Each candidate's check runs immediately before its
  own removal, never once for the whole list.
* **A refused delete is not a commit.** The delete re-tests the row inside
  the base config lock AND, nested, the overlay's own lock: the base row must
  still carry the same ``kiro_agent`` and the fresh-sync shape, and the overlay
  must still not name it. A row that changed meanwhile is refused, the pass
  writes no marker and logs which rows, and the next boot re-judges them.
* **Agent files are never touched.** Only ``config.json`` rows move. The specs
  under ``~/.kiro/agents`` and any transcript on disk stay exactly as they are.
* **Idempotent, marker-gated.** A completed pass (even a no-op) writes
  :data:`PRUNE_MARKER` under the config directory with what it did -- the same
  marker-file seam the config loader's own one-shot migrations use
  (``CONNECTIONS_UI_MIGRATION_MARKER``); the next boot finds the marker and
  returns at once. It runs from ``start_dashboard`` rather than inside the
  loader because the decision needs chat history, which only the running
  gateway has. Removed rows do not come back: nothing in the dashboard calls
  ``POST /api/agents/sync`` (``useAgents`` reads the catalog), so the rows this
  pass removes come only from installs that ran an enrol-on-mount build.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import stat
import time
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    coerce_dict_section,
    config_local_path,
    read_config_for_update,
    update_config_locked,
)
from kiro_crew.config.paths import config_dir
from kiro_crew.platform_compat import open_file_no_reparse

logger = logging.getLogger(__name__)

#: Written under the config directory once a pass completes. Its body is the
#: record of what the pass did, so an operator can see which crewmates left
#: and which were kept because their history could not be read.
PRUNE_MARKER = "crewmate_prune_migrated.json"

#: The discovery ``source`` of a user-authored spec under ``~/.kiro/agents``
#: (``kirocrew`` = the runtime's own, ``package`` = installed by a package).
#: Tested on the SPEC the row is bound to, never on the row's own stamp.
USER_SPEC_SOURCE = "builtin"


class HistoryUnreadable(RuntimeError):
    """A piece of chat history could not be read.

    Raised by the strict readers below and caught by :func:`prune_synced_crewmates`,
    which keeps the crewmates the unreadable piece could have vouched for.
    """


@dataclasses.dataclass
class PruneReport:
    removed: list[str] = dataclasses.field(default_factory=list)
    kept: list[str] = dataclasses.field(default_factory=list)
    #: Kept because the candidate's own history could not be read; reason per name.
    doubted: dict[str, str] = dataclasses.field(default_factory=dict)
    #: Session files skipped as evidence because they could not be read or parsed.
    unreadable_sessions: list[str] = dataclasses.field(default_factory=list)
    refused: list[str] = dataclasses.field(default_factory=list)
    skipped_marker: bool = False


def marker_path() -> Path:
    return config_dir() / PRUNE_MARKER


def _raw_agents_section(path: Path | None = None) -> dict:
    """The ``agents`` section exactly as one config file holds it.

    ``path`` defaults to ``config.json``; pass :func:`config_local_path` for the
    overlay. A file that is absent reads as an empty section; one that is
    present but unreadable raises ``ConfigReadError`` (fail closed: the pass
    cannot judge a layer it cannot see).
    """
    doc = read_config_for_update(path)
    agents = doc.get("agents") if isinstance(doc, dict) else None
    return agents if isinstance(agents, dict) else {}


def _fresh_sync_row(kiro_agent: str, description: str, source: str) -> dict:
    """What ``_do_agents_sync`` wrote for one spec: the binding, the spec's
    description and source, every other field at its default."""
    return dataclasses.asdict(
        KiroCrewAgentConfig(kiro_agent=kiro_agent, description=description, source=source)
    )


def _is_fresh_sync_shape(raw: dict, *, kiro_agent: str, description: str) -> bool:
    """Whether a RAW ``config.agents`` row is exactly a sync-written row.

    Compared field by field against :func:`_fresh_sync_row` built from the
    binding and the SPEC's current ``description`` (the sync copied it from the
    spec, so a row whose description differs from the spec's was edited by the
    owner -- or the spec moved on -- and is kept either way); every other
    declared field must equal its default -- a row the owner gave a model,
    triggers, a colour, a star, an avatar or a workspace is the owner's and is
    never a candidate -- and the row may carry no key the record does not
    declare, since an unknown key is something a writer other than the sync put
    there. A declared key the row lacks reads as its default: a row written by
    a build whose record had fewer fields is still the sync's row.
    """
    expected = _fresh_sync_row(kiro_agent, description, USER_SPEC_SOURCE)
    if set(raw) - set(expected):
        return False
    for key, default in expected.items():
        if raw.get(key, default) != default:
            return False
    return True


def _synced_candidates(
    cfg: KiroCrewConfig, raw_agents: dict, overlay_agents: dict
) -> dict[str, str]:
    """The crewmates an older sync generated, in config order, each with the
    current description of the spec it is bound to.

    ``raw_agents`` is the ``agents`` section as ``config.json`` holds it (not
    the default-filled dataclasses): the shape test must see the row the file
    holds, and the same test -- against the same spec description -- is re-run
    inside the delete's lock. ``overlay_agents`` is the same section from
    ``config.local.json``; any name it mentions is excluded, whatever it says
    about it.
    """
    from kiro_crew.agent import kiro_agents_dir_path
    from kiro_crew.agent_discovery import list_agents

    specs = {info.name: info for info in list_agents(agents_dir=kiro_agents_dir_path())}
    out: dict[str, str] = {}
    for name, agent in cfg.agents.items():
        if name in ("default", cfg.default_agent):
            continue
        if name in overlay_agents:
            continue
        raw = raw_agents.get(name)
        if not isinstance(raw, dict) or name != agent.kiro_agent:
            continue
        spec = specs.get(agent.kiro_agent)
        if (
            spec is None
            or spec.source != USER_SPEC_SOURCE
            or spec.kirocrew_owned
            or spec.private_to
        ):
            continue
        if not spec.filename:
            continue
        description = str(spec.description or "")
        if not _is_fresh_sync_shape(raw, kiro_agent=agent.kiro_agent, description=description):
            continue
        out[name] = description
    return out


#: Longest metadata line the session scan reads. A real metadata record is a few
#: hundred bytes; the cap bounds the per-file cost on an agent-writable tree.
_SESSION_META_LINE_MAX = 64 * 1024


def _read_first_line_of_regular_file(path: Path, budget: int) -> str | None:
    """The first line of *path* when it is a plain regular file, else ``None``.

    Opened with ``open_file_no_reparse``: the link/reparse refusal is settled
    in the same operation as the open (no check-then-open window), and
    ``O_NONBLOCK`` makes a FIFO return at once so ``fstat`` can refuse it
    instead of the open waiting for a writer that never comes. Anything that
    is not a regular file is ``None`` -- "no record" -- never a hang. Reads at
    most ``budget`` bytes. I/O and decode failures propagate to the caller.
    """
    fd = open_file_no_reparse(path, nonblocking=True)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            raw = fh.readline(budget + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    return raw.decode("utf-8").strip()


def _agents_named_in_history(conversation_log) -> tuple[set[str], list[str]]:
    """Every agent any session's metadata line names, plus the files skipped.

    ``ConversationLog.agent_usage()`` is built on ``list_sessions()``, which
    skips a file it cannot stat and swallows a first line it cannot read or
    parse. Here every ``*.jsonl`` in the history directory is opened
    (:func:`_read_first_line_of_regular_file`) and its first line parsed by
    this module. A file that cannot be read or parsed is SKIPPED as evidence
    -- it names nobody and it condemns nobody -- and returned in the second
    element so the pass can record it. A first line that is not a metadata
    record simply names no agent (that is the contract ``list_sessions``
    applies too); a link or a non-regular file is no record.

    The ``agent`` field is slot-owned and rewritten in place when the slot
    switches agents, so it names the LAST agent a session ran as; a session
    that ran as a crewmate and later switched is found through the member
    activity log instead (:func:`_activity_names_member`).
    """
    history_dir = getattr(conversation_log, "_dir", None)
    if not isinstance(history_dir, Path):
        raise HistoryUnreadable("conversation log exposes no history directory")
    named: set[str] = set()
    unreadable: list[str] = []
    try:
        if not history_dir.exists():
            return named, unreadable
        paths = list(history_dir.glob("*.jsonl"))
    except OSError as exc:
        raise HistoryUnreadable(f"could not list session history: {exc}") from exc
    for path in paths:
        try:
            first = _read_first_line_of_regular_file(path, _SESSION_META_LINE_MAX)
        except FileNotFoundError:
            continue  # gone between the listing and the open
        except (OSError, UnicodeError):
            unreadable.append(path.name)
            continue
        if not first:
            continue
        try:
            record = json.loads(first)
        except ValueError:
            unreadable.append(path.name)
            continue
        if isinstance(record, dict) and record.get("_type") == "metadata":
            agent = record.get("agent")
            if isinstance(agent, str) and agent:
                named.add(agent)
    return named, unreadable


def _activity_names_member(slug: str, name: str) -> bool:
    """Whether the member activity log under ``slug`` records a session for ``name``.

    ``record_activity`` appends one ``activity/record`` event per session a chat
    ran as this member, carrying the exact member name (slugs collide) and the
    session key; it is written once per session and never rewritten, so it
    survives the slot switching to another agent afterwards. Two places hold
    it: the member event log, and -- on an install that has not yet folded it
    -- the pre-log ``activity.jsonl`` / ``activity.jsonl.1`` in the member
    directory. Both are read here, STRICTLY and read-only: nothing is created
    or folded, an event log that cannot be loaded or a legacy file that cannot
    be read or parsed raises :class:`HistoryUnreadable`. Only a log and files
    that do not exist -- or that are not regular files -- read as "no record".

    The legacy path is agent-writable, so it is opened through
    ``open_file_no_reparse`` (no link following, no FIFO hang) and streamed
    line by line under ``MAX_LEGACY_ACTIVITY_BYTES``, the budget the event
    log's own fold applies to the same file; a file over budget is doubt.
    """
    from kiro_crew import members as members_mod
    from kiro_crew.eventlog.log import MemberLog
    from kiro_crew.eventlog.service import MAX_LEGACY_ACTIVITY_BYTES
    from kiro_crew.eventlog.types import ACTIVITY_RECORD

    try:
        log = MemberLog(slug)
        if log.exists():
            # Streamed oldest-first without retaining: the store's own read
            # path, which refuses (``LogCorrupt``) rather than guesses.
            for event in log.iter_events():
                if event.get("type") != ACTIVITY_RECORD:
                    continue
                data = event.get("data") or {}
                if data.get("member") == name and data.get("session"):
                    return True
    except Exception as exc:  # noqa: BLE001 -- a log that will not load is "unknown"
        raise HistoryUnreadable(f"could not read {name!r}'s activity log: {exc}") from exc
    try:
        base = members_mod.member_dir(slug) / members_mod.ACTIVITY_FILE_NAME
    except Exception as exc:  # noqa: BLE001
        raise HistoryUnreadable(f"could not resolve {name!r}'s activity file: {exc}") from exc
    for path in (base.with_name(base.name + ".1"), base):
        try:
            fd = open_file_no_reparse(path, nonblocking=True)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise HistoryUnreadable(f"could not open {name!r}'s activity file: {exc}") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                continue
            fh = os.fdopen(fd, "rb")
        except OSError as exc:
            os.close(fd)
            raise HistoryUnreadable(f"could not read {name!r}'s activity file: {exc}") from exc
        budget = MAX_LEGACY_ACTIVITY_BYTES
        try:
            with fh:
                while True:
                    line = fh.readline(budget + 1)
                    if not line:
                        break
                    budget -= len(line)
                    if budget < 0:
                        raise HistoryUnreadable(
                            f"{name!r}'s activity file {path.name} exceeds "
                            f"{MAX_LEGACY_ACTIVITY_BYTES} bytes"
                        )
                    text = line.decode("utf-8").strip()
                    if not text:
                        continue
                    row = json.loads(text)
                    if isinstance(row, dict) and row.get("member") == name and row.get("session"):
                        return True
        except HistoryUnreadable:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            raise HistoryUnreadable(
                f"could not read {name!r}'s activity file {path.name}: {exc}"
            ) from exc
    return False


def _chatted(cfg: KiroCrewConfig, name: str, named: set[str]) -> bool:
    """Whether any chat history records this crewmate.

    Three sources, any suffices: a session whose metadata names it as the agent
    (``named``, from :func:`_agents_named_in_history`); the crewmate's member
    activity log (:func:`_activity_names_member`); or the crewmate's own DM
    thread on the Crewmates page -- the thread route writes the binding file
    the first time the owner opens the thread, so a binding that names this
    crewmate IS the evidence, whether or not a message was ever sent.

    The binding is read STRICTLY here, not through ``read_dm_binding``: that
    reader is total by contract and answers "not bound" for an unreadable
    directory, an unreadable file and a malformed payload alike, which a
    removal must never mistake for "never opened". Only a binding file that
    does not exist reads as never opened. Every other failure -- the path
    cannot be resolved (a containment refusal included), the file cannot be
    statted or read, the payload does not parse -- raises
    :class:`HistoryUnreadable`, and the caller keeps the crewmate.
    """
    from kiro_crew import members as members_mod
    from kiro_crew.atomic_write import read_bytes_with_retry

    if name in named:
        return True
    try:
        slug = members_mod.member_slug(name, cfg)
    except members_mod.MemberSlugError:
        # No slug means no DM thread and no activity log can exist; the usage
        # read above covers the rest.
        return False
    if _activity_names_member(slug, name):
        return True
    try:
        path = members_mod.dm_binding_path(slug)
    except Exception as exc:  # noqa: BLE001 -- an unresolvable path is "unknown", never "no"
        # ``MemberSlugError`` included: the slug passed ``member_slug`` above, so
        # here it means the containment check refused a path that resolves
        # outside the trust root -- a binding that may exist, not one that does not.
        raise HistoryUnreadable(f"could not resolve {name!r}'s DM binding: {exc}") from exc
    try:
        raw = read_bytes_with_retry(path)
    except FileNotFoundError:
        return False
    except Exception as exc:  # noqa: BLE001 -- present but unreadable is "unknown"
        raise HistoryUnreadable(f"could not read {name!r}'s DM binding: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise HistoryUnreadable(f"{name!r}'s DM binding does not parse: {exc}") from exc
    if not isinstance(data, dict):
        raise HistoryUnreadable(f"{name!r}'s DM binding is not a record")
    # A colliding slug's binding belongs to exactly one crew name; one that
    # names another crew is legitimately not this crewmate's thread.
    return data.get("member") == name


def remove_never_chatted(
    cfg: KiroCrewConfig, candidates: dict[str, str]
) -> tuple[list[str], list[str]]:
    """Delete the ``config.agents`` rows named; returns ``(removed, refused)``.

    ``candidates`` maps each name to its spec's current description, as
    :func:`_synced_candidates` returned it. Each delete re-runs the candidate
    test on the ROWS AS THE FILES HOLD THEM, inside the base config lock and,
    nested inside it, the overlay's own lock: the base row must carry the same
    ``kiro_agent``, still be the fresh-sync shape against that same spec
    description (:func:`_is_fresh_sync_shape`), and ``config.local.json`` must still
    not name it. A row that changed meanwhile -- the owner edited it, a
    member-aware write stamped it, an overlay leaf appeared -- is newer
    evidence and is refused, not deleted. The test is on identity and shape,
    never on equality with a default-filled snapshot: a row written by a build
    whose record had fewer keys must still be recognised as the sync's. Nothing
    but the base row moves: the overlay, the spec under ``~/.kiro/agents`` and
    any transcript stay.
    """
    removed: list[str] = []
    refused: list[str] = []
    for name, description in candidates.items():
        kiro_agent = cfg.agents[name].kiro_agent
        deleted = False

        def _mutate(
            doc: dict, _name: str = name, _bound: str = kiro_agent, _desc: str = description
        ) -> dict | None:
            nonlocal deleted
            agents = coerce_dict_section(doc, "agents")
            raw = agents.get(_name)
            if not isinstance(raw, dict) or raw.get("kiro_agent") != _bound:
                return None
            if not _is_fresh_sync_shape(raw, kiro_agent=_bound, description=_desc):
                return None
            overlay_names_it = False

            def _peek_overlay(overlay: dict) -> None:
                nonlocal overlay_names_it
                overlay_agents = overlay.get("agents")
                overlay_names_it = isinstance(overlay_agents, dict) and _name in overlay_agents

            # Held nested so the overlay cannot gain a leaf for this name between
            # the check and the delete; ``mutate`` returning None writes nothing.
            update_config_locked(config_local_path(), mutate=_peek_overlay)
            if overlay_names_it:
                return None
            del agents[_name]
            deleted = True
            return doc

        update_config_locked(mutate=_mutate)
        if deleted:
            removed.append(name)
            del cfg.agents[name]
        else:
            refused.append(name)
    return removed, refused


def _write_marker(report: PruneReport) -> None:
    body = {
        "migrated_at": time.time(),
        "removed": report.removed,
        "kept": report.kept,
        "doubted": report.doubted,
        "unreadable_sessions": report.unreadable_sessions,
    }
    marker = marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(marker, json.dumps(body, indent=2) + "\n")


def prune_synced_crewmates(conversation_log) -> PruneReport:
    """Run the pass once. Thread-side; safe to call on every boot.

    ``conversation_log`` is the gateway's :class:`~kiro_crew.history.ConversationLog`;
    ``None`` means history is unavailable, so every candidate is kept on doubt.
    Never raises :class:`HistoryUnreadable`: unreadable evidence keeps the
    crewmates it could have vouched for, and the pass still finishes.
    """
    report = PruneReport()
    marker = marker_path()
    if marker.exists():
        report.skipped_marker = True
        return report
    cfg = KiroCrewConfig.load()
    raw_agents = _raw_agents_section()
    overlay_agents = _raw_agents_section(config_local_path())
    candidates = _synced_candidates(cfg, raw_agents, overlay_agents)
    if candidates:
        named: set[str] = set()
        try:
            if conversation_log is None:
                raise HistoryUnreadable("no conversation log; removal needs chat history")
            named, report.unreadable_sessions = _agents_named_in_history(conversation_log)
        except HistoryUnreadable as exc:
            # No history at all: nothing can vouch for anyone, so everyone is kept.
            for name in candidates:
                report.doubted[name] = str(exc)
            candidates = {}
        # Check and delete ONE candidate at a time: the strict history check
        # runs immediately before its own row's removal, never once for the
        # whole list up front. The gateway holds every mutating request
        # back while the pass runs (``DashboardState.crewmate_prune_settled``,
        # armed before the listener bound), so no session can bind an agent and
        # no thread can be opened between a candidate's check and its delete.
        for name, description in candidates.items():
            try:
                chatted = _chatted(cfg, name, named)
            except HistoryUnreadable as exc:
                report.doubted[name] = str(exc)
                continue
            if chatted:
                report.kept.append(name)
            else:
                removed, refused = remove_never_chatted(cfg, {name: description})
                report.removed.extend(removed)
                report.refused.extend(refused)
    if report.unreadable_sessions:
        logger.warning(
            "crewmate prune: %d session file(s) could not be read and were not used as "
            "evidence: %s",
            len(report.unreadable_sessions),
            ", ".join(sorted(report.unreadable_sessions)),
        )
    if report.doubted:
        logger.warning(
            "crewmate prune: %d crewmate(s) kept because their history could not be read: %s",
            len(report.doubted),
            "; ".join(f"{name}: {why}" for name, why in report.doubted.items()),
        )
    if report.refused:
        # A refused delete is not a commit: the row on disk was not the row
        # judged. Nothing is recorded as done; the next boot re-judges it.
        logger.warning(
            "crewmate prune: %d row(s) changed while being judged, pass not recorded: %s",
            len(report.refused),
            ", ".join(report.refused),
        )
        return report
    _write_marker(report)
    return report
