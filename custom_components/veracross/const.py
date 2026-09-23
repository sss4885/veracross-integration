"""Constants for the Veracross integration."""

from datetime import timedelta

DOMAIN = "veracross"
PLATFORMS = ["sensor", "button"]

CACHE_MAX_AGE = timedelta(hours=12)
REFRESH_THROTTLE = timedelta(minutes=5)

