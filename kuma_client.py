"""The ONLY place that talks to Uptime Kuma.

Everything else (Slack handlers, scheduler) goes through KumaClient, so if your
Uptime Kuma version needs a different integration (see README → "Version
compatibility"), this is the one file you swap. The public surface used by the
rest of the app is:

    with KumaClient() as kc:
        monitor_id = kc.add_monitor(name, url)
        kc.delete_monitor(monitor_id)
        kc.rename_monitor(monitor_id, new_name)
        info = kc.get_status(monitor_id)        # {'status': 'up', 'ping': 123}
        ids  = kc.list_monitor_ids()            # {1, 2, 3}

KumaClient opens a fresh connection per `with` block and logs in, which keeps it
robust across threads (Slack handlers + the scheduler run on different threads).
"""
import logging

import config

log = logging.getLogger("uptimebot.kuma")

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

        token = ""
        if self.mfa_secret:
            if pyotp is None:
                self._safe_disconnect()
                raise KumaError("KUMA_MFA_SECRET is set but the 'pyotp' package isn't installed.")
            token = pyotp.TOTP(self.mfa_secret).now()

        try:
            self._api.login(self.username, self.password, token)
        except Exception as exc:
            self._safe_disconnect()
            raise KumaError(
                f"Uptime Kuma login failed for user '{self.username}': {exc}\n"
                f"Check KUMA_USERNAME / KUMA_PASSWORD (and KUMA_MFA_SECRET if 2FA is on)."
            ) from exc
        return self

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
        """Return {'status': <label>, 'ping': <ms or None>} from the latest heartbeat."""
        try:
            beats = self._api.get_monitor_beats(int(monitor_id), 24)
        except Exception as exc:
            raise KumaError(f"Failed to read status for monitor {monitor_id}: {exc}") from exc
        if not beats:
            return {"status": "pending", "ping": None}
        last = beats[-1]
        return {
            "status": STATUS_MAP.get(last.get("status"), "pending"),
            "ping": last.get("ping"),
        }

    def list_monitor_ids(self):
        try:
            monitors = self._api.get_monitors()
        except Exception as exc:
            raise KumaError(f"Failed to list monitors: {exc}") from exc
        return {int(m["id"]) for m in monitors}
