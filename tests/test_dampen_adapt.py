"""Tests for Solcast Solar automated dampening adaptation."""

from collections import defaultdict
import copy
from datetime import datetime as dt, timedelta
import json
import logging
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.recorder import Recorder
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
    ALL,
    AUTO_DAMPEN,
    AUTO_UPDATE,
    CONFIG_DISCRETE_NAME,
    CONFIG_FOLDER_DISCRETE,
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
    SITE_EXPORT_ENTITY,
    SITE_EXPORT_LIMIT,
    SITE_INFO,
    USE_ACTUALS,
    VALUE_ADAPTIVE_DAMPENING_NO_DELTA,
)
from homeassistant.components.solcast_solar.solcastapi import SolcastApi
from homeassistant.components.solcast_solar.util import (
    DateTimeEncoder,
    JSONDecoder,
    NoIndentEncoder,
)
from homeassistant.core import HomeAssistant

from . import (
    DEFAULT_INPUT2,
    MOCK_CORRUPT_ACTUALS,
    ZONE_RAW,
    ExtraSensors,
    async_cleanup_integration_tests,
    async_init_integration,
    entity_history,
    no_exception,
    reload_integration,
    session_clear,
    wait_for_it,
)

_LOGGER = logging.getLogger(__name__)


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
        coordinator, solcast = await reload_integration(hass, entry)
        if coordinator is None or solcast is None:
            pytest.fail("Reload failed")

        # Assert good start, that actuals and generation are enabled, and that the caches are saved
        _LOGGER.debug("Testing good start happened")
        await wait_for_it(hass, caplog, freezer, "Clear presumed dead flag", long_time=False)
        no_exception(caplog)

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
            solcast.data_actuals["siteinfo"]["1111-1111-1111-1111"]["forecasts"].pop(removed)
            freezer.move_to((dt.now(solcast.tz) + timedelta(**roll)).replace(minute=0, second=0, microsecond=0))
            await hass.async_block_till_done()
            solcast.suppress_advanced_watchdog_reload = True
            await solcast.advanced_opt.read_advanced_options()
            await wait_for_it(hass, caplog, freezer, "Update generation data", long_time=True)
            await wait_for_it(hass, caplog, freezer, "Estimated actual mean APE", long_time=True)
            no_exception(caplog)
            assert "Updating automated dampening adaptation history" in caplog.text
            assert "Task dampening update_history took" in caplog.text
            match count:
                case 2:
                    assert "Determining best automated dampening settings" in caplog.text
                    assert "Dampening history actuals suppressed site 3333-3333-3333-3333" in caplog.text
                    assert "Skipping model 2 and delta 0 as history of 2 days" in caplog.text
                    assert "Skipping model 2 and delta 1 as history of 1 days" in caplog.text
                    assert "Advanced option 'automated_dampening_delta_adjustment_model' set to: 1" in caplog.text
                    assert "Advanced option 'automated_dampening_model' set to: 0" in caplog.text
                    assert "Task serialise_advanced_options took" in caplog.text
                    assert re.search(r"Advanced options file .+ exists", caplog.text) is None

                    solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_APE_SHIT] = True
                    solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_EXCLUDE] = [{"model": 2, "delta": -1}]
                    await solcast.dampening.determine_best_settings()
                    assert "Adaptive dampening selection going ape shit" in caplog.text
                    assert "Skipping model 2 and delta -1 as in automated_dampening_adaptive_model_exclude" in caplog.text
                    solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_APE_SHIT] = False
                    solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_EXCLUDE] = []

                    solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_NO_DELTA_ADJUSTMENT] = True
                    await solcast.dampening.determine_best_settings()
                    solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_NO_DELTA_ADJUSTMENT] = False

                    # Remove the 2nd day from all model/deltas
                    now_missing_day_2 = defaultdict(dict)
                    for model in solcast.dampening.auto_factors_history:
                        for delta in solcast.dampening.auto_factors_history[model]:
                            if len(solcast.dampening.auto_factors_history[model][delta]) > 1:
                                now_missing_day_2[model][delta] = copy.deepcopy(solcast.dampening.auto_factors_history[model][delta][1])
                                solcast.dampening.auto_factors_history[model][delta].pop(1)

                case 3:
                    assert "Determining best automated dampening settings" in caplog.text
                    assert "Insufficient continuous dampening history to determine best automated dampening settings" in caplog.text

                    # Reinstate the 2nd day from all model/deltas
                    for model in solcast.dampening.auto_factors_history:
                        for delta in solcast.dampening.auto_factors_history[model]:
                            if model in now_missing_day_2 and delta in now_missing_day_2[model]:
                                solcast.dampening.auto_factors_history[model][delta].append(now_missing_day_2[model][delta])

                    # Write history to file so it persists through reload
                    Path(f"{config_dir}/solcast-dampening-history.json").write_text(
                        json.dumps(
                            solcast.dampening.auto_factors_history, ensure_ascii=False, indent=2, cls=NoIndentEncoder, above_level=4
                        ),
                        encoding="utf-8",
                    )

                case 1:
                    assert "Insufficient continuous dampening history" in caplog.text
                    # Knobble the history for some combos
                    now_missing_2_1 = copy.deepcopy(solcast.dampening.auto_factors_history[2][1])
                    now_missing_2_0_1 = copy.deepcopy(solcast.dampening.auto_factors_history[2][0][1])
                    now_missing_3_0_1 = copy.deepcopy(solcast.dampening.auto_factors_history[3][0][1])
                    solcast.dampening.auto_factors_history[2][0] = solcast.dampening.auto_factors_history[3][0][:-1]
                    solcast.dampening.auto_factors_history[2][1] = []
                    solcast.dampening.auto_factors_history[3][0] = [solcast.dampening.auto_factors_history[3][0][0]]

                case 0:
                    assert "Insufficient continuous dampening history" in caplog.text

        # Reload to load dampening factor history
        caplog.clear()
        coordinator, solcast = await reload_integration(hass, entry)
        if coordinator is None or solcast is None:
            pytest.fail("Reload failed")
        await wait_for_it(hass, caplog, freezer, "Completed task stale_update", long_time=True)
        await wait_for_it(hass, caplog, freezer, "Task dampening load_history took")

        # Re-add dampening history for today
        caplog.clear()
        _LOGGER.debug("Re-adding dampening history for today")
        await solcast.dampening.update_history()

        # Test valid and full history
        solcast.dampening.auto_factors_history[2][1] = solcast.dampening.auto_factors_history[2][1] + list(now_missing_2_1)
        solcast.dampening.auto_factors_history[2][0].append(now_missing_2_0_1)
        solcast.dampening.auto_factors_history[3][0].append(now_missing_3_0_1)
        Path(f"{config_dir}/solcast-dampening-history.json").write_text(
            json.dumps(solcast.dampening.auto_factors_history, ensure_ascii=False, indent=2, cls=NoIndentEncoder, above_level=4),
            encoding="utf-8",
        )
        caplog.clear()
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL_DAYS] = 4
        await solcast.dampening.load_history()
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
        old_data = copy.deepcopy(solcast.dampening.auto_factors_history)
        for delta in range(-1, 2):
            if len(solcast.dampening.auto_factors_history[1][delta]) >= 4:
                solcast.dampening.auto_factors_history[1][delta].pop(0)
        for delta in range(-1, 2):
            if len(solcast.dampening.auto_factors_history[2][delta]) >= 4:
                solcast.dampening.auto_factors_history[2][delta].pop(0)
                solcast.dampening.auto_factors_history[2][delta].pop(0)
        for delta in range(-1, 2):
            if len(solcast.dampening.auto_factors_history[3][delta]) >= 4:
                solcast.dampening.auto_factors_history[3][delta].pop(0)
        await solcast.dampening.determine_best_settings()  # Should skip early entries for models 0, 1, 3
        # Should complete successfully despite staggered dates
        assert "Determining best automated dampening settings" in caplog.text
        assert "Earliest date with complete dampening history" in caplog.text
        assert "delta is" in caplog.text and "days" in caplog.text
        assert "Skipping model" in caplog.text or "history of" in caplog.text
        solcast.dampening.auto_factors_history = old_data

        # Test scenario where all generation is zero (all APE values become infinity)
        # This exercises the defensive check: if error_metric == math.inf
        caplog.clear()
        _LOGGER.debug("Testing adaptive dampening with all zero generation (infinity APE)")
        # Store original generation data
        original_generation = copy.deepcopy(solcast.dampening.data_generation)
        # Set all generation to zero to trigger infinity APE
        for gen_entry in solcast.dampening.data_generation[GENERATION]:
            gen_entry[GENERATION] = 0.0
        await solcast.dampening.determine_best_settings()
        # Should log the defensive check message for skipping APE calculation
        assert "Determining best automated dampening settings" in caplog.text
        assert "Skipping APE calculation for model" in caplog.text
        assert "due to APE calculation issue" in caplog.text
        # Restore original generation data
        solcast.dampening.data_generation = original_generation

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
        await solcast.dampening.determine_best_settings()
        # With borda_scores non-empty the improvement is forced to inf, so the
        # threshold is always exceeded and settings are updated when is_different.
        assert "Updating automated dampening settings based on" in caplog.text
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
        sample_entry = solcast.dampening.auto_factors_history[0][-1][1]
        problem_day = solcast.dt_helper.day_start(sample_entry["period_start"])
        saved_actuals = {}
        for site_id in solcast.data_actuals[SITE_INFO]:
            if site_id not in saved_actuals:
                saved_actuals[site_id] = []

            remaining_actuals = []
            for actual in solcast.data_actuals[SITE_INFO][site_id][FORECASTS]:
                ts = actual[PERIOD_START].astimezone(solcast.tz)
                day_start = solcast.dt_helper.day_start(ts)
                if day_start == problem_day:
                    saved_actuals[site_id].append(actual)
                else:
                    remaining_actuals.append(actual)

            solcast.data_actuals[SITE_INFO][site_id][FORECASTS] = remaining_actuals
        await solcast.dampening.determine_best_settings()
        assert "Determining best automated dampening settings" in caplog.text
        assert "skipped due to missing actuals for dampening history entry" in caplog.text

        # Restore the actuals data
        for site_id, saved in saved_actuals.items():
            solcast.data_actuals[SITE_INFO][site_id][FORECASTS].extend(saved)

        # Corrupt the history and reload it
        caplog.clear()
        _LOGGER.debug("Corrupting dampening history and reloading it")
        Path(f"{config_dir}/solcast-dampening-history.json").write_text("having a bad day", encoding="utf-8")
        await solcast.dampening.load_history()
        assert "Dampening history file is corrupt" in caplog.text

    finally:
        entity_history["days_generation"] = 3
        entity_history["days_suppression"] = 3
        entity_history["offset"] = -1
        session_clear(MOCK_CORRUPT_ACTUALS)
        assert await async_cleanup_integration_tests(hass)


