"""Solcast hobbyist API simulator.

Install:

* This script runs in a Home Assistant DevContainer
* Modify /etc/hosts (need sudo): 127.0.0.1 localhost api.solcast.com.au
* Adjust TIMEZONE constant to match the Home Assistant configuration (the DevContainer will be set to UTC, so the time zone cannot be read from the environment).
* pip install Flask
* Script start: python3 -m wsgi

Optional run arguments:

* --limit LIMIT      Set the API call limit available, example --limit 100
* --no429            Do not generate 429 responses, example --no429
* --bomb429 w,x,y,z  The minute(s) of the hour to return API too busy, comma separated, example --bomb429 0,15,30,45

Theory of operation:

* Configure integration to use either API key "1", "2", "3", or any combination of multiple. Any other key will return an error.
* API key 1 has two sites, API key 2 has one site, API key 3 has an impossible (for hobbyists) three sites.
* Forecast for every day is the same bell curve
* 429 responses are given when minute=0, unless --no429 is set, or other minutes are specified with --bomb429

SSL certificate:

* To generate a new self-signed certificate if needed: openssl req -x509 -newkey rsa:4096 -nodes -out cert.pem -keyout key.pem -days 3650
* openssl will already be installed in the DevContainer

Integration issues raised regarding the simulator will be closed without response.

"""  # noqa: INP001

import argparse
import datetime
from datetime import datetime as dt, timedelta
from logging.config import dictConfig
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request
from flask.json.provider import DefaultJSONProvider

TIMEZONE = ZoneInfo("Australia/Melbourne")
DEBUG = False  # Run Flask in debug mode (auto-restart on code changes)

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
                "location": "Downunder",
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
                "location": "Downunder",
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
                "capacity_dc": 3.5,
                "azimuth": 90,
                "tilt": 30,
                "location": "Downunder",
            },
        ],
        "counter": 0,
    },
    "3": {
        "sites": [
            {
                "resource_id": "4444-4444-4444-4444",
                "name": "Fourth Site",
                "latitude": -11.11111,
                "longitude": 111.1111,
                "install_date": "2024-01-01T00:00:00+00:00",
                "loss_factor": 0.99,
                "capacity": 4.5,
                "capacity_dc": 5.0,
                "azimuth": 90,
                "tilt": 30,
                "location": "Downunder",
            },
            {
                "resource_id": "5555-5555-5555-5555",
                "name": "Fifth Site",
                "latitude": -11.11111,
                "longitude": 111.1111,
                "install_date": "2024-01-01T00:00:00+00:00",
                "loss_factor": 0.99,
                "capacity": 3.2,
                "capacity_dc": 3.7,
                "azimuth": 90,
                "tilt": 30,
                "location": "Downunder",
            },
            {
                "resource_id": "6666-6666-6666-6666",
                "name": "Sixth Site",
                "latitude": -11.11111,
                "longitude": 111.1111,
                "install_date": "2024-01-01T00:00:00+00:00",
                "loss_factor": 0.99,
                "capacity": 4.2,
                "capacity_dc": 4.8,
                "azimuth": 90,
                "tilt": 30,
                "location": "Downunder",
            },
        ],
        "counter": 0,
    },
}
BOMB_429 = [0]
FORECAST = 0.9
FORECAST_10 = 0.75
FORECAST_90 = 1.0
GENERATE_429 = True
GENERATION_FACTOR = [
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0.01,
    0.025,
    0.04,
    0.075,
    0.11,
    0.17,
    0.26,
    0.38,
    0.52,
    0.65,
    0.8,
    0.9,
    0.97,
    1,
    1,
    0.97,
    0.9,
    0.8,
    0.65,
    0.52,
    0.38,
    0.26,
    0.17,
    0.11,
    0.075,
    0.04,
    0.025,
    0.01,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
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
        "root": {"level": "DEBUG", "handlers": ["wsgi"]},
    }
)


class DtJSONProvider(DefaultJSONProvider):
    """Custom JSON provider."""

    def default(self, o):
        """Convert datetime to ISO format."""
        if isinstance(o, dt):
            return o.isoformat()

        return super().default(o)


app = Flask(__name__)
app.json = DtJSONProvider(app)
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
    global counter_last_reset  # noqa: PLW0603 pylint: disable=global-statement
    if counter_last_reset.day != dt.now(datetime.UTC).day:
        _LOGGER.info("Resetting API usage counter")
        for v in API_KEY_SITES.values():
            v["counter"] = 0
        counter_last_reset = dt.now(datetime.UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    if not api_key:
        return False, {"response_status": {"error_code": "KeyRequired", "message": "An API key must be specified"}}, 400
    if api_key not in API_KEY_SITES:
        return False, {"response_status": {"error_code": "InvalidKey", "message": "Invalid API key"}}, 401
    if GENERATE_429 and dt.now(datetime.UTC).minute in BOMB_429:
        return False, {}, 429
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
                    "period_end": period_end + timedelta(minutes=minute * 30),
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
                "period_end": period_end + timedelta(minutes=minute * 30),
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", help="Set the API call limit available, example --limit 100", type=int, required=False)
    parser.add_argument("--no429", help="Do not generate 429 responses", action="store_true", required=False)
    parser.add_argument(
        "--bomb429",
        help="The minute(s) of the hour to return API too busy, comma separated, example --bomb429 0,15,30,45",
        type=str,
        required=False,
    )
    args = parser.parse_args()
    if args.limit:
        API_LIMIT = args.limit
        _LOGGER.debug("API limit has been set to %s", API_LIMIT)
    if args.no429:
        GENERATE_429 = False
        _LOGGER.debug("429 responses will not be generated")
    if args.bomb429:
        BOMB_429 = [int(x) for x in args.bomb429.split(",")]
        _LOGGER.debug("API too busy responses will be returned at minute(s) %s", BOMB_429)

    _LOGGER.info("Starting Solcast hobbyist API simulator, will listen on localhost:443")
    _LOGGER.info("API limit is set to %s, usage has been reset", API_LIMIT)
    _LOGGER.info("Simulator originally written by @autoSteve")
    _LOGGER.info("Integration issues raised regarding this script will be closed without response because it is a development tool")
    app.run(debug=DEBUG, host="127.0.0.1", port=443, ssl_context=("cert.pem", "key.pem"))
