"""Offline provenance for the distribution actually executing this process."""

from importlib.metadata import PackageNotFoundError, version
from platform import python_version


def package_version() -> str:
    try:
        return version("viajante")
    except PackageNotFoundError:
        return "unknown"


def get_runtime_info() -> dict[str, object]:
    return {
        "viajante_version": package_version(),
        "python_version": python_version(),
        "hotel_schema_version": 2,
    }
