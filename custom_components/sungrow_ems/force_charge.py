"""Force charge to a target SoC, then put every setting back.

State machine (exposed by the "Force charge state" sensor):

    idle ──start──▶ charging ──SoC ≥ target──▶ target_reached
                       │
                       ├─ timeout / cancel / HA stop / write failure ──▶ aborted
                       └─ setting changed outside HA ──▶ aborted (nothing written back)

Methods:

  ``ihm_compulsory`` (default with an iHM): iHM 8026 = power kW, 8025 = 0xAA
      (charge), 8024 = 5 (Compulsory, the app's "Forced mode"). The iHM has no
      heartbeat for Compulsory mode, so this integration must end it: on the
      target, on the timeout, on cancel and on Home Assistant shutdown. The
      session is persisted before the first write, so after a crash or power
      cut the settings are restored on the next start.
  ``inverter_compulsory`` (no iHM only): inverter 13052 = W, 13051 = 0xAA,
      13050 = 2. Never used behind an iHM: the iHM owns the inverter (13050
      reads 3, External EMS) and the two would fight.
  ``reserve``: raises the Backup reserve (13100) to the target. Per [INV]
      3.3.2 the inverter then emergency-charges on grid until SoC reaches
      the reserve and holds it there itself, even if Home Assistant stops.
      Charge power is chosen by the inverter. The reserve is put back when
      the target is reached.

Before anything is written, every setting the method touches is read fresh
from the device (FC03, not the cached poll) and stored. Restores write
those exact values back in reverse order, the mode first, and are verified
by read-back. The inverter's forced-charging schedule (33208-33219) and the
EV charger mode are captured read-only for the record: this feature never
writes them, and a warning is logged if they changed during the session.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components import persistent_notification
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .modbus import SungrowModbusError
from .registers import Table
from .writer import WriteError, WriteRefused

if TYPE_CHECKING:
    from .controller import SungrowController
    from .coordinator import SungrowEmsConfigEntry

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION: Final = 1
#: Seconds between retries of a restore that failed (device offline).
RESTORE_RETRY_SECONDS: Final = 60.0
#: Polls of the iHM settings showing another mode before the session is
#: treated as taken over from outside.
EXTERNAL_CHANGE_POLLS: Final = 2
SCHEDULE_START: Final = 33208
SCHEDULE_WORDS: Final = 12
IHM_MODE_COMPULSORY: Final = 5
IHM_MODE_SELF_CONSUMPTION: Final = 1
IHM_MODE_VPP: Final = 4
CMD_CHARGE: Final = 0xAA


class ForceChargeState(StrEnum):
    IDLE = "idle"
    CHARGING = "charging"
    TARGET_REACHED = "target_reached"
    ABORTED = "aborted"


class Method(StrEnum):
    IHM_COMPULSORY = "ihm_compulsory"
    INVERTER_COMPULSORY = "inverter_compulsory"
    RESERVE = "reserve"


#: (key, how the value is chosen) in apply order. Restore runs in reverse.
_PLAN: Final[dict[Method, tuple[str, ...]]] = {
    Method.IHM_COMPULSORY: ("ihm_charge_power", "ihm_charge_command", "ihm_ems_mode"),
    Method.INVERTER_COMPULSORY: (
        "inverter_charge_power",
        "inverter_charge_command",
        "inverter_ems_mode",
    ),
    Method.RESERVE: ("backup_reserve_soc",),
}


@dataclass(slots=True)
class Session:
    """One forced charge. Persisted while active."""

    method: str
    target_soc: float
    power_kw: float | None
    started: str
    deadline: str
    start_soc: float | None
    previous: dict[str, float | int] = field(default_factory=dict)
    applied: list[str] = field(default_factory=list)
    schedule: list[int] | None = None
    ev_charger_mode: float | None = None
    restore_pending: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


class ForceChargeManager:
    """Runs at most one forced charge per config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SungrowEmsConfigEntry,
        controller: SungrowController,
        *,
        default_timeout_minutes: int,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.controller = controller
        self.default_timeout_minutes = int(default_timeout_minutes)
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.force_charge.{entry.entry_id}"
        )
        self.state = ForceChargeState.IDLE
        self.session: Session | None = None
        self.last_reason: str | None = None
        self.last_ended: str | None = None
        self.last_session: Session | None = None
        self._unsub_timeout: CALLBACK_TYPE | None = None
        self._unsub_listeners: list[Callable[[], None]] = []
        self._unsub_retry: CALLBACK_TYPE | None = None
        self._external_polls = 0
        self._finishing = False

    # -- persistence ------------------------------------------------------------

    async def async_load(self) -> None:
        """Called at setup. A stored session means HA stopped mid-charge."""
        data = await self._store.async_load()
        if not data or not data.get("session"):
            return
        session = Session.from_dict(data["session"])
        _LOGGER.warning(
            "A forced charge (%s, target %s %%) was still active when Home "
            "Assistant stopped; restoring the previous settings",
            session.method,
            session.target_soc,
        )
        self.session = session
        await self._async_finish(ForceChargeState.ABORTED, "restart")

    async def _async_save(self) -> None:
        await self._store.async_save(
            {"session": asdict(self.session) if self.session is not None else None}
        )

    # -- public -------------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self.state is ForceChargeState.CHARGING

    def default_method(self) -> Method:
        if self.controller.has_ihm:
            return Method.IHM_COMPULSORY
        return Method.INVERTER_COMPULSORY

    async def async_start(
        self,
        target_soc: float,
        *,
        power_kw: float | None = None,
        timeout_minutes: int | None = None,
        method: Method | str | None = None,
    ) -> Session:
        """Start a forced charge. Raises ServiceValidationError / HomeAssistantError."""
        if self.active or self._finishing:
            raise _invalid("force_charge_active", "a forced charge is already running")
        chosen = Method(method) if method else self.default_method()
        if chosen is Method.INVERTER_COMPULSORY and self.controller.has_ihm:
            raise _invalid(
                "force_charge_method",
                "inverter_compulsory is not allowed behind an iHomeManager "
                "(it drives the inverter in External EMS mode); use ihm_compulsory or reserve",
            )
        if chosen is Method.IHM_COMPULSORY and not self.controller.has_ihm:
            raise _invalid(
                "force_charge_method", "ihm_compulsory needs the iHomeManager"
            )

        soc = self.controller.battery_soc()
        if soc is None:
            raise _invalid("force_charge_soc", "battery SoC is not available")
        max_soc = self.controller.value("mb_max_soc") or 100.0
        if not 0 < target_soc <= min(100.0, max_soc):
            raise _invalid(
                "force_charge_soc",
                f"target must be above 0 and at most Max SoC ({max_soc:g} %)",
            )
        if soc >= target_soc:
            raise _invalid(
                "force_charge_soc",
                f"battery is already at {soc:g} % (target {target_soc:g} %)",
            )
        if chosen is Method.RESERVE:
            # 13100 has 1 % resolution: aim for exactly what the inverter holds.
            target_soc = float(round(target_soc))
            power_kw = None
            low, _high = self.controller.reserve_bounds()
            if target_soc < low:
                raise _invalid(
                    "force_charge_soc", f"reserve target below Min SoC {low:g} %"
                )
        else:
            power_kw = self._power(chosen, power_kw)

        timeout = int(timeout_minutes or self.default_timeout_minutes)
        now = dt_util.utcnow()
        session = Session(
            method=str(chosen),
            target_soc=float(target_soc),
            power_kw=power_kw,
            started=now.isoformat(),
            deadline=(now + timedelta(minutes=timeout)).isoformat(),
            start_soc=soc,
        )

        # 1. Fresh snapshot of everything this method touches.
        writer = self.controller.writer
        try:
            for key in _PLAN[chosen]:
                writer.check(key)
                session.previous[key] = await writer.async_read(key)
        except WriteRefused as err:
            raise _invalid("write_refused", str(err)) from err
        except (WriteError, SungrowModbusError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_failed",
                translation_placeholders={"error": f"snapshot failed: {err}"},
            ) from err
        if chosen is Method.IHM_COMPULSORY:
            mode = int(session.previous["ihm_ems_mode"])
            if mode == IHM_MODE_VPP:
                raise _invalid(
                    "force_charge_mode",
                    "the iHM is in VPP mode (an external system controls it)",
                )
            if (
                mode not in (IHM_MODE_SELF_CONSUMPTION, IHM_MODE_COMPULSORY)
                and not self.controller.allow_unverified
            ):
                raise _invalid(
                    "force_charge_mode",
                    f"the iHM EMS mode code {mode} is not mapped, so restoring it is "
                    "untested; switch to Self-consumption in the app first, or turn on "
                    "'Allow unverified writes'",
                )
        session.schedule = await self._read_schedule()
        session.ev_charger_mode = self.controller.value("ihm_charger_mode")

        # 2. Persist before the first write, so a crash still restores.
        self.session = session
        self.state = ForceChargeState.CHARGING
        self.last_reason = None
        self._external_polls = 0
        await self._async_save()

        # 3. Apply. Any failure restores what was already changed.
        try:
            for key in _PLAN[chosen]:
                await writer.async_write(
                    key,
                    self._apply_value(chosen, key, session),
                    bounds=self._bounds(key),
                )
                session.applied.append(key)
                await self._async_save()
        except (WriteError, SungrowModbusError) as err:
            _LOGGER.warning("Forced charge could not start: %s", err)
            await self._async_finish(ForceChargeState.ABORTED, f"start failed: {err}")
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        _LOGGER.info(
            "Forced charge started: %s to %s %% (from %s %%)%s, timeout %d min",
            chosen,
            target_soc,
            soc,
            f" at {power_kw} kW" if power_kw is not None else "",
            timeout,
        )
        self._arm(timeout * 60.0)
        self.controller.async_notify()
        return session

    async def async_cancel(self) -> None:
        if not self.active:
            raise _invalid("force_charge_idle", "no forced charge is running")
        await self._async_finish(ForceChargeState.ABORTED, "cancelled")

    async def async_shutdown(self) -> None:
        """HA stop or entry unload: restore now (the iHM has no heartbeat)."""
        if self.active and not self._finishing:
            await self._async_finish(ForceChargeState.ABORTED, "shutdown")
        self._disarm()
        if self._unsub_retry is not None:
            self._unsub_retry()
            self._unsub_retry = None

    def diagnostics(self) -> dict[str, Any]:
        return {
            "state": str(self.state),
            "session": asdict(self.session) if self.session else None,
            "last_session": asdict(self.last_session) if self.last_session else None,
            "last_reason": self.last_reason,
            "last_ended": self.last_ended,
        }

    def attributes(self) -> dict[str, Any]:
        """Attributes for the state sensor."""
        session = self.session or self.last_session
        attrs: dict[str, Any] = {"reason": self.last_reason, "ended": self.last_ended}
        if session is not None:
            attrs.update(
                {
                    "method": session.method,
                    "target_soc": session.target_soc,
                    "power_kw": session.power_kw,
                    "started": session.started,
                    "deadline": session.deadline,
                    "start_soc": session.start_soc,
                    "previous_settings": dict(session.previous),
                    "restore_pending": session.restore_pending,
                }
            )
        return attrs

    # -- monitoring -------------------------------------------------------------

    def _arm(self, timeout_s: float) -> None:
        self._disarm()
        self._unsub_timeout = async_call_later(self.hass, timeout_s, self._on_timeout)
        for coordinator in self.controller.runtime.coordinators:
            self._unsub_listeners.append(
                coordinator.async_add_listener(self._on_update)
            )

    def _disarm(self) -> None:
        if self._unsub_timeout is not None:
            self._unsub_timeout()
            self._unsub_timeout = None
        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()

    @callback
    def _on_timeout(self, _now: datetime) -> None:
        self._unsub_timeout = None
        if self.active:
            self.hass.async_create_task(
                self._async_finish(ForceChargeState.ABORTED, "timeout"),
                eager_start=True,
            )

    @callback
    def _on_update(self) -> None:
        if not self.active or self._finishing or self.session is None:
            return
        session = self.session
        soc = self.controller.battery_soc()
        if soc is not None and soc >= session.target_soc:
            self.hass.async_create_task(
                self._async_finish(ForceChargeState.TARGET_REACHED, f"SoC {soc:g} %"),
                eager_start=True,
            )
            return
        if self._changed_externally(session):
            self._external_polls += 1
            if self._external_polls >= EXTERNAL_CHANGE_POLLS:
                self.hass.async_create_task(
                    self._async_finish(
                        ForceChargeState.ABORTED,
                        "changed outside Home Assistant",
                        restore=False,
                    ),
                    eager_start=True,
                )
        else:
            self._external_polls = 0

    def _changed_externally(self, session: Session) -> bool:
        method = Method(session.method)
        if method is Method.IHM_COMPULSORY:
            mode = self.controller.value("ihm_ems_mode")
            return mode is not None and int(mode) != IHM_MODE_COMPULSORY
        if method is Method.INVERTER_COMPULSORY:
            mode = self.controller.value("mb_ems_mode")
            return mode is not None and int(mode) != 2
        reserve = self.controller.value("mb_backup_reserve_soc")
        return reserve is not None and abs(reserve - session.target_soc) > 0.5

    # -- finishing --------------------------------------------------------------

    async def _async_finish(
        self, state: ForceChargeState, reason: str, *, restore: bool = True
    ) -> None:
        if self.session is None or self._finishing:
            return
        self._finishing = True
        self._disarm()
        session = self.session
        try:
            ok = await self._async_restore(session) if restore else True
            if not restore:
                _LOGGER.warning(
                    "Forced charge stopped: a setting was changed outside Home "
                    "Assistant, so the previous settings were not written back"
                )
            await self._check_schedule(session)
        finally:
            self._finishing = False
        self.state = state
        self.last_reason = reason
        self.last_ended = dt_util.utcnow().isoformat()
        if ok:
            session.restore_pending = False
            self.last_session = session
            self.session = None
            await self._async_save()
            if self._unsub_retry is not None:
                self._unsub_retry()
                self._unsub_retry = None
            _LOGGER.info("Forced charge ended (%s): %s", state, reason)
        else:
            session.restore_pending = True
            await self._async_save()
            self._notify_restore_failed(session)
            self._schedule_retry()
        self.controller.async_notify()

    async def _async_restore(self, session: Session) -> bool:
        """Write the previous values back, mode first. True if all verified."""
        method = Method(session.method)
        ok = True
        for key in reversed(_PLAN[method]):
            if key not in session.previous:
                continue
            if key not in session.applied and not session.restore_pending:
                # Never written in this session (start failed before it). It
                # may still have been touched if HA died mid-write, so check.
                try:
                    current = await self.controller.writer.async_read(key)
                except (WriteError, SungrowModbusError):
                    current = None
                if current == session.previous[key]:
                    continue
            try:
                await self.controller.writer.async_write(
                    key, session.previous[key], restore=True
                )
            except (WriteError, SungrowModbusError) as err:
                ok = False
                _LOGGER.error(
                    "Could not restore %s to %s: %s", key, session.previous[key], err
                )
        return ok

    def _schedule_retry(self) -> None:
        if self._unsub_retry is not None:
            return

        @callback
        def _retry(_now: datetime) -> None:
            self._unsub_retry = None
            if self.session is not None and self.session.restore_pending:
                self.hass.async_create_task(
                    self._async_finish(
                        ForceChargeState.ABORTED, self.last_reason or "retry"
                    ),
                    eager_start=True,
                )

        self._unsub_retry = async_call_later(self.hass, RESTORE_RETRY_SECONDS, _retry)

    def _notify_restore_failed(self, session: Session) -> None:
        previous = ", ".join(f"{k} = {v}" for k, v in session.previous.items())
        persistent_notification.async_create(
            self.hass,
            "Sungrow EMS could not put the settings back after a forced charge "
            f"({session.method}). It retries every minute. Previous values: "
            f"{previous}. Check the iSolarCloud app if this does not clear.",
            title="Sungrow EMS: forced charge restore pending",
            notification_id=f"{DOMAIN}_force_charge_{self.entry.entry_id}",
        )

    # -- helpers ----------------------------------------------------------------

    def _power(self, method: Method, power_kw: float | None) -> float:
        if method is Method.IHM_COMPULSORY:
            # Default: the settable upper limit (8149). The iHM itself holds
            # the actual power to what the battery allows right now (8151).
            limit = self.controller.value("ihm_battery_power_limit")
            default = limit or self.controller.value("ihm_max_charge_power")
        else:
            rated_w = self.controller.value("mb_bdc_rated_power")
            limit = rated_w / 1000.0 if rated_w else None
            default = limit
        if power_kw is None:
            if default is None:
                raise _invalid(
                    "force_charge_power", "give power_kw: no charge limit is reported"
                )
            power_kw = float(default)
        if power_kw <= 0 or (limit is not None and power_kw > limit + 1e-9):
            raise _invalid(
                "force_charge_power",
                f"power must be above 0 and at most {limit:g} kW"
                if limit
                else "power must be above 0",
            )
        return round(float(power_kw), 1)

    def _apply_value(self, method: Method, key: str, session: Session) -> float | int:
        if key == "backup_reserve_soc":
            return round(session.target_soc)
        if key == "ihm_charge_power":
            return float(session.power_kw or 0)
        if key == "inverter_charge_power":
            return round((session.power_kw or 0) * 1000)
        if key in ("ihm_charge_command", "inverter_charge_command"):
            return CMD_CHARGE
        if key == "ihm_ems_mode":
            return IHM_MODE_COMPULSORY
        if key == "inverter_ems_mode":
            return 2
        raise KeyError(key)

    def _bounds(self, key: str) -> tuple[float | None, float | None] | None:
        if key == "backup_reserve_soc":
            return self.controller.reserve_bounds()
        if key == "ihm_charge_power":
            return self.controller.ihm_power_bounds()
        if key == "inverter_charge_power":
            return self.controller.inverter_power_bounds()
        return None

    async def _read_schedule(self) -> list[int] | None:
        """Inverter forced-charging schedule 33208-33219, read-only record."""
        hub = self.controller.runtime.inverter.hub
        try:
            return await hub.async_read_raw(
                Table.HOLDING, SCHEDULE_START, SCHEDULE_WORDS
            )
        except SungrowModbusError as err:
            _LOGGER.debug("Forced-charging schedule not readable: %s", err)
            return None

    async def _check_schedule(self, session: Session) -> None:
        if session.schedule is None:
            return
        now = await self._read_schedule()
        if now is not None and now != session.schedule:
            _LOGGER.warning(
                "The inverter's forced-charging schedule (33208-33219) changed during "
                "the forced charge: was %s, now %s. This integration does not write it",
                session.schedule,
                now,
            )


def _invalid(_key: str, message: str) -> ServiceValidationError:
    """Validation error. ``_key`` names the check (kept for readability)."""
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="force_charge_invalid",
        translation_placeholders={"error": message},
    )
