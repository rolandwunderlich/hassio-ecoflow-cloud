import logging
import struct
from typing import Any, override

from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.switch import SwitchEntity

from custom_components.ecoflow_cloud.api import EcoflowApiClient
from custom_components.ecoflow_cloud.devices import const
from custom_components.ecoflow_cloud.devices.internal.delta_pro_3 import DeltaPro3
from custom_components.ecoflow_cloud.sensor import (
    AmpSensorEntity,
    LevelSensorEntity,
    QuotaStatusSensorEntity,
    VoltSensorEntity,
    WattsSensorEntity,
)

_LOGGER = logging.getLogger(__name__)

# SHP3 has 32 monitored load circuits.
CIRCUITS = 32
# DisplayPropertyUpload (cmdFunc 254 / cmdId 21) per-circuit array: fields
# 1015..1046 are one submessage per circuit {1: volt, 2: watt(signed), 3: amp}.
# These are absent from the DP3 proto (unknown fields dropped on ParseFromString),
# so they are recovered with a second raw pass below.
CIRCUIT_FIELD_BASE = 1015
# Aggregate float fields (wiretype-5) confirmed in the same frame; home/grid/battery
# attribution is structural (magnitude + L1+L2==total) and pending an app/portal
# cross-check, so those flow sensors are disabled-by-default until labeled.
F_SYS_PWR = 515  # total real power (~L1+L2)
F_L1_PWR, F_L2_PWR = 962, 963
F_L1_VOL, F_L2_VOL = 956, 957
F_NET_FLOW = 1227  # signed aggregate flow (grid or battery)


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


class SmartHomePanel3(DeltaPro3):
    """EcoFlow Smart Home Panel 3 (private / app API).

    The SHP3 speaks the Delta Pro 3 protobuf dialect (same header, same
    cmdFunc/cmdId routing, DisplayPropertyUpload = 254/21), so the DP3 decode
    pipeline is inherited unchanged. The SHP-specific data — a 32-circuit
    load array (fields 1015..1046) plus aggregate flow powers — lives in fields
    the DP3 proto doesn't declare, so they are recovered with a second raw pass.
    Read-only: no control entities until actuation is deliberately in scope (M3).
    """

    @override
    def sensors(self, client: EcoflowApiClient) -> list[Any]:
        out: list[Any] = [
            # Battery SoC — DP3 field 262 (cms_batt_soc), decodes via the parent.
            LevelSensorEntity(client, self, "cms_batt_soc", const.COMBINED_BATTERY_LEVEL),
            # Total system real power (aggregate). Enabled headline.
            WattsSensorEntity(client, self, "shp_sys_pwr", "System Power"),
            QuotaStatusSensorEntity(client, self),
            # Per-phase power + line voltage + net flow — disabled until home/grid/
            # battery attribution is confirmed against the app/portal.
            WattsSensorEntity(client, self, "shp_l1_pwr", "L1 Power", False),
            WattsSensorEntity(client, self, "shp_l2_pwr", "L2 Power", False),
            VoltSensorEntity(client, self, "shp_l1_vol", "L1 Voltage", False),
            VoltSensorEntity(client, self, "shp_l2_vol", "L2 Voltage", False),
            WattsSensorEntity(client, self, "shp_net_flow_pwr", "Net Grid/Battery Power (unverified)", False),
        ]
        # 32 per-circuit sensors: power enabled (the M1b payoff), volt/amp disabled.
        for n in range(1, CIRCUITS + 1):
            out.append(WattsSensorEntity(client, self, f"ch_{n}_pwr", f"Circuit {n} Power"))
            out.append(VoltSensorEntity(client, self, f"ch_{n}_vol", f"Circuit {n} Voltage", False))
            out.append(AmpSensorEntity(client, self, f"ch_{n}_amp", f"Circuit {n} Current", False))
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
                    ("shp_sys_pwr", F_SYS_PWR),
                    ("shp_l1_pwr", F_L1_PWR),
                    ("shp_l2_pwr", F_L2_PWR),
                    ("shp_l1_vol", F_L1_VOL),
                    ("shp_l2_vol", F_L2_VOL),
                    ("shp_net_flow_pwr", F_NET_FLOW),
                ):
                    v = _f(no)
                    if v is not None:
                        result[name] = round(v, 2)

                # 32-circuit array: field 1015+i -> {1: volt, 2: watt, 3: amp}.
                for i in range(CIRCUITS):
                    entry = fields.get(CIRCUIT_FIELD_BASE + i)
                    if not entry or entry[0][0] != 2:
                        continue
                    sub = _parse_fields(entry[0][1])
                    n = i + 1
                    if 1 in sub:
                        result[f"ch_{n}_vol"] = round(sub[1][0][1], 2)
                    if 2 in sub:
                        # EcoFlow signs load consumption negative (power flowing out
                        # of the panel bus to the branch load). Negate so consumption
                        # reads positive — matches system_power and the HA Energy
                        # dashboard's "individual device" consumption convention.
                        result[f"ch_{n}_pwr"] = round(-sub[2][0][1], 2)
                    if 3 in sub:
                        result[f"ch_{n}_amp"] = round(sub[3][0][1], 2)
            except Exception as e:
                _LOGGER.debug("SHP3 circuit/aggregate parse skipped: %s", e)
        return result
