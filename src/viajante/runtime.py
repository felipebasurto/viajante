"""Offline provenance for the distribution actually executing this process."""

from importlib.metadata import PackageNotFoundError, version
from platform import python_version

from viajante.sweep_config import get_sweep_config


def package_version() -> str:
    try:
        return version("viajante")
    except PackageNotFoundError:
        return "unknown"


def get_runtime_info() -> dict[str, object]:
    sweep = get_sweep_config()
    return {
        "viajante_version": package_version(),
        "python_version": python_version(),
        "hotel_schema_version": 2,
        "flight_transport": "public_page",
        "flight_capabilities": {
            "one_way": True,
            "round_trip": "bounded",
            "public_outbound_limit": 8,
            "bags": False,
            "carry_on": True,
            "carrier_filters": True,
            "exclude_alliances": False,
            "multi_city": True,
            "multi_city_fetch": "detail_only",
            "explore_catalog": True,
            "explore_catalog_scope": "bounded",
        },
        "sweep_mode": sweep.mode,
        "sweep_concurrency": sweep.concurrency,
    }
