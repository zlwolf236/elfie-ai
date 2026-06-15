"""Weather skill for Elfie — current conditions and a simple forecast.

Backed by Open-Meteo (https://open-meteo.com): free, no API key required,
includes its own geocoding endpoint. Nothing to configure in .env.
"""

import httpx

from livekit.agents import function_tool, RunContext

DEFAULT_CITY = "New York"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT = 10.0

# WMO weather interpretation codes -> spoken description
_WMO_CODES = {
    0: "clear skies",
    1: "mostly clear skies",
    2: "partly cloudy skies",
    3: "overcast skies",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "heavy freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "heavy freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light rain showers",
    81: "rain showers",
    82: "violent rain showers",
    85: "light snow showers",
    86: "heavy snow showers",
    95: "thunderstorms",
    96: "thunderstorms with light hail",
    99: "thunderstorms with heavy hail",
}


def _describe(code) -> str:
    try:
        return _WMO_CODES.get(int(code), "mixed conditions")
    except (TypeError, ValueError):
        return "mixed conditions"


async def _geocode(client: httpx.AsyncClient, city: str):
    """Resolve a city name to (latitude, longitude, spoken place name)."""
    resp = await client.get(
        GEOCODE_URL, params={"name": city, "count": 1, "language": "en"}
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        return None
    hit = results[0]
    name = hit["name"]
    region = hit.get("admin1") or hit.get("country") or ""
    spoken = f"{name}, {region}" if region and region != name else name
    return hit["latitude"], hit["longitude"], spoken


@function_tool
async def get_weather(context: RunContext, city: str = "") -> str:
    """Call this whenever the user asks about the weather, temperature,
    rain, snow, wind, or the forecast — for today, tonight, tomorrow, or
    the next few days. Pass the city the user named; leave city empty if
    they did not name one (defaults to the New York area). Returns current
    conditions plus a short forecast for today and tomorrow.

    Args:
        city: City name the user asked about, e.g. "Boston" or "Tokyo".
              Leave empty for the user's default area (New York).
    """
    city = (city or "").strip() or DEFAULT_CITY

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            place = await _geocode(client, city)
            if place is None:
                return (
                    f"Sorry, I couldn't find a place called {city}. "
                    "Could you try a nearby bigger city?"
                )
            lat, lon, spoken_place = place

            resp = await client.get(
                FORECAST_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": (
                        "temperature_2m,apparent_temperature,"
                        "relative_humidity_2m,weather_code,wind_speed_10m"
                    ),
                    "daily": (
                        "weather_code,temperature_2m_max,temperature_2m_min,"
                        "precipitation_probability_max"
                    ),
                    "temperature_unit": "fahrenheit",
                    "wind_speed_unit": "mph",
                    "timezone": "auto",
                    "forecast_days": 3,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        return "Sorry, the weather service is taking too long to answer. Try again in a moment."
    except httpx.HTTPError:
        return "Sorry, I couldn't reach the weather service right now."

    try:
        cur = data["current"]
        daily = data["daily"]

        parts = [
            f"Right now in {spoken_place} it's {round(cur['temperature_2m'])} degrees "
            f"with {_describe(cur['weather_code'])}."
        ]

        feels = round(cur["apparent_temperature"])
        if abs(feels - round(cur["temperature_2m"])) >= 3:
            parts.append(f"It feels like {feels}.")

        wind = round(cur.get("wind_speed_10m") or 0)
        if wind >= 15:
            parts.append(f"It's windy, around {wind} miles per hour.")

        today_hi = round(daily["temperature_2m_max"][0])
        today_lo = round(daily["temperature_2m_min"][0])
        rain_today = daily["precipitation_probability_max"][0]
        today = f"Today expect {_describe(daily['weather_code'][0])}, high of {today_hi}, low of {today_lo}."
        if rain_today is not None and rain_today >= 30:
            today += f" There's a {round(rain_today)} percent chance of precipitation."
        parts.append(today)

        if len(daily["temperature_2m_max"]) > 1:
            parts.append(
                f"Tomorrow looks like {_describe(daily['weather_code'][1])} "
                f"with a high of {round(daily['temperature_2m_max'][1])}."
            )

        parts.append("(Source: Open-Meteo — say this is where the data came from.)")
        return " ".join(parts)
    except (KeyError, IndexError, TypeError):
        return "Sorry, the weather service sent back something I couldn't read. Try again in a moment."


TOOLS = [get_weather]
