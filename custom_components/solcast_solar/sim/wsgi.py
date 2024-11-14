"""Solcast hobbyist API simulator.

Install:

* Run this script in a Home Assistant DevContainer
* Modify /etc/hosts (need sudo): 127.0.0.1 localhost api.solcast.com.au
* Adjust TIMEZONE constant to match the Home Assistant configuration (the DevContainer will be set to UTC).
* pip install Flask
* Script start: python3 -m wsgi

Theory of operation:

* Configure integration to use either API key "1", "2", or both. Any other key will return an error.
* API key 1 has two sites, API key 2 has one site
* Forecast for every day is the same bell curve
* 429 responses are always given when minute=0

SSL certificate:

* To generate a new self-signed certificate if needed: openssl req -x509 -newkey rsa:4096 -nodes -out cert.pem -keyout key.pem -days 3650

Integration issues raised regarding the simulator will be closed without response.

"""  # noqa: INP001

import datetime
from datetime import datetime as dt, timedelta
from logging.config import dictConfig
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request

TIMEZONE = ZoneInfo("Australia/Melbourne")

API_LIMIT = 50
API_KEY_SITES = {
    "1": {
        "sites": [
            {
                "resource_id": "1111-1111-1111-1111",
                "name": "First Site",
                "latitude": -11.11111,
                "longitude": 111.1111,
                "install_date": "2024-01-01T00:00:00+00:00",
                "loss_factor": 0.99,
                "capacity": 5.0,
                "capacity_dc": 6.2,
                "azimuth": 90,
                "tilt": 30,
                "location": "Oz",
            },
            {
                "resource_id": "2222-2222-2222-2222",
                "name": "Second Site",
                "latitude": -11.11111,
                "longitude": 111.1111,
                "install_date": "2024-01-01T00:00:00+00:00",
                "loss_factor": 0.99,
                "capacity": 3.0,
                "capacity_dc": 4.2,
                "azimuth": 90,
                "tilt": 30,
                "location": "Oz",
            },
        ],
        "counter": 0,
    },
    "2": {
        "sites": [
            {
                "resource_id": "3333-3333-3333-3333",
                "name": "Third Site",
                "latitude": -11.11111,
                "longitude": 111.1111,
                "install_date": "2024-01-01T00:00:00+00:00",
                "loss_factor": 0.99,
                "capacity": 3.0,
                "capacity_dc": 4.2,
                "azimuth": 90,
                "tilt": 30,
                "location": "Oz",
            },
        ],
        "counter": 0,
    },
}
FORECAST = 0.9
FORECAST_10 = 0.75
FORECAST_90 = 1.0
GENERATION_FACTOR = [
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.2,
    0.3,
    0.4,
    0.5,
    0.6,
    0.7,
    0.85,
    0.9,
    1.0,
    1.0,
    0.9,
    0.85,
    0.7,
    0.6,
    0.5,
    0.4,
    0.3,
    0.2,
    0.1,
    0.075,
    0.05,
    0.025,
    0.01,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
]

dictConfig(
    {
        "version": 1,
        "formatters": {
            "default": {
                "format": "[%(asctime)s] %(levelname)s in %(module)s: %(message)s",
            }
        },
        "handlers": {
            "wsgi": {"class": "logging.StreamHandler", "stream": "ext://flask.logging.wsgi_errors_stream", "formatter": "default"}
        },
        "root": {"level": "INFO", "handlers": ["wsgi"]},
    }
)

