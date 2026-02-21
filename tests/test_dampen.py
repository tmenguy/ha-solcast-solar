"""Tests for the Solcast Solar automated dampening."""

import asyncio
from collections import defaultdict
import copy
import datetime
from datetime import datetime as dt, timedelta
import json
import logging
import math
from pathlib import Path
import re
from unittest.mock import patch
from zoneinfo import ZoneInfo

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.recorder import Recorder
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.solcast_solar.config_flow import (
    SolcastSolarOptionFlowHandler,
)
from homeassistant.components.solcast_solar.const import (
    ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_APE_SHIT,
    ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_EXCLUDE,
    ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_MINIMUM_ERROR_DELTA,
    ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_MINIMUM_HISTORY_DAYS,
    ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL,
    ADVANCED_AUTOMATED_DAMPENING_MODEL,
    ADVANCED_AUTOMATED_DAMPENING_MODEL_DAYS,
    ADVANCED_AUTOMATED_DAMPENING_NO_DELTA_ADJUSTMENT,
    ADVANCED_OPTIONS,
    AUTO_DAMPEN,
    AUTO_UPDATE,
    CONFIG_DISCRETE_NAME,
    CONFIG_FOLDER_DISCRETE,
    DOMAIN,
    EXCLUDE_SITES,
    EXPORT_LIMITING,
    FORECASTS,
    GENERATION,
    GENERATION_ENTITIES,
    GET_ACTUALS,
    MAXIMUM,
    MINIMUM,
    MINIMUM_EXTENDED,
    PERIOD_START,
    PRESUMED_DEAD,
    SERVICE_SET_DAMPENING,
    SITE_EXPORT_ENTITY,
    SITE_EXPORT_LIMIT,
    SITE_INFO,
    USE_ACTUALS,
    VALUE_ADAPTIVE_DAMPENING_NO_DELTA,
)
from homeassistant.components.solcast_solar.coordinator import SolcastUpdateCoordinator
from homeassistant.components.solcast_solar.solcastapi import SolcastApi
from homeassistant.components.solcast_solar.util import (
    DateTimeEncoder,
    JSONDecoder,
    NoIndentEncoder,
    SolcastApiStatus,
    percentile,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryDisabler

from . import (
    DEFAULT_INPUT2,
    MOCK_CORRUPT_ACTUALS,
    ZONE_RAW,
    ExtraSensors,
    async_cleanup_integration_tests,
    async_init_integration,
    entity_history,
    session_clear,
    session_set,
)

from tests.common import MockConfigEntry

ZONE = ZoneInfo(ZONE_RAW)
NOW = dt.now(ZONE)

_LOGGER = logging.getLogger(__name__)


def _no_exception(caplog: pytest.LogCaptureFixture):
    assert "Exception" not in caplog.text


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> tuple[SolcastUpdateCoordinator | None, SolcastApi | None]:
    """Reload the integration."""

    _LOGGER.warning("Reloading integration")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    if hass.data[DOMAIN].get(entry.entry_id):
        try:
            return entry.runtime_data.coordinator, entry.runtime_data.coordinator.solcast
        except:  # noqa: E722
            _LOGGER.error("Failed to load coordinator (or solcast), which may be expected given test conditions")
    return None, None


async def _exec_update_actuals(
    hass: HomeAssistant,
    coordinator: SolcastUpdateCoordinator,
    solcast: SolcastApi,
    caplog: pytest.LogCaptureFixture,
    freezer: FrozenDateTimeFactory,
    action: str,
    last_update_delta: int = 0,
    wait: bool = True,
) -> None:
    """Execute an estimated actuals action and wait for completion."""

    caplog.clear()
    if last_update_delta == 0:
        last_updated = dt(year=2020, month=1, day=1, hour=1, minute=1, second=1, tzinfo=datetime.UTC)
    else:
        last_updated = solcast._data_actuals["last_updated"] - timedelta(seconds=last_update_delta)  # pyright: ignore[reportPrivateUsage]
        _LOGGER.info("Mock last updated: %s", last_updated)
    solcast._data_actuals["last_updated"] = last_updated  # pyright: ignore[reportPrivateUsage]
    await hass.services.async_call(DOMAIN, action, {}, blocking=True)
    if wait:
        await _wait_for_update(hass, caplog, freezer)
        await solcast.tasks_cancel()
        async with asyncio.timeout(1):
            while "Task model_automated_dampening took" not in caplog.text:
                await hass.async_block_till_done()
    await hass.async_block_till_done()


async def _wait_for_update(hass: HomeAssistant, caplog: pytest.LogCaptureFixture, freezer: FrozenDateTimeFactory) -> None:
    """Wait for forecast update completion."""

    async with asyncio.timeout(10):
        while (
            "Forecast update completed successfully" not in caplog.text
            and "Saved estimated actual cache" not in caplog.text
            and "Not requesting a solar forecast" not in caplog.text
            and "aborting forecast update" not in caplog.text
            and "update already in progress" not in caplog.text
            and "pausing" not in caplog.text
            and "Completed task update" not in caplog.text
            and "Completed task force_update" not in caplog.text
            and "ConfigEntryAuthFailed" not in caplog.text
        ):  # Wait for task to complete
            freezer.tick(0.1)
            await hass.async_block_till_done()


async def _wait_for_it(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, freezer: FrozenDateTimeFactory, wait_for: str, long_time: bool = False
) -> None:
    """Wait for forecast update completion."""

    async with asyncio.timeout(300 if not long_time else 3000):
        while wait_for not in caplog.text:  # Wait for task to complete
            freezer.tick(0.1)
            await hass.async_block_till_done()


async def test_auto_dampen(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test automated dampening."""

    assert await async_cleanup_integration_tests(hass)

    try:
        config_dir = f"{hass.config.config_dir}/{CONFIG_DISCRETE_NAME}" if CONFIG_FOLDER_DISCRETE else hass.config.config_dir
        if CONFIG_FOLDER_DISCRETE:
            Path(config_dir).mkdir(parents=False, exist_ok=True)

        Path(f"{config_dir}/solcast-advanced.json").write_text(
            json.dumps(
                {
                    "automated_dampening_ignore_intervals": ["17:00"],
                    "automated_dampening_no_limiting_consistency": True,
                    "automated_dampening_generation_fetch_delay": 5,
                    "automated_dampening_insignificant_factor": 0.988,
                    "automated_dampening_insignificant_factor_adjusted": 0.989,
                    "estimated_actuals_fetch_delay": 5,
                    "estimated_actuals_log_mape_breakdown": True,
                }
            ),
            encoding="utf-8",
        )

        options = copy.deepcopy(DEFAULT_INPUT2)
        options[AUTO_UPDATE] = 0
        options[GET_ACTUALS] = True
        options[USE_ACTUALS] = 1
        options[AUTO_DAMPEN] = True
        options[EXCLUDE_SITES] = ["3333-3333-3333-3333"]
        options[GENERATION_ENTITIES] = [
            "sensor.solar_export_sensor_1111_1111_1111_1111",
            "sensor.solar_export_sensor_2222_2222_2222_2222",
        ]
        options[SITE_EXPORT_ENTITY] = "sensor.site_export_sensor"
        options[SITE_EXPORT_LIMIT] = 5.0
        entry = await async_init_integration(hass, options, extra_sensors=ExtraSensors.YES_WATT_HOUR)

        # Fiddle with undampened data cache
        undampened = json.loads(Path(f"{config_dir}/solcast-undampened.json").read_text(encoding="utf-8"), cls=JSONDecoder)
        for site in undampened["siteinfo"].values():
            for forecast in site["forecasts"]:
                forecast["pv_estimate"] *= 0.85
        Path(f"{config_dir}/solcast-undampened.json").write_text(json.dumps(undampened, cls=DateTimeEncoder), encoding="utf-8")

        # Fiddle with estimated actual data cache
        actuals = json.loads(Path(f"{config_dir}/solcast-actuals.json").read_text(encoding="utf-8"), cls=JSONDecoder)
        for site in actuals["siteinfo"].values():
            for forecast in site["forecasts"]:
                if (
                    forecast["period_start"].astimezone(ZoneInfo(ZONE_RAW)).hour == 10
                    and forecast["period_start"].astimezone(ZoneInfo(ZONE_RAW)).minute == 30
                ):
                    forecast["pv_estimate"] *= 0.91
        Path(f"{config_dir}/solcast-actuals.json").write_text(json.dumps(actuals, cls=DateTimeEncoder), encoding="utf-8")

        # Reload to load saved data and prime initial generation
        caplog.clear()
        coordinator, solcast = await _reload(hass, entry)
        if coordinator is None or solcast is None:
            pytest.fail("Reload failed")

        # Assert good start, that actuals and generation are enabled, and that the caches are saved
        _LOGGER.debug("Testing good start happened")
        for _ in range(30):  # Extra time needed for reload to complete
            await hass.async_block_till_done()
            freezer.tick(0.1)
        assert hass.data[DOMAIN].get(PRESUMED_DEAD, True) is False
        _no_exception(caplog)

        assert "Auto-dampening suppressed: Excluded site for 3333-3333-3333-3333" in caplog.text
        assert "Interval 08:30 has peak estimated actual 0.936" in caplog.text
        assert "Interval 08:30 max generation: 0.777" in caplog.text
        assert "Auto-dampen factor for 08:30 is 0.830" in caplog.text
        # assert "Auto-dampen factor for 11:00" not in caplog.text
        assert "Ignoring insignificant factor for 11:00 of 0.993" in caplog.text
        assert "Ignoring excessive PV generation" not in caplog.text

        # Reload to load saved generation data
        caplog.clear()
        coordinator, solcast = await _reload(hass, entry)
        if coordinator is None or solcast is None:
            pytest.fail("Reload failed")
        assert Path(f"{config_dir}/solcast-actuals.json").is_file()
        assert Path(f"{config_dir}/solcast-generation.json").is_file()
        assert "Generation data loaded" in caplog.text

        # Test service action to update dampening manually refused
        caplog.clear()
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(DOMAIN, SERVICE_SET_DAMPENING, {"damp_factor": ("1.0," * 24)[:-1]}, blocking=True)

        # Test service action to force update actuals
        caplog.clear()
        _LOGGER.debug("Testing force update actuals with dampening enabled")
        await _exec_update_actuals(hass, coordinator, solcast, caplog, freezer, "force_update_estimates")
        await _wait_for_it(hass, caplog, freezer, "Estimated actual mean APE", long_time=True)
        assert "Estimated actuals dictionary for site 1111-1111-1111-1111" in caplog.text
        assert "Estimated actuals dictionary for site 2222-2222-2222-2222" in caplog.text
        assert "Estimated actuals dictionary for site 3333-3333-3333-3333" in caplog.text
        assert "Task model_automated_dampening took" in caplog.text
        assert "Apply dampening to previous day estimated actuals" not in caplog.text

        # Roll over to tomorrow.
        _LOGGER.debug("Rolling over to tomorrow")
        caplog.clear()
        removed = -5
        value_removed = solcast._data_actuals["siteinfo"]["1111-1111-1111-1111"]["forecasts"].pop(removed)  # pyright: ignore[reportPrivateUsage]
        freezer.move_to((dt.now(solcast._tz) + timedelta(hours=12)).replace(minute=0, second=0, microsecond=0))  # pyright: ignore[reportPrivateUsage]
        await hass.async_block_till_done()
        await _wait_for_it(hass, caplog, freezer, "Update generation data", long_time=True)
        await _wait_for_it(hass, caplog, freezer, "Estimated actual mean APE", long_time=True)
        _no_exception(caplog)
        assert "Advanced option set automated_dampening_ignore_intervals: ['17:00']" in caplog.text
        assert "Calculating dampened estimated actual MAPE" in caplog.text
        assert "Calculating undampened estimated actual MAPE" in caplog.text
        assert "APE calculation for day" in caplog.text
        assert "Estimated actual mean APE" in caplog.text
        assert "Getting estimated actuals update for site" in caplog.text
        assert "Apply dampening to previous day estimated actuals" in caplog.text
        assert "Task model_automated_dampening took" in caplog.text
        assert (
            solcast._data_actuals["siteinfo"]["1111-1111-1111-1111"]["forecasts"][removed - 24]["period_start"]  # pyright: ignore[reportPrivateUsage,reportOptionalMemberAccess]
            == value_removed["period_start"]
        )  # pyright: ignore[reportPrivateUsage]
        assert "Auto-dampen factor for 08:30 is 0.830" in caplog.text

        ADVANCED_CHECKS = {
            0: {"base": 0.830, "adjusted": [0.858, 0.834]},
            1: {"base": 0.830, "adjusted": [0.858, 0.834]},
            2: {"base": 0.652, "adjusted": [0.709, 0.660]},
            3: {"base": 0.296, "adjusted": [0.410, 0.312]},
        }
        for preseve in (False, True):
            solcast.advanced_options["automated_dampening_preserve_unmatched_factors"] = preseve
            for model in (0, 1, 2, 3):
                caplog.clear()
                solcast.advanced_options["automated_dampening_model"] = model
                await solcast.model_automated_dampening()
                assert "Auto-dampen factor for 08:30 is {:.3f}".format(ADVANCED_CHECKS[model]["base"]) in caplog.text

                for adjustment_model in (0, 1):
                    caplog.clear()
                    solcast.advanced_options["automated_dampening_delta_adjustment_model"] = adjustment_model
                    await solcast.apply_forward_dampening()
                    _LOGGER.critical("Model %d/%d tested", model, adjustment_model)
                    assert (
                        re.search(
                            r"Adjusted granular dampening factor for .+ 08:30:00, {:.3f}".format(
                                ADVANCED_CHECKS[model]["adjusted"][adjustment_model]
                            ),
                            caplog.text,
                        )
                        is not None
                    )

        # Verify that the dampening entity that should be disabled by default is, then enable it.
        entity = "sensor.solcast_pv_forecast_dampening"
        assert hass.states.get(entity) is None
        er.async_get(hass).async_update_entity(entity, disabled_by=None)
        async with asyncio.timeout(300):
            while "Reloading configuration entries because disabled_by changed" not in caplog.text:
                freezer.tick(0.01)
                await hass.async_block_till_done()

        # Roll over to another tomorrow.
        _LOGGER.debug("Rolling over to another tomorrow")
        caplog.clear()
        session_set(MOCK_CORRUPT_ACTUALS)
        freezer.move_to((dt.now(solcast._tz) + timedelta(days=1)).replace(minute=0, second=0, microsecond=0))  # pyright: ignore[reportPrivateUsage,reportOptionalMemberAccess]
        await _wait_for_it(hass, caplog, freezer, "Update estimated actuals failed: No valid json returned", long_time=True)
        session_clear(MOCK_CORRUPT_ACTUALS)
        for _ in range(300):  # Extra time needed for get_generation to complete
            freezer.tick(0.1)
            await hass.async_block_till_done()

        # Cause an actual build exception
        _LOGGER.debug("Causing an actual build exception")
        caplog.clear()
        old_data = copy.deepcopy(solcast._data_actuals)  # pyright: ignore[reportPrivateUsage,reportOptionalMemberAccess]
        solcast._data_actuals["siteinfo"]["1111-1111-1111-1111"] = None  # pyright: ignore[reportPrivateUsage,reportOptionalMemberAccess]
        with pytest.raises(ConfigEntryNotReady):
            await solcast.build_forecast_and_actuals(raise_exc=True)  # pyright: ignore[reportOptionalMemberAccess]
        assert solcast.status == SolcastApiStatus.BUILD_FAILED_ACTUALS
        await solcast.model_automated_dampening()  # pyright: ignore[reportOptionalMemberAccess] # Hit an actuals missing deal-breaker
        assert "Auto-dampening suppressed: No estimated actuals yet for 1111-1111-1111-1111" in caplog.text
        solcast._data_actuals = old_data  # pyright: ignore[reportPrivateUsage,reportOptionalMemberAccess]
        solcast.status = SolcastApiStatus.OK

        # Cause a forecast build exception
        _LOGGER.debug("Causing a forecast build exception")
        caplog.clear()
        old_data = copy.deepcopy(solcast._data)  # pyright: ignore[reportPrivateUsage,reportOptionalMemberAccess]
        solcast._data["siteinfo"]["1111-1111-1111-1111"] = None  # pyright: ignore[reportPrivateUsage,reportOptionalMemberAccess,reportOptionalMemberAccess]
        with pytest.raises(ConfigEntryNotReady):
            await solcast.build_forecast_and_actuals(raise_exc=True)  # pyright: ignore[reportOptionalMemberAccess]
        assert solcast.status == SolcastApiStatus.BUILD_FAILED_FORECASTS
        solcast._data = old_data  # pyright: ignore[reportPrivateUsage,reportOptionalMemberAccess]

        # Turn off auto-dampen.
        caplog.clear()
        opt = {**entry.options}
        opt[AUTO_DAMPEN] = False
        hass.config_entries.async_update_entry(entry, options=opt)
        await hass.async_block_till_done()
        assert "Options updated, action: The integration will reload" in caplog.text
        for _ in range(300):  # Extra time needed for reload to complete
            freezer.tick(0.1)
            await hass.async_block_till_done()

    finally:
        session_clear(MOCK_CORRUPT_ACTUALS)
        assert await async_cleanup_integration_tests(hass)


@pytest.mark.parametrize(
    "extra_sensors",
    [
        ExtraSensors.YES_WITH_SUPPRESSION,
        ExtraSensors.YES_UNIT_NOT_IN_HISTORY,
        ExtraSensors.YES_NO_UNIT,
        ExtraSensors.DODGY,
    ],
)
async def test_auto_dampen_issues(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    freezer: FrozenDateTimeFactory,
    extra_sensors: ExtraSensors,
) -> None:
    """Test automated dampening."""

    try:
        options = copy.deepcopy(DEFAULT_INPUT2)
        options[GET_ACTUALS] = True
        options[USE_ACTUALS] = 2
        options[AUTO_DAMPEN] = True
        options[EXCLUDE_SITES] = ["3333-3333-3333-3333"]
        options[GENERATION_ENTITIES] = [
            "sensor.solar_export_sensor_1111_1111_1111_1111",
            "sensor.solar_export_sensor_2222_2222_2222_2222",
        ]
        if extra_sensors != ExtraSensors.YES_WITH_SUPPRESSION:
            options[SITE_EXPORT_ENTITY] = "sensor.site_export_sensor"
            options[SITE_EXPORT_LIMIT] = 5.0
        if extra_sensors == ExtraSensors.YES_UNIT_NOT_IN_HISTORY:
            options[GENERATION_ENTITIES][0] = "sensor.not_valid"
        if extra_sensors == ExtraSensors.DODGY:
            options[SITE_EXPORT_ENTITY] = "sensor.not_valid"
        entry = await async_init_integration(hass, options, extra_sensors=extra_sensors)

        # An orphaned forecast day sensor is created along with the extra sensors
        assert "Cleaning up orphaned sensor.solcast_solar_forecast_day_20" in caplog.text

        entity_registry = er.async_get(hass)
        if extra_sensors == ExtraSensors.YES_NO_UNIT:
            e = entity_registry.async_get(options[GENERATION_ENTITIES][0])
            if e is not None:
                entity_registry.async_update_entity(e.entity_id, disabled_by=RegistryEntryDisabler.USER)
            else:
                pytest.fail("Failed to get generation entity to disable")
            await hass.async_block_till_done()
        if extra_sensors == ExtraSensors.YES_UNIT_NOT_IN_HISTORY:
            e = entity_registry.async_get(options[SITE_EXPORT_ENTITY])
            if e is not None:
                entity_registry.async_update_entity(e.entity_id, disabled_by=RegistryEntryDisabler.USER)
            else:
                pytest.fail("Failed to get site export entity to disable")
            await hass.async_block_till_done()

        # Reload to load saved data and prime initial generation
        caplog.clear()
        coordinator, solcast = await _reload(hass, entry)
        if coordinator is None or solcast is None:
            pytest.fail("Reload failed")

        # Assert good start, that actuals and generation are enabled, and that the caches are saved
        _LOGGER.debug("Testing good start happened")
        for _ in range(30):  # Extra time needed for reload to complete
            freezer.tick(0.1)
            await hass.async_block_till_done()
        assert hass.data[DOMAIN].get(PRESUMED_DEAD, True) is False
        _no_exception(caplog)
        assert "Calculating dampened estimated actual MAPE" not in caplog.text
        assert "Estimated actual mean APE" in caplog.text
        if extra_sensors not in [ExtraSensors.YES_UNIT_NOT_IN_HISTORY, ExtraSensors.YES_NO_UNIT]:
            assert "Retrieved day -1 PV generation data from entity: sensor.solar_export_sensor_1111_1111_1111_1111" in caplog.text
            assert "No day -2 PV generation data (or barely any) from entity: sensor.solar_export_sensor_1111_1111_1111_1111" in caplog.text
            # assert "Retrieved day -3 PV generation data from entity: sensor.solar_export_sensor_1111_1111_1111_1111" in caplog.text

        match extra_sensors:
            case ExtraSensors.YES_WITH_SUPPRESSION:
                for interval in ("12:00", "12:30", "13:00", "13:30", "14:00"):
                    assert re.search(r"Auto-dampen suppressed for interval.+" + interval, caplog.text) is not None
                    assert f"Interval {interval} max generation: 0.000, []" in caplog.text
            case ExtraSensors.YES_UNIT_NOT_IN_HISTORY:
                assert "has no unit_of_measurement, assuming kWh" not in caplog.text
                assert f"Generation entity {options[GENERATION_ENTITIES][0]} is not a valid entity" in caplog.text
                assert f"Site export entity {options[SITE_EXPORT_ENTITY]} is disabled, please enable it" in caplog.text
            case ExtraSensors.YES_NO_UNIT:
                assert "has no unit_of_measurement, assuming kWh" in caplog.text
                assert f"Generation entity {options[GENERATION_ENTITIES][0]} is disabled, please enable it" in caplog.text
            case ExtraSensors.DODGY:
                assert "has an unsupported unit_of_measurement 'MJ'" in caplog.text  # A dodgy unit should be logged
                assert f"Site export entity {options[SITE_EXPORT_ENTITY]} is not a valid entity" in caplog.text
                assert "Interval 11:00 max generation: 0.000, []" in caplog.text  # A jump in generation should not be seen as a peak
                assert "Interval 12:30 max generation: 3.900" in caplog.text  # Dodgy generation filtered but some valid data remains
                assert "Auto-dampen factor for 10:00 is 0.940" in caplog.text  # A valid interval still considered
                assert (
                    "Ignoring excessive PV generation jump of 6.000 kWh, time delta 5406 seconds" in caplog.text
                )  # Dodgy generation should be logged
            case _:
                pytest.fail("Assertions missing for extra_sensors value")

    finally:
        assert await async_cleanup_integration_tests(hass)


async def test_percentile() -> None:
    """Test percentile function."""

    data: list[float]

    data = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile(data, 0) == 1.0
    assert percentile(data, 25) == 2.0
    assert percentile(data, 50) == 3.0
    assert percentile(data, 75) == 4.0
    assert percentile(data, 100) == 5.0

    data = [5.0]
    assert percentile(data, 0) == 5.0
    assert percentile(data, 25) == 5.0
    assert percentile(data, 50) == 5.0
    assert percentile(data, 75) == 5.0
    assert percentile(data, 100) == 5.0

    data = [0.1] * 10 + [0.5]
    assert percentile(data, 90) == 0.1

    data = [0.1] * 8 + [0.5]
    assert round(percentile(data, 90), 2) == 0.18

    data = []
    assert percentile(data, 50) == 0.0


async def test_adaptive_auto_dampen(  # noqa: C901
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test dampening adaptations."""

    entity_history["days_generation"] = 7
    entity_history["days_suppression"] = 7
    entity_history["offset"] = 2

    try:
        config_dir = f"{hass.config.config_dir}/{CONFIG_DISCRETE_NAME}" if CONFIG_FOLDER_DISCRETE else hass.config.config_dir
        if CONFIG_FOLDER_DISCRETE:
            Path(config_dir).mkdir(parents=False, exist_ok=True)

        Path(f"{config_dir}/solcast-advanced.json").write_text(
            json.dumps(
                {
                    "automated_dampening_adaptive_model_configuration": True,
                    "automated_dampening_model": 3,
                    "automated_dampening_delta_adjustment_model": -1,
                    "automated_dampening_adaptive_model_exclude": [{"model": 3, "delta": 0}],
                    "automated_dampening_ignore_intervals": ["17:00"],
                    "automated_dampening_no_limiting_consistency": True,
                    "automated_dampening_generation_fetch_delay": 5,
                    "automated_dampening_insignificant_factor": 0.988,
                    "automated_dampening_insignificant_factor_adjusted": 0.989,
                    "estimated_actuals_fetch_delay": 5,
                    "estimated_actuals_log_mape_breakdown": True,
                }
            ),
            encoding="utf-8",
        )

        options = copy.deepcopy(DEFAULT_INPUT2)
        options[AUTO_UPDATE] = 0
        options[GET_ACTUALS] = True
        options[USE_ACTUALS] = 1
        options[AUTO_DAMPEN] = True
        options[EXCLUDE_SITES] = ["3333-3333-3333-3333"]
        options[GENERATION_ENTITIES] = [
            "sensor.solar_export_sensor_1111_1111_1111_1111",
            "sensor.solar_export_sensor_2222_2222_2222_2222",
        ]
        options[SITE_EXPORT_ENTITY] = "sensor.site_export_sensor"
        options[SITE_EXPORT_LIMIT] = 5.0
        entry = await async_init_integration(hass, options, extra_sensors=ExtraSensors.YES)

        # Fiddle with undampened data cache
        undampened = json.loads(Path(f"{config_dir}/solcast-undampened.json").read_text(encoding="utf-8"), cls=JSONDecoder)
        for site in undampened["siteinfo"].values():
            for forecast in site["forecasts"]:
                forecast["pv_estimate"] *= 0.85
        Path(f"{config_dir}/solcast-undampened.json").write_text(json.dumps(undampened, cls=DateTimeEncoder), encoding="utf-8")

        # Fiddle with estimated actual data cache
        actuals = json.loads(Path(f"{config_dir}/solcast-actuals.json").read_text(encoding="utf-8"), cls=JSONDecoder)
        for site in actuals["siteinfo"].values():
            for forecast in site["forecasts"]:
                if (
                    forecast["period_start"].astimezone(ZoneInfo(ZONE_RAW)).hour == 10
                    and forecast["period_start"].astimezone(ZoneInfo(ZONE_RAW)).minute == 30
                ):
                    forecast["pv_estimate"] *= 0.91
        Path(f"{config_dir}/solcast-actuals.json").write_text(json.dumps(actuals, cls=DateTimeEncoder), encoding="utf-8")

        # Reload to load saved data and prime initial generation
        caplog.clear()
        coordinator, solcast = await _reload(hass, entry)
        if coordinator is None or solcast is None:
            pytest.fail("Reload failed")

        # Assert good start, that actuals and generation are enabled, and that the caches are saved
        _LOGGER.debug("Testing good start happened")
        await _wait_for_it(hass, caplog, freezer, "Clear presumed dead flag", long_time=False)
        _no_exception(caplog)

        assert "Auto-dampening suppressed: Excluded site for 3333-3333-3333-3333" in caplog.text
        assert "Interval 08:30 has peak estimated actual 0.936" in caplog.text
        # assert "Interval 08:30 max generation: 0.778" in caplog.text
        assert "Auto-dampen factor for 08:30 is 0.296" in caplog.text

        # Roll over to tomorrow three times.
        roll_to = [
            {"days": 0, "hours": 12},
            {"days": 1, "hours": 0},
            {"days": 1, "hours": 0},
            {"days": 1, "hours": 0},
        ]
        for count, roll in enumerate(roll_to):
            _LOGGER.debug("Rolling over to tomorrow")
            caplog.clear()
            removed = -5
            solcast._data_actuals["siteinfo"]["1111-1111-1111-1111"]["forecasts"].pop(removed)
            freezer.move_to((dt.now(solcast._tz) + timedelta(**roll)).replace(minute=0, second=0, microsecond=0))
            await hass.async_block_till_done()
            solcast.suppress_advanced_watchdog_reload = True
            await solcast.read_advanced_options()
            await _wait_for_it(hass, caplog, freezer, "Update generation data", long_time=True)
            await _wait_for_it(hass, caplog, freezer, "Estimated actual mean APE", long_time=True)
            _no_exception(caplog)
            assert "Updating automated dampening adaptation history" in caplog.text
            assert "Task update_dampening_history took" in caplog.text
            match count:
                case 2:
                    assert "Determining best automated dampening settings" in caplog.text
                    assert "Dampening history actuals suppressed site 3333-3333-3333-3333" in caplog.text
                    assert "Skipping model 2 and delta 0 as history of 2 days" in caplog.text
                    assert "Skipping model 2 and delta 1 as history of 1 days" in caplog.text
                    assert "Advanced option 'automated_dampening_delta_adjustment_model' set to: 0" in caplog.text
                    assert "Advanced option 'automated_dampening_model' set to: 0" in caplog.text
                    assert "Task serialise_advanced_options took" in caplog.text
                    assert re.search(r"Advanced options file .+ exists", caplog.text) is None

                    solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_APE_SHIT] = True
                    solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_EXCLUDE] = [{"model": 2, "delta": -1}]
                    await solcast.determine_best_dampening_settings()
                    assert "Adaptive dampening selection going ape shit" in caplog.text
                    assert "Skipping model 2 and delta -1 as in automated_dampening_adaptive_model_exclude" in caplog.text
                    solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_APE_SHIT] = False
                    solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_EXCLUDE] = []

                    solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_NO_DELTA_ADJUSTMENT] = True
                    await solcast.determine_best_dampening_settings()
                    solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_NO_DELTA_ADJUSTMENT] = False

                    # Remove the 2nd day from all model/deltas
                    now_missing_day_2 = defaultdict(dict)
                    for model in solcast._data_dampening_history:
                        for delta in solcast._data_dampening_history[model]:
                            if len(solcast._data_dampening_history[model][delta]) > 1:
                                now_missing_day_2[model][delta] = copy.deepcopy(solcast._data_dampening_history[model][delta][1])
                                solcast._data_dampening_history[model][delta].pop(1)

                case 3:
                    assert "Determining best automated dampening settings" in caplog.text
                    assert "Insufficient continuous dampening history to determine best automated dampening settings" in caplog.text

                    # Reinstate the 2nd day from all model/deltas
                    for model in solcast._data_dampening_history:
                        for delta in solcast._data_dampening_history[model]:
                            if model in now_missing_day_2 and delta in now_missing_day_2[model]:
                                solcast._data_dampening_history[model][delta].append(now_missing_day_2[model][delta])

                    # Write history to file so it persists through reload
                    Path(f"{config_dir}/solcast-dampening-history.json").write_text(
                        json.dumps(solcast._data_dampening_history, ensure_ascii=False, indent=2, cls=NoIndentEncoder, above_level=4),
                        encoding="utf-8",
                    )

                case 1:
                    assert "Insufficient continuous dampening history" in caplog.text
                    # Knobble the history for some combos
                    now_missing_2_1 = copy.deepcopy(solcast._data_dampening_history[2][1])
                    now_missing_2_0_1 = copy.deepcopy(solcast._data_dampening_history[2][0][1])
                    now_missing_3_0_1 = copy.deepcopy(solcast._data_dampening_history[3][0][1])
                    solcast._data_dampening_history[2][0] = solcast._data_dampening_history[3][0][:-1]
                    solcast._data_dampening_history[2][1] = []
                    solcast._data_dampening_history[3][0] = [solcast._data_dampening_history[3][0][0]]

                case 0:
                    assert "Insufficient continuous dampening history" in caplog.text

        # Reload to load dampening factor history
        caplog.clear()
        coordinator, solcast = await _reload(hass, entry)
        if coordinator is None or solcast is None:
            pytest.fail("Reload failed")
        await _wait_for_it(hass, caplog, freezer, "Completed task stale_update", long_time=True)
        await _wait_for_it(hass, caplog, freezer, "Task load_dampening_history took")

        # Re-add dampening history for today
        caplog.clear()
        _LOGGER.debug("Re-adding dampening history for today")
        await solcast.update_dampening_history()

        # Test valid and full history
        solcast._data_dampening_history[2][1] = solcast._data_dampening_history[2][1] + list(now_missing_2_1)
        solcast._data_dampening_history[2][0].append(now_missing_2_0_1)
        solcast._data_dampening_history[3][0].append(now_missing_3_0_1)
        Path(f"{config_dir}/solcast-dampening-history.json").write_text(
            json.dumps(solcast._data_dampening_history, ensure_ascii=False, indent=2, cls=NoIndentEncoder, above_level=4), encoding="utf-8"
        )
        caplog.clear()
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL_DAYS] = 4
        await solcast.load_dampening_history()
        assert "Automated dampening adaptive model configuration may be sub-optimal" not in caplog.text
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL_DAYS] = 14

        # Test staggered history start dates (exercises if period_start < earliest_common)
        # Model 0 will have entries from all 4 days (days 0-3)
        # Model 1 will have entries from 3 days (days 1-3, missing day 0)
        # Model 2 will have entries from 2 days (days 2-3, missing days 0-1)
        # Model 3 will have entries from 3 days (days 1-3, missing day 0)
        # This makes earliest_common = day 2 (where all models have continuous history)
        # When processing models 0, 1, 3, entries for day 0-1 should be skipped (period_start < earliest_common)
        caplog.clear()
        _LOGGER.debug("Testing adaptive dampening with staggered history start dates")
        old_data = copy.deepcopy(solcast._data_dampening_history)
        for delta in range(-1, 2):
            if len(solcast._data_dampening_history[1][delta]) >= 4:
                solcast._data_dampening_history[1][delta].pop(0)
        for delta in range(-1, 2):
            if len(solcast._data_dampening_history[2][delta]) >= 4:
                solcast._data_dampening_history[2][delta].pop(0)
                solcast._data_dampening_history[2][delta].pop(0)
        for delta in range(-1, 2):
            if len(solcast._data_dampening_history[3][delta]) >= 4:
                solcast._data_dampening_history[3][delta].pop(0)
        await solcast.determine_best_dampening_settings()  # Should skip early entries for models 0, 1, 3
        # Should complete successfully despite staggered dates
        assert "Determining best automated dampening settings" in caplog.text
        assert "Earliest date with complete dampening history" in caplog.text
        assert "delta is" in caplog.text and "days" in caplog.text
        assert "Skipping model" in caplog.text or "history of" in caplog.text
        solcast._data_dampening_history = old_data

        # Test scenario where all generation is zero (all APE values become infinity)
        # This exercises the defensive check: if error_metric == math.inf
        caplog.clear()
        _LOGGER.debug("Testing adaptive dampening with all zero generation (infinity APE)")
        # Store original generation data
        original_generation = copy.deepcopy(solcast._data_generation)
        # Set all generation to zero to trigger infinity APE
        for gen_entry in solcast._data_generation[GENERATION]:
            gen_entry[GENERATION] = 0.0
        await solcast.determine_best_dampening_settings()
        # Should log the defensive check message for skipping APE calculation
        assert "Determining best automated dampening settings" in caplog.text
        assert "Skipping APE calculation for model" in caplog.text
        assert "due to APE calculation issue" in caplog.text
        # Restore original generation data
        solcast._data_generation = original_generation

        # Test scenario where a better model is found but improvement is insufficient
        # This exercises the "Insufficient improvement" log message
        caplog.clear()
        _LOGGER.debug("Testing adaptive dampening with insufficient improvement threshold")
        # Set a very high minimum error delta (100%) so any improvement is insufficient
        original_min_error_delta = solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_MINIMUM_ERROR_DELTA]
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_MINIMUM_ERROR_DELTA] = 100.0
        original_model = solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL]
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL] = 1
        original_delta = solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL]
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL] = 1
        await solcast.determine_best_dampening_settings()
        assert "Insufficient improvement" in caplog.text
        # Restore original values
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_MINIMUM_ERROR_DELTA] = original_min_error_delta
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL] = original_model
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL] = original_delta

        # Test scenario where dampening history references days not in actuals
        # This exercises: if day_start not in actuals: valid = False
        # Need to trigger this without breaking the continuity check in _find_earliest_common_history so remove _data_actuals data for a specific day
        caplog.clear()
        _LOGGER.debug("Testing adaptive dampening with missing actuals for dampening history entry")
        # Get one of the days from dampening history that should have actuals
        sample_entry = solcast._data_dampening_history[0][-1][1]
        problem_day = solcast._get_day_start(sample_entry["period_start"])
        saved_actuals = {}
        for site_id in solcast._data_actuals[SITE_INFO]:
            if site_id not in saved_actuals:
                saved_actuals[site_id] = []

            remaining_actuals = []
            for actual in solcast._data_actuals[SITE_INFO][site_id][FORECASTS]:
                ts = actual[PERIOD_START].astimezone(solcast._tz)
                day_start = solcast._get_day_start(ts)
                if day_start == problem_day:
                    saved_actuals[site_id].append(actual)
                else:
                    remaining_actuals.append(actual)

            solcast._data_actuals[SITE_INFO][site_id][FORECASTS] = remaining_actuals
        await solcast.determine_best_dampening_settings()
        assert "Determining best automated dampening settings" in caplog.text
        assert "skipped due to missing actuals for dampening history entry" in caplog.text

        # Restore the actuals data
        for site_id, saved in saved_actuals.items():
            solcast._data_actuals[SITE_INFO][site_id][FORECASTS].extend(saved)

        # Corrupt the history and reload it
        caplog.clear()
        _LOGGER.debug("Corrupting dampening history and reloading it")
        Path(f"{config_dir}/solcast-dampening-history.json").write_text("having a bad day", encoding="utf-8")
        await solcast.load_dampening_history()
        assert "Dampening history file is corrupt" in caplog.text

    finally:
        entity_history["days_generation"] = 3
        entity_history["days_suppression"] = 3
        entity_history["offset"] = -1
        session_clear(MOCK_CORRUPT_ACTUALS)
        assert await async_cleanup_integration_tests(hass)


