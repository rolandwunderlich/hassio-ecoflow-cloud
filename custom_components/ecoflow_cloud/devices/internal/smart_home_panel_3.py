import asyncio
import logging
import struct
from typing import TYPE_CHECKING, Any, override

from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from custom_components.ecoflow_cloud.api import EcoflowApiClient
from custom_components.ecoflow_cloud.devices import const
from custom_components.ecoflow_cloud.devices.internal.delta_pro_3 import DeltaPro3
from custom_components.ecoflow_cloud.sensor import (
    AmpSensorEntity,
    InAmpSensorEntity,
    InVoltSensorEntity,
    InWattsSensorEntity,
    LevelSensorEntity,
    QuotaStatusSensorEntity,
    VoltSensorEntity,
    WattsSensorEntity,
)

_LOGGER = logging.getLogger(__name__)

# SHP3 has 32 monitored load circuits.
CIRCUITS = 32
# Per-circuit metadata submessages (sub-field 5 = app circuit label, sub-field 3 =
# breaker rating A) in two field blocks 794..805 then 920..939, in the same order as
# the 1015..1046 power array: circuit N (1-based) -> NAME_FIELDS[N-1].
NAME_FIELDS = list(range(794, 806)) + list(range(920, 940))
# DisplayPropertyUpload (cmdFunc 254 / cmdId 21) per-circuit array: fields 1015..1046,
# one submessage per circuit {1: volt, 2: watt(signed), 3: amp}. Absent from the DP3
# proto (dropped on ParseFromString), so recovered with a second raw pass below.
CIRCUIT_FIELD_BASE = 1015
# Aggregate float fields (wiretype-5): grid power (0 when islanded) and home load;
# battery contribution is computed as load - grid.
F_GRID_PWR = 515
F_LOAD_PWR = 516
F_GRID_L1_PWR, F_GRID_L2_PWR = 962, 963
F_L1_VOL, F_L2_VOL = 956, 957
F_GRID_L1_AMP, F_GRID_L2_AMP = 958, 959


def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return r, i


def _parse_fields(b: bytes) -> dict[int, list[tuple[int, Any]]]:
    """Minimal protobuf reader: {field_no: [(wire_type, value), ...]}."""
    i, n = 0, len(b)
    out: dict[int, list[tuple[int, Any]]] = {}
    while i < n:
        try:
            tag, i = _read_varint(b, i)
            fn, wt = tag >> 3, tag & 7
            v: Any
            if wt == 0:
                v, i = _read_varint(b, i)
            elif wt == 2:
                ln, i = _read_varint(b, i)
                v, i = b[i : i + ln], i + ln
            elif wt == 5:
                v, i = struct.unpack("<f", b[i : i + 4])[0], i + 4
            elif wt == 1:
                v, i = struct.unpack("<d", b[i : i + 8])[0], i + 8
            else:
                break
        except (IndexError, struct.error):
            break
        out.setdefault(fn, []).append((wt, v))
    return out


if TYPE_CHECKING:
    from custom_components.ecoflow_cloud.entities import BaseSensorEntity

    _CircuitNamedBase = BaseSensorEntity
else:
    _CircuitNamedBase = object


class _CircuitNamed(_CircuitNamedBase):
    """Mixin: name a per-circuit entity from the device-provided circuit label.

    The SHP3 streams circuit labels (`ch_N_name`) over the first ~minute, after the
    entities are built. The name is derived live from coordinator params (single
    source of truth, so an app-side rename is picked up too), falling back to the
    original "Circuit N …" title until the label arrives.
    """

    def for_circuit(self, n: int, suffix: str) -> Any:
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
        # Force one state write when the label first streams in, so idle circuits
        # (no value change) pick up their name immediately.
        if not getattr(self, "_labelled", False) and data.get(f"ch_{self._circuit_no}_name"):
            self._labelled = True
            if getattr(self, "hass", None) is not None:
                self.schedule_update_ha_state()


class NamedCircuitWatts(_CircuitNamed, WattsSensorEntity):
    pass


class NamedCircuitVolt(_CircuitNamed, VoltSensorEntity):
    pass


class NamedCircuitAmp(_CircuitNamed, AmpSensorEntity):
    pass


