import asyncio
import itertools
import logging
import struct
import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Self, override

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from custom_components.ecoflow_cloud.api import EcoflowApiClient
from custom_components.ecoflow_cloud.binary_sensor import MiscBinarySensorEntity
from custom_components.ecoflow_cloud.devices import const
from custom_components.ecoflow_cloud.devices.internal.delta_pro_3 import DeltaPro3
from custom_components.ecoflow_cloud.devices.internal.proto import ef_dp3_iobroker_pb2 as dp3
from custom_components.ecoflow_cloud.entities import EcoFlowAbstractDataEntity
from custom_components.ecoflow_cloud.sensor import (
    AmpSensorEntity,
    BatteryLimitSensorEntity,
    InAmpSensorEntity,
    InVoltSensorEntity,
    InWattsSensorEntity,
    LevelSensorEntity,
    MiscSensorEntity,
    QuotaStatusSensorEntity,
    VoltSensorEntity,
    WattsSensorEntity,
)

_LOGGER = logging.getLogger(__name__)

# DisplayPropertyUpload (cmdFunc 254 / cmdId 21) per-circuit array: fields
# 1015.., one submessage per circuit {1: volt, 2: watt (signed), 3: amp}.
# These fields are absent from the DP3 proto (dropped on ParseFromString),
# so they are recovered with a second raw pass over the payload.
# The circuit count, name-field blocks and array base are class attributes on
# SmartHomePanel3 (below) so wider variants (e.g. Ocean Pro, 40 circuits) can
# extend this device by overriding them rather than duplicating the decoders.
# Aggregate flow and grid per-leg fields (float, wiretype 5).
F_GRID_PWR = 515
F_LOAD_PWR = 516
F_GRID_L1_PWR, F_GRID_L2_PWR = 962, 963
F_L1_VOL, F_L2_VOL = 956, 957
F_GRID_L1_AMP, F_GRID_L2_AMP = 958, 959
# Panel state, sent only in the 120 s full frames. Names and meanings follow
# the SHP3 schema in rabits/ha-ef-ble (dev_apl_comm), which agrees with
# 1RadMan/ecoflow-cloud-dpu-extra. 619 and 461 travel in one frame, 752, 1244
# and 1245 in another.
F_GRID_STA = 619  # grid_connection_sta: 1 grid in, 2 offline, 3 feeding
F_GRID_ENERGIZED = 752
F_BACKUP_RESERVE = 461  # %
F_CHARGE_LIMIT = 1244  # W
F_PANEL_MODE = 1245  # submessage of mode flags, see PANEL_MODES
# Operating-mode flag sub-fields of 1245; none set means Backup. Sub-field 4
# (EPS) is a separate flag that combines with any of them.
PANEL_MODES = {1: "self_powered", 2: "scheduled", 3: "tou", 5: "mix_scheduled", 6: "intelligent"}
PANEL_MODE_EPS = 4
GRID_STA_ON = {1: True, 2: False, 3: True}
# 2.5 full-frame periods: one lost frame is tolerated, a dead stream is not.
PANEL_STATE_STALE_S = 300

# Scheduled tasks. Full frames carry only a change counter; the task list
# itself (TimeTaskItemV2List) arrives in the ConfigReadAck (254/20) to a
# ConfigRead (254/19) for action 129, which the app sends when its schedule
# screen opens and after each edit. The integration sends the same request.
F_TASK_CHANGE_CNT = 286  # time_task_change_cnt
F_TASK_LIST = 129  # read_time_task_v2_list
PANEL_ADDR = 11
TASK_TYPES = {0: "charge", 1: "discharge"}
TASK_DETAIL_SOC = 4
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # day mask bit 0 = Monday
# Edits made in the app are followed by the app's own read, so ours matter at
# startup and for edits from elsewhere; this caps retries if the panel ignores us.
TASK_READ_MIN_INTERVAL_S = 300

# Protobuf wire types (as tagged by _parse_fields).
WIRE_VARINT = 0
WIRE_F64 = 1
WIRE_LEN = 2
WIRE_F32 = 5

