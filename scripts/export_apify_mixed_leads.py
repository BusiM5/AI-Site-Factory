from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "data" / "exports"
OUTPUT_BASENAME = "apify-mixed-leads-100-2026-07-17"
TARGET_COUNT = 100
RESULTS_PER_INDUSTRY = 8
LOCATION = "South Africa"

SEARCH_TERMS = [
    "plumber",
    "electrician",
    "hair salon",
    "auto repair shop",
    "cleaning service",
    "landscaper",
    "restaurant",
    "dentist",
    "gym",
    "roofer",
    "pest control service",
    "accountant",
    "solar energy contractor",
    "locksmith",
    "bakery",
    "physiotherapist",
    "photographer",
    "construction company",
    "moving company",
    "beauty salon",
]

EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CSV_FIELDS = [
    "leadNumber",
    "businessName",
    "industry",
    "category",
    "address",
    "city",
    "state",
    "postalCode",
    "countryCode",
    "location",
    "phone",
    "email",
    "contactChannel",
    "website",
    "sourceUrl",
    "placeId",
    "rating",
    "reviewsCount",
    "mainImageUrl",
    "latitude",
    "longitude",
    "searchTerm",
    "canonicalLeadKey",
]


def text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def first(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, list):
            value = value[0] if value else ""
        normalized = text(value)
        if normalized:
            return normalized
    return ""


def valid_phone(value: Any) -> str:
    phone = text(value)
    digits = re.sub(r"\D", "", phone)
    return phone if 7 <= len(digits) <= 15 else ""


def valid_email(item: dict[str, Any]) -> str:
    candidates: list[str] = []
    for key in ("email", "contactEmail", "mail"):
        candidates.extend(EMAIL_PATTERN.findall(text(item.get(key))))
    for key in ("emails", "emailAddresses"):
        value = item.get(key)
        if isinstance(value, list):
            for entry in value:
                candidates.extend(EMAIL_PATTERN.findall(text(entry)))
        else:
            candidates.extend(EMAIL_PATTERN.findall(text(value)))
    return candidates[0].lower() if candidates else ""


def has_website(item: dict[str, Any]) -> bool:
    return any(first(item, key) for key in ("website", "site", "homepage", "domain"))


def main_image(item: dict[str, Any]) -> str:
    direct = first(item, "imageUrl", "mainImage", "mainImageUrl", "thumbnailUrl")
    if direct:
        return direct
    images = item.get("imageUrls") or item.get("images") or []
    if isinstance(images, list) and images:
        first_image = images[0]
        if isinstance(first_image, dict):
            return first(first_image, "imageUrl", "url", "src")
        return text(first_image)
    return ""


def coordinates(item: dict[str, Any]) -> tuple[Any, Any]:
    location = item.get("location") if isinstance(item.get("location"), dict) else {}
    latitude = item.get("latitude") or item.get("lat") or location.get("lat")
    longitude = item.get("longitude") or item.get("lng") or location.get("lng")
    return latitude, longitude


def normalized_record(item: dict[str, Any]) -> dict[str, Any] | None:
    if has_website(item):
        return None

    business_name = first(item, "title", "name", "businessName", "placeName", "companyName")
    phone = valid_phone(first(item, "phone", "phoneUnformatted", "contactPhone", "telephone"))
    email = valid_email(item)
    if not business_name or not (phone or email):
        return None

    place_id = first(item, "placeId", "place_id", "googlePlaceId", "googleId", "cid", "fid")
    source_url = first(item, "googleMapsUrl", "placeUrl", "searchPageUrl", "url")
    address = first(item, "address", "street", "fullAddress", "formattedAddress")
    city = first(item, "city", "neighborhood")
    state = first(item, "state", "province")
    country_code = first(item, "countryCode") or "ZA"
    category = first(item, "categoryName", "category", "primaryCategory", "type")
    search_term = first(item, "searchString", "searchTerm", "query") or category
    latitude, longitude = coordinates(item)
    identity = place_id or source_url or f"{business_name}|{phone}|{address}"
    canonical_key = hashlib.sha1(identity.lower().encode("utf-8")).hexdigest()[:16]

    if email and phone:
        channel = "email_and_phone"
    elif email:
        channel = "email"
    else:
        channel = "phone"

    return {
        "leadNumber": 0,
        "businessName": business_name,
        "industry": search_term or category,
        "category": category,
        "address": address,
        "city": city,
        "state": state,
        "postalCode": first(item, "postalCode", "zip"),
        "countryCode": country_code,
        "location": ", ".join(value for value in (city, state, "South Africa") if value),
        "phone": phone,
        "email": email,
        "contactChannel": channel,
        "website": "",
        "sourceUrl": source_url,
        "placeId": place_id,
        "rating": item.get("totalScore") or item.get("rating") or item.get("stars") or "",
        "reviewsCount": item.get("reviewsCount") or item.get("numberOfReviews") or "",
        "mainImageUrl": main_image(item),
        "latitude": latitude if latitude is not None else "",
        "longitude": longitude if longitude is not None else "",
        "searchTerm": search_term,
        "canonicalLeadKey": canonical_key,
    }