class SmartHomePanel3(DeltaPro3):
    """EcoFlow Smart Home Panel 3 (private / app API).

    The SHP3 speaks the Delta Pro 3 protobuf dialect (same header, same
    cmdFunc/cmdId routing, DisplayPropertyUpload = 254/21), so the DP3 decode
    pipeline is inherited unchanged. The SHP-specific data — a 32-circuit
    load array (fields 1015..1046) plus aggregate flow powers — lives in fields
    the DP3 proto doesn't declare, so they are recovered with a second raw pass.
    Read-only: no control entities until actuation is deliberately in scope (M3).
    """

    _meta: dict[int, dict[str, Any]]

    @override
    def sensors(self, client: EcoflowApiClient) -> list[Any]:
        out: list[Any] = [
            # Battery SoC — DP3 field 262 (cms_batt_soc), decodes via the parent.
            LevelSensorEntity(client, self, "cms_batt_soc", const.COMBINED_BATTERY_LEVEL),
            # The three headline flows, each with integrated energy (kWh,
            # total_increasing) for the HA Energy dashboard: home load, grid
            # import, and the battery's contribution (computed load - grid).
            WattsSensorEntity(client, self, "shp_load_pwr", "Home Load Power")
            .with_icon("mdi:home-lightning-bolt")
            .with_energy(),
            InWattsSensorEntity(client, self, "shp_grid_pwr", "Grid Power").with_energy(),
            # "Storage" = EcoFlow's term for the non-grid source (battery, and any
            # generator on the connection box), which is what load - grid measures.
            WattsSensorEntity(client, self, "shp_batt_pwr", "Storage Output Power").with_icon(
                "mdi:home-battery"
            ),
            QuotaStatusSensorEntity(client, self),
            # Grid-side per-leg detail — disabled by default, diagnostic category.
            InWattsSensorEntity(client, self, "shp_grid_l1_pwr", "Grid L1 Power", False, diagnostic=True),
            InWattsSensorEntity(client, self, "shp_grid_l2_pwr", "Grid L2 Power", False, diagnostic=True),
            InVoltSensorEntity(client, self, "shp_l1_vol", "Grid L1 Voltage", False, diagnostic=True),
            InVoltSensorEntity(client, self, "shp_l2_vol", "Grid L2 Voltage", False, diagnostic=True),
            InAmpSensorEntity(client, self, "shp_grid_l1_amp", "Grid L1 Current", False, diagnostic=True),
            InAmpSensorEntity(client, self, "shp_grid_l2_amp", "Grid L2 Current", False, diagnostic=True),
        ]
        # 32 per-circuit sensors: power enabled (the M1b payoff) with a companion
        # integrated energy sensor (kWh) for the Energy dashboard's per-device
        # breakdown; volt/amp disabled by default. Title uses the device-provided
        # circuit label (e.g. "Refrigerator") when known — cached in params from
        # the metadata submessages, which arrive over the first few frames, so real
        # names apply after the integration has seen a full cycle (else "Circuit N").
        params = getattr(self, "data", None)
        params = params.params if params is not None else {}

        def cname(n: int) -> str:
            return params.get(f"ch_{n}_name") or f"Circuit {n}"

        for n in range(1, CIRCUITS + 1):
            label = cname(n)
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

    async def async_restore_state(self) -> None:
        # Load persisted circuit labels before entities are built, so names are
        # correct immediately after a restart/reload instead of showing "Circuit N"
        # until the panel re-streams its metadata.
        data = await self._store.async_load()
        if data:
            self._meta = {int(n): m for n, m in data.items()}
            self._publish_meta(self.data.params)

    def _publish_meta(self, result: dict[str, Any]) -> None:
        for n, m in self._meta.items():
            if "name" in m:
                result[f"ch_{n}_name"] = m["name"]
            if "partner" in m:
                result[f"ch_{n}_partner"] = m["partner"]

    def _save_meta(self) -> None:
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

                def _f(no: int) -> float | None:
                    vals = fields.get(no)
                    return vals[0][1] if vals and vals[0][0] == 5 else None

                for name, no in (
                    ("shp_grid_pwr", F_GRID_PWR),
                    ("shp_load_pwr", F_LOAD_PWR),
                    ("shp_grid_l1_pwr", F_GRID_L1_PWR),
                    ("shp_grid_l2_pwr", F_GRID_L2_PWR),
                    ("shp_l1_vol", F_L1_VOL),
                    ("shp_l2_vol", F_L2_VOL),
                    ("shp_grid_l1_amp", F_GRID_L1_AMP),
                    ("shp_grid_l2_amp", F_GRID_L2_AMP),
                ):
                    v = _f(no)
                    if v is not None:
                        result[name] = round(v, 2)

                # Battery contribution = load - grid. The two fields don't always
                # share a frame (grid updates rarely while islanded), so cache the
                # last-seen values and compute from the freshest of each.
                if not hasattr(self, "_flows"):
                    self._flows: dict[str, float] = {}
                for k in ("shp_grid_pwr", "shp_load_pwr"):
                    if k in result:
                        self._flows[k] = result[k]
                # Signed: positive = battery supplying the load, negative = the
                # battery charging through the panel (grid exceeds home load).
                if len(self._flows) == 2:
                    result["shp_batt_pwr"] = round(
                        self._flows["shp_load_pwr"] - self._flows["shp_grid_pwr"], 2
                    )

                # 32-circuit array: field 1015+i -> {1: volt, 2: watt, 3: amp}.
                for i in range(CIRCUITS):
                    entry = fields.get(CIRCUIT_FIELD_BASE + i)
                    if not entry or entry[0][0] != 2:
                        continue
                    sub = _parse_fields(entry[0][1])
                    n = i + 1
                    # The panel sends every circuit's entry each frame, but proto3
                    # omits a sub-field whose value is 0. So an absent watt/amp means
                    # the circuit is idle (0), NOT "unchanged" — set it explicitly.
                    # Leaving it unset lets the coordinator's param merge retain the
                    # last drawn value, which latches an idle circuit (e.g. an AC
                    # condenser that just cycled off) at ~kW and massively inflates
                    # its integrated energy.
                    result[f"ch_{n}_vol"] = round(sub[1][0][1], 2) if 1 in sub else 0.0
                    # EcoFlow signs branch consumption negative; negate to read
                    # positive, and clamp idle-noise negatives to 0 so the integrated
                    # energy stays monotonic (total_increasing).
                    result[f"ch_{n}_pwr"] = max(round(-sub[2][0][1], 2), 0.0) if 2 in sub else 0.0
                    result[f"ch_{n}_amp"] = round(sub[3][0][1], 2) if 3 in sub else 0.0

                # Circuit metadata submessages (rotate in over several frames):
                # sub-field 5 = app label; sub-field 2 = split-phase link {2: partner}.
                if not hasattr(self, "_meta"):
                    self._meta = {}
                before = {n: dict(m) for n, m in self._meta.items()}
                for n, fld in enumerate(NAME_FIELDS, 1):
                    entry = fields.get(fld)
                    if not entry or entry[0][0] != 2:
                        continue
                    meta = _parse_fields(entry[0][1])
                    label = meta.get(5)
                    if label and label[0][0] == 2:
                        try:
                            self._meta.setdefault(n, {})["name"] = label[0][1].decode("utf-8").strip().strip("\x00")
                        except UnicodeDecodeError:
                            pass
                    link = meta.get(2)
                    if link and link[0][0] == 2:
                        partner = _parse_fields(link[0][1]).get(2)
                        if partner and partner[0][0] == 0:
                            self._meta.setdefault(n, {})["partner"] = partner[0][1]
                # Persist labels across restart/reload, but only when they change.
                if self._meta != before:
                    self._save_meta()
                self._publish_meta(result)
                # Combine each 240V pair onto the primary (lower-numbered) leg and zero
                # the secondary, so the appliance reads once and energy isn't doubled.
                for n, m in self._meta.items():
                    p = m.get("partner")
                    if p and n < p and f"ch_{n}_pwr" in result and f"ch_{p}_pwr" in result:
                        result[f"ch_{n}_pwr"] = round(result[f"ch_{n}_pwr"] + result[f"ch_{p}_pwr"], 2)
                        result[f"ch_{p}_pwr"] = 0.0
            except Exception as e:
                _LOGGER.debug("SHP3 circuit/aggregate parse skipped: %s", e)
        return result
