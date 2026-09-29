"""Infrastructure roll-up - "which networks keep appearing?" (analyst view).

Blockchain analysis answers *what moved*.  The network layer answers a different and
often more actionable question: **which hosting provider, ASN or country keeps showing
up behind the flagged wallets?**  Three providers carrying a third of the alerts is a
lead an investigator can act on today - a takedown request, a KYC query, a provider
conversation - where "this wallet looks bad" is only a starting point.

This module is deliberately small: it groups the evidence that already exists on the
alerts (and, optionally, the correlated packet rows) by ``geo_asn`` and
``geo_country``, and returns plain dictionaries the dashboard and the API can render
without any further processing.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence


def _blank_bucket(key: str, label: str, kind: str) -> Dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "kind": kind,               # "asn" | "country"
        "alerts": 0,
        "wallet_alerts": 0,
        "transaction_alerts": 0,
        "entities": [],
        "max_risk": 0.0,
        "correlated_packets": 0,
        "countries": set(),
        "orgs": set(),
        "asns": set(),          # which providers sit behind this bucket (used for countries)
    }


def _finish(bucket: Dict[str, Any]) -> Dict[str, Any]:
    entities = sorted(set(bucket["entities"]))
    return {
        **{k: v for k, v in bucket.items() if k not in ("entities", "countries", "orgs", "asns")},
        "entities": entities[:8],
        "entity_count": len(entities),
        "countries": sorted(bucket["countries"]),
        "orgs": sorted(bucket["orgs"]),
        "providers": len(bucket["asns"]),
        "max_risk": round(bucket["max_risk"], 4),
    }


def rollup(
    alerts: Sequence[Dict[str, Any]],
    correlated_pairs: Optional[Iterable[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Group alerts (and correlated packets) by ASN and by country.

    ``alerts`` may be the summary or the full packages - whatever the caller has.  An
    alert is counted once per distinct ASN it touches, so a wallet seen from three
    addresses in one provider contributes one to that provider, and
    ``alerts_with_network_evidence`` says how many alerts had network backing at all.
    """
    asn_buckets: Dict[str, Dict[str, Any]] = {}
    country_buckets: Dict[str, Dict[str, Any]] = {}
    with_network = 0

    for alert in alerts:
        evidence = alert.get("evidence") or {}
        records = evidence.get("network_evidence") or []
        seen_asns: set = set()
        seen_countries: set = set()
        if records:
            with_network += 1
        for record in records:
            asn = str(record.get("geo_asn") or "unknown")
            country = str(record.get("geo_country") or "??")
            org = str(record.get("asn_org") or "unknown operator")
            seen_asns.add(asn)
            seen_countries.add(country)
            bucket = asn_buckets.setdefault(asn, _blank_bucket(asn, org, "asn"))
            bucket["orgs"].add(org)
            bucket["countries"].add(country)
            bucket["asns"].add(asn)
            if country != "??":
                country_bucket = country_buckets.setdefault(
                    country, _blank_bucket(country, country, "country")
                )
                country_bucket["asns"].add(asn)
                country_bucket["orgs"].add(org)

        for key, buckets in ((seen_asns, asn_buckets), (seen_countries, country_buckets)):
            for name in key:
                bucket = buckets.get(name)
                if bucket is None:
                    continue
                bucket["alerts"] += 1
                if alert.get("entity_type") == "wallet":
                    bucket["wallet_alerts"] += 1
                else:
                    bucket["transaction_alerts"] += 1
                bucket["entities"].append(alert.get("entity"))
                bucket["max_risk"] = max(bucket["max_risk"], float(alert.get("risk_score") or 0.0))

    for pair in correlated_pairs or []:
        asn = str(pair.get("geo_asn") or "unknown")
        country = str(pair.get("geo_country") or "??")
        if asn in asn_buckets:
            asn_buckets[asn]["correlated_packets"] += 1
        if country in country_buckets:
            country_buckets[country]["correlated_packets"] += 1

    providers = [_finish(bucket) for bucket in asn_buckets.values()]
    providers = [row for row in providers if row["alerts"] > 0]
    providers.sort(key=lambda row: (row["alerts"], row["max_risk"]), reverse=True)

    countries = [_finish(bucket) for bucket in country_buckets.values()]
    countries = [row for row in countries if row["alerts"] > 0]
    countries.sort(key=lambda row: (row["alerts"], row["max_risk"]), reverse=True)

    total_alerts = len(alerts)
    top_providers = providers[:3]
    covered = sum(row["alerts"] for row in top_providers)
    if providers:
        headline = (
            f"{len(providers)} hosting networks appear behind {total_alerts} alerts. "
            f"The top three ({', '.join(row['label'] for row in top_providers)}) carry "
            f"{covered} linked alerts between them - start the provider conversation there."
        )
    else:
        headline = (
            "No alert carries network evidence yet, so there is no infrastructure lead "
            "to roll up. Run an analysis with network metadata included."
        )

    return {
        "headline": headline,
        "providers": providers,
        "countries": countries,
        "totals": {
            "alerts": total_alerts,
            "alerts_with_network_evidence": with_network,
            "distinct_asns": len(providers),
            "distinct_countries": len(countries),
            "cross_border_countries": sum(
                1 for row in countries if row["correlated_packets"] >= 0
            ),
        },
    }


def focus_list(rollup_result: Dict[str, Any], limit: int = 5) -> List[Dict[str, Any]]:
    """The shortest possible "what next" list: the top providers, one line each."""
    out: List[Dict[str, Any]] = []
    for index, row in enumerate(rollup_result.get("providers", [])[:limit], start=1):
        out.append(
            {
                "rank": index,
                "provider": row["label"],
                "asn": row["key"],
                "countries": ", ".join(row["countries"]) or "-",
                "alerts": row["alerts"],
                "max_risk": row["max_risk"],
                "action": (
                    "Ask the provider to confirm subscriber data for the flagged sessions"
                    if row["alerts"] >= 3
                    else "Monitor - low overlap so far"
                ),
            }
        )
    return out