async def test_update_history_deal_breaker(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test update_history with deal breaker conditions."""

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
        _LOGGER.debug("Testing update_history with no generation data")
        # Clear generation data to trigger the "No generation yet" deal breaker
        solcast.dampening.data_generation[GENERATION] = []
        await solcast.dampening.update_history()
        assert "Auto-dampening suppressed: No generation yet" in caplog.text

    finally:
        entity_history["days_generation"] = 3
        entity_history["days_suppression"] = 3
        entity_history["offset"] = -1
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

        day_start = solcast.dt_helper.day_start_utc() - timedelta(days=1)
        ts = day_start
        generation_dampening = defaultdict(dict, {ts: {GENERATION: 1.0, EXPORT_LIMITING: False}})

        factors_a = [1.0] * 48
        factors_b = [1.0] * 48
        factors_a[0] = 0.8
        factors_b[0] = 0.6

        solcast.dampening.auto_factors_history = {
            0: {
                VALUE_ADAPTIVE_DAMPENING_NO_DELTA: [
                    {"period_start": day_start, "factors": factors_a},
                    {"period_start": day_start, "factors": factors_b},
                ]
            },
            1: {
                VALUE_ADAPTIVE_DAMPENING_NO_DELTA: [
                    {"period_start": day_start, "factors": factors_b},
                    {"period_start": day_start, "factors": factors_a},
                ]
            },
        }

        selected_interval, avg_gen, avg_factor, variance = solcast.dampening._select_comparison_interval(generation_dampening, 1)

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

        day_start = solcast.dt_helper.day_start_utc() - timedelta(days=1)
        generation_dampening = defaultdict(dict, {day_start: {GENERATION: 1.0, EXPORT_LIMITING: False}})

        factors = [1.0] * 48
        factors[0] = 0.9

        solcast.dampening.auto_factors_history = {0: {VALUE_ADAPTIVE_DAMPENING_NO_DELTA: [{"period_start": day_start, "factors": factors}]}}

        selected_interval, avg_gen, avg_factor, variance = solcast.dampening._select_comparison_interval(generation_dampening, 1)

        assert selected_interval == 0
        assert avg_gen > 0
        assert avg_factor < 1.0
        assert variance == 0.0
    finally:
        assert await async_cleanup_integration_tests(hass)


async def test_select_comparison_interval_diluted_variance(
    recorder_mock: Recorder,
    hass: HomeAssistant,
) -> None:
    """Test that variance is computed over active-only (factor < 1.0) entries.

    When many overcast/undampened days (factor=1.0) exist alongside a handful of
    dampened days, including those 1.0 entries in the variance calculation inflates N
    and pulls the mean toward 1.0, making genuine model disagreement look negligible.
    The fix computes variance only over active entries so the inter-model signal is
    preserved, and the returned variance should match the active-only computation.
    """

    assert await async_cleanup_integration_tests(hass)

    try:
        entry = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT2))
        solcast = entry.runtime_data.coordinator.solcast

        day_start = solcast.dt_helper.day_start_utc() - timedelta(days=1)
        generation_dampening = defaultdict(dict, {day_start: {GENERATION: 1.0, EXPORT_LIMITING: False}})

        # Build 10-entry histories where interval 0 has 8 undampened days (1.0) and
        # one dampened day per model with strongly differing values (0.9 vs 0.5).
        # Including the eight 1.0s in the variance formula would dilute the signal;
        # active-only variance over [0.9, 0.5] should be 0.04.
        factors_a = [1.0] * 48
        factors_b = [1.0] * 48
        factors_a[0] = 0.9
        factors_b[0] = 0.5

        undampened_entry = {"period_start": day_start, "factors": [1.0] * 48}
        history_a = [undampened_entry] * 8 + [{"period_start": day_start, "factors": factors_a}]
        history_b = [undampened_entry] * 8 + [{"period_start": day_start, "factors": factors_b}]

        solcast.dampening.auto_factors_history = {
            0: {VALUE_ADAPTIVE_DAMPENING_NO_DELTA: history_a},
            1: {VALUE_ADAPTIVE_DAMPENING_NO_DELTA: history_b},
        }

        selected_interval, _, avg_factor, variance = solcast.dampening._select_comparison_interval(generation_dampening, 1)

        # Interval 0 should still be selected — it is the only interval with dampening
        assert selected_interval == 0
        assert avg_factor < 1.0
        # Variance must equal the active-only value: variance([0.9, 0.5]) == 0.04
        assert abs(variance - 0.04) < 1e-9
    finally:
        assert await async_cleanup_integration_tests(hass)


async def test_select_comparison_interval_current_factors_fallback(
    recorder_mock: Recorder,
    hass: HomeAssistant,
) -> None:
    """Test that the current-factors fallback selects by max dampening, not by generation.

    When all history entries have factor=1.0 (e.g. a fresh install or a long
    overcast streak), both the primary formula and the breadth-based fallback
    score every interval as zero. The current-factors fallback must then select
    by the heaviest dampening in factors[ALL], filtered to intervals with at least
    10% of peak generation.

    Critically, this must NOT be weighted by generation. A generation-weighted
    formula (normalized_gen × (1 − factor)) biases toward the peak-energy interval
    even when it has weak dampening, producing a poor comparison discriminator.
    The correct choice is the interval where the model applies the most aggressive
    dampening among those with adequate daylight generation.
    """

    assert await async_cleanup_integration_tests(hass)

    try:
        entry = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT2))
        solcast = entry.runtime_data.coordinator.solcast

        day_start = solcast.dt_helper.day_start_utc() - timedelta(days=1)

        # Interval 15 has modest generation (2 kWh) but heavy dampening (factor 0.55).
        # Interval 21 has much more generation (8 kWh) but weaker dampening (factor 0.80).
        ts_15 = day_start + timedelta(minutes=15 * 30)
        ts_21 = day_start + timedelta(minutes=21 * 30)
        generation_dampening = defaultdict(
            dict,
            {
                ts_15: {GENERATION: 2.0, EXPORT_LIMITING: False},
                ts_21: {GENERATION: 8.0, EXPORT_LIMITING: False},
            },
        )

        # History is entirely undampened — all factors 1.0 — so history-based scoring
        # produces zero for every interval.
        solcast.dampening.auto_factors_history = {
            0: {VALUE_ADAPTIVE_DAMPENING_NO_DELTA: [{"period_start": day_start, "factors": [1.0] * 48}]},
        }

        # The running model applies heavy dampening at interval 15 (factor 0.55)
        # and moderate dampening at interval 21 (factor 0.80).
        current_factors = [1.0] * 48
        current_factors[15] = 0.55  # 45% dampening — heavier discriminator
        current_factors[21] = 0.80  # 20% dampening — weaker discriminator
        solcast.dampening.factors = {ALL: current_factors}

        selected_interval, _avg_gen, avg_factor, _variance = solcast.dampening._select_comparison_interval(generation_dampening, 1)

        # Interval 15 must win: (1 − 0.55) = 0.45 > (1 − 0.80) = 0.20.
        # A generation-weighted formula would pick interval 21:
        #   21: (8/8 = 1.0) × 0.20 = 0.20 beats 15: (2/8 = 0.25) × 0.45 = 0.11
        # The correct approach ignores generation magnitude and selects maximum
        # dampening among intervals with adequate daylight production.
        assert selected_interval == 15
        assert avg_factor == 1.0  # history-based avg_factor — no active history entries
    finally:
        assert await async_cleanup_integration_tests(hass)


async def test_build_dampened_actuals_count_mismatch(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that _build_dampened_actuals_for_model returns None on day count mismatch.

    Sets up actuals covering two days but model history only covering one, so the
    post-loop check (len(dampened_actuals) != len(actuals)) fires and returns None.
    """
    assert await async_cleanup_integration_tests(hass)

    try:
        entry = await async_init_integration(hass, copy.deepcopy(DEFAULT_INPUT2))
        solcast = entry.runtime_data.coordinator.solcast

        day1 = solcast.dt_helper.day_start_utc() - timedelta(days=2)
        day2 = solcast.dt_helper.day_start_utc() - timedelta(days=1)

        factors = [1.0] * 48
        factors[0] = 0.9

        # Model history only has an entry for day1 — day2 exists in actuals but not history.
        solcast.dampening.auto_factors_history = {0: {0: [{"period_start": day1, "factors": factors}]}}

        # actuals covers two days; dampened_actuals will only build one day.
        actuals: defaultdict[dt, list[float]] = defaultdict(lambda: [0.0] * 48)
        actuals[day1] = [1.0] * 48
        actuals[day2] = [1.0] * 48

        included_intervals: defaultdict[dt, list[bool]] = defaultdict(lambda: [False] * 48)

        caplog.clear()
        result = solcast.dampening._build_dampened_actuals_for_model(0, 0, day1, actuals, included_intervals)

        assert result is None
        assert "mismatched actuals count" in caplog.text
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

        day_start = solcast.dt_helper.day_start_utc()
        peak_interval = 0
        dampened_actuals = defaultdict(lambda: [4.0] * 48)
        dampened_actuals[solcast.dt_helper.day_start(day_start)] = [4.0] * 48
        generation_dampening = defaultdict(dict, {day_start: {GENERATION: 1.0, EXPORT_LIMITING: False}})

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(solcast.dampening, "adjusted_interval_dt", lambda _ts: 0)

        has_inf, mean_ape, percentiles, _ = await solcast.dampening.calculate_single_interval_error(
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

        day_start = solcast.dt_helper.day_start_utc()
        dampened_actuals = defaultdict(lambda: [1.0] * 48)
        dampened_actuals[solcast.dt_helper.day_start(day_start)] = [1.0] * 48
        generation_dampening = defaultdict(dict, {day_start: {GENERATION: 0.0, EXPORT_LIMITING: False}})

        has_inf, mean_ape, percentiles, _ = await solcast.dampening.calculate_single_interval_error(
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

        day_start = solcast.dt_helper.day_start_utc()
        next_day = day_start + timedelta(days=1)

        generation_dampening = defaultdict(
            dict,
            {
                day_start: {GENERATION: 1.0, EXPORT_LIMITING: False},
                next_day: {GENERATION: 1.0, EXPORT_LIMITING: False},
            },
        )

        dampened_actuals = defaultdict(lambda: [1.0] * 48)
        day_start_key = solcast.dt_helper.day_start(day_start)
        dampened_actuals[day_start_key] = [1.0] * 48

        ignored_days = {day_start_key: True}

        monkeypatch.setattr(solcast.dampening, "adjusted_interval_dt", lambda _ts: 0)

        has_inf, mean_ape, percentiles, _ = await solcast.dampening.calculate_single_interval_error(
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


async def test_determine_best_settings_alternative_issue(
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

        day_start = solcast.dt_helper.day_start_utc() - timedelta(days=1)
        factors = [1.0] * 48
        factors[0] = 0.9
        history_entry = {"period_start": day_start, "factors": factors}

        min_model = ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_MODEL][MINIMUM]
        max_model = ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_MODEL][MAXIMUM]
        min_delta = ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL][MINIMUM_EXTENDED]
        max_delta = ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL][MAXIMUM]

        solcast.dampening.auto_factors_history = {
            model: {delta: [copy.deepcopy(history_entry)] for delta in range(min_delta, max_delta + 1)}
            for model in range(min_model, max_model + 1)
        }

        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_MINIMUM_HISTORY_DAYS] = 1
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_MINIMUM_ERROR_DELTA] = 1.0
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_NO_DELTA_ADJUSTMENT] = False
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL] = max_model
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL] = max_delta

        monkeypatch.setattr(solcast.dampening, "_find_earliest_common_history", lambda _days: day_start)
        monkeypatch.setattr(solcast.dampening, "_build_actuals_from_sites", lambda _start: {day_start: [1.0] * 48})

        async def _fake_prepare_generation_data(_earliest: dt):
            generation_dampening = defaultdict(dict)
            generation_dampening[day_start] = {GENERATION: 1.0, EXPORT_LIMITING: False}
            generation_dampening_day = defaultdict(float)
            generation_dampening_day[solcast.dt_helper.day_start(day_start)] = 1.0
            return generation_dampening, generation_dampening_day

        monkeypatch.setattr(solcast.dampening, "prepare_generation_data", _fake_prepare_generation_data)

        def _record_should_skip(model: int, delta: int, _min_days: int) -> tuple[bool, str]:
            solcast._test_current_model = model  # pyright: ignore[reportPrivateUsage]
            solcast._test_current_delta = delta  # pyright: ignore[reportPrivateUsage]
            return False, ""

        monkeypatch.setattr(solcast.dampening, "_should_skip_model_delta", _record_should_skip)

        alternate_better = True

        async def _fake_calculate_single_interval_error(*_args, **_kwargs):
            model = solcast._test_current_model  # pyright: ignore[reportPrivateUsage]
            delta = solcast._test_current_delta  # pyright: ignore[reportPrivateUsage]
            if delta == VALUE_ADAPTIVE_DAMPENING_NO_DELTA:
                error = 5.0 if alternate_better and model == min_model else 15.0
                return False, error, [error], {}
            return True, 10.0, [10.0], {}

        monkeypatch.setattr(solcast.dampening, "calculate_single_interval_error", _fake_calculate_single_interval_error)

        caplog.clear()
        await solcast.dampening.determine_best_settings()
        assert "but adaptive dampening found that model" in caplog.text

        alternate_better = False
        caplog.clear()
        await solcast.dampening.determine_best_settings()
        assert "but adaptive dampening found that model" not in caplog.text
    finally:
        assert await async_cleanup_integration_tests(hass)


