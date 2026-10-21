"""Disk-cached OpenStreetMap Nominatim reverse-geocoder implementing ReverseGeocoder."""

import json
import time
import urllib.request
from pathlib import Path

from expensieve.domain.models import GeoPoint


class CachedNominatimGeocoder:
    """Resolves (lat, lng) coordinates to human-readable place names with persistent local JSON caching."""

    def __init__(
        self,
        cache_path: Path | None = None,
        network_enabled: bool = True,
    ) -> None:
        self._cache_path = cache_path or (Path.home() / ".cache" / "expensieve" / "geocode_cache.json")
        self._network_enabled = network_enabled
        self._cache: dict[str, str] = self._load_cache()

    def _load_cache(self) -> dict[str, str]:
        if self._cache_path.exists():
            try:
                data = json.loads(self._cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
            except (OSError, ValueError):
                pass
        return {}

    def _save_cache(self) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(self._cache, indent=2), encoding="utf-8")

    @staticmethod
    def _coord_key(point: GeoPoint) -> str:
        # Round to 4 decimal places (~11m precision) for cache hits
        return f"{point.lat:.4f},{point.lng:.4f}"

    def resolve_place_name(self, point: GeoPoint) -> str | None:
        key = self._coord_key(point)
        if key in self._cache:
            return self._cache[key]
        if not self._network_enabled:
            return None

        url = f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat={point.lat}&lon={point.lng}&zoom=18"
        req = urllib.request.Request(url, headers={"User-Agent": "expensieve-geo-resolver/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                display_name = str(payload.get("display_name") or "")
                parts = [p.strip() for p in display_name.split(",") if p.strip()]
                short_name = ", ".join(parts[:2]) if parts else ""
                if short_name:
                    self._cache[key] = short_name
                    self._save_cache()
                    time.sleep(1.0)
                    return short_name
        except (OSError, ValueError):
            return None
        return None