app = Flask(__name__)
_LOGGER = app.logger
counter_last_reset = dt.now(datetime.UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def find_site(site_id, api_key):
    """Find the site details by site_id."""
    return next((site for site in API_KEY_SITES[api_key]["sites"] if site["resource_id"] == site_id), None)


def get_period(delta):
    """Return the start period and factors for the current time."""
    period_end = dt.now(datetime.UTC)
    return period_end.replace(minute=(int(period_end.minute / 30) * 30), second=0, microsecond=0) + delta


def validate_call(api_key, counter=True):
    """Return the state of the API call."""
    global counter_last_reset  # noqa: PLW0603
    if counter_last_reset.day != dt.now(datetime.UTC).day:
        _LOGGER.info("Resetting API usage counter")
        for v in API_KEY_SITES.values():
            v["counter"] = 0
        counter_last_reset = dt.now(datetime.UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    if not api_key:
        return False, {"response_status": {"error_code": "KeyRequired", "message": "An API key must be specified"}}, 400
    if api_key not in API_KEY_SITES:
        return False, {"response_status": {"error_code": "InvalidKey", "message": "Invalid API key"}}, 401
    if dt.now(datetime.UTC).minute == 0:
        return False, {"response_status": {}}, 429
    if counter and API_KEY_SITES[api_key]["counter"] >= API_LIMIT:
        return False, {"response_status": {"error_code": "TooManyRequests", "message": "You have exceeded your free daily limit."}}, 429
    return True, None, 200


@app.route("/rooftop_sites", methods=["GET"])
def get_sites():
    """Return sites for an API key."""

    api_key = request.args.get("api_key")

    state, issue, response_code = validate_call(api_key, counter=False)
    if not state:
        return jsonify(issue), response_code

    # Simulate different responses based on the API key
    sites = API_KEY_SITES[api_key]
    meta = {
        "page_count": 1,
        "current_page": 1,
        "total_records": 1,
    }
    return jsonify(sites | meta), 200


@app.route("/rooftop_sites/<site_id>/estimated_actuals", methods=["GET"])
def get_site_estimated_actuals(site_id):
    """Return simulated estimated actials for a site."""

    api_key = request.args.get("api_key")
    state, issue, response_code = validate_call(api_key)
    if not state:
        return jsonify(issue), response_code
    API_KEY_SITES[api_key]["counter"] += 1

    # Find the site by site_id
    site = find_site(site_id, api_key)
    if not site:
        return jsonify({"response_status": {"error_code": "SiteNotFound", "message": "The specified site cannot be found"}}), 404

    _hours = int(request.args.get("hours"))
    period_end = get_period(timedelta(hours=_hours) * -1)

    return jsonify(
        {
            "estimated_actuals": [
                {
                    "period_end": (period_end + timedelta(minutes=minute * 30)).isoformat(),
                    "pv_estimate": round(
                        site["capacity"]
                        * FORECAST
                        * GENERATION_FACTOR[
                            int(
                                (period_end + timedelta(minutes=minute * 30)).astimezone(TIMEZONE).hour * 2
                                + (period_end + timedelta(minutes=minute * 30)).astimezone(TIMEZONE).minute / 30
                            )
                        ],
                        4,
                    ),
                }
                for minute in range((_hours + 1) * 2)
            ],
        },
    ), 200


@app.route("/rooftop_sites/<site_id>/forecasts", methods=["GET"])
def get_site_forecasts(site_id):
    """Return simulated forecasts for a site."""

    api_key = request.args.get("api_key")
    state, issue, response_code = validate_call(api_key)
    if not state:
        return jsonify(issue), response_code
    API_KEY_SITES[api_key]["counter"] += 1
    _LOGGER.info("API key %s has been used %s times", api_key, API_KEY_SITES[api_key]["counter"])

    # Find the site by site_id
    site = find_site(site_id, api_key)
    if not site:
        return jsonify({"response_status": {"error_code": "SiteNotFound", "message": "The specified site cannot be found"}}), 404

    _hours = int(request.args.get("hours"))
    period_end = get_period(timedelta(minutes=30))

    response = {
        "forecasts": [
            {
                "period_end": (period_end + timedelta(minutes=minute * 30)).isoformat(),
                "pv_estimate": round(
                    site["capacity"]
                    * FORECAST
                    * GENERATION_FACTOR[
                        int(
                            (period_end + timedelta(minutes=minute * 30)).astimezone(TIMEZONE).hour * 2
                            + (period_end + timedelta(minutes=minute * 30)).astimezone(TIMEZONE).minute / 30
                        )
                    ],
                    4,
                ),
                "pv_estimate10": round(
                    site["capacity"]
                    * FORECAST_10
                    * GENERATION_FACTOR[
                        int(
                            (period_end + timedelta(minutes=minute * 30)).astimezone(TIMEZONE).hour * 2
                            + (period_end + timedelta(minutes=minute * 30)).astimezone(TIMEZONE).minute / 30
                        )
                    ],
                    4,
                ),
                "pv_estimate90": round(
                    site["capacity"]
                    * FORECAST_90
                    * GENERATION_FACTOR[
                        int(
                            (period_end + timedelta(minutes=minute * 30)).astimezone(TIMEZONE).hour * 2
                            + (period_end + timedelta(minutes=minute * 30)).astimezone(TIMEZONE).minute / 30
                        )
                    ],
                    4,
                ),
            }
            for minute in range(_hours * 2)
        ],
    }
    # _LOGGER.info(response)
    return jsonify(response), 200


if __name__ == "__main__":
    _LOGGER.info("Starting Solcast hobbyist API simulator, will listen on localhost:443")
    _LOGGER.info("API limit is set to %s, usage has been reset", API_LIMIT)
    _LOGGER.info("Simulator originally written by @autoSteve")
    _LOGGER.info("Integration issues raised regarding this script will be closed without response because it is a development tool")
    app.run(host="127.0.0.1", port=443, ssl_context=("cert.pem", "key.pem"))