# proto3 omits a scalar field whose value is 0, and the coordinator merges
# each frame's params cumulatively — so a key that is simply not written
# keeps its previous (stale) value. For bimodal fields that feed energy
# integration (per-circuit watts, grid import) an omitted value means 0,
# not "unchanged", and must be written explicitly; anything sampled sparsely
# must instead be skipped when absent. _decode_flows / _decode_circuits
# apply this distinction per field.

FieldMap = dict[int, list[tuple[int, Any]]]


def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return r, i


def _parse_fields(b: bytes) -> FieldMap:
    """Minimal protobuf reader: {field_no: [(wire_type, value), ...]}."""
    i, n = 0, len(b)
    out: FieldMap = {}
    while i < n:
        try:
            tag, i = _read_varint(b, i)
            fn, wt = tag >> 3, tag & 7
            v: Any
            if wt == WIRE_VARINT:
                v, i = _read_varint(b, i)
            elif wt == WIRE_LEN:
                ln, i = _read_varint(b, i)
                v, i = b[i : i + ln], i + ln
            elif wt == WIRE_F32:
                v, i = struct.unpack("<f", b[i : i + 4])[0], i + 4
            elif wt == WIRE_F64:
                v, i = struct.unpack("<d", b[i : i + 8])[0], i + 8
            else:
                break
        except (IndexError, struct.error):
            break
        out.setdefault(fn, []).append((wt, v))
    return out


def _first(fields: FieldMap, no: int, wire_type: int) -> Any | None:
    """First value of a field, or None if absent or of another wire type."""
    vals = fields.get(no)
    return vals[0][1] if vals and vals[0][0] == wire_type else None


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _at(day: date, hhmm: str) -> datetime:
    """Local datetime for "HH:MM" on a day; "24:00" is the next midnight."""
    h, m = map(int, hhmm.split(":"))
    return dt_util.start_of_local_day(day) + timedelta(hours=h, minutes=m)


def _varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if not v:
            out.append(b)
            return bytes(out)
        out.append(b | 0x80)


def _len_field(no: int, data: bytes) -> bytes:
    return _varint(no << 3 | WIRE_LEN) + _varint(len(data)) + data


def _decode_task(item: bytes) -> dict[str, Any] | None:
    """One TimeTaskItemV2 as plain data, or None for an unconfigured slot."""
    t = _parse_fields(item)
    if not _first(t, 2, WIRE_VARINT):  # is_cfg
        return None
    mode = _first(t, 5, WIRE_VARINT) or 0
    param = _first(t, 6, WIRE_VARINT) or 0  # weekday mask when weekly
    # time_table packs the window as end << 16 | start, in minutes.
    table = _first(t, 7, WIRE_VARINT) or 0
    # Zero values are omitted on the wire (proto3): index 0, type 0 = charge.
    kind = _first(t, 8, WIRE_VARINT) or 0
    soc = None
    for wire_type, detail in t.get(10, []):  # task_param_detail
        d = _parse_fields(detail) if wire_type == WIRE_LEN else {}
        if _first(d, 1, WIRE_VARINT) == TASK_DETAIL_SOC:
            soc = _first(d, 2, WIRE_F32)
    task: dict[str, Any] = {
        "index": _first(t, 1, WIRE_VARINT) or 0,
        "enabled": bool(_first(t, 3, WIRE_VARINT)),
        "mode": "weekly" if mode == 1 else "once" if mode == 2 else f"mode_{mode}",
        "start": _hhmm(table & 0xFFFF),
        "end": _hhmm(table >> 16),
        "type": TASK_TYPES.get(kind, f"type_{kind}"),
        "target_soc": round(soc) if soc is not None else None,
    }
    if mode == 1:
        task["days"] = [day for bit, day in enumerate(DAYS) if param >> bit & 1]
    else:
        task["time_param"] = param
    return task


_config_read_seq = itertools.count(1)


