"""Solcast automated dampening."""

# pylint: disable=pointless-string-statement

from __future__ import annotations

import asyncio
from collections import OrderedDict, defaultdict
import copy
from datetime import UTC, datetime as dt, timedelta
from itertools import pairwise
import json
import logging
import math
from operator import itemgetter
from pathlib import Path
import random
import time
from typing import TYPE_CHECKING, Any, Final, NamedTuple, cast

import aiofiles

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .const import (
    ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_APE_SELECTION,
    ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_APE_SHIT,
    ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_CONFIGURATION,
    ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_EXCLUDE,
    ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_MINIMUM_ERROR_DELTA,
    ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_MINIMUM_HISTORY_DAYS,
    ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL,
    ADVANCED_AUTOMATED_DAMPENING_GENERATION_HISTORY_LOAD_DAYS,
    ADVANCED_AUTOMATED_DAMPENING_IGNORE_INTERVALS,
    ADVANCED_AUTOMATED_DAMPENING_INSIGNIFICANT_FACTOR,
    ADVANCED_AUTOMATED_DAMPENING_INSIGNIFICANT_FACTOR_ADJUSTED,
    ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_GENERATION,
    ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_INTERVALS,
    ADVANCED_AUTOMATED_DAMPENING_MODEL,
    ADVANCED_AUTOMATED_DAMPENING_MODEL_DAYS,
    ADVANCED_AUTOMATED_DAMPENING_NO_DELTA_ADJUSTMENT,
    ADVANCED_AUTOMATED_DAMPENING_NO_LIMITING_CONSISTENCY,
    ADVANCED_AUTOMATED_DAMPENING_PRESERVE_UNMATCHED_FACTORS,
    ADVANCED_AUTOMATED_DAMPENING_SIMILAR_PEAK,
    ADVANCED_AUTOMATED_DAMPENING_SUPPRESSION_ENTITY,
    ADVANCED_ESTIMATED_ACTUALS_LOG_MAPE_BREAKDOWN,
    ADVANCED_GRANULAR_DAMPENING_DELTA_ADJUSTMENT,
    ADVANCED_HISTORY_MAX_DAYS,
    ADVANCED_OPTIONS,
    ALL,
    AMENDABLE,
    DEFAULT_DAMPENING_DELTA_ADJUSTMENT_MODEL,
    DOMAIN,
    DT_DATE_FORMAT,
    DT_DATE_FORMAT_SHORT,
    DT_DATE_FORMAT_UTC,
    DT_DATE_MONTH_DAY,
    DT_DATE_ONLY_FORMAT,
    DT_TIME_FORMAT_SHORT,
    ESTIMATE,
    ESTIMATE10,
    ESTIMATE90,
    EXPORT_LIMITING,
    FORECASTS,
    GENERATION,
    GENERATION_VERSION,
    LAST_UPDATED,
    MAXIMUM,
    MINIMUM,
    MINIMUM_EXTENDED,
    PERIOD_START,
    PLATFORM_BINARY_SENSOR,
    PLATFORM_SENSOR,
    PLATFORM_SWITCH,
    RESOURCE_ID,
    SITE,
    SITE_DAMP,
    SITE_INFO,
    VALUE_ADAPTIVE_DAMPENING_CONFIG_UNCHANGED,
    VALUE_ADAPTIVE_DAMPENING_NO_DELTA,
    VERSION,
)
from .util import (
    JSONDecoder,
    NoIndentEncoder,
    diff,
    forecast_entry_update,
    interquartile_bounds,
    ordinal,
    percentile,
)

if TYPE_CHECKING:
    from .solcastapi import SolcastApi

GRANULAR_DAMPENING_OFF: Final[bool] = False
GRANULAR_DAMPENING_ON: Final[bool] = True
SET_ALLOW_RESET: Final[bool] = True


_LOGGER = logging.getLogger(__name__)


class _ModelEvalResult(NamedTuple):
    """Result of evaluating all model/delta combinations for adaptive dampening."""

    daily_model_errors: dict[dt, dict[tuple[int, int], float]]
    daily_ranks: dict[dt, dict[tuple[int, int], int]]
    borda_scores: dict[tuple[int, int], float]
    best_ape_adjusted: float
    best_ape_no_delta: float
    best_model_adjusted: int
    best_model_no_delta: int
    best_delta_adjusted: int
    extant_ape: float