def run_actor(token: str, actor_id: str) -> list[dict[str, Any]]:
    actor_path = actor_id.replace("/", "~")
    max_items = len(SEARCH_TERMS) * RESULTS_PER_INDUSTRY
    url = (
        f"https://api.apify.com/v2/actors/{actor_path}/run-sync-get-dataset-items"
        f"?clean=true&format=json&timeout=300&maxItems={max_items}"
    )
    payload = {
        "searchStringsArray": SEARCH_TERMS,
        "locationQuery": LOCATION,
        "countryCode": "za",
        "maxCrawledPlacesPerSearch": RESULTS_PER_INDUSTRY,
        "language": "en",
        "website": "withoutWebsite",
        "skipClosedPlaces": True,
        "scrapePlaceDetailPage": False,
        "maximumLeadsEnrichmentRecords": 0,
        "maxReviews": 0,
        "maxImages": 1,
        "maxCompetitorsToAnalyze": 0,
    }
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=600,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError("Apify returned an unexpected dataset response.")
    return data


def fetch_dataset(token: str, dataset_id: str) -> list[dict[str, Any]]:
    response = requests.get(
        f"https://api.apify.com/v2/datasets/{dataset_id}/items",
        headers={"Authorization": f"Bearer {token}"},
        params={"clean": "true", "format": "json", "limit": 1000},
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError(f"Apify dataset {dataset_id} returned an unexpected response.")
    return data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset-id",
        action="append",
        default=[],
        help="Reuse an existing Apify dataset instead of launching another paid Actor run.",
    )
    args = parser.parse_args()
    load_dotenv(ROOT / "backend" / ".env", override=True)
    token = text(os.getenv("APIFY_API_TOKEN"))
    actor_id = text(os.getenv("APIFY_GOOGLE_MAPS_ACTOR_ID")) or "compass/crawler-google-places"
    if not token:
        raise RuntimeError("APIFY_API_TOKEN is missing from backend/.env")

    if args.dataset_id:
        raw_items = []
        for dataset_id in args.dataset_id:
            raw_items.extend(fetch_dataset(token, dataset_id))
    else:
        raw_items = run_actor(token, actor_id)
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        record = normalized_record(item)
        if not record:
            continue
        identity = record["placeId"] or record["sourceUrl"] or record["canonicalLeadKey"]
        if identity in seen:
            continue
        seen.add(identity)
        records.append(record)
        if len(records) == TARGET_COUNT:
            break

    if len(records) < TARGET_COUNT:
        raise RuntimeError(
            f"Only {len(records)} qualified leads were returned from {len(raw_items)} scraped places; "
            "increase RESULTS_PER_INDUSTRY and run again."
        )

    for index, record in enumerate(records, start=1):
        record["leadNumber"] = index

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUTPUT_DIR / f"{OUTPUT_BASENAME}.json"
    csv_path = OUTPUT_DIR / f"{OUTPUT_BASENAME}.csv"
    json_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(records)

    email_count = sum(bool(record["email"]) for record in records)
    phone_count = sum(bool(record["phone"]) for record in records)
    both_count = sum(bool(record["email"] and record["phone"]) for record in records)
    industries = sorted({record["industry"] for record in records if record["industry"]})
    print(
        json.dumps(
            {
                "rawItems": len(raw_items),
                "exported": len(records),
                "withEmail": email_count,
                "withPhone": phone_count,
                "withBoth": both_count,
                "industries": industries,
                "json": str(json_path),
                "csv": str(csv_path),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