def _config_read(device_sn: str) -> bytes:
    """A ConfigRead (254/19) to the panel, framed as the EcoFlow app frames it:
    src 32, dest 11, version 4, the action list packed, and the serial repeated
    in header fields 26 and 27, which the DP3 proto doesn't declare, `from`
    "ios" and a small seq. The panel ignores an unpacked action list, and
    ignored `from` "HomeAssistant" with a `Message.gen_seq()` seq."""
    pdata = b"\x0a\x02\x81\x01"  # action_id = [129], packed
    header = dp3.DP3Header(
        pdata=pdata,
        src=32,
        dest=PANEL_ADDR,
        d_src=1,
        d_dest=1,
        check_type=3,
        cmd_func=254,
        cmd_id=19,
        data_len=len(pdata),
        need_ack=1,
        seq=next(_config_read_seq),
        version=4,
        payload_ver=1,
        device_sn=device_sn,
    )
    setattr(header, "from", "ios")  # "from" is a Python keyword
    sn = device_sn.encode()
    return _len_field(1, header.SerializeToString() + _len_field(26, sn) + _len_field(27, sn))  # DP3SendHeaderMsg.msg


if TYPE_CHECKING:
    from custom_components.ecoflow_cloud.entities import BaseSensorEntity, EcoFlowDictEntity

    _CircuitNamedBase = BaseSensorEntity
    _PanelStateBase = EcoFlowDictEntity
else:
    _CircuitNamedBase = object
    _PanelStateBase = object


class _CircuitNamed(_CircuitNamedBase):
    """Mixin: name a per-circuit entity from the device-provided circuit label.

    The SHP3 streams circuit labels (`ch_N_name`) over the first ~minute,
    after the entities are built. The name is derived live from coordinator
    params (single source of truth, so an app-side rename is picked up too),
    falling back to the original "Circuit N …" title until the label arrives.
    """

    def for_circuit(self, n: int, suffix: str) -> Self:
        self._circuit_no = n
        self._circuit_suffix = suffix
        return self

    def _circuit_name(self) -> str:
        data = getattr(self._device, "data", None)
        params = data.params if data is not None else {}
        label = params.get(f"ch_{self._circuit_no}_name")
        if not label:
            return self._attr_name
        partner = params.get(f"ch_{self._circuit_no}_partner")
        if partner:
            # Primary leg carries the combined 240V load; the secondary reads 0.
            label += " (240V L2)" if partner < self._circuit_no else " (240V)"
        return f"{label} {self._circuit_suffix}"

    @property
    def name(self) -> str:
        return self._circuit_name()

    def title(self) -> str:
        return self._circuit_name()

    def _updated(self, data: dict[str, Any]) -> None:
        super()._updated(data)  # type: ignore[misc]
        # The base class only writes state when the value changes, so a
        # circuit holding a steady value (idle at 0 W, a flat 10 A) would keep
        # its old name forever. Write whenever the name itself changes: the
        # label first streaming in, or an app-side rename later on.
        name = self._circuit_name()
        if name != getattr(self, "_written_name", None):
            self._written_name = name
            if getattr(self, "hass", None) is not None:
                self.schedule_update_ha_state()


class NamedCircuitWatts(_CircuitNamed, WattsSensorEntity):
    pass


class NamedCircuitVolt(_CircuitNamed, VoltSensorEntity):
    pass


class NamedCircuitAmp(_CircuitNamed, AmpSensorEntity):
    pass


class _PanelState(_PanelStateBase):
    """Mixin: unavailable once the panel-state value is PANEL_STATE_STALE_S old.

    These values arrive only in the 120 s full frames and hold in between, so
    without this a lost frame stream would show the last value indefinitely.
    """

    @property
    def available(self) -> bool:
        seen = getattr(self._device, "panel_state_seen", {}).get(self.mqtt_key)
        fresh = seen is not None and time.monotonic() - seen < PANEL_STATE_STALE_S
        return super().available and fresh

    def _handle_coordinator_update(self) -> None:
        super()._handle_coordinator_update()  # type: ignore[misc]
        # The base class writes state only on a new value; going stale is not one.
        available = self.available
        if available != getattr(self, "_written_available", None):
            self._written_available = available
            self.schedule_update_ha_state()


