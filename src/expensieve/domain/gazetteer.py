"""Namma Metro station gazetteer, personal landmark registry, and Haversine coordinate snapping."""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from expensieve.domain.models import GeoPoint


@dataclass(frozen=True, slots=True)
class NamedPlace:
    """A geographic landmark or transit station."""

    name: str
    point: GeoPoint
    place_type: str  # "METRO_STATION" | "LANDMARK"
    radius_meters: float = 450.0


def haversine_meters(a: GeoPoint, b: GeoPoint) -> float:
    """Compute great-circle distance in meters between two WGS-84 coordinates."""
    earth_radius_m = 6_371_000.0
    lat1, lon1 = math.radians(a.lat), math.radians(a.lng)
    lat2, lon2 = math.radians(b.lat), math.radians(b.lng)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    sin_dlat = math.sin(dlat / 2.0)
    sin_dlon = math.sin(dlon / 2.0)
    h = sin_dlat * sin_dlat + math.cos(lat1) * math.cos(lat2) * sin_dlon * sin_dlon
    return 2.0 * earth_radius_m * math.asin(min(1.0, math.sqrt(h)))


# Comprehensive Namma Metro (Purple Line & Green Line) station coordinates
NAMMA_METRO_STATIONS: tuple[NamedPlace, ...] = (
    # Purple Line (Whitefield / Kadugodi -> Challaghatta)
    NamedPlace("Whitefield (Kadugodi)", GeoPoint(12.9955, 77.7575), "METRO_STATION"),
    NamedPlace("Hopefarm Channasandra", GeoPoint(12.9873, 77.7538), "METRO_STATION"),
    NamedPlace("Kadugodi Tree Park", GeoPoint(12.9854, 77.7468), "METRO_STATION"),
    NamedPlace("Pattandur Agrahara", GeoPoint(12.9876, 77.7378), "METRO_STATION"),
    NamedPlace("Sri Sathya Sai Hospital", GeoPoint(12.9812, 77.7275), "METRO_STATION"),
    NamedPlace("Nallurhalli", GeoPoint(12.9767, 77.7248), "METRO_STATION"),
    NamedPlace("Kundalahalli", GeoPoint(12.9776, 77.7156), "METRO_STATION"),
    NamedPlace("Seetharampalya", GeoPoint(12.9810, 77.7087), "METRO_STATION"),
    NamedPlace("Hoodi", GeoPoint(12.9888, 77.7113), "METRO_STATION"),
    NamedPlace("Garudacharpalya", GeoPoint(12.9934, 77.7037), "METRO_STATION"),
    NamedPlace("Singayyanapalya", GeoPoint(12.9966, 77.6928), "METRO_STATION"),
    NamedPlace("KR Pura", GeoPoint(12.9999, 77.6776), "METRO_STATION"),
    NamedPlace("Benniganahalli", GeoPoint(12.9962, 77.6685), "METRO_STATION"),
    NamedPlace("Baiyappanahalli", GeoPoint(12.9908, 77.6525), "METRO_STATION"),
    NamedPlace("Swami Vivekananda Road", GeoPoint(12.9859, 77.6449), "METRO_STATION"),
    NamedPlace("Indiranagar", GeoPoint(12.9783, 77.6386), "METRO_STATION"),
    NamedPlace("Halasuru", GeoPoint(12.9765, 77.6267), "METRO_STATION"),
    NamedPlace("Trinity", GeoPoint(12.9730, 77.6170), "METRO_STATION"),
    NamedPlace("MG Road", GeoPoint(12.9755, 77.6068), "METRO_STATION"),
    NamedPlace("Cubbon Park", GeoPoint(12.9810, 77.5967), "METRO_STATION"),
    NamedPlace("Dr. B.R. Ambedkar Station, Vidhana Soudha", GeoPoint(12.9799, 77.5928), "METRO_STATION"),
    NamedPlace("Sir M. Visvesvaraya Stn., Central College", GeoPoint(12.9744, 77.5841), "METRO_STATION"),
    NamedPlace("Nadaprabhu Kempegowda Station, Majestic", GeoPoint(12.9757, 77.5728), "METRO_STATION"),
    NamedPlace("KSR Bengaluru City Railway Station", GeoPoint(12.9759, 77.5666), "METRO_STATION"),
    NamedPlace("Magadi Road", GeoPoint(12.9756, 77.5553), "METRO_STATION"),
    NamedPlace("Sri Balagangadharanatha Swamiji Stn., Hosahalli", GeoPoint(12.9743, 77.5455), "METRO_STATION"),
    NamedPlace("Vijayanagar", GeoPoint(12.9708, 77.5374), "METRO_STATION"),
    NamedPlace("Attiguppe", GeoPoint(12.9619, 77.5336), "METRO_STATION"),
    NamedPlace("Deepanjali Nagar", GeoPoint(12.9521, 77.5370), "METRO_STATION"),
    NamedPlace("Mysuru Road", GeoPoint(12.9467, 77.5302), "METRO_STATION"),
    NamedPlace("Nayandahalli", GeoPoint(12.9416, 77.5253), "METRO_STATION"),
    NamedPlace("Rajarajeshwari Nagar", GeoPoint(12.9366, 77.5196), "METRO_STATION"),
    NamedPlace("Jnanabharathi", GeoPoint(12.9354, 77.5123), "METRO_STATION"),
    NamedPlace("Pattanagere", GeoPoint(12.9242, 77.4983), "METRO_STATION"),
    NamedPlace("Kengeri Bus Terminal", GeoPoint(12.9147, 77.4878), "METRO_STATION"),
    NamedPlace("Kengeri", GeoPoint(12.9079, 77.4764), "METRO_STATION"),
    NamedPlace("Challaghatta", GeoPoint(12.8958, 77.4622), "METRO_STATION"),
    # Green Line (Madavara / Nagasandra -> Silk Institute)
    NamedPlace("Yeshwantpur", GeoPoint(13.0233, 77.5499), "METRO_STATION"),
    NamedPlace("Sandal Soap Factory", GeoPoint(13.0147, 77.5540), "METRO_STATION"),
    NamedPlace("Mahalakshmi", GeoPoint(13.0082, 77.5488), "METRO_STATION"),
    NamedPlace("Rajajinagar", GeoPoint(13.0005, 77.5497), "METRO_STATION"),
    NamedPlace("Mahakavi Kuvempu Road", GeoPoint(12.9985, 77.5568), "METRO_STATION"),
    NamedPlace("Srirampura", GeoPoint(12.9965, 77.5633), "METRO_STATION"),
    NamedPlace("Mantri Square Sampige Road", GeoPoint(12.9905, 77.5707), "METRO_STATION"),
    NamedPlace("Chickpete", GeoPoint(12.9669, 77.5746), "METRO_STATION"),
    NamedPlace("Krishna Rajendra Market", GeoPoint(12.9613, 77.5746), "METRO_STATION"),
    NamedPlace("National College", GeoPoint(12.9506, 77.5737), "METRO_STATION"),
    NamedPlace("Lalbagh", GeoPoint(12.9464, 77.5801), "METRO_STATION"),
    NamedPlace("South End Circle", GeoPoint(12.9383, 77.5801), "METRO_STATION"),
    NamedPlace("Jayanagar", GeoPoint(12.9295, 77.5801), "METRO_STATION"),
    NamedPlace("Rashtreeya Vidyalaya Road", GeoPoint(12.9215, 77.5800), "METRO_STATION"),
    NamedPlace("Banashankari", GeoPoint(12.9152, 77.5736), "METRO_STATION"),
    NamedPlace("Jaya Prakash Nagar", GeoPoint(12.9075, 77.5731), "METRO_STATION"),
    NamedPlace("Yelachenahalli", GeoPoint(12.8960, 77.5702), "METRO_STATION"),
    NamedPlace("Konanakunte Cross", GeoPoint(12.8848, 77.5679), "METRO_STATION"),
)