async def test_update_dampening_history_deal_breaker(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test update_dampening_history with deal breaker conditions."""

    assert await async_cleanup_integration_tests(hass)

    try:
        config_dir = f"{hass.config.config_dir}/{CONFIG_DISCRETE_NAME}" if CONFIG_FOLDER_DISCRETE else hass.config.config_dir
        if CONFIG_FOLDER_DISCRETE:
            Path(config_dir).mkdir(parents=False, exist_ok=True)

        Path(f"{config_dir}/solcast-advanced.json").write_text(
            json.dumps(
                {
                    "automated_dampening_adaptive_model_configuration": True,
                }
            ),
            encoding="utf-8",
        )

        entity_history["days_generation"] = 1
        entity_history["days_suppression"] = 0
        entity_history["offset"] = -1

        options = copy.deepcopy(DEFAULT_INPUT2)
        options[AUTO_UPDATE] = 0
        options[AUTO_DAMPEN] = True
        options[GET_ACTUALS] = True
        options[USE_ACTUALS] = True

        entry = await async_init_integration(hass, options, extra_sensors=ExtraSensors.YES)
        solcast: SolcastApi = entry.runtime_data.coordinator.solcast

        # Test scenario: No generation data (deal breaker)
        caplog.clear()
        _LOGGER.debug("Testing update_dampening_history with no generation data")
        # Clear generation data to trigger the "No generation yet" deal breaker
        solcast._data_generation[GENERATION] = []
        await solcast.update_dampening_history()
        assert "Auto-dampening suppressed: No generation yet" in caplog.text

    finally:
        entity_history["days_generation"] = 3
        entity_history["days_suppression"] = 3
        entity_history["offset"] = -1
        assert await async_cleanup_integration_tests(hass)


async def test_get_pv_generation_period_edges_and_gaps(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test period start/end handling and long gaps in generation history."""

    assert await async_cleanup_integration_tests(hass)

    try:
        options = copy.deepcopy(DEFAULT_INPUT2)
        options[GENERATION_ENTITIES] = ["sensor.solar_export_sensor_1111_1111_1111_1111"]
        entry = await async_init_integration(hass, options, extra_sensors=ExtraSensors.YES)
        solcast = entry.runtime_data.coordinator.solcast
        entity_id = options[GENERATION_ENTITIES][0]

        period_start_raw = solcast.get_day_start_utc(future=0) - timedelta(days=1)
        period_end_raw = solcast.get_day_start_utc(future=0)
        period_start = dt.fromtimestamp(period_start_raw.timestamp(), datetime.UTC)
        period_end = dt.fromtimestamp(period_end_raw.timestamp(), datetime.UTC)

        def _state(value: float, when: dt) -> State:
            return State(
                entity_id,
                str(value),
                {ATTR_UNIT_OF_MEASUREMENT: "kWh"},
                last_changed=when,
                last_updated=when,
            )

        states = [
            _state(0.0, period_start),
            _state(0.1, period_start + timedelta(minutes=5)),
            _state(0.2, period_start + timedelta(minutes=10)),
            _state(0.3, period_start + timedelta(minutes=15)),
            _state(0.4, period_start + timedelta(minutes=20)),
            _state(0.6, period_start + timedelta(hours=2)),
            _state(0.7, period_end),
        ]

        solcast._data_generation[GENERATION] = [{PERIOD_START: period_start, GENERATION: 0.0, EXPORT_LIMITING: False}]

        caplog.clear()
        with patch(
            "homeassistant.components.solcast_solar.solcastapi.state_changes_during_period",
            return_value={entity_id: states},
        ):
            await solcast.get_pv_generation()

        generation = {record[PERIOD_START]: record[GENERATION] for record in solcast._data_generation[GENERATION]}
        day_start = min(generation)
        day_end = day_start + timedelta(days=1)
        day_generation = {k: v for k, v in generation.items() if day_start <= k < day_end}

        assert "Generation-consistent increments detected" in caplog.text
        assert sum(day_generation.values()) > 0
    finally:
        assert await async_cleanup_integration_tests(hass)


async def test_get_pv_generation_uniform_increment_log(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test uniform increment detection log in get_pv_generation."""

    assert await async_cleanup_integration_tests(hass)

    try:
        options = copy.deepcopy(DEFAULT_INPUT2)
        options[GENERATION_ENTITIES] = ["sensor.solar_export_sensor_1111_1111_1111_1111"]
        entry = await async_init_integration(hass, options, extra_sensors=ExtraSensors.YES)
        solcast = entry.runtime_data.coordinator.solcast
        entity_id = options[GENERATION_ENTITIES][0]

        fixed_day = dt(2026, 2, 9, tzinfo=datetime.UTC)
        period_start = fixed_day - timedelta(days=1)
        period_end = fixed_day

        monkeypatch.setattr(solcast, "get_day_start_utc", lambda future=0: fixed_day)

        def _state(value: float, when: dt) -> State:
            return State(
                entity_id,
                str(value),
                {ATTR_UNIT_OF_MEASUREMENT: "kWh"},
                last_changed=when,
                last_updated=when,
            )

        states = [
            _state(0.0, period_start),
            _state(0.1, period_start + timedelta(minutes=5)),
            _state(0.2, period_start + timedelta(minutes=10)),
            _state(0.3, period_start + timedelta(minutes=15)),
            _state(0.4, period_start + timedelta(minutes=20)),
            _state(0.5, period_start + timedelta(minutes=25)),
            _state(0.6, period_start + timedelta(minutes=30)),
            _state(0.7, period_start + timedelta(minutes=35)),
            _state(0.8, period_start + timedelta(minutes=40)),
            _state(0.9, period_start + timedelta(minutes=45)),
            _state(1.0, period_end),
        ]

        solcast._data_generation[GENERATION] = [{PERIOD_START: period_start, GENERATION: 0.0, EXPORT_LIMITING: False}]

        with (
            patch(
                "homeassistant.components.solcast_solar.solcastapi.state_changes_during_period",
                return_value={entity_id: states},
            ),
            patch("homeassistant.components.solcast_solar.solcastapi._LOGGER.debug") as debug_mock,
        ):
            await solcast.get_pv_generation()

        assert any("increments detected" in call.args[0] for call in debug_mock.call_args_list)
    finally:
        assert await async_cleanup_integration_tests(hass)


async def test_get_pv_generation_zero_timedelta_samples(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test generation handling when time delta samples are all zero."""

    assert await async_cleanup_integration_tests(hass)

    try:
        options = copy.deepcopy(DEFAULT_INPUT2)
        options[GENERATION_ENTITIES] = ["sensor.solar_export_sensor_1111_1111_1111_1111"]
        entry = await async_init_integration(hass, options, extra_sensors=ExtraSensors.YES)
        solcast = entry.runtime_data.coordinator.solcast
        entity_id = options[GENERATION_ENTITIES][0]

        fixed_day = dt(2026, 2, 9, tzinfo=datetime.UTC)
        period_start = fixed_day - timedelta(days=1)

        monkeypatch.setattr(solcast, "get_day_start_utc", lambda future=0: fixed_day)

        def _state(value: float, when: dt) -> State:
            return State(
                entity_id,
                str(value),
                {ATTR_UNIT_OF_MEASUREMENT: "kWh"},
                last_changed=when,
                last_updated=when,
            )

        states = [
            _state(0.0, period_start),
            _state(0.1, period_start),
            _state(0.2, period_start),
            _state(0.3, period_start),
            _state(0.4, period_start),
            _state(0.5, period_start),
        ]

        solcast._data_generation[GENERATION] = [{PERIOD_START: period_start, GENERATION: 0.0, EXPORT_LIMITING: False}]

        with patch(
            "homeassistant.components.solcast_solar.solcastapi.state_changes_during_period",
            return_value={entity_id: states},
        ):
            await solcast.get_pv_generation()

        assert "increments detected" in caplog.text
    finally:
        assert await async_cleanup_integration_tests(hass)


async def test_select_comparison_interval_variance(
    recorder_mock: Recorder,
    hass: HomeAssistant,
) -> None:
    """Test comparison interval selection with variance across models."""

    assert await async_cleanup_integration_tests(hass)

    try:
        entry = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT2))
        solcast = entry.runtime_data.coordinator.solcast

        day_start = solcast.get_day_start_utc() - timedelta(days=1)
        ts = day_start
        generation_dampening = defaultdict(dict, {ts: {GENERATION: 1.0, EXPORT_LIMITING: False}})

        factors_a = [1.0] * 48
        factors_b = [1.0] * 48
        factors_a[0] = 0.8
        factors_b[0] = 0.6

        solcast._data_dampening_history = {
            0: {0: [{"period_start": day_start, "factors": factors_a}, {"period_start": day_start, "factors": factors_b}]},
            1: {0: [{"period_start": day_start, "factors": factors_b}, {"period_start": day_start, "factors": factors_a}]},
        }

        selected_interval, avg_gen, avg_factor, variance = solcast._select_comparison_interval(generation_dampening, 1)

        assert selected_interval == 0
        assert avg_gen > 0
        assert avg_factor < 1.0
        assert variance > 0
    finally:
        assert await async_cleanup_integration_tests(hass)


