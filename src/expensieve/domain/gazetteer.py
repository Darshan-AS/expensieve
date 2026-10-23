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
    NamedPlace(
        "Sri Balagangadharanatha Swamiji Stn., Hosahalli", GeoPoint(12.9743, 77.5455), "METRO_STATION"
    ),
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
    NamedPlace(
        "Prestige Shantiniketan (Whitefield)", GeoPoint(12.9896, 77.7279), "LANDMARK", radius_meters=450.0
    ),
    NamedPlace("Dhruvathare (NH75)", GeoPoint(12.9779, 76.8330), "LANDMARK", radius_meters=400.0),
    NamedPlace("Arasikere", GeoPoint(13.3205, 76.2555), "LANDMARK", radius_meters=2000.0),
    # Singapore & Malaysia Transit Stations & Landmarks (Sep–Oct 2026 Trip)
    NamedPlace("Changi Airport T1/T2", GeoPoint(1.3593, 103.9903), "LANDMARK", radius_meters=600.0),
    NamedPlace(
        "YWCA Fort Canning Lodge (Dhoby Ghaut)",
        GeoPoint(1.2975, 103.8453),
        "LANDMARK",
        radius_meters=350.0,
    ),
    NamedPlace("Dhoby Ghaut MRT", GeoPoint(1.2989, 103.8458), "METRO_STATION", radius_meters=350.0),
    NamedPlace("Chinatown MRT", GeoPoint(1.2843, 103.8433), "METRO_STATION", radius_meters=350.0),
    NamedPlace("Raffles Place MRT", GeoPoint(1.2827, 103.8519), "METRO_STATION", radius_meters=350.0),
    NamedPlace(
        "Lau Pa Sat (18 Raffles Quay)", GeoPoint(1.2806, 103.8504), "LANDMARK", radius_meters=300.0
    ),
    NamedPlace(
        "Bayfront MRT (Marina Bay Sands)", GeoPoint(1.2823, 103.8591), "METRO_STATION", radius_meters=450.0
    ),
    NamedPlace("Gardens by the Bay", GeoPoint(1.2792, 103.8645), "LANDMARK", radius_meters=650.0),
    NamedPlace(
        "Esplanade Drive (Marina Bay)", GeoPoint(1.2901, 103.8555), "LANDMARK", radius_meters=400.0
    ),
    NamedPlace("City Hall / Funan", GeoPoint(1.2925, 103.8500), "LANDMARK", radius_meters=350.0),
    NamedPlace(
        "Science Centre Singapore (Jurong East)",
        GeoPoint(1.3337, 103.7363),
        "LANDMARK",
        radius_meters=500.0,
    ),
    NamedPlace(
        "VivoCity / HarbourFront MRT", GeoPoint(1.2651, 103.8222), "METRO_STATION", radius_meters=450.0
    ),
    NamedPlace(
        "Resorts World Sentosa (Universal Studios & Oceanarium)",
        GeoPoint(1.2562, 103.8215),
        "LANDMARK",
        radius_meters=550.0,
    ),
    NamedPlace(
        "Siloso Beach (Wings of Time), Sentosa",
        GeoPoint(1.2515, 103.8175),
        "LANDMARK",
        radius_meters=400.0,
    ),
    NamedPlace("Google Singapore (Mapletree Business City, Pasir Panjang)", GeoPoint(1.2756, 103.8000), "LANDMARK", radius_meters=450.0),
    NamedPlace("Labrador Park MRT", GeoPoint(1.2723, 103.8028), "METRO_STATION", radius_meters=350.0),
    NamedPlace("Buona Vista MRT", GeoPoint(1.3068, 103.7905), "METRO_STATION", radius_meters=350.0),
    NamedPlace("Kranji MRT", GeoPoint(1.4252, 103.7618), "METRO_STATION", radius_meters=350.0),
    NamedPlace("Woodlands Checkpoint", GeoPoint(1.4460, 103.7685), "LANDMARK", radius_meters=450.0),
    NamedPlace("Marine Parade MRT (i12 Katong)", GeoPoint(1.3043, 103.9060), "METRO_STATION", radius_meters=400.0),
    NamedPlace(
        "Betel Box Hostel (Joo Chiat Road)", GeoPoint(1.3122, 103.9002), "LANDMARK", radius_meters=350.0
    ),
    NamedPlace("Joo Chiat Complex (Geylang Serai)", GeoPoint(1.3162, 103.8982), "LANDMARK", radius_meters=350.0),
    NamedPlace("JB Sentral CIQ (Bukit Chagar)", GeoPoint(1.4645, 103.7655), "LANDMARK", radius_meters=500.0),
    NamedPlace(
        "Larkin Sentral Bus Terminal (Johor Bahru)",
        GeoPoint(1.4956, 103.7418),
        "LANDMARK",
        radius_meters=500.0,
    ),
    NamedPlace("Melaka Sentral Bus Terminal", GeoPoint(2.2208, 102.2500), "LANDMARK", radius_meters=500.0),
    NamedPlace(
        "Heng Ann Guest House (93 Jalan Laksamana Cheng Ho, Melaka)",
        GeoPoint(2.1950, 102.2591),
        "LANDMARK",
        radius_meters=350.0,
    ),
    NamedPlace(
        "Encore Melaka Theatre (Impression City, Klebang)",
        GeoPoint(2.1882, 102.2200),
        "LANDMARK",
        radius_meters=600.0,
    ),
    NamedPlace(
        "Jonker Walk & Dutch Square (Stadhuys, Bandar Hilir, Melaka)",
        GeoPoint(2.1955, 102.2495),
        "LANDMARK",
        radius_meters=450.0,
    ),
    NamedPlace(
        "Terminal Bersepadu Selatan (TBS / Bandar Tasik Selatan LRT)",
        GeoPoint(3.0765, 101.7112),
        "METRO_STATION",
        radius_meters=500.0,
    ),
    NamedPlace("Masjid Jamek LRT", GeoPoint(3.1495, 101.6964), "METRO_STATION", radius_meters=300.0),
    NamedPlace(
        "Swiss Hotel Kuala Lumpur (12 Jalan Tuanku Abdul Rahman)",
        GeoPoint(3.1515, 101.6953),
        "LANDMARK",
        radius_meters=250.0,
    ),
    NamedPlace(
        "Jalan Masjid India (Little India, Kuala Lumpur)",
        GeoPoint(3.1520, 101.6968),
        "LANDMARK",
        radius_meters=250.0,
    ),
    NamedPlace("Bank Negara KTM Station", GeoPoint(3.1545, 101.6928), "METRO_STATION", radius_meters=350.0),
    NamedPlace("Batu Caves KTM & Temple Complex", GeoPoint(3.2377, 101.6815), "LANDMARK", radius_meters=500.0),
    NamedPlace("Kuala Lumpur KTM Station", GeoPoint(3.1395, 101.6936), "METRO_STATION", radius_meters=350.0),
    NamedPlace(
        "Planetarium Negara (Perdana Botanical Gardens, KL)",
        GeoPoint(3.1395, 101.6887),
        "LANDMARK",
        radius_meters=400.0,
    ),
    NamedPlace("Central Market (Pasar Seni, Jalan Hang Kasturi)", GeoPoint(3.1462, 101.6957), "LANDMARK", radius_meters=300.0),
    NamedPlace("Medan Pasar (Old Market Square, KL)", GeoPoint(3.1485, 101.6962), "LANDMARK", radius_meters=250.0),
    NamedPlace("Suria KLCC & Petronas Twin Towers", GeoPoint(3.1573, 101.7122), "LANDMARK", radius_meters=450.0),
    NamedPlace("Medan Tuanku Monorail (Chow Kit, KL)", GeoPoint(3.1590, 101.6975), "METRO_STATION", radius_meters=400.0),
    NamedPlace(
        "KL Sentral &Axiata Tower (Google Malaysia)",
        GeoPoint(3.1342, 101.6861),
        "METRO_STATION",
        radius_meters=450.0,
    ),
    NamedPlace(
        "Awana SkyCentral & Genting Highlands Premium Outlets",
        GeoPoint(3.4040, 101.7827),
        "LANDMARK",
        radius_meters=600.0,
    ),
    NamedPlace(
        "SkyAvenue Mall, Resorts World Genting",
        GeoPoint(3.4237, 101.7945),
        "LANDMARK",
        radius_meters=600.0,
    ),
    NamedPlace("KLIA Terminal 1 (Sepang)", GeoPoint(2.7485, 101.7045), "LANDMARK", radius_meters=1200.0),
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
            if not any(skip in p.upper() for skip in ("KARNATAKA", "INDIA", "BENGALURU", "BANGALORE", "560"))
        ]
        if filtered:
            return filtered[-1]
        return parts[0] if parts else cleaned