# Default frequent landmarks (can be extended via user profile config)
DEFAULT_LANDMARKS: tuple[NamedPlace, ...] = (
    NamedPlace("Bangalore Home", GeoPoint(13.0250, 77.6738), "LANDMARK", radius_meters=650.0),
    NamedPlace("Benniganahalli Metro", GeoPoint(12.9962, 77.6685), "LANDMARK", radius_meters=400.0),
    NamedPlace("KR Pura", GeoPoint(12.9999, 77.6776), "LANDMARK", radius_meters=400.0),
    NamedPlace(
        "Bangalore International Centre (Domlur)",
        GeoPoint(12.9667, 77.6354),
        "LANDMARK",
        radius_meters=350.0,
    ),
    NamedPlace("Indiranagar", GeoPoint(12.9783, 77.6386), "LANDMARK", radius_meters=450.0),
    NamedPlace("Church Street (MG Road)", GeoPoint(12.9748, 77.6045), "LANDMARK", radius_meters=400.0),
    NamedPlace("Cubbon Park", GeoPoint(12.9780, 77.5945), "LANDMARK", radius_meters=550.0),
    NamedPlace("IISc Bengaluru", GeoPoint(13.0210, 77.5650), "LANDMARK", radius_meters=700.0),
    NamedPlace("MS Ramaiah Hospital", GeoPoint(13.0285, 77.5705), "LANDMARK", radius_meters=500.0),
    NamedPlace("Vasanth Nagar", GeoPoint(12.9913, 77.5968), "LANDMARK", radius_meters=400.0),
    NamedPlace("Jawaharlal Nehru Planetarium", GeoPoint(12.9849, 77.5896), "LANDMARK", radius_meters=350.0),
    NamedPlace("Prestige Shantiniketan (Whitefield)", GeoPoint(12.9896, 77.7279), "LANDMARK", radius_meters=450.0),
    NamedPlace("Dhruvathare (NH75)", GeoPoint(12.9779, 76.8330), "LANDMARK", radius_meters=400.0),
    NamedPlace("Arasikere", GeoPoint(13.3205, 76.2555), "LANDMARK", radius_meters=2000.0),
)


