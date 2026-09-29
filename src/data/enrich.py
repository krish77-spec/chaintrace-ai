"""Stage 2 - Enrich.

Offline GeoIP + a handful of cheap derived columns (spec section 8).

Order of preference:

1. MaxMind GeoLite2 ``.mmdb`` files dropped into ``data/geo/`` (city + ASN).
2. A deterministic mock mapping (``mock_geoip.json``) derived from a hash of the
   IP address, so the same IP always resolves to the same country/ASN.

No external service is ever contacted - that is a hard constraint of the brief.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from src import config

logger = logging.getLogger(__name__)

# A small but plausible offline country/ASN table.  Chosen to cover common
# hosting/VPN jurisdictions plus a few high-risk-of-abuse registries, which keeps
# the "same host, different country" investigative angle interesting.
GEO_TABLE: List[Tuple[str, str, str, str]] = [
    # country_code, country_name, asn, asn_org
    ("NL", "Netherlands", "AS9009", "M247 Europe SRL"),
    ("DE", "Germany", "AS24940", "Hetzner Online GmbH"),
    ("US", "United States", "AS16509", "Amazon.com Inc."),
    ("SG", "Singapore", "AS14061", "DigitalOcean LLC"),
    ("RU", "Russian Federation", "AS200350", "Yandex Cloud"),
    ("CN", "China", "AS4134", "Chinanet"),
    ("SE", "Sweden", "AS13335", "Cloudflare Inc."),
    ("PA", "Panama", "AS266706", "Offshore Hosting Ltd"),
    ("SC", "Seychelles", "AS328543", "Island Datacom Ltd"),
    ("IN", "India", "AS9498", "Bharti Airtel Ltd"),
    ("GB", "United Kingdom", "AS2856", "British Telecommunications PLC"),
    ("BR", "Brazil", "AS27699", "TELEFONICA BRASIL S.A."),
    ("UA", "Ukraine", "AS13188", "Kyivstar PJSC"),
    ("TR", "Turkey", "AS9121", "Turk Telekomunikasyon A.S."),
    ("HK", "Hong Kong", "AS45102", "Alibaba Cloud LLC"),
    ("CH", "Switzerland", "AS3303", "Swisscom AG"),
]

PRIVATE_NETWORKS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"]
PRIVATE_NETS = [ipaddress.ip_network(cidr) for cidr in PRIVATE_NETWORKS]


def is_private_ip(ip: Optional[str]) -> bool:
    if not ip:
        return True
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return any(address in network for network in PRIVATE_NETS) or address.is_private


def ip_version(ip: Optional[str]) -> Optional[int]:
    if not ip:
        return None
    try:
        return ipaddress.ip_address(ip).version
    except ValueError:
        return None


def _hash_country(ip: str) -> Tuple[str, str, str, str]:
    digest = hashlib.sha256(ip.encode("utf-8")).hexdigest()
    index = int(digest[:8], 16) % len(GEO_TABLE)
    return GEO_TABLE[index]


class GeoIPEnricher:
    """Resolves IPs to (country, ASN) fully offline."""

    def __init__(self, geo_dir: Optional[Path | str] = None, persist_mock: bool = True) -> None:
        self.geo_dir = Path(geo_dir or config.GEO_DIR)
        self.geo_dir.mkdir(parents=True, exist_ok=True)
        self.city_reader = None
        self.asn_reader = None
        self.mode = "mock"
        self.cache: Dict[str, Dict[str, Optional[str]]] = {}
        self._persist_mock = persist_mock
        self._load_mmdb()

    # ------------------------------------------------------------------ #
    def _load_mmdb(self) -> None:
        try:
            import geoip2.database  # type: ignore
        except Exception:
            logger.info("geoip2 not installed - using deterministic mock GeoIP table")
            return
        try:
            if config.GEOLITE2_CITY_DB.exists():
                self.city_reader = geoip2.database.Reader(str(config.GEOLITE2_CITY_DB))
                self.mode = "geolite2"
            if config.GEOLITE2_ASN_DB.exists():
                self.asn_reader = geoip2.database.Reader(str(config.GEOLITE2_ASN_DB))
                self.mode = "geolite2"
        except Exception as exc:  # pragma: no cover - depends on local files
            logger.warning("Could not open GeoLite2 database (%s) - falling back to mock", exc)
            self.mode = "mock"

    # ------------------------------------------------------------------ #
    def lookup(self, ip: Optional[str]) -> Dict[str, Optional[str]]:
        if not ip:
            return {"geo_country": None, "geo_asn": None, "asn_org": None}
        if ip in self.cache:
            return self.cache[ip]

        result: Dict[str, Optional[str]] = {"geo_country": None, "geo_asn": None, "asn_org": None}
        if self.mode == "geolite2":
            try:
                if self.city_reader is not None:
                    city = self.city_reader.city(ip)
                    result["geo_country"] = city.country.iso_code
                if self.asn_reader is not None:
                    asn = self.asn_reader.asn(ip)
                    result["geo_asn"] = f"AS{asn.autonomous_system_number}"
                    result["asn_org"] = asn.autonomous_system_organization
            except Exception:
                pass  # unknown IP -> fall through to mock mapping

        if not result["geo_country"]:
            code, name, asn, org = _hash_country(ip)
            result["geo_country"] = code
            result["geo_asn"] = asn
            result["asn_org"] = org
        result["geo_country_name"] = dict((c, n) for c, n, _, _ in GEO_TABLE).get(
            result["geo_country"], result["geo_country"]
        )

        self.cache[ip] = result
        return result

    # ------------------------------------------------------------------ #
    def mock_mapping(self, ips: List[str]) -> Dict[str, Dict[str, Any]]:
        return {ip: self.lookup(ip) for ip in sorted(set(ips)) if ip}

    def persist_mock_mapping(self, ips: List[str]) -> Optional[Path]:
        if not self._persist_mock:
            return None
        mapping = self.mock_mapping(ips)
        path = config.MOCK_GEO_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "mode": self.mode,
            "description": (
                "Offline IP -> country/ASN mapping used by the prototype. "
                "Generated deterministically from a SHA-256 of each IP so results are stable "
                "across runs. Drop GeoLite2-City.mmdb / GeoLite2-ASN.mmdb into data/geo/ to "
                "use real MaxMind data instead."
            ),
            "entries": mapping,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path


# --------------------------------------------------------------------------- #
# frame-level enrichment
# --------------------------------------------------------------------------- #
def _prefer_existing(frame: pd.DataFrame, column: str, fallback: str) -> pd.Series:
    """Return ``column`` when it is populated, otherwise the freshly resolved one."""
    resolved = frame.get(fallback)
    if resolved is None:
        return frame.get(column)
    if column not in frame.columns:
        return resolved
    provided = frame[column]
    missing = provided.isna() | (provided.astype(str).str.strip() == "")
    out = provided.where(~missing, resolved)
    return out.where(~out.isna(), None)


def enrich_network(df: pd.DataFrame, enricher: Optional[GeoIPEnricher] = None) -> pd.DataFrame:
    """Add geo_country / geo_asn (and a few cheap features) to network records."""
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    enricher = enricher or GeoIPEnricher()
    out = df.copy()

    ips: List[str] = []
    for column in ("src_ip", "dst_ip"):
        if column in out.columns:
            ips.extend([ip for ip in out[column].tolist() if ip])

    def _geo(ip: Optional[str]) -> pd.Series:
        info = enricher.lookup(ip)
        return pd.Series(
            {
                "geo_country": info.get("geo_country"),
                "geo_asn": info.get("geo_asn"),
                "asn_org": info.get("asn_org"),
            }
        )

    geo_frame = pd.DataFrame(
        [_geo(ip) for ip in out.get("src_ip", pd.Series(dtype=object)).tolist()]
    )
    if not geo_frame.empty:
        geo_frame.columns = ["src_geo_country", "src_geo_asn", "src_asn_org"]
        out = pd.concat([out.reset_index(drop=True), geo_frame.reset_index(drop=True)], axis=1)

    dst_geo = pd.DataFrame(
        [_geo(ip) for ip in out.get("dst_ip", pd.Series(dtype=object)).tolist()]
    )
    if not dst_geo.empty:
        dst_geo.columns = ["dst_geo_country", "dst_geo_asn", "dst_asn_org"]
        out = pd.concat([out.reset_index(drop=True), dst_geo.reset_index(drop=True)], axis=1)

    out["src_is_private"] = out.get("src_ip", pd.Series(dtype=object)).apply(is_private_ip)
    out["dst_is_private"] = out.get("dst_ip", pd.Series(dtype=object)).apply(is_private_ip)
    out["ip_version"] = out.get("src_ip", pd.Series(dtype=object)).apply(ip_version)
    if "src_geo_country" in out.columns and "dst_geo_country" in out.columns:
        out["cross_border"] = (
            out["src_geo_country"].notna()
            & out["dst_geo_country"].notna()
            & (out["src_geo_country"] != out["dst_geo_country"])
        )
    # spec column names.  A dataset that already carries geo_country/geo_asn (the
    # SIH26146 minimum-field list includes them) keeps its own values; the lookup
    # only fills the gaps, so an operator's real GeoIP column is never overwritten
    # by the offline fallback table.
    out["geo_country"] = _prefer_existing(out, "geo_country", "src_geo_country")
    out["geo_asn"] = _prefer_existing(out, "geo_asn", "src_geo_asn")

    enricher.persist_mock_mapping(ips)
    logger.info(
        "Enriched %d network records with offline GeoIP (%s)", len(out), enricher.mode
    )
    return out


def enrich_transactions_with_geo(
    transactions: pd.DataFrame, correlated: pd.DataFrame
) -> pd.DataFrame:
    """Copy geo context from correlated network records onto transactions."""
    if transactions is None or transactions.empty or correlated is None or correlated.empty:
        return transactions
    out = transactions.copy()
    geo_columns = [c for c in ("txid", "geo_country", "geo_asn") if c in correlated.columns]
    if "geo_country" not in geo_columns:
        return out
    per_tx = (
        correlated[geo_columns]
        .dropna(subset=["txid"])
        .drop_duplicates(subset=["txid"])
        .set_index("txid")
    )
    out["geo_country"] = out["txid"].map(per_tx["geo_country"])
    if "geo_asn" in per_tx.columns:
        out["geo_asn"] = out["txid"].map(per_tx["geo_asn"])
    return out


def build_enrichment_summary(network: pd.DataFrame) -> Dict[str, Any]:
    """Small dict of counters the dashboard/API can show for stage 2."""
    if network is None or network.empty:
        return {"records": 0, "countries": 0, "asns": 0, "cross_border": 0}
    return {
        "records": int(len(network)),
        "countries": int(network.get("geo_country", pd.Series(dtype=object)).nunique()),
        "asns": int(network.get("geo_asn", pd.Series(dtype=object)).nunique()),
        "cross_border": int(network.get("cross_border", pd.Series(dtype=bool)).sum()),
    }
