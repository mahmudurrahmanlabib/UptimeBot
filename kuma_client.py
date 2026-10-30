"""The ONLY place that talks to Uptime Kuma.

Everything else (Slack handlers, scheduler) goes through KumaClient, so if your
Uptime Kuma version needs a different integration (see README → "Version
compatibility"), this is the one file you swap. The public surface used by the
rest of the app is:

    with KumaClient() as kc:
        monitor_id = kc.add_monitor(name, url)
        kc.delete_monitor(monitor_id)
        kc.rename_monitor(monitor_id, new_name)
        info = kc.get_status(monitor_id)        # {'status': 'up', 'ping': 123, 'msg': '200 - OK'}
        ids  = kc.list_monitor_ids()            # {1, 2, 3}

KumaClient opens a fresh connection per `with` block, which keeps it robust
across threads (Slack handlers + the scheduler run on different threads). After
the first password login it reuses Kuma's session token, so frequent connections
don't trip Kuma's login rate limit or re-use a 2FA code (which Kuma rejects).
"""
import logging
import math
import threading

import config

log = logging.getLogger("uptimebot.kuma")

# Session token from the last password login, shared by every KumaClient.
_token_lock = threading.Lock()
_session_token = None

# How much heartbeat history to read for a status check: enough to always
# include the latest beat, rather than a whole day of beats per site per poll.
_BEAT_WINDOW_HOURS = max(1, math.ceil(config.MONITOR_INTERVAL * 3 / 3600))

try:
    from uptime_kuma_api import UptimeKumaApi, MonitorType
    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - only hit when dependency missing
    UptimeKumaApi = None
    MonitorType = None
    _IMPORT_ERROR = exc

try:
    import pyotp
except Exception:  # pragma: no cover
    pyotp = None

# Uptime Kuma heartbeat status codes -> human labels.
STATUS_MAP = {0: "down", 1: "up", 2: "pending", 3: "maintenance"}


class KumaError(Exception):
    """Raised for any failure talking to Uptime Kuma, with a user-friendly message."""


def _save_session(token, only_if=None):
    """Remember (or with None, forget) the session token. `only_if` makes the
    change conditional, so a stale failure can't wipe a newer token."""
    global _session_token
    with _token_lock:
        if only_if is None or _session_token == only_if:
            _session_token = token


class KumaClient:
    def __init__(self):
        self.url = config.KUMA_URL
        self.username = config.KUMA_USERNAME
        self.password = config.KUMA_PASSWORD
        self.mfa_secret = config.KUMA_MFA_SECRET
        self._api = None

    # -- connection lifecycle -------------------------------------------------
    def __enter__(self):
        if UptimeKumaApi is None:
            raise KumaError(
                f"The 'uptime-kuma-api' package isn't installed ({_IMPORT_ERROR}). "
                f"Run: pip install -r requirements.txt"
            )
        try:
            self._api = UptimeKumaApi(self.url)
        except Exception as exc:
            raise KumaError(
                f"Couldn't connect to Uptime Kuma at {self.url}: {exc}\n"
                f"If your instance is Uptime Kuma v2.x, see README → 'Version compatibility'."
            ) from exc

        if self._login_with_session():
            return self

        token = ""
        if self.mfa_secret:
            if pyotp is None:
                self._safe_disconnect()
                raise KumaError("KUMA_MFA_SECRET is set but the 'pyotp' package isn't installed.")
            token = pyotp.TOTP(self.mfa_secret).now()

        try:
            result = self._api.login(self.username, self.password, token)
        except Exception as exc:
            self._safe_disconnect()
            raise KumaError(
                f"Uptime Kuma login failed for user '{self.username}': {exc}\n"
                f"Check KUMA_USERNAME / KUMA_PASSWORD (and KUMA_MFA_SECRET if 2FA is on)."
            ) from exc
        if isinstance(result, dict) and result.get("tokenRequired"):
            # Kuma answers a 2FA account's password-only login without an error.
            self._safe_disconnect()
            raise KumaError(
                f"Uptime Kuma user '{self.username}' has 2FA enabled. Set KUMA_MFA_SECRET to "
                f"its TOTP secret, or use a dedicated bot account without 2FA."
            )
        _save_session(result.get("token") if isinstance(result, dict) else None)
        return self

    def _login_with_session(self):
        """Log in with the saved session token. False if there isn't one or Kuma
        no longer accepts it (password changed, server secret rotated, …)."""
        with _token_lock:
            token = _session_token
        if not token:
            return False
        try:
            self._api.login_by_token(token)
            return True
        except Exception as exc:
            log.info("saved Kuma session not accepted (%s); logging in with password", exc)
            _save_session(None, only_if=token)
            return False

    def __exit__(self, *exc):
        self._safe_disconnect()
        self._api = None

    def _safe_disconnect(self):
        if self._api is not None:
            try:
                self._api.disconnect()
            except Exception:
                pass

    # -- operations -----------------------------------------------------------
    def add_monitor(self, name, url, interval=None, retries=None):
        try:
            result = self._api.add_monitor(
                type=MonitorType.HTTP,
                name=name,
                url=url,
                interval=interval or config.MONITOR_INTERVAL,
                maxretries=retries if retries is not None else config.MONITOR_RETRIES,
            )
        except Exception as exc:
            raise KumaError(f"Failed to add monitor: {exc}") from exc
        monitor_id = result.get("monitorId") or result.get("monitorID")
        if not monitor_id:
            raise KumaError(f"Unexpected response when adding monitor: {result}")
        return int(monitor_id)

    def delete_monitor(self, monitor_id):
        try:
            self._api.delete_monitor(int(monitor_id))
        except Exception as exc:
            raise KumaError(f"Failed to delete monitor {monitor_id}: {exc}") from exc

    def rename_monitor(self, monitor_id, new_name):
        try:
            self._api.edit_monitor(int(monitor_id), name=new_name)
        except Exception as exc:
            # Cosmetic only; callers may ignore.
            raise KumaError(f"Failed to rename monitor {monitor_id}: {exc}") from exc

    def get_status(self, monitor_id):
        """Return {'status': <label>, 'ping': <ms or None>, 'msg': <Kuma's reason>}
        from the latest heartbeat."""
        try:
            beats = self._api.get_monitor_beats(int(monitor_id), _BEAT_WINDOW_HOURS)
        except Exception as exc:
            raise KumaError(f"Failed to read status for monitor {monitor_id}: {exc}") from exc
        if not beats:
            return {"status": "pending", "ping": None, "msg": ""}
        last = max(beats, key=lambda b: (str(b.get("time") or ""), b.get("id") or 0))
        return {
            "status": STATUS_MAP.get(last.get("status"), "pending"),
            "ping": last.get("ping"),
            "msg": str(last.get("msg") or "").strip(),
        }

    def list_monitor_ids(self):
        try:
            monitors = self._api.get_monitors()
        except Exception as exc:
            raise KumaError(f"Failed to list monitors: {exc}") from exc
        return {int(m["id"]) for m in monitors}
