"""MetatubeConnectionState — runtime-only connection & availability state (TASK-63a-4).

Singleton (`metatube_state`) tracks:
- Whether OpenAver is connected to a metatube HTTP server.
- Per-provider availability (key = 'metatube:{ProviderName}', matches config `id` field).
- Thread-safe: all mutations and reads are guarded by a reentrant lock (RLock).

Design notes:
- NOT persistent: nothing is written to config.json / DB / disk.
- `_availability` keys are kept on disconnect (bulk set to False) so the UI
  can render grey capsules for known-but-unavailable providers (63b).
- Use RLock (not Lock) so that future callers within the same thread (e.g. a
  method that calls another method internally) won't deadlock.  RLock is a
  strict superset of Lock and matches the house pattern in
  core/similar/ranker_cache.py and web/routers/notifications.py.

Usage:
    from core.metatube.state import metatube_state

    metatube_state.connect(base_url, token, ['FANZA', 'HEYZO'])
    metatube_state.availability_map()   # -> {'metatube:FANZA': True, ...}
    metatube_state.disconnect()
"""
import threading

from core.logger import get_logger

logger = get_logger(__name__)


class MetatubeConnectionState:
    """Thread-safe, runtime-only metatube connection & provider availability state."""

    def __init__(self) -> None:
        # Use RLock: reentrant, so same-thread nested acquisitions won't deadlock.
        self._lock: threading.RLock = threading.RLock()
        self.connected: bool = False
        self.base_url: str | None = None
        self.token: str | None = None
        self._availability: dict[str, bool] = {}   # key = 'metatube:{ProviderName}'
        self._providers: list[str] = []            # raw ProviderName list
        # Probe progress tracking (CD-63b-2)
        self._probe_done: bool = True
        self._probe_progress: int = 0
        # Generation counter — incremented on every connect/disconnect so that
        # in-flight probes from a prior session can be detected and ignored.
        self._generation: int = 0

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def connect(self, base_url: str, token: str, provider_names: list[str]) -> int:
        """Mark as connected and bulk-set all named providers to available.

        Repeated connect rebuilds availability from scratch: _availability is
        cleared first, then bulk-set to True for all providers in
        `provider_names`.  Stale keys from a previous connection to a different
        server are removed, preventing phantom-available providers from being
        routed to a server that no longer serves them.

        Args:
            base_url: Base URL of the metatube HTTP server (e.g. 'http://host:8080').
            token:    API token (empty string means no auth required).
            provider_names: Raw provider names WITHOUT 'metatube:' prefix
                            (e.g. ['FANZA', 'HEYZO']).

        Returns:
            The generation counter this connect set (captured under the lock).
            Callers that fire a generation-fenced probe AFTER an await/threadpool
            boundary MUST use this returned value rather than re-reading
            `state.generation` later — a concurrent connect/disconnect could have
            advanced the global generation in between (66 Codex P2 race).
        """
        with self._lock:
            self._availability = {}  # clear stale keys before rebuilding
            self.connected = True
            self.base_url = base_url
            self.token = token
            self._providers = list(provider_names)
            for name in provider_names:
                self._availability[f'metatube:{name}'] = True
            self._generation += 1
            gen = self._generation  # capture while lock held — atomic with the bump
        logger.debug(
            'MetatubeConnectionState.connect: base_url=%r providers=%r',
            base_url, provider_names,
        )
        return gen

    def disconnect(self) -> None:
        """Mark as disconnected; bulk-set all known providers to unavailable.

        Keys are preserved (not cleared) so the UI can still render grey
        capsules for previously known providers (63b feature).

        Also resets probe progress to a terminal state: bumping the generation
        means any in-flight probe's generation-guarded set_probe_done() is
        skipped, so disconnect() must clear _probe_done itself — otherwise
        /status would report probe_done=false forever after a disconnect during
        an active probe (Codex P2).
        """
        with self._lock:
            self.connected = False
            self.base_url = None
            self.token = None
            self._providers = []
            for key in self._availability:
                self._availability[key] = False
            self._generation += 1
            self._probe_done = True
            self._probe_progress = 0
        logger.debug('MetatubeConnectionState.disconnect')

    # ------------------------------------------------------------------
    # Probe progress setters (CD-63b-2) — all Lock-guarded
    # ------------------------------------------------------------------

    def set_probe_started(self, generation: int | None = None) -> None:
        """Mark probe as in-progress and reset progress counter."""
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            self._probe_done = False
            self._probe_progress = 0

    def set_probe_progress(self, done: int, total: int, generation: int | None = None) -> None:
        """Update probe progress counter."""
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            self._probe_progress = done

    def set_probe_done(self, generation: int | None = None) -> None:
        """Mark probe as completed."""
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            self._probe_done = True

    def mark_failed(self, source_id: str, generation: int | None = None) -> None:
        """Set a single provider source to unavailable.

        Works on unknown source_ids (creates entry with False value).

        Args:
            source_id:  Full source id including prefix, e.g. 'metatube:FANZA'.
            generation: If provided, write is skipped when current generation
                        differs (stale probe guard).
        """
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            self._availability[source_id] = False

    def mark_available(self, source_id: str, generation: int | None = None) -> None:
        """Set a single provider source to available.

        Works on unknown source_ids (creates entry with True value).

        Args:
            source_id:  Full source id including prefix, e.g. 'metatube:FANZA'.
            generation: If provided, write is skipped when current generation
                        differs (stale probe guard).
        """
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            self._availability[source_id] = True

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def availability_map(self) -> dict[str, bool]:
        """Return a shallow copy of the current availability dict.

        Returns a copy so that external callers (e.g. get_enabled_source_ids,
        fan-out routing) cannot accidentally mutate the internal state during
        concurrent probe operations.
        """
        with self._lock:
            return dict(self._availability)

    def is_available(self, source_id: str) -> bool:
        """Return True iff the source is currently marked available.

        Unknown source_ids return False (safe default).
        """
        with self._lock:
            return self._availability.get(source_id, False)

    def probe_snapshot(self) -> tuple[list[str], int, str, str]:
        """Atomically snapshot (names, generation, base_url, token) for firing a probe.

        Single _lock acquisition so /test cannot dance with a concurrent
        connect/disconnect across separate reads (CD-66b-3). `names` is sourced
        from `_availability` keys (stripped of the 'metatube:' prefix) — NOT
        `_providers` — to preserve the existing behaviour that /test still lists
        previously-known providers after a disconnect (which clears _providers but
        keeps _availability keys set False).
        """
        with self._lock:
            names = [k.split(":", 1)[1] for k in self._availability]
            gen = self._generation
            base_url = self.base_url or ""
            token = self.token or ""
            return names, gen, base_url, token

    # ------------------------------------------------------------------
    # Read-only properties
    # ------------------------------------------------------------------

    @property
    def generation(self) -> int:
        """Current generation counter.  Incremented on every connect/disconnect."""
        with self._lock:
            return self._generation

    @property
    def is_connected(self) -> bool:
        """True if currently connected to a metatube server."""
        with self._lock:
            return self.connected

    @property
    def provider_count(self) -> int:
        """Number of providers registered at the last connect()."""
        with self._lock:
            return len(self._providers)

    @property
    def probe_done(self) -> bool:
        """True when no probe is currently running (initial state = True)."""
        with self._lock:
            return self._probe_done

    @property
    def probe_progress(self) -> int:
        """Number of providers probed so far in the current probe run."""
        with self._lock:
            return self._probe_progress

    def status_dict(self) -> dict:
        """Return a snapshot dict for the /status endpoint (CD-63b-2).

        Keys: connected, base_url, probe_done, probe_progress, providers.
        """
        with self._lock:
            return {
                "connected": self.connected,       # runtime, NOT config
                "base_url": self.base_url,
                "probe_done": self._probe_done,
                "probe_progress": self._probe_progress,
                "providers": [
                    {"id": k, "available": v}
                    for k, v in self._availability.items()
                ],
            }


# ---------------------------------------------------------------------------
# Module-level singleton (placed after class definition per house convention)
# ---------------------------------------------------------------------------
metatube_state = MetatubeConnectionState()
