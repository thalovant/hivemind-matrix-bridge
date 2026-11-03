"""The part of the Matrix client-server API this bridge uses, and nothing else.

A bridge needs to know who it is (``/account/whoami``), to receive what
happens in its rooms (``/sync``), to accept an invite (``/rooms/{id}/join``),
to count a room's members (``/rooms/{id}/joined_members``), to look up one
event (``/rooms/{id}/event/{id}``) and to post a message
(``/rooms/{id}/send``). That is small enough to speak directly over HTTP,
which keeps the bridge off the unmaintained ``matrix-client`` library and
gives it exact control over the two things a managed bridge must get right:
a refused access token is told apart from every other failure, and the token
never leaves the ``Authorization`` header (it is never in a URL, so it cannot
end up in a proxy or library log line).

End-to-end encryption is not implemented: it needs a persistent device key
store per bot. :class:`hm_matrix_bridge.HiveMindMatrixBridge` refuses to
answer in an encrypted room instead.
"""
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote

import requests
from ovos_utils.log import LOG

#: Client-server API prefix (Matrix spec v1.1 and later).
API_PREFIX = "/_matrix/client/v3"

#: Error codes that mean the access token itself is no good, whatever the
#: HTTP status says. A 401 always means that too.
REJECTED_TOKEN_ERRCODES = frozenset({
    "M_UNKNOWN_TOKEN",
    "M_MISSING_TOKEN",
    "M_USER_DEACTIVATED",
    "M_USER_LOCKED",
})

#: Seconds to wait for an ordinary request.
DEFAULT_REQUEST_TIMEOUT = 30.0

#: Seconds to leave between two sends. A homeserver rate limits a client
#: that talks faster than its budget; pacing keeps the bridge out of that.
DEFAULT_MIN_SEND_INTERVAL = 1.0

#: How many times one send is tried when the homeserver rate limits it.
MAX_SEND_ATTEMPTS = 3

#: Longest wait honoured for one rate-limited send, in seconds. A server that
#: asks for longer gets the message dropped instead of a stalled bridge.
MAX_RATE_LIMIT_WAIT = 30.0

#: Wait used when a 429 names no ``retry_after_ms``.
DEFAULT_RATE_LIMIT_WAIT = 1.0

#: A send held this long was almost certainly held by the server's rate limit.
SLOW_SEND_WARNING_SECONDS = 2.0


class MatrixError(Exception):
    """A request the homeserver answered with an error.

    Carries the HTTP status, the Matrix ``errcode`` and, for a rate limit, how
    long the server asked the client to wait. It never carries the server's
    free-text ``error`` field or anything from the request.
    """

    def __init__(self, status: int, errcode: Optional[str] = None,
                 retry_after_ms: Optional[int] = None):
        self.status = status
        self.errcode = errcode
        self.retry_after_ms = retry_after_ms
        super().__init__(self.describe())

    def describe(self) -> str:
        """``HTTP <status> <errcode>``: safe to log."""
        return f"HTTP {self.status}" + (f" {self.errcode}" if self.errcode else "")

    @property
    def rate_limited(self) -> bool:
        """Whether the homeserver asked the client to slow down."""
        return self.status == 429 or self.errcode == "M_LIMIT_EXCEEDED"


class TokenRejected(MatrixError):
    """The homeserver refused the access token: retrying cannot help."""


class AccountMismatch(TokenRejected):
    """The access token works, but for another account than the one configured."""

    def __init__(self, expected: str, actual: Optional[str]):
        self.expected = expected
        self.actual = actual
        super().__init__(200, "ACCOUNT_MISMATCH")

    def describe(self) -> str:
        return (f"the access token belongs to {self.actual or 'an unknown account'}, "
                f"not {self.expected}")


def _quote(value: str) -> str:
    """One path segment: room and event ids carry ``!``, ``$`` and ``:``."""
    return quote(value, safe="")


