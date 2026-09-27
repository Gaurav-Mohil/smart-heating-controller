"""Modern Wi-Fi thermostats through Home Assistant.

Home Assistant already supports most thermostat brands (ecobee, Nest, Honeywell,
Mysa, Sinopé...). We only call its REST API to set the target temperature of one
`climate.*` entity, using a long-lived access token created in Home Assistant.
"""
from __future__ import annotations

import requests


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def check(url: str, token: str, entity_id: str) -> dict:
    resp = requests.get(f"{url.rstrip('/')}/api/states/{entity_id}", headers=_headers(token), timeout=8)
    resp.raise_for_status()
    data = resp.json()
    attrs = data.get("attributes", {})
    return {
        "state": data.get("state"),
        "current_temperature": attrs.get("current_temperature"),
        "target": attrs.get("temperature"),
        "name": attrs.get("friendly_name", entity_id),
    }


def set_temperature(url: str, token: str, entity_id: str, temp: float) -> None:
    resp = requests.post(
        f"{url.rstrip('/')}/api/services/climate/set_temperature",
        headers=_headers(token),
        json={"entity_id": entity_id, "temperature": temp},
        timeout=8,
    )
    resp.raise_for_status()