async def test_determine_best_settings_insufficient_improvement(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test the APE-fallback path where improvement is below the minimum threshold.

    When calculate_single_interval_error returns no per-day breakdown (empty
    daily_errors), borda_scores is empty and _apply_best_settings falls back to
    comparing raw APE values.  This exercises the 'Insufficient improvement'
    log branch.
    """
    assert await async_cleanup_integration_tests(hass)

    try:
        options = copy.deepcopy(DEFAULT_INPUT2)
        options[GENERATION_ENTITIES] = ["sensor.solar_export_sensor_1111_1111_1111_1111"]
        entry = await async_init_integration(hass, options, extra_sensors=ExtraSensors.YES)
        solcast = entry.runtime_data.coordinator.solcast

        day_start = solcast.dt_helper.day_start_utc() - timedelta(days=1)
        factors = [1.0] * 48
        factors[0] = 0.9
        history_entry = {"period_start": day_start, "factors": factors}

        min_model = ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_MODEL][MINIMUM]
        max_model = ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_MODEL][MAXIMUM]
        min_delta = ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL][MINIMUM_EXTENDED]
        max_delta = ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL][MAXIMUM]

        solcast.dampening.auto_factors_history = {
            model: {delta: [copy.deepcopy(history_entry)] for delta in range(min_delta, max_delta + 1)}
            for model in range(min_model, max_model + 1)
        }

        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_MINIMUM_HISTORY_DAYS] = 1
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_NO_DELTA_ADJUSTMENT] = False
        # Current config: model 1, delta 0 (will be evaluated at 10% error)
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL] = min_model + 1
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL] = 0
        # High threshold so 5% improvement (10% -> 5%) is insufficient
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_MINIMUM_ERROR_DELTA] = 100.0

        monkeypatch.setattr(solcast.dampening, "_find_earliest_common_history", lambda _days: day_start)
        monkeypatch.setattr(solcast.dampening, "_build_actuals_from_sites", lambda _start: {day_start: [1.0] * 48})

        async def _fake_prepare_generation_data(_earliest: dt):
            generation_dampening: defaultdict = defaultdict(dict)
            generation_dampening[day_start] = {GENERATION: 1.0, EXPORT_LIMITING: False}
            generation_dampening_day: defaultdict = defaultdict(float)
            generation_dampening_day[solcast.dt_helper.day_start(day_start)] = 1.0
            return generation_dampening, generation_dampening_day

        monkeypatch.setattr(solcast.dampening, "prepare_generation_data", _fake_prepare_generation_data)

        def _record_should_skip(model: int, delta: int, _min_days: int) -> tuple[bool, str]:
            solcast._test_current_model = model  # pyright: ignore[reportPrivateUsage]
            solcast._test_current_delta = delta  # pyright: ignore[reportPrivateUsage]
            return False, ""

        monkeypatch.setattr(solcast.dampening, "_should_skip_model_delta", _record_should_skip)

        async def _fake_calculate_single_interval_error(*_args, **_kwargs):
            model = solcast._test_current_model  # pyright: ignore[reportPrivateUsage]
            _ = solcast._test_current_delta  # pyright: ignore[reportPrivateUsage]
            # Model min_model with any delta is best (5%); everything else is 10%.
            # Return empty daily_errors so borda_scores stays empty → APE fallback.
            error = 5.0 if model == min_model else 10.0
            return False, error, [], {}

        monkeypatch.setattr(solcast.dampening, "calculate_single_interval_error", _fake_calculate_single_interval_error)

        caplog.clear()
        await solcast.dampening.determine_best_settings()
        assert "Insufficient improvement" in caplog.text

        # Re-run with no-delta mode enabled: exercises lines 520-521, the APE fallback
        # inside the use_delta_mode=False branch of _apply_best_settings.
        caplog.clear()
        solcast.advanced_options[ADVANCED_AUTOMATED_DAMPENING_NO_DELTA_ADJUSTMENT] = True
        await solcast.dampening.determine_best_settings()
        assert "Insufficient improvement" in caplog.text
    finally:
        assert await async_cleanup_integration_tests(hass)