class PanelStateBinarySensorEntity(_PanelState, MiscBinarySensorEntity):
    pass


class BackupReserveSensorEntity(_PanelState, BatteryLimitSensorEntity):
    pass


class GridStatusBinarySensorEntity(_PanelState, MiscBinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.POWER

    def _update_value(self, val: Any) -> bool:
        # 0 (no valid status) and unknown codes read as unknown, not "off".
        self._attr_is_on = GRID_STA_ON.get(val)
        return True


class ChargeLimitSensorEntity(_PanelState, MiscSensorEntity):  # type: ignore[misc]
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT


class PanelModeSensorEntity(_PanelState, MiscSensorEntity):  # type: ignore[misc]
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["backup", *PANEL_MODES.values()]


class ScheduleCalendarEntity(EcoFlowAbstractDataEntity, CalendarEntity):
    """The panel's weekly scheduled tasks as a read-only calendar.

    The full task list (including disabled and one-off tasks) is the `tasks`
    attribute, kept out of the recorder; `current` is false while the change
    counter is ahead of the last list read (an edit not read back yet).
    """

    _attr_icon = "mdi:calendar-clock"
    _unrecorded_attributes = frozenset({"tasks"})

    def __init__(self, client: EcoflowApiClient, device: "SmartHomePanel3") -> None:
        super().__init__(client, device, "Schedule", "shp_schedule")
        self._panel = device

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.now()
        return next(iter(self._panel.schedule_events(now, now + timedelta(days=8))), None)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "tasks": self._panel.tasks or [],
            "current": self._panel.task_list_current,
            "change_count": self._panel.task_change_count,
        }

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        return self._panel.schedule_events(start_date, end_date)

    def _handle_coordinator_update(self) -> None:
        if not self._panel.task_list_current:
            self.hass.async_add_executor_job(self._panel.request_task_list, self._client)
        super()._handle_coordinator_update()