async def test_select_comparison_interval_single_factor(
    recorder_mock: Recorder,
    hass: HomeAssistant,
) -> None:
    """Test comparison interval selection with single-factor history."""

    assert await async_cleanup_integration_tests(hass)

    try:
        entry = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT2))
        solcast = entry.runtime_data.coordinator.solcast

        day_start = solcast.get_day_start_utc() - timedelta(days=1)
        generation_dampening = defaultdict(dict, {day_start: {GENERATION: 1.0, EXPORT_LIMITING: False}})

        factors = [1.0] * 48
        factors[0] = 0.9

        solcast._data_dampening_history = {0: {0: [{"period_start": day_start, "factors": factors}]}}

        selected_interval, avg_gen, avg_factor, variance = solcast._select_comparison_interval(generation_dampening, 1)

        assert selected_interval == 0
        assert avg_gen > 0
        assert avg_factor < 1.0
        assert variance == 0.0
    finally:
        assert await async_cleanup_integration_tests(hass)


async def test_calculate_single_interval_error_with_generation(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test single-interval error when generation is present."""

    assert await async_cleanup_integration_tests(hass)

    try:
        entry = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT2))
        solcast = entry.runtime_data.coordinator.solcast

        day_start = solcast.get_day_start_utc()
        peak_interval = 0
        dampened_actuals = defaultdict(lambda: [4.0] * 48)
        dampened_actuals[solcast._get_day_start(day_start)] = [4.0] * 48
        generation_dampening = defaultdict(dict, {day_start: {GENERATION: 1.0, EXPORT_LIMITING: False}})

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(solcast, "adjusted_interval_dt", lambda _ts: 0)

        has_inf, mean_ape, percentiles = await solcast.calculate_single_interval_error(
            dampened_actuals,
            generation_dampening,
            peak_interval,
            percentiles=(50,),
            log_breakdown=True,
        )

        assert has_inf is False
        assert mean_ape > 0
        assert percentiles[0] > 0
        assert "Single interval APE for day" in caplog.text
    finally:
        monkeypatch.undo()
        assert await async_cleanup_integration_tests(hass)


async def test_calculate_single_interval_error_no_generation(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test single-interval error handling when no generation is present."""

    assert await async_cleanup_integration_tests(hass)

    try:
        entry = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT2))
        solcast = entry.runtime_data.coordinator.solcast

        day_start = solcast.get_day_start_utc()
        dampened_actuals = defaultdict(lambda: [1.0] * 48)
        dampened_actuals[solcast._get_day_start(day_start)] = [1.0] * 48
        generation_dampening = defaultdict(dict, {day_start: {GENERATION: 0.0, EXPORT_LIMITING: False}})

        has_inf, mean_ape, percentiles = await solcast.calculate_single_interval_error(
            dampened_actuals,
            generation_dampening,
            0,
            percentiles=(50,),
            log_breakdown=True,
        )

        assert has_inf is False
        assert mean_ape == math.inf
        assert percentiles == [math.inf]
        assert "Single interval APE for day" in caplog.text
    finally:
        assert await async_cleanup_integration_tests(hass)