class MatrixClient:
    """A minimal, thread-safe Matrix client-server API client.

    Each thread gets its own HTTP session: the sync loop long-polls on one
    thread while replies are sent from the HiveMind bus thread, and a
    ``requests.Session`` is not meant to be shared between threads.
    """

    def __init__(self, homeserver: str, access_token: str, *,
                 timeout: float = DEFAULT_REQUEST_TIMEOUT,
                 min_send_interval: float = DEFAULT_MIN_SEND_INTERVAL,
                 session_factory: Callable[[], requests.Session] = requests.Session,
                 sleep: Callable[[float], None] = time.sleep):
        """
        Parameters
        ----------
        homeserver: base URL of the client API, e.g. ``https://matrix.example.org``.
        access_token: the bot account's access token. Sent only in the
            ``Authorization`` header.
        timeout: seconds to wait for an ordinary request.
        min_send_interval: seconds to leave between two sends.
        session_factory: makes one HTTP session per thread (tests).
        sleep: how to wait (tests).
        """
        self.homeserver = homeserver.rstrip("/")
        self._token = access_token
        self.timeout = timeout
        self.min_send_interval = float(min_send_interval)
        self._session_factory = session_factory
        self._sleep = sleep
        self._local = threading.local()
        self._send_lock = threading.Lock()
        self._last_send = 0.0
        self._txn_prefix = uuid.uuid4().hex[:12]
        self._txn_counter = 0

    def __repr__(self) -> str:  # never show the token
        return f"MatrixClient({self.homeserver!r})"

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------
    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._session_factory()
            self._local.session = session
        return session

    def request(self, method: str, path: str, *,
                params: Optional[Dict[str, Any]] = None,
                body: Optional[Dict[str, Any]] = None,
                timeout: Optional[float] = None) -> Dict[str, Any]:
        """One API call; the decoded JSON answer, or :class:`MatrixError`.

        Raises :class:`TokenRejected` for a refused token and lets
        ``requests.RequestException`` (network failures) through unchanged.
        """
        response = self._session().request(
            method,
            f"{self.homeserver}{API_PREFIX}{path}",
            params=params,
            json=body,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=self.timeout if timeout is None else timeout,
        )
        if response.status_code >= 400:
            raise self._error(response)
        if not response.content:
            return {}
        try:
            answer = response.json()
        except ValueError:
            raise MatrixError(response.status_code, "NOT_JSON") from None
        return answer if isinstance(answer, dict) else {}

    @staticmethod
    def _error(response: requests.Response) -> MatrixError:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            payload = {}
        errcode = payload.get("errcode") if isinstance(payload.get("errcode"), str) else None
        retry_after_ms = payload.get("retry_after_ms")
        if not isinstance(retry_after_ms, int) or isinstance(retry_after_ms, bool):
            retry_after_ms = None
            header = response.headers.get("Retry-After") if response.headers else None
            try:
                retry_after_ms = int(float(header) * 1000) if header else None
            except ValueError:
                retry_after_ms = None
        if response.status_code == 401 or errcode in REJECTED_TOKEN_ERRCODES:
            return TokenRejected(response.status_code, errcode, retry_after_ms)
        return MatrixError(response.status_code, errcode, retry_after_ms)

    # ------------------------------------------------------------------
    # endpoints
    # ------------------------------------------------------------------
    def whoami(self) -> Dict[str, Any]:
        """``{"user_id": ..., "device_id": ...}`` for the access token."""
        return self.request("GET", "/account/whoami")

    def display_name(self, user_id: str) -> Optional[str]:
        """The account's global display name, or ``None`` when it has none."""
        try:
            answer = self.request("GET", f"/profile/{_quote(user_id)}/displayname")
        except TokenRejected:
            raise
        except MatrixError as error:
            if error.status == 404:
                return None
            raise
        name = answer.get("displayname")
        return name if isinstance(name, str) and name.strip() else None

    def sync(self, since: Optional[str] = None, timeout_ms: int = 0,
             filter_json: Optional[str] = None) -> Dict[str, Any]:
        """One ``/sync``: long-polls up to ``timeout_ms`` for new events."""
        params: Dict[str, Any] = {"timeout": int(timeout_ms)}
        if since:
            params["since"] = since
        if filter_json:
            params["filter"] = filter_json
        return self.request("GET", "/sync", params=params,
                            timeout=self.timeout + timeout_ms / 1000.0)

    def join(self, room_id: str) -> Dict[str, Any]:
        """Join (or accept the invite to) ``room_id``."""
        return self.request("POST", f"/rooms/{_quote(room_id)}/join", body={})

    def joined_members(self, room_id: str) -> Dict[str, Any]:
        """``{"joined": {user_id: {...}}}`` for ``room_id``."""
        return self.request("GET", f"/rooms/{_quote(room_id)}/joined_members")

    def event(self, room_id: str, event_id: str) -> Dict[str, Any]:
        """One event of ``room_id``."""
        return self.request("GET", f"/rooms/{_quote(room_id)}/event/{_quote(event_id)}")

    def next_txn_id(self) -> str:
        """A transaction id unique for this process and token."""
        with self._send_lock:
            self._txn_counter += 1
            return f"hm{self._txn_prefix}{self._txn_counter}"

    def send_message(self, room_id: str, content: Dict[str, Any]) -> Optional[str]:
        """Post one ``m.room.message``; its event id.

        Sends are paced ``min_send_interval`` apart across every caller. A
        rate-limited send is retried with the same transaction id (so the
        homeserver never posts it twice) at most ``MAX_SEND_ATTEMPTS`` times,
        each time waiting what the server asked, up to
        ``MAX_RATE_LIMIT_WAIT``. Then the :class:`MatrixError` is raised.
        """
        txn_id = self.next_txn_id()
        path = f"/rooms/{_quote(room_id)}/send/m.room.message/{_quote(txn_id)}"
        for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
            try:
                return self._paced("PUT", path, content).get("event_id")
            except TokenRejected:
                raise
            except MatrixError as error:
                if not error.rate_limited or attempt == MAX_SEND_ATTEMPTS:
                    raise
                wait = (error.retry_after_ms / 1000.0 if error.retry_after_ms is not None
                        else DEFAULT_RATE_LIMIT_WAIT)
                if wait > MAX_RATE_LIMIT_WAIT:
                    raise
                self._sleep(wait)
        return None  # unreachable: the last attempt returns or raises

    def _paced(self, method: str, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        with self._send_lock:
            wait = self.min_send_interval - (time.monotonic() - self._last_send)
            if wait > 0:
                self._sleep(wait)
            started = time.monotonic()
            try:
                return self.request(method, path, body=body)
            finally:
                self._last_send = time.monotonic()
                held = self._last_send - started
                if held >= SLOW_SEND_WARNING_SECONDS:
                    LOG.warning(f"the homeserver held a send for {held:.1f}s, which is "
                                "what its rate limit looks like")