class SmartHomePanel3(DeltaPro3):
    """EcoFlow Smart Home Panel 3 (private / app API).

    The SHP3 speaks the Delta Pro 3 protobuf dialect (same header, same
    cmdFunc/cmdId routing, DisplayPropertyUpload = 254/21), so the DP3 decode
    pipeline is inherited unchanged. The SHP-specific data — a 32-circuit
    load array plus aggregate flow powers — lives in fields the DP3 proto
    doesn't declare, so it is recovered with a second raw pass per frame.
    Read-only: no control entities until actuation is deliberately in scope.

    Circuit geometry is expressed as class attributes so wider panels (e.g.
    Ocean Pro, 40 circuits) can subclass and override without touching the
    decoders.
    """

    # SHP3 has 32 monitored load circuits.
    CIRCUITS = 32
    # Circuit metadata submessages (sub-field 5 = the app's circuit label,
    # sub-field 2 = split-phase link) in two field blocks, in the same order as
    # the power array: circuit N (1-based) -> NAME_FIELDS[N - 1].
    NAME_FIELDS = list(range(794, 806)) + list(range(920, 940))
    # Per-circuit array base field in DisplayPropertyUpload (254/21).
    CIRCUIT_FIELD_BASE = 1015

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Circuit metadata (label, 240V partner), keyed by circuit number.
        self._meta: dict[int, dict[str, Any]] = {}
        # Last-seen grid/load pair, carried across frames that omit the group.
        self._flows: dict[str, float] = {}
        # Panel-state key -> monotonic time it was last decoded (see _PanelState).
        self.panel_state_seen: dict[str, float] = {}
        # Last task list read and the change counter it belongs to (None until
        # a counter has been seen with it); the latest counter; when the last
        # read was requested.
        self._tasks: list[dict[str, Any]] | None = None
        self._task_list_cnt: int | None = None
        self._task_cnt: int | None = None
        self._task_read_at: float | None = None

    @override
    def sensors(self, client: EcoflowApiClient) -> list[SensorEntity]:
        out: list[SensorEntity] = [
            # Battery SoC — DP3 field 262 (cms_batt_soc), decodes via the parent.
            LevelSensorEntity(client, self, "cms_batt_soc", const.COMBINED_BATTERY_LEVEL),
            # The three headline flows, with integrated energy (kWh,
            # total_increasing) for the HA Energy dashboard.
            WattsSensorEntity(client, self, "shp_load_pwr", "Home Load Power")
            .with_icon("mdi:home-lightning-bolt")
            .with_energy(),
            InWattsSensorEntity(client, self, "shp_grid_pwr", "Grid Power").with_energy(),
            # "Storage" = EcoFlow's term for the non-grid source (battery, and
            # any generator on the connection box), which is what load - grid
            # measures.
            WattsSensorEntity(client, self, "shp_batt_pwr", "Storage Output Power").with_icon("mdi:home-battery"),
            QuotaStatusSensorEntity(client, self),
            # Grid-side per-leg detail — disabled by default, diagnostic.
            InWattsSensorEntity(client, self, "shp_grid_l1_pwr", "Grid L1 Power", False, diagnostic=True),
            InWattsSensorEntity(client, self, "shp_grid_l2_pwr", "Grid L2 Power", False, diagnostic=True),
            InVoltSensorEntity(client, self, "shp_l1_vol", "Grid L1 Voltage", False, diagnostic=True),
            InVoltSensorEntity(client, self, "shp_l2_vol", "Grid L2 Voltage", False, diagnostic=True),
            InAmpSensorEntity(client, self, "shp_grid_l1_amp", "Grid L1 Current", False, diagnostic=True),
            InAmpSensorEntity(client, self, "shp_grid_l2_amp", "Grid L2 Current", False, diagnostic=True),
            # Panel settings, read-only. Updated every 120 s (full frame).
            BackupReserveSensorEntity(client, self, "shp_backup_reserve", "Backup Reserve"),
            PanelModeSensorEntity(client, self, "shp_panel_mode", "Operating Mode", diagnostic=False),
            ChargeLimitSensorEntity(client, self, "shp_charge_limit", "Charge Power Limit", False),
        ]
        # Per-circuit sensors: power enabled with an integrated-energy
        # companion (kWh) for the Energy dashboard; volt/amp disabled by
        # default. Titles use the device-provided circuit label once it has
        # been seen (persisted across restarts), falling back to "Circuit N".
        data = getattr(self, "data", None)
        params = data.params if data is not None else {}

        def circuit_label(n: int) -> str:
            return params.get(f"ch_{n}_name") or f"Circuit {n}"

        for n in range(1, self.CIRCUITS + 1):
            label = circuit_label(n)
            out.append(
                NamedCircuitWatts(client, self, f"ch_{n}_pwr", f"{label} Power").for_circuit(n, "Power").with_energy()
            )
            out.append(
                NamedCircuitVolt(client, self, f"ch_{n}_vol", f"{label} Voltage", False, diagnostic=True).for_circuit(
                    n, "Voltage"
                )
            )
            out.append(
                NamedCircuitAmp(client, self, f"ch_{n}_amp", f"{label} Current", False, diagnostic=True).for_circuit(
                    n, "Current"
                )
            )
        return out

    @override
    def binary_sensors(self, client: EcoflowApiClient) -> list[BinarySensorEntity]:
        return [
            GridStatusBinarySensorEntity(client, self, "shp_grid_sta", "Grid"),
            PanelStateBinarySensorEntity(
                client, self, "shp_grid_energized", "Grid Energized", False, diagnostic=True
            ).with_device_class("power"),
            PanelStateBinarySensorEntity(client, self, "shp_eps_mode", "EPS Mode", False, diagnostic=True),
        ]

    @override
    def numbers(self, client: EcoflowApiClient) -> list[NumberEntity]:
        return []

    @override
    def switches(self, client: EcoflowApiClient) -> list[SwitchEntity]:
        return []

    @override
    def selects(self, client: EcoflowApiClient) -> list[SelectEntity]:
        return []

    def configure(self, hass: HomeAssistant) -> None:
        super().configure(hass)
        self._hass = hass
        self._store: Store[dict] = Store(hass, 1, f"ecoflow_cloud.{self.device_info.sn}.circuit_names")
        self._task_store: Store[dict] = Store(hass, 1, f"ecoflow_cloud.{self.device_info.sn}.time_tasks")

    async def async_restore_state(self) -> None:
        # Load persisted circuit labels before entities are built, so names
        # are correct immediately after a restart/reload instead of showing
        # "Circuit N" until the panel re-streams its metadata.
        data = await self._store.async_load()
        if data:
            self._meta = {int(n): m for n, m in data.items()}
            self._publish_meta(self.data.params)
        # The task list is read only on request, so keep the last one across
        # restarts; the change counter says whether it still holds.
        tasks = await self._task_store.async_load()
        if tasks:
            self._tasks = tasks["tasks"]
            self._task_list_cnt = tasks["change_cnt"]

    def calendars(self, client: EcoflowApiClient) -> list[CalendarEntity]:
        return [ScheduleCalendarEntity(client, self)]

    @property
    def task_list_current(self) -> bool:
        """A task list has been read and the schedule hasn't changed since."""
        if self._tasks is None:
            return False
        return self._task_cnt is None or self._task_list_cnt is None or self._task_cnt == self._task_list_cnt

    @property
    def tasks(self) -> list[dict[str, Any]] | None:
        return self._tasks

    @property
    def task_change_count(self) -> int | None:
        return self._task_cnt

    def schedule_events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """Enabled weekly tasks expanded into events overlapping [start, end)."""
        events: list[CalendarEvent] = []
        day = dt_util.as_local(start).date() - timedelta(days=1)
        while day <= dt_util.as_local(end).date():
            for task in self._tasks or []:
                if not task["enabled"] or DAYS[day.weekday()] not in task.get("days", ()):
                    continue
                t_start = _at(day, task["start"])
                t_end = _at(day, task["end"])
                if t_end <= t_start:
                    t_end += timedelta(days=1)
                if t_start < end and t_end > start:
                    summary = task["type"].capitalize()
                    if task["target_soc"] is not None:
                        summary += f" to {task['target_soc']} %"
                    events.append(
                        CalendarEvent(
                            start=t_start,
                            end=t_end,
                            summary=summary,
                            description=f"Task {task['index']}",
                            uid=f"{task['index']}-{day.isoformat()}",
                        )
                    )
            day += timedelta(days=1)
        return sorted(events, key=lambda e: e.start)

    def request_task_list(self, client: EcoflowApiClient) -> None:
        """Ask the panel for its task list (the ConfigRead the app sends)."""
        if not client.mqtt_client.connected:
            return
        now = time.monotonic()
        if self._task_read_at is not None and now - self._task_read_at < TASK_READ_MIN_INTERVAL_S:
            return
        self._task_read_at = now
        # Published directly rather than via send_set_message, which would
        # mark the device as freshly updated.
        client.mqtt_client.publish(self.device_info.set_topic, _config_read(self.device_info.sn), qos=0)

    def _publish_meta(self, result: dict[str, Any]) -> None:
        for n, m in self._meta.items():
            if "name" in m:
                result[f"ch_{n}_name"] = m["name"]
            if "partner" in m:
                result[f"ch_{n}_partner"] = m["partner"]

    def _save_meta(self) -> None:
        # Decode runs on the MQTT client thread; hop to the event loop to
        # write the Store.
        asyncio.run_coroutine_threadsafe(
            self._store.async_save({str(n): m for n, m in self._meta.items()}),
            self._hass.loop,
        )

    @override
    def _decode_message_by_type(self, pdata: bytes, header_info: dict[str, Any]) -> dict[str, Any]:
        result = super()._decode_message_by_type(pdata, header_info)
        # Only DisplayPropertyUpload (254/21) carries the SHP-specific fields.
        if header_info.get("cmdFunc") == 254 and header_info.get("cmdId") == 21:
            try:
                fields = _parse_fields(pdata)
                self._decode_flows(fields, result)
                self._decode_panel_state(fields, result)
                self._decode_task_counter(fields)
                self._decode_circuits(fields, result)
                self._decode_metadata(fields, result)
                self._combine_split_phase(result)
            except Exception as e:  # reverse-engineered payload; never break the DP3 decode
                _LOGGER.debug("SHP3 field parse skipped: %s", e)
        # The panel's ConfigReadAck; requests (ours and the app's) share the topics.
        elif (header_info.get("cmdFunc"), header_info.get("cmdId"), header_info.get("src")) == (254, 20, PANEL_ADDR):
            self._decode_task_list(_parse_fields(pdata))
        return result

    def _decode_task_counter(self, fields: FieldMap) -> None:
        """Schedule change counter; when it passes the last read, the list is stale."""
        cnt = _first(fields, F_TASK_CHANGE_CNT, WIRE_VARINT)
        if cnt is None:
            return
        self._task_cnt = cnt
        # A list read before any counter was seen belongs to the first one.
        if self._tasks is not None and self._task_list_cnt is None:
            self._task_list_cnt = cnt
            self._save_tasks()

    def _decode_task_list(self, fields: FieldMap) -> None:
        """The task list from a TimeTaskItemV2List (unconfigured slots dropped)."""
        lst = _first(fields, F_TASK_LIST, WIRE_LEN)
        if lst is None:
            return
        tasks = [
            t
            for wire_type, item in _parse_fields(lst).get(1, [])
            if wire_type == WIRE_LEN and (t := _decode_task(item))
        ]
        # The app reads the list several times per visit; store only changes.
        if (tasks, self._task_cnt) != (self._tasks, self._task_list_cnt):
            self._tasks, self._task_list_cnt = tasks, self._task_cnt
            self._save_tasks()

    def _save_tasks(self) -> None:
        asyncio.run_coroutine_threadsafe(
            self._task_store.async_save({"tasks": self._tasks, "change_cnt": self._task_list_cnt}),
            self._hass.loop,
        )

    def _decode_flows(self, fields: FieldMap, result: dict[str, Any]) -> None:
        """Aggregate flows: home load, grid import, computed battery power."""
        # Grid import and home load travel as one group, carried only by full
        # frames. Home load is realistically never 0, so its presence marks
        # such a frame — and within it an absent grid means 0 import
        # (islanded), not "unchanged" (zero-omission, see module note).
        load = _first(fields, F_LOAD_PWR, WIRE_F32)
        if load is not None:
            result["shp_load_pwr"] = round(load, 2)
            grid = _first(fields, F_GRID_PWR, WIRE_F32) or 0.0
            result["shp_grid_pwr"] = round(grid, 2)

        # Per-leg grid detail is sampled sparsely — here absent genuinely
        # means "not reported this frame", so skip rather than zero.
        for key, no in (
            ("shp_grid_l1_pwr", F_GRID_L1_PWR),
            ("shp_grid_l2_pwr", F_GRID_L2_PWR),
            ("shp_l1_vol", F_L1_VOL),
            ("shp_l2_vol", F_L2_VOL),
            ("shp_grid_l1_amp", F_GRID_L1_AMP),
            ("shp_grid_l2_amp", F_GRID_L2_AMP),
        ):
            v = _first(fields, no, WIRE_F32)
            if v is not None:
                result[key] = round(v, 2)

        # Battery contribution = load - grid, signed (positive = battery
        # supplying the load, negative = charging through the panel). The
        # last-seen pair is cached so frames without the group still compute.
        for k in ("shp_grid_pwr", "shp_load_pwr"):
            if k in result:
                self._flows[k] = result[k]
        if len(self._flows) == 2:
            result["shp_batt_pwr"] = round(self._flows["shp_load_pwr"] - self._flows["shp_grid_pwr"], 2)

    def _decode_panel_state(self, fields: FieldMap, result: dict[str, Any]) -> None:
        """Grid status, backup reserve, operating mode, charge limit."""
        # 619 and 461 have no reliable marker for their frame, so absent means
        # "not in this frame" and the last value holds. Neither is ever 0 in
        # practice: grid offline is 2, and the app's lowest reserve is 10 %.
        now = time.monotonic()
        for key, no in (("shp_grid_sta", F_GRID_STA), ("shp_backup_reserve", F_BACKUP_RESERVE)):
            v = _first(fields, no, WIRE_VARINT)
            if v is not None:
                result[key] = v
                self.panel_state_seen[key] = now

        # The mode submessage marks the 752/1244 frame, and within it an absent
        # value means 0: grid de-energized, no charge limit (zero-omission, see
        # module note). If the frame came without it, everything here holds.
        mode = _first(fields, F_PANEL_MODE, WIRE_LEN)
        if mode is None:
            return
        flags = _parse_fields(mode)
        result["shp_panel_mode"] = next(
            (name for no, name in PANEL_MODES.items() if _first(flags, no, WIRE_VARINT)), "backup"
        )
        result["shp_eps_mode"] = bool(_first(flags, PANEL_MODE_EPS, WIRE_VARINT))
        result["shp_grid_energized"] = bool(_first(fields, F_GRID_ENERGIZED, WIRE_VARINT))
        result["shp_charge_limit"] = _first(fields, F_CHARGE_LIMIT, WIRE_VARINT) or 0
        for key in ("shp_panel_mode", "shp_eps_mode", "shp_grid_energized", "shp_charge_limit"):
            self.panel_state_seen[key] = now

    def _decode_circuits(self, fields: FieldMap, result: dict[str, Any]) -> None:
        """Per-circuit volt / watt / amp from the 32-entry array."""
        for i in range(self.CIRCUITS):
            entry = _first(fields, self.CIRCUIT_FIELD_BASE + i, WIRE_LEN)
            if entry is None:
                continue
            sub = _parse_fields(entry)
            n = i + 1
            vol = _first(sub, 1, WIRE_F32)
            watt = _first(sub, 2, WIRE_F32)
            amp = _first(sub, 3, WIRE_F32)
            # An entry is present for every circuit, so an absent sub-field
            # means 0 (zero-omission, see module note) — write it explicitly.
            result[f"ch_{n}_vol"] = round(vol, 2) if vol is not None else 0.0
            # EcoFlow signs branch consumption negative; negate to read
            # positive and clamp noise so integrated energy stays monotonic.
            result[f"ch_{n}_pwr"] = max(round(-watt, 2), 0.0) if watt is not None else 0.0
            result[f"ch_{n}_amp"] = round(amp, 2) if amp is not None else 0.0

    def _decode_metadata(self, fields: FieldMap, result: dict[str, Any]) -> None:
        """Circuit labels + 240V partner links (rotate in over several frames)."""
        before = {n: dict(m) for n, m in self._meta.items()}
        for n, field_no in enumerate(self.NAME_FIELDS, 1):
            entry = _first(fields, field_no, WIRE_LEN)
            if entry is None:
                continue
            meta = _parse_fields(entry)
            label = _first(meta, 5, WIRE_LEN)
            if label:
                try:
                    self._meta.setdefault(n, {})["name"] = label.decode("utf-8").strip().strip("\x00")
                except UnicodeDecodeError:
                    pass
            link = _first(meta, 2, WIRE_LEN)
            if link is not None:
                partner = _first(_parse_fields(link), 2, WIRE_VARINT)
                if partner is not None:
                    self._meta.setdefault(n, {})["partner"] = partner
        # Persist labels across restart/reload, but only when they change.
        if self._meta != before:
            self._save_meta()
        self._publish_meta(result)

    def _combine_split_phase(self, result: dict[str, Any]) -> None:
        """Fold each 240V pair onto its primary (lower-numbered) leg.

        The appliance then reads once — energy isn't double-counted — and
        the secondary leg reads 0.
        """
        for n, m in self._meta.items():
            p = m.get("partner")
            if p and n < p and f"ch_{n}_pwr" in result and f"ch_{p}_pwr" in result:
                result[f"ch_{n}_pwr"] = round(result[f"ch_{n}_pwr"] + result[f"ch_{p}_pwr"], 2)
                result[f"ch_{p}_pwr"] = 0.0