async def test_calculate_single_interval_error_skips_ignored_and_missing(
    recorder_mock: Recorder,
    hass: HomeAssistant,
) -> None:
    """Test single-interval error skips ignored days and missing actuals."""

    assert await async_cleanup_integration_tests(hass)

    monkeypatch = pytest.MonkeyPatch()

    try:
        entry = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT2))
        solcast = entry.runtime_data.coordinator.solcast

        day_start = solcast.get_day_start_utc()
        next_day = day_start + timedelta(days=1)

        generation_dampening = defaultdict(
            dict,
            {
                day_start: {GENERATION: 1.0, EXPORT_LIMITING: False},
                next_day: {GENERATION: 1.0, EXPORT_LIMITING: False},
            },
        )

        dampened_actuals = defaultdict(lambda: [1.0] * 48)
        day_start_key = solcast._get_day_start(day_start)
        dampened_actuals[day_start_key] = [1.0] * 48

        ignored_days = {day_start_key: True}

        monkeypatch.setattr(solcast, "adjusted_interval_dt", lambda _ts: 0)

        has_inf, mean_ape, percentiles = await solcast.calculate_single_interval_error(
            dampened_actuals,
            generation_dampening,
            0,
            percentiles=(50,),
            ignored_days=ignored_days,
        )

        assert has_inf is False
        assert mean_ape == math.inf
        assert percentiles == [math.inf]
    finally:
        monkeypatch.undo()
        assert await async_cleanup_integration_tests(hass)


