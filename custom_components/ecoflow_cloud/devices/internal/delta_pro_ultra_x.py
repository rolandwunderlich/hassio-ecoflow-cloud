import logging
from typing import Any, override

from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.switch import SwitchEntity

from custom_components.ecoflow_cloud.api import EcoflowApiClient
from custom_components.ecoflow_cloud.devices import const
from custom_components.ecoflow_cloud.devices.internal.delta_pro_3 import DeltaPro3
from custom_components.ecoflow_cloud.sensor import (
    InWattsSensorEntity,
    LevelSensorEntity,
    OutWattsSensorEntity,
    QuotaStatusSensorEntity,
    RemainSensorEntity,
)

_LOGGER = logging.getLogger(__name__)


class DeltaProUltraX(DeltaPro3):
    """DELTA Pro Ultra X (private / app API).

    The DPU X speaks the same protobuf dialect as the Delta Pro 3 — identical
    ``DP3Header`` framing, the same ``cmdFunc/cmdId`` routing, and the same
    ``DisplayPropertyUpload`` field numbers (verified live: ``cms_batt_soc`` =
    field 262 tracks the app's aggregate SoC exactly). So the whole DP3 decode
    pipeline (``_prepare_data`` and helpers) is inherited unchanged.

    Differences from the Delta Pro 3 modelled here:
      * The DPU X is an inverter with **no internal main battery** — energy lives
        in up to 5 external packs — so the ``bms_*`` main-battery sensors never
        populate and are dropped; the combined ``cms_batt_soc`` is the headline.
      * Control surfaces (number/switch/select) are intentionally **suppressed**
        for now: this is live home backup hardware and M1a is read-only. They
        can be added once actuation is deliberately in scope.
    """

    @override
    def sensors(self, client: EcoflowApiClient) -> list[Any]:
        return [
            # Headline: aggregate battery SoC across the external packs.
            LevelSensorEntity(client, self, "cms_batt_soc", const.COMBINED_BATTERY_LEVEL),
            # In/out power (DisplayPropertyUpload fields 3 / 4).
            InWattsSensorEntity(client, self, "pow_in_sum_w", const.TOTAL_IN_POWER),
            OutWattsSensorEntity(client, self, "pow_out_sum_w", const.TOTAL_OUT_POWER),
            # Combined charge / discharge remaining time (fields 269 / 268).
            RemainSensorEntity(client, self, "cms_chg_rem_time", const.CHARGE_REMAINING_TIME),
            RemainSensorEntity(client, self, "cms_dsg_rem_time", const.DISCHARGE_REMAINING_TIME),
            # SoC limits reported by the device (read-only mirrors of the app
            # settings; the writable number entities are deliberately omitted).
            LevelSensorEntity(client, self, "cms_max_chg_soc", const.MAX_CHARGE_LEVEL),
            LevelSensorEntity(client, self, "cms_min_dsg_soc", const.MIN_DISCHARGE_LEVEL),
            QuotaStatusSensorEntity(client, self),
        ]

    @override
    def numbers(self, client: EcoflowApiClient) -> list[NumberEntity]:
        # Read-only for now — no control on live backup hardware (see class doc).
        return []

    @override
    def switches(self, client: EcoflowApiClient) -> list[SwitchEntity]:
        return []

    @override
    def selects(self, client: EcoflowApiClient) -> list[SelectEntity]:
        return []