class Gazetteer:
    """Snaps WGS-84 coordinates to Namma Metro stations or named landmarks."""

    def __init__(
        self,
        metro_stations: Sequence[NamedPlace] = NAMMA_METRO_STATIONS,
        landmarks: Sequence[NamedPlace] = DEFAULT_LANDMARKS,
    ) -> None:
        self._metro_stations = tuple(metro_stations)
        self._landmarks = tuple(landmarks)

    def snap_metro_station(self, point: GeoPoint, max_distance_m: float = 900.0) -> str | None:
        """Find the nearest Namma Metro station within max_distance_m."""
        best_name: str | None = None
        best_dist = max_distance_m
        for station in self._metro_stations:
            dist = haversine_meters(point, station.point)
            if dist <= best_dist:
                best_dist = dist
                best_name = station.name
        return best_name

    def snap_landmark(self, point: GeoPoint) -> str | None:
        """Find the nearest known landmark or Metro station within its configured radius."""
        best_name: str | None = None
        best_dist = float("inf")
        for place in (*self._landmarks, *self._metro_stations):
            dist = haversine_meters(point, place.point)
            if dist <= place.radius_meters and dist < best_dist:
                best_dist = dist
                best_name = place.name
        return best_name

    def normalize_address(self, raw_address: str) -> str:
        """Normalize a raw street address from an invoice PDF or email into a clean landmark/locality."""
        cleaned = " ".join(raw_address.split()).strip(" ,.-")
        if not cleaned:
            return "Unknown"
        upper = cleaned.upper()

        # Personal home & frequent landmarks
        if "RAGHAVENDRA NAGAR" in upper and ("HORAMAVU" in upper or "KALKERE" in upper):
            return "Bangalore Home"
        if "BENNIGANAHALLI" in upper:
            return "Benniganahalli Metro"
        if "BANGALORE INTERNATIONAL CENTRE" in upper:
            return "Bangalore International Centre (Domlur)"
        if "JAWAHARLAL NEHRU PLANETARIUM" in upper:
            return "Jawaharlal Nehru Planetarium"
        if "PRESTIGE SHANTINIKETAN" in upper:
            return "Prestige Shantiniketan (Whitefield)"
        if "OCEANUS FREESIA" in upper:
            return "Oceanus Freesia Enclave (Bellandur)"
        if "PHOENIX MALL OF ASIA" in upper or "MALL OF ASIA" in upper:
            return "Phoenix Mall of Asia (Yelahanka)"
        if "PARK VIEW LAYOUT" in upper:
            return "Park View Layout (Sahakara Nagar)"
        if "KEMPEGOWDA INTERNATIONAL AIRPORT" in upper or "INTERNATIONAL AIRPORT" in upper:
            if "TERMINAL 2" in upper:
                return "Kempegowda International Airport Terminal 2"
            return "Kempegowda International Airport"
        if "SCIENCE GALLERY" in upper:
            return "Science Gallery Bengaluru"
        if "VR BENGALURU" in upper:
            return "VR Bengaluru Mall"
        if "TELEPHONE HOUSE" in upper:
            return "Telephone House (Raj Bhavan Rd)"
        if "COOKE TOWN" in upper:
            return "Cooke Town"
        if "PAI LAYOUT" in upper or "DOORAVANI NAGAR" in upper or "TIN FACTORY" in upper:
            if "PAI LAYOUT" in upper:
                return "Pai Layout (Tin Factory)"
            return "Tin Factory"
        if "RAMAMURTHY NAGAR" in upper:
            return "Ramamurthy Nagar"
        if "DOMLUR" in upper:
            return "Domlur"
        if "INDIRANAGAR" in upper or "HAL 2ND STAGE" in upper:
            return "Indiranagar (HAL 2nd Stage)"
        if "MATHIKERE" in upper:
            return "Mathikere"
        if "BTM LAYOUT" in upper:
            return "BTM Layout"
        if "SAHAKARA NAGAR" in upper:
            return "Sahakara Nagar"
        if "GANGANAGAR" in upper:
            return "Ganganagar"

        # Generic fallback: strip trailing city/state/country/pincode and take last 1-2 meaningful segments
        parts = [p.strip() for p in cleaned.split(",") if p.strip()]
        filtered = [
            p
            for p in parts
            if not any(
                skip in p.upper()
                for skip in ("KARNATAKA", "INDIA", "BENGALURU", "BANGALORE", "560")
            )
        ]
        if filtered:
            return filtered[-1]
        return parts[0] if parts else cleaned