async def test_determine_best_dampening_settings_alternative_issue(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test alternate model issue creation and clearing in adaptive dampening."""

    assert await async_cleanup_integration_tests(hass)

    try:
        options = copy.deepcopy(DEFAULT_INPUT2)
        options[GENERATION_ENTITIES] = ["sensor.solar_export_sensor_1111_1111_1111_1111"]
        entry = await async_init_integration(hass, options, extra_sensors=ExtraSensors.YES)
        solcast = entry.runtime_data.coordinator.solcast

        day_start = solcast.get_day_start_utc() - timedelta(days=1)
        factors = [1.0] * 48
        factors[0] = 0.9
        history_entry = {"period_start": day_start, "factors": factors}

        min_model = ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_MODEL][MINIMUM]
        max_model = ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_MODEL][MAXIMUM]
        min_delta = ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL][MINIMUM_EXTENDED]
        max_delta = ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL][MAXIMUM]

        solcast._data_dampening_history = {
            model: {delta: [copy.deepcopy(history_entry)] for delta in range(min_delta, max_delta + 1)}
            for model in range(min_model, max_model + 1)
        }

        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_MINIMUM_HISTORY_DAYS] = 1
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_MINIMUM_ERROR_DELTA] = 1.0
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_NO_DELTA_ADJUSTMENT] = False
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL] = max_model
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL] = max_delta

        monkeypatch.setattr(solcast, "_find_earliest_common_history", lambda _days: day_start)
        monkeypatch.setattr(solcast, "_build_actuals_from_sites", lambda _start: {day_start: [1.0] * 48})

        async def _fake_prepare_generation_data(_earliest: dt):
            generation_dampening = defaultdict(dict)
            generation_dampening[day_start] = {GENERATION: 1.0, EXPORT_LIMITING: False}
            generation_dampening_day = defaultdict(float)
            generation_dampening_day[solcast._get_day_start(day_start)] = 1.0
            return generation_dampening, generation_dampening_day

        monkeypatch.setattr(solcast, "prepare_generation_data", _fake_prepare_generation_data)

        def _record_should_skip(model: int, delta: int, _min_days: int) -> tuple[bool, str]:
            solcast._test_current_model = model  # pyright: ignore[reportPrivateUsage]
            solcast._test_current_delta = delta  # pyright: ignore[reportPrivateUsage]
            return False, ""

        monkeypatch.setattr(solcast, "_should_skip_model_delta", _record_should_skip)

        alternate_better = True

        async def _fake_calculate_single_interval_error(*_args, **_kwargs):
            model = solcast._test_current_model  # pyright: ignore[reportPrivateUsage]
            delta = solcast._test_current_delta  # pyright: ignore[reportPrivateUsage]
            if delta == VALUE_ADAPTIVE_DAMPENING_NO_DELTA:
                error = 5.0 if alternate_better and model == min_model else 15.0
                return False, error, [error]
            return True, 10.0, [10.0]

        monkeypatch.setattr(solcast, "calculate_single_interval_error", _fake_calculate_single_interval_error)

        caplog.clear()
        await solcast.determine_best_dampening_settings()
        assert "but adaptive dampening found that model" in caplog.text

        alternate_better = False
        caplog.clear()
        await solcast.determine_best_dampening_settings()
        assert "but adaptive dampening found that model" not in caplog.text
    finally:
        assert await async_cleanup_integration_tests(hass)


async def test_get_pv_generation_power_entity(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test power entity (kW) processing with time-weighted averaging in get_pv_generation."""

    assert await async_cleanup_integration_tests(hass)

    try:
        power_entity_id = "sensor.solar_power_sensor"

        options = copy.deepcopy(DEFAULT_INPUT2)
        options[GENERATION_ENTITIES] = [power_entity_id]
        entry = await async_init_integration(hass, options, extra_sensors=ExtraSensors.YES)
        solcast = entry.runtime_data.coordinator.solcast

        # Register the entity as a power entity in the entity registry.
        entity_registry = er.async_get(hass)
        entity_registry.async_get_or_create(
            "sensor",
            "pytest",
            "solar_power_sensor",
            config_entry=entry,
            suggested_object_id="solar_power_sensor",
            unit_of_measurement="kW",
            original_device_class=SensorDeviceClass.POWER,
        )

        period_start_raw = solcast.get_day_start_utc(future=0) - timedelta(days=1)
        period_end_raw = solcast.get_day_start_utc(future=0)
        period_start = dt.fromtimestamp(period_start_raw.timestamp(), datetime.UTC)
        period_end = dt.fromtimestamp(period_end_raw.timestamp(), datetime.UTC)

        def _state(value: float, when: dt) -> State:
            return State(
                power_entity_id,
                str(value),
                {ATTR_UNIT_OF_MEASUREMENT: "kW"},
                last_changed=when,
                last_updated=when,
            )

        # Interval 1 (00:00-00:30): 4.0 kW for 15 min, then 0.0 kW for 15 min.
        #   weighted avg = (4.0*900 + 0.0*900) / 1800 = 2.0 kW → 2.0 * 0.5 = 1.0 kWh
        # Interval 2 (00:30-01:00): constant 6.0 kW.
        #   weighted avg = 6.0 kW → 6.0 * 0.5 = 3.0 kWh
        states = [
            _state(4.0, period_start),
            _state(0.0, period_start + timedelta(minutes=15)),
            _state(6.0, period_start + timedelta(minutes=30)),
            _state(6.0, period_start + timedelta(minutes=45)),
            _state(0.0, period_start + timedelta(minutes=60)),
            _state(0.0, period_end),
        ]

        solcast._data_generation[GENERATION] = [{PERIOD_START: period_start, GENERATION: 0.0, EXPORT_LIMITING: False}]

        caplog.clear()
        with patch(
            "homeassistant.components.solcast_solar.solcastapi.state_changes_during_period",
            return_value={power_entity_id: states},
        ):
            await solcast.get_pv_generation()

        generation = {record[PERIOD_START]: record[GENERATION] for record in solcast._data_generation[GENERATION]}
        day_start = min(generation)
        day_end = day_start + timedelta(days=1)
        day_generation = {k: v for k, v in generation.items() if day_start <= k < day_end}

        # First interval: time-weighted average 2.0 kW → 1.0 kWh
        assert abs(day_generation[period_start] - 1.0) < 0.01
        # Second interval: constant 6.0 kW → 3.0 kWh
        second_interval = period_start + timedelta(minutes=30)
        assert abs(day_generation[second_interval] - 3.0) < 0.01

        assert "Retrieved day" in caplog.text
    finally:
        assert await async_cleanup_integration_tests(hass)


async def test_get_pv_generation_power_entity_watt_conversion(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test power entity using watts with unit conversion in get_pv_generation."""

    assert await async_cleanup_integration_tests(hass)

    try:
        power_entity_id = "sensor.solar_power_watts"

        options = copy.deepcopy(DEFAULT_INPUT2)
        options[GENERATION_ENTITIES] = [power_entity_id]
        entry = await async_init_integration(hass, options, extra_sensors=ExtraSensors.YES)
        solcast = entry.runtime_data.coordinator.solcast

        # Register entity with watts unit and POWER device class.
        entity_registry = er.async_get(hass)
        entity_registry.async_get_or_create(
            "sensor",
            "pytest",
            "solar_power_watts",
            config_entry=entry,
            suggested_object_id="solar_power_watts",
            unit_of_measurement="W",
            original_device_class=SensorDeviceClass.POWER,
        )

        period_start_raw = solcast.get_day_start_utc(future=0) - timedelta(days=1)
        period_end_raw = solcast.get_day_start_utc(future=0)
        period_start = dt.fromtimestamp(period_start_raw.timestamp(), datetime.UTC)
        period_end = dt.fromtimestamp(period_end_raw.timestamp(), datetime.UTC)

        def _state(value: float, when: dt) -> State:
            return State(
                power_entity_id,
                str(value),
                {ATTR_UNIT_OF_MEASUREMENT: "W"},
                last_changed=when,
                last_updated=when,
            )

        # Constant 2000 W (= 2.0 kW after conversion factor 0.001) for the first 30 minutes.
        # avg = 2.0 kW → 2.0 * 0.5 = 1.0 kWh
        states = [
            _state(2000.0, period_start),
            _state(2000.0, period_start + timedelta(minutes=10)),
            _state(2000.0, period_start + timedelta(minutes=20)),
            _state(0.0, period_start + timedelta(minutes=30)),
            _state(0.0, period_end),
        ]

        solcast._data_generation[GENERATION] = [{PERIOD_START: period_start, GENERATION: 0.0, EXPORT_LIMITING: False}]

        caplog.clear()
        with patch(
            "homeassistant.components.solcast_solar.solcastapi.state_changes_during_period",
            return_value={power_entity_id: states},
        ):
            await solcast.get_pv_generation()

        generation = {record[PERIOD_START]: record[GENERATION] for record in solcast._data_generation[GENERATION]}

        # First interval: 2000 W = 2.0 kW → 1.0 kWh
        assert abs(generation[period_start] - 1.0) < 0.01

        # Verify the W → kW conversion log was emitted.
        assert "applying conversion factor" in caplog.text
    finally:
        assert await async_cleanup_integration_tests(hass)


async def test_get_pv_generation_power_entity_insufficient_readings(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test power entity with insufficient numeric readings in get_pv_generation."""

    assert await async_cleanup_integration_tests(hass)

    try:
        power_entity_id = "sensor.solar_power_sensor"

        options = copy.deepcopy(DEFAULT_INPUT2)
        options[GENERATION_ENTITIES] = [power_entity_id]
        entry = await async_init_integration(hass, options, extra_sensors=ExtraSensors.YES)
        solcast = entry.runtime_data.coordinator.solcast

        # Register entity as a power entity.
        entity_registry = er.async_get(hass)
        entity_registry.async_get_or_create(
            "sensor",
            "pytest",
            "solar_power_sensor",
            config_entry=entry,
            suggested_object_id="solar_power_sensor",
            unit_of_measurement="kW",
            original_device_class=SensorDeviceClass.POWER,
        )

        period_start_raw = solcast.get_day_start_utc(future=0) - timedelta(days=1)
        period_start = dt.fromtimestamp(period_start_raw.timestamp(), datetime.UTC)

        # 5 states total (passes the >4 check) but only 1 numeric reading.
        states = [
            State(power_entity_id, "2.0", {ATTR_UNIT_OF_MEASUREMENT: "kW"}, last_changed=period_start, last_updated=period_start),
            State(
                power_entity_id,
                "unavailable",
                {},
                last_changed=period_start + timedelta(minutes=5),
                last_updated=period_start + timedelta(minutes=5),
            ),
            State(
                power_entity_id,
                "unknown",
                {},
                last_changed=period_start + timedelta(minutes=10),
                last_updated=period_start + timedelta(minutes=10),
            ),
            State(
                power_entity_id,
                "unavailable",
                {},
                last_changed=period_start + timedelta(minutes=15),
                last_updated=period_start + timedelta(minutes=15),
            ),
            State(
                power_entity_id,
                "unknown",
                {},
                last_changed=period_start + timedelta(minutes=20),
                last_updated=period_start + timedelta(minutes=20),
            ),
        ]

        solcast._data_generation[GENERATION] = [{PERIOD_START: period_start, GENERATION: 0.0, EXPORT_LIMITING: False}]

        caplog.clear()
        with patch(
            "homeassistant.components.solcast_solar.solcastapi.state_changes_during_period",
            return_value={power_entity_id: states},
        ):
            await solcast.get_pv_generation()

        assert "Insufficient power readings for entity" in caplog.text
    finally:
        assert await async_cleanup_integration_tests(hass)


async def test_config_flow_mixed_generation_entity_types(
    hass: HomeAssistant,
) -> None:
    """Test config flow rejects mixed energy and power generation entities."""

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="solcast_pv_solar",
        title="Solcast PV Forecast",
        data=copy.deepcopy(DEFAULT_INPUT2),
        options=copy.deepcopy(DEFAULT_INPUT2),
    )
    entry.add_to_hass(hass)

    # Register one ENERGY and one POWER entity.
    entity_registry = er.async_get(hass)
    entity_registry.async_get_or_create(
        "sensor",
        "pytest",
        "energy_sensor",
        config_entry=entry,
        suggested_object_id="energy_sensor",
        unit_of_measurement="kWh",
        original_device_class=SensorDeviceClass.ENERGY,
    )
    entity_registry.async_get_or_create(
        "sensor",
        "pytest",
        "power_sensor",
        config_entry=entry,
        suggested_object_id="power_sensor",
        unit_of_measurement="kW",
        original_device_class=SensorDeviceClass.POWER,
    )

    flow = SolcastSolarOptionFlowHandler(entry)
    flow.hass = hass
    user_input = copy.deepcopy(DEFAULT_INPUT2)
    user_input[GENERATION_ENTITIES] = ["sensor.energy_sensor", "sensor.power_sensor"]
    user_input[SITE_EXPORT_ENTITY] = []
    result = await flow.async_step_init(user_input)
    assert result["errors"]["base"] == "generation_mixed_types"  # type: ignore[index]
