"""Shared helper for reading OHGO API answers (standard library only)."""
from __future__ import annotations


def pascal(key):
    """'latitude' -> 'Latitude'; leaves keys that are already PascalCase alone."""
    return key[:1].upper() + key[1:] if isinstance(key, str) else key


def normalize_keys(obj):
    """Return a copy of an OHGO answer with every field name in PascalCase.

    OHGO's documentation shows PascalCase names (Results, Id, Latitude,
    AtmosphericSensors, AirTemperature ...) but the live API actually answers
    in camelCase (results, id, latitude, atmosphericSensors, airTemperature ...).
    Normalizing in one place lets the rest of the code use a single spelling,
    and keeps working if OHGO ever switches to the documented one.
    """
    if isinstance(obj, dict):
        return {pascal(k): normalize_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize_keys(v) for v in obj]
    return obj