class Dampening:
    """Manages all dampening-related operations for Solcast forecasts."""

    def __init__(self, api: SolcastApi) -> None:
        """Initialise the dampening manager.

        Arguments:
            api: The parent SolcastApi instance.
        """
        self.api = api
        self.auto_factors: dict[dt, float] = {}
        self.auto_factors_history: dict[int, dict[int, list[dict[str, Any]]]] = {}
        self.data_generation: dict[str, list[dict[str, Any]] | Any] = {
            LAST_UPDATED: dt.fromtimestamp(0, UTC),
            GENERATION: [],
            VERSION: GENERATION_VERSION,
        }
        self.filename_generation = api.filename_generation
        self.granular_allow_reset = True
        self.factors: dict[str, list[float]] = {}
        self.factors_mtime: float = 0

    def allow_granular_reset(self) -> bool:
        """Allow options change to reset the granular dampening file to an empty dictionary."""
        return self.granular_allow_reset

    def get_filename(self) -> str:
        """Return the dampening configuration filename."""
        return self.api.filename_dampening

    def set_allow_granular_reset(self, enable: bool) -> None:
        """Set/clear allow reset granular dampening file to an empty dictionary by options change."""
        self.granular_allow_reset = enable

    def adjusted_interval_dt(self, interval: dt) -> int:
        """Adjust a datetime as standard time."""
        offset = 1 if self.api.dt_helper.dst(interval.astimezone(self.api.tz)) else 0
        return (
            ((interval.astimezone(self.api.tz).hour - offset) * 2 + interval.astimezone(self.api.tz).minute // 30)
            if interval.astimezone(self.api.tz).hour - offset >= 0
            else 0
        )

    async def apply_forward(self, applicable_sites: list[str] | None = None, do_past_hours: int = 0) -> None:
        """Apply dampening to forward forecasts."""
        if len(self.api.data_undampened[SITE_INFO]) > 0:
            _LOGGER.debug("Applying future dampening")

            self.auto_factors = {
                period_start: factor
                for period_start, factor in self.auto_factors.items()
                if period_start >= self.api.dt_helper.day_start_utc()
            }

            undampened_interval_pv50: dict[dt, float] = {}
            for site in self.api.sites:
                if site[RESOURCE_ID] in self.api.options.exclude_sites:
                    continue
                for forecast in self.api.data_undampened[SITE_INFO][site[RESOURCE_ID]][FORECASTS]:
                    period_start = forecast[PERIOD_START]
                    if period_start >= self.api.dt_helper.day_start_utc():
                        if period_start not in undampened_interval_pv50:
                            undampened_interval_pv50[period_start] = forecast[ESTIMATE] * 0.5
                        else:
                            undampened_interval_pv50[period_start] += forecast[ESTIMATE] * 0.5

            record_adjustment = True
            for site in self.api.sites:
                # Load all forecasts.
                forecasts_undampened_future = [
                    forecast
                    for forecast in self.api.data_undampened[SITE_INFO][site[RESOURCE_ID]][FORECASTS]
                    if forecast[PERIOD_START]
                    >= (
                        self.api.dt_helper.day_start_utc()
                        if self.api.data[SITE_INFO].get(site[RESOURCE_ID])
                        else self.api.dt_helper.day_start_utc() - timedelta(hours=do_past_hours)
                    )  # Was >= dt.now(UTC)
                ]
                forecasts = (
                    {forecast[PERIOD_START]: forecast for forecast in self.api.data[SITE_INFO][site[RESOURCE_ID]][FORECASTS]}
                    if self.api.data[SITE_INFO].get(site[RESOURCE_ID])
                    else {}
                )

                if site[RESOURCE_ID] not in self.api.options.exclude_sites and (
                    (site[RESOURCE_ID] in applicable_sites) if applicable_sites else True
                ):
                    # Apply dampening to forward data
                    for forecast in sorted(forecasts_undampened_future, key=itemgetter(PERIOD_START)):
                        period_start = forecast[PERIOD_START]
                        pv = round(forecast[ESTIMATE], 4)
                        pv10 = round(forecast[ESTIMATE10], 4)
                        pv90 = round(forecast[ESTIMATE90], 4)

                        # Retrieve the dampening factor for the period, and dampen the estimates.
                        dampening_factor = self.get_factor(
                            site[RESOURCE_ID],
                            period_start.astimezone(self.api.tz),
                            undampened_interval_pv50.get(period_start, -1),
                            record_adjustment=record_adjustment,
                        )
                        if record_adjustment:
                            self.auto_factors[period_start] = dampening_factor
                        pv_dampened = round(pv * dampening_factor, 4)
                        pv10_dampened = round(pv10 * dampening_factor, 4)
                        pv90_dampened = round(pv90 * dampening_factor, 4)

                        # Add or update the new entries.
                        forecast_entry_update(forecasts, period_start, pv_dampened, pv10_dampened, pv90_dampened)
                    record_adjustment = False
                else:
                    for forecast in sorted(forecasts_undampened_future, key=itemgetter(PERIOD_START)):
                        period_start = forecast[PERIOD_START]
                        forecast_entry_update(
                            forecasts,
                            period_start,
                            round(forecast[ESTIMATE], 4),
                            round(forecast[ESTIMATE10], 4),
                            round(forecast[ESTIMATE90], 4),
                        )

                await self.api.sort_and_prune(
                    site[RESOURCE_ID], self.api.data, self.api.advanced_options[ADVANCED_HISTORY_MAX_DAYS], forecasts
                )

    async def apply_yesterday(self) -> None:
        """Apply dampening to yesterday's estimated actuals."""
        undampened_interval_pv50: dict[dt, float] = {}
        for site in self.api.sites:
            if site[RESOURCE_ID] in self.api.options.exclude_sites:
                continue
            for forecast in self.api.data_actuals[SITE_INFO][site[RESOURCE_ID]][FORECASTS]:
                period_start = forecast[PERIOD_START]
                if period_start >= self.api.dt_helper.day_start_utc(future=-1) and period_start < self.api.dt_helper.day_start_utc():
                    if period_start not in undampened_interval_pv50:
                        undampened_interval_pv50[period_start] = forecast[ESTIMATE] * 0.5
                    else:
                        undampened_interval_pv50[period_start] += forecast[ESTIMATE] * 0.5

        for site in self.api.sites:
            if site[RESOURCE_ID] not in self.api.options.exclude_sites:
                _LOGGER.debug("Apply dampening to previous day estimated actuals for %s", site[RESOURCE_ID])
                # Load the undampened estimated actual day yesterday.
                actuals_undampened_day = [
                    actual
                    for actual in self.api.data_actuals[SITE_INFO][site[RESOURCE_ID]][FORECASTS]
                    if actual[PERIOD_START] >= self.api.dt_helper.day_start_utc(future=-1)
                    and actual[PERIOD_START] < self.api.dt_helper.day_start_utc()
                ]
                extant_actuals = (
                    {actual[PERIOD_START]: actual for actual in self.api.data_actuals_dampened[SITE_INFO][site[RESOURCE_ID]][FORECASTS]}
                    if self.api.data_actuals_dampened[SITE_INFO].get(site[RESOURCE_ID])
                    else {}
                )

                for actual in actuals_undampened_day:
                    period_start = actual[PERIOD_START]
                    undampened = actual[ESTIMATE]
                    factor = self.get_factor(
                        site[RESOURCE_ID], period_start.astimezone(self.api.tz), undampened_interval_pv50.get(period_start, -1.0)
                    )
                    dampened = round(undampened * factor, 4)
                    forecast_entry_update(
                        extant_actuals,
                        period_start,
                        dampened,
                    )

                await self.api.sort_and_prune(
                    site[RESOURCE_ID],
                    self.api.data_actuals_dampened,
                    self.api.advanced_options[ADVANCED_HISTORY_MAX_DAYS],
                    extant_actuals,
                )

    async def calculate_error(
        self,
        generation_day: defaultdict[dt, float],
        generation: defaultdict[dt, dict[str, Any]],
        values: tuple[dict[str, Any], ...],
        percentiles: tuple[int, ...] = (50,),
        log_breakdown: bool = False,
        ignored_days: dict[dt, bool] | None = None,
    ) -> tuple[bool, float, list[float]]:
        """Calculate mean and percentile absolute percentage error."""
        value_day: defaultdict[dt, float] = defaultdict(float)
        error: defaultdict[dt, float] = defaultdict(float)
        last_day: dt | None = None

        for interval in values:
            i = interval[PERIOD_START].astimezone(self.api.options.tz).replace(hour=0, minute=0, second=0, microsecond=0)
            if i != last_day:
                value_day[i] = 0.0
                last_day = i
            if generation.get(interval[PERIOD_START]) is not None and not generation[interval[PERIOD_START]][EXPORT_LIMITING]:
                value_day[i] += interval[ESTIMATE] / 2  # 30 minute intervals

        for day, value in value_day.items():
            if (ignored_days is not None and ignored_days.get(day, False)) or generation_day[day] <= 0:
                error[day] = math.inf
            else:
                error[day] = abs(generation_day[day] - value) / generation_day[day] * 100.0

            if log_breakdown:
                _LOGGER.debug(
                    "APE calculation for day %s, Actual %.2f kWh, Estimate %.2f kWh, Error %.2f%s",
                    day.strftime(DT_DATE_ONLY_FORMAT),
                    generation_day[day],
                    value,
                    error[day],
                    "%" if error[day] != math.inf else "",
                )

        non_inf_error: dict[dt, float] = {k: v for k, v in error.items() if v != math.inf}
        return (
            (
                (len(error) != len(non_inf_error)),
                sum(non_inf_error.values()) / len(non_inf_error),
                [percentile(sorted(error.values()), p) for p in percentiles],
            )
            if len(non_inf_error) > 0
            else (False, math.inf, [math.inf] * len(percentiles))
        )

    async def calculate_single_interval_error(
        self,
        dampened_actuals: defaultdict[dt, list[float]],
        generation_dampening: defaultdict[dt, dict[str, Any]],
        peak_interval: int,
        percentiles: tuple[int, ...] = (50,),
        log_breakdown: bool = False,
        ignored_days: dict[dt, bool] | None = None,
    ) -> tuple[bool, float, list[float], dict[dt, float]]:
        """Calculate error for a single common peak interval across all days.

        Compares actual generation vs dampened estimated actual for one specific interval
        (e.g., 12:00-12:30) across all available days. This prevents compensating errors
        and focuses model selection on performance at the most critical time of day.

        Only compares timestamps that actually exist in generation_dampening (i.e., not
        filtered out due to export limiting or other exclusions).

        Returns:
            Tuple of (has_inf, mean_ape, percentile_list, daily_errors)
        """
        interval_errors: list[float] = []
        daily_errors: dict[dt, float] = {}

        # Iterate through actual generation timestamps that exist (not filtered out)
        for timestamp, gen_data in generation_dampening.items():
            # Calculate which interval this timestamp represents
            interval_idx = self.adjusted_interval_dt(timestamp)

            if interval_idx != peak_interval:
                continue
            day_start = self.api.dt_helper.day_start(timestamp)
            if ignored_days is not None and ignored_days.get(day_start, False):
                continue
            if day_start not in dampened_actuals:
                continue

            actual_gen = gen_data[GENERATION]
            dampened_estimate = dampened_actuals[day_start][peak_interval] * 0.5  # Convert to 30-min kWh

            if actual_gen > 0:
                interval_ape = abs(actual_gen - dampened_estimate) / actual_gen * 100.0
                interval_errors.append(interval_ape)
            else:
                interval_ape = math.inf

            daily_errors[day_start] = interval_ape

            if log_breakdown:
                _LOGGER.debug(
                    "Single interval APE for day %s, Actual %.2f kWh, Estimate %.2f kWh, Error %.2f%s",
                    day_start.astimezone(self.api.options.tz).strftime(DT_DATE_ONLY_FORMAT),
                    actual_gen,
                    dampened_estimate,
                    interval_ape,
                    "%" if interval_ape != math.inf else "",
                )

        if len(interval_errors) == 0:
            return (False, math.inf, [math.inf] * len(percentiles), daily_errors)

        return (
            False,  # No inf values since we filtered them out
            sum(interval_errors) / len(interval_errors),
            [percentile(sorted(interval_errors), p) for p in percentiles],
            daily_errors,
        )

    async def determine_best_settings(self) -> None:
        """Determine which dampening settings result in the lowest error rate.

        Finds earliest common history start date for all models with > minimum dampening history.
        Builds actuals for dates since that earliest start date, then applies dampening history
        for all model/delta combinations to those actuals & calculates error rate.  Selects settings
        with lowest error rate and serialises to solcast-advanced.json.
        """

        _LOGGER.debug("Determining best automated dampening settings")
        start_time = time.time()

        if not self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_APE_SHIT]:
            use_error = self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_APE_SELECTION]
        else:
            use_error = random.randint(-1, 100)
            _LOGGER.debug("Adaptive dampening selection going ape shit with USE_ERROR=%d", use_error)

        min_history_days = self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_MINIMUM_HISTORY_DAYS]
        earliest_common = self._find_earliest_common_history(min_history_days)

        if earliest_common is None or earliest_common > self.api.dt_helper.day_start_utc() - timedelta(days=min_history_days):
            _LOGGER.info("Insufficient continuous dampening history to determine best automated dampening settings")
            return

        _LOGGER.debug(
            "Earliest date with complete dampening history is %s, delta is %d days",
            earliest_common.astimezone(self.api.tz).strftime(DT_DATE_ONLY_FORMAT),
            (self.api.dt_helper.day_start_utc() - earliest_common).days,
        )

        actuals = self._build_actuals_from_sites(earliest_common)
        generation_dampening, _ = await self.prepare_generation_data(earliest_common)

        common_peak_interval, avg_gen, avg_factor, variance = self._select_comparison_interval(generation_dampening, min_history_days)
        _LOGGER.debug(
            "Selected interval %d (%02d:%02d) for adaptive comparison: %.3f kWh, factor %.3f, variance %.4f",
            common_peak_interval,
            common_peak_interval // 2,
            (common_peak_interval % 2) * 30,
            avg_gen,
            avg_factor,
            variance,
        )

        included_intervals = self._build_included_intervals()
        result = await self._evaluate_model_combinations(
            earliest_common, actuals, generation_dampening, included_intervals, common_peak_interval, use_error
        )

        self._log_model_rankings(result)
        await self._apply_best_settings(result, common_peak_interval, use_error)

        _LOGGER.debug("Task dampening determine_best_settings took %.3f seconds", time.time() - start_time)

    async def _apply_best_settings(
        self,
        result: _ModelEvalResult,
        common_peak_interval: int,
        use_error: int,
    ) -> None:
        """Log and conditionally serialise the best adaptive dampening configuration."""
        if result.borda_scores:
            metric_desc = "Borda score"
            metric_suffix = ""
        else:
            metric_desc = "single interval MAPE" if use_error == -1 else f"single interval {ordinal(use_error)} percentile APE"
            metric_suffix = "%"
        min_error_delta = self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_MINIMUM_ERROR_DELTA]
        use_delta_mode = not self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_NO_DELTA_ADJUSTMENT]

        if use_delta_mode:
            selected_model = result.best_model_adjusted
            selected_delta: int | None = result.best_delta_adjusted
            current_valid = {selected_model, selected_delta} != {VALUE_ADAPTIVE_DAMPENING_CONFIG_UNCHANGED}
            is_different = {selected_model, selected_delta} != {
                self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL],
                self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL],
            }
            alternative_model = result.best_model_no_delta
            if result.borda_scores:
                selected_md = (result.best_model_adjusted, result.best_delta_adjusted)
                alternate_md = (result.best_model_no_delta, VALUE_ADAPTIVE_DAMPENING_NO_DELTA)
                selected_error = result.borda_scores[selected_md]
                alternative_error = result.borda_scores[alternate_md]
            else:
                selected_error = result.best_ape_adjusted
                alternative_error = result.best_ape_no_delta
        else:
            selected_model = result.best_model_no_delta
            selected_delta = None
            current_valid = selected_model != VALUE_ADAPTIVE_DAMPENING_CONFIG_UNCHANGED
            is_different = (
                selected_model != self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL]
                or self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL] != VALUE_ADAPTIVE_DAMPENING_NO_DELTA
            )
            alternative_model = result.best_model_adjusted
            if result.borda_scores:
                selected_md = (result.best_model_no_delta, VALUE_ADAPTIVE_DAMPENING_NO_DELTA)
                alternate_md = (result.best_model_adjusted, result.best_delta_adjusted)
                selected_error = result.borda_scores[selected_md]
                alternative_error = result.borda_scores[alternate_md]
            else:
                selected_error = result.best_ape_no_delta
                alternative_error = result.best_ape_adjusted

        if not current_valid:
            _LOGGER.info("Could not determine best automated dampening settings - values unmodified")
        else:
            _LOGGER.info(
                "Best automated dampening settings: model %d%s with %s of %.3f%s (interval %d: %02d:%02d)",
                selected_model,
                f" and delta {selected_delta}" if use_delta_mode else "",
                metric_desc,
                selected_error,
                metric_suffix,
                common_peak_interval,
                common_peak_interval // 2,
                (common_peak_interval % 2) * 30,
            )
            improvement = result.extant_ape - selected_error
            if is_different and (result.borda_scores or improvement > min_error_delta):
                _LOGGER.info(
                    "Updating automated dampening settings based on %s",
                    metric_desc if result.borda_scores else f"{improvement:.3f}% improvement over current settings",
                )
                self.api.advanced_options.update(
                    {
                        ADVANCED_AUTOMATED_DAMPENING_MODEL: selected_model,
                        ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL: selected_delta
                        if use_delta_mode
                        else DEFAULT_DAMPENING_DELTA_ADJUSTMENT_MODEL,
                    }
                )
                await self._serialise_advanced_options()
            elif is_different:
                _LOGGER.info(
                    "Insufficient improvement of %.3f%%%s over current model %d%s %s of %.3f%%, not updating settings",
                    improvement,
                    f" (minimum {min_error_delta:.3f}%)" if min_error_delta != 0.0 else "",
                    self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL],
                    f" delta {self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL]}" if use_delta_mode else "",
                    metric_desc,
                    result.extant_ape,
                )
            else:
                _LOGGER.info("Adaptive dampening configuration unchanged")

        if alternative_model != VALUE_ADAPTIVE_DAMPENING_CONFIG_UNCHANGED and alternative_error < selected_error:
            _LOGGER.info(
                "%s is set %s but adaptive dampening found that model %d%s had a lower %s of %.3f%s vs the selected %.3f%s",
                ADVANCED_AUTOMATED_DAMPENING_NO_DELTA_ADJUSTMENT,
                "false" if use_delta_mode else "true",
                alternative_model,
                " with no delta adjustment" if use_delta_mode else f" and delta {result.best_delta_adjusted}",
                metric_desc,
                alternative_error,
                metric_suffix,
                selected_error,
                metric_suffix,
            )

    def _build_dampened_actuals_for_model(
        self,
        model: int,
        delta: int,
        earliest_common: dt,
        actuals: defaultdict[dt, list[float]],
        included_intervals: defaultdict[dt, list[bool]],
    ) -> defaultdict[dt, list[float]] | None:
        """Build dampened actuals for a single model/delta combination.

        Applies the model's historical dampening factors to undampened actuals from the
        common start date forward. Returns None if any required actuals are missing or
        the resulting day count does not match the actuals input.
        """
        model_entries = self.auto_factors_history[model][delta]
        dampened_actuals: defaultdict[dt, list[float]] = defaultdict(lambda: [0.0] * 48)

        for model_entry in model_entries:
            period_start = model_entry["period_start"]
            if period_start < earliest_common:
                continue
            day_start = self.api.dt_helper.day_start(period_start)
            if day_start not in actuals:
                _LOGGER.debug(
                    "Model %d and delta %d skipped due to missing actuals for dampening history entry %s",
                    model,
                    delta,
                    day_start.strftime(DT_DATE_FORMAT),
                )
                return None
            factors = model_entry["factors"]
            dampened_actuals[day_start] = [actuals[day_start][i] * factors[i] for i in range(48)]

        if len(dampened_actuals) != len(actuals):
            _LOGGER.debug(
                "Model %d and delta %d produced mismatched actuals count (%d dampened vs %d actuals)",
                model,
                delta,
                sum(len(v) for v in dampened_actuals.values()),
                sum(len(v) for v in actuals.values()),
            )
            return None

        return dampened_actuals

    def _build_included_intervals(self) -> defaultdict[dt, list[bool]]:
        """Build a per-day map of which intervals had dampening applied by any model.

        Uses the delta=-1 (no-delta) history entries as the reference for whether
        dampening was active at each interval on each day.
        """
        return defaultdict(
            lambda: [False] * 48,
            {
                day_start: [
                    any(
                        entry["factors"][i] != 1.0
                        for deltas in self.auto_factors_history.values()
                        for entry in deltas[-1]
                        if self.api.dt_helper.day_start(entry["period_start"]) == day_start
                    )
                    for i in range(48)
                ]
                for day_start in {
                    self.api.dt_helper.day_start(entry["period_start"])
                    for deltas in self.auto_factors_history.values()
                    for entry in deltas[-1]
                }
            },
        )

    def _get_daily_ranks(self, daily_model_errors: dict[dt, dict[tuple[int, int], float]]) -> dict[dt, dict[tuple[int, int], int]]:
        """Helper to calculate error rankings for each day."""
        daily_ranks = {}
        for day, errors in daily_model_errors.items():
            sorted_items = sorted(errors.items(), key=lambda x: x[1])  # sort only on errors
            day_rank_map = {}
            current_rank = 1
            for i, (md, error) in enumerate(sorted_items):
                if i > 0 and error > sorted_items[i - 1][1]:
                    current_rank = i + 1  # new rank is current index+1
                day_rank_map[md] = current_rank
            daily_ranks[day] = day_rank_map
        return daily_ranks

    async def _evaluate_model_combinations(
        self,
        earliest_common: dt,
        actuals: defaultdict[dt, list[float]],
        generation_dampening: defaultdict[dt, dict[str, Any]],
        included_intervals: defaultdict[dt, list[bool]],
        common_peak_interval: int,
        use_error: int,
    ) -> _ModelEvalResult:
        """Evaluate all model/delta combinations and return the best-performing settings.

        For each model/delta combination applies its historical dampening factors to the
        undampened actuals, then computes single-interval error at the selected comparison
        interval. Returns per-day error rankings and the best model/delta for each mode.
        """
        ignored_days: dict[dt, bool] = {}
        daily_model_errors: dict[dt, dict[tuple[int, int], float]] = defaultdict(dict)
        model_metrics: dict[tuple[int, int], float] = {}
        best_ape_adjusted = math.inf
        best_ape_no_delta = math.inf
        best_model_adjusted = VALUE_ADAPTIVE_DAMPENING_CONFIG_UNCHANGED
        best_model_no_delta = VALUE_ADAPTIVE_DAMPENING_CONFIG_UNCHANGED
        best_delta_adjusted = VALUE_ADAPTIVE_DAMPENING_CONFIG_UNCHANGED
        extant_ape = math.inf

        for model in range(
            ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_MODEL][MINIMUM],
            ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_MODEL][MAXIMUM] + 1,
        ):
            for delta in range(
                ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL][MINIMUM_EXTENDED],
                ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL][MAXIMUM] + 1,
            ):
                should_skip, reason = self._should_skip_model_delta(
                    model, delta, self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_MINIMUM_HISTORY_DAYS]
                )
                if should_skip:
                    _LOGGER.debug("Skipping model %d and delta %d as %s", model, delta, reason)
                    continue

                await asyncio.sleep(0)  # Be nice to HA
                _LOGGER.debug("Evaluating model %d and delta %d", model, delta)

                dampened_actuals = self._build_dampened_actuals_for_model(model, delta, earliest_common, actuals, included_intervals)
                if dampened_actuals is None:
                    _LOGGER.debug("Skipping APE calculation for model %d and delta %d", model, delta)
                    continue

                _, error_single_interval, percentiles_single, daily_errors = await self.calculate_single_interval_error(
                    dampened_actuals,
                    generation_dampening,
                    common_peak_interval,
                    percentiles=(use_error,) if use_error != -1 else (),
                    log_breakdown=self.api.advanced_options[ADVANCED_ESTIMATED_ACTUALS_LOG_MAPE_BREAKDOWN],
                    ignored_days=ignored_days,
                )

                for day, error in daily_errors.items():
                    daily_model_errors[day][(model, delta)] = error

                error_metric = percentiles_single[0] if percentiles_single else error_single_interval
                if (
                    model == self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL]
                    and delta == self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL]
                ):
                    extant_ape = error_metric

                model_metrics[(model, delta)] = error_metric

                if error_metric == math.inf:
                    _LOGGER.debug("Skipping APE calculation for model %d and delta %d due to APE calculation issue", model, delta)
                    continue

                _LOGGER.debug(
                    "Model %d and delta %d achieved single-interval %s of %.3f%%%s",
                    model,
                    delta,
                    "MAPE" if use_error == -1 else f"{ordinal(use_error)} percentile APE",
                    error_metric,
                    f" (MAPE {error_single_interval:.2f}%)" if use_error != -1 else "",
                )

        daily_ranks = self._get_daily_ranks(daily_model_errors)

        # Compute Borda scores (mean rank position, lower = better).
        rank_sums: defaultdict[tuple[int, int], float] = defaultdict(float)
        rank_counts: defaultdict[tuple[int, int], int] = defaultdict(int)

        for ranks in daily_ranks.values():
            for md, rank in ranks.items():
                rank_sums[md] += rank
                rank_counts[md] += 1

        borda_scores = {md: rank_sums[md] / rank_counts[md] for md in rank_sums}

        if borda_scores:
            # Select Borda winners.
            no_delta_candidates = {md: s for md, s in borda_scores.items() if md[1] == VALUE_ADAPTIVE_DAMPENING_NO_DELTA}
            adjusted_candidates = {md: s for md, s in borda_scores.items() if md[1] != VALUE_ADAPTIVE_DAMPENING_NO_DELTA}

            if no_delta_candidates:
                best_nd = min(no_delta_candidates, key=lambda md: (no_delta_candidates[md], md[0]))
                best_model_no_delta = best_nd[0]
                best_ape_no_delta = model_metrics.get(best_nd, math.inf)

            if adjusted_candidates:
                best_adj = min(
                    adjusted_candidates,
                    key=lambda md: (adjusted_candidates[md], md[0], md[1] if md[1] >= 0 else float("inf")),
                )
                best_model_adjusted = best_adj[0]
                best_delta_adjusted = best_adj[1]
                best_ape_adjusted = model_metrics.get(best_adj, math.inf)
        else:
            # No per-day breakdown available (e.g. single-interval only); fall back to minimum APE
            _LOGGER.debug("No per-day errors collected; using APE-based model selection")
            valid_metrics = [(md, m) for md, m in model_metrics.items() if m != math.inf]
            no_delta_sorted = sorted(
                ((md, m) for md, m in valid_metrics if md[1] == VALUE_ADAPTIVE_DAMPENING_NO_DELTA),
                key=lambda item: (item[1], item[0][0]),
            )
            adjusted_sorted = sorted(
                ((md, m) for md, m in valid_metrics if md[1] != VALUE_ADAPTIVE_DAMPENING_NO_DELTA),
                key=lambda item: (item[1], item[0][0], item[0][1] if item[0][1] >= 0 else float("inf")),
            )
            if no_delta_sorted:
                best_nd, best_ape_no_delta = no_delta_sorted[0]
                best_model_no_delta = best_nd[0]
            if adjusted_sorted:
                best_adj, best_ape_adjusted = adjusted_sorted[0]
                best_model_adjusted = best_adj[0]
                best_delta_adjusted = best_adj[1]

        return _ModelEvalResult(
            daily_model_errors=daily_model_errors,
            daily_ranks=daily_ranks,
            borda_scores=borda_scores,
            best_ape_adjusted=best_ape_adjusted,
            best_ape_no_delta=best_ape_no_delta,
            best_model_adjusted=best_model_adjusted,
            best_model_no_delta=best_model_no_delta,
            best_delta_adjusted=best_delta_adjusted,
            extant_ape=extant_ape,
        )

    def _log_model_rankings(self, result: _ModelEvalResult) -> None:
        """Log a per-day rank distribution table for all evaluated model/delta combinations."""
        model_rank_frequencies: defaultdict[tuple[int, int], defaultdict[int, int]] = defaultdict(lambda: defaultdict(int))
        model_chronological_logs: defaultdict[tuple[int, int], list[str]] = defaultdict(list)
        max_rank_observed = 0

        daily_model_errors = result.daily_model_errors
        daily_ranks = result.daily_ranks
        borda_scores = result.borda_scores

        for day, ranks in daily_ranks.items():
            for md, rank in ranks.items():
                model_rank_frequencies[md][rank] += 1
                max_rank_observed = max(max_rank_observed, rank)
                err = daily_model_errors[day][md]
                model_chronological_logs[md].append(f"{err:.2f}% ({ordinal(rank)})")

        if not model_rank_frequencies:
            _LOGGER.debug("No ranking data available (insufficient history or all-infinity errors)")
            return

        # Use existing borda_scores for sorting
        sorted_by_borda = sorted(
            borda_scores.keys(),
            key=lambda md: (borda_scores[md], md[0], md[1] if md[1] >= 0 else float("inf")),
        )

        model_rank_profiles = {md: [freqs[r] for r in range(1, max_rank_observed + 1)] for md, freqs in model_rank_frequencies.items()}

        _LOGGER.debug("Ranking:")
        for i, md in enumerate(sorted_by_borda, 1):
            _LOGGER.debug(
                "  #%d: Model %d Delta %d : Borda %.3f : Distribution: [%s]",
                i,
                md[0],
                md[1],
                borda_scores[md],
                ", ".join([f"{ordinal(r)}:{count}" for r, count in enumerate(model_rank_profiles[md], 1)]),
            )
            _LOGGER.debug("      History: [%s]", ", ".join(model_chronological_logs[md]))

        no_delta_winner = next((md for md in sorted_by_borda if md[1] == VALUE_ADAPTIVE_DAMPENING_NO_DELTA), None)
        adjusted_winner = next((md for md in sorted_by_borda if md[1] != VALUE_ADAPTIVE_DAMPENING_NO_DELTA), None)
        if no_delta_winner:
            _LOGGER.info("Ranking winner (no delta): Model %d (Borda %.3f)", no_delta_winner[0], borda_scores[no_delta_winner])
        if adjusted_winner:
            _LOGGER.info(
                "Ranking winner (adjusted): Model %d Delta %d (Borda %.3f)",
                adjusted_winner[0],
                adjusted_winner[1],
                borda_scores[adjusted_winner],
            )

    async def get(self, site: str | None, site_underscores: bool) -> list[dict[str, Any]]:
        """Retrieve the currently set dampening factors.

        Arguments:
            site (str): An optional site.
            site_underscores (bool): Whether to replace dashes with underscores in returned site names.

        Returns:
            (list[dict[str, Any]]): The action response for the presently set dampening factors.
        """
        if self.api.entry_options.get(SITE_DAMP):
            if not site:
                sites = [_site[RESOURCE_ID] for _site in self.api.sites]
            else:
                sites = [site]
            all_set = self.factors.get(ALL) is not None
            if site:
                if not all_set:
                    if site in self.factors:
                        return [
                            {
                                SITE: _site if not site_underscores else _site.replace("-", "_"),
                                "damp_factor": ",".join(str(factor) for factor in self.factors[_site]),
                            }
                            for _site in sites
                            if self.factors.get(_site)
                        ]
                    raise ServiceValidationError(
                        translation_domain=DOMAIN,
                        translation_key="damp_not_for_site",
                        translation_placeholders={SITE: site},
                    )
                if site != ALL:
                    if site in self.factors:
                        _LOGGER.warning(
                            "There is dampening for site %s, but it is being overridden by an all sites entry, returning the 'all' entries instead",
                            site,
                        )
                    else:
                        _LOGGER.warning(
                            "There is no dampening set for site %s, but it is being overridden by an all sites entry, returning the 'all' entries instead",
                            site,
                        )
                return [
                    {
                        SITE: ALL,
                        "damp_factor": ",".join(str(factor) for factor in self.factors[ALL]),
                    }
                ]
            if all_set:
                return [
                    {
                        SITE: ALL,
                        "damp_factor": ",".join(str(factor) for factor in self.factors[ALL]),
                    }
                ]
            return [
                {
                    SITE: _site if not site_underscores else _site.replace("-", "_"),
                    "damp_factor": ",".join(str(factor) for factor in self.factors[_site]),
                }
                for _site in sites
                if self.factors.get(_site)
            ]
        if not site or site == ALL:
            return [
                {
                    SITE: ALL,
                    "damp_factor": ",".join(str(factor) for _, factor in self.api.damp.items()),
                }
            ]
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="damp_use_all",
            translation_placeholders={SITE: site},
        )

    def get_earliest_estimate_after_dampened(self, after: dt) -> dt | None:
        """Get the earliest contiguous dampened estimated actual datetime.

        Returns:
            dt | None: The earliest dampened estimated actual datetime, or None if no data.
        """
        return self._get_earliest_estimate_after(self.api.data_estimated_actuals_dampened, after=after, dampened=True)

    def get_earliest_estimate_after_undampened(self, after: dt) -> dt | None:
        """Get the earliest contiguous undampened estimated actual datetime.

        Returns:
            dt | None: The earliest undampened estimated actual datetime, or None if no data.
        """
        return self._get_earliest_estimate_after(self.api.data_estimated_actuals, after=after)

    def get_factor(self, site: str | None, period_start: dt, interval_pv50: float, record_adjustment: bool = False) -> float:
        """Retrieve either a traditional or granular dampening factor."""
        if site is not None:
            if self.api.entry_options.get(SITE_DAMP):
                if self.factors.get(ALL):
                    return self._get_granular_factor(ALL, period_start, interval_pv50, record_adjustment=record_adjustment)
                if self.factors.get(site):
                    return self._get_granular_factor(site, period_start)
                return 1.0
        return self.api.damp.get(f"{period_start.hour}", 1.0)

    async def get_pv_generation(self) -> None:  # noqa: C901
        """Get PV generation from external entity/entities.

        Supports two entity types:
        - Energy entities (Wh/kWh/MWh, total increasing): Computes energy deltas and distributes across intervals.
        - Power entities (W/kW/MW, instantaneous): Computes time-weighted average power per interval, then converts to kWh.

        The entities must have state history. Very large units are not supported (e.g. GWh, TWh) because of precision loss.
        """

        start_time = time.time()

        _ON = ("on", "1", "true", "True")
        _ALL = ("on", "off", "1", "0", "true", "false", "True", "False")

        # Load the generation history.
        generation: dict[dt, dict[str, Any]] = {generated[PERIOD_START]: generated for generated in self.data_generation[GENERATION]}
        days = 1 if len(generation) > 0 else self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_GENERATION_HISTORY_LOAD_DAYS]

        entity_registry = er.async_get(self.api.hass)

        for day in range(days):
            # PV generation
            generation_intervals: dict[dt, float] = {
                self.api.dt_helper.day_start_utc(future=(-1 * day)) - timedelta(days=1) + timedelta(minutes=minute): 0
                for minute in range(0, 1440, 30)
            }
            for entity in self.api.options.generation_entities:
                r_entity = entity_registry.async_get(entity)
                if r_entity is None:
                    _LOGGER.error("Generation entity %s is not a valid entity", entity)
                    continue
                if r_entity.disabled_by is not None:
                    _LOGGER.error("Generation entity %s is disabled, please enable it", entity)
                    continue
                entity_history = await get_instance(self.api.hass).async_add_executor_job(
                    state_changes_during_period,
                    self.api.hass,
                    self.api.dt_helper.day_start_utc(future=(-1 * day)) - timedelta(days=1),
                    self.api.dt_helper.day_start_utc(future=(-1 * day)),
                    entity,
                )
                if entity_history.get(entity) and len(entity_history[entity]) > 4:
                    _LOGGER.debug("Retrieved day %d PV generation data from entity: %s", -1 + day * -1, entity)

                    if self._is_power_entity(entity):
                        # Power entity: compute time-weighted average kW per interval, then convert to kWh (* 0.5).
                        conversion_factor = self._get_conversion_factor(entity, entity_history[entity], is_power=True)

                        # Build list of (timestamp, power_kW) from state history.
                        power_readings: list[tuple[dt, float]] = [
                            (e.last_updated.astimezone(UTC), float(e.state) * conversion_factor)
                            for e in entity_history[entity]
                            if e.state.replace(".", "").isnumeric()
                        ]

                        if len(power_readings) > 1:
                            period_start = self.api.dt_helper.day_start_utc(future=(-1 * day)) - timedelta(days=1)

                            # For each 30-min interval, compute the time-weighted average power.
                            for interval_start in generation_intervals:
                                interval_end = interval_start + timedelta(minutes=30)
                                weighted_sum = 0.0
                                total_weight = 0.0

                                for i, (reading_time, power_kw) in enumerate(power_readings):
                                    # Determine the time span this reading represents.
                                    # It holds until the next reading (or interval end).
                                    if i + 1 < len(power_readings):
                                        next_time = power_readings[i + 1][0]
                                    else:
                                        next_time = interval_end

                                    # Clip to interval boundaries.
                                    seg_start = max(reading_time, interval_start)
                                    seg_end = min(next_time, interval_end)

                                    if seg_start < seg_end:
                                        duration = (seg_end - seg_start).total_seconds()
                                        weighted_sum += power_kw * duration
                                        total_weight += duration

                                if total_weight > 0:
                                    avg_power_kw = weighted_sum / total_weight
                                    # Convert average kW over 30 minutes to kWh.
                                    generation_intervals[interval_start] += avg_power_kw * 0.5
                        else:
                            _LOGGER.debug("Insufficient power readings for entity: %s", entity)

                    else:
                        # Energy entity: compute deltas and distribute across intervals.
                        conversion_factor = self._get_conversion_factor(entity, entity_history[entity])
                        # Arrange the generation samples into half-hour intervals.
                        sample_time: list[dt] = [
                            e.last_updated.astimezone(UTC).replace(
                                minute=e.last_updated.astimezone(UTC).minute // 30 * 30, second=0, microsecond=0
                            )
                            for e in entity_history[entity]
                            if e.state.replace(".", "").isnumeric()
                        ]
                        # Build a list of generation delta values.
                        sample_generation: list[float] = [
                            0.0,
                            *diff(
                                [float(e.state) * conversion_factor for e in entity_history[entity] if e.state.replace(".", "").isnumeric()]
                            ),
                        ]
                        sample_generation_time: list[dt] = [
                            e.last_updated.astimezone(UTC) for e in entity_history[entity] if e.state.replace(".", "").isnumeric()
                        ]
                        sample_timedelta: list[int] = [
                            0,
                            *diff(
                                [
                                    (
                                        e.last_updated.astimezone(UTC)
                                        - (self.api.dt_helper.day_start_utc(future=(-1 * day)) - timedelta(days=1))
                                    ).total_seconds()
                                    for e in entity_history[entity]
                                    if e.state.replace(".", "").isnumeric()
                                ]
                            ),
                        ]

                        period_start = self.api.dt_helper.day_start_utc(future=(-1 * day)) - timedelta(days=1)
                        period_end = self.api.dt_helper.day_start_utc(future=(-1 * day))
                        if sample_generation_time and sample_generation_time[0] == period_start:
                            sample_generation[0] = 0.0
                            sample_timedelta[0] = 0

                        # Detemine generation-consistent or time-consistent increments, and the inter-quartile upper bound for ignoring excessive jumps.
                        uniform_increment = False
                        non_zero_samples = sorted([round(sample, 5) for sample in sample_generation if sample > 0.0003])
                        if percentile(non_zero_samples, 25) == percentile(non_zero_samples, 75):
                            uniform_increment = True
                        else:
                            non_zero_samples = sorted([sample for sample in sample_timedelta if sample > 0])
                        _, upper = interquartile_bounds(non_zero_samples, factor=(1.5 if uniform_increment else 2.2))
                        upper += 0.1 if uniform_increment else 1
                        time_delta_samples = [sample for sample in sample_timedelta if sample > 0]
                        if time_delta_samples:
                            _, time_upper = interquartile_bounds(time_delta_samples, factor=2.2)
                            time_upper += 1
                        else:
                            time_upper = 0
                        _LOGGER.debug(
                            f"%s increments detected for entity: %s, outlier upper bound: {'%.3f kWh' if uniform_increment else '%d seconds'}",  # noqa: G004
                            "Generation-consistent" if uniform_increment else "Time-consistent",
                            entity,
                            upper,
                        )

                        # Build generation values for each interval, ignoring any excessive jumps.
                        # Track previous sample time for proportional distribution
                        ignored: dict[dt, bool] = {}
                        last_interval: dt | None = None
                        prev_report_time: dt | None = None

                        if (
                            len(sample_time) == len(sample_generation)
                            and len(sample_time) == len(sample_generation_time)
                            and len(sample_time) == len(sample_timedelta)
                        ):
                            for idx, (interval, kWh, report_time, time_delta) in enumerate(
                                zip(sample_time, sample_generation, sample_generation_time, sample_timedelta, strict=True)
                            ):
                                # Check for excessive jumps
                                is_excessive = False
                                if interval != last_interval:
                                    last_interval = interval
                                    if uniform_increment:
                                        if round(kWh, 4) > upper:
                                            is_excessive = True
                                            ignored[interval] = True
                                    elif time_delta > upper and kWh > 0.0003:
                                        if kWh > 0.14:
                                            is_excessive = True
                                            ignored[interval] = True
                                    if is_excessive:
                                        # Invalidate both this interval and the previous one
                                        ignored[interval - timedelta(minutes=30)] = True
                                        _LOGGER.debug(
                                            "Ignoring excessive PV generation jump of %.3f kWh, time delta %d seconds, at %s from entity: %s; Invalidating intervals %s and %s",
                                            kWh,
                                            time_delta,
                                            report_time.astimezone(self.api.tz).strftime(DT_DATE_FORMAT),
                                            entity,
                                            (interval - timedelta(minutes=30)).astimezone(self.api.tz).strftime(DT_TIME_FORMAT_SHORT),
                                            interval.astimezone(self.api.tz).strftime(DT_TIME_FORMAT_SHORT),
                                        )

                                if not is_excessive and idx > 0 and prev_report_time is not None:
                                    # Distribute energy delta proportionally across interval boundaries
                                    delta_start = prev_report_time
                                    delta_end = report_time

                                    # Get interval boundaries that might be crossed
                                    current_interval_start = interval
                                    prev_interval_start = delta_start.replace(minute=delta_start.minute // 30 * 30, second=0, microsecond=0)

                                    if prev_report_time == period_start:
                                        generation_intervals[current_interval_start] += kWh
                                        prev_report_time = report_time
                                        continue

                                    if report_time == period_end:
                                        if prev_interval_start in generation_intervals:
                                            generation_intervals[prev_interval_start] += kWh
                                        prev_report_time = report_time
                                        continue

                                    if time_upper and time_delta > time_upper and kWh > 0.0003:
                                        generation_intervals[current_interval_start] += kWh
                                    elif prev_interval_start == current_interval_start:
                                        # Delta entirely within one interval
                                        generation_intervals[interval] += kWh
                                    else:
                                        # Delta spans multiple intervals - distribute proportionally
                                        total_seconds = (delta_end - delta_start).total_seconds()
                                        if total_seconds > 0:
                                            # Calculate time in each interval
                                            intervals_crossed = []
                                            temp_interval = prev_interval_start
                                            while temp_interval <= current_interval_start:
                                                interval_end = temp_interval + timedelta(minutes=30)
                                                overlap_start = max(delta_start, temp_interval)
                                                overlap_end = min(delta_end, interval_end)
                                                if overlap_start < overlap_end:
                                                    overlap_seconds = (overlap_end - overlap_start).total_seconds()
                                                    proportion = overlap_seconds / total_seconds
                                                    intervals_crossed.append((temp_interval, proportion))
                                                temp_interval = interval_end

                                            # Distribute energy proportionally
                                            for crossed_interval, proportion in intervals_crossed:
                                                if crossed_interval in generation_intervals:
                                                    generation_intervals[crossed_interval] += kWh * proportion
                                elif not is_excessive and idx == 0:
                                    # First sample - assign to its interval
                                    generation_intervals[interval] += kWh

                                prev_report_time = report_time

                            for interval in ignored:
                                generation_intervals[interval] = 0.0
                else:
                    _LOGGER.debug(
                        "No day %d PV generation data (or barely any) from entity: %s (%s)",
                        -1 + day * -1,
                        entity,
                        entity_history.get(entity),
                    )
            for i, gen in generation_intervals.items():
                generation_intervals[i] = round(gen, 3)

            export_limiting: dict[dt, bool] = {
                self.api.dt_helper.day_start_utc(future=(-1 * day)) - timedelta(days=1) + timedelta(minutes=minute): False
                for minute in range(0, 1440, 30)
            }

            # Identify intervals intentionally disabled by the user.
            platforms = [PLATFORM_BINARY_SENSOR, PLATFORM_SENSOR, PLATFORM_SWITCH]
            find_entity = self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_SUPPRESSION_ENTITY]
            entity = ""
            found = False
            for p in platforms:
                entity = f"{p}.{find_entity}"
                r_entity = entity_registry.async_get(entity)
                if r_entity is not None and r_entity.disabled_by is None:
                    found = True
                    break
            if found:
                _LOGGER.debug("Suppression entity %s exists", entity)
                query_start_time = self.api.dt_helper.day_start_utc(future=(-1 * day)) - timedelta(days=1)
                query_end_time = self.api.dt_helper.day_start_utc(future=(-1 * day))

                # Get state changes during the period
                entity_history = await get_instance(self.api.hass).async_add_executor_job(
                    state_changes_during_period,
                    self.api.hass,
                    query_start_time,
                    query_end_time,
                    entity,
                    True,  # No attributes
                    False,  # Descending order
                    None,  # Limit
                    True,  # Include start time state
                )

                if entity_history.get(entity) and len(entity_history[entity]):
                    entity_state: dict[dt, bool] = {}
                    state = False

                    for e in entity_history[entity]:
                        if e.state not in _ALL:
                            continue
                        interval = e.last_updated.astimezone(UTC).replace(
                            minute=e.last_updated.astimezone(UTC).minute // 30 * 30, second=0, microsecond=0
                        )
                        if e.state in _ON:
                            state = True
                            if not entity_state.get(interval):
                                entity_state[interval] = state
                                if state and entity_state.get(interval + timedelta(minutes=30)) is not None:
                                    entity_state.pop(interval + timedelta(minutes=30))
                            _LOGGER.debug(
                                "Interval %s state change %s at %s",
                                interval.astimezone(self.api.tz).strftime(DT_DATE_FORMAT_SHORT),
                                entity_state[interval],
                                e.last_updated.astimezone(self.api.tz).strftime(DT_DATE_FORMAT_SHORT),
                            )
                        elif state:
                            state = False
                            entity_state[interval + timedelta(minutes=30)] = False
                            _LOGGER.debug(
                                "Interval %s state change %s at %s",
                                (interval + timedelta(minutes=30)).astimezone(self.api.tz).strftime(DT_DATE_FORMAT_SHORT),
                                entity_state[interval + timedelta(minutes=30)],
                                e.last_updated.astimezone(self.api.tz).strftime(DT_DATE_FORMAT_SHORT),
                            )
                    state = False
                    for interval in export_limiting:
                        if entity_state.get(interval) is not None:
                            state = entity_state[interval]
                        export_limiting[interval] = state
                        if state:
                            _LOGGER.debug(
                                "Auto-dampen suppressed for interval %s", interval.astimezone(self.api.tz).strftime(DT_DATE_FORMAT_SHORT)
                            )

            # Detect site export limiting
            if self.api.options.site_export_limit > 0 and self.api.options.site_export_entity != "":
                _INTERVAL = 5  # The time window in minutes to detect export limiting

                entity = self.api.options.site_export_entity
                r_entity = entity_registry.async_get(entity)
                if r_entity is None:
                    _LOGGER.error("Site export entity %s is not a valid entity", entity)
                    entity = ""
                elif r_entity.disabled_by is not None:
                    _LOGGER.error("Site export entity %s is disabled, please enable it", entity)
                    entity = ""
                export_intervals: dict[dt, float] = {
                    self.api.dt_helper.day_start_utc(future=(-1 * day)) - timedelta(days=1) + timedelta(minutes=minute): 0
                    for minute in range(0, 1440, _INTERVAL)
                }
                if entity:
                    entity_history = await get_instance(self.api.hass).async_add_executor_job(
                        state_changes_during_period,
                        self.api.hass,
                        self.api.dt_helper.day_start_utc(future=(-1 * day)) - timedelta(days=1),
                        self.api.dt_helper.day_start_utc(future=(-1 * day)),
                        entity,
                    )
                    if entity_history.get(entity) and len(entity_history[entity]):
                        # Get the conversion factor for the entity to convert to kWh.
                        conversion_factor = self._get_conversion_factor(entity, entity_history[entity])
                        # Arrange the site export samples into intervals.
                        sample_time: list[dt] = [
                            e.last_updated.astimezone(UTC).replace(
                                minute=e.last_updated.astimezone(UTC).minute // _INTERVAL * _INTERVAL, second=0, microsecond=0
                            )
                            for e in entity_history[entity]
                            if e.state.replace(".", "").isnumeric()
                        ]
                        # Build a list of export delta values.
                        sample_export: list[float] = [
                            0.0,
                            *diff(
                                [float(e.state) * conversion_factor for e in entity_history[entity] if e.state.replace(".", "").isnumeric()]
                            ),
                        ]
                        for interval, kWh in zip(sample_time, sample_export, strict=True):
                            export_intervals[interval] += kWh
                        # Convert to export per interval in kW.
                        for i, export in export_intervals.items():
                            export_intervals[i] = round(export * (60 / _INTERVAL), 3)

                        for i, export in export_intervals.items():
                            export_interval = i.replace(minute=i.minute // 30 * 30)
                            if export >= self.api.options.site_export_limit:
                                export_limiting[export_interval] = True
                    else:
                        _LOGGER.debug("No site export history found for %s", entity)

            # Add recent generation intervals to the history.
            generation.update(
                {
                    i: {PERIOD_START: i, GENERATION: generated, EXPORT_LIMITING: export_limiting[i]}
                    for i, generated in generation_intervals.items()
                }
            )

        # Trim, sort and serialise.
        self.data_generation = {
            LAST_UPDATED: dt.now(UTC).replace(microsecond=0),
            GENERATION: sorted(
                filter(
                    lambda generated: generated[PERIOD_START] >= self.api.dt_helper.day_start_utc(future=-22),
                    generation.values(),
                ),
                key=itemgetter(PERIOD_START),
            ),
        }
        await self.api.serialise_data(self.data_generation, self.filename_generation)
        _LOGGER.debug("Task get_pv_generation took %.3f seconds", time.time() - start_time)

    async def granular_data(self) -> bool:
        """Read the current granular dampening file.

        Returns:
            bool: Granular dampening in use.
        """

        def option(enable: bool, set_allow_reset: bool = False):
            site_damp = self.api.entry_options.get(SITE_DAMP, False) if self.api.entry_options.get(SITE_DAMP) is not None else False
            if enable ^ site_damp:
                options = {**self.api.entry_options}
                options[SITE_DAMP] = enable
                self.api.entry_options[SITE_DAMP] = enable
                if set_allow_reset:
                    self.granular_allow_reset = enable
                if self.api.entry is not None:
                    self.api.hass.config_entries.async_update_entry(self.api.entry, options=options)
            return enable

        error = False
        return_value = False
        mtime = True
        filename = self.get_filename()
        try:
            if not Path(filename).is_file():
                self.factors = {}
                self.factors_mtime = 0
                mtime = False
                return option(GRANULAR_DAMPENING_OFF)
            async with aiofiles.open(filename) as file:
                content = await file.read()
                try:
                    response_json = json.loads(content)
                except json.decoder.JSONDecodeError:
                    _LOGGER.error("JSONDecodeError, dampening ignored: %s", filename)
                    error = True
                    return option(GRANULAR_DAMPENING_OFF, SET_ALLOW_RESET)
                self.factors = cast(dict[str, Any], response_json)
                if content.replace("\n", "").replace("\r", "").strip() != "" and isinstance(response_json, dict) and self.factors:
                    first_site_len = 0
                    for site, damp_list in self.factors.items():
                        if first_site_len == 0:
                            first_site_len = len(damp_list)
                        elif len(damp_list) != first_site_len:
                            _LOGGER.error(
                                "Number of dampening factors for all sites must be the same in %s, dampening ignored",
                                filename,
                            )
                            self.factors = {}
                            error = True
                        if len(damp_list) not in (24, 48):
                            _LOGGER.error(
                                "Number of dampening factors for site %s must be 24 or 48 in %s, dampening ignored",
                                site,
                                filename,
                            )
                            self.factors = {}
                            error = True
                    if error:
                        return_value = option(GRANULAR_DAMPENING_OFF, SET_ALLOW_RESET)
                    else:
                        _LOGGER.debug("Granular dampening %s", str(self.factors))
                        return_value = option(GRANULAR_DAMPENING_ON, SET_ALLOW_RESET)
            return return_value
        finally:
            if mtime:
                self.factors_mtime = Path(filename).stat().st_mtime if Path(filename).exists() else 0
            if error:
                self.factors = {}

    async def load_history(self) -> bool:
        """Load dampening history from JSON, validate, and repopulate."""

        start_time = time.time()
        _LOGGER.debug("Loading dampening history from file: %s", self.api.filename_dampening_history)

        valid = True
        loaded_count = 0

        expected_records = (
            (ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL][MAXIMUM] + 2)
            * (ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_MODEL][MAXIMUM] + 1)
            * self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL_DAYS]
        )

        # --- Initialise structure if needed ---
        if not self.auto_factors_history:
            self.auto_factors_history = {
                m: {
                    d: []
                    for d in range(
                        ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL][MINIMUM_EXTENDED],
                        ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL][MAXIMUM] + 1,
                    )
                }
                for m in range(
                    ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_MODEL][MINIMUM],
                    ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_MODEL][MAXIMUM] + 1,
                )
            }

        if Path(self.api.filename_dampening_history).is_file():
            async with aiofiles.open(self.api.filename_dampening_history) as file:
                try:
                    raw = json.loads(await file.read(), cls=JSONDecoder)
                except json.decoder.JSONDecodeError:
                    _LOGGER.warning("Dampening history file is corrupt - could not decode JSON - adaptive model configuration failed")
                    valid = False
        else:
            valid = False
            _LOGGER.warning("No dampening history file found - adaptive model configuration failed")

        if valid:
            # --- Parse and add history ---
            for model_str, deltas in raw.items():
                model = int(model_str)
                for delta_str, entries in deltas.items():
                    delta = int(delta_str)
                    for entry in entries:
                        await self._add_history(period_start=entry["period_start"], model=model, delta=delta, factors=entry["factors"])
                        loaded_count += 1

            msg = f"Load dampening history loaded {loaded_count} of a maximum of {expected_records} records"

            if loaded_count != expected_records:
                _LOGGER.warning(
                    "%s Automated dampening adaptive model configuration may be sub-optimal until maximum history of %d days is built",
                    msg,
                    self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL_DAYS],
                )
            else:
                _LOGGER.debug(msg)

        _LOGGER.debug("Task dampening load_history took %.3f seconds", time.time() - start_time)

        return valid

    async def load_generation_data(self) -> dict[str, Any] | None:
        """Load generation data from cache file.

        Returns:
            dict[str, Any] | None: The loaded generation data, or None if not found.
        """
        data = None
        if Path(self.filename_generation).is_file():
            async with aiofiles.open(self.filename_generation) as data_file:
                json_data: dict[str, Any] = json.loads(await data_file.read(), cls=JSONDecoder)
                # Note that the generation data cache does not have a version number
                # Future changes to the structure, if any, will need to be handled here by checking current version by allowing for None
                _LOGGER.debug(
                    "Data cache %s exists, file type is %s",
                    self.filename_generation,
                    type(json_data),
                )
                if isinstance(json_data, dict):
                    data = json_data
                    _LOGGER.debug("Generation data loaded")
        return data

    async def migrate_undampened_history(self) -> None:
        """Migrate un-dampened forecasts if un-dampened data for a site does not exist."""
        apply_dampening: list[str] = []
        forecasts: dict[str, dict[dt, Any]] = {}
        past_days = self.api.dt_helper.day_start_utc(future=-14)
        for site in self.api.sites:
            site = site[RESOURCE_ID]
            if not self.api.data_undampened[SITE_INFO].get(site) or len(self.api.data_undampened[SITE_INFO][site].get(FORECASTS, [])) == 0:
                _LOGGER.info(
                    "Migrating un-dampened history to %s for %s",
                    self.api.filename_undampened,
                    site,
                )
                apply_dampening.append(site)
            else:
                continue
            # Load the forecast history.
            forecasts[site] = {forecast[PERIOD_START]: forecast for forecast in self.api.data[SITE_INFO][site][FORECASTS]}
            forecasts_undampened: list[dict[str, Any]] = []
            # Migrate forecast history if un-dampened data does not yet exist.
            if len(forecasts[site]) > 0:
                forecasts_undampened = sorted(
                    {
                        forecast[PERIOD_START]: forecast
                        for forecast in self.api.data[SITE_INFO][site][FORECASTS]
                        if forecast[PERIOD_START] >= past_days
                    }.values(),
                    key=itemgetter(PERIOD_START),
                )
                _LOGGER.debug(
                    "Migrating %d forecast entries to un-dampened forecasts for site %s",
                    len(forecasts_undampened),
                    site,
                )
            self.api.data_undampened[SITE_INFO].update({site: {FORECASTS: copy.deepcopy(forecasts_undampened)}})

        if len(apply_dampening) > 0:
            self.api.data_undampened[LAST_UPDATED] = dt.now(UTC).replace(microsecond=0)
            await self.api.serialise_data(self.api.data_undampened, self.api.filename_undampened)

        if len(apply_dampening) > 0:
            await self.apply_forward(applicable_sites=apply_dampening)
            await self.api.serialise_data(self.api.data, self.api.filename)

    async def model_automated(self, force: bool = False) -> None:
        """Model the automated dampening of the forecast data.

        Look for consistently low PV generation in consistently high estimated actual intervals.
        Dampening factors are always referenced using standard time (not daylight savings time).
        """
        start_time = time.time()

        if not self.api.options.auto_dampen and not force:
            _LOGGER.debug("Automated dampening is not enabled, skipping dampening model_automated()")
            await self._prepare_data(only_peaks=True)
            return

        if await self._check_deal_breaker_automated():
            return

        actuals, ignored_intervals, generation, matching_intervals = await self._prepare_data()

        _LOGGER.debug("Modelling automated dampening factors")

        dampening = await self._calculate(
            matching_intervals, generation, actuals, ignored_intervals, self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL]
        )

        if dampening != self.factors.get(ALL):
            self.factors[ALL] = dampening
            await self.serialise_granular()
            await self.granular_data()
        _LOGGER.debug("Task dampening model_automated took %.3f seconds", time.time() - start_time)

    async def prepare_generation_data(self, earliest_start: dt) -> tuple[defaultdict[dt, dict[str, Any]], defaultdict[dt, float]]:
        """Prepare generation data for accuracy metrics calculation.

        ignore_unmatched excludes intervals below minimum peak in
        determine_best_settings.
        """
        ignored_intervals: list[int] = []  # Intervals to ignore in standard time

        for time_string in self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_IGNORE_INTERVALS]:
            hour, minute = map(int, time_string.split(":"))
            interval = hour * 2 + minute // 30
            ignored_intervals.append(interval)

        export_limited_intervals = dict.fromkeys(range(48), False)
        if not self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_NO_LIMITING_CONSISTENCY]:
            for gen in self.data_generation[GENERATION][-1 * self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL_DAYS] * 48 :]:
                if gen[EXPORT_LIMITING]:
                    export_limited_intervals[self._adjusted_interval(gen)] = True

        data_generation = copy.deepcopy(self.data_generation)
        generation_dampening: defaultdict[dt, dict[str, Any]] = defaultdict(dict[str, Any])
        generation_dampening_day: defaultdict[dt, float] = defaultdict(float)
        for record in data_generation.get(GENERATION, [])[-1 * self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL_DAYS] * 48 :]:
            if record[PERIOD_START] < earliest_start:
                continue

            interval = self.adjusted_interval_dt(record[PERIOD_START])
            if interval in ignored_intervals or export_limited_intervals[interval]:
                record[EXPORT_LIMITING] = True
                continue

            generation_dampening[record[PERIOD_START]] = {
                GENERATION: record[GENERATION],
                EXPORT_LIMITING: record[EXPORT_LIMITING],
            }
            if not record[EXPORT_LIMITING]:
                generation_dampening_day[
                    record[PERIOD_START].astimezone(self.api.options.tz).replace(hour=0, minute=0, second=0, microsecond=0)
                ] += record[GENERATION]

        return generation_dampening, generation_dampening_day

    async def refresh_granular_data(self) -> None:
        """Load granular dampening data if the file has changed."""
        if Path(self.get_filename()).is_file():
            mtime = Path(self.get_filename()).stat().st_mtime
            if mtime != self.factors_mtime:
                await self.granular_data()
                _LOGGER.info("Granular dampening loaded")
                _LOGGER.debug(
                    "Granular dampening file mtime %s",
                    dt.fromtimestamp(mtime, self.api.tz).strftime(DT_DATE_FORMAT),
                )

    async def serialise_granular(self) -> None:
        """Serialise the site dampening file."""
        filename = self.get_filename()
        _LOGGER.debug("Writing granular dampening to %s", filename)
        payload = json.dumps(
            self.factors,
            ensure_ascii=False,
            cls=NoIndentEncoder,
            indent=2,
        )
        async with self.api.serialise_lock, aiofiles.open(filename, "w") as file:
            await file.write(payload)
        self.factors_mtime = Path(filename).stat().st_mtime
        _LOGGER.debug(
            "Granular dampening file mtime %s",
            dt.fromtimestamp(self.factors_mtime, self.api.tz).strftime(DT_DATE_FORMAT),
        )

    async def update_history(self) -> None:
        """Generate history of dampening factors for all models."""

        if self.api.options.auto_dampen and self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_CONFIGURATION]:
            start_time = time.time()
            _LOGGER.debug("Updating automated dampening adaptation history")

            if await self._check_deal_breaker_automated():
                return

            actuals, ignored_intervals, generation, matching_intervals = await self._prepare_data()

            # Build undampened pv50 estimates for the previous day

            undampened_interval_pv50: dict[dt, float] = {}
            for site in self.api.sites:
                if site[RESOURCE_ID] in self.api.options.exclude_sites:
                    continue
                for forecast in self.api.data_undampened[SITE_INFO][site[RESOURCE_ID]][FORECASTS]:
                    period_start = forecast[PERIOD_START]
                    if period_start >= self.api.dt_helper.day_start_utc(future=-1) and period_start < self.api.dt_helper.day_start_utc():
                        if period_start not in undampened_interval_pv50:
                            undampened_interval_pv50[period_start] = forecast[ESTIMATE] * 0.5
                        else:
                            undampened_interval_pv50[period_start] += forecast[ESTIMATE] * 0.5

            for dampening_model in range(
                ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_MODEL][MINIMUM],
                ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_MODEL][MAXIMUM] + 1,
            ):
                dampening = await self._calculate(matching_intervals, generation, actuals, ignored_intervals, dampening_model, False)

                await self._add_history(  # Add entry for no delta adjustment
                    period_start=self.api.dt_helper.day_start_utc(future=-1),
                    model=dampening_model,
                    delta=VALUE_ADAPTIVE_DAMPENING_NO_DELTA,
                    factors=dampening,
                )

                _LOGGER.debug(
                    "Dampening factors on %s for model %d and delta adjustment %d: %s",
                    self.api.dt_helper.day_start_utc(future=-1).strftime(DT_DATE_FORMAT_UTC),
                    dampening_model,
                    VALUE_ADAPTIVE_DAMPENING_NO_DELTA,
                    ",".join(f"{factor:.3f}" for factor in dampening),
                )

                for delta_adjustment in range(
                    ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL][MINIMUM],
                    ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL][MAXIMUM] + 1,
                ):
                    adjusted_dampening = copy.deepcopy(dampening)
                    for period_start, period_value in undampened_interval_pv50.items():
                        interval = self.adjusted_interval_dt(period_start)
                        if self.api.peak_intervals[interval] > 0 and period_value > 0 and dampening[interval] < 1.0:
                            adjusted_dampening[interval] = self._apply_adjustment(
                                actuals[period_start], dampening[interval], interval, delta_adjustment
                            )  # Adjust based on actual vs peak rather than forecast vs peak
                            adjusted_dampening[interval] = (
                                1.0
                                if (
                                    self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_INSIGNIFICANT_FACTOR]
                                    <= adjusted_dampening[interval]
                                    < 1.0
                                )
                                else adjusted_dampening[interval]
                            )

                    await self._add_history(
                        period_start=self.api.dt_helper.day_start_utc(future=-1),  # Adding history for the previous day
                        model=dampening_model,
                        delta=delta_adjustment,
                        factors=adjusted_dampening,
                    )
                    _LOGGER.debug(
                        "Dampening factors on %s for model %d and delta adjustment %d: %s",
                        self.api.dt_helper.day_start_utc(future=-1).strftime(DT_DATE_FORMAT_UTC),
                        dampening_model,
                        delta_adjustment,
                        ",".join(f"{factor:.3f}" for factor in adjusted_dampening),
                    )

            # Trim, sort and serialise.

            cutoff = self.api.dt_helper.day_start_utc(future=-self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL_DAYS])

            serialisable = {}

            for model, deltas in self.auto_factors_history.items():
                serialisable[model] = {}

                for delta, entries in deltas.items():
                    # Filter entries newer than cutoff
                    recent_entries = [entry for entry in entries if entry["period_start"] >= cutoff]

                    # Sort by period_start
                    recent_entries.sort(key=lambda e: e["period_start"])

                    # Update in-memory structure
                    self.auto_factors_history[model][delta] = recent_entries

                    # Build serialisable version
                    serialisable[model][delta] = [
                        {"period_start": entry["period_start"], "factors": entry["factors"]} for entry in recent_entries
                    ]

            payload = json.dumps(serialisable, ensure_ascii=False, indent=2, cls=NoIndentEncoder, above_level=4)
            async with self.api.serialise_lock, aiofiles.open(self.api.filename_dampening_history, "w") as file:
                await file.write(payload)

            _LOGGER.debug("Task dampening update_history took %.3f seconds", time.time() - start_time)

    async def _add_history(self, period_start: dt, model: int, delta: int, factors: list[float]) -> None:
        """Adds a dampening history record to self.auto_factors_history."""

        # Update or add the entry
        entries = self.auto_factors_history[model][delta]
        new_entry = {"period_start": period_start, "factors": factors}

        # Try to update existing entry
        for i, entry in enumerate(entries):
            if entry["period_start"] == period_start:
                entries[i] = new_entry
                return

        # Add new entry if not found
        entries.append(new_entry)

    def _adjusted_interval(self, interval: dict[str, Any]) -> int:
        """Adjust a forecast/actual interval as standard time."""
        offset = 1 if self.api.dt_helper.is_interval_dst(interval) else 0
        return (
            (
                (interval[PERIOD_START].astimezone(self.api.tz).hour - offset) * 2
                + interval[PERIOD_START].astimezone(self.api.tz).minute // 30
            )
            if interval[PERIOD_START].astimezone(self.api.tz).hour - offset >= 0
            else 0
        )

    def _apply_adjustment(self, interval_pv50, factor, interval, delta_adjustment_model) -> float:
        """Applies selected delta_adjustment_model to past dampening factor."""
        match delta_adjustment_model:
            case 1:
                # Adjust the factor based on forecast vs. peak interval using squared ratio
                factor = max(factor, factor + ((1.0 - factor) * ((1.0 - (interval_pv50 / self.api.peak_intervals[interval])) ** 2)))
            case _:
                # Adjust the factor based on forecast vs. peak interval delta-logarithmically.
                factor = max(
                    factor,
                    min(
                        1.0,
                        factor + ((1.0 - factor) * (math.log(self.api.peak_intervals[interval]) - math.log(interval_pv50))),
                    ),
                )

        return round(factor, 3)

    def _build_actuals_from_sites(self, earliest_common: dt) -> defaultdict[dt, list[float]]:
        """Build actuals dictionary from site data.

        Args:
            earliest_common: Start date for collecting actuals data.

        Returns:
            Dictionary mapping day_start to 48 interval values.
        """
        _LOGGER.debug(
            "Getting undampened actuals from %s to %s",
            earliest_common.strftime(DT_DATE_FORMAT_UTC),
            self.api.dt_helper.day_start_utc().strftime(DT_DATE_FORMAT_UTC),
        )
        actuals: defaultdict[dt, list[float]] = defaultdict(lambda: [0.0] * 48)
        for site in self.api.sites:
            if site[RESOURCE_ID] in self.api.options.exclude_sites:
                _LOGGER.debug("Dampening history actuals suppressed site %s", site[RESOURCE_ID])
                continue

            start, end = self.api.get_list_slice(
                self.api.data_actuals[SITE_INFO][site[RESOURCE_ID]][FORECASTS],
                earliest_common,
                self.api.dt_helper.day_start_utc() - timedelta(minutes=30),
                search_past=True,
            )
            for actual in self.api.data_actuals[SITE_INFO][site[RESOURCE_ID]][FORECASTS][start:end]:
                ts: dt = actual[PERIOD_START].astimezone(self.api.tz)
                day_start = self.api.dt_helper.day_start(ts)

                if day_start not in actuals:
                    _LOGGER.debug("Adding actuals entry for %s", day_start.strftime(DT_DATE_ONLY_FORMAT))

                actuals[day_start][self.adjusted_interval_dt(ts)] += actual[ESTIMATE]

        return actuals

    def _get_conversion_factor(self, entity: str, entity_history: list[State] | None = None, is_power: bool = False) -> float:
        """Get the conversion factor for an entity to convert to kWh (energy) or kW (power)."""

        if is_power:
            unit_factors = {"mW": 1e-6, "W": 0.001, "kW": 1.0, "MW": 1000.0}
            default_unit = "kW"
        else:
            unit_factors = {"mWh": 1e-6, "Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}
            default_unit = "kWh"

        entity_unit = None

        if entity_history:
            latest_state = entity_history[-1]
            if hasattr(latest_state, "attributes") and latest_state.attributes:
                entity_unit = latest_state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)

        if not entity_unit:
            entity_registry = er.async_get(self.api.hass)
            entity_entry = entity_registry.async_get(entity)
            if entity_entry and entity_entry.unit_of_measurement:
                entity_unit = entity_entry.unit_of_measurement

        if not entity_unit:
            _LOGGER.warning("Entity %s has no %s, assuming %s", entity, ATTR_UNIT_OF_MEASUREMENT, default_unit)
            return 1.0

        conversion_factor = unit_factors.get(entity_unit)
        if conversion_factor is None:
            _LOGGER.error("Entity %s has an unsupported %s '%s', assuming %s", entity, ATTR_UNIT_OF_MEASUREMENT, entity_unit, default_unit)
            return 1.0

        if conversion_factor != 1.0:
            _LOGGER.debug("Entity %s uses %s, applying conversion factor %s", entity, entity_unit, conversion_factor)

        return conversion_factor

    def _is_power_entity(self, entity: str) -> bool:
        """Determine whether a generation entity is a power (W/kW) entity rather than energy (Wh/kWh)."""

        entity_registry = er.async_get(self.api.hass)
        r_entity = entity_registry.async_get(entity)
        if r_entity is not None:
            dc = r_entity.device_class or r_entity.original_device_class
            if dc == SensorDeviceClass.POWER:
                return True
        return False

    async def _calculate(  # noqa: C901
        self,
        matching_intervals: dict[int, list[dt]],
        generation: dict[dt, float],
        actuals: dict[dt, float],
        ignored_intervals: list[int],
        dampening_model: int,
        verbose_log: bool = True,
    ) -> list[float]:
        """Applies selected dampening_model to passed data to calculate list of dampening factors."""

        dampening = [1.0] * 48  # Initialize dampening factors

        # Check the generation for each interval and determine if it is consistently lower than the peak.
        for interval, matching in matching_intervals.items():
            # Get current factor if required
            if self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_PRESERVE_UNMATCHED_FACTORS]:
                prior_factor = self.factors[ALL][interval] if self.factors.get(ALL) is not None else 1.0

            dst_offset = (
                1
                if self.api.dt_helper.dst(
                    dt.now(self.api.tz).replace(hour=interval // 2, minute=30 * (interval % 2), second=0, microsecond=0)
                )
                else 0
            )
            interval_time = f"{interval // 2 + (dst_offset):02}:{30 * (interval % 2):02}"
            if interval in ignored_intervals:
                if verbose_log:
                    _LOGGER.debug("Interval %s is intentionally ignored, skipping", interval_time)
                continue
            generation_samples: list[float] = [
                round(generation.get(timestamp, 0.0), 3) for timestamp in matching if generation.get(timestamp, 0.0) != 0.0
            ]
            preserve_this_interval = False
            if len(matching) > 0:
                msg = ""
                log_msg = True
                if verbose_log:
                    _LOGGER.debug(
                        "Interval %s has peak estimated actual %.3f and %d matching intervals: %s",
                        interval_time,
                        self.api.peak_intervals[interval],
                        len(matching),
                        ", ".join([date.astimezone(self.api.tz).strftime(DT_DATE_MONTH_DAY) for date in matching]),
                    )
                match dampening_model:
                    case 1 | 2 | 3:
                        if len(matching) >= self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_INTERVALS]:
                            actual_samples: list[float] = [
                                actuals.get(timestamp, 0.0) for timestamp in matching if generation.get(timestamp, 0.0) != 0.0
                            ]
                            if verbose_log:
                                _LOGGER.debug(
                                    "Selected %d estimated actuals for %s: %s",
                                    len(actual_samples),
                                    interval_time,
                                    ", ".join(f"{act:.3f}" for act in actual_samples),
                                )
                                _LOGGER.debug(
                                    "Selected %d generation records for %s: %s",
                                    len(generation_samples),
                                    interval_time,
                                    generation_samples,
                                )
                            if (
                                len(generation_samples)
                                >= self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_GENERATION]
                            ):
                                if len(actual_samples) == len(generation_samples):
                                    raw_factors: list[float] = []
                                    for act, gen in zip(actual_samples, generation_samples, strict=True):
                                        raw_factors.append(min(gen / act, 1.0) if act > 0 else 1.0)
                                    if verbose_log:
                                        _LOGGER.debug(
                                            "Candidate factors for %s: %s",
                                            interval_time,
                                            ", ".join(f"{fact:.3f}" for fact in raw_factors),
                                        )
                                    match dampening_model:
                                        case 1:  # max factor from matched pairs
                                            factor = max(raw_factors)
                                        case 2:  # average factor from matched pairs
                                            factor = sum(raw_factors) / len(raw_factors)
                                        case 3:  # min factor from matched pairs
                                            factor = min(raw_factors)
                                    factor = round(factor, 3) if factor > 0 else 1.0
                                    if self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_INSIGNIFICANT_FACTOR] <= factor < 1.0:
                                        msg = f"Ignoring insignificant factor for {interval_time} of {factor:.3f}"
                                        factor = 1.0
                                    else:
                                        msg = f"Auto-dampen factor for {interval_time} is {factor:.3f}"
                                    dampening[interval] = factor
                                msg = (
                                    f"Mismatched sample lengths for {interval_time}: {len(actual_samples)} actuals vs {len(generation_samples)} generations"
                                    if len(actual_samples) != len(generation_samples)
                                    else msg
                                )
                            else:
                                msg = f"Not enough reliable generation samples for {interval_time} to determine dampening ({len(generation_samples)})"
                                preserve_this_interval = self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_PRESERVE_UNMATCHED_FACTORS]
                    case _:
                        peak = max(generation_samples) if len(generation_samples) > 0 else 0.0
                        if verbose_log:
                            _LOGGER.debug("Interval %s max generation: %.3f, %s", interval_time, peak, generation_samples)
                        if len(matching) >= self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_INTERVALS]:
                            if peak < self.api.peak_intervals[interval]:
                                if (
                                    len(generation_samples)
                                    >= self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_GENERATION]
                                ):
                                    factor = (peak / self.api.peak_intervals[interval]) if self.api.peak_intervals[interval] != 0 else 1.0
                                    if self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_INSIGNIFICANT_FACTOR] <= factor < 1.0:
                                        msg = f"Ignoring insignificant factor for {interval_time} of {factor:.3f}"
                                        factor = 1.0
                                    else:
                                        msg = f"Auto-dampen factor for {interval_time} is {factor:.3f}"
                                    dampening[interval] = round(factor, 3)
                                else:
                                    msg = f"Not enough reliable generation samples for {interval_time} to determine dampening ({len(generation_samples)})"
                                    preserve_this_interval = self.api.advanced_options[
                                        ADVANCED_AUTOMATED_DAMPENING_PRESERVE_UNMATCHED_FACTORS
                                    ]
                            else:
                                log_msg = False

                if not preserve_this_interval:
                    msg = (
                        f"Not enough matching intervals for {interval_time} to determine dampening"
                        if len(matching) < self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_INTERVALS]
                        else msg
                    )
                    preserve_this_interval = (
                        self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_PRESERVE_UNMATCHED_FACTORS]
                        and len(matching) < self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MINIMUM_MATCHING_INTERVALS]
                    )

                if preserve_this_interval:
                    dampening[interval] = prior_factor
                    msg = msg + f", preserving prior factor {prior_factor:.3f}" if prior_factor != 1.0 else msg

                if log_msg and msg != "" and verbose_log:
                    _LOGGER.debug(msg)

        return dampening

    async def _check_deal_breaker_automated(self) -> bool:
        """Check for deal breakers that would prevent automated dampening from running.

        Returns:
            bool: True if a deal breaker is found, False otherwise.
        """
        deal_breaker = ""
        deal_breaker_site = ""
        if len(self.data_generation[GENERATION]) == 0:
            deal_breaker = "No generation yet"
        else:
            for site in self.api.sites:
                if self.api.data_actuals[SITE_INFO].get(site[RESOURCE_ID]) is None:
                    deal_breaker = "No estimated actuals yet"
                    deal_breaker_site = site[RESOURCE_ID]
                    break
        if deal_breaker != "":
            _LOGGER.info("Auto-dampening suppressed: %s%s", deal_breaker, f" for {deal_breaker_site}" if deal_breaker_site != "" else "")
            return True
        return False

    def _find_earliest_common_history(self, min_days: int) -> dt | None:
        """Find earliest date where continuous dampening history is available for all models and deltas.

        Returns:
            Earliest common date with continuous history, or None if insufficient history exists.
        """
        period_lists = []
        for model in range(
            ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_MODEL][MINIMUM], ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_MODEL][MAXIMUM] + 1
        ):
            for delta in range(
                ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL][MINIMUM_EXTENDED],
                ADVANCED_OPTIONS[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL][MAXIMUM] + 1,
            ):
                if self._should_skip_model_delta(model, delta, min_days)[0]:
                    continue

                period_lists.append(sorted(entry["period_start"] for entry in self.auto_factors_history[model][delta]))

        if len(period_lists) == 0:
            return None

        # Find intersection of all period_start values
        common_periods = set.intersection(*(set(period_list) for period_list in period_lists))
        earliest_common = min(common_periods) if common_periods else None
        if earliest_common is not None:
            # Validate daily continuity from earliest_common forward
            if not all(
                all(curr == prev + timedelta(days=1) for prev, curr in pairwise(sorted(p for p in periods if p >= earliest_common)))
                for periods in period_lists
            ):
                earliest_common = None

        return earliest_common

    def _get_earliest_estimate_after(self, data: list[dict[str, Any]], after: dt, dampened: bool = False) -> dt | None:
        """Get the earliest estimated actual datetime after a specified datetime."""
        earliest = None
        if len(data) > 0:
            # Find all actuals with period_start >= after, then get the earliest one
            in_scope_actuals = [actual[PERIOD_START] for actual in data if actual[PERIOD_START] >= after]
            earliest = min(in_scope_actuals) if in_scope_actuals else None
            _LOGGER.debug(
                "Earliest applicable %s estimated actual datetime is %s",
                "dampened" if dampened else "undampened",
                earliest,
            )
        return earliest

    def _get_granular_factor(self, site: str, period_start: dt, interval_pv50: float = -1.0, record_adjustment: bool = False) -> float:
        """Retrieve a granular dampening factor."""
        factor = self.factors[site][
            period_start.hour if len(self.factors[site]) == 24 else ((period_start.hour * 2) + (1 if period_start.minute > 0 else 0))
        ]
        if (
            site == ALL
            and (self.api.options.auto_dampen or self.api.advanced_options[ADVANCED_GRANULAR_DAMPENING_DELTA_ADJUSTMENT])
            and self.factors.get(ALL)
        ):
            interval = self.adjusted_interval_dt(period_start)
            factor = min(1.0, self.factors[ALL][interval])
            if (
                not self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_NO_DELTA_ADJUSTMENT]
                and self.api.peak_intervals[interval] > 0
                and interval_pv50 > 0
                and factor < 1.0
            ):
                interval_time = period_start.astimezone(self.api.tz).strftime(DT_DATE_FORMAT)
                factor_pre_adjustment = factor

                factor = self._apply_adjustment(
                    interval_pv50, factor, interval, self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_DELTA_ADJUSTMENT_MODEL]
                )

                if (
                    record_adjustment
                    and period_start.astimezone(self.api.tz).date() == dt.now(self.api.tz).date()
                    and round(factor, 3) != round(factor_pre_adjustment, 3)
                ):
                    _LOGGER.debug(
                        "%sdjusted granular dampening factor for %s, %.3f (was %.3f, peak %.3f, interval pv50 %.3f)",
                        "Ignoring insignificant a"
                        if self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_INSIGNIFICANT_FACTOR_ADJUSTED] <= factor < 1.0
                        else "A",
                        interval_time,
                        factor,
                        factor_pre_adjustment,
                        self.api.peak_intervals[interval],
                        interval_pv50,
                    )
                factor = 1.0 if factor >= self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_INSIGNIFICANT_FACTOR_ADJUSTED] else factor

        return min(1.0, factor)

    async def _prepare_data(
        self, only_peaks: bool = False
    ) -> tuple[OrderedDict[dt, float], list[int], dict[dt, float], dict[int, list[dt]]]:
        """Builds data required for dampening calculations."""
        actuals: OrderedDict[dt, float] = OrderedDict()

        _LOGGER.debug("Determining peak estimated actual intervals%s", " and dampening data" if not only_peaks else "")
        if (
            self.api.options.auto_dampen or self.api.advanced_options[ADVANCED_GRANULAR_DAMPENING_DELTA_ADJUSTMENT]
        ) and self.api.options.get_actuals:
            for site in self.api.sites:
                if site[RESOURCE_ID] in self.api.options.exclude_sites:
                    _LOGGER.debug("Auto-dampening suppressed: Excluded site for %s", site[RESOURCE_ID])
                    continue
                start, end = self.api.get_list_slice(
                    self.api.data_actuals[SITE_INFO][site[RESOURCE_ID]][FORECASTS],
                    self.api.dt_helper.day_start_utc() - timedelta(days=self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL_DAYS]),
                    self.api.dt_helper.day_start_utc(),
                    search_past=True,
                )
                site_actuals = {
                    actual[PERIOD_START]: actual for actual in self.api.data_actuals[SITE_INFO][site[RESOURCE_ID]][FORECASTS][start:end]
                }
                for period_start, actual in site_actuals.items():
                    extant: float | None = actuals.get(period_start)
                    if extant is not None:
                        actuals[period_start] += actual[ESTIMATE] * 0.5
                    else:
                        actuals[period_start] = actual[ESTIMATE] * 0.5

            # Collect top intervals from the past MODEL_DAYS days.
            self.api.peak_intervals = dict.fromkeys(range(48), 0.0)
            for period_start, actual in actuals.items():
                interval = self.adjusted_interval_dt(period_start)
                if self.api.peak_intervals[interval] < actual:
                    self.api.peak_intervals[interval] = round(actual, 3)

        if only_peaks:
            return actuals, [], {}, {}

        ignored_intervals: list[int] = []  # Intervals to ignore in local time zone
        for time_string in self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_IGNORE_INTERVALS]:
            hour, minute = map(int, time_string.split(":"))
            interval = hour * 2 + minute // 30
            ignored_intervals.append(interval)

        export_limited_intervals = dict.fromkeys(range(50), False)
        if not self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_NO_LIMITING_CONSISTENCY]:
            for gen in self.data_generation[GENERATION][-1 * self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL_DAYS] * 48 :]:
                if gen[EXPORT_LIMITING]:
                    export_limited_intervals[self._adjusted_interval(gen)] = True

        generation: dict[dt, float] = {}
        for gen in self.data_generation[GENERATION][-1 * self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_MODEL_DAYS] * 48 :]:
            if not self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_NO_LIMITING_CONSISTENCY]:
                if not export_limited_intervals[self._adjusted_interval(gen)]:
                    generation[gen[PERIOD_START]] = gen[GENERATION]
            elif not gen[EXPORT_LIMITING]:
                generation[gen[PERIOD_START]] = gen[GENERATION]

        # Collect intervals that are close to the peak.
        matching_intervals: dict[int, list[dt]] = {i: [] for i in range(48)}
        for period_start, actual in actuals.items():
            interval = self.adjusted_interval_dt(period_start)
            if actual > self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_SIMILAR_PEAK] * self.api.peak_intervals[interval]:
                matching_intervals[interval].append(period_start)
        return actuals, ignored_intervals, generation, matching_intervals

    def _select_comparison_interval(
        self,
        generation_dampening: defaultdict[dt, dict[str, Any]],
        min_history_days: int,
    ) -> tuple[int, float, float, float]:
        """Select the best interval for single-interval adaptive comparison.

        Identifies the interval with highest dampening impact by balancing:
        - Substantial generation (not dawn/dusk)
        - Dampening actually being applied (factor < 1.0)
        - Model disagreement (variance in factors)
        - Breadth of dampening across model/delta configurations

        Args:
            generation_dampening: Generation data for calculating interval totals.
            min_history_days: Minimum number of history days required for a model.

        Returns:
            Tuple of (interval_index, avg_generation, avg_dampen_factor, variance).
        """
        interval_totals = [0.0] * 48
        interval_counts = [0] * 48
        interval_dampen_sum = [0.0] * 48
        interval_dampen_count = [0] * 48

        for ts, gen_data in generation_dampening.items():
            if not gen_data.get(EXPORT_LIMITING, False):
                interval = self.adjusted_interval_dt(ts)
                interval_totals[interval] += gen_data[GENERATION]
                interval_counts[interval] += 1

        # Analyse dampening factors using only the no-delta (raw) history entries.
        # Delta-adjusted entries (delta 0, 1, 2 …) have been pushed toward 1.0 by the
        # adjustment algorithm, which artificially deflates both variance and breadth.
        # The raw no-delta factors represent what each dampening model strength genuinely
        # computed, without post-processing bias — giving cleaner discrimination.
        interval_active_factors: list[list[float]] = [[] for _ in range(48)]
        total_models = 0
        combo_dampens: list[set[int]] = [set() for _ in range(48)]

        for model_key, model_data in self.auto_factors_history.items():
            no_delta_entries = model_data.get(VALUE_ADAPTIVE_DAMPENING_NO_DELTA, [])
            if len(no_delta_entries) >= min_history_days:
                total_models += 1
                for entry in no_delta_entries:
                    for i, factor in enumerate(entry["factors"]):
                        if factor < 1.0:  # Only count where dampening is applied
                            interval_dampen_sum[i] += factor
                            interval_dampen_count[i] += 1
                            combo_dampens[i].add(model_key)
                            interval_active_factors[i].append(factor)

        # Calculate averages and normalize generation to peak interval (0-1 range)
        avg_generation = [interval_totals[i] / interval_counts[i] if interval_counts[i] > 0 else 0.0 for i in range(48)]
        max_generation = max(avg_generation) if any(g > 0 for g in avg_generation) else 1.0
        normalized_generation = [g / max_generation for g in avg_generation]
        avg_dampen_factor = [interval_dampen_sum[i] / interval_dampen_count[i] if interval_dampen_count[i] > 0 else 1.0 for i in range(48)]

        # Calculate variance of dampening factors across models for each interval,
        # using only entries where dampening was actually applied (factor < 1.0).
        dampen_variance = []
        for i in range(48):
            if len(interval_active_factors[i]) > 1:
                active = interval_active_factors[i]
                mean = sum(active) / len(active)
                variance = sum((f - mean) ** 2 for f in active) / len(active)
                dampen_variance.append(variance)
            else:
                dampen_variance.append(0.0)

        # Calculate breadth of dampening: fraction of dampening models that apply dampening
        # Intervals where more model strengths agree dampening is needed are better for comparison
        dampening_breadth = [len(combo_dampens[i]) / total_models if total_models > 0 else 0.0 for i in range(48)]

        # Score = (1 - avg_factor) × sqrt(variance) × dampening_breadth, for intervals
        # with adequate generation only (≥ 10% of peak to exclude pre-dawn/post-dusk).
        # The goal here is dampening quality, not energy magnitude.
        min_gen_fraction = 0.10
        dampening_impact = [
            (1.0 - avg_dampen_factor[i]) * (dampen_variance[i] ** 0.5) * dampening_breadth[i]
            if normalized_generation[i] >= min_gen_fraction
            else 0.0
            for i in range(48)
        ]

        # Fall back progressively when history-based scoring cannot discriminate.
        if not dampening_impact or max(dampening_impact) == 0.0:
            # First fallback: drop the variance term — (1 - dampening) × breadth, still generation-gated
            dampening_impact = [
                (1.0 - avg_dampen_factor[i]) * dampening_breadth[i] if normalized_generation[i] >= min_gen_fraction else 0.0
                for i in range(48)
            ]
        if max(dampening_impact) == 0.0:
            # Second fallback: use the current model factors as a proxy for where dampening
            # matters. This handles the case where the history contains only 1.0 entries
            # (fresh install, overcast streak, etc.).
            current_all_factors: list[float] = self.factors.get(ALL, [])
            if current_all_factors and any(f < 1.0 for f in current_all_factors):
                min_gen_fraction = 0.10  # Require at least 10% of peak to exclude pre-dawn/dusk
                dampening_impact = [
                    (1.0 - current_all_factors[i]) if normalized_generation[i] >= min_gen_fraction else 0.0 for i in range(48)
                ]
        if max(dampening_impact) == 0.0:
            # Final fallback: pure generation — ensures a daytime interval is always chosen
            dampening_impact = list(normalized_generation)

        # Select interval with highest weighted score
        selected_interval = dampening_impact.index(max(dampening_impact)) if dampening_impact else 0

        return (
            selected_interval,
            avg_generation[selected_interval],
            avg_dampen_factor[selected_interval],
            dampen_variance[selected_interval],
        )

    async def _serialise_advanced_options(self) -> None:
        """Serialise advanced options to JSON."""
        start_time = time.time()
        _LOGGER.debug("Serialising advanced options to file: %s", self.api.filename_advanced)

        data = {}

        for option, value in self.api.advanced_options.items():
            adv_cfg = ADVANCED_OPTIONS.get(option)

            if adv_cfg and adv_cfg.get(AMENDABLE, False):  # Always update amendable options from memory
                data[option] = value
                _LOGGER.debug("Advanced option '%s' set to: %s", option, data[option])
            elif option in self.api.extant_advanced_options:
                data[option] = self.api.extant_advanced_options[option]  # write back non-amendable options unchanged

        payload = json.dumps(data, ensure_ascii=False, cls=NoIndentEncoder, indent=2, above_level=2)
        self.api.suppress_advanced_watchdog_reload = True  # Turn off watchdog for this change

        async with self.api.serialise_lock, aiofiles.open(self.api.filename_advanced, "w") as file:
            await file.write(payload)

        _LOGGER.debug("Task serialise_advanced_options took %.3f seconds", time.time() - start_time)

    def _should_skip_model_delta(self, model: int, delta: int, min_days: int) -> tuple[bool, str | None]:
        """Check if a model/delta combination should be skipped.

        Returns:
            tuple of (should_skip, reason)
        """
        if self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_EXCLUDE] and any(
            entry["model"] == model and entry["delta"] == delta
            for entry in self.api.advanced_options[ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_EXCLUDE]
        ):
            return True, f"in {ADVANCED_AUTOMATED_DAMPENING_ADAPTIVE_MODEL_EXCLUDE}"

        entries = self.auto_factors_history[model][delta]
        if len(entries) < min_days:
            return True, f"history of {len(entries)} days is less than minimum {min_days} days"

        return False, None
