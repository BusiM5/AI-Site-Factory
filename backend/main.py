from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from uuid import NAMESPACE_URL, uuid4, uuid5
import base64
import binascii
from collections import deque
import csv
import hashlib
import hmac
import html
import io
import json
import logging
import re
import sqlite3
import threading
import time
import zipfile
from urllib.parse import urlparse
import os
from dotenv import load_dotenv

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field


app = FastAPI(title="AI Site Factory Backend - Phase 1")


def load_environment() -> None:
    """Load local env files without overriding values already set by the host."""
    backend_env = os.path.join(os.path.dirname(__file__), ".env")
    root_env = os.path.join(os.path.dirname(__file__), "..", ".env")
    for env_path in [backend_env, root_env]:
        if os.path.exists(env_path):
            load_dotenv(env_path, override=False)


load_environment()

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("ai_site_factory")
logger.setLevel(os.getenv("APP_LOG_LEVEL", "INFO").upper())
logger.handlers.clear()

log_formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
file_handler = logging.FileHandler(os.path.join(LOG_DIR, "backend.log"), encoding="utf-8")
file_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)
logger.addHandler(file_handler)

STARTED_AT = datetime.now(timezone.utc)
LOG_BUFFER = deque(maxlen=int(os.getenv("APP_LOG_BUFFER_SIZE", "250")))
SENSITIVE_KEY_PATTERN = re.compile(r"(token|key|secret|password|authorization|auth|email)", re.IGNORECASE)
MODEL_CHUNK_CHARS = int(os.getenv("MODEL_CHUNK_CHARS", "1800"))
MODEL_MAX_CHUNKS = int(os.getenv("MODEL_MAX_CHUNKS", "4"))
RUNTIME_INTEGRATION_OVERRIDES: Dict[str, str] = {}
API_SAFETY_CHECK_RESULTS: Dict[str, Dict[str, Any]] = {}
ADMIN_SESSION_COOKIE = "asf_admin_session"
ADMIN_LOGIN_ATTEMPTS: Dict[str, List[float]] = {}
ADMIN_SESSION_MAX_AGE_SECONDS = int(os.getenv("ADMIN_SESSION_MAX_AGE_SECONDS", str(8 * 60 * 60)))
BACKGROUND_JOB_LOCK = threading.Lock()
ACTIVE_CAMPAIGN_INTAKE_JOBS: Set[str] = set()
ACTIVE_CAMPAIGN_IMPORT_JOBS: Set[str] = set()
ACTIVE_CAMPAIGN_ZENDESK_SYNCS: Set[str] = set()


def admin_auth_settings() -> Dict[str, Any]:
    username = compact_text(os.getenv("ADMIN_USERNAME"), "admin")
    password_hash = compact_text(os.getenv("ADMIN_PASSWORD_HASH"))
    password = os.getenv("ADMIN_PASSWORD") or ""
    return {
        "configured": bool(username and (password_hash or password)),
        "username": username,
        "passwordHash": password_hash,
        "password": password,
        "source": "password-hash" if password_hash else "password" if password else "not-configured",
    }


def hash_admin_password(password: str, iterations: int = 600_000, salt: Optional[bytes] = None) -> str:
    if len(password or "") < 12:
        raise ValueError("The administrator password must contain at least 12 characters.")
    salt_value = salt or os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_value, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.urlsafe_b64encode(salt_value).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(derived).decode("ascii").rstrip("="),
    )


def verify_admin_password(password: str, settings: Optional[Dict[str, Any]] = None) -> bool:
    settings = settings or admin_auth_settings()
    stored_hash = compact_text(settings.get("passwordHash"))
    if stored_hash:
        try:
            algorithm, iterations_text, salt_text, digest_text = stored_hash.split("$", 3)
            if algorithm != "pbkdf2_sha256":
                return False
            padded_salt = salt_text + "=" * (-len(salt_text) % 4)
            padded_digest = digest_text + "=" * (-len(digest_text) % 4)
            salt = base64.urlsafe_b64decode(padded_salt.encode("ascii"))
            expected = base64.urlsafe_b64decode(padded_digest.encode("ascii"))
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations_text))
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError, binascii.Error):
            return False
    return bool(settings.get("password")) and hmac.compare_digest(password, str(settings["password"]))


def admin_session_secret(settings: Optional[Dict[str, Any]] = None) -> bytes:
    settings = settings or admin_auth_settings()
    supplied = os.getenv("ADMIN_SESSION_SECRET") or ""
    if supplied:
        return supplied.encode("utf-8")
    password_material = settings.get("passwordHash") or settings.get("password") or "unconfigured"
    return hashlib.sha256(f"ai-site-factory-session:{password_material}".encode("utf-8")).digest()


def admin_credential_version(settings: Optional[Dict[str, Any]] = None) -> str:
    settings = settings or admin_auth_settings()
    password_material = settings.get("passwordHash") or settings.get("password") or "unconfigured"
    return hashlib.sha256(str(password_material).encode("utf-8")).hexdigest()[:16]


def issue_admin_session(username: str, settings: Optional[Dict[str, Any]] = None) -> str:
    settings = settings or admin_auth_settings()
    expires_at = int(time.time()) + ADMIN_SESSION_MAX_AGE_SECONDS
    payload = json.dumps(
        {
            "username": username,
            "expiresAt": expires_at,
            "credentialVersion": admin_credential_version(settings),
            "nonce": os.urandom(8).hex(),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    payload_text = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(admin_session_secret(settings), payload_text.encode("ascii"), hashlib.sha256).digest()
    signature_text = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{payload_text}.{signature_text}"


def read_admin_session(token: Optional[str], settings: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    settings = settings or admin_auth_settings()
    try:
        payload_text, signature_text = token.split(".", 1)
        expected = hmac.new(admin_session_secret(settings), payload_text.encode("ascii"), hashlib.sha256).digest()
        supplied = base64.urlsafe_b64decode((signature_text + "=" * (-len(signature_text) % 4)).encode("ascii"))
        if not hmac.compare_digest(expected, supplied):
            return None
        payload = json.loads(
            base64.urlsafe_b64decode((payload_text + "=" * (-len(payload_text) % 4)).encode("ascii"))
        )
        if int(payload.get("expiresAt") or 0) <= int(time.time()):
            return None
        if compact_text(payload.get("username")) != settings["username"]:
            return None
        if not hmac.compare_digest(
            compact_text(payload.get("credentialVersion")),
            admin_credential_version(settings),
        ):
            return None
        return payload
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, binascii.Error):
        return None


def admin_cookie_secure() -> bool:
    return bool(os.getenv("RENDER") or os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_BACKEND_URL"))


AUTH_PUBLIC_PATHS = {
    "/",
    "/favicon.ico",
    "/api/health",
    "/api/auth/session",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/zendesk/webhook",
    "/api/deployments/refresh-business-media",
}


class GeminiRateLimitError(RuntimeError):
    """Gemini quota is temporarily unavailable."""


class GeminiTransientError(RuntimeError):
    """Gemini failed after retryable transport or server errors."""


REQUIRED_PROVIDER_ENV = {
    "apify": ["APIFY_API_TOKEN"],
    "gemini": ["GEMINI_API_KEY"],
    "groq": ["GROQ_API_KEY"],
    "netlify": ["NETLIFY_AUTH_TOKEN"],
    "zendesk": ["ZENDESK_SUBDOMAIN", "ZENDESK_EMAIL", "ZENDESK_API_TOKEN"],
    "github": ["GITHUB_OWNER", "GITHUB_TOKEN"],
}

VALID_PUBLISH_MODES = {"github-netlify", "direct-netlify", "direct-netlify-fallback"}
FREEFORM_TEMPLATE_ID = "gemini-freeform"
NETLIFY_GITHUB_INSTALLATION_DEFAULTS = {"busim5": 99999032}

FREEFORM_SITE_SPEC = {
    "id": FREEFORM_TEMPLATE_ID,
    "name": "Gemini Freeform",
    "description": "Gemini controls page structure and visual direction; backend enforces the highly interactive profile, business facts, SEO, and safety features.",
    "accent": "#0f9f96",
    "background": "#f8fbff",
    "siteProfile": "highly-interactive",
}

HIGHLY_INTERACTIVE_SITE_PROFILE = {
    "id": "highly-interactive",
    "name": "Highly Interactive",
    "libraries": {
        "layout": "Bootstrap 5.3.8",
        "interaction": "Alpine.js 3.15.12",
        "heroAnimation": "GSAP 3.15",
        "scrollAnimation": "Motion Mini 12.42.2",
    },
    "features": [
        "business detail tabs",
        "fact-based FAQ accordions",
        "theme controls",
        "reduced-motion-safe hero animation",
        "viewport-triggered section reveals",
        "SEO validation gate",
    ],
}
FREEFORM_SITE_SPEC["profile"] = HIGHLY_INTERACTIVE_SITE_PROFILE

LANDING_PAGE_PROMPT_HEADER = """
You are creating a production-ready, single-file HTML landing page for a business with no current website.
Return strict JSON with keys: html, qaNotes, structureNotes, stylingLibraries.
The html value must be a complete <!doctype html> document.

Rules:
- Use only the supplied public lead/business information. Do not invent awards, prices, guarantees, team members, certifications, or unavailable services.
- Gemini may choose the page structure, copy hierarchy, and styling direction.
- Use Bootstrap 5.3.8 as the only styling framework. Do not use the Tailwind browser CDN or Animate.css.
- Implement the highly interactive profile with Alpine.js 3.15.12 for stateful components, GSAP 3.15 for hero choreography, and Motion Mini 12.42.2 for viewport reveals and micro-interactions. Respect prefers-reduced-motion.
- Include a visible dynamic color/theme widget that lets visitors change site colors and persists selections with localStorage.
- Include accessible semantic sections, mobile-first responsive layout, clear contact options, and grounded calls to action.
- Include a visible Business Details section that explicitly states every supplied fact that is useful to a visitor: business name, industry/category, location, address, phone, email, rating/review count, source listing, and main image. Omit unavailable values instead of inventing them.
- Include crawlable Alpine-powered detail tabs and fact-based FAQ accordions. Interactive content must remain present in the HTML source and usable without animation.
- Include one descriptive h1, a unique title, a concise meta description, robots metadata, Open Graph metadata, and LocalBusiness JSON-LD based only on supplied facts. The backend SEO gate will normalize and validate them.
- When mainImageUrl is supplied, use that exact public business-listing image as the prominent hero/banner image. Do not add, generate, or substitute any other image. When mainImageUrl is unavailable, keep the page image-free instead of inventing an image or using unrelated stock photography.
- Use the supplied brandTheme as the default page palette. Keep the colours appropriate to the business industry and maintain accessible text contrast; the colour widget may let visitors override those defaults.
- Treat the supplied businessProfile as the authoritative copy plan. Use its tagline, services heading, services intro, and four distinct services; weave in the business name and location naturally. Never repeat generic cards such as "Local service" when a specific profile is available.
- Personalise the copy with concrete supplied details such as business name, category/industry, city/location, address, rating/review count, source listing, phone, email, service keywords, differentiators, and proof points when they are present. Avoid generic filler when a supplied detail is available.
- CTA buttons must have working destinations: use mailto: for email leads, tel: for phone leads, the supplied website URL if present, or valid in-page anchors such as #services and #contact. Do not use empty hrefs, href="#", javascript:void(0), or non-functional buttons.
- Do not include external tracking pixels, forms that submit data, or claims of consent.
""".strip()

DEFAULT_BUSINESS_THEME = {
    "text": "#102033",
    "background": "#f8fbff",
    "highlight": "#0f9f96",
    "name": "local-service",
}

BUSINESS_THEME_RULES = [
    (("physio", "health", "medical", "clinic", "dental", "dentist", "dentistry", "wellness", "therapy"), "#0f766e", "#f0fdfa", "healthcare"),
    (("restaurant", "food", "cafe", "bakery", "catering", "takeaway"), "#b45309", "#fff7ed", "hospitality"),
    (("plumb", "water", "hvac", "air condition", "electric", "repair", "locksmith"), "#0369a1", "#f0f9ff", "trade-services"),
    (("beauty", "salon", "hair", "nail", "spa", "cosmetic"), "#be185d", "#fff1f2", "beauty"),
    (("fitness", "gym", "sport", "training"), "#c2410c", "#fff7ed", "fitness"),
    (("landscap", "garden", "pest", "cleaning", "environment"), "#15803d", "#f0fdf4", "home-and-garden"),
    (("account", "legal", "finance", "consult", "insurance", "property"), "#1d4ed8", "#eff6ff", "professional-services"),
    (("photo", "creative", "design", "studio", "event", "media"), "#7c3aed", "#f5f3ff", "creative"),
    (("auto", "vehicle", "mechanic", "transport", "courier"), "#b91c1c", "#fef2f2", "automotive"),
]

BUSINESS_THEME_FALLBACKS = [
    ("#0f766e", "#f0fdfa", "teal"),
    ("#0369a1", "#f0f9ff", "blue"),
    ("#7c3aed", "#f5f3ff", "violet"),
    ("#b45309", "#fff7ed", "amber"),
    ("#15803d", "#f0fdf4", "green"),
]

GENERIC_INDUSTRY_LABELS = {
    "",
    "business",
    "local business",
    "local service",
    "local services",
    "mixed industry",
    "mixed industries",
    "service",
    "services",
    "unknown",
}

BUSINESS_PROFILE_RULES = [
    {
        "keywords": ("physio", "physical therapy"),
        "industry": "Physiotherapy",
        "tagline": "Move with confidence. Recover with care.",
        "servicesHeading": "Physiotherapy support for movement and recovery",
        "services": [
            ("Physiotherapy Care", "Professional support centred on movement, comfort, and everyday function."),
            ("Mobility Support", "Practical guidance that helps customers work towards easier, more confident movement."),
            ("Recovery-Focused Support", "A considered approach for people navigating recovery and returning to daily activities."),
            ("Movement Guidance", "Clear next steps and customer-focused guidance for individual movement needs."),
        ],
    },
    {
        "keywords": ("account", "bookkeep", "tax", "financial"),
        "industry": "Accounting",
        "tagline": "Clear numbers. Confident business decisions.",
        "servicesHeading": "Practical accounting support for growing businesses",
        "services": [
            ("Business Accounting", "Clear, organised accounting support designed around day-to-day business needs."),
            ("Bookkeeping Support", "Practical assistance for keeping financial records accurate, current, and easy to understand."),
            ("Financial Record Organisation", "Structured support that turns business records into clearer financial information."),
            ("Business Advisory", "Grounded financial guidance to help business owners consider their next steps with confidence."),
        ],
    },
    {
        "keywords": ("restaurant", "food", "cafe", "bakery", "catering", "takeaway"),
        "industry": "Food & Hospitality",
        "tagline": "Good food, warm welcomes, memorable local moments.",
        "servicesHeading": "Food and hospitality made for local customers",
        "services": [
            ("Food & Menu Experience", "A clear introduction to the flavours and food experience customers can expect."),
            ("Dine-In Visits", "Useful location and contact information for customers planning their next visit."),
            ("Takeaway Enquiries", "A simple contact route for customers checking availability and collection options."),
            ("Group & Event Enquiries", "Direct contact details for customers planning a shared meal or special occasion."),
        ],
    },
    {
        "keywords": ("dental", "dentist", "dentistry"),
        "industry": "Dental Care",
        "tagline": "Friendly dental care with a clear path forward.",
        "servicesHeading": "Dental support focused on patient confidence",
        "services": [
            ("Dental Consultations", "A straightforward first step for discussing dental needs and available care."),
            ("Preventive Care", "Patient-focused guidance that supports ongoing oral health and regular care."),
            ("Restorative Support", "Clear information and contact options for customers exploring restorative dental care."),
            ("Patient Guidance", "Helpful next steps for appointments, questions, and individual dental concerns."),
        ],
    },
    {
        "keywords": ("production", "productions"),
        "industry": "Creative Production",
        "tagline": "Creative ideas, presented with purpose.",
        "servicesHeading": "Creative production support for local projects",
        "services": [
            ("Creative Projects", "A clear route for discussing the idea, audience, and project requirements."),
            ("Visual Content", "Practical support for customers exploring content for a business or occasion."),
            ("Event Content", "Useful contact options for discussing creative coverage around an event."),
            ("Project Enquiries", "A direct way to check availability and talk through the next creative step."),
        ],
    },
    {
        "keywords": ("photo", "photography", "creative", "studio", "media"),
        "industry": "Photography & Creative Services",
        "tagline": "Real moments, thoughtfully captured.",
        "servicesHeading": "Creative photography for people, brands, and occasions",
        "services": [
            ("Portrait Photography", "Personal, polished imagery created around the subject and the moment."),
            ("Event Photography", "Visual coverage that preserves the atmosphere and important details of an occasion."),
            ("Business Content", "Professional imagery that helps local businesses present themselves with confidence."),
            ("Shoot Enquiries", "A direct way to discuss ideas, availability, locations, and the right creative approach."),
        ],
    },
    {
        "keywords": ("plumb", "water"),
        "industry": "Plumbing",
        "tagline": "Practical plumbing help when it matters.",
        "servicesHeading": "Reliable plumbing support for local properties",
        "services": [
            ("Plumbing Repairs", "A direct contact route for customers dealing with everyday plumbing problems."),
            ("Leak & Water Issues", "Practical support for identifying and addressing common water-related concerns."),
            ("Fixture Support", "Help with plumbing fixtures, replacements, and general property requirements."),
            ("Maintenance Enquiries", "Clear next steps for ongoing plumbing checks and planned maintenance."),
        ],
    },
    {
        "keywords": ("electric", "electrical"),
        "industry": "Electrical Services",
        "tagline": "Clear, dependable support for electrical needs.",
        "servicesHeading": "Electrical support for homes and businesses",
        "services": [
            ("Electrical Repairs", "A straightforward contact route for discussing electrical faults and repair needs."),
            ("Installation Enquiries", "Practical support for customers planning electrical additions or replacements."),
            ("Fault-Finding Support", "Clear next steps for customers experiencing an electrical problem."),
            ("Maintenance Planning", "Useful contact options for routine and planned electrical requirements."),
        ],
    },
    {
        "keywords": ("landscap", "garden"),
        "industry": "Landscaping",
        "tagline": "Outdoor spaces shaped with care.",
        "servicesHeading": "Landscaping support for inviting outdoor spaces",
        "services": [
            ("Garden Care", "Practical support for keeping outdoor areas neat, healthy, and welcoming."),
            ("Landscape Enquiries", "A simple way to discuss ideas and requirements for an outdoor space."),
            ("Outdoor Maintenance", "Clear contact options for recurring and seasonal property care."),
            ("Property Presentation", "Thoughtful outdoor support that helps a property make a stronger first impression."),
        ],
    },
    {
        "keywords": ("beauty", "salon", "hair", "nail", "spa", "cosmetic"),
        "industry": "Beauty & Personal Care",
        "tagline": "Feel polished, confident, and cared for.",
        "servicesHeading": "Personal care designed around every client",
        "services": [
            ("Beauty Appointments", "A clear route for discussing treatments, availability, and individual preferences."),
            ("Personal Care", "Customer-focused care designed to create a comfortable, polished experience."),
            ("Style Consultations", "A useful first step for customers exploring a new look or service."),
            ("Booking Enquiries", "Simple contact options for checking availability and planning a visit."),
        ],
    },
    {
        "keywords": ("fitness", "gym", "training"),
        "industry": "Fitness & Training",
        "tagline": "Build strength, momentum, and everyday confidence.",
        "servicesHeading": "Fitness support for personal goals",
        "services": [
            ("Fitness Guidance", "A practical starting point for customers working towards their fitness goals."),
            ("Training Support", "Clear information for customers exploring structured training and ongoing support."),
            ("Wellness Focus", "An approachable experience centred on movement, consistency, and wellbeing."),
            ("Membership Enquiries", "Direct contact options for schedules, availability, and getting started."),
        ],
    },
    {
        "keywords": ("courier", "transport", "delivery", "logistics"),
        "industry": "Courier & Transport",
        "tagline": "Local deliveries with a clear route from enquiry to arrival.",
        "servicesHeading": "Straightforward courier and transport support",
        "services": [
            ("Delivery Enquiries", "A direct way to discuss collection points, destinations, and delivery requirements."),
            ("Local Transport", "Clear contact information for customers arranging transport within the service area."),
            ("Business Deliveries", "Practical support for businesses coordinating regular or once-off deliveries."),
            ("Collection Planning", "Useful next steps for confirming timing, availability, and collection details."),
        ],
    },
    {
        "keywords": ("pest",),
        "industry": "Pest Control",
        "tagline": "Practical pest support for more comfortable spaces.",
        "servicesHeading": "Pest-control support for local properties",
        "services": [
            ("Pest Enquiries", "A direct contact route for explaining the issue and discussing appropriate next steps."),
            ("Residential Support", "Practical information for households dealing with common pest concerns."),
            ("Commercial Support", "Clear contact options for businesses and managed properties seeking assistance."),
            ("Prevention Guidance", "Useful next steps for customers looking to reduce recurring pest problems."),
        ],
    },
    {
        "keywords": ("cleaning", "cleaner"),
        "industry": "Cleaning Services",
        "tagline": "Fresh, cared-for spaces start with a simple conversation.",
        "servicesHeading": "Cleaning support for homes and businesses",
        "services": [
            ("Home Cleaning", "A clear contact route for discussing household cleaning needs and availability."),
            ("Business Cleaning", "Practical support for workplaces and customer-facing spaces."),
            ("Once-Off Cleaning", "Useful next steps for customers planning a focused or seasonal clean."),
            ("Regular Service Enquiries", "Direct contact options for discussing recurring cleaning requirements."),
        ],
    },
    {
        "keywords": ("auto", "mechanic", "vehicle", "motor"),
        "industry": "Automotive Services",
        "tagline": "Practical vehicle support to help keep you moving.",
        "servicesHeading": "Automotive support for local drivers",
        "services": [
            ("Vehicle Repairs", "A clear first step for discussing a vehicle problem and repair requirements."),
            ("Maintenance Enquiries", "Direct contact options for routine and planned vehicle care."),
            ("Diagnostic Support", "A practical route for explaining symptoms and arranging the next step."),
            ("Service Bookings", "Useful contact details for checking availability and planning a visit."),
        ],
    },
    {
        "keywords": ("hvac", "air condition", "heating", "refrigeration"),
        "industry": "Heating & Cooling",
        "tagline": "Comfort-focused support for every season.",
        "servicesHeading": "Heating and cooling support for local properties",
        "services": [
            ("Cooling Support", "A direct route for discussing air-conditioning and cooling requirements."),
            ("Heating Enquiries", "Clear contact options for customers exploring heating support."),
            ("System Maintenance", "Practical next steps for planned heating and cooling maintenance."),
            ("Repair Enquiries", "A straightforward way to explain an issue and check availability."),
        ],
    },
    {
        "keywords": ("roof", "roofing"),
        "industry": "Roofing",
        "tagline": "Roofing support built around clear, practical next steps.",
        "servicesHeading": "Roofing support for local properties",
        "services": [
            ("Roofing Enquiries", "A clear first step for discussing the property and roofing requirement."),
            ("Repair Support", "Direct contact options for customers concerned about a roofing issue."),
            ("Maintenance Planning", "Useful next steps for planned roof care and property maintenance."),
            ("Property Assessments", "A straightforward route for discussing the visible concern and arranging follow-up."),
        ],
    },
    {
        "keywords": ("locksmith", "lock "),
        "industry": "Locksmith Services",
        "tagline": "Clear, direct help for lock and access needs.",
        "servicesHeading": "Locksmith support for homes and businesses",
        "services": [
            ("Lock Enquiries", "A direct route for explaining the lock or access issue."),
            ("Key Support", "Clear contact options for customers with key-related requirements."),
            ("Property Access", "Practical next steps for home, business, and managed-property enquiries."),
            ("Security Hardware", "Useful contact details for discussing locks and related hardware."),
        ],
    },
    {
        "keywords": ("paint", "painter"),
        "industry": "Painting Services",
        "tagline": "A cleaner finish starts with a clear plan.",
        "servicesHeading": "Painting support for local properties",
        "services": [
            ("Interior Painting", "A clear contact route for discussing indoor spaces and project requirements."),
            ("Exterior Painting", "Practical next steps for customers planning exterior property work."),
            ("Residential Enquiries", "Direct contact options for home painting and finishing needs."),
            ("Commercial Enquiries", "Useful information for businesses planning a painting project."),
        ],
    },
]


def normalized_hex_color(value: Any) -> Optional[str]:
    candidate = compact_text(value).lower()
    if re.fullmatch(r"#[0-9a-f]{6}", candidate):
        return candidate
    return None


def mix_hex_color(source: str, target: str, amount: float) -> str:
    source_value = normalized_hex_color(source) or DEFAULT_BUSINESS_THEME["highlight"]
    target_value = normalized_hex_color(target) or "#000000"
    source_channels = [int(source_value[index:index + 2], 16) for index in (1, 3, 5)]
    target_channels = [int(target_value[index:index + 2], 16) for index in (1, 3, 5)]
    mixed = [
        round(channel + (target_channels[index] - channel) * amount)
        for index, channel in enumerate(source_channels)
    ]
    return "#" + "".join(f"{channel:02x}" for channel in mixed)


def business_theme_for_context(context: Optional[Dict[str, Any]]) -> Dict[str, str]:
    context = context if isinstance(context, dict) else {}
    supplied = context.get("brandTheme") if isinstance(context.get("brandTheme"), dict) else {}
    raw = context.get("rawLead") if isinstance(context.get("rawLead"), dict) else {}
    supplied_highlight = normalized_hex_color(
        supplied.get("highlight")
        or uploaded_row_value(raw, ["brandColor", "brandColour", "primaryColor", "primaryColour", "accentColor"])
    )
    supplied_background = normalized_hex_color(supplied.get("background"))
    supplied_text = normalized_hex_color(supplied.get("text"))

    descriptor_parts = [
        context.get("industry"),
        context.get("category"),
        context.get("businessName"),
        *(context.get("serviceKeywords") if isinstance(context.get("serviceKeywords"), list) else []),
    ]
    descriptor = normalize_identity_text(" ".join(compact_text(item) for item in descriptor_parts if compact_text(item)))
    highlight = background = palette_name = None
    for keywords, candidate_highlight, candidate_background, candidate_name in BUSINESS_THEME_RULES:
        if any(keyword in descriptor for keyword in keywords):
            highlight, background, palette_name = candidate_highlight, candidate_background, candidate_name
            break
    if not highlight and descriptor:
        index = int(hashlib.sha256(descriptor.encode("utf-8")).hexdigest()[:8], 16) % len(BUSINESS_THEME_FALLBACKS)
        highlight, background, palette_name = BUSINESS_THEME_FALLBACKS[index]

    return {
        "text": supplied_text or DEFAULT_BUSINESS_THEME["text"],
        "background": supplied_background or background or DEFAULT_BUSINESS_THEME["background"],
        "highlight": supplied_highlight or highlight or DEFAULT_BUSINESS_THEME["highlight"],
        "name": compact_text(supplied.get("name"), palette_name or DEFAULT_BUSINESS_THEME["name"]),
    }


def is_generic_industry_label(value: Any) -> bool:
    return normalize_identity_text(compact_text(value)) in GENERIC_INDUSTRY_LABELS


def personalized_business_profile(context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Build factual, deterministic copy that remains specific when AI is unavailable."""
    context = context if isinstance(context, dict) else {}
    business_name = compact_text(context.get("businessName"), "This business")
    location = compact_text(context.get("location"), "the local area")
    category = compact_text(context.get("category"))
    industry = compact_text(context.get("industry"))
    keywords = context.get("serviceKeywords") if isinstance(context.get("serviceKeywords"), list) else []
    descriptor = normalize_identity_text(
        " ".join(
            compact_text(value)
            for value in [business_name, category, industry, *keywords]
            if compact_text(value)
        )
    )

    matched_rule: Optional[Dict[str, Any]] = None
    for rule in BUSINESS_PROFILE_RULES:
        if any(keyword in descriptor for keyword in rule["keywords"]):
            matched_rule = rule
            break

    supplied_label = next(
        (
            value
            for value in (category, industry, *(compact_text(item) for item in keywords))
            if value and not is_generic_industry_label(value)
        ),
        "",
    )
    resolved_industry = compact_text(
        matched_rule.get("industry") if matched_rule else supplied_label,
        supplied_label or "Local Business",
    )

    if matched_rule:
        tagline = compact_text(matched_rule.get("tagline"))
        services_heading = compact_text(matched_rule.get("servicesHeading"))
        rule_services = matched_rule.get("services") or []
    else:
        tagline = f"Local service, clearly presented by {business_name}."
        services_heading = f"How {business_name} can help local customers"
        rule_services = [
            (f"{resolved_industry} Enquiries", "A clear first step for discussing the service and what the customer needs."),
            ("Customer Support", "An easy contact route for questions, availability, and practical next steps."),
            ("Service Planning", "Useful information for customers considering a service and planning ahead."),
            ("Local Contact", "Business and location details kept together so customers can connect quickly."),
        ]

    personalization_lines = [
        f"Customers in {location} can contact {business_name} to discuss their needs and the right next step.",
        f"{business_name} gives local customers a direct route for questions and availability.",
        f"The service is presented around {business_name}'s public business details in {location}.",
        f"Customers can reach {business_name} using the contact details provided on this page.",
    ]
    services: List[Dict[str, str]] = []
    for index, service in enumerate(rule_services[:4]):
        if isinstance(service, dict):
            title = compact_text(service.get("title"), f"{resolved_industry} Service {index + 1}")
            description = compact_text(service.get("description"))
        else:
            title = compact_text(service[0], f"{resolved_industry} Service {index + 1}")
            description = compact_text(service[1] if len(service) > 1 else "")
        services.append(
            {
                "title": title,
                "description": f"{description} {personalization_lines[index]}".strip(),
            }
        )

    hero_caption = f"{tagline} Connect with {business_name} in {location} using the contact route below."
    return {
        "industry": resolved_industry,
        "tagline": tagline,
        "heroCaption": hero_caption,
        "servicesHeading": services_heading,
        "servicesIntro": (
            f"Explore the {resolved_industry.lower()} support associated with {business_name}, "
            f"then get in touch directly for availability and details."
        ),
        "aboutHeading": f"Local {resolved_industry.lower()} support, made easier to understand.",
        "services": services,
    }

def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            if SENSITIVE_KEY_PATTERN.search(str(key)):
                redacted[key] = mask_secret(item)
            else:
                redacted[key] = redact_value(item)
        return redacted

    if isinstance(value, list):
        return [redact_value(item) for item in value[:25]]

    if isinstance(value, str):
        return sanitize_message(value)

    return value


def mask_secret(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "missing"
    if len(text) <= 8:
        return "***"
    return f"{text[:3]}...{text[-3:]} ({len(text)} chars)"


def mask_email(value: str) -> str:
    if "@" not in value:
        return value
    local, domain = value.split("@", 1)
    return f"{local[:2]}***@{domain}"


def sanitize_message(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""

    for name in [
        "APIFY_API_TOKEN",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "NETLIFY_AUTH_TOKEN",
        "ZENDESK_API_TOKEN",
        "GITHUB_TOKEN",
    ]:
        secret = RUNTIME_INTEGRATION_OVERRIDES.get(name, os.getenv(name))
        if secret and len(secret) >= 4:
            text = text.replace(secret, mask_secret(secret))

    text = re.sub(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1***", text, flags=re.IGNORECASE)
    text = re.sub(r"(token=)[^&\s]+", r"\1***", text, flags=re.IGNORECASE)
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", lambda match: mask_email(match.group(0)), text)
    return text


def log_event(level: str, event: str, message: str, **details: Any) -> Dict[str, Any]:
    entry = {
        "id": str(uuid4()),
        "timestamp": datetime.now().isoformat(),
        "level": level.upper(),
        "event": event,
        "message": sanitize_message(message),
        "details": redact_value(details),
    }
    LOG_BUFFER.appendleft(entry)
    log_method = getattr(logger, level.lower(), logger.info)
    log_method("%s | %s | %s", event, message, json.dumps(entry["details"], default=str))
    return entry


def chunk_text(value: str, chunk_size: int = MODEL_CHUNK_CHARS) -> List[str]:
    text = compact_text(value)
    if not text:
        return []
    return [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)]


def model_safe_value(value: Any, chunk_size: int = MODEL_CHUNK_CHARS, max_chunks: int = MODEL_MAX_CHUNKS) -> Any:
    if isinstance(value, dict):
        return {key: model_safe_value(item, chunk_size, max_chunks) for key, item in value.items()}

    if isinstance(value, list):
        return [model_safe_value(item, chunk_size, max_chunks) for item in value[:40]]

    if isinstance(value, str):
        chunks = chunk_text(value, chunk_size)
        if len(chunks) <= 1:
            return value
        return {
            "_chunked": True,
            "chunkSize": chunk_size,
            "totalChunks": len(chunks),
            "includedChunks": min(len(chunks), max_chunks),
            "omittedChunks": max(0, len(chunks) - max_chunks),
            "chunks": chunks[:max_chunks],
        }

    return value


def model_safe_json(value: Any) -> str:
    return json.dumps(model_safe_value(value), default=str)


def provider_env_status() -> Dict[str, Any]:
    providers: Dict[str, Any] = {}
    for provider, names in REQUIRED_PROVIDER_ENV.items():
        checks = []
        for name in names:
            value = RUNTIME_INTEGRATION_OVERRIDES.get(name, os.getenv(name))
            is_placeholder = bool(value and re.search(r"(replace|your_|example|placeholder)", value, re.IGNORECASE))
            checks.append(
                {
                    "name": name,
                    "configured": bool(value) and not is_placeholder,
                    "maskedValue": mask_secret(value),
                    "issue": "missing" if not value else "placeholder" if is_placeholder else None,
                }
            )
        providers[provider] = {
            "configured": all(check["configured"] for check in checks),
            "checks": checks,
            }
    return providers


@app.middleware("http")
async def request_logger(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid4())
    start = time.perf_counter()
    log_event(
        "info",
        "request.start",
        f"{request.method} {request.url.path}",
        requestId=request_id,
        method=request.method,
        path=request.url.path,
    )

    try:
        response = await call_next(request)
    except Exception as error:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        log_event(
            "error",
            "request.error",
            str(error),
            requestId=request_id,
            method=request.method,
            path=request.url.path,
            durationMs=duration_ms,
        )
        raise

    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    log_event(
        "info",
        "request.finish",
        f"{request.method} {request.url.path} -> {response.status_code}",
        requestId=request_id,
        method=request.method,
        path=request.url.path,
        statusCode=response.status_code,
        durationMs=duration_ms,
    )
    return response


@app.middleware("http")
async def require_admin_session(request: Request, call_next):
    settings = admin_auth_settings()
    if (
        request.method.upper() == "OPTIONS"
        or not settings["configured"]
        or request.url.path in AUTH_PUBLIC_PATHS
    ):
        return await call_next(request)

    session = read_admin_session(request.cookies.get(ADMIN_SESSION_COOKIE), settings)
    if not session:
        return JSONResponse(
            status_code=401,
            content={"detail": "Administrator login required.", "code": "ADMIN_AUTH_REQUIRED"},
        )
    request.state.admin_username = session["username"]
    return await call_next(request)


# Keep CORS outside the authentication middleware so cross-origin clients can
# read login-expiry 401 responses and route back to the sign-in screen.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "https://ai-site-factory-frontend.vercel.app",
        "https://ai-site-factory-sable.vercel.app",
        "https://ai-site-factory-git-main-ai-site-factory.vercel.app",
    ],
    allow_origin_regex=r"^(http://(localhost|127\.0\.0\.1):\d+|https://[a-z0-9-]+\.vercel\.app)$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


LEADS_DB: Dict[str, dict] = {}
CONTENT_DB: Dict[str, dict] = {}
PREVIEW_DB: Dict[str, dict] = {}
DISCOVERY_DB: Dict[str, dict] = {}
PIPELINE_DB: Dict[str, dict] = {}


LEAD_PRESETS = [
    {
        "id": "restaurants",
        "label": "Restaurants",
        "industry": "Restaurant",
        "query": "restaurants",
        "description": "Local restaurants, cafes, takeaways, and food venues.",
    },
    {
        "id": "plumbers",
        "label": "Plumbers",
        "industry": "Plumbing",
        "query": "plumbers",
        "description": "Emergency plumbing, repairs, leak detection, and maintenance.",
    },
    {
        "id": "dentists",
        "label": "Dentists",
        "industry": "Dental",
        "query": "dentists",
        "description": "Dental practices, cosmetic dentistry, and oral care providers.",
    },
    {
        "id": "beauty-salons",
        "label": "Beauty Salons",
        "industry": "Beauty",
        "query": "beauty salons",
        "description": "Beauty salons, spas, nail bars, and personal care studios.",
    },
    {
        "id": "gyms-fitness",
        "label": "Gyms/Fitness",
        "industry": "Fitness",
        "query": "gyms fitness studios",
        "description": "Gyms, personal trainers, wellness studios, and fitness centers.",
    },
    {
        "id": "electricians",
        "label": "Electricians",
        "industry": "Electrical",
        "query": "electricians electrical services",
        "description": "Electrical repairs, wiring, inspections, and maintenance providers.",
    },
    {
        "id": "roofers",
        "label": "Roofers",
        "industry": "Roofing",
        "query": "roofers roofing contractors",
        "description": "Roof repairs, waterproofing, installations, and maintenance teams.",
    },
    {
        "id": "hvac",
        "label": "HVAC",
        "industry": "HVAC",
        "query": "hvac air conditioning heating",
        "description": "Air conditioning, heating, refrigeration, and ventilation specialists.",
    },
    {
        "id": "auto-repair",
        "label": "Auto Repair",
        "industry": "Automotive",
        "query": "auto repair mechanics",
        "description": "Mechanics, vehicle repair workshops, panel beaters, and service centers.",
    },
    {
        "id": "locksmiths",
        "label": "Locksmiths",
        "industry": "Locksmith",
        "query": "locksmiths",
        "description": "Lock repairs, key cutting, security locks, and emergency access services.",
    },
    {
        "id": "pest-control",
        "label": "Pest Control",
        "industry": "Pest Control",
        "query": "pest control",
        "description": "Residential, commercial, and specialist pest control providers.",
    },
    {
        "id": "cleaning-services",
        "label": "Cleaning Services",
        "industry": "Cleaning",
        "query": "cleaning services",
        "description": "Home, office, carpet, industrial, and specialist cleaning teams.",
    },
    {
        "id": "landscapers",
        "label": "Landscapers",
        "industry": "Landscaping",
        "query": "landscapers garden services",
        "description": "Garden maintenance, landscaping, irrigation, and outdoor care businesses.",
    },
    {
        "id": "painters",
        "label": "Painters",
        "industry": "Painting",
        "query": "painters painting contractors",
        "description": "Interior, exterior, residential, and commercial painting contractors.",
    },
    {
        "id": "accountants",
        "label": "Accountants",
        "industry": "Accounting",
        "query": "accountants bookkeeping tax",
        "description": "Accounting, bookkeeping, payroll, and tax practices.",
    },
]

APIFY_PRESET_SEARCH_VARIANTS: Dict[str, List[str]] = {
    "restaurants": ["restaurants", "cafes", "takeaways", "food outlets", "local eateries", "family restaurants"],
    "plumbers": ["plumbers", "emergency plumbers", "plumbing services", "drain cleaning", "leak repair", "geyser repair"],
    "dentists": ["dentists", "dental clinics", "cosmetic dentists", "family dentists", "oral care clinics", "dental practices"],
    "beauty salons": ["beauty salons", "nail salons", "day spas", "hair salons", "beauty spas", "skin care clinics"],
    "gyms fitness studios": ["gyms", "fitness centers", "personal trainers", "fitness studios", "health clubs", "wellness studios"],
    "electricians electrical services": [
        "electricians",
        "electrical installation services",
        "electrical repair services",
        "emergency electricians",
        "electrical contractors",
        "solar electricians",
    ],
    "roofers roofing contractors": [
        "roofers",
        "roofing contractors",
        "waterproofing services",
        "roof repair",
        "gutter installation",
        "roof maintenance",
    ],
    "hvac air conditioning heating": [
        "HVAC contractors",
        "air conditioning services",
        "refrigeration services",
        "air conditioning repair",
        "heating contractors",
        "ventilation services",
    ],
    "auto repair mechanics": ["auto repair shops", "mechanics", "panel beaters", "car service centers", "auto electricians", "brake repair"],
    "locksmiths": ["locksmiths", "emergency locksmiths", "key cutting services", "mobile locksmiths", "lock repair", "security locksmiths"],
    "pest control": ["pest control", "exterminators", "fumigation services", "termite control", "rodent control", "pest management"],
    "cleaning services": ["cleaning services", "office cleaning", "carpet cleaning", "house cleaning", "industrial cleaning", "deep cleaning"],
    "landscapers garden services": ["landscapers", "garden services", "irrigation services", "garden maintenance", "lawn care", "tree services"],
    "painters painting contractors": ["painters", "painting contractors", "commercial painters", "house painters", "industrial painters", "painting services"],
    "accountants bookkeeping tax": ["accountants", "bookkeeping services", "tax consultants", "payroll services", "accounting firms", "tax practitioners"],
}


SITE_TEMPLATES = [
    {
        "id": "default-service",
        "name": "Default Service",
        "description": "Clean landing page with hero, four service cards, about, contact, and footer.",
        "accent": "#0f766e",
        "background": "#f7faf9",
    },
    {
        "id": "bold-local",
        "name": "Bold Local",
        "description": "High-contrast local-business page with strong calls to action.",
        "accent": "#c2410c",
        "background": "#fff8f3",
    },
    {
        "id": "premium-trust",
        "name": "Premium Trust",
        "description": "Polished trust-led page for professional service businesses.",
        "accent": "#1d4ed8",
        "background": "#f7f9ff",
    },
]


class ScrapeRequest(BaseModel):
    url: str


class RawLeadRow(BaseModel):
    businessName: str
    email: EmailStr
    domain: Optional[str] = "Not provided"
    category: str
    location: Optional[str] = "Not provided"
    notes: Optional[str] = "No additional notes provided."


class IntakeRequest(BaseModel):
    rawLeadRow: RawLeadRow
    sourceType: Optional[str] = "manual"
    batchId: Optional[str] = None


class IntakeResponse(BaseModel):
    leadId: str
    intakeStatus: str
    validationIssues: List[str]


class CleanedLead(BaseModel):
    leadId: str
    businessName: str
    email: EmailStr
    domain: str
    category: str
    location: str
    sourceRef: str
    cleanSummary: str
    cleanStatus: str
    validationIssues: List[str]


class ServiceBlock(BaseModel):
    title: str
    description: str


class ContentPacket(BaseModel):
    headline: str
    summary: str
    serviceBlocks: List[ServiceBlock]
    CTA: str
    tone: str
    brandNotes: str


class OutreachDraft(BaseModel):
    subject: str
    body: str
    recipientEmail: EmailStr
    previewUrl: Optional[str] = None
    approvalStatus: str = "Pending Review"


class GenerationRequest(BaseModel):
    leadRecord: CleanedLead
    generationProfile: Optional[str] = "default"
    templateId: Optional[str] = "standard-service-template"
    toneProfile: Optional[str] = "professional"


class GenerationResponse(BaseModel):
    contentPacket: ContentPacket
    outreachDraft: OutreachDraft
    generationStatus: str
    generatedAt: str


class SiteBuildRequest(BaseModel):
    leadId: str
    contentPacket: ContentPacket
    templateId: Optional[str] = "standard-service-template"
    deployMode: Optional[str] = "preview"

class ZendeskSyncRequest(BaseModel):
    leadId: str
    businessName: str
    email: EmailStr
    category: str
    previewReference: str
    approvalStatus: str

class SiteBuildResponse(BaseModel):
    previewUrl: str
    deploymentStatus: str
    buildReference: str
    generatedAt: str
    reviewStatus: str
    previewType: str
    limitationNote: str

class OutreachGenerateRequest(BaseModel):
    leadId: str
    businessName: str
    email: EmailStr
    category: str
    previewReference: str


class OutreachDraftResponse(BaseModel):
    subject: str
    body: str
    recipientEmail: EmailStr
    status: str


class OutreachSendRequest(BaseModel):
    zendeskTicketId: int
    subject: str
    body: str
    recipientEmail: EmailStr


ZENDESK_FIELD_KEYS = [
    "campaignId",
    "campaignName",
    "canonicalLeadKey",
    "pipelineId",
    "approvalId",
    "batchId",
    "businessName",
    "contactName",
    "contactEmail",
    "contactPhone",
    "industry",
    "location",
    "address",
    "contactChannel",
    "leadStatus",
    "deployRequested",
    "emailSendRequested",
    "phoneCallStatus",
    "liveUrl",
    "sourceUrl",
]

ZENDESK_FIELD_BLUEPRINT = [
    {"key": "campaignId", "title": "AI Site Factory - Campaign ID", "type": "text", "forms": ["email", "phone"], "description": "Stable campaign identifier supplied by AI Site Factory."},
    {"key": "campaignName", "title": "AI Site Factory - Campaign name", "type": "text", "forms": ["email", "phone"], "description": "Human-readable lead generation campaign name."},
    {"key": "canonicalLeadKey", "title": "AI Site Factory - Canonical lead key", "type": "text", "forms": ["email", "phone"], "description": "Deduplication key for the discovered business."},
    {"key": "pipelineId", "title": "AI Site Factory - Pipeline ID", "type": "text", "forms": ["email", "phone"], "description": "Pipeline execution identifier used for traceability."},
    {"key": "approvalId", "title": "AI Site Factory - Approval ID", "type": "text", "forms": ["email", "phone"], "description": "Deployment approval identifier used by webhook callbacks."},
    {"key": "batchId", "title": "AI Site Factory - Apify batch ID", "type": "text", "forms": ["email", "phone"], "description": "Source discovery batch identifier."},
    {"key": "businessName", "title": "AI Site Factory - Business name", "type": "text", "forms": ["email", "phone"], "description": "Business discovered by the lead campaign."},
    {"key": "contactName", "title": "AI Site Factory - Contact name", "type": "text", "forms": ["email", "phone"], "description": "Named person of contact when available."},
    {"key": "contactEmail", "title": "AI Site Factory - Contact email", "type": "text", "forms": ["email"], "description": "Email address used by the email lead workflow."},
    {"key": "contactPhone", "title": "AI Site Factory - Contact phone", "type": "text", "forms": ["phone"], "description": "Phone number used by the call lead workflow."},
    {"key": "industry", "title": "AI Site Factory - Industry", "type": "text", "forms": ["email", "phone"], "description": "Campaign industry or business category."},
    {"key": "location", "title": "AI Site Factory - Location", "type": "text", "forms": ["email", "phone"], "description": "Campaign location associated with the lead."},
    {"key": "address", "title": "AI Site Factory - Address", "type": "textarea", "forms": ["email", "phone"], "description": "Public business address returned by discovery."},
    {
        "key": "contactChannel",
        "title": "AI Site Factory - Contact channel",
        "type": "tagger",
        "forms": ["email", "phone"],
        "description": "Workflow channel selected for this ticket.",
        "custom_field_options": [
            {"name": "Email", "value": "asf_cf_channel_email"},
            {"name": "Phone call", "value": "asf_cf_channel_phone"},
        ],
    },
    {
        "key": "leadStatus",
        "title": "AI Site Factory - Lead status",
        "type": "tagger",
        "forms": ["email", "phone"],
        "description": "Current AI Site Factory workflow state.",
        "custom_field_options": [
            {"name": "Awaiting deployment", "value": "asf_cf_status_awaiting_deployment"},
            {"name": "Generating site", "value": "asf_cf_status_generating"},
            {"name": "Deployed", "value": "asf_cf_status_deployed"},
            {"name": "Email sent", "value": "asf_cf_status_email_sent"},
            {"name": "Phone updated", "value": "asf_cf_status_phone_updated"},
            {"name": "Failed", "value": "asf_cf_status_failed"},
        ],
    },
    {"key": "deployRequested", "title": "AI Site Factory - Deploy site", "type": "checkbox", "forms": ["email", "phone"], "description": "Agent approval checkbox that starts AI generation and deployment.", "tag": "asf_deploy_requested"},
    {"key": "emailSendRequested", "title": "AI Site Factory - Send approved email", "type": "checkbox", "forms": ["email"], "description": "Agent approval checkbox for the separate email webhook.", "tag": "asf_email_send_requested"},
    {
        "key": "phoneCallStatus",
        "title": "AI Site Factory - Call status",
        "type": "tagger",
        "forms": ["phone"],
        "description": "Agent outcome for the phone lead workflow.",
        "custom_field_options": [
            {"name": "New", "value": "asf_cf_call_new"},
            {"name": "Attempted", "value": "asf_cf_call_attempted"},
            {"name": "Connected", "value": "asf_cf_call_connected"},
            {"name": "Follow up", "value": "asf_cf_call_follow_up"},
            {"name": "Qualified", "value": "asf_cf_call_qualified"},
            {"name": "Not interested", "value": "asf_cf_call_not_interested"},
            {"name": "No answer", "value": "asf_cf_call_no_answer"},
            {"name": "Other", "value": "asf_cf_call_other"},
        ],
    },
    {"key": "liveUrl", "title": "AI Site Factory - Live site URL", "type": "text", "forms": ["email", "phone"], "description": "Netlify URL returned after a successful deployment."},
    {"key": "sourceUrl", "title": "AI Site Factory - Lead source URL", "type": "text", "forms": ["email", "phone"], "description": "Public listing URL where the lead was discovered."},
]

ZENDESK_SETUP_TAGS = [
    "asf_managed",
    "asf_intake",
    "asf_form_email_lead",
    "asf_form_call_lead",
    "asf_channel_email",
    "asf_channel_phone",
    "asf_source_apify_google_maps",
    "asf_source_upload",
    "asf_deploy_pending",
    "asf_deploy_requested",
    "asf_can_deploy",
    "asf_stage_intake",
    "asf_stage_generating",
    "asf_artifact_ready",
    "asf_repo_ready",
    "asf_stage_deploying",
    "asf_email_send_pending",
    "asf_call_pending",
    "asf_deployed",
    "asf_stage_live",
    "asf_deployment_cancelled",
    "asf_stage_cancelled",
    "asf_cancel_email_fired",
    "asf_cancel_phone_fired",
    "asf_customer_notified_deployed",
    "asf_10_day_clock_started",
    "asf_10_day_cancellation_due",
    "asf_10_day_cancellation_sent",
    "asf_phone_cancellation_due",
    "asf_phone_cancellation_note_added",
    "asf_deployment_approval_withdrawn",
    "asf_generation_failed",
    "asf_deploy_failed",
    "asf_stage_failed",
    "asf_email_sent",
    "asf_phone_updated",
]

ZENDESK_SETUP_DEFAULTS = {
    "emailFormName": "AI Site Factory - Email Lead",
    "callFormName": "AI Site Factory - Call Lead",
    "emailViewName": "AI Site Factory - Email Leads",
    "callViewName": "AI Site Factory - Call Leads",
    "deployedViewName": "AI Site Factory - Deployed Sites",
    "webhookName": "AI Site Factory - Ticket actions",
}


class ZendeskFieldSettingsRequest(BaseModel):
    fields: Dict[str, Optional[str]] = Field(default_factory=dict)


class ZendeskSetupRequest(BaseModel):
    emailFormName: str = ZENDESK_SETUP_DEFAULTS["emailFormName"]
    callFormName: str = ZENDESK_SETUP_DEFAULTS["callFormName"]
    emailViewName: str = ZENDESK_SETUP_DEFAULTS["emailViewName"]
    callViewName: str = ZENDESK_SETUP_DEFAULTS["callViewName"]
    deployedViewName: str = ZENDESK_SETUP_DEFAULTS["deployedViewName"]
    webhookName: str = ZENDESK_SETUP_DEFAULTS["webhookName"]
    brandId: Optional[str] = None
    createViews: bool = True
    createAutomation: bool = False
    webhookUrl: Optional[str] = None
    confirm: bool = False


class ZendeskConnectionRequest(BaseModel):
    subdomain: str
    username: str
    apiToken: str
    validateConnection: bool = True


class ZendeskWebhookRequest(BaseModel):
    action: str
    approvalId: Optional[str] = None
    canonicalLeadKey: Optional[str] = None
    zendeskTicketId: Optional[int] = None
    channel: Optional[str] = None
    value: Optional[Any] = None
    actor: Optional[str] = "Zendesk Webhook"
    notes: Optional[str] = None


class ZendeskCampaignRestoreRequest(BaseModel):
    confirm: bool = False
    includePendingIntake: bool = False
    ticketIds: List[int] = Field(default_factory=list)
    maxTickets: int = Field(default=200, ge=1, le=1000)


class DeploymentMediaRefreshRequest(BaseModel):
    zendeskTicketId: int
    mainImageUrl: str
    githubRepoFullName: str


class DiscoverLeadsRequest(BaseModel):
    presetId: str
    industry: Optional[str] = None
    location: str = "South Africa"
    query: Optional[str] = None
    limit: int = Field(default=3, ge=1, le=10000)
    forceRefresh: bool = False


class CampaignIntakeRequest(BaseModel):
    campaignName: str = ""
    presetId: str
    industry: Optional[str] = None
    location: str = "South Africa"
    query: Optional[str] = None
    limit: int = Field(default=10, ge=1, le=10000)
    channels: List[str] = Field(default_factory=lambda: ["email", "phone"])
    forceRefresh: bool = True
    syncZendesk: bool = True
    idempotencyKey: Optional[str] = None
    autoGenerateMetadata: bool = False


class DiscoveredLead(BaseModel):
    leadKey: str
    canonicalLeadKey: Optional[str] = None
    businessName: str
    email: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    domain: Optional[str] = None
    category: str = "General Services"
    address: Optional[str] = None
    location: str = "South Africa"
    province: Optional[str] = None
    rating: Optional[float] = None
    reviewsCount: Optional[int] = None
    source: str = "apify-google-maps"
    sourceUrl: Optional[str] = None
    notes: Optional[str] = None
    raw: Dict[str, Any] = Field(default_factory=dict)


class DiscoverLeadsResponse(BaseModel):
    batchId: str
    preset: Dict[str, Any]
    location: str
    query: str
    leads: List[DiscoveredLead]
    sourceStatus: str
    warnings: List[str]
    provinceStats: Dict[str, Any] = Field(default_factory=dict)
    duplicatesSkipped: int = 0
    requestedCount: int = 0
    rawFetched: int = 0
    eligibleReturned: int = 0
    websitesSkipped: int = 0
    noContactSkipped: int = 0
    generatedDuplicatesSkipped: int = 0
    currentSearchDuplicatesSkipped: int = 0
    alreadyDeployedSkipped: int = 0
    activeDeploymentSkipped: int = 0
    policyExcludedSkipped: int = 0
    locationSkipped: int = 0
    invalidRecordSkipped: int = 0
    reusedPendingOrFailed: int = 0
    targetOverflowSkipped: int = 0
    shortfall: int = 0
    stopReason: str = "RESULTS_EXHAUSTED"
    searchVariantCount: int = 0
    providerStatus: str = "UNKNOWN"
    providerDurationSeconds: float = 0
    emailLeads: int = 0
    phoneLeads: int = 0
    emailAndPhoneLeads: int = 0
    cached: bool = False


class PipelineRunRequest(BaseModel):
    leads: List[DiscoveredLead]
    templateId: Optional[str] = FREEFORM_TEMPLATE_ID
    sourceBatchId: Optional[str] = None
    regenerateExistingSites: bool = True
    resumeExisting: bool = True
    forceRegenerate: bool = False


class PipelineLeadResult(BaseModel):
    leadKey: str
    canonicalLeadKey: Optional[str] = None
    businessName: str
    status: str
    pipelineStatus: Optional[str] = None
    currentStep: Optional[str] = None
    stepHistory: List[Dict[str, Any]] = Field(default_factory=list)
    approvalStatus: Optional[str] = None
    pendingApprovalId: Optional[str] = None
    pendingPreviewHtml: Optional[str] = None
    cleanedLead: Optional[Dict[str, Any]] = None
    siteContent: Optional[Dict[str, Any]] = None
    outreachDraft: Optional[Dict[str, Any]] = None
    deployment: Optional[Dict[str, Any]] = None
    deploymentHistory: Optional[Dict[str, Any]] = None
    zendesk: Optional[Dict[str, Any]] = None
    publishMode: Optional[str] = None
    githubExport: Optional[Dict[str, Any]] = None
    structuredErrors: List[Dict[str, Any]] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


class PipelineRunResponse(BaseModel):
    pipelineId: str
    status: str
    templateId: str
    createdAt: str
    results: List[PipelineLeadResult]
    warnings: List[str] = Field(default_factory=list)


class ApprovalActionRequest(BaseModel):
    approvedBy: Optional[str] = "Dashboard Operator"
    rejectedBy: Optional[str] = "Dashboard Operator"
    requestedBy: Optional[str] = "Dashboard Operator"
    notes: Optional[str] = None
    reason: Optional[str] = None
    regenerateExistingSite: bool = False
    publishMode: str = "github-netlify"
    zendeskTicketId: Optional[int] = None


class ApprovalActionResponse(BaseModel):
    approvalId: str
    status: str
    leadKey: Optional[str] = None
    canonicalLeadKey: str
    businessName: str
    deployment: Optional[Dict[str, Any]] = None
    deploymentHistory: Optional[Dict[str, Any]] = None
    zendesk: Optional[Dict[str, Any]] = None
    outreachDraft: Optional[Dict[str, Any]] = None
    publishMode: Optional[str] = None
    githubExport: Optional[Dict[str, Any]] = None
    errors: List[Dict[str, Any]] = Field(default_factory=list)


class ApiProbeRequest(BaseModel):
    includeExternal: bool = False
    checks: List[str] = Field(default_factory=list)


class AdminLoginRequest(BaseModel):
    username: str
    password: str


class ApiSafetyProbeRequest(BaseModel):
    provider: str


class ApiProbeCheck(BaseModel):
    name: str
    status: str
    message: str
    durationMs: float
    details: Dict[str, Any] = Field(default_factory=dict)


class ApiProbeResponse(BaseModel):
    status: str
    generatedAt: str
    checks: List[ApiProbeCheck]


def fetch_page_text(url: str) -> str:
    try:
        response = requests.get(
            url,
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        return soup.get_text(" ", strip=True)
    except requests.RequestException:
        return ""


def detect_location_from_text(text: str, domain: str, base_url: str) -> str:
    text_lower = text.lower()

    locations = [
        "south africa",
        "durban",
        "johannesburg",
        "cape town",
        "pretoria",
        "gauteng",
        "kwazulu-natal",
        "kzn",
        "western cape",
        "eastern cape",
        "africa",
        "global",
        "international",
        "worldwide",
    ]

    for location in locations:
        if location in text_lower:
            return location.title()

    extra_paths = ["/contact", "/contact-us", "/about", "/about-us"]

    for path in extra_paths:
        extra_text = fetch_page_text(base_url.rstrip("/") + path).lower()
        for location in locations:
            if location in extra_text:
                return location.title()

    if domain.endswith(".co.za") or ".co.za" in domain:
        return "South Africa"

    if domain.endswith(".com"):
        return "Global"

    return "Not provided"


def get_preset_or_404(preset_id: str) -> Dict[str, Any]:
    for preset in LEAD_PRESETS:
        if preset["id"] == preset_id:
            return preset
    raise HTTPException(status_code=404, detail="Lead preset not found.")


def resolve_lead_preset(preset_id: str, industry: Optional[str], query: Optional[str]) -> Dict[str, Any]:
    normalized_id = compact_text(preset_id).lower()
    if normalized_id != "custom":
        return get_preset_or_404(normalized_id)

    custom_industry = compact_text(industry)
    custom_query = compact_text(query)
    if not custom_industry:
        raise HTTPException(status_code=400, detail="Industry is required for a custom campaign.")
    if not custom_query:
        raise HTTPException(status_code=400, detail="Search intent is required for a custom campaign.")
    return {
        "id": "custom",
        "label": custom_industry,
        "industry": custom_industry,
        "query": custom_query,
        "description": "User-defined campaign search.",
    }


def get_template_or_404(template_id: str) -> Dict[str, Any]:
    for template in SITE_TEMPLATES:
        if template["id"] == template_id:
            return template
    raise HTTPException(status_code=404, detail="Site template not found.")


def require_env(name: str) -> str:
    value = RUNTIME_INTEGRATION_OVERRIDES.get(name, os.getenv(name))
    if not value:
        raise RuntimeError(f"{name} environment variable is missing.")
    return value


def compact_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    return re.sub(r"\s+", " ", str(value)).strip() or fallback


def normalize_publish_mode(value: Optional[str]) -> str:
    publish_mode = compact_text(value, "github-netlify")
    if publish_mode not in VALID_PUBLISH_MODES:
        raise HTTPException(status_code=400, detail=f"Unsupported publish mode: {publish_mode}")
    return publish_mode


def slugify(value: str, max_length: int = 42) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug[:max_length].strip("-") or "site").lower()


def stable_lead_key(*parts: Any) -> str:
    raw = "|".join(compact_text(part).lower() for part in parts if compact_text(part))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16] if raw else str(uuid4())


def normalize_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None

    url = compact_text(value)
    if not url or url.lower().startswith(("mailto:", "tel:")):
        return None

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)
    if not parsed.netloc or "." not in parsed.netloc:
        return None

    return url


def domain_from_url(value: Optional[str]) -> Optional[str]:
    url = normalize_url(value)
    if not url:
        return None
    return urlparse(url).netloc.replace("www.", "")


def normalize_domain(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    domain = domain_from_url(value) or compact_text(value).lower().replace("www.", "")
    domain = re.sub(r"^https?://", "", domain).split("/", 1)[0].strip()
    return domain if domain and "." in domain else None


def normalize_email_identity(value: Optional[str]) -> Optional[str]:
    email = compact_text(value).lower()
    if not email or len(email) > 254:
        return None
    if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", email):
        return None
    local_part, domain = email.rsplit("@", 1)
    if len(local_part) > 64 or ".." in email or domain.endswith((".invalid", ".localhost")):
        return None
    return email


def normalize_phone_identity(value: Optional[str]) -> Optional[str]:
    phone = re.sub(r"\D+", "", compact_text(value))
    if not phone:
        return None
    if phone.startswith("27") and len(phone) >= 11:
        return phone
    if phone.startswith("0") and len(phone) == 10:
        return "27" + phone[1:]
    return phone if len(phone) >= 7 else None


def normalize_identity_text(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", compact_text(value).lower()).strip()


def lead_has_website(lead: DiscoveredLead) -> bool:
    return bool(normalize_url(lead.website) or normalize_domain(lead.domain))


def first_present(data: Dict[str, Any], keys: List[str], fallback: Optional[str] = None) -> Optional[str]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list) and value:
            value = value[0]
        value = compact_text(value)
        if value:
            return value
    return fallback


def extract_emails_from_text(text: str) -> List[str]:
    matches = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text or "")
    seen = set()
    emails = []
    for match in matches:
        email = match.lower()
        if email not in seen:
            seen.add(email)
            emails.append(email)
    return emails


def extract_phone_from_text(text: str) -> Optional[str]:
    match = re.search(r"(?:\+?\d[\d\s().-]{7,}\d)", text or "")
    return compact_text(match.group(0)) if match else None


def extract_email_from_item(item: Dict[str, Any]) -> Optional[str]:
    direct = first_present(item, ["email", "contactEmail", "mail"])
    if direct:
        found = extract_emails_from_text(direct)
        return found[0] if found else direct.lower()

    emails = item.get("emails") or item.get("emailAddresses")
    if isinstance(emails, list):
        for email in emails:
            normalized = compact_text(email).lower()
            if normalized:
                return normalized
    if isinstance(emails, str):
        found = extract_emails_from_text(emails)
        if found:
            return found[0]

    found = extract_emails_from_text(json.dumps(item, default=str))
    return found[0] if found else None


def lead_disqualification_reason(lead: DiscoveredLead) -> Optional[str]:
    website = normalize_url(lead.website)
    domain = compact_text(lead.domain) or domain_from_url(website)

    if website or domain:
        return "hasWebsite"

    return None
def is_qualified_discovery_lead(lead: DiscoveredLead) -> bool:
    return lead_disqualification_reason(lead) is None


SOUTH_AFRICA_TERMS = [
    "south africa",
    "gauteng",
    "kwazulu-natal",
    "kwazulu natal",
    "kzn",
    "western cape",
    "eastern cape",
    "free state",
    "limpopo",
    "mpumalanga",
    "north west",
    "northern cape",
    "durban",
    "johannesburg",
    "cape town",
    "pretoria",
    "polokwane",
    "bloemfontein",
    "east london",
    "port elizabeth",
    "gqeberha",
    "pietermaritzburg",
    "umhlanga",
    "ballito",
    "sandton",
    "centurion",
    "midrand",
]

SOUTH_AFRICA_PROVINCES = [
    "Eastern Cape",
    "Free State",
    "Gauteng",
    "KwaZulu-Natal",
    "Limpopo",
    "Mpumalanga",
    "Northern Cape",
    "North West",
    "Western Cape",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_api_datetime(value: Optional[str]) -> Optional[datetime]:
    cleaned = compact_text(value)
    if not cleaned:
        return None
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def elapsed_seconds(start_value: Optional[str], end_value: Optional[str] = None) -> float:
    started = parse_api_datetime(start_value)
    if not started:
        return 0.0
    finished = parse_api_datetime(end_value) if end_value else datetime.now(timezone.utc)
    if not finished:
        finished = datetime.now(timezone.utc)
    return round(max(0.0, (finished - started).total_seconds()), 1)


def env_enabled(name: str, default: bool = False) -> bool:
    value = compact_text(os.getenv(name), "true" if default else "false").lower()
    return value in {"1", "true", "yes", "on"}


def safe_json_loads(value: Optional[str], fallback: Any = None) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def pipeline_db_path() -> str:
    default_path = os.path.join(os.path.dirname(__file__), "data", "pipeline.db")
    path = os.getenv("PIPELINE_DB_PATH", default_path)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    return path


PIPELINE_SEED_TABLES = [
    "lead_registry",
    "lead_identity_index",
    "discovery_batches",
    "pipeline_runs",
    "pipeline_steps",
    "site_registry",
    "github_site_repos",
    "deployment_history",
    "approval_records",
    "campaigns",
    "campaign_deployments",
    "campaign_email_leads",
    "campaign_call_leads",
    "zendesk_field_settings",
    "zendesk_provisioned_resources",
    "zendesk_ticket_links",
    "zendesk_webhook_events",
]

PIPELINE_SEED_ANCHOR_TABLES = [
    "campaigns",
    "pipeline_runs",
    "approval_records",
    "lead_registry",
]

PIPELINE_DATA_CLEANUP_VERSION = "demo-reset-2026-07-17-v1"
PIPELINE_OPERATIONAL_TABLES = [
    "campaign_intake_jobs",
    "campaign_import_items",
    "campaign_import_jobs",
    "campaign_lead_identity_claims",
    "deploy_webhook_claims",
    "zendesk_webhook_events",
    "zendesk_ticket_links",
    "campaign_email_leads",
    "campaign_call_leads",
    "campaign_deployments",
    "campaigns",
    "approval_records",
    "deployment_history",
    "github_site_repos",
    "site_registry",
    "pipeline_steps",
    "pipeline_runs",
    "discovery_batches",
    "lead_identity_index",
    "lead_registry",
]

ZENDESK_CONFIG_SEED_TABLES = [
    "zendesk_field_settings",
    "zendesk_provisioned_resources",
]


def pipeline_seed_path() -> str:
    return os.getenv(
        "PIPELINE_SEED_PATH",
        os.path.join(os.path.dirname(__file__), "data", "pipeline.seed.json"),
    )


def get_pipeline_db() -> sqlite3.Connection:
    connection = sqlite3.connect(pipeline_db_path())
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def ensure_db_column(db: sqlite3.Connection, table_name: str, column_name: str, column_sql: str) -> None:
    columns = db.execute(f"PRAGMA table_info({table_name})").fetchall()
    if any(column["name"] == column_name for column in columns):
        return
    db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")


def init_pipeline_db() -> None:
    with get_pipeline_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS lead_registry (
                canonical_lead_key TEXT PRIMARY KEY,
                lead_key TEXT,
                business_name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                website TEXT,
                domain TEXT,
                category TEXT,
                address TEXT,
                location TEXT,
                province TEXT,
                source TEXT,
                source_url TEXT,
                owner_name TEXT,
                owner_email TEXT,
                owner_status TEXT DEFAULT 'unassigned',
                assigned_at TEXT,
                status TEXT DEFAULT 'DISCOVERED',
                raw_json TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                discovery_count INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS app_metadata (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lead_identity_index (
                identity_key TEXT PRIMARY KEY,
                identity_type TEXT NOT NULL,
                canonical_lead_key TEXT NOT NULL,
                lead_key TEXT,
                business_name TEXT,
                source TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_lead_identity_canonical
            ON lead_identity_index(canonical_lead_key);

            CREATE TABLE IF NOT EXISTS campaign_lead_identity_claims (
                identity_key TEXT PRIMARY KEY,
                identity_type TEXT NOT NULL,
                canonical_lead_key TEXT NOT NULL,
                campaign_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_campaign_lead_claim_owner
            ON campaign_lead_identity_claims(campaign_id, canonical_lead_key);

            CREATE TABLE IF NOT EXISTS discovery_batches (
                batch_id TEXT PRIMARY KEY,
                preset_id TEXT,
                query TEXT,
                location TEXT,
                lead_count INTEGER NOT NULL DEFAULT 0,
                duplicates_skipped INTEGER NOT NULL DEFAULT 0,
                leads_json TEXT,
                province_stats_json TEXT,
                warnings_json TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT,
                name TEXT NOT NULL,
                batch_id TEXT,
                preset_id TEXT,
                industry TEXT,
                query TEXT,
                location TEXT,
                requested_count INTEGER NOT NULL DEFAULT 0,
                discovered_count INTEGER NOT NULL DEFAULT 0,
                channel_filter TEXT NOT NULL DEFAULT 'email,phone',
                status TEXT NOT NULL DEFAULT 'INTAKE_READY',
                warnings_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(batch_id) REFERENCES discovery_batches(batch_id)
            );

            CREATE TABLE IF NOT EXISTS campaign_import_jobs (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'QUEUED',
                total_rows INTEGER NOT NULL DEFAULT 0,
                processed_rows INTEGER NOT NULL DEFAULT 0,
                succeeded_rows INTEGER NOT NULL DEFAULT 0,
                skipped_rows INTEGER NOT NULL DEFAULT 0,
                failed_rows INTEGER NOT NULL DEFAULT 0,
                chunk_size INTEGER NOT NULL DEFAULT 5,
                channels_json TEXT NOT NULL,
                error TEXT,
                errors_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS campaign_intake_jobs (
                id TEXT PRIMARY KEY,
                request_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'QUEUED',
                stage TEXT NOT NULL DEFAULT 'QUEUED',
                progress_percent REAL NOT NULL DEFAULT 0,
                campaign_id TEXT,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_campaign_intake_jobs_status
            ON campaign_intake_jobs(status, created_at);

            CREATE TABLE IF NOT EXISTS campaign_import_items (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                raw_json TEXT NOT NULL,
                canonical_lead_key TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                approval_ids_json TEXT,
                ticket_ids_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(job_id, row_number),
                FOREIGN KEY(job_id) REFERENCES campaign_import_jobs(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_campaign_import_items_work
            ON campaign_import_items(job_id, status, row_number);

            CREATE TABLE IF NOT EXISTS campaign_email_leads (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                canonical_lead_key TEXT NOT NULL,
                approval_id TEXT NOT NULL,
                business_name TEXT NOT NULL,
                contact_name TEXT,
                email TEXT NOT NULL,
                source_url TEXT,
                status TEXT NOT NULL DEFAULT 'AWAITING_DEPLOYMENT',
                deploy_requested INTEGER NOT NULL DEFAULT 0,
                ticket_id INTEGER,
                deployment_id TEXT,
                fields_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(campaign_id, canonical_lead_key),
                FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE,
                FOREIGN KEY(canonical_lead_key) REFERENCES lead_registry(canonical_lead_key),
                FOREIGN KEY(approval_id) REFERENCES approval_records(id)
            );

            CREATE TABLE IF NOT EXISTS campaign_call_leads (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                canonical_lead_key TEXT NOT NULL,
                approval_id TEXT NOT NULL,
                business_name TEXT NOT NULL,
                contact_name TEXT,
                phone TEXT NOT NULL,
                source_url TEXT,
                status TEXT NOT NULL DEFAULT 'AWAITING_DEPLOYMENT',
                deploy_requested INTEGER NOT NULL DEFAULT 0,
                ticket_id INTEGER,
                deployment_id TEXT,
                fields_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(campaign_id, canonical_lead_key),
                FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE,
                FOREIGN KEY(canonical_lead_key) REFERENCES lead_registry(canonical_lead_key),
                FOREIGN KEY(approval_id) REFERENCES approval_records(id)
            );

            CREATE TABLE IF NOT EXISTS campaign_deployments (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                canonical_lead_key TEXT NOT NULL,
                approval_id TEXT NOT NULL UNIQUE,
                channel TEXT NOT NULL CHECK(channel IN ('email', 'phone')),
                status TEXT NOT NULL DEFAULT 'AWAITING_DEPLOYMENT',
                ai_generation_count INTEGER NOT NULL DEFAULT 0,
                repo_created INTEGER NOT NULL DEFAULT 0,
                repo_url TEXT,
                live_url TEXT,
                deployment_history_id TEXT,
                requested_at TEXT,
                completed_at TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE,
                FOREIGN KEY(canonical_lead_key) REFERENCES lead_registry(canonical_lead_key),
                FOREIGN KEY(approval_id) REFERENCES approval_records(id),
                FOREIGN KEY(deployment_history_id) REFERENCES deployment_history(id)
            );

            CREATE INDEX IF NOT EXISTS idx_campaign_email_status
            ON campaign_email_leads(campaign_id, status);

            CREATE INDEX IF NOT EXISTS idx_campaign_call_status
            ON campaign_call_leads(campaign_id, status);

            CREATE INDEX IF NOT EXISTS idx_campaign_deployments_status
            ON campaign_deployments(campaign_id, status);

            CREATE TABLE IF NOT EXISTS pipeline_runs (
                pipeline_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                template_id TEXT NOT NULL,
                source_batch_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                lead_count INTEGER NOT NULL DEFAULT 0,
                completed_count INTEGER NOT NULL DEFAULT 0,
                pending_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                warnings_json TEXT
            );

            CREATE TABLE IF NOT EXISTS pipeline_steps (
                id TEXT PRIMARY KEY,
                pipeline_id TEXT NOT NULL,
                canonical_lead_key TEXT,
                step TEXT NOT NULL,
                status TEXT NOT NULL,
                provider TEXT,
                message TEXT,
                started_at TEXT,
                finished_at TEXT,
                duration_ms REAL,
                retryable INTEGER NOT NULL DEFAULT 0,
                details_json TEXT
            );

            CREATE TABLE IF NOT EXISTS site_registry (
                canonical_lead_key TEXT PRIMARY KEY,
                site_id TEXT NOT NULL,
                site_name TEXT,
                url TEXT,
                admin_url TEXT,
                github_repo_full_name TEXT,
                github_repo_url TEXT,
                last_commit_sha TEXT,
                last_build_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_deploy_id TEXT,
                last_deploy_state TEXT,
                deployment_count INTEGER NOT NULL DEFAULT 0,
                publish_mode TEXT NOT NULL DEFAULT 'github-netlify'
            );

            CREATE TABLE IF NOT EXISTS github_site_repos (
                canonical_lead_key TEXT PRIMARY KEY,
                repo_id INTEGER,
                repo_name TEXT NOT NULL,
                repo_full_name TEXT NOT NULL,
                repo_url TEXT NOT NULL,
                default_branch TEXT DEFAULT 'main',
                private INTEGER NOT NULL DEFAULT 0,
                index_content_sha TEXT,
                readme_content_sha TEXT,
                commit_sha TEXT,
                html_checksum TEXT,
                export_status TEXT NOT NULL,
                export_error TEXT,
                pipeline_id TEXT,
                approval_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                exported_at TEXT
            );

            CREATE TABLE IF NOT EXISTS deployment_history (
                id TEXT PRIMARY KEY,
                canonical_lead_key TEXT NOT NULL,
                pipeline_id TEXT,
                approval_id TEXT,
                site_id TEXT,
                site_name TEXT,
                deploy_id TEXT,
                build_id TEXT,
                url TEXT,
                deploy_action TEXT,
                state TEXT,
                html_checksum TEXT,
                deployed_at TEXT NOT NULL,
                approved_by TEXT,
                approval_status TEXT,
                github_repo_full_name TEXT,
                github_repo_url TEXT,
                commit_sha TEXT,
                publish_mode TEXT DEFAULT 'github-netlify',
                github_export_json TEXT,
                raw_json TEXT
            );

            CREATE TABLE IF NOT EXISTS approval_records (
                id TEXT PRIMARY KEY,
                pipeline_id TEXT NOT NULL,
                canonical_lead_key TEXT NOT NULL,
                lead_key TEXT,
                business_name TEXT NOT NULL,
                status TEXT NOT NULL,
                html TEXT,
                html_checksum TEXT,
                context_json TEXT,
                site_content_json TEXT,
                outreach_json TEXT,
                template_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                approved_by TEXT,
                rejected_by TEXT,
                notes TEXT,
                deployment_history_id TEXT,
                zendesk_json TEXT,
                publish_mode TEXT DEFAULT 'github-netlify',
                github_export_json TEXT,
                errors_json TEXT
            );

            CREATE TABLE IF NOT EXISTS deploy_webhook_claims (
                approval_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                claim_token TEXT,
                lease_expires_at REAL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                claimed_at TEXT,
                completed_at TEXT,
                last_error TEXT,
                result_json TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(approval_id) REFERENCES approval_records(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_deploy_webhook_claim_state_lease
            ON deploy_webhook_claims(state, lease_expires_at);

            CREATE TABLE IF NOT EXISTS zendesk_field_settings (
                field_key TEXT PRIMARY KEY,
                field_id TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS zendesk_provisioned_resources (
                resource_key TEXT PRIMARY KEY,
                resource_type TEXT NOT NULL,
                resource_id TEXT,
                display_name TEXT,
                metadata_json TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS zendesk_ticket_links (
                id TEXT PRIMARY KEY,
                approval_id TEXT NOT NULL,
                canonical_lead_key TEXT NOT NULL,
                pipeline_id TEXT,
                external_id TEXT,
                ticket_id INTEGER,
                ticket_url TEXT,
                channel TEXT NOT NULL,
                stage TEXT NOT NULL,
                status TEXT,
                tags_json TEXT,
                payload_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(approval_id, channel, stage)
            );

            CREATE TABLE IF NOT EXISTS zendesk_webhook_events (
                id TEXT PRIMARY KEY,
                approval_id TEXT,
                canonical_lead_key TEXT,
                ticket_id INTEGER,
                channel TEXT,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT,
                payload_json TEXT,
                result_json TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        ensure_db_column(db, "approval_records", "publish_mode", "publish_mode TEXT DEFAULT 'github-netlify'")
        ensure_db_column(db, "approval_records", "github_export_json", "github_export_json TEXT")
        ensure_db_column(db, "discovery_batches", "leads_json", "leads_json TEXT")
        ensure_db_column(db, "deployment_history", "publish_mode", "publish_mode TEXT DEFAULT 'github-netlify'")
        ensure_db_column(db, "deployment_history", "github_export_json", "github_export_json TEXT")
        ensure_db_column(db, "deployment_history", "build_id", "build_id TEXT")
        ensure_db_column(db, "deployment_history", "approval_status", "approval_status TEXT")
        ensure_db_column(db, "deployment_history", "github_repo_full_name", "github_repo_full_name TEXT")
        ensure_db_column(db, "deployment_history", "github_repo_url", "github_repo_url TEXT")
        ensure_db_column(db, "deployment_history", "commit_sha", "commit_sha TEXT")
        ensure_db_column(db, "site_registry", "github_repo_full_name", "github_repo_full_name TEXT")
        ensure_db_column(db, "site_registry", "github_repo_url", "github_repo_url TEXT")
        ensure_db_column(db, "site_registry", "last_commit_sha", "last_commit_sha TEXT")
        ensure_db_column(db, "site_registry", "last_build_id", "last_build_id TEXT")
        ensure_db_column(db, "site_registry", "publish_mode", "publish_mode TEXT NOT NULL DEFAULT 'github-netlify'")
        db.execute(
            """
            UPDATE site_registry
            SET publish_mode = COALESCE(
                (
                    SELECT deployment_history.publish_mode
                    FROM deployment_history
                    WHERE deployment_history.canonical_lead_key = site_registry.canonical_lead_key
                      AND deployment_history.publish_mode IS NOT NULL
                    ORDER BY deployment_history.deployed_at DESC
                    LIMIT 1
                ),
                publish_mode,
                'github-netlify'
            )
            WHERE EXISTS (
                SELECT 1 FROM deployment_history
                WHERE deployment_history.canonical_lead_key = site_registry.canonical_lead_key
                  AND deployment_history.publish_mode IS NOT NULL
            )
            """
        )
        ensure_db_column(db, "campaigns", "idempotency_key", "idempotency_key TEXT")
        ensure_db_column(
            db,
            "campaign_import_jobs",
            "background_requested",
            "background_requested INTEGER NOT NULL DEFAULT 0",
        )
        ensure_db_column(db, "zendesk_ticket_links", "external_id", "external_id TEXT")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_campaigns_idempotency_key "
            "ON campaigns(idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_zendesk_ticket_links_external_id "
            "ON zendesk_ticket_links(external_id) WHERE external_id IS NOT NULL"
        )


def restore_pipeline_seed_if_empty() -> Dict[str, Any]:
    """Restore the committed baseline only when the deployment database has no app data."""
    seed_path = pipeline_seed_path()
    if not os.path.exists(seed_path):
        return {"restored": False, "reason": "seed_not_found", "path": seed_path}

    with get_pipeline_db() as db:
        existing_counts = {
            table: db.execute(f'SELECT COUNT(*) AS count FROM "{table}"').fetchone()["count"]
            for table in PIPELINE_SEED_ANCHOR_TABLES
        }
        if any(existing_counts.values()):
            return {
                "restored": False,
                "reason": "database_not_empty",
                "existingCounts": existing_counts,
            }

        try:
            with open(seed_path, "r", encoding="utf-8") as seed_file:
                payload = json.load(seed_file)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Pipeline seed could not be read: {exc}") from exc

        if payload.get("schemaVersion") != 1 or not isinstance(payload.get("tables"), dict):
            raise RuntimeError("Pipeline seed has an unsupported schema.")

        restored_counts: Dict[str, int] = {}
        db.execute("PRAGMA foreign_keys = OFF")
        try:
            db.execute("BEGIN IMMEDIATE")
            for table in PIPELINE_SEED_TABLES:
                rows = payload["tables"].get(table, [])
                if not isinstance(rows, list):
                    raise RuntimeError(f"Pipeline seed table '{table}' is invalid.")
                table_columns = {
                    column["name"]
                    for column in db.execute(f'PRAGMA table_info("{table}")').fetchall()
                }
                inserted = 0
                for row in rows:
                    if not isinstance(row, dict):
                        raise RuntimeError(f"Pipeline seed row in '{table}' is invalid.")
                    columns = [column for column in row if column in table_columns]
                    if not columns:
                        continue
                    quoted_columns = ", ".join(f'"{column}"' for column in columns)
                    placeholders = ", ".join("?" for _ in columns)
                    before = db.total_changes
                    db.execute(
                        f'INSERT OR IGNORE INTO "{table}" ({quoted_columns}) VALUES ({placeholders})',
                        tuple(row[column] for column in columns),
                    )
                    inserted += db.total_changes - before
                restored_counts[table] = inserted

            foreign_key_issues = db.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_issues:
                first_issue = dict(foreign_key_issues[0])
                raise RuntimeError(f"Pipeline seed violates a foreign key: {first_issue}")
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys = ON")

    total = sum(restored_counts.values())
    result = {
        "restored": total > 0,
        "reason": "seed_restored" if total > 0 else "seed_empty",
        "restoredCounts": restored_counts,
        "total": total,
    }
    if total:
        log_event(
            "info",
            "pipeline.seed_restored",
            "Previous application data restored into an empty deployment database.",
            total=total,
            restoredCounts=restored_counts,
        )
    return result


def clear_pipeline_operational_data(cleanup_version: str = PIPELINE_DATA_CLEANUP_VERSION) -> Dict[str, Any]:
    """Delete campaign/demo activity while retaining Zendesk configuration and field mappings."""
    deleted_counts: Dict[str, int] = {}
    with get_pipeline_db() as db:
        db.execute("PRAGMA foreign_keys = OFF")
        try:
            db.execute("BEGIN IMMEDIATE")
            available_tables = {
                row["name"]
                for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            for table in PIPELINE_OPERATIONAL_TABLES:
                if table not in available_tables:
                    continue
                count = db.execute(f'SELECT COUNT(*) AS count FROM "{table}"').fetchone()["count"]
                db.execute(f'DELETE FROM "{table}"')
                deleted_counts[table] = count
            db.execute(
                """
                INSERT INTO app_metadata (key, value, updated_at) VALUES ('pipeline_data_cleanup_version', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (cleanup_version, now_iso()),
            )
            foreign_key_issues = db.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_issues:
                raise RuntimeError(f"Operational data cleanup violates a foreign key: {dict(foreign_key_issues[0])}")
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys = ON")

    LEADS_DB.clear()
    CONTENT_DB.clear()
    PREVIEW_DB.clear()
    DISCOVERY_DB.clear()
    PIPELINE_DB.clear()
    total = sum(deleted_counts.values())
    log_event(
        "info",
        "pipeline.operational_data_cleared",
        "Historical campaign and demo activity was removed; configuration was retained.",
        cleanupVersion=cleanup_version,
        total=total,
        deletedCounts=deleted_counts,
    )
    return {"cleared": True, "cleanupVersion": cleanup_version, "total": total, "deletedCounts": deleted_counts}


def cleanup_pipeline_operational_data_on_startup() -> Dict[str, Any]:
    """Run the requested demo reset once on Render, with an environment override available."""
    enabled = env_enabled("ENABLE_PIPELINE_DATA_CLEANUP") if os.getenv("ENABLE_PIPELINE_DATA_CLEANUP") is not None else env_enabled("RENDER")
    if not enabled:
        return {"cleared": False, "reason": "pipeline_data_cleanup_disabled"}
    with get_pipeline_db() as db:
        marker = db.execute(
            "SELECT value FROM app_metadata WHERE key = 'pipeline_data_cleanup_version'"
        ).fetchone()
    if marker and marker["value"] == PIPELINE_DATA_CLEANUP_VERSION:
        return {"cleared": False, "reason": "cleanup_already_applied", "cleanupVersion": marker["value"]}
    return clear_pipeline_operational_data()


def pipeline_seed_restore_enabled() -> bool:
    """Honor an explicit flag, otherwise enable the safe empty-DB restore on Render."""
    if os.getenv("ENABLE_PIPELINE_SEED_RESTORE") is not None:
        return env_enabled("ENABLE_PIPELINE_SEED_RESTORE")
    return env_enabled("RENDER")


def bootstrap_pipeline_seed_on_startup() -> Dict[str, Any]:
    """Restore historical app data for opted-in or Render-hosted deployments."""
    if not pipeline_seed_restore_enabled():
        return {"restored": False, "reason": "pipeline_seed_restore_disabled"}
    return restore_pipeline_seed_if_empty()


def restore_zendesk_config_seed_if_empty() -> Dict[str, Any]:
    """Bootstrap only the managed Zendesk blueprint, never historical app data."""
    seed_path = pipeline_seed_path()
    if not os.path.exists(seed_path):
        return {"restored": False, "reason": "seed_not_found", "path": seed_path}

    try:
        with open(seed_path, "r", encoding="utf-8") as seed_file:
            payload = json.load(seed_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Pipeline seed could not be read: {exc}") from exc

    if payload.get("schemaVersion") != 1 or not isinstance(payload.get("tables"), dict):
        raise RuntimeError("Pipeline seed has an unsupported schema.")

    resource_rows = payload["tables"].get("zendesk_provisioned_resources", [])
    configuration = next(
        (
            row
            for row in resource_rows
            if isinstance(row, dict) and compact_text(row.get("resource_key")) == "configuration"
        ),
        None,
    )
    configured_subdomain = compact_text((configuration or {}).get("resource_id")).lower()
    configured_subdomain = re.sub(r"^https?://", "", configured_subdomain).split("/", 1)[0]
    configured_subdomain = configured_subdomain.removesuffix(".zendesk.com")
    live_subdomain = compact_text(os.getenv("ZENDESK_SUBDOMAIN")).lower()
    live_subdomain = re.sub(r"^https?://", "", live_subdomain).split("/", 1)[0]
    live_subdomain = live_subdomain.removesuffix(".zendesk.com")
    if not live_subdomain or configured_subdomain != live_subdomain:
        return {
            "restored": False,
            "reason": "subdomain_mismatch",
            "configuredSubdomain": configured_subdomain or None,
            "liveSubdomain": live_subdomain or None,
        }

    restored_counts: Dict[str, int] = {}
    with get_pipeline_db() as db:
        existing_counts = {
            table: db.execute(f'SELECT COUNT(*) AS count FROM "{table}"').fetchone()["count"]
            for table in ZENDESK_CONFIG_SEED_TABLES
        }
        if any(existing_counts.values()):
            return {
                "restored": False,
                "reason": "zendesk_config_not_empty",
                "existingCounts": existing_counts,
            }

        db.execute("BEGIN IMMEDIATE")
        try:
            for table in ZENDESK_CONFIG_SEED_TABLES:
                rows = payload["tables"].get(table, [])
                if not isinstance(rows, list):
                    raise RuntimeError(f"Pipeline seed table '{table}' is invalid.")
                if table == "zendesk_provisioned_resources":
                    allowed_resource_keys = {
                        "configuration",
                        "form:email",
                        "form:phone",
                        *[f"field:{key}" for key in ZENDESK_FIELD_KEYS],
                    }
                    rows = [
                        row
                        for row in rows
                        if isinstance(row, dict) and compact_text(row.get("resource_key")) in allowed_resource_keys
                    ]
                table_columns = {
                    column["name"]
                    for column in db.execute(f'PRAGMA table_info("{table}")').fetchall()
                }
                inserted = 0
                for row in rows:
                    if not isinstance(row, dict):
                        raise RuntimeError(f"Pipeline seed row in '{table}' is invalid.")
                    columns = [column for column in row if column in table_columns]
                    if not columns:
                        continue
                    quoted_columns = ", ".join(f'"{column}"' for column in columns)
                    placeholders = ", ".join("?" for _ in columns)
                    before = db.total_changes
                    db.execute(
                        f'INSERT OR IGNORE INTO "{table}" ({quoted_columns}) VALUES ({placeholders})',
                        tuple(row[column] for column in columns),
                    )
                    inserted += db.total_changes - before
                restored_counts[table] = inserted
            db.commit()
        except Exception:
            db.rollback()
            raise

    total = sum(restored_counts.values())
    result = {
        "restored": total > 0,
        "reason": "zendesk_config_restored" if total > 0 else "seed_empty",
        "restoredCounts": restored_counts,
        "total": total,
    }
    if total:
        log_event(
            "info",
            "zendesk.config_seed_restored",
            "Managed Zendesk setup restored without historical campaigns or tickets.",
            total=total,
            restoredCounts=restored_counts,
        )
    return result


def bootstrap_zendesk_config_on_startup() -> Dict[str, Any]:
    """Restore only Zendesk blueprint metadata; enabled by default for ephemeral hosts."""
    if not env_enabled("ENABLE_ZENDESK_CONFIG_BOOTSTRAP", default=True):
        return {"restored": False, "reason": "zendesk_config_bootstrap_disabled"}
    return restore_zendesk_config_seed_if_empty()


def canonical_lead_key_from_values(
    raw: Dict[str, Any],
    business_name: Optional[str],
    website: Optional[str],
    phone: Optional[str],
    address: Optional[str],
    source_url: Optional[str],
) -> str:
    place_id = first_present(
        raw,
        ["placeId", "place_id", "googlePlaceId", "googleId", "cid", "fid", "id"],
    )
    if place_id:
        return stable_lead_key("place", place_id)

    if source_url and "google" in source_url.lower():
        return stable_lead_key("source", source_url)

    domain = domain_from_url(website)
    if domain:
        return stable_lead_key("domain", business_name, domain)

    if phone:
        return stable_lead_key("phone", business_name, phone)

    if address:
        return stable_lead_key("address", business_name, address)

    return stable_lead_key("business", business_name, raw.get("location") or raw.get("city") or raw.get("country"))


def canonical_lead_key_for_lead(lead: DiscoveredLead) -> str:
    return lead.canonicalLeadKey or canonical_lead_key_from_values(
        lead.raw or {},
        lead.businessName,
        lead.website,
        lead.phone,
        lead.address,
        lead.sourceUrl,
    )


def lead_identity_pairs(lead: DiscoveredLead) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    raw = lead.raw or {}

    email = normalize_email_identity(lead.email)
    if email:
        pairs.append(("email", f"email:{email}"))

    phone = normalize_phone_identity(lead.phone)
    if phone:
        pairs.append(("phone", f"phone:{phone}"))

    domain = normalize_domain(lead.website) or normalize_domain(lead.domain)
    if domain:
        pairs.append(("domain", f"domain:{domain}"))

    place_id_keys = ["placeId", "place_id", "googlePlaceId", "googleId", "cid", "fid"]
    if compact_text(lead.source).lower() != "uploaded-lead-data":
        place_id_keys.append("id")
    place_id = first_present(raw, place_id_keys)
    if place_id:
        pairs.append(("source_place", f"source_place:{compact_text(place_id).lower()}"))

    source_url = compact_text(lead.sourceUrl)
    if source_url and ("google" in source_url.lower() or "maps" in source_url.lower()):
        pairs.append(("source_url", f"source_url:{source_url.lower()}"))

    business = normalize_identity_text(lead.businessName)
    location = normalize_identity_text(lead.address or lead.location)
    if business and location:
        pairs.append(("business_location", f"business_location:{stable_lead_key(business, location)}"))

    unique: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for identity_type, identity_key in pairs:
        if identity_key not in seen:
            seen.add(identity_key)
            unique.append((identity_type, identity_key))
    return unique


def existing_canonical_lead_keys(keys: List[str]) -> set:
    if not keys:
        return set()
    placeholders = ",".join("?" for _ in keys)
    with get_pipeline_db() as db:
        rows = db.execute(
            f"SELECT canonical_lead_key FROM lead_registry WHERE canonical_lead_key IN ({placeholders})",
            keys,
        ).fetchall()
    return {row["canonical_lead_key"] for row in rows}


def existing_lead_identity_keys(identity_keys: List[str]) -> Dict[str, str]:
    if not identity_keys:
        return {}
    placeholders = ",".join("?" for _ in identity_keys)
    with get_pipeline_db() as db:
        rows = db.execute(
            f"SELECT identity_key, canonical_lead_key FROM lead_identity_index WHERE identity_key IN ({placeholders})",
            identity_keys,
        ).fetchall()
    return {row["identity_key"]: row["canonical_lead_key"] for row in rows}


def upsert_lead_identity_index(db: sqlite3.Connection, lead: DiscoveredLead, canonical_key: str, timestamp: str) -> None:
    for identity_type, identity_key in lead_identity_pairs(lead):
        db.execute(
            """
            INSERT INTO lead_identity_index (
                identity_key, identity_type, canonical_lead_key, lead_key,
                business_name, source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identity_key) DO UPDATE SET
                canonical_lead_key = excluded.canonical_lead_key,
                lead_key = excluded.lead_key,
                business_name = excluded.business_name,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (
                identity_key,
                identity_type,
                canonical_key,
                lead.leadKey,
                lead.businessName,
                lead.source,
                timestamp,
                timestamp,
            ),
        )


def lead_identity_conflicts(lead: DiscoveredLead, canonical_key: str) -> Dict[str, str]:
    identities = [identity_key for _identity_type, identity_key in lead_identity_pairs(lead)]
    existing = existing_lead_identity_keys(identities)
    return {
        identity_key: existing_key
        for identity_key, existing_key in existing.items()
        if existing_key != canonical_key
    }


def lead_has_contact(lead: DiscoveredLead) -> bool:
    return bool(normalize_email_identity(lead.email) or normalize_phone_identity(lead.phone))


def lead_contact_bucket(lead: DiscoveredLead) -> str:
    has_email = bool(normalize_email_identity(lead.email))
    has_phone = bool(normalize_phone_identity(lead.phone))
    if has_email and has_phone:
        return "email_phone"
    if has_email:
        return "email"
    if has_phone:
        return "phone"
    return "none"


def generated_lead_identity_conflicts(lead: DiscoveredLead, canonical_key: str) -> Dict[str, str]:
    candidate_identities = {identity_key for _identity_type, identity_key in lead_identity_pairs(lead)}
    if not candidate_identities:
        return {}

    with get_pipeline_db() as db:
        rows = db.execute(
            """
            SELECT canonical_lead_key, lead_key, business_name, context_json
            FROM approval_records
            WHERE status NOT IN ('SUPERSEDED')
            """
        ).fetchall()

    conflicts: Dict[str, str] = {}
    for row in rows:
        existing_key = row["canonical_lead_key"]
        if existing_key == canonical_key:
            conflicts[f"canonical:{canonical_key}"] = existing_key
            continue

        context = safe_json_loads(row["context_json"], {})
        existing_lead = DiscoveredLead(
            leadKey=row["lead_key"] or existing_key,
            canonicalLeadKey=existing_key,
            businessName=context.get("businessName") or row["business_name"] or "Generated lead",
            email=context.get("email"),
            phone=context.get("phone"),
            website=context.get("website"),
            domain=context.get("domain"),
            category=context.get("category") or context.get("industry") or "General Services",
            address=context.get("address"),
            location=context.get("location") or "South Africa",
            source=context.get("source") or "generated-approval",
            sourceUrl=context.get("sourceUrl"),
            raw=context.get("rawLead") if isinstance(context.get("rawLead"), dict) else {},
        )
        existing_identities = {identity_key for _identity_type, identity_key in lead_identity_pairs(existing_lead)}
        for identity_key in candidate_identities.intersection(existing_identities):
            conflicts[identity_key] = existing_key
    return conflicts


DISCOVERY_REUSABLE_PRIOR_STATUSES = {
    "AWAITING_DEPLOYMENT",
    "PENDING",
    "GENERATION_FAILED",
    "EXPORT_FAILED",
    "PUBLISH_FAILED",
    "DEPLOY_FAILED",
}
DISCOVERY_ACTIVE_PRIOR_STATUSES = {"EXPORTING", "DEPLOY_REQUESTED", "DEPLOYING"}
DISCOVERY_LIVE_PRIOR_STATUSES = {"APPROVED", "DEPLOYED_ZENDESK_FAILED"}
LIVE_DEPLOYMENT_STATES = {"ready", "live", "published", "active", "succeeded", "success"}


def prior_lead_usage_index() -> Dict[str, List[Dict[str, Any]]]:
    with get_pipeline_db() as db:
        approval_rows = db.execute(
            """
            SELECT id, canonical_lead_key, lead_key, business_name, status,
                   context_json, deployment_history_id
            FROM approval_records
            """
        ).fetchall()
        deployment_rows = db.execute(
            """
            SELECT canonical_lead_key, approval_id, state, url, approval_status
            FROM deployment_history
            """
        ).fetchall()

    live_canonical_keys: Set[str] = set()
    live_approval_ids: Set[str] = set()
    for row in deployment_rows:
        state = compact_text(row["state"]).lower()
        approval_status = compact_text(row["approval_status"]).upper()
        if state in LIVE_DEPLOYMENT_STATES or (
            compact_text(row["url"]) and approval_status in {"APPROVED", "DEPLOYED", "DEPLOYED_ZENDESK_FAILED"}
        ):
            live_canonical_keys.add(compact_text(row["canonical_lead_key"]))
            live_approval_ids.add(compact_text(row["approval_id"]))

    index: Dict[str, List[Dict[str, Any]]] = {}
    for row in approval_rows:
        canonical_key = compact_text(row["canonical_lead_key"])
        context = safe_json_loads(row["context_json"], {})
        existing_lead = DiscoveredLead(
            leadKey=row["lead_key"] or canonical_key,
            canonicalLeadKey=canonical_key,
            businessName=context.get("businessName") or row["business_name"] or "Existing lead",
            email=context.get("email"),
            phone=context.get("phone"),
            website=context.get("website"),
            domain=context.get("domain"),
            category=context.get("category") or context.get("industry") or "General Services",
            address=context.get("address"),
            location=context.get("location") or "South Africa",
            source=context.get("source") or "approval-record",
            sourceUrl=context.get("sourceUrl"),
            raw=context.get("rawLead") if isinstance(context.get("rawLead"), dict) else {},
        )
        record = {
            "approvalId": row["id"],
            "canonicalLeadKey": canonical_key,
            "status": compact_text(row["status"]).upper(),
            "live": (
                canonical_key in live_canonical_keys
                or compact_text(row["id"]) in live_approval_ids
                or (
                    compact_text(row["status"]).upper() in DISCOVERY_LIVE_PRIOR_STATUSES
                    and bool(row["deployment_history_id"])
                )
            ),
        }
        identity_keys = {identity_key for _identity_type, identity_key in lead_identity_pairs(existing_lead)}
        identity_keys.add(f"canonical:{canonical_key}")
        for identity_key in identity_keys:
            index.setdefault(identity_key, []).append(record)
    return index


def classify_prior_lead_usage(
    lead: DiscoveredLead,
    canonical_key: str,
    usage_index: Dict[str, List[Dict[str, Any]]],
) -> str:
    identity_keys = {identity_key for _identity_type, identity_key in lead_identity_pairs(lead)}
    identity_keys.add(f"canonical:{canonical_key}")
    matching: Dict[str, Dict[str, Any]] = {}
    for identity_key in identity_keys:
        for record in usage_index.get(identity_key, []):
            matching[record["approvalId"]] = record
    records = list(matching.values())
    if any(record["live"] for record in records):
        return "ALREADY_DEPLOYED"
    if any(record["status"] in DISCOVERY_ACTIVE_PRIOR_STATUSES for record in records):
        return "ACTIVE_DEPLOYMENT"
    if any(record["status"] in DISCOVERY_REUSABLE_PRIOR_STATUSES for record in records):
        return "REUSABLE_PENDING_OR_FAILED"
    if records:
        return "POLICY_EXCLUDED"
    return "NEW"


def select_mixed_contact_leads(leads: List[DiscoveredLead], limit: int) -> List[DiscoveredLead]:
    buckets: Dict[str, List[DiscoveredLead]] = {"email_phone": [], "email": [], "phone": []}
    for lead in leads:
        bucket = lead_contact_bucket(lead)
        if bucket in buckets:
            buckets[bucket].append(lead)

    selected: List[DiscoveredLead] = []
    seen: Set[str] = set()

    def take(bucket_name: str) -> None:
        if len(selected) >= limit:
            return
        bucket = buckets[bucket_name]
        while bucket:
            lead = bucket.pop(0)
            key = lead.canonicalLeadKey or lead.leadKey
            if key in seen:
                continue
            seen.add(key)
            selected.append(lead)
            break

    while len(selected) < limit and any(buckets.values()):
        take("email_phone")
        take("email")
        take("phone")

    return selected


def mixed_channel_counts(leads: List[DiscoveredLead]) -> Dict[str, int]:
    return {
        "emailLeads": sum(1 for lead in leads if normalize_email_identity(lead.email)),
        "phoneLeads": sum(1 for lead in leads if normalize_phone_identity(lead.phone)),
        "emailAndPhoneLeads": sum(
            1 for lead in leads if normalize_email_identity(lead.email) and normalize_phone_identity(lead.phone)
        ),
    }


def require_contactable_leads(leads: List[DiscoveredLead], source_label: str) -> Dict[str, int]:
    counts = mixed_channel_counts(leads)
    if counts["emailLeads"] < 1 and counts["phoneLeads"] < 1:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "CONTACTABLE_LEADS_REQUIRED",
                "message": (
                    f"{source_label} does not contain any valid contact details. "
                    "Include at least one real email address or phone number."
                ),
                **counts,
            },
        )
    return counts


def suggest_campaign_metadata(
    leads: List[DiscoveredLead],
    location: str,
    fallback_industry: str = "Mixed industries",
) -> Dict[str, Any]:
    fallback_label = compact_text(fallback_industry)
    prefer_fallback = bool(fallback_label and not is_generic_industry_label(fallback_label))
    industry_counts: Dict[str, int] = {}
    for lead in leads:
        category = compact_text(lead.category)
        if category and not is_generic_industry_label(category):
            industry_counts[category] = industry_counts.get(category, 0) + 1
    ranked = sorted(industry_counts.items(), key=lambda item: (-item[1], item[0].casefold()))
    total_classified = sum(industry_counts.values())
    if prefer_fallback:
        industry = fallback_label
    elif len(ranked) == 1 or (ranked and ranked[0][1] / max(1, total_classified) >= 0.65):
        industry = ranked[0][0]
    elif ranked:
        industry = "Mixed industries"
    else:
        industry = compact_text(fallback_industry, "Mixed industries")
        if is_generic_industry_label(industry):
            industry = "Mixed industries"

    top_industries = [name for name, _count in ranked[:3]]
    if prefer_fallback:
        descriptor = fallback_label
    elif not top_industries:
        descriptor = "Mixed Business"
    elif len(top_industries) == 1:
        descriptor = top_industries[0]
    elif len(top_industries) == 2:
        descriptor = f"{top_industries[0]} & {top_industries[1]}"
    else:
        descriptor = f"{top_industries[0]}, {top_industries[1]} & {top_industries[2]}"
    location_label = compact_text(location, "South Africa").split(",", 1)[0]
    name_suffix = "Mixed Leads" if industry == "Mixed industries" else "Leads"
    campaign_name = f"{location_label} {descriptor} {name_suffix} — {datetime.now().strftime('%b %Y')}"
    return {
        "campaignName": campaign_name,
        "industry": industry,
        "topIndustries": top_industries,
        "industryCounts": industry_counts,
        "location": compact_text(location, "South Africa"),
        "channelCounts": mixed_channel_counts(leads),
    }


def upsert_lead_registry(lead: DiscoveredLead) -> None:
    canonical_key = canonical_lead_key_for_lead(lead)
    lead.canonicalLeadKey = canonical_key
    timestamp = now_iso()

    with get_pipeline_db() as db:
        db.execute(
            """
            INSERT INTO lead_registry (
                canonical_lead_key, lead_key, business_name, email, phone, website, domain,
                category, address, location, province, source, source_url, owner_name,
                owner_email, owner_status, assigned_at, status, raw_json, first_seen_at,
                last_seen_at, discovery_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(canonical_lead_key) DO UPDATE SET
                lead_key = excluded.lead_key,
                email = COALESCE(excluded.email, lead_registry.email),
                phone = COALESCE(excluded.phone, lead_registry.phone),
                website = COALESCE(excluded.website, lead_registry.website),
                domain = COALESCE(excluded.domain, lead_registry.domain),
                category = COALESCE(excluded.category, lead_registry.category),
                address = COALESCE(excluded.address, lead_registry.address),
                location = COALESCE(excluded.location, lead_registry.location),
                province = COALESCE(excluded.province, lead_registry.province),
                source_url = COALESCE(excluded.source_url, lead_registry.source_url),
                owner_name = NULL,
                owner_email = NULL,
                owner_status = NULL,
                assigned_at = NULL,
                raw_json = excluded.raw_json,
                last_seen_at = excluded.last_seen_at,
                discovery_count = lead_registry.discovery_count + 1
            """,
            (
                canonical_key,
                lead.leadKey,
                lead.businessName,
                lead.email,
                lead.phone,
                lead.website,
                lead.domain,
                lead.category,
                lead.address,
                lead.location,
                lead.province,
                lead.source,
                lead.sourceUrl,
                None,
                None,
                None,
                None,
                "DISCOVERED",
                json.dumps(lead.raw, default=str),
                timestamp,
                timestamp,
            ),
        )
        upsert_lead_identity_index(db, lead, canonical_key, timestamp)


def record_discovery_batch(
    batch_id: str,
    preset_id: str,
    query: str,
    location: str,
    lead_count: int,
    duplicates_skipped: int,
    leads: List[DiscoveredLead],
    province_stats: Dict[str, Any],
    warnings: List[str],
) -> None:
    with get_pipeline_db() as db:
        db.execute(
            """
            INSERT OR REPLACE INTO discovery_batches (
                batch_id, preset_id, query, location, lead_count, duplicates_skipped,
                leads_json, province_stats_json, warnings_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                preset_id,
                query,
                location,
                lead_count,
                duplicates_skipped,
                json.dumps([lead.model_dump() for lead in leads], default=str),
                json.dumps(province_stats, default=str),
                json.dumps(warnings, default=str),
                now_iso(),
            ),
        )


def cached_discovery_response(
    preset: Dict[str, Any],
    preset_id: str,
    query: str,
    location: str,
    limit: int,
) -> Optional[DiscoverLeadsResponse]:
    with get_pipeline_db() as db:
        row = db.execute(
            """
            SELECT *
            FROM discovery_batches
            WHERE preset_id = ? AND query = ? AND location = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (preset_id, query, location),
        ).fetchone()

    if not row:
        return None

    cached_leads = [DiscoveredLead(**lead) for lead in safe_json_loads(row["leads_json"], [])]
    eligible: List[DiscoveredLead] = []
    seen_keys: Set[str] = set()
    seen_identities: Set[str] = set()
    revalidated_duplicates = 0
    revalidated_deployed = 0
    revalidated_active = 0
    revalidated_policy = 0
    revalidated_reused = 0
    usage_index = prior_lead_usage_index()
    for lead in cached_leads:
        canonical_key = canonical_lead_key_for_lead(lead)
        lead.canonicalLeadKey = canonical_key
        identities = {identity_key for _identity_type, identity_key in lead_identity_pairs(lead)}
        if lead_has_website(lead) or not lead_has_contact(lead):
            continue
        if (
            canonical_key in seen_keys
            or identities.intersection(seen_identities)
        ):
            revalidated_duplicates += 1
            continue
        seen_keys.add(canonical_key)
        seen_identities.update(identities)
        prior_usage = classify_prior_lead_usage(lead, canonical_key, usage_index)
        if prior_usage == "ALREADY_DEPLOYED":
            revalidated_deployed += 1
            continue
        if prior_usage == "ACTIVE_DEPLOYMENT":
            revalidated_active += 1
            continue
        if prior_usage == "POLICY_EXCLUDED":
            revalidated_policy += 1
            continue
        if prior_usage == "REUSABLE_PENDING_OR_FAILED":
            revalidated_reused += 1
        eligible.append(lead)
    leads = select_mixed_contact_leads(eligible, limit)
    if not leads:
        return None

    email_count = sum(1 for lead in leads if normalize_email_identity(lead.email))
    phone_count = sum(1 for lead in leads if compact_text(lead.phone))
    province_stats = safe_json_loads(row["province_stats_json"], {})
    stat_rows = list(province_stats.values()) if isinstance(province_stats, dict) else []

    def cached_stat(key: str) -> int:
        return sum(int((value or {}).get(key) or 0) for value in stat_rows if isinstance(value, dict))

    first_stats = next((value for value in stat_rows if isinstance(value, dict)), {})
    current_search_duplicates = cached_stat("currentSearchDuplicatesSkipped") + revalidated_duplicates
    already_deployed = cached_stat("alreadyDeployedSkipped") + revalidated_deployed
    active_deployment = cached_stat("activeDeploymentSkipped") + revalidated_active
    policy_excluded = cached_stat("policyExcludedSkipped") + revalidated_policy
    shortfall = max(0, limit - len(leads))

    return DiscoverLeadsResponse(
        batchId=row["batch_id"],
        preset=preset,
        location=row["location"],
        query=row["query"],
        leads=leads,
        sourceStatus="CACHE",
        warnings=safe_json_loads(row["warnings_json"], []),
        provinceStats=province_stats,
        duplicatesSkipped=current_search_duplicates,
        requestedCount=limit,
        rawFetched=cached_stat("rawItems") or len(cached_leads),
        eligibleReturned=len(leads),
        websitesSkipped=cached_stat("websitesSkipped") or sum(1 for lead in cached_leads if lead_has_website(lead)),
        noContactSkipped=cached_stat("noContactSkipped") or sum(1 for lead in cached_leads if not lead_has_contact(lead)),
        generatedDuplicatesSkipped=already_deployed + active_deployment + policy_excluded,
        currentSearchDuplicatesSkipped=current_search_duplicates,
        alreadyDeployedSkipped=already_deployed,
        activeDeploymentSkipped=active_deployment,
        policyExcludedSkipped=policy_excluded,
        locationSkipped=cached_stat("locationSkipped"),
        invalidRecordSkipped=cached_stat("invalidRecordSkipped"),
        reusedPendingOrFailed=cached_stat("reusedPendingOrFailed") + revalidated_reused,
        targetOverflowSkipped=cached_stat("targetOverflowSkipped") + max(0, len(eligible) - len(leads)),
        shortfall=shortfall,
        stopReason="TARGET_MET" if not shortfall else first_stats.get("stopReason") or "RESULTS_EXHAUSTED",
        searchVariantCount=int(first_stats.get("searchVariantCount") or 0),
        providerStatus=first_stats.get("providerStatus") or "CACHE",
        providerDurationSeconds=float(first_stats.get("providerDurationSeconds") or 0),
        emailLeads=email_count,
        phoneLeads=phone_count,
        emailAndPhoneLeads=sum(
            1
            for lead in leads
            if normalize_email_identity(lead.email) and compact_text(lead.phone)
        ),
        cached=True,
    )


def save_pipeline_run(
    pipeline_id: str,
    status: str,
    template_id: str,
    source_batch_id: Optional[str],
    lead_count: int,
    completed_count: int,
    pending_count: int,
    failed_count: int,
    warnings: List[str],
    created_at: Optional[str] = None,
) -> None:
    timestamp = now_iso()
    with get_pipeline_db() as db:
        db.execute(
            """
            INSERT INTO pipeline_runs (
                pipeline_id, status, template_id, source_batch_id, created_at, updated_at,
                lead_count, completed_count, pending_count, failed_count, warnings_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pipeline_id) DO UPDATE SET
                status = excluded.status,
                updated_at = excluded.updated_at,
                completed_count = excluded.completed_count,
                pending_count = excluded.pending_count,
                failed_count = excluded.failed_count,
                warnings_json = excluded.warnings_json
            """,
            (
                pipeline_id,
                status,
                template_id,
                source_batch_id,
                created_at or timestamp,
                timestamp,
                lead_count,
                completed_count,
                pending_count,
                failed_count,
                json.dumps(warnings, default=str),
            ),
        )


def refresh_pipeline_run_status_from_approvals(pipeline_id: str) -> None:
    with get_pipeline_db() as db:
        run = db.execute(
            "SELECT * FROM pipeline_runs WHERE pipeline_id = ?",
            (pipeline_id,),
        ).fetchone()
        if not run:
            return

        rows = db.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM approval_records
            WHERE pipeline_id = ?
            GROUP BY status
            """,
            (pipeline_id,),
        ).fetchall()
        counts = {row["status"]: row["count"] for row in rows}
        pending_count = sum(
            counts.get(value, 0)
            for value in ("PENDING", "AWAITING_DEPLOYMENT", "EXPORTING", "GENERATION_FAILED")
        )
        completed_count = counts.get("APPROVED", 0)
        failed_count = (
            counts.get("REJECTED", 0)
            + counts.get("EXPORT_FAILED", 0)
            + counts.get("PUBLISH_FAILED", 0)
            + counts.get("DEPLOY_FAILED", 0)
            + counts.get("DEPLOYED_ZENDESK_FAILED", 0)
        )
        total = sum(counts.values())

        if pending_count:
            status = "PENDING_APPROVAL" if not completed_count and not failed_count else "PARTIAL_PENDING"
        elif total and completed_count == total:
            status = "COMPLETED"
        elif completed_count:
            status = "PARTIAL_FAILURE"
        elif counts.get("SUPERSEDED", 0) == total and total:
            status = "SUPERSEDED"
        else:
            status = "FAILED" if failed_count else run["status"]

        db.execute(
            """
            UPDATE pipeline_runs
            SET status = ?, updated_at = ?, completed_count = ?, pending_count = ?, failed_count = ?
            WHERE pipeline_id = ?
            """,
            (status, now_iso(), completed_count, pending_count, failed_count, pipeline_id),
        )


def record_pipeline_step(
    pipeline_id: str,
    canonical_key: Optional[str],
    step: str,
    status: str,
    provider: Optional[str],
    message: str,
    started_at: str,
    finished_at: str,
    retryable: bool = False,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    started = datetime.fromisoformat(started_at)
    finished = datetime.fromisoformat(finished_at)
    duration_ms = round((finished - started).total_seconds() * 1000, 2)
    safe_message = sanitize_message(message)
    snapshot = {
        "step": step,
        "status": status,
        "provider": provider,
        "message": safe_message,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "durationMs": duration_ms,
        "retryable": retryable,
        "details": redact_value(details or {}),
    }

    with get_pipeline_db() as db:
        db.execute(
            """
            INSERT INTO pipeline_steps (
                id, pipeline_id, canonical_lead_key, step, status, provider, message,
                started_at, finished_at, duration_ms, retryable, details_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                pipeline_id,
                canonical_key,
                step,
                status,
                provider,
                safe_message,
                started_at,
                finished_at,
                duration_ms,
                1 if retryable else 0,
                json.dumps(redact_value(details or {}), default=str),
            ),
        )

    return snapshot


def record_skipped_pipeline_step(
    pipeline_id: str,
    canonical_key: Optional[str],
    step: str,
    message: str,
    provider: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    timestamp = now_iso()
    return record_pipeline_step(
        pipeline_id=pipeline_id,
        canonical_key=canonical_key,
        step=step,
        status="SKIPPED",
        provider=provider,
        message=message,
        started_at=timestamp,
        finished_at=timestamp,
        retryable=False,
        details=details,
    )


def structured_pipeline_error(
    step: str,
    error: Exception,
    provider: Optional[str] = None,
    retryable: bool = False,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "step": step,
        "provider": provider,
        "message": sanitize_message(error),
        "retryable": retryable,
        "details": redact_value(details or {}),
    }


def skipped_pipeline_result(
    lead: DiscoveredLead,
    canonical_key: str,
    pipeline_id: str,
    status: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
) -> PipelineLeadResult:
    step_name = status.lower()
    step_history = [
        record_skipped_pipeline_step(
            pipeline_id,
            canonical_key,
            step_name,
            message,
            details=details,
        )
    ]
    return PipelineLeadResult(
        leadKey=lead.leadKey,
        canonicalLeadKey=canonical_key,
        businessName=lead.businessName,
        status=status,
        pipelineStatus=status,
        currentStep=step_name,
        stepHistory=step_history,
        structuredErrors=[
            {
                "step": step_name,
                "provider": None,
                "message": sanitize_message(message),
                "retryable": False,
                "details": redact_value(details or {}),
            }
        ],
        errors=[sanitize_message(message)],
    )


def html_checksum(site_html: str) -> str:
    return hashlib.sha256(site_html.encode("utf-8")).hexdigest()


def create_approval_record(
    pipeline_id: str,
    canonical_key: str,
    lead_key: str,
    business_name: str,
    site_html: Optional[str],
    context: Dict[str, Any],
    site_content: Dict[str, Any],
    template: Dict[str, Any],
    status: str = "PENDING",
    approval_id: Optional[str] = None,
) -> str:
    approval_id = compact_text(approval_id) or str(uuid4())
    timestamp = now_iso()
    with get_pipeline_db() as db:
        db.execute(
            """
            INSERT OR IGNORE INTO approval_records (
                id, pipeline_id, canonical_lead_key, lead_key, business_name, status,
                html, html_checksum, context_json, site_content_json, template_json,
                created_at, updated_at, publish_mode
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                approval_id,
                pipeline_id,
                canonical_key,
                lead_key,
                business_name,
                status,
                site_html,
                html_checksum(site_html) if site_html else None,
                json.dumps(context, default=str),
                json.dumps(site_content, default=str),
                json.dumps(template, default=str),
                timestamp,
                timestamp,
                "github-netlify",
            ),
        )
    return approval_id


def get_zendesk_field_settings() -> Dict[str, Optional[str]]:
    with get_pipeline_db() as db:
        rows = db.execute("SELECT field_key, field_id FROM zendesk_field_settings").fetchall()
    values = {key: None for key in ZENDESK_FIELD_KEYS}
    for row in rows:
        if row["field_key"] in values:
            values[row["field_key"]] = row["field_id"]
    return values


def save_zendesk_field_settings(fields: Dict[str, Optional[str]]) -> Dict[str, Optional[str]]:
    timestamp = now_iso()
    current = get_zendesk_field_settings()
    cleaned = {
        key: (compact_text(fields.get(key)) or None) if key in fields else current.get(key)
        for key in ZENDESK_FIELD_KEYS
    }
    with get_pipeline_db() as db:
        for key, value in cleaned.items():
            db.execute(
                """
                INSERT INTO zendesk_field_settings (field_key, field_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(field_key) DO UPDATE SET
                    field_id = excluded.field_id,
                    updated_at = excluded.updated_at
                """,
                (key, value, timestamp),
            )
    return get_zendesk_field_settings()


def list_zendesk_provisioned_resources() -> Dict[str, Dict[str, Any]]:
    with get_pipeline_db() as db:
        rows = db.execute(
            "SELECT * FROM zendesk_provisioned_resources ORDER BY resource_type, resource_key"
        ).fetchall()
    return {
        row["resource_key"]: {
            "resourceKey": row["resource_key"],
            "resourceType": row["resource_type"],
            "resourceId": row["resource_id"],
            "displayName": row["display_name"],
            "metadata": safe_json_loads(row["metadata_json"], {}),
            "updatedAt": row["updated_at"],
        }
        for row in rows
    }


def save_zendesk_provisioned_resource(
    resource_key: str,
    resource_type: str,
    resource_id: Optional[Any],
    display_name: Optional[str],
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    timestamp = now_iso()
    with get_pipeline_db() as db:
        db.execute(
            """
            INSERT INTO zendesk_provisioned_resources (
                resource_key, resource_type, resource_id, display_name, metadata_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(resource_key) DO UPDATE SET
                resource_type = excluded.resource_type,
                resource_id = excluded.resource_id,
                display_name = excluded.display_name,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                resource_key,
                resource_type,
                compact_text(resource_id) or None,
                compact_text(display_name) or None,
                json.dumps(metadata or {}, default=str),
                timestamp,
            ),
        )
    return list_zendesk_provisioned_resources()[resource_key]


def zendesk_managed_field_value(key: str, value: Any, resource: Optional[Dict[str, Any]]) -> Any:
    if not resource or resource.get("metadata", {}).get("type") != "tagger":
        return value
    normalized = compact_text(value).upper().replace("-", "_").replace(" ", "_")
    mappings = {
        "contactChannel": {
            "EMAIL": "asf_cf_channel_email",
            "PHONE": "asf_cf_channel_phone",
            "CALL": "asf_cf_channel_phone",
        },
        "leadStatus": {
            "AWAITING_DEPLOYMENT": "asf_cf_status_awaiting_deployment",
            "GENERATING": "asf_cf_status_generating",
            "DEPLOYED": "asf_cf_status_deployed",
            "EMAIL_SENT": "asf_cf_status_email_sent",
            "PHONE_UPDATED": "asf_cf_status_phone_updated",
            "FAILED": "asf_cf_status_failed",
        },
        "phoneCallStatus": {
            "NEW": "asf_cf_call_new",
            "ATTEMPTED": "asf_cf_call_attempted",
            "CONNECTED": "asf_cf_call_connected",
            "FOLLOW_UP": "asf_cf_call_follow_up",
            "QUALIFIED": "asf_cf_call_qualified",
            "NOT_INTERESTED": "asf_cf_call_not_interested",
            "NO_ANSWER": "asf_cf_call_no_answer",
        },
    }
    if key == "phoneCallStatus":
        return mappings[key].get(normalized, "asf_cf_call_other")
    return mappings.get(key, {}).get(normalized, value)


def zendesk_custom_fields(values: Dict[str, Any]) -> List[Dict[str, Any]]:
    settings = get_zendesk_field_settings()
    resources = list_zendesk_provisioned_resources()
    fields: List[Dict[str, Any]] = []
    for key, value in values.items():
        field_id = compact_text(settings.get(key))
        if not field_id or value in (None, ""):
            continue
        try:
            parsed_id: Any = int(field_id)
        except ValueError:
            parsed_id = field_id
        managed_resource = resources.get(f"field:{key}")
        if managed_resource and compact_text(managed_resource.get("resourceId")) != field_id:
            managed_resource = None
        fields.append({"id": parsed_id, "value": zendesk_managed_field_value(key, value, managed_resource)})
    return fields


def zendesk_ticket_routing_fields(channel: str) -> Dict[str, Any]:
    resources = list_zendesk_provisioned_resources()
    routing: Dict[str, Any] = {}
    normalized_channel = "email" if compact_text(channel).lower() == "email" else "phone"
    form = resources.get(f"form:{normalized_channel}")
    if form and compact_text(form.get("resourceId")):
        form_id = compact_text(form["resourceId"])
        routing["ticket_form_id"] = int(form_id) if form_id.isdigit() else form_id
    brand_id = compact_text(resources.get("configuration", {}).get("metadata", {}).get("brandId"))
    if brand_id:
        routing["brand_id"] = int(brand_id) if brand_id.isdigit() else brand_id
    return routing


def require_zendesk_ticket_contract(channel: str, verify_live: bool = False) -> Dict[str, Any]:
    normalized_channel = compact_text(channel).lower()
    if normalized_channel not in {"email", "phone"}:
        raise HTTPException(status_code=400, detail="Zendesk intake channel must be email or phone.")

    readiness = zendesk_workspace_readiness()
    resources = list_zendesk_provisioned_resources()
    settings = get_zendesk_field_settings()
    configuration = resources.get("configuration", {})
    configuration_metadata = configuration.get("metadata", {})
    brand_id = compact_text(configuration_metadata.get("brandId"))
    form = resources.get(f"form:{normalized_channel}", {})
    form_id = compact_text(form.get("resourceId"))
    problems: List[str] = []

    if not readiness.get("workspaceReady"):
        problems.append("the connected Zendesk workspace is not fully provisioned")
    if not brand_id:
        problems.append("the selected brand ID is missing")
    if not form_id:
        problems.append(f"the managed {normalized_channel} form ID is missing")
    form_metadata = form.get("metadata", {})
    if compact_text(form_metadata.get("channel")) != normalized_channel:
        problems.append(f"the saved {normalized_channel} form channel does not match")
    if brand_id and compact_text(form_metadata.get("brandId")) != brand_id:
        problems.append(f"the saved {normalized_channel} form is not assigned to the selected brand")

    definitions = [definition for definition in ZENDESK_FIELD_BLUEPRINT if normalized_channel in definition["forms"]]
    field_ids: Dict[str, str] = {}
    for definition in definitions:
        key = definition["key"]
        setting_id = compact_text(settings.get(key))
        resource = resources.get(f"field:{key}", {})
        resource_id = compact_text(resource.get("resourceId"))
        if not setting_id or not resource_id:
            problems.append(f"the managed field mapping for {key} is missing")
            continue
        if setting_id != resource_id:
            problems.append(f"the managed field mapping for {key} is inconsistent")
            continue
        field_ids[key] = setting_id

    if problems:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ZENDESK_TICKET_CONTRACT_INVALID",
                "message": "Zendesk ticket creation was stopped before any records were changed.",
                "channel": normalized_channel,
                "problems": list(dict.fromkeys(problems)),
            },
        )

    if verify_live:
        try:
            live_brand = zendesk_api_request("get", f"/brands/{brand_id}.json").get("brand") or {}
            live_form = zendesk_api_request("get", f"/ticket_forms/{form_id}.json").get("ticket_form") or {}
        except Exception as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ZENDESK_TICKET_CONTRACT_UNREACHABLE",
                    "message": "The selected Zendesk brand and form could not be verified.",
                    "channel": normalized_channel,
                    "reason": sanitize_message(error),
                },
            ) from error

        live_problems: List[str] = []
        if compact_text(live_brand.get("id")) != brand_id or not live_brand.get("active", True):
            live_problems.append("the selected brand is missing or inactive")
        if compact_text(live_form.get("id")) != form_id or not live_form.get("active", True):
            live_problems.append(f"the managed {normalized_channel} form is missing or inactive")
        restricted_brands = {compact_text(value) for value in (live_form.get("restricted_brand_ids") or [])}
        if not live_form.get("in_all_brands") and brand_id not in restricted_brands:
            live_problems.append(f"the managed {normalized_channel} form is not available on the selected brand")
        live_field_ids = {compact_text(value) for value in (live_form.get("ticket_field_ids") or [])}
        missing_live_fields = [key for key, value in field_ids.items() if value not in live_field_ids]
        if missing_live_fields:
            live_problems.append(
                f"the managed {normalized_channel} form is missing fields: {', '.join(missing_live_fields)}"
            )
        if live_problems:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ZENDESK_TICKET_CONTRACT_INVALID",
                    "message": "Zendesk ticket creation was stopped because the live brand/form contract changed.",
                    "channel": normalized_channel,
                    "problems": live_problems,
                },
            )

    return {
        "channel": normalized_channel,
        "brandId": int(brand_id) if brand_id.isdigit() else brand_id,
        "formId": int(form_id) if form_id.isdigit() else form_id,
        "fieldIds": field_ids,
    }


def verify_zendesk_ticket_contracts(channels: List[str]) -> Dict[str, Dict[str, Any]]:
    normalized = [value for value in dict.fromkeys(compact_text(channel).lower() for channel in channels) if value]
    return {channel: require_zendesk_ticket_contract(channel, verify_live=True) for channel in normalized}


def list_zendesk_ticket_links(approval_id: str) -> List[Dict[str, Any]]:
    with get_pipeline_db() as db:
        rows = db.execute(
            """
            SELECT *
            FROM zendesk_ticket_links
            WHERE approval_id = ?
            ORDER BY created_at ASC
            """,
            (approval_id,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "approvalId": row["approval_id"],
            "canonicalLeadKey": row["canonical_lead_key"],
            "pipelineId": row["pipeline_id"],
            "externalId": row["external_id"],
            "ticketId": row["ticket_id"],
            "ticketUrl": row["ticket_url"],
            "channel": row["channel"],
            "stage": row["stage"],
            "status": row["status"],
            "tags": safe_json_loads(row["tags_json"], []),
            "payload": safe_json_loads(row["payload_json"], {}),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }
        for row in rows
    ]


def get_zendesk_ticket_link(
    approval_id: str,
    channel: Optional[str] = None,
    stage: str = "intake",
    ticket_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    clauses = ["approval_id = ?"]
    params: List[Any] = [approval_id]
    if channel:
        clauses.append("channel = ?")
        params.append(channel)
    if stage:
        clauses.append("stage = ?")
        params.append(stage)
    if ticket_id:
        clauses.append("ticket_id = ?")
        params.append(ticket_id)
    with get_pipeline_db() as db:
        row = db.execute(
            f"SELECT * FROM zendesk_ticket_links WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT 1",
            params,
        ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "approvalId": row["approval_id"],
        "canonicalLeadKey": row["canonical_lead_key"],
        "pipelineId": row["pipeline_id"],
        "externalId": row["external_id"],
        "ticketId": row["ticket_id"],
        "ticketUrl": row["ticket_url"],
        "channel": row["channel"],
        "stage": row["stage"],
        "status": row["status"],
        "tags": safe_json_loads(row["tags_json"], []),
        "payload": safe_json_loads(row["payload_json"], {}),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def get_zendesk_ticket_link_by_external_id(external_id: str) -> Optional[Dict[str, Any]]:
    normalized = compact_text(external_id)
    if not normalized:
        return None
    with get_pipeline_db() as db:
        row = db.execute(
            "SELECT * FROM zendesk_ticket_links WHERE external_id = ? ORDER BY created_at DESC LIMIT 1",
            (normalized,),
        ).fetchone()
    if not row:
        return None
    return get_zendesk_ticket_link(
        row["approval_id"], row["channel"], row["stage"], row["ticket_id"]
    )


def save_zendesk_ticket_link(
    approval_id: str,
    canonical_key: str,
    pipeline_id: str,
    channel: str,
    stage: str,
    ticket_id: Optional[int],
    ticket_url: Optional[str],
    status: str,
    tags: List[str],
    payload: Dict[str, Any],
    external_id: Optional[str] = None,
) -> Dict[str, Any]:
    timestamp = now_iso()
    link_id = str(uuid4())

    with get_pipeline_db() as db:
        db.execute(
            """
            INSERT INTO zendesk_ticket_links (
                id, approval_id, canonical_lead_key, pipeline_id, external_id, ticket_id, ticket_url,
                channel, stage, status, tags_json, payload_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(approval_id, channel, stage) DO UPDATE SET
                external_id = COALESCE(excluded.external_id, zendesk_ticket_links.external_id),
                ticket_id = COALESCE(excluded.ticket_id, zendesk_ticket_links.ticket_id),
                ticket_url = COALESCE(excluded.ticket_url, zendesk_ticket_links.ticket_url),
                status = excluded.status,
                tags_json = excluded.tags_json,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                link_id,
                approval_id,
                canonical_key,
                pipeline_id,
                compact_text(external_id) or None,
                ticket_id,
                ticket_url,
                channel,
                stage,
                status,
                json.dumps(tags, default=str),
                json.dumps(payload, default=str),
                timestamp,
                timestamp,
            ),
        )
    return get_zendesk_ticket_link(approval_id, channel, stage) or {}


def record_zendesk_webhook_event(
    action: str,
    status: str,
    payload: Dict[str, Any],
    result: Optional[Dict[str, Any]] = None,
    message: Optional[str] = None,
) -> None:
    with get_pipeline_db() as db:
        db.execute(
            """
            INSERT INTO zendesk_webhook_events (
                id, approval_id, canonical_lead_key, ticket_id, channel, action,
                status, message, payload_json, result_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                payload.get("approvalId"),
                payload.get("canonicalLeadKey"),
                payload.get("zendeskTicketId"),
                payload.get("channel"),
                action,
                status,
                sanitize_message(message or ""),
                json.dumps(redact_value(payload), default=str),
                json.dumps(redact_value(result or {}), default=str),
                now_iso(),
            ),
        )


def approval_row_to_dict(row: sqlite3.Row, include_html: bool = False) -> Dict[str, Any]:
    context = safe_json_loads(row["context_json"], {})
    for key in ["ownerName", "ownerEmail", "ownerStatus"]:
        context.pop(key, None)
    publish_mode = row["publish_mode"] or "github-netlify"
    deployment_history = deployment_history_row_to_dict(get_deployment_history_row(row["deployment_history_id"])) if row["deployment_history_id"] else None
    approval = {
        "approvalId": row["id"],
        "pipelineId": row["pipeline_id"],
        "canonicalLeadKey": row["canonical_lead_key"],
        "leadKey": row["lead_key"],
        "businessName": row["business_name"],
        "status": row["status"],
        "htmlChecksum": row["html_checksum"],
        "previewAvailable": bool(row["html"]),
        "context": context,
        "siteContent": safe_json_loads(row["site_content_json"], {}),
        "outreachDraft": safe_json_loads(row["outreach_json"], None),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "approvedBy": row["approved_by"],
        "rejectedBy": row["rejected_by"],
        "notes": row["notes"],
        "deploymentHistoryId": row["deployment_history_id"],
        "deploymentHistory": deployment_history,
        "publishMode": publish_mode,
        "deploymentMode": deployment_mode_label(publish_mode),
        "githubExport": safe_json_loads(row["github_export_json"], None),
        "zendesk": safe_json_loads(row["zendesk_json"], None),
        "zendeskTickets": list_zendesk_ticket_links(row["id"]),
        "errors": safe_json_loads(row["errors_json"], []),
    }
    if include_html:
        approval["pendingPreviewHtml"] = ensure_required_site_features(row["html"], context) if row["html"] else None
    return approval


def get_approval_or_404(approval_id: str) -> sqlite3.Row:
    with get_pipeline_db() as db:
        row = db.execute(
            "SELECT * FROM approval_records WHERE id = ?",
            (approval_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Approval record not found.")
    return row


def latest_reusable_approval_for_lead(canonical_key: str) -> Optional[sqlite3.Row]:
    with get_pipeline_db() as db:
        return db.execute(
            """
            SELECT *
            FROM approval_records
            WHERE canonical_lead_key = ?
              AND status IN ('PENDING', 'EXPORT_FAILED', 'APPROVED', 'DEPLOYED_ZENDESK_FAILED')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (canonical_key,),
        ).fetchone()


def supersede_pending_approvals(canonical_key: str, except_approval_id: str, requested_by: str) -> None:
    with get_pipeline_db() as db:
        db.execute(
            """
            UPDATE approval_records
            SET status = 'SUPERSEDED', updated_at = ?, notes = ?
            WHERE canonical_lead_key = ? AND status IN ('PENDING', 'EXPORT_FAILED') AND id != ?
            """,
            (
                now_iso(),
                f"Superseded by force regenerate from {requested_by}. New approval: {except_approval_id}",
                canonical_key,
                except_approval_id,
            ),
        )


def get_deployment_history_row(deployment_history_id: Optional[str]) -> Optional[sqlite3.Row]:
    if not deployment_history_id:
        return None
    with get_pipeline_db() as db:
        return db.execute(
            "SELECT * FROM deployment_history WHERE id = ?",
            (deployment_history_id,),
        ).fetchone()


def latest_deployment_history_for_lead(canonical_key: str) -> Optional[sqlite3.Row]:
    with get_pipeline_db() as db:
        return db.execute(
            """
            SELECT *
            FROM deployment_history
            WHERE canonical_lead_key = ?
            ORDER BY deployed_at DESC
            LIMIT 1
            """,
            (canonical_key,),
        ).fetchone()


def deployment_history_row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    item = dict(row)
    item["raw"] = safe_json_loads(item.pop("raw_json", None), {})
    item["githubExport"] = safe_json_loads(item.pop("github_export_json", None), None)
    item["publishMode"] = item.pop("publish_mode", None) or "github-netlify"
    item["deploymentMode"] = deployment_mode_label(item["publishMode"])
    return item


def deployment_from_history(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    history = deployment_history_row_to_dict(row)
    if not history:
        return None
    raw = history.get("raw")
    if isinstance(raw, dict) and raw:
        raw.setdefault("deploymentHistoryId", history.get("id"))
        raw.setdefault("publishMode", history.get("publishMode"))
        raw.setdefault("deploymentMode", history.get("deploymentMode"))
        if history.get("githubExport") and not raw.get("githubExport"):
            raw["githubExport"] = history["githubExport"]
        return raw
    return {
        "deploymentHistoryId": history.get("id"),
        "siteId": history.get("site_id"),
        "siteName": history.get("site_name"),
        "deployId": history.get("deploy_id"),
        "buildId": history.get("build_id"),
        "state": history.get("state"),
        "url": history.get("url"),
        "deployAction": history.get("deploy_action"),
        "htmlChecksum": history.get("html_checksum"),
        "deployedAt": history.get("deployed_at"),
        "publishMode": history.get("publishMode"),
        "deploymentMode": history.get("deploymentMode"),
        "githubExport": history.get("githubExport"),
        "githubRepoUrl": history.get("github_repo_url"),
        "githubRepoFullName": history.get("github_repo_full_name"),
        "commitSha": history.get("commit_sha"),
    }


def pipeline_result_from_reused_approval(
    lead: DiscoveredLead,
    row: sqlite3.Row,
    pipeline_id: str,
) -> PipelineLeadResult:
    canonical_key = row["canonical_lead_key"]
    step_history = [
        record_skipped_pipeline_step(
            pipeline_id,
            canonical_key,
            "resume_existing",
            f"Reused existing {row['status'].lower()} approval.",
            details={"approvalId": row["id"], "status": row["status"]},
        )
    ]
    context = safe_json_loads(row["context_json"], {})
    for key in ["ownerName", "ownerEmail", "ownerStatus"]:
        context.pop(key, None)
    site_content = safe_json_loads(row["site_content_json"], {})
    github_export = safe_json_loads(row["github_export_json"], None)
    publish_mode = row["publish_mode"] or "github-netlify"

    if row["status"] in {"PENDING", "EXPORT_FAILED"}:
        result_status = "PENDING_APPROVAL" if row["status"] == "PENDING" else "EXPORT_FAILED"
        return PipelineLeadResult(
            leadKey=lead.leadKey,
            canonicalLeadKey=canonical_key,
            businessName=row["business_name"],
            status=result_status,
            pipelineStatus=result_status,
            currentStep="reused_pending_approval" if row["status"] == "PENDING" else "reused_export_failed",
            stepHistory=step_history,
            approvalStatus=row["status"],
            pendingApprovalId=row["id"],
            pendingPreviewHtml=row["html"],
            cleanedLead=context,
            siteContent=site_content,
            publishMode=publish_mode,
            githubExport=github_export,
        )

    deployment_row = get_deployment_history_row(row["deployment_history_id"]) or latest_deployment_history_for_lead(canonical_key)
    deployment = deployment_from_history(deployment_row)
    deployment_history = deployment_history_row_to_dict(deployment_row)

    return PipelineLeadResult(
        leadKey=lead.leadKey,
        canonicalLeadKey=canonical_key,
        businessName=row["business_name"],
        status="COMPLETED_REUSED",
        pipelineStatus="COMPLETED_REUSED",
        currentStep="reused_deployment",
        stepHistory=step_history,
        approvalStatus=row["status"],
        pendingApprovalId=row["id"],
        pendingPreviewHtml=None,
        cleanedLead=context,
        siteContent=site_content,
        outreachDraft=safe_json_loads(row["outreach_json"], None),
        deployment=deployment,
        deploymentHistory=deployment_history,
        zendesk=safe_json_loads(row["zendesk_json"], None),
        publishMode=publish_mode,
        githubExport=github_export or (deployment or {}).get("githubExport"),
    )


def legacy_campaign_name(
    preset_id: Optional[str],
    query: Optional[str],
    location: Optional[str],
    created_at: Optional[str],
    suffix: Optional[str] = None,
) -> str:
    preset_label = compact_text(preset_id).replace("-", " ").title()
    query_label = re.split(r"\s+in\s+", compact_text(query), maxsplit=1, flags=re.IGNORECASE)[0]
    label = preset_label or query_label.title() or "Legacy pipeline"
    place = compact_text(location).split(",", 1)[0] or "Imported"
    try:
        date_label = datetime.fromisoformat(compact_text(created_at)).strftime("%d %b %Y")
    except (TypeError, ValueError):
        date_label = "Existing data"
    parts = [label, place, date_label]
    if suffix:
        parts.append(suffix)
    return " · ".join(parts)


def legacy_approval_status(approval: sqlite3.Row, deployment: Optional[sqlite3.Row]) -> str:
    approval_status = compact_text(approval["status"]).upper()
    deployment_state = compact_text(deployment["state"] if deployment else "").lower()
    if deployment_state == "ready" or approval_status in {"APPROVED", "DEPLOYED_ZENDESK_FAILED"}:
        return "DEPLOYED"
    if approval_status in {"DEPLOY_FAILED", "PUBLISH_FAILED", "EXPORT_FAILED", "GENERATION_FAILED"}:
        return "DEPLOY_FAILED"
    if approval_status in {"REJECTED", "SUPERSEDED"}:
        return "FAILED"
    if approval_status in {"PENDING", "EXPORTING"}:
        return "ARTIFACT_READY"
    return "AWAITING_DEPLOYMENT"


def backfill_legacy_campaign_data() -> Dict[str, int]:
    """Populate campaign tables from pre-campaign pipeline data without duplicating rows."""
    stats = {
        "campaignsCreated": 0,
        "emailLeadsCreated": 0,
        "callLeadsCreated": 0,
        "deploymentsCreated": 0,
    }
    timestamp = now_iso()

    with get_pipeline_db() as db:
        runs = db.execute(
            """
            SELECT r.*, b.preset_id, b.query, b.location, b.lead_count AS batch_lead_count,
                   b.leads_json, b.warnings_json AS batch_warnings_json
            FROM pipeline_runs r
            LEFT JOIN discovery_batches b ON b.batch_id = r.source_batch_id
            ORDER BY r.created_at ASC
            """
        ).fetchall()
        referenced_batches = {row["source_batch_id"] for row in runs if row["source_batch_id"]}

        for run in runs:
            campaign_id = f"legacy-run-{run['pipeline_id']}"
            approvals = db.execute(
                "SELECT * FROM approval_records WHERE pipeline_id = ? ORDER BY updated_at DESC",
                (run["pipeline_id"],),
            ).fetchall()
            context = safe_json_loads(approvals[0]["context_json"], {}) if approvals else {}
            industry = compact_text(run["preset_id"]).replace("-", " ").title() or compact_text(
                context.get("industry") or context.get("category"), "Legacy"
            )
            location = compact_text(run["location"] or context.get("location"), "South Africa")
            query = compact_text(run["query"], industry)
            batch_leads = safe_json_loads(run["leads_json"], [])
            channel_values: List[str] = []
            if any(normalize_email_identity(item.get("email")) for item in batch_leads if isinstance(item, dict)):
                channel_values.append("email")
            if any(compact_text(item.get("phone")) for item in batch_leads if isinstance(item, dict)):
                channel_values.append("phone")
            if not channel_values:
                if any(normalize_email_identity(safe_json_loads(item["context_json"], {}).get("email")) for item in approvals):
                    channel_values.append("email")
                if any(compact_text(safe_json_loads(item["context_json"], {}).get("phone")) for item in approvals):
                    channel_values.append("phone")
            if not channel_values:
                channel_values = ["email", "phone"]

            before = db.total_changes
            db.execute(
                """
                INSERT OR IGNORE INTO campaigns (
                    id, name, batch_id, preset_id, industry, query, location, requested_count,
                    discovered_count, channel_filter, status, warnings_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign_id,
                    legacy_campaign_name(
                        run["preset_id"], query, location, run["created_at"],
                        f"Run {run['pipeline_id'][:6]}",
                    ),
                    run["source_batch_id"],
                    run["preset_id"],
                    industry,
                    query,
                    location,
                    run["lead_count"] or run["batch_lead_count"] or len(batch_leads),
                    run["batch_lead_count"] or len(batch_leads) or run["lead_count"],
                    ",".join(channel_values),
                    run["status"],
                    run["batch_warnings_json"] or run["warnings_json"],
                    run["created_at"],
                    run["updated_at"],
                ),
            )
            if db.total_changes > before:
                stats["campaignsCreated"] += 1

            for approval in approvals:
                approval_context = safe_json_loads(approval["context_json"], {})
                ticket_links = db.execute(
                    "SELECT * FROM zendesk_ticket_links WHERE approval_id = ? ORDER BY created_at ASC",
                    (approval["id"],),
                ).fetchall()
                link_by_channel = {
                    compact_text(link["channel"]).lower(): link
                    for link in ticket_links
                    if compact_text(link["channel"]).lower() in {"email", "phone"}
                }
                legacy_zendesk = safe_json_loads(approval["zendesk_json"], {})
                channels: List[str] = list(link_by_channel.keys())
                if normalize_email_identity(approval_context.get("email")) and "email" not in channels:
                    channels.append("email")
                if compact_text(approval_context.get("phone")) and "phone" not in channels:
                    channels.append("phone")
                if not channels:
                    channels = [
                        "email" if normalize_email_identity(approval_context.get("email"))
                        else "phone" if compact_text(approval_context.get("phone"))
                        else "unknown"
                    ]
                channels = [channel for channel in channels if channel in {"email", "phone"}]
                if not channels:
                    continue

                deployment = None
                if approval["deployment_history_id"]:
                    deployment = db.execute(
                        "SELECT * FROM deployment_history WHERE id = ?",
                        (approval["deployment_history_id"],),
                    ).fetchone()
                if not deployment:
                    deployment = db.execute(
                        "SELECT * FROM deployment_history WHERE approval_id = ? ORDER BY deployed_at DESC LIMIT 1",
                        (approval["id"],),
                    ).fetchone()
                github_export = safe_json_loads(approval["github_export_json"], {})
                if not github_export:
                    repo = db.execute(
                        "SELECT * FROM github_site_repos WHERE canonical_lead_key = ?",
                        (approval["canonical_lead_key"],),
                    ).fetchone()
                    if repo:
                        github_export = {
                            "repoUrl": repo["repo_url"],
                            "repository": repo["repo_full_name"],
                            "commitSha": repo["commit_sha"],
                            "exportAction": "CREATED" if repo["created_at"] == repo["updated_at"] else "UPDATED",
                        }
                workflow_status = legacy_approval_status(approval, deployment)
                requested = bool(
                    deployment
                    or approval["approved_by"]
                    or compact_text(approval["status"]).upper() in {"APPROVED", "DEPLOY_FAILED", "PUBLISH_FAILED", "DEPLOYED_ZENDESK_FAILED"}
                )
                deployment_id = f"legacy-deployment-{approval['id']}"
                primary_channel = compact_text(approval_context.get("contactChannel")).lower()
                if primary_channel not in channels:
                    primary_channel = channels[0]
                fallback_ticket_channel = compact_text(legacy_zendesk.get("contactType")).lower()
                if fallback_ticket_channel not in channels:
                    fallback_ticket_channel = primary_channel
                repo_url = github_export.get("repoUrl") or (deployment["github_repo_url"] if deployment else None)
                live_url = deployment["url"] if deployment else None
                ai_generation_count = 1 if (
                    approval["html_checksum"] or approval["site_content_json"] or github_export
                ) else 0

                before = db.total_changes
                db.execute(
                    """
                    INSERT OR IGNORE INTO campaign_deployments (
                        id, campaign_id, canonical_lead_key, approval_id, channel, status,
                        ai_generation_count, repo_created, repo_url, live_url,
                        deployment_history_id, requested_at, completed_at, error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        deployment_id,
                        campaign_id,
                        approval["canonical_lead_key"],
                        approval["id"],
                        primary_channel,
                        workflow_status,
                        ai_generation_count,
                        1 if compact_text(github_export.get("exportAction")).upper() == "CREATED" else 0,
                        repo_url,
                        live_url,
                        deployment["id"] if deployment else None,
                        approval["updated_at"] if requested else None,
                        deployment["deployed_at"] if deployment and workflow_status == "DEPLOYED" else None,
                        compact_text(approval["errors_json"]) or None,
                        approval["created_at"],
                        approval["updated_at"],
                    ),
                )
                if db.total_changes > before:
                    stats["deploymentsCreated"] += 1

                field_values = {
                    "campaignId": campaign_id,
                    "campaignName": legacy_campaign_name(
                        run["preset_id"], query, location, run["created_at"],
                        f"Run {run['pipeline_id'][:6]}",
                    ),
                    "businessName": approval["business_name"],
                    "contactName": approval_context.get("contactName"),
                    "email": approval_context.get("email"),
                    "phone": approval_context.get("phone"),
                    "industry": approval_context.get("industry") or approval_context.get("category") or industry,
                    "location": approval_context.get("location") or location,
                    "address": approval_context.get("address"),
                    "sourceUrl": approval_context.get("sourceUrl"),
                }
                for channel in channels:
                    link = link_by_channel.get(channel)
                    payload = safe_json_loads(link["payload_json"], {}) if link else {}
                    channel_requested = bool(requested or payload.get("deployRequested"))
                    table = "campaign_email_leads" if channel == "email" else "campaign_call_leads"
                    contact_column = "email" if channel == "email" else "phone"
                    contact_value = normalize_email_identity(approval_context.get("email")) if channel == "email" else compact_text(approval_context.get("phone"))
                    if not contact_value:
                        contact_value = compact_text(payload.get("contact"))
                    if not contact_value:
                        continue
                    lead_id = f"legacy-{channel}-{approval['id']}"
                    before = db.total_changes
                    ticket_id = link["ticket_id"] if link else (
                        legacy_zendesk.get("ticketId") if channel == fallback_ticket_channel else None
                    )
                    db.execute(
                        f"""
                        INSERT OR IGNORE INTO {table} (
                            id, campaign_id, canonical_lead_key, approval_id, business_name,
                            contact_name, {contact_column}, source_url, status, deploy_requested,
                            ticket_id, deployment_id, fields_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            lead_id,
                            campaign_id,
                            approval["canonical_lead_key"],
                            approval["id"],
                            approval["business_name"],
                            approval_context.get("contactName"),
                            contact_value,
                            approval_context.get("sourceUrl"),
                            workflow_status,
                            1 if channel_requested else 0,
                            ticket_id,
                            deployment_id,
                            json.dumps({**field_values, "channel": channel}, default=str),
                            approval["created_at"],
                            approval["updated_at"],
                        ),
                    )
                    if db.total_changes > before:
                        stats["emailLeadsCreated" if channel == "email" else "callLeadsCreated"] += 1
                    elif ticket_id:
                        db.execute(
                            f"UPDATE {table} SET ticket_id = COALESCE(ticket_id, ?), updated_at = ? WHERE id = ?",
                            (ticket_id, approval["updated_at"], lead_id),
                        )

        orphan_batches = db.execute(
            "SELECT * FROM discovery_batches ORDER BY created_at ASC"
        ).fetchall()
        for batch in orphan_batches:
            if batch["batch_id"] in referenced_batches:
                continue
            campaign_id = f"legacy-batch-{batch['batch_id']}"
            leads = safe_json_loads(batch["leads_json"], [])
            channels: List[str] = []
            if any(normalize_email_identity(item.get("email")) for item in leads if isinstance(item, dict)):
                channels.append("email")
            if any(compact_text(item.get("phone")) for item in leads if isinstance(item, dict)):
                channels.append("phone")
            before = db.total_changes
            db.execute(
                """
                INSERT OR IGNORE INTO campaigns (
                    id, name, batch_id, preset_id, industry, query, location, requested_count,
                    discovered_count, channel_filter, status, warnings_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign_id,
                    legacy_campaign_name(batch["preset_id"], batch["query"], batch["location"], batch["created_at"], "Discovery"),
                    batch["batch_id"],
                    batch["preset_id"],
                    compact_text(batch["preset_id"]).replace("-", " ").title() or "Discovery",
                    batch["query"],
                    batch["location"],
                    batch["lead_count"],
                    batch["lead_count"],
                    ",".join(channels or ["email", "phone"]),
                    "DISCOVERED",
                    batch["warnings_json"],
                    batch["created_at"],
                    batch["created_at"],
                ),
            )
            if db.total_changes > before:
                stats["campaignsCreated"] += 1

    if any(stats.values()):
        log_event("info", "campaigns.legacy_backfill", "Legacy pipeline data populated campaign tables.", **stats)
    return stats


init_pipeline_db()
bootstrap_pipeline_seed_on_startup()
cleanup_pipeline_operational_data_on_startup()
bootstrap_zendesk_config_on_startup()
if env_enabled("ENABLE_LEGACY_CAMPAIGN_BACKFILL"):
    backfill_legacy_campaign_data()


def infer_country_code(location: str) -> Optional[str]:
    location_lower = compact_text(location).lower()
    location_tokens = set(re.findall(r"[a-z]+", location_lower))
    if (
        "south africa" in location_lower
        or location_lower.strip() in {"za", "zaf"}
        or bool({"za", "zaf"}.intersection(location_tokens))
        or any(term in location_lower for term in SOUTH_AFRICA_TERMS)
    ):
        return "za"
    return None


def build_google_maps_query(preset: Dict[str, Any], location: str, custom_query: Optional[str] = None) -> str:
    """Build a Google Maps search query with clear geographic intent."""
    query_term = compact_text(custom_query) or compact_text(preset.get("query", ""))
    if not query_term:
        query_term = preset.get("industry", "services")

    location_term = compact_text(location, "South Africa")
    return f"{query_term} in {location_term}"


def apify_google_maps_search_variants(query: str, location: str = "") -> List[str]:
    primary = compact_text(query)
    location_term = compact_text(location)
    location_suffix = f" in {location_term}"
    if (
        primary
        and location_term
        and primary.casefold().endswith(location_suffix.casefold())
    ):
        primary = primary[: -len(location_suffix)].strip()
    if not primary:
        return []
    configured = APIFY_PRESET_SEARCH_VARIANTS.get(primary.casefold())
    return configured or [primary]


class ApifySearchItems(list):
    def __init__(
        self,
        items: Iterable[Dict[str, Any]],
        *,
        run_id: str,
        status: str,
        duration_seconds: float,
        search_variant_count: int,
        partial: bool,
    ):
        super().__init__(items)
        self.run_id = run_id
        self.status = status
        self.duration_seconds = duration_seconds
        self.search_variant_count = search_variant_count
        self.partial = partial


def run_apify_google_maps(query: str, limit: int, location: str = "South Africa") -> List[Dict[str, Any]]:
    provider_started = time.monotonic()
    token = require_env("APIFY_API_TOKEN")
    actor_id = os.getenv("APIFY_GOOGLE_MAPS_ACTOR_ID", "compass/crawler-google-places").replace("/", "~")
    max_items = max(limit, 5)
    country_code = infer_country_code(location)
    location_query = compact_text(location)
    search_terms = apify_google_maps_search_variants(query, location_query)
    per_search_limit = max(1, (max_items + len(search_terms) - 1) // len(search_terms))

    log_event(
        "info",
        "provider.apify.start",
        "Starting Apify Google Maps discovery.",
        query=query,
        location=location,
        limit=max_items,
        actorId=actor_id,
        countryCode=country_code,
        searchVariantCount=len(search_terms),
        perSearchLimit=per_search_limit,
    )

    total_budget = max(90, min(int(os.getenv("APIFY_DISCOVERY_BUDGET_SECONDS", "120")), 120))
    actor_timeout = max(
        75,
        min(int(os.getenv("APIFY_DISCOVERY_TIMEOUT_SECONDS", "105")), 105, total_budget - 15),
    )
    deadline = time.monotonic() + total_budget
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    payload = {
        "searchStringsArray": search_terms,
        "locationQuery": location_query,
        "language": "en",
        "maxCrawledPlacesPerSearch": per_search_limit,
        "includeWebResults": False,
        "skipClosedPlaces": True,
        "website": "withoutWebsite",
    }

    start_response = requests.post(
        f"https://api.apify.com/v2/actors/{actor_id}/runs?timeout={actor_timeout}",
        headers=headers,
        json=payload,
        timeout=20,
    )
    start_response.raise_for_status()
    start_payload = start_response.json()
    run = start_payload.get("data", start_payload) if isinstance(start_payload, dict) else {}
    run_id = compact_text(run.get("id"))
    dataset_id = compact_text(run.get("defaultDatasetId"))
    status = compact_text(run.get("status")).upper()
    status_message = compact_text(run.get("statusMessage"))
    if not run_id or not dataset_id:
        raise RuntimeError("Apify did not return a run ID and dataset ID.")

    terminal_statuses = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}
    while status not in terminal_statuses and deadline - time.monotonic() > 12:
        remaining = deadline - time.monotonic()
        wait_seconds = max(1, min(10, int(remaining - 10)))
        run_response = requests.get(
            f"https://api.apify.com/v2/actor-runs/{run_id}?waitForFinish={wait_seconds}",
            headers=headers,
            timeout=wait_seconds + 3,
        )
        run_response.raise_for_status()
        run_payload = run_response.json()
        run = run_payload.get("data", run_payload) if isinstance(run_payload, dict) else {}
        status = compact_text(run.get("status")).upper()
        status_message = compact_text(run.get("statusMessage"))

    remaining = deadline - time.monotonic()
    if remaining <= 2:
        raise TimeoutError("The two-minute Apify discovery budget was exhausted before its dataset could be read.")

    dataset_response = requests.get(
        f"https://api.apify.com/v2/datasets/{dataset_id}/items"
        f"?clean=true&format=json&limit={max_items}",
        headers=headers,
        timeout=max(2.0, min(10.0, remaining)),
    )
    dataset_response.raise_for_status()
    data = dataset_response.json()
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        data = data["data"]
    elif isinstance(data, dict) and isinstance(data.get("items"), list):
        data = data["items"]
    items = data if isinstance(data, list) else []

    if not items and status in {"FAILED", "ABORTED", "TIMED-OUT"}:
        detail = f": {status_message}" if status_message else ""
        raise RuntimeError(f"Apify actor ended with status {status}{detail}")

    log_event(
        "info",
        "provider.apify.finish",
        "Apify returned Google Maps items.",
        runId=run_id,
        runStatus=status or "UNKNOWN",
        itemCount=len(items),
        partial=status != "SUCCEEDED",
    )
    return ApifySearchItems(
        items,
        run_id=run_id,
        status=status or "UNKNOWN",
        duration_seconds=round(max(0.0, time.monotonic() - provider_started), 1),
        search_variant_count=len(search_terms),
        partial=status != "SUCCEEDED",
    )


def item_matches_requested_location(
    item: Dict[str, Any],
    requested_location: str,
    address: Optional[str],
    raw_location: Optional[str],
    website: Optional[str],
    domain: Optional[str],
    source_url: Optional[str],
) -> bool:
    requested = compact_text(requested_location).lower()
    if not requested:
        return True

    country_value = compact_text(
        first_present(
            item,
            [
                "country",
                "countryCode",
                "countryName",
                "locatedIn",
                "state",
                "city",
            ],
        )
    ).lower()

    combined_text = " ".join(
        [
            compact_text(address),
            compact_text(raw_location),
            compact_text(website),
            compact_text(domain),
            compact_text(source_url),
            compact_text(json.dumps(item, default=str)),
        ]
    ).lower()

    requested_tokens = set(re.findall(r"[a-z]+", requested))
    south_africa_requested = (
        "south africa" in requested
        or requested.strip() in {"za", "zaf"}
        or bool({"za", "zaf"}.intersection(requested_tokens))
        or any(term in requested for term in SOUTH_AFRICA_TERMS)
    )
    if south_africa_requested:
        if country_value in ["za", "zaf", "south africa"]:
            return True
        if domain and ".co.za" in domain.lower():
            return True
        if website and ".co.za" in website.lower():
            return True
        return any(term in combined_text for term in SOUTH_AFRICA_TERMS)

    requested_words = [
        word
        for word in re.split(r"[\s,]+", requested)
        if len(word) >= 4 and word not in ["near", "with", "from"]
    ]

    if requested in combined_text:
        return True

    return any(word in combined_text for word in requested_words)


def normalize_apify_items(
    items: List[Dict[str, Any]],
    fallback_category: str,
    location: str,
    limit: int,
    stats: Optional[Dict[str, int]] = None,
) -> List[DiscoveredLead]:
    leads: List[DiscoveredLead] = []
    seen = set()
    skipped_location = 0
    skipped_invalid = 0
    skipped_duplicate = 0

    for item in items:
        business_name = first_present(
            item,
            ["title", "name", "businessName", "placeName", "companyName"],
        )
        if not business_name:
            skipped_invalid += 1
            continue

        website = normalize_url(first_present(item, ["website", "site", "homepage"]))
        domain = normalize_domain(first_present(item, ["domain", "websiteDomain", "website_domain"]) or domain_from_url(website))
        address = first_present(item, ["address", "street", "fullAddress", "formattedAddress"])
        phone = first_present(item, ["phone", "phoneUnformatted", "contactPhone", "telephone"])
        email = extract_email_from_item(item)
        category = first_present(
            item,
            ["categoryName", "category", "primaryCategory", "type"],
            fallback_category,
        ) or fallback_category
        source_url = first_present(item, ["googleMapsUrl", "searchPageUrl", "placeUrl", "url"])

        # Do not default to the requested location before filtering.
        # Otherwise, UK/US results can be incorrectly marked as South Africa.
        raw_location = first_present(
            item,
            ["city", "neighborhood", "state", "country", "countryCode", "countryName"],
            None,
        )

        if not item_matches_requested_location(
            item=item,
            requested_location=location,
            address=address,
            raw_location=raw_location,
            website=website,
            domain=domain,
            source_url=source_url,
        ):
            skipped_location += 1
            continue

        display_location = raw_location or location
        notes = first_present(item, ["description", "about", "reviewsTags", "popularTimesLiveText"])

        lead_key = stable_lead_key(business_name, website, phone, address)
        canonical_key = canonical_lead_key_from_values(item, business_name, website, phone, address, source_url)
        if lead_key in seen:
            skipped_duplicate += 1
            continue
        seen.add(lead_key)

        rating = item.get("rating") or item.get("stars")
        reviews_count = item.get("reviewsCount") or item.get("numberOfReviews")

        try:
            rating = float(rating) if rating is not None else None
        except (TypeError, ValueError):
            rating = None

        try:
            reviews_count = int(reviews_count) if reviews_count is not None else None
        except (TypeError, ValueError):
            reviews_count = None

        leads.append(
            DiscoveredLead(
                leadKey=lead_key,
                canonicalLeadKey=canonical_key,
                businessName=business_name,
                email=email,
                phone=phone,
                website=website,
                domain=domain,
                category=category,
                address=address,
                location=display_location,
                rating=rating,
                reviewsCount=reviews_count,
                sourceUrl=source_url,
                notes=notes,
                raw=item,
            )
        )

        if len(leads) >= limit:
            break

    if stats is not None:
        stats.update(
            {
                "locationSkipped": skipped_location,
                "invalidRecordSkipped": skipped_invalid,
                "currentSearchDuplicatesSkipped": skipped_duplicate,
            }
        )

    log_event(
        "info",
        "leads.normalize.finish",
        "Lead normalization finished.",
        returned=len(leads),
        skippedLocation=skipped_location,
        skippedInvalid=skipped_invalid,
        skippedDuplicate=skipped_duplicate,
        requestedLocation=location,
    )

    return leads


def scrape_contact_details(lead: DiscoveredLead) -> Dict[str, Any]:
    website = normalize_url(lead.website)
    details = {
        "email": lead.email,
        "phone": lead.phone,
        "website": website,
        "notes": lead.notes,
    }

    if not website:
        return details

    pages = [website, website.rstrip("/") + "/contact", website.rstrip("/") + "/contact-us", website.rstrip("/") + "/about"]
    collected_text = []

    for page in pages:
        try:
            response = requests.get(
                page,
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
        except requests.RequestException:
            continue

        soup = BeautifulSoup(response.text, "html.parser")
        collected_text.append(soup.get_text(" ", strip=True))

    text = " ".join(collected_text)
    emails = extract_emails_from_text(text)
    phone = extract_phone_from_text(text)

    if emails and not details["email"]:
        details["email"] = emails[0]
    if phone and not details["phone"]:
        details["phone"] = phone
    if text and not details["notes"]:
        details["notes"] = compact_text(text[:500])

    return details


def parse_json_response(text: str) -> Dict[str, Any]:
    cleaned = compact_text(text)
    cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    return json.loads(cleaned)


def gemini_text_json(prompt: str, model: Optional[str] = None) -> Dict[str, Any]:
    api_key = require_env("GEMINI_API_KEY")
    model_name = model or os.getenv("GEMINI_TEXT_MODEL", "gemini-2.0-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
    max_attempts = max(1, min(int(os.getenv("GEMINI_MAX_ATTEMPTS", "2")), 5))

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.35,
        },
    }

    for attempt in range(max_attempts):
        try:
            response = requests.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key,
                },
                json=payload,
                timeout=90,
            )

            if response.status_code == 429:
                if attempt == max_attempts - 1:
                    raise GeminiRateLimitError(
                        "Gemini rate limit reached; using the local landing-page fallback."
                    )
                wait_time = 5 * (attempt + 1)
                log_event(
                    "warning",
                    "provider.gemini.rate_limited",
                    f"Gemini rate limited. Retrying in {wait_time}s.",
                    model=model_name,
                    attempt=attempt + 1,
                )
                time.sleep(wait_time)
                continue

            if response.status_code in {401, 403}:
                log_event(
                    "error",
                    "provider.gemini_text.auth_failed",
                    "Gemini rejected the configured API key.",
                    model=model_name,
                    statusCode=response.status_code,
                )
                raise RuntimeError(
                    "Gemini authentication failed. Replace GEMINI_API_KEY with a current "
                    "Gemini auth key from Google AI Studio."
                )

            response.raise_for_status()

            data = response.json()
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            text = "".join(part.get("text", "") for part in parts)

            if not text:
                raise RuntimeError("Gemini returned an empty response.")

            return parse_json_response(text)

        except requests.RequestException as error:
            if attempt == max_attempts - 1:
                log_event(
                    "error",
                    "provider.gemini_text.error",
                    "Gemini text request failed after retries.",
                    model=model_name,
                    reason=error.__class__.__name__,
                )
                raise GeminiTransientError(
                    "Gemini request failed after retries; using the local landing-page fallback."
                ) from error

            time.sleep(5 * (attempt + 1))

    raise GeminiTransientError(
        "Gemini request failed after retries; using the local landing-page fallback."
    )

def groq_chat_json(prompt: str, system_prompt: str) -> Dict[str, Any]:
    api_key = require_env("GROQ_API_KEY")
    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    log_event("info", "provider.groq.start", "Sending chunked chat prompt to GroqCloud.", model=model, promptChars=len(prompt))

    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.55,
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
        if response.status_code in {401, 403}:
            raise RuntimeError(
                "Groq authentication failed. Replace GROQ_API_KEY with an active key "
                "from the Groq console."
            )
        response.raise_for_status()
    except requests.RequestException as error:
        log_event("error", "provider.groq.error", "GroqCloud request failed.", model=model, reason=str(error))
        raise

    data = response.json()
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not text:
        raise RuntimeError("GroqCloud returned an empty response.")
    log_event("info", "provider.groq.finish", "GroqCloud returned JSON.", model=model, responseChars=len(text))
    return parse_json_response(text)


def enrich_lead_with_gemini(lead: DiscoveredLead, contact_details: Dict[str, Any]) -> Dict[str, Any]:
    lead_payload = lead.model_dump()
    lead_payload.update(contact_details)

    prompt = (
        "Clean and enrich this Google Maps lead for a web design outreach pipeline. "
        "Return strict JSON with keys: businessName, industry, location, email, phone, "
        "website, summary, targetCustomers, differentiators, serviceKeywords, imagePrompts, sourceNote. "
        "differentiators and serviceKeywords must be arrays. imagePrompts must contain exactly five "
        "business-appropriate text-to-image prompts for a landing page hero image and four service-card images. "
        "Do not invent private information; use public lead context only.\n\n"
        f"Lead JSON: {model_safe_json(lead_payload)}"
    )

    enriched = gemini_text_json(prompt)
    enriched.setdefault("businessName", lead.businessName)
    enriched.setdefault("industry", lead.category)
    enriched.setdefault("location", lead.location)
    enriched.setdefault("email", contact_details.get("email") or lead.email)
    enriched.setdefault("phone", contact_details.get("phone") or lead.phone)
    enriched.setdefault("website", contact_details.get("website") or lead.website)
    enriched.setdefault("summary", contact_details.get("notes") or lead.notes or f"{lead.businessName} is a local {lead.category} business.")
    enriched.setdefault("targetCustomers", "Local customers")
    enriched.setdefault("differentiators", [])
    enriched.setdefault("serviceKeywords", [lead.category])
    enriched.setdefault("sourceNote", "Public Google Maps and website context.")

    prompts = enriched.get("imagePrompts")
    if not isinstance(prompts, list) or len(prompts) < 5:
        industry = enriched.get("industry", lead.category)
        location = enriched.get("location", lead.location)
        enriched["imagePrompts"] = [
            f"Modern realistic landing page hero image for a {industry} business in {location}, no text",
            f"Professional service detail image for {industry}, clean composition, no text",
            f"Friendly customer experience image for {industry}, authentic local business setting, no text",
            f"Trust and quality image for {industry}, polished commercial photography, no text",
            f"Contact and booking themed image for {industry}, bright approachable scene, no text",
        ]

    return enriched


def generate_site_content_with_groq(context: Dict[str, Any], template: Dict[str, Any]) -> Dict[str, Any]:
    prompt = (
        "Create conversion-focused website copy for a static landing page. Return strict JSON with keys: "
        "headline, subheadline, about, services, ctaLabel, contactIntro, footerText. "
        "services must contain exactly four objects with title and description. "
        "Do not claim awards, guarantees, prices, or unavailable services unless present in the lead context.\n\n"
        f"Template: {model_safe_json(template)}\n"
        f"Lead context: {model_safe_json(context)}"
    )

    content = groq_chat_json(
        prompt,
        "You write concise, polished website copy for small business landing pages and return valid JSON only.",
    )

    services = content.get("services")
    if not isinstance(services, list):
        services = []
    keywords = context.get("serviceKeywords")
    if not isinstance(keywords, list) or not keywords:
        keywords = [context.get("industry", "service")]
    while len(services) < 4:
        keyword = keywords[len(services) % len(keywords)]
        services.append(
            {
                "title": f"{compact_text(keyword, 'Professional Service').title()} Support",
                "description": f"Reliable {compact_text(keyword, 'service').lower()} support tailored to local customer needs.",
            }
        )

    content["services"] = services[:4]
    content.setdefault("headline", f"{context.get('businessName')} - {context.get('industry')} in {context.get('location')}")
    content.setdefault("subheadline", context.get("summary", "A local business ready to serve customers."))
    content.setdefault("about", context.get("summary", "Built from public business context."))
    content.setdefault("ctaLabel", "Get in touch")
    content.setdefault("contactIntro", "Reach out to learn more or request a booking.")
    content.setdefault("footerText", f"{context.get('businessName')} | {context.get('location')}")

    return content


def build_public_lead_context(
    lead: DiscoveredLead,
    contact_details: Dict[str, Any],
    canonical_key: str,
) -> Dict[str, Any]:
    email = contact_details.get("email") or lead.email
    phone = contact_details.get("phone") or lead.phone
    website = contact_details.get("website") or lead.website
    raw_lead = lead.raw or {}
    main_image_url = uploaded_row_value(
        raw_lead,
        ["mainImageUrl", "main_image_url", "imageUrl", "image_url", "photoUrl", "photo_url"],
    )
    if not main_image_url:
        images = raw_lead.get("images") or raw_lead.get("imageUrls") or raw_lead.get("image_urls")
        if isinstance(images, list) and images:
            first_image = images[0]
            main_image_url = first_image.get("url") if isinstance(first_image, dict) else first_image
    main_image_url = normalize_url(main_image_url)
    context = {
        "canonicalLeadKey": canonical_key,
        "leadKey": lead.leadKey,
        "businessName": lead.businessName,
        "industry": lead.category,
        "category": lead.category,
        "location": lead.location,
        "province": lead.province,
        "address": lead.address,
        "email": email,
        "phone": phone,
        "website": website,
        "domain": lead.domain,
        "hasWebsite": bool(normalize_url(website) or normalize_domain(lead.domain)),
        "rating": lead.rating,
        "reviewsCount": lead.reviewsCount,
        "source": lead.source,
        "sourceUrl": lead.sourceUrl,
        "mainImageUrl": main_image_url,
        "notes": contact_details.get("notes") or lead.notes,
        "summary": contact_details.get("notes") or lead.notes or f"{lead.businessName} is a local {lead.category} business.",
        "targetCustomers": "Local customers",
        "differentiators": [],
        "serviceKeywords": [lead.category],
        "sourceNote": "Public Google Maps, business listing, and website context.",
        "rawLead": raw_lead,
        "noWebsiteLead": not bool(normalize_url(website) or normalize_domain(lead.domain)),
        "seoIndexingEnabled": False,
    }
    context["brandTheme"] = business_theme_for_context(context)
    context["businessProfile"] = personalized_business_profile(context)
    return context


def compact_lead_with_groq(context: Dict[str, Any]) -> Dict[str, Any]:
    business_profile = personalized_business_profile(context)
    prompt = (
        "Compact this public lead into a concise business brief for Gemini to build a landing page. "
        "Use as much of the lead as is useful, including raw listing fields, but remove repetition. "
        "Return strict JSON with keys: businessName, industry, location, address, email, phone, "
        "summary, serviceKeywords, differentiators, proofPoints, sourceLabel, sourceUrl, mainImageUrl, brandTheme, businessProfile, noWebsiteLead, "
        "contactType, designHints, complianceNotes. Arrays must be arrays. Preserve the supplied mainImageUrl and brandTheme exactly. "
        "Preserve the supplied businessProfile and use its distinct service titles and business-specific captions. "
        "Never replace a specific category with the generic label Local service. "
        "Do not invent private facts, services, guarantees, prices, awards, or consent.\n\n"
        f"Lead context: {model_safe_json(context)}"
    )

    brief = groq_chat_json(
        prompt,
        "You compact public business lead records into factual, source-grounded JSON briefs. Return valid JSON only.",
    )

    service_keywords = brief.get("serviceKeywords")
    if not isinstance(service_keywords, list) or not service_keywords:
        service_keywords = context.get("serviceKeywords") or [context.get("industry", "Local service")]

    differentiators = brief.get("differentiators")
    if not isinstance(differentiators, list):
        differentiators = []

    proof_points = brief.get("proofPoints")
    if not isinstance(proof_points, list):
        proof_points = []

    contact_type = "email" if context.get("email") else "phone" if context.get("phone") else "unknown"

    brief.setdefault("businessName", context.get("businessName"))
    if is_generic_industry_label(brief.get("industry")):
        brief["industry"] = business_profile["industry"]
    else:
        brief.setdefault("industry", business_profile["industry"])
    brief.setdefault("location", context.get("location"))
    brief.setdefault("address", context.get("address"))
    brief.setdefault("email", context.get("email"))
    brief.setdefault("phone", context.get("phone"))
    brief.setdefault("summary", context.get("summary"))
    brief.setdefault("sourceLabel", context.get("source") or "public business listing")
    brief.setdefault("sourceUrl", context.get("sourceUrl"))
    # Dataset/listing media and the deterministic business palette are authoritative.
    # A compaction model returning null or a generic palette must not discard them.
    brief["mainImageUrl"] = normalize_url(context.get("mainImageUrl")) or normalize_url(brief.get("mainImageUrl"))
    brief["brandTheme"] = business_theme_for_context(context)
    brief["businessProfile"] = business_profile
    brief["seoIndexingEnabled"] = context.get("seoIndexingEnabled") is True
    brief.setdefault("noWebsiteLead", True)
    brief.setdefault("designHints", [])
    brief.setdefault("complianceNotes", "Use public lead details only; outreach requires opt-in or agent consent handling.")
    brief["serviceKeywords"] = service_keywords[:12]
    brief["differentiators"] = differentiators[:8]
    brief["proofPoints"] = proof_points[:8]
    brief["contactType"] = contact_type
    brief["rawLeadSnapshot"] = model_safe_value(context.get("rawLead", {}), chunk_size=900, max_chunks=2)
    return brief


def contact_cta_for_context(context: Dict[str, Any]) -> Tuple[str, str]:
    email = normalize_email_identity(context.get("email"))
    phone = compact_text(context.get("phone"))
    website = normalize_url(context.get("website"))
    if phone:
        return f"tel:{phone}", "Call now"
    if email:
        return f"mailto:{email}", "Email us"
    if website:
        return website, "Visit website"
    return "#contact", "Get in touch"


def ensure_generated_hero_and_working_links(site_html: str, context: Dict[str, Any]) -> str:
    html_value = site_html
    contact_href, contact_label = contact_cta_for_context(context)
    main_image_url = normalize_url(context.get("mainImageUrl"))
    main_image_markup_url = html.escape(main_image_url, quote=True) if main_image_url else ""

    def has_visible_source_image(value: str) -> bool:
        if not main_image_url:
            return False
        for image_match in re.finditer(r"<img\b[^>]*>", value, flags=re.IGNORECASE | re.DOTALL):
            source_match = re.search(
                r'\bsrc=(["\'])(.*?)\1',
                image_match.group(0),
                flags=re.IGNORECASE | re.DOTALL,
            )
            if source_match and normalize_url(html.unescape(source_match.group(2))) == main_image_url:
                return True
        return False

    if main_image_url and not has_visible_source_image(html_value):
        image_tags = list(re.finditer(r"<img\b[^>]*>", html_value, flags=re.IGNORECASE | re.DOTALL))
        preferred_image = next(
            (
                match
                for match in image_tags
                if "logo" not in match.group(0).lower()
                and (
                    re.search(r"hero|banner|cover|main[\s_-]*image", match.group(0), flags=re.IGNORECASE)
                    or re.search(
                        r"(?:class|id)=[\"'][^\"']*(?:hero|banner)[^\"']*[\"']",
                        html_value[max(0, match.start() - 1400):match.start()],
                        flags=re.IGNORECASE,
                    )
                )
            ),
            None,
        )
        if preferred_image:
            image_tag = preferred_image.group(0)
            replacement = re.sub(
                r'(\bsrc=)(["\'])(.*?)(\2)',
                lambda match: f'{match.group(1)}{match.group(2)}{html.escape(main_image_url, quote=True)}{match.group(4)}',
                image_tag,
                count=1,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if "data-ai-business-main-image" not in replacement.lower():
                replacement = replacement[:-1] + ' data-ai-business-main-image fetchpriority="high" loading="eager">'
            html_value = html_value[:preferred_image.start()] + replacement + html_value[preferred_image.end():]

    if not main_image_url:
        # A missing source image is valid. Remove model-authored image tags so the
        # generated page cannot invent a logo, photo, or stock image.
        html_value = re.sub(r"\s*<img\b[^>]*>", "", html_value, flags=re.IGNORECASE | re.DOTALL)

    if main_image_url and not has_visible_source_image(html_value):
        business_name = compact_text(context.get("businessName"), "Local Business")
        industry = compact_text(context.get("industry"), "Local Service")
        location = compact_text(context.get("location"), "South Africa")
        hero_markup = f"""
<section class="ai-generated-hero-image" aria-label="Business banner" data-ai-business-main-image-container>
  <div>
    <span>{html.escape(industry)} in {html.escape(location)}</span>
    <h2>{html.escape(business_name)}</h2>
    <p>Image supplied by the public business listing.</p>
  </div>
  <img src="{main_image_markup_url}" alt="Main image for {html.escape(business_name)}" data-ai-business-main-image fetchpriority="high" loading="eager">
</section>
"""
        hero_css = """
<style id="ai-generated-hero-image-style">
  .ai-generated-hero-image {
    display: grid;
    grid-template-columns: minmax(0, 0.9fr) minmax(280px, 1.1fr);
    gap: 1.5rem;
    align-items: center;
    width: min(1120px, calc(100% - 32px));
    margin: 2rem auto;
    padding: 1.25rem;
    border-radius: 24px;
    background: linear-gradient(135deg, rgba(15,159,150,0.12), rgba(37,99,235,0.10));
    box-shadow: 0 20px 60px rgba(15, 23, 42, 0.12);
  }
  .ai-generated-hero-image span {
    color: var(--ai-primary, #0f9f96);
    font-weight: 900;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-size: 0.78rem;
  }
  .ai-generated-hero-image h2 {
    margin: 0.45rem 0;
    font-size: clamp(1.8rem, 4vw, 3.4rem);
    font-weight: 900;
  }
  .ai-generated-hero-image img {
    width: 100%;
    border-radius: 18px;
    display: block;
  }
  @media (max-width: 820px) {
    .ai-generated-hero-image { grid-template-columns: 1fr; }
  }
</style>
"""
        html_value = inject_before_closing_tag(html_value, "head", hero_css)
        html_value = re.sub(r"<body\b[^>]*>", lambda match: f"{match.group(0)}\n{hero_markup}", html_value, count=1, flags=re.IGNORECASE)
        if hero_markup not in html_value:
            html_value = inject_before_closing_tag(html_value, "body", hero_markup)

    html_value = re.sub(
        r'href=(["\'])(?:#|javascript:void\(0\)|javascript:;)?\1',
        f'href="{html.escape(contact_href)}"',
        html_value,
        flags=re.IGNORECASE,
    )
    html_value = re.sub(
        r"<button([^>]*)>\s*(get started|get in touch|contact|contact us|call now|email us)\s*</button>",
        lambda match: f'<a{match.group(1)} href="{html.escape(contact_href)}">{html.escape(contact_label)}</a>',
        html_value,
        flags=re.IGNORECASE,
    )
    return html_value


def generate_final_html_with_gemini(lead_brief: Dict[str, Any]) -> Dict[str, Any]:
    prompt = (
        f"{LANDING_PAGE_PROMPT_HEADER}\n\n"
        "Business information to append to the predefined prompt header:\n"
        f"{model_safe_json(lead_brief)}"
    )
    try:
        result = gemini_text_json(prompt)
    except (GeminiRateLimitError, GeminiTransientError) as error:
        log_event(
            "warning",
            "provider.gemini_final_html.fallback",
            "Gemini final HTML was unavailable. Using the local highly interactive renderer.",
            reason=str(error),
        )
        fallback_html = build_bootstrap_gsap_landing_html(
            lead_brief,
            dict(FREEFORM_SITE_SPEC),
        )
        return {
            "html": fallback_html,
            "qaNotes": "Gemini was temporarily unavailable; backend generated the validated local fallback.",
            "structureNotes": "Deterministic hero, services, about, contact, and footer layout.",
            "stylingLibraries": ["Bootstrap", "Alpine.js", "GSAP", "Motion Mini"],
            "promptHeader": LANDING_PAGE_PROMPT_HEADER,
            "fallbackReason": sanitize_message(error),
        }

    site_html = result.get("html") or result.get("siteHtml") or result.get("finalHtml")
    if not site_html:
        raise RuntimeError("Gemini did not return an html field.")
    business_profile = personalized_business_profile(lead_brief)
    rendered_text = normalize_identity_text(
        html.unescape(re.sub(r"<[^>]+>", " ", str(site_html), flags=re.DOTALL))
    )
    tagline_present = normalize_identity_text(business_profile["tagline"]) in rendered_text
    service_titles_present = sum(
        normalize_identity_text(service["title"]) in rendered_text
        for service in business_profile["services"]
    )
    if not tagline_present or service_titles_present < 4:
        log_event(
            "warning",
            "provider.gemini_final_html.personalization_fallback",
            "Gemini omitted the required business-specific copy; using the deterministic personalized renderer.",
            businessName=lead_brief.get("businessName"),
            taglinePresent=tagline_present,
            serviceTitlesPresent=service_titles_present,
        )
        site_html = build_bootstrap_gsap_landing_html(lead_brief, dict(FREEFORM_SITE_SPEC))
        result["qaNotes"] = (
            "Gemini output omitted required personalized services or captions; "
            "backend generated the validated business-specific fallback."
        )
    final_html = ensure_required_site_features(
        ensure_generated_hero_and_working_links(str(site_html), lead_brief),
        lead_brief,
    )
    return {
        "html": final_html,
        "qaNotes": result.get("qaNotes") or "Gemini generated final HTML; backend enforced the interactive profile, business facts, SEO gate, and color widget.",
        "structureNotes": result.get("structureNotes"),
        "stylingLibraries": ["Bootstrap", "Alpine.js", "GSAP", "Motion Mini"],
        "promptHeader": LANDING_PAGE_PROMPT_HEADER,
    }


def generate_page_prompt_with_gemini(context: Dict[str, Any], template: Dict[str, Any]) -> Dict[str, Any]:
    prompt = (
        "Create a production-ready prompt for a single-file HTML landing page. "
        "Return strict JSON with keys: pagePrompt, designNotes, contentGuardrails, imageDirection. "
        "The pagePrompt must preserve: hero, four service cards, about section, contact section, footer. "
        "The page must use Bootstrap 5.3.8 as its only styling framework, Alpine.js 3.15.12 for stateful interactions, GSAP 3.15 for hero choreography, and Motion Mini 12.42.2 for viewport reveals. "
        "Require an animated hero, business-detail tabs, fact-based FAQs, strong CTA buttons, modern service cards, polished gradients, spacing, shadows, hover effects, reduced-motion support, SEO metadata, and responsive layout. "
        "Use only public lead context. Do not invent awards, prices, guarantees, or unavailable services.\n\n"
        f"Template: {model_safe_json(template)}\n"
        f"Lead context: {model_safe_json(context)}"
    )

    try:
        result = gemini_text_json(prompt)
    except Exception as error:
        log_event(
            "warning",
            "provider.gemini_page_prompt.fallback",
            "Gemini page prompt failed. Using local fallback.",
            reason=str(error),
        )
        result = {}

    result.setdefault(
        "pagePrompt",
        (
            f"Build a standalone responsive HTML landing page for {context.get('businessName')} "
            f"in {context.get('location')}. Include a hero, exactly four service cards, about, "
            "contact, explicit business details, fact-based FAQs, and footer. Use Bootstrap 5.3.8, Alpine.js 3.15.12, GSAP 3.15, Motion Mini 12.42.2, accessible semantic HTML, reduced-motion support, SEO metadata, strong CTA buttons, modern cards, polished gradients, spacing, shadows, hover effects, and grounded claims only."
        ),
    )
    result.setdefault("designNotes", f"Use accent {template.get('accent')} and background {template.get('background')}.")
    result.setdefault("contentGuardrails", "Use only the provided public lead context.")
    result.setdefault(
        "imageDirection",
        (
            "Use the supplied mainImageUrl exactly and do not add other images."
            if normalize_url(context.get("mainImageUrl"))
            else "Do not add or generate images because the source lead has no image."
        ),
    )
    return result

def build_bootstrap_gsap_landing_html(context: Dict[str, Any], template: Dict[str, Any]) -> str:
    business_profile = personalized_business_profile(context)
    business_name_raw = compact_text(context.get("businessName"), "Local Business")
    industry_raw = compact_text(business_profile.get("industry"), context.get("industry") or "Local Business")
    location_raw = compact_text(context.get("location"), "South Africa")
    address_raw = compact_text(context.get("address"))
    rating_raw = compact_text(context.get("rating"))
    reviews_raw = compact_text(context.get("reviewsCount"))
    source_raw = compact_text(context.get("source") or context.get("sourceLabel"), "public business listing")
    business_name = html.escape(business_name_raw)
    industry = html.escape(industry_raw)
    location = html.escape(location_raw)
    address = html.escape(address_raw)
    source = html.escape(source_raw)
    supplied_summary = compact_text(context.get("summary"))
    normalized_summary = normalize_identity_text(supplied_summary)
    if not supplied_summary or "local local service" in normalized_summary or normalized_summary in GENERIC_INDUSTRY_LABELS:
        summary_raw = compact_text(business_profile.get("heroCaption"))
    else:
        summary_raw = supplied_summary
    summary = html.escape(
        compact_text(
            summary_raw,
            business_profile.get("heroCaption") or f"Connect with {business_name_raw} in {location_raw}."
        )
    )
    tagline = html.escape(compact_text(business_profile.get("tagline"), f"Discover {business_name_raw}."))
    hero_caption = html.escape(compact_text(business_profile.get("heroCaption"), summary_raw))
    services_heading = html.escape(compact_text(business_profile.get("servicesHeading"), f"How {business_name_raw} can help"))
    services_intro = html.escape(compact_text(business_profile.get("servicesIntro"), summary_raw))
    about_heading = html.escape(compact_text(business_profile.get("aboutHeading"), f"About {business_name_raw}"))

    email = compact_text(context.get("email"))
    phone = compact_text(context.get("phone"))
    website = normalize_url(context.get("website"))
    source_url = normalize_url(context.get("sourceUrl"))

    accent = compact_text(template.get("accent"), "#00AEEF")
    background = compact_text(template.get("background"), "#F7FAFC")

    hero_image = normalize_url(context.get("mainImageUrl"))
    hero_image_markup = (
        f'<img src="{html.escape(hero_image, quote=True)}" alt="Main image for {business_name}" '
        'data-ai-business-main-image fetchpriority="high" loading="eager">'
        if hero_image
        else ""
    )

    contact_target = "#contact"
    contact_label = "Get in touch"
    if phone:
        contact_target = f"tel:{phone}"
        contact_label = "Call now"
    elif email:
        contact_target = f"mailto:{email}"
        contact_label = "Email us"
    elif website:
        contact_target = website
        contact_label = "Visit website"

    default_services = business_profile["services"]

    services_html = ""
    for index, service in enumerate(default_services):
        services_html += f"""
        <div class="col-md-6 col-lg-3">
          <article class="service-card h-100">
            <div class="service-icon">{index + 1}</div>
            <h3>{html.escape(service["title"])}</h3>
            <p>{html.escape(service["description"])}</p>
          </article>
        </div>
        """

    contact_buttons = ""
    if phone:
        contact_buttons += f'<a class="btn btn-light btn-lg rounded-pill px-4" href="tel:{html.escape(phone)}">Call now</a>'
    if email:
        contact_buttons += f'<a class="btn btn-outline-light btn-lg rounded-pill px-4" href="mailto:{html.escape(email)}">Email us</a>'
    if website:
        contact_buttons += f'<a class="btn btn-outline-dark btn-lg rounded-pill px-4" href="{html.escape(website)}" target="_blank" rel="noreferrer">Visit website</a>'
    if source_url:
        contact_buttons += f'<a class="btn btn-outline-light btn-lg rounded-pill px-4" href="{html.escape(source_url)}" target="_blank" rel="noreferrer">View listing</a>'

    if not contact_buttons:
        contact_buttons = '<a class="btn btn-light btn-lg rounded-pill px-4" href="#contact">Get in touch</a>'

    hero_cta_attrs = ' target="_blank" rel="noreferrer"' if contact_target.startswith("http") else ""
    proof_chips = [
        f"{industry_raw}",
        address_raw or location_raw,
        f"{rating_raw} rating" if rating_raw else source_raw,
        f"{reviews_raw} reviews" if reviews_raw else "Direct contact details",
    ]
    proof_chips_html = "".join(
        f'<div class="floating-chip">{html.escape(compact_text(chip, "Business detail"))}</div>'
        for chip in proof_chips
        if compact_text(chip)
    )

    site_html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{business_name} | {industry} in {location}</title>

  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet" integrity="sha384-sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB" crossorigin="anonymous">

  <style>
    :root {{
      --primary: #00c2a8;
      --secondary: #00aeef;
      --accent: #7b61ff;
      --warning: #ffb800;
      --dark: #102033;
      --muted: #5d6b82;
      --surface: #ffffff;
      --soft: #f6f9fc;
      --template-accent: {accent};
      --template-bg: {background};
    }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      margin: 0;
      font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--soft);
      color: var(--dark);
      overflow-x: hidden;
    }}

    .hero {{
      min-height: 92vh;
      position: relative;
      display: flex;
      align-items: center;
      overflow: hidden;
      background:
        radial-gradient(circle at 10% 10%, rgba(123, 97, 255, 0.35), transparent 32%),
        radial-gradient(circle at 90% 20%, rgba(0, 174, 239, 0.35), transparent 30%),
        linear-gradient(135deg, #00c2a8 0%, #00aeef 48%, #7b61ff 100%);
      color: white;
    }}

    .hero::before {{
      content: "";
      position: absolute;
      inset: 0;
      background-image:
        linear-gradient(rgba(255,255,255,0.14) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.14) 1px, transparent 1px);
      background-size: 42px 42px;
      opacity: 0.22;
    }}

    .hero .container {{
      position: relative;
      z-index: 2;
    }}

    .hero-badge {{
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.65rem 1rem;
      border-radius: 999px;
      background: rgba(255,255,255,0.18);
      border: 1px solid rgba(255,255,255,0.32);
      backdrop-filter: blur(12px);
      font-weight: 800;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      font-size: 0.78rem;
    }}

    .hero-title {{
      font-size: clamp(2.7rem, 7vw, 6.5rem);
      line-height: 0.95;
      font-weight: 900;
      letter-spacing: -0.08em;
      margin: 1.4rem 0;
      max-width: 950px;
    }}

    .hero-text {{
      font-size: clamp(1.05rem, 2vw, 1.35rem);
      max-width: 760px;
      color: rgba(255,255,255,0.92);
    }}

    .hero-tagline {{
      margin: -0.45rem 0 0.85rem;
      max-width: 760px;
      font-size: clamp(1.35rem, 2.4vw, 2rem);
      font-weight: 850;
      line-height: 1.2;
      color: white;
    }}

    .hero-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 1rem;
      margin-top: 2rem;
    }}

    .hero-card {{
      border-radius: 2rem;
      padding: 1rem;
      background: rgba(255,255,255,0.18);
      border: 1px solid rgba(255,255,255,0.35);
      box-shadow: 0 24px 70px rgba(16, 32, 51, 0.26);
      backdrop-filter: blur(16px);
    }}

    .hero-card img {{
      width: 100%;
      aspect-ratio: 4 / 3;
      object-fit: cover;
      display: block;
      border-radius: 1.35rem;
      background: white;
      box-shadow: 0 18px 48px rgba(16, 32, 51, 0.18);
    }}

    .hero-card-body {{
      padding: 1.25rem;
    }}

    .floating-chip {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 999px;
      padding: 0.7rem 1rem;
      background: white;
      color: var(--dark);
      font-weight: 800;
      box-shadow: 0 18px 40px rgba(16,32,51,0.18);
      margin: 0.35rem;
    }}

    .section-padding {{
      padding: 6rem 0;
    }}

    .section-kicker {{
      color: var(--primary);
      font-weight: 900;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      font-size: 0.78rem;
    }}

    .section-title {{
      font-size: clamp(2rem, 4vw, 3.5rem);
      font-weight: 900;
      letter-spacing: -0.05em;
      margin: 0.5rem 0 1rem;
    }}

    .service-card {{
      border: 0;
      border-radius: 1.5rem;
      padding: 1.7rem;
      background: white;
      box-shadow: 0 20px 60px rgba(16,32,51,0.09);
      transition: transform 0.25s ease, box-shadow 0.25s ease;
      border-top: 5px solid var(--primary);
    }}

    .service-card:hover {{
      transform: translateY(-8px);
      box-shadow: 0 28px 80px rgba(16,32,51,0.16);
    }}

    .service-icon {{
      width: 54px;
      height: 54px;
      border-radius: 1rem;
      display: grid;
      place-items: center;
      color: white;
      font-weight: 900;
      margin-bottom: 1.1rem;
      background: linear-gradient(135deg, var(--primary), var(--secondary), var(--accent));
      box-shadow: 0 14px 35px rgba(0,174,239,0.28);
    }}

    .service-card h3 {{
      font-size: 1.2rem;
      font-weight: 900;
      margin-bottom: 0.75rem;
    }}

    .service-card p,
    .about-text,
    .contact-text {{
      color: var(--muted);
      line-height: 1.75;
    }}

    .about-panel {{
      border-radius: 2rem;
      overflow: hidden;
      background: white;
      box-shadow: 0 24px 80px rgba(16,32,51,0.10);
    }}

    .about-gradient {{
      min-height: 100%;
      background:
        radial-gradient(circle at 20% 20%, rgba(255,255,255,0.45), transparent 34%),
        linear-gradient(135deg, var(--accent), var(--secondary));
      color: white;
      padding: 3rem;
    }}

    .stat-card {{
      border-radius: 1.2rem;
      padding: 1.2rem;
      background: rgba(255,255,255,0.16);
      border: 1px solid rgba(255,255,255,0.25);
    }}

    .cta-band {{
      border-radius: 2rem;
      padding: 4rem 2rem;
      color: white;
      background:
        radial-gradient(circle at top right, rgba(255,184,0,0.35), transparent 30%),
        linear-gradient(135deg, var(--dark), #173b69 45%, var(--primary));
      box-shadow: 0 24px 80px rgba(16,32,51,0.18);
    }}

    .contact-pill {{
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.9rem 1.2rem;
      border-radius: 999px;
      background: rgba(255,255,255,0.15);
      border: 1px solid rgba(255,255,255,0.25);
      color: white;
      margin: 0.25rem;
      text-decoration: none;
    }}

    footer {{
      padding: 2rem 0;
      color: var(--muted);
    }}

    @media (max-width: 768px) {{
      .hero {{
        min-height: auto;
        padding: 5rem 0;
      }}

      .hero-card {{
        margin-top: 2rem;
      }}

      .section-padding {{
        padding: 4rem 0;
      }}
    }}
  </style>
</head>

<body>
  <header class="hero">
    <div class="container py-5">
      <div class="row align-items-center g-5">
        <div class="col-lg-7">
          <span class="hero-badge hero-animate">{industry} in {location}</span>
          <h1 class="hero-title hero-animate">{business_name}</h1>
          <p class="hero-tagline hero-animate">{tagline}</p>
          <p class="hero-text hero-animate">{hero_caption}</p>
          <div class="hero-actions hero-animate">
            <a href="{html.escape(contact_target)}" class="btn btn-light btn-lg rounded-pill px-4 shadow"{hero_cta_attrs}>{html.escape(contact_label)}</a>
            <a href="#services" class="btn btn-outline-light btn-lg rounded-pill px-4">View services</a>
          </div>
        </div>

        <div class="col-lg-5">
          <div class="hero-card hero-visual">
            {hero_image_markup}
            <div class="hero-card-body">
              <p class="fw-bold mb-3">Serving {location}</p>
              {proof_chips_html}
              <hr class="border-light opacity-25 my-4">
              <p class="mb-0">{hero_caption}</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  </header>

  <main>
    <section id="services" class="section-padding">
      <div class="container">
        <div class="text-center mb-5">
          <span class="section-kicker">What we offer</span>
          <h2 class="section-title">{services_heading}</h2>
          <p class="text-secondary mx-auto" style="max-width: 720px;">{services_intro}</p>
        </div>

        <div class="row g-4">
          {services_html}
        </div>
      </div>
    </section>

    <section class="section-padding pt-0">
      <div class="container">
        <div class="about-panel">
          <div class="row g-0">
            <div class="col-lg-5">
              <div class="about-gradient h-100">
                <span class="section-kicker text-white">Why choose us</span>
                <h2 class="section-title">{about_heading}</h2>
                <div class="row g-3 mt-3">
                  <div class="col-6">
                    <div class="stat-card">
                      <strong>{industry}</strong>
                      <small class="d-block opacity-75">Industry</small>
                    </div>
                  </div>
                  <div class="col-6">
                    <div class="stat-card">
                      <strong>{location}</strong>
                      <small class="d-block opacity-75">Location</small>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <div class="col-lg-7">
              <div class="p-4 p-lg-5">
                <span class="section-kicker">About</span>
                <h2 class="section-title">About {business_name}</h2>
                <p class="about-text">{summary}</p>
                <p class="about-text">This page uses the available public details{f' including {address}' if address_raw else ''} to give customers a fast, mobile-friendly way to understand and contact {business_name}.</p>
                <a href="{html.escape(contact_target)}" class="btn btn-primary btn-lg rounded-pill px-4"{hero_cta_attrs}>{html.escape(contact_label)}</a>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section id="contact" class="section-padding">
      <div class="container">
        <div class="cta-band text-center">
          <span class="section-kicker text-white">Ready to connect?</span>
          <h2 class="section-title">Contact {business_name}</h2>
          <p class="contact-text text-white-50 mx-auto" style="max-width: 680px;">{tagline} Use the options below to contact {business_name} directly.</p>
          <div class="hero-actions justify-content-center">
            {contact_buttons}
          </div>
          <div class="mt-4">
            <span class="contact-pill">{industry}</span>
            <span class="contact-pill">{location}</span>
          </div>
        </div>
      </div>
    </section>
  </main>

  <footer>
    <div class="container d-flex flex-wrap justify-content-between gap-2">
      <span>&copy; {datetime.now().year} {business_name}</span>
      <span>Generated by AI Site Factory</span>
    </div>
  </footer>

  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/js/bootstrap.bundle.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/gsap@3.15/dist/gsap.min.js"></script>

  <script>
    window.addEventListener("DOMContentLoaded", function () {{
      if (!window.gsap || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

      gsap.from(".hero-animate", {{
        y: 36,
        opacity: 0,
        duration: 0.9,
        ease: "power3.out",
        stagger: 0.12
      }});

      gsap.from(".hero-visual", {{
        scale: 0.94,
        y: 28,
        opacity: 0,
        duration: 1,
        ease: "power3.out",
        delay: 0.25
      }});

      gsap.from(".service-card", {{
        y: 42,
        opacity: 0,
        duration: 0.8,
        ease: "power3.out",
        stagger: 0.12,
        delay: 0.45
      }});

      gsap.from(".about-panel, .cta-band", {{
        y: 38,
        opacity: 0,
        duration: 0.9,
        ease: "power3.out",
        stagger: 0.15,
        delay: 0.65
      }});
    }});
  </script>
</body>
</html>"""
    return ensure_required_site_features(site_html, context)

def generate_draft_html_with_groq(
    context: Dict[str, Any],
    template: Dict[str, Any],
    page_prompt: Dict[str, Any],
) -> Dict[str, Any]:
    site_html = build_bootstrap_gsap_landing_html(context, template)
    return {
        "html": site_html,
        "notes": "Generated deterministic Bootstrap + GSAP landing page."
    }

def finalize_html_with_gemini(
    context: Dict[str, Any],
    template: Dict[str, Any],
    page_prompt: Dict[str, Any],
    draft_html: str,
) -> Dict[str, Any]:
    return {
        "html": ensure_bootstrap_gsap_assets(draft_html),
        "qaNotes": "Final HTML uses Bootstrap 5.3.8, GSAP 3.15, custom styling, and animations."
    }


def inject_before_closing_tag(site_html: str, tag: str, content: str) -> str:
    html_value = site_html
    lower_html = html_value.lower()
    closing_tag = f"</{tag}>"
    if closing_tag in lower_html:
        return re.sub(closing_tag, f"{content}\n{closing_tag}", html_value, count=1, flags=re.IGNORECASE)
    return f"{html_value}\n{content}"


def replace_element_by_id(site_html: str, tag: str, element_id: str, replacement: str) -> str:
    pattern = (
        rf"<{tag}\b[^>]*\bid=[\"']{re.escape(element_id)}[\"'][^>]*>"
        rf".*?</{tag}>"
    )
    replaced = False

    def replace_once(match: re.Match) -> str:
        nonlocal replaced
        if replaced:
            return ""
        replaced = True
        return replacement

    return re.sub(pattern, replace_once, site_html, flags=re.IGNORECASE | re.DOTALL)


def upgrade_legacy_fallback_image_data_uris(site_html: str) -> str:
    """Upgrade only the deterministic fallback SVGs generated by this service."""

    data_uri_pattern = re.compile(
        r"data:image/svg\+xml;base64,(?P<payload>[A-Za-z0-9+/]+={0,2})",
        flags=re.IGNORECASE,
    )

    def upgrade(match: re.Match) -> str:
        try:
            svg = base64.b64decode(match.group("payload"), validate=True).decode("utf-8")
        except (ValueError, binascii.Error, UnicodeDecodeError):
            return match.group(0)

        if "ai-site-fallback-image-v2" in svg:
            return match.group(0)

        legacy_markers = (
            "width='1200' height='800' viewBox='0 0 1200 800'",
            "<linearGradient id='accent'",
            "<rect x='138' y='158' width='426' height='300'",
            "<text x='626' y='197'",
            "<text x='626' y='408'",
            "<text x='626' y='472'",
        )
        if not all(marker in svg for marker in legacy_markers):
            return match.group(0)

        accent_match = re.search(
            r"<linearGradient id='accent'.*?<stop offset='0' stop-color='(#[0-9a-fA-F]{6})'",
            svg,
            flags=re.DOTALL,
        )
        subtitle_match = re.search(r"<text x='626' y='197'[^>]*>(.*?)</text>", svg, flags=re.DOTALL)
        label_match = re.search(r"<text x='626' y='408'[^>]*>(.*?)</text>", svg, flags=re.DOTALL)
        detail_match = re.search(r"<text x='626' y='472'[^>]*>(.*?)</text>", svg, flags=re.DOTALL)
        if not all((accent_match, subtitle_match, label_match, detail_match)):
            return match.group(0)

        def legacy_text(value: str) -> str:
            return compact_text(html.unescape(re.sub(r"<[^>]+>", "", value)))

        return fallback_image_data_uri(
            legacy_text(label_match.group(1)),
            accent_match.group(1),
            legacy_text(subtitle_match.group(1)),
            legacy_text(detail_match.group(1)),
        )

    return data_uri_pattern.sub(upgrade, site_html)


def existing_site_business_theme(site_html: str) -> Optional[Dict[str, str]]:
    widget_match = re.search(
        r"<aside\b[^>]*data-ai-site-theme-widget[^>]*>",
        site_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not widget_match:
        return None
    widget = widget_match.group(0)

    def attribute(name: str) -> Optional[str]:
        match = re.search(rf'\b{name}=["\']([^"\']+)["\']', widget, flags=re.IGNORECASE)
        return html.unescape(match.group(1)) if match else None

    text = normalized_hex_color(attribute("data-ai-default-text"))
    background = normalized_hex_color(attribute("data-ai-default-background"))
    highlight = normalized_hex_color(attribute("data-ai-default-highlight"))
    if not all((text, background, highlight)):
        return None
    return {
        "text": text,
        "background": background,
        "highlight": highlight,
        "name": compact_text(attribute("data-ai-theme-name"), DEFAULT_BUSINESS_THEME["name"]),
    }


class SiteSeoValidationError(RuntimeError):
    def __init__(self, report: Dict[str, Any]):
        self.report = report
        failures = ", ".join(report.get("errors") or ["unknown SEO validation error"])
        super().__init__(f"Generated site failed the SEO validation gate: {failures}")


def ensure_html_document_shell(site_html: str) -> str:
    html_value = str(site_html or "").strip()
    if not html_value:
        html_value = "<html><head></head><body></body></html>"
    if not re.search(r"<!doctype\s+html", html_value, flags=re.IGNORECASE):
        html_value = f"<!doctype html>\n{html_value}"
    if not re.search(r"<html\b", html_value, flags=re.IGNORECASE):
        body_value = re.sub(r"<!doctype\s+html>", "", html_value, flags=re.IGNORECASE)
        html_value = f'<!doctype html>\n<html lang="en"><head></head><body>{body_value}</body></html>'
    if re.search(r"<html\b[^>]*\blang=", html_value, flags=re.IGNORECASE):
        html_value = re.sub(
            r"(<html\b[^>]*\blang=)([\"'])[^\"']*\2",
            r'\1"en"',
            html_value,
            count=1,
            flags=re.IGNORECASE,
        )
    else:
        html_value = re.sub(r"<html\b([^>]*)>", r'<html\1 lang="en">', html_value, count=1, flags=re.IGNORECASE)
    if not re.search(r"<head\b", html_value, flags=re.IGNORECASE):
        html_value = re.sub(r"(<html\b[^>]*>)", r"\1\n<head></head>", html_value, count=1, flags=re.IGNORECASE)
    if not re.search(r"<body\b", html_value, flags=re.IGNORECASE):
        html_value = inject_before_closing_tag(html_value, "html", "<body></body>")
    return html_value


def replace_head_element(site_html: str, pattern: str, replacement: str) -> str:
    cleaned = re.sub(
        rf"[ \t]*{pattern}[ \t]*(?:\r?\n)?",
        "",
        site_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return inject_before_closing_tag(cleaned, "head", f"  {replacement}")


def concise_meta_text(value: Any, fallback: str, limit: int = 160) -> str:
    plain = compact_text(html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))), fallback)
    if len(plain) <= limit:
        return plain
    shortened = plain[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-.")
    return f"{shortened[: limit - 1].rstrip()}." if shortened else plain[:limit]


def ensure_primary_heading(site_html: str, context: Dict[str, Any]) -> str:
    business_name = html.escape(compact_text(context.get("businessName"), "Local Business"))
    h1_pattern = re.compile(r"<h1\b(?P<attrs>[^>]*)>(?P<body>.*?)</h1>", flags=re.IGNORECASE | re.DOTALL)
    matches = list(h1_pattern.finditer(site_html))
    if not matches:
        heading = f'<header class="container py-4" data-ai-primary-heading><h1>{business_name}</h1></header>'
        if re.search(r"<main\b[^>]*>", site_html, flags=re.IGNORECASE):
            return re.sub(
                r"<main\b[^>]*>",
                lambda match: f"{match.group(0)}\n{heading}",
                site_html,
                count=1,
                flags=re.IGNORECASE,
            )
        return re.sub(
            r"<body\b[^>]*>",
            lambda match: f"{match.group(0)}\n{heading}",
            site_html,
            count=1,
            flags=re.IGNORECASE,
        )

    seen = 0

    def keep_one_h1(match: re.Match) -> str:
        nonlocal seen
        seen += 1
        if seen == 1:
            return match.group(0)
        return f"<h2{match.group('attrs')}>{match.group('body')}</h2>"

    return h1_pattern.sub(keep_one_h1, site_html)


def ensure_image_alt_text(site_html: str, context: Dict[str, Any]) -> str:
    business_name = compact_text(context.get("businessName"), "Local Business")
    industry = compact_text(context.get("industry") or context.get("category"), "business")
    fallback_alt = html.escape(f"{business_name} {industry} visual", quote=True)

    def update_image(match: re.Match) -> str:
        tag = match.group(0)
        additions = []
        if not re.search(r"\balt\s*=", tag, flags=re.IGNORECASE):
            additions.append(f'alt="{fallback_alt}"')
        if not re.search(r"\bdecoding\s*=", tag, flags=re.IGNORECASE):
            additions.append('decoding="async"')
        if not additions:
            return tag
        closing = " />" if re.search(r"/\s*>$", tag) else ">"
        tag_without_closing = re.sub(r"\s*/?>$", "", tag)
        return f"{tag_without_closing} {' '.join(additions)}{closing}"

    return re.sub(r"<img\b[^>]*>", update_image, site_html, flags=re.IGNORECASE | re.DOTALL)


def business_details_section(context: Dict[str, Any]) -> str:
    profile = personalized_business_profile(context)
    business_name_raw = compact_text(context.get("businessName"), "Local Business")
    industry_raw = compact_text(profile.get("industry"), context.get("industry") or "Local Business")
    location_raw = compact_text(context.get("location"), "South Africa")
    address_raw = compact_text(context.get("address"))
    phone_raw = compact_text(context.get("phone"))
    email_raw = normalize_email_identity(context.get("email"))
    rating_raw = compact_text(context.get("rating"))
    reviews_raw = compact_text(context.get("reviewsCount"))
    source_raw = compact_text(context.get("source") or context.get("sourceLabel"), "Public business listing")
    source_url = normalize_url(context.get("sourceUrl"))
    summary_raw = concise_meta_text(
        context.get("summary") or profile.get("heroCaption"),
        f"Learn about {business_name_raw}, a {industry_raw} business serving {location_raw}.",
        240,
    )

    def escaped(value: Any) -> str:
        return html.escape(compact_text(value))

    facts: List[Tuple[str, str]] = [
        ("Business", business_name_raw),
        ("Industry", industry_raw),
        ("Location", location_raw),
    ]
    if address_raw:
        facts.append(("Address", address_raw))
    if phone_raw:
        facts.append(("Phone", phone_raw))
    if email_raw:
        facts.append(("Email", email_raw))
    if rating_raw:
        rating_detail = f"{rating_raw} out of 5"
        if reviews_raw:
            rating_detail += f" from {reviews_raw} public reviews"
        facts.append(("Public rating", rating_detail))
    elif reviews_raw:
        facts.append(("Public reviews", reviews_raw))
    facts.append(("Information source", source_raw))

    fact_markup = "".join(
        f'<div class="ai-business-fact"><dt>{escaped(label)}</dt><dd>{escaped(value)}</dd></div>'
        for label, value in facts
    )
    contact_links = []
    if phone_raw:
        contact_links.append(
            f'<a class="btn btn-primary" href="tel:{html.escape(phone_raw, quote=True)}">Call {escaped(phone_raw)}</a>'
        )
    if email_raw:
        contact_links.append(
            f'<a class="btn btn-outline-primary" href="mailto:{html.escape(email_raw, quote=True)}">Email {escaped(email_raw)}</a>'
        )
    if source_url:
        contact_links.append(
            f'<a class="btn btn-outline-secondary" href="{html.escape(source_url, quote=True)}" target="_blank" rel="noreferrer">View {escaped(source_raw)}</a>'
        )
    if not contact_links:
        contact_links.append('<a class="btn btn-primary" href="#contact">View contact options</a>')

    service_titles = [compact_text(service.get("title")) for service in profile.get("services", []) if compact_text(service.get("title"))]
    services_text = ", ".join(service_titles) or industry_raw
    location_text = address_raw or location_raw
    contact_text = " or ".join(value for value in (phone_raw, email_raw) if value) or "the contact options shown on this page"
    faq_items = [
        (f"What does {business_name_raw} offer?", f"The supplied business profile lists {services_text}."),
        (f"Where is {business_name_raw} located?", f"The public business details identify {location_text}."),
        (f"How can I contact {business_name_raw}?", f"Use {contact_text} to contact the business directly."),
    ]
    faq_markup = "".join(
        f'''<article class="ai-business-faq" x-data="{{ open: false }}">
          <h3><button type="button" @click="open = !open" :aria-expanded="open.toString()" data-ai-interaction>{escaped(question)}<span aria-hidden="true">+</span></button></h3>
          <div x-show="open" x-cloak><p>{escaped(answer)}</p></div>
        </article>'''
        for question, answer in faq_items
    )

    return f'''<section id="business-details" class="ai-business-details section-padding" data-ai-business-details data-ai-reveal x-data="{{ activeTab: 'overview' }}">
  <div class="container">
    <span class="section-kicker">Verified public profile</span>
    <h2>Business details for {escaped(business_name_raw)}</h2>
    <p class="ai-business-summary">{escaped(summary_raw)}</p>
    <div class="ai-business-tabs" role="tablist" aria-label="Business information">
      <button type="button" role="tab" @click="activeTab = 'overview'" :aria-selected="(activeTab === 'overview').toString()" :class="{{ active: activeTab === 'overview' }}" data-ai-interaction>Overview</button>
      <button type="button" role="tab" @click="activeTab = 'contact'" :aria-selected="(activeTab === 'contact').toString()" :class="{{ active: activeTab === 'contact' }}" data-ai-interaction>Contact</button>
      <button type="button" role="tab" @click="activeTab = 'questions'" :aria-selected="(activeTab === 'questions').toString()" :class="{{ active: activeTab === 'questions' }}" data-ai-interaction>Common questions</button>
    </div>
    <div class="ai-business-panel" x-show="activeTab === 'overview'">
      <dl class="ai-business-facts">{fact_markup}</dl>
    </div>
    <div class="ai-business-panel" x-show="activeTab === 'contact'" x-cloak>
      <p>Contact {escaped(business_name_raw)} using the verified details supplied with this business record.</p>
      <div class="ai-business-actions">{''.join(contact_links)}</div>
    </div>
    <div class="ai-business-panel" x-show="activeTab === 'questions'" x-cloak>
      <div class="ai-business-faqs">{faq_markup}</div>
    </div>
  </div>
</section>'''


def ensure_business_details_section(site_html: str, context: Dict[str, Any]) -> str:
    section = business_details_section(context)
    if "data-ai-business-details" in site_html.lower():
        return re.sub(
            r"<section\b[^>]*data-ai-business-details[^>]*>.*?</section>",
            section,
            site_html,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
    contact_match = re.search(r"<section\b[^>]*\bid=[\"']contact[\"'][^>]*>", site_html, flags=re.IGNORECASE)
    if contact_match:
        return site_html[:contact_match.start()] + section + "\n" + site_html[contact_match.start():]
    return inject_before_closing_tag(site_html, "main" if "</main>" in site_html.lower() else "body", section)


def local_business_schema(context: Dict[str, Any], description: str) -> Dict[str, Any]:
    schema: Dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "LocalBusiness",
        "name": compact_text(context.get("businessName"), "Local Business"),
        "description": description,
        "areaServed": compact_text(context.get("location"), "South Africa"),
    }
    phone = compact_text(context.get("phone"))
    email = normalize_email_identity(context.get("email"))
    address = compact_text(context.get("address"))
    image_url = normalize_url(context.get("mainImageUrl"))
    source_url = normalize_url(context.get("sourceUrl"))
    if phone:
        schema["telephone"] = phone
    if email:
        schema["email"] = email
    if address:
        schema["address"] = {"@type": "PostalAddress", "streetAddress": address}
    if image_url:
        schema["image"] = image_url
    if source_url:
        schema["sameAs"] = [source_url]
    return schema


def ensure_site_seo_metadata(site_html: str, context: Dict[str, Any]) -> str:
    html_value = ensure_html_document_shell(site_html)
    profile = personalized_business_profile(context)
    business_name = compact_text(context.get("businessName"), "Local Business")
    industry = compact_text(profile.get("industry"), context.get("industry") or "Local Business")
    location = compact_text(context.get("location"), "South Africa")
    title = concise_meta_text(f"{business_name} | {industry} in {location}", business_name, 70)
    description = concise_meta_text(
        context.get("summary") or profile.get("heroCaption"),
        f"Explore {industry.lower()} services, verified business details, and contact options for {business_name} in {location}.",
    )
    title_escaped = html.escape(title)
    description_escaped = html.escape(description, quote=True)
    image_url = normalize_url(context.get("mainImageUrl"))
    theme = business_theme_for_context(context)
    indexing_enabled = context.get("seoIndexingEnabled") is True
    robots_content = "index, follow, max-image-preview:large" if indexing_enabled else "noindex, nofollow"
    indexing_mode = "production" if indexing_enabled else "preview"

    html_value = replace_head_element(html_value, r"<title\b[^>]*>.*?</title>", f"<title>{title_escaped}</title>")
    html_value = replace_head_element(
        html_value,
        r"<meta\b(?=[^>]*\bname=[\"']description[\"'])[^>]*>",
        f'<meta name="description" content="{description_escaped}">',
    )
    html_value = replace_head_element(
        html_value,
        r"<meta\b(?=[^>]*\bname=[\"']robots[\"'])[^>]*>",
        f'<meta name="robots" content="{robots_content}">',
    )
    html_value = replace_head_element(
        html_value,
        r"<meta\b(?=[^>]*\bname=[\"']ai-site-indexing-mode[\"'])[^>]*>",
        f'<meta name="ai-site-indexing-mode" content="{indexing_mode}">',
    )
    html_value = replace_head_element(
        html_value,
        r"<meta\b(?=[^>]*\bname=[\"']theme-color[\"'])[^>]*>",
        f'<meta name="theme-color" content="{theme["highlight"]}">',
    )
    social_tags = [
        '<meta property="og:type" content="website">',
        f'<meta property="og:title" content="{html.escape(title, quote=True)}">',
        f'<meta property="og:description" content="{description_escaped}">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{html.escape(title, quote=True)}">',
        f'<meta name="twitter:description" content="{description_escaped}">',
    ]
    if image_url:
        image_escaped = html.escape(image_url, quote=True)
        social_tags.extend(
            [
                f'<meta property="og:image" content="{image_escaped}">',
                f'<meta name="twitter:image" content="{image_escaped}">',
            ]
        )
    for attribute, key, tag in (
        ("property", "og:type", social_tags[0]),
        ("property", "og:title", social_tags[1]),
        ("property", "og:description", social_tags[2]),
        ("name", "twitter:card", social_tags[3]),
        ("name", "twitter:title", social_tags[4]),
        ("name", "twitter:description", social_tags[5]),
    ):
        html_value = replace_head_element(
            html_value,
            rf"<meta\b(?=[^>]*\b{attribute}=[\"']{re.escape(key)}[\"'])[^>]*>",
            tag,
        )
    if image_url:
        html_value = replace_head_element(
            html_value,
            r"<meta\b(?=[^>]*\bproperty=[\"']og:image[\"'])[^>]*>",
            social_tags[6],
        )
        html_value = replace_head_element(
            html_value,
            r"<meta\b(?=[^>]*\bname=[\"']twitter:image[\"'])[^>]*>",
            social_tags[7],
        )

    schema_markup = (
        '<script id="ai-site-local-business-schema" type="application/ld+json">'
        + json.dumps(local_business_schema(context, description), ensure_ascii=False).replace("</", "<\\/")
        + "</script>"
    )
    html_value = re.sub(
        r"[ \t]*<script\b[^>]*\bid=[\"']ai-site-local-business-schema[\"'][^>]*>.*?</script>[ \t]*(?:\r?\n)?",
        "",
        html_value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    html_value = inject_before_closing_tag(html_value, "head", schema_markup)

    html_value = ensure_primary_heading(html_value, context)
    return ensure_image_alt_text(html_value, context)


def validate_generated_site_seo(site_html: str, context: Dict[str, Any]) -> Dict[str, Any]:
    soup = BeautifulSoup(site_html, "html.parser")
    business_name = compact_text(context.get("businessName"))
    industry = compact_text(personalized_business_profile(context).get("industry"))
    location = compact_text(context.get("location"))
    visible_text = normalize_identity_text(soup.get_text(" ", strip=True))
    title = compact_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    description_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.IGNORECASE)})
    robots_tag = soup.find("meta", attrs={"name": re.compile(r"^robots$", re.IGNORECASE)})
    robots_value = compact_text(robots_tag.get("content") if robots_tag else "").lower()
    robots_tokens = {token.strip() for token in robots_value.split(",") if token.strip()}
    expected_robots = {"index", "follow"} if context.get("seoIndexingEnabled") is True else {"noindex", "nofollow"}
    schema_tag = soup.find("script", attrs={"id": "ai-site-local-business-schema"})
    schema_value: Dict[str, Any] = {}
    schema_valid = False
    if schema_tag:
        try:
            schema_value = json.loads(schema_tag.string or schema_tag.get_text())
            schema_valid = (
                schema_value.get("@context") == "https://schema.org"
                and schema_value.get("@type") == "LocalBusiness"
                and normalize_identity_text(schema_value.get("name")) == normalize_identity_text(business_name)
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            schema_valid = False

    expected_facts = [value for value in (business_name, industry, location) if compact_text(value)]
    for optional_key in ("address", "phone", "email", "rating", "reviewsCount"):
        optional_value = compact_text(context.get(optional_key))
        if optional_value:
            expected_facts.append(optional_value)
    facts_present = all(normalize_identity_text(value) in visible_text for value in expected_facts)
    images = soup.find_all("img")
    image_alt_valid = all(image.has_attr("alt") for image in images)
    checks = {
        "doctype": bool(re.search(r"<!doctype\s+html", site_html, flags=re.IGNORECASE)),
        "language": bool(soup.html and compact_text(soup.html.get("lang"))),
        "title": bool(title and business_name and normalize_identity_text(business_name) in normalize_identity_text(title)),
        "metaDescription": bool(description_tag and compact_text(description_tag.get("content"))),
        "robots": bool(robots_tag and expected_robots.issubset(robots_tokens)),
        "singleH1": len(soup.find_all("h1")) == 1,
        "businessDetails": bool(soup.find(attrs={"data-ai-business-details": True})) and facts_present,
        "localBusinessSchema": schema_valid,
        "imageAltText": image_alt_valid,
        "interactiveProfile": (
            "alpinejs@3.15.12" in site_html.lower()
            and "motion@12.42.2" in site_html.lower()
            and bool(soup.find(attrs={"x-data": True}))
        ),
    }
    errors = [name for name, passed in checks.items() if not passed]
    warnings = []
    if not soup.find("link", attrs={"rel": lambda value: value and "canonical" in value}):
        warnings.append("canonical URL is deferred until the final production domain is known")
    if len(title) > 70:
        warnings.append("title may be truncated in search results")
    description = compact_text(description_tag.get("content") if description_tag else "")
    if len(description) > 160:
        warnings.append("meta description exceeds the preferred concise summary length")
    return {
        "passed": not errors,
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "title": title,
        "description": description,
        "robots": robots_value,
        "indexingEnabled": "index" in robots_tokens,
    }


def enforce_generated_site_seo(site_html: str, context: Dict[str, Any]) -> Dict[str, Any]:
    report = validate_generated_site_seo(site_html, context)
    if not report["passed"]:
        raise SiteSeoValidationError(report)
    return report


def remove_disallowed_generated_site_assets(site_html: str) -> str:
    html_value = re.sub(
        r"\s*<script\b(?=[^>]*\bsrc=[\"'][^\"']*cdn\.tailwindcss\.com[^\"']*[\"'])[^>]*>\s*</script>",
        "",
        site_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return re.sub(
        r"\s*<link\b(?=[^>]*\bhref=[\"'][^\"']*(?:animate(?:\.min)?\.css|/animate\.css/)[^\"']*[\"'])[^>]*>",
        "",
        html_value,
        flags=re.IGNORECASE | re.DOTALL,
    )


def ensure_required_site_features(
    site_html: str,
    theme_context: Optional[Dict[str, Any]] = None,
) -> str:
    html_value = remove_disallowed_generated_site_assets(
        ensure_html_document_shell(upgrade_legacy_fallback_image_data_uris(site_html))
    )
    has_business_context = bool(
        isinstance(theme_context, dict) and compact_text(theme_context.get("businessName"))
    )
    if has_business_context:
        html_value = ensure_business_details_section(html_value, theme_context)
    lower_html = html_value.lower()
    business_theme = (
        business_theme_for_context(theme_context)
        if isinstance(theme_context, dict)
        else existing_site_business_theme(html_value) or dict(DEFAULT_BUSINESS_THEME)
    )
    business_theme_deep = mix_hex_color(business_theme["highlight"], "#000000", 0.28)
    business_theme_soft = mix_hex_color(business_theme["highlight"], "#ffffff", 0.24)

    bootstrap_css = (
        '<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" '
        'rel="stylesheet" '
        'integrity="sha384-sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB" crossorigin="anonymous">'
    )
    alpine_js = (
        '<script id="ai-site-alpine-runtime" defer '
        'src="https://cdn.jsdelivr.net/npm/alpinejs@3.15.12/dist/cdn.min.js"></script>'
    )
    motion_js = (
        '<script id="ai-site-motion-runtime" type="module">'
        'import { animate } from "https://cdn.jsdelivr.net/npm/motion@12.42.2/mini/+esm";'
        'window.aiSiteMotionAnimate = animate;'
        '</script>'
    )
    bootstrap_js = (
        '<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/js/bootstrap.bundle.min.js"></script>'
    )
    gsap_js = '<script src="https://cdn.jsdelivr.net/npm/gsap@3.15/dist/gsap.min.js"></script>'
    interactive_css = """<style id="ai-site-interactive-profile-style" data-ai-site-profile="highly-interactive">
  [x-cloak] { display: none !important; }
  .ai-business-details { padding: clamp(3.5rem, 8vw, 6.5rem) 0; background: color-mix(in srgb, var(--ai-background) 92%, var(--ai-highlight) 8%); }
  .ai-business-details h2 { max-width: 820px; margin: .5rem 0 1rem; font-size: clamp(2rem, 5vw, 3.5rem); }
  .ai-business-summary { max-width: 760px; color: color-mix(in srgb, var(--ai-text) 75%, transparent); font-size: 1.08rem; }
  .ai-business-tabs { display: flex; flex-wrap: wrap; gap: .65rem; margin: 1.75rem 0 1rem; }
  .ai-business-tabs button { min-height: 44px; padding: .7rem 1rem; border: 1px solid color-mix(in srgb, var(--ai-highlight) 35%, transparent); border-radius: 999px; background: transparent; color: var(--ai-text); font-weight: 800; }
  .ai-business-tabs button.active, .ai-business-tabs button:hover, .ai-business-tabs button:focus-visible { background: var(--ai-highlight); color: var(--ai-on-highlight); outline-offset: 3px; }
  .ai-business-panel { padding: clamp(1.1rem, 3vw, 2rem); border: 1px solid color-mix(in srgb, var(--ai-highlight) 24%, transparent); border-radius: 22px; background: color-mix(in srgb, var(--ai-background) 82%, #ffffff 18%); box-shadow: 0 18px 55px rgba(15, 23, 42, .09); }
  .ai-business-facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: .85rem; margin: 0; }
  .ai-business-fact { min-width: 0; padding: 1rem; border-radius: 15px; background: color-mix(in srgb, var(--ai-background) 88%, var(--ai-highlight) 12%); }
  .ai-business-fact dt { margin-bottom: .3rem; color: var(--ai-highlight); font-size: .75rem; font-weight: 900; letter-spacing: .08em; text-transform: uppercase; }
  .ai-business-fact dd { margin: 0; overflow-wrap: anywhere; color: var(--ai-text); font-weight: 750; }
  .ai-business-actions { display: flex; flex-wrap: wrap; gap: .75rem; }
  .ai-business-faq { border-bottom: 1px solid color-mix(in srgb, var(--ai-highlight) 22%, transparent); }
  .ai-business-faq:last-child { border-bottom: 0; }
  .ai-business-faq h3 { margin: 0; font-size: 1rem; }
  .ai-business-faq button { width: 100%; min-height: 52px; display: flex; align-items: center; justify-content: space-between; gap: 1rem; border: 0; background: transparent; color: var(--ai-text); text-align: left; font: inherit; font-weight: 850; }
  .ai-business-faq p { margin: 0; padding: 0 0 1rem; }
  [data-ai-interaction] { transition: transform .2s ease, box-shadow .2s ease; }
  @media (prefers-reduced-motion: reduce) {
    html { scroll-behavior: auto !important; }
    *, *::before, *::after { animation-duration: .001ms !important; animation-iteration-count: 1 !important; transition-duration: .001ms !important; }
  }
</style>"""
    widget_css = """<style id="ai-site-theme-widget-style" data-ai-site-theme-version="3">
  :root {
    --ai-text: __AI_TEXT__;
    --ai-background: __AI_BACKGROUND__;
    --ai-highlight: __AI_HIGHLIGHT__;
    --ai-highlight-deep: __AI_HIGHLIGHT_DEEP__;
    --ai-highlight-soft: __AI_HIGHLIGHT_SOFT__;
    --ai-on-highlight: #ffffff;
  }
  body {
    color: var(--ai-text) !important;
    background: var(--ai-background) !important;
    accent-color: var(--ai-highlight);
  }
  main,
  section,
  .card,
  .service-card,
  .about-panel,
  .contact-card {
    color: var(--ai-text);
  }
  a,
  .section-kicker,
  .service-number {
    color: var(--ai-highlight);
  }
  .btn-primary,
  .btn-brand,
  .badge,
  .service-icon,
  button[type="submit"] {
    background: var(--ai-highlight) !important;
    border-color: var(--ai-highlight) !important;
    color: var(--ai-on-highlight) !important;
  }
  .hero,
  .hero-section,
  .about-gradient,
  .cta-band,
  .ai-generated-hero-image {
    background: linear-gradient(
      135deg,
      var(--ai-highlight-deep) 0%,
      var(--ai-highlight) 52%,
      var(--ai-highlight-soft) 100%
    ) !important;
    color: var(--ai-on-highlight) !important;
  }
  :is(.hero, .hero-section, .about-gradient, .cta-band, .ai-generated-hero-image)
  :is(h1, h2, h3, h4, h5, h6, p, .hero-title, .hero-text, .hero-badge, .section-kicker) {
    color: var(--ai-on-highlight) !important;
  }
  :is(.hero, .hero-section, .about-gradient, .cta-band, .ai-generated-hero-image)
  :is(.floating-chip, .fact, .btn-light) {
    color: var(--ai-text) !important;
  }
  :is(.hero, .hero-section, .about-gradient, .cta-band, .ai-generated-hero-image)
  :is(.btn-outline-light, .contact-pill) {
    color: var(--ai-on-highlight) !important;
    border-color: var(--ai-on-highlight) !important;
  }
  .hero-title,
  .hero-card,
  .hero-card-body,
  .hero-card-body :is(h1, h2, h3, h4, h5, h6, p),
  .floating-chip {
    min-width: 0;
    max-width: 100%;
    overflow-wrap: anywhere;
  }
  .floating-chip {
    white-space: normal;
    text-align: left;
  }
  :is(.hero-card, .hero-image, .ai-generated-hero-image)
  img[src^="data:image/svg+xml;base64,"] {
    width: 100% !important;
    height: auto !important;
    min-height: 0 !important;
    aspect-ratio: 3 / 2 !important;
    object-fit: contain !important;
  }
  .floating-chip,
  .fact,
  .stat-card {
    border-color: var(--ai-highlight) !important;
  }
  .ai-site-theme-widget {
    position: fixed;
    right: 16px;
    bottom: 16px;
    z-index: 2147483000;
    width: min(290px, calc(100vw - 32px));
    padding: 12px;
    border: 1px solid rgba(15, 23, 42, 0.14);
    border-radius: 12px;
    background: rgba(255, 255, 255, 0.94);
    color: #0f172a;
    box-shadow: 0 18px 42px rgba(15, 23, 42, 0.2);
    backdrop-filter: blur(10px);
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  .ai-site-theme-widget strong {
    display: block;
    margin-bottom: 8px;
    font-size: 13px;
  }
  .ai-site-theme-controls {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
  }
  .ai-site-theme-controls label {
    display: grid;
    gap: 4px;
    font-size: 11px;
    font-weight: 700;
  }
  .ai-site-theme-controls input {
    width: 100%;
    min-height: 34px;
    border: 0;
    padding: 0;
    background: transparent;
  }
  .ai-site-theme-reset {
    width: 100%;
    margin-top: 8px;
    border: 1px solid rgba(15, 23, 42, 0.18);
    border-radius: 8px;
    background: #f8fafc;
    color: #0f172a;
    font-weight: 800;
    min-height: 34px;
  }
</style>"""
    widget_html = """<aside class="ai-site-theme-widget" data-ai-site-theme-widget data-ai-default-text="__AI_TEXT__" data-ai-default-background="__AI_BACKGROUND__" data-ai-default-highlight="__AI_HIGHLIGHT__" data-ai-theme-name="__AI_THEME_NAME__" aria-label="Site color controls">
  <strong>Site colors</strong>
  <div class="ai-site-theme-controls">
    <label>Text<input type="color" data-theme-color="text" value="__AI_TEXT__"></label>
    <label>Background<input type="color" data-theme-color="background" value="__AI_BACKGROUND__"></label>
    <label>Highlights<input type="color" data-theme-color="highlight" value="__AI_HIGHLIGHT__"></label>
  </div>
  <button class="ai-site-theme-reset" type="button" data-theme-reset>Reset colors</button>
</aside>"""
    widget_js = """<script id="ai-site-theme-widget-script" data-ai-site-theme-version="3">
  window.addEventListener("DOMContentLoaded", function () {
    var widget = document.querySelector("[data-ai-site-theme-widget]");
    var themeName = widget && widget.dataset.aiThemeName ? widget.dataset.aiThemeName : "local-service";
    var storageKey = "ai-site-factory-theme-v3-" + themeName;
    var defaults = {
      text: widget && widget.dataset.aiDefaultText ? widget.dataset.aiDefaultText : "__AI_TEXT__",
      background: widget && widget.dataset.aiDefaultBackground ? widget.dataset.aiDefaultBackground : "__AI_BACKGROUND__",
      highlight: widget && widget.dataset.aiDefaultHighlight ? widget.dataset.aiDefaultHighlight : "__AI_HIGHLIGHT__"
    };
    var root = document.documentElement;
    function normalizeHex(value, fallback) {
      return /^#[0-9a-f]{6}$/i.test(value || "") ? value : fallback;
    }
    function rgb(hex) {
      var value = parseInt(hex.slice(1), 16);
      return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
    }
    function mix(hex, target, amount) {
      var sourceRgb = rgb(hex);
      var targetRgb = rgb(target);
      var channels = sourceRgb.map(function (channel, index) {
        return Math.round(channel + (targetRgb[index] - channel) * amount);
      });
      return "#" + channels.map(function (channel) {
        return channel.toString(16).padStart(2, "0");
      }).join("");
    }
    function luminance(hex) {
      return rgb(hex).map(function (channel) {
        var value = channel / 255;
        return value <= 0.04045 ? value / 12.92 : Math.pow((value + 0.055) / 1.055, 2.4);
      }).reduce(function (total, value, index) {
        return total + value * [0.2126, 0.7152, 0.0722][index];
      }, 0);
    }
    function contrast(first, second) {
      var brighter = Math.max(luminance(first), luminance(second));
      var darker = Math.min(luminance(first), luminance(second));
      return (brighter + 0.05) / (darker + 0.05);
    }
    function decodeSvgDataUri(source) {
      try {
        var payload = source.split(",", 2)[1] || "";
        var binary = atob(payload);
        var bytes = Uint8Array.from(binary, function (character) { return character.charCodeAt(0); });
        return new TextDecoder().decode(bytes);
      } catch (error) {
        return "";
      }
    }
    function encodeSvgDataUri(svg) {
      var bytes = new TextEncoder().encode(svg);
      var binary = "";
      bytes.forEach(function (value) { binary += String.fromCharCode(value); });
      return "data:image/svg+xml;base64," + btoa(binary);
    }
    function recolorFallbackImages(highlight, highlightSoft) {
      document.querySelectorAll('img[src^="data:image/svg+xml;base64,"]').forEach(function (image) {
        var template = image.aiSiteFallbackSvgTemplate;
        if (!template) {
          template = decodeSvgDataUri(image.getAttribute("src") || "");
          if (!template.includes("ai-site-fallback-image-v2")) return;
          image.aiSiteFallbackSvgTemplate = template;
        }
        var documentValue = new DOMParser().parseFromString(template, "image/svg+xml");
        if (documentValue.querySelector("parsererror")) return;
        documentValue.querySelectorAll('[data-ai-color="highlight"]').forEach(function (element) {
          var attribute = element.tagName.toLowerCase() === "stop" ? "stop-color" : "fill";
          element.setAttribute(attribute, highlight);
        });
        documentValue.querySelectorAll('[data-ai-color="highlight-soft"]').forEach(function (element) {
          var attribute = element.tagName.toLowerCase() === "stop" ? "stop-color" : "fill";
          element.setAttribute(attribute, highlightSoft);
        });
        image.setAttribute("src", encodeSvgDataUri(new XMLSerializer().serializeToString(documentValue)));
      });
    }
    function readTheme() {
      try { return Object.assign({}, defaults, JSON.parse(localStorage.getItem(storageKey) || "{}")); }
      catch (error) { return Object.assign({}, defaults); }
    }
    function applyTheme(theme) {
      var text = normalizeHex(theme.text, defaults.text);
      var background = normalizeHex(theme.background, defaults.background);
      var highlight = normalizeHex(theme.highlight, defaults.highlight);
      var lightText = "#ffffff";
      var darkText = "#102033";
      var onHighlight = contrast(darkText, highlight) >= contrast(lightText, highlight) ? darkText : lightText;
      var highlightDeep = mix(highlight, onHighlight === darkText ? "#ffffff" : "#000000", onHighlight === darkText ? 0.08 : 0.28);
      var highlightSoft = mix(highlight, onHighlight === darkText ? "#ffffff" : "#000000", onHighlight === darkText ? 0.24 : 0.08);
      root.style.setProperty("--ai-text", text);
      root.style.setProperty("--ai-background", background);
      root.style.setProperty("--ai-highlight", highlight);
      root.style.setProperty("--ai-highlight-deep", highlightDeep);
      root.style.setProperty("--ai-highlight-soft", highlightSoft);
      root.style.setProperty("--ai-on-highlight", onHighlight);
      root.style.setProperty("--ink", text);
      root.style.setProperty("--dark", text);
      root.style.setProperty("--background", background);
      root.style.setProperty("--template-bg", background);
      root.style.setProperty("--primary", highlight);
      root.style.setProperty("--secondary", highlightSoft);
      root.style.setProperty("--accent", highlightDeep);
      root.style.setProperty("--template-accent", highlight);
      recolorFallbackImages(highlight, highlightSoft);
      document.querySelectorAll("[data-theme-color]").forEach(function (input) {
        input.value = theme[input.dataset.themeColor] || defaults[input.dataset.themeColor];
      });
    }
    function saveTheme(theme) {
      localStorage.setItem(storageKey, JSON.stringify(theme));
      applyTheme(theme);
    }
    var theme = readTheme();
    applyTheme(theme);
    document.querySelectorAll("[data-theme-color]").forEach(function (input) {
      input.addEventListener("input", function () {
        theme[input.dataset.themeColor] = input.value;
        saveTheme(theme);
      });
    });
    var reset = document.querySelector("[data-theme-reset]");
    if (reset) {
      reset.addEventListener("click", function () {
        theme = Object.assign({}, defaults);
        localStorage.removeItem(storageKey);
        applyTheme(theme);
      });
    }
  });
</script>"""
    theme_tokens = {
        "__AI_TEXT__": business_theme["text"],
        "__AI_BACKGROUND__": business_theme["background"],
        "__AI_HIGHLIGHT__": business_theme["highlight"],
        "__AI_HIGHLIGHT_DEEP__": business_theme_deep,
        "__AI_HIGHLIGHT_SOFT__": business_theme_soft,
        "__AI_THEME_NAME__": html.escape(business_theme["name"], quote=True),
    }
    for token, value in theme_tokens.items():
        widget_css = widget_css.replace(token, value)
        widget_html = widget_html.replace(token, value)
        widget_js = widget_js.replace(token, value)
    animation_js = """
<script id="ai-site-gsap-fallback-animation">
  window.addEventListener("DOMContentLoaded", function () {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    if (window.gsap) {
      gsap.from("header, .hero, .hero-content > *, .hero-visual", {
        y: 24,
        opacity: 0,
        duration: 0.75,
        ease: "power2.out",
        stagger: 0.08
      });
    }
  });
</script>"""
    interactive_js = """<script id="ai-site-interactive-profile-script" data-ai-site-profile="highly-interactive">
  window.addEventListener("DOMContentLoaded", function () {
    var reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reducedMotion || !window.aiSiteMotionAnimate) return;
    var animate = window.aiSiteMotionAnimate;
    var reveal = function (element) {
      if (element.dataset.aiRevealed) return;
      element.dataset.aiRevealed = "true";
      animate(element, { opacity: [0, 1], transform: ["translateY(28px)", "translateY(0px)"] }, { duration: .65, ease: "easeOut" });
    };
    var targets = Array.from(document.querySelectorAll("main > section, [data-ai-reveal]"));
    if ("IntersectionObserver" in window) {
      var observer = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          reveal(entry.target);
          observer.unobserve(entry.target);
        });
      }, { threshold: .12 });
      targets.forEach(function (element) { observer.observe(element); });
    } else {
      targets.forEach(reveal);
    }
    document.querySelectorAll("[data-ai-interaction]").forEach(function (element) {
      element.addEventListener("click", function () {
        animate(element, { transform: ["scale(1)", "scale(.97)", "scale(1)"] }, { duration: .24 });
      });
    });
  });
</script>"""

    if "ai-site-theme-widget-style" in lower_html:
        html_value = replace_element_by_id(
            html_value,
            "style",
            "ai-site-theme-widget-style",
            widget_css,
        )
    if "ai-site-theme-widget-script" in lower_html:
        html_value = replace_element_by_id(
            html_value,
            "script",
            "ai-site-theme-widget-script",
            widget_js,
        )
    if "data-ai-site-theme-widget" in lower_html:
        html_value = re.sub(
            r"<aside\b[^>]*data-ai-site-theme-widget[^>]*>.*?</aside>",
            widget_html,
            html_value,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )

    lower_html = html_value.lower()
    if "bootstrap@5.3.8/dist/css/bootstrap.min.css" not in lower_html:
        html_value = inject_before_closing_tag(html_value, "head", f"  {bootstrap_css}")

    lower_html = html_value.lower()
    if "alpinejs@3.15.12" not in lower_html:
        html_value = inject_before_closing_tag(html_value, "head", f"  {alpine_js}")

    lower_html = html_value.lower()
    if "motion@12.42.2" not in lower_html:
        html_value = inject_before_closing_tag(html_value, "head", f"  {motion_js}")

    lower_html = html_value.lower()
    if "ai-site-interactive-profile-style" in lower_html:
        html_value = replace_element_by_id(
            html_value,
            "style",
            "ai-site-interactive-profile-style",
            interactive_css,
        )
    else:
        html_value = inject_before_closing_tag(html_value, "head", interactive_css)

    lower_html = html_value.lower()
    if "ai-site-theme-widget-style" not in lower_html:
        html_value = inject_before_closing_tag(html_value, "head", widget_css)

    lower_html = html_value.lower()
    scripts = []
    if "bootstrap@5.3.8/dist/js/bootstrap.bundle.min.js" not in lower_html:
        scripts.append(bootstrap_js)
    if "gsap@3.15/dist/gsap.min.js" not in lower_html:
        scripts.append(gsap_js)
    if "gsap.from" not in lower_html and "ai-site-gsap-fallback-animation" not in lower_html:
        scripts.append(animation_js)
    if "data-ai-site-theme-widget" not in lower_html:
        scripts.append(widget_html)
    if "ai-site-theme-widget-script" not in lower_html:
        scripts.append(widget_js)
    if "ai-site-interactive-profile-script" not in lower_html:
        scripts.append(interactive_js)

    if scripts:
        injection = "\n".join(scripts)
        html_value = inject_before_closing_tag(html_value, "body", injection)

    if has_business_context:
        html_value = ensure_site_seo_metadata(html_value, theme_context)
        report = enforce_generated_site_seo(html_value, theme_context)
        html_value = replace_head_element(
            html_value,
            r"<meta\b(?=[^>]*\bname=[\"']ai-site-seo-gate[\"'])[^>]*>",
            '<meta name="ai-site-seo-gate" content="passed">',
        )

    return html_value


def prepare_generated_site_artifact(site_html: str, context: Dict[str, Any]) -> Dict[str, Any]:
    enriched_html = ensure_required_site_features(site_html, context)
    return {
        "html": enriched_html,
        "seoValidation": enforce_generated_site_seo(enriched_html, context),
    }


def ensure_bootstrap_gsap_assets(
    site_html: str,
    theme_context: Optional[Dict[str, Any]] = None,
) -> str:
    return ensure_required_site_features(site_html, theme_context)


def generate_outreach_with_groq(context: Dict[str, Any], site_url: str) -> Dict[str, Any]:
    business_name = compact_text(context.get("businessName"), "your business")
    source_label = compact_text(context.get("source") or context.get("sourceLabel"), "a public business listing")
    source_url = compact_text(context.get("sourceUrl"))
    email = normalize_email_identity(context.get("email"))
    phone = compact_text(context.get("phone"))
    contact_type = "email" if email else "phone" if phone else "unknown"

    if contact_type == "email":
        body = (
            f"Hi {business_name} team,\n\n"
            f"We found your business through {source_label}"
            f"{f' ({source_url})' if source_url else ''}.\n\n"
            "If you are interested in exploring a simple website for your business, please reply to this email to opt in. "
            "An agent can then follow up with details.\n\n"
            f"Preview link: {site_url}\n\n"
            "Kind regards,\nAI Site Factory"
        )
        subject = f"Website preview for {business_name}"
    elif contact_type == "phone":
        body = (
            "Private agent note: phone-only no-website lead.\n\n"
            f"Business: {business_name}\n"
            f"Phone: {phone}\n"
            f"Source: {source_label}{f' ({source_url})' if source_url else ''}\n"
            f"Live link: {site_url}\n\n"
            "Agent action: call the business, explain where the public listing was found, ask for consent to discuss the site, "
            "and only continue if they opt in."
        )
        subject = f"Consent call needed for {business_name}"
    else:
        body = (
            "Private agent note: no email or phone was available for this no-website lead.\n\n"
            f"Business: {business_name}\n"
            f"Source: {source_label}{f' ({source_url})' if source_url else ''}\n"
            f"Live link: {site_url}\n\n"
            "Agent action: research a lawful contact path before any outreach."
        )
        subject = f"Review contact path for {business_name}"

    return {
        "subject": subject,
        "body": body,
        "recipientEmail": email,
        "phone": phone or None,
        "siteUrl": site_url,
        "contactType": contact_type,
        "publicComment": False,
    }


def fit_svg_text(
    value: str,
    max_width: int,
    max_height: int,
    preferred_font_size: int,
    width_factor: float,
) -> Tuple[List[str], int, int]:
    """Wrap text into a bounded SVG region, shrinking only when wrapping is insufficient."""

    clean_value = compact_text(value)
    for font_size in range(preferred_font_size, 0, -1):
        line_height = max(font_size + 2, round(font_size * 1.2))
        line_capacity = max(1, int(max_width / (font_size * width_factor)))
        words = clean_value.split(" ") if clean_value else [""]
        lines: List[str] = []
        current = ""

        for word in words:
            remaining = word
            while len(remaining) > line_capacity:
                if current:
                    lines.append(current)
                    current = ""
                lines.append(remaining[:line_capacity])
                remaining = remaining[line_capacity:]

            candidate = f"{current} {remaining}".strip() if remaining else current
            if current and len(candidate) > line_capacity:
                lines.append(current)
                current = remaining
            else:
                current = candidate

        if current or not lines:
            lines.append(current)

        if len(lines) * line_height <= max_height:
            return lines, font_size, line_height

    return [clean_value], 1, 3


def svg_text_block(
    role: str,
    value: str,
    x: int,
    top: int,
    max_width: int,
    max_height: int,
    preferred_font_size: int,
    width_factor: float,
    font_weight: int,
    fill: str,
) -> str:
    lines, font_size, line_height = fit_svg_text(
        value,
        max_width,
        max_height,
        preferred_font_size,
        width_factor,
    )
    tspans = []
    for index, line in enumerate(lines):
        estimated_width = max(1, min(max_width, round(len(line) * font_size * width_factor)))
        safe_line = html.escape(line, quote=False)
        tspans.append(
            f"<tspan x='{x}' y='{top + font_size + (index * line_height)}' "
            f"textLength='{estimated_width}' lengthAdjust='spacingAndGlyphs'>{safe_line}</tspan>"
        )
    return (
        f"<text data-role='{role}' font-family='Arial, sans-serif' font-size='{font_size}' "
        f"font-weight='{font_weight}' fill='{fill}'>"
        f"{''.join(tspans)}</text>"
    )


def fallback_image_data_uri(label: str, accent: str, subtitle: str = "", detail: str = "") -> str:
    label_text = compact_text(label, "Business")
    subtitle_text = compact_text(subtitle, "Local service")
    detail_text = compact_text(detail, "Customer focused")
    safe_accent = compact_text(accent, "#0f9f96")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", safe_accent):
        safe_accent = "#0f9f96"
    title = html.escape(" | ".join((label_text, subtitle_text, detail_text)), quote=False)
    subtitle_markup = svg_text_block(
        "subtitle", subtitle_text, 626, 170, 436, 62, 22, 0.56, 800, safe_accent
    )
    label_markup = svg_text_block(
        "business-name", label_text, 626, 340, 436, 176, 48, 0.62, 800, "#102033"
    )
    detail_markup = svg_text_block(
        "detail", detail_text, 626, 526, 436, 92, 24, 0.56, 600, "#475467"
    )
    svg = (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='1200' height='800' viewBox='0 0 1200 800' role='img'>"
        f"<metadata id='ai-site-fallback-image-v2'>2</metadata>"
        f"<title>{title}</title>"
        f"<defs>"
        f"<linearGradient id='bg' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='#ecfeff'/><stop offset='0.55' stop-color='#f8fafc'/><stop offset='1' stop-color='#eef2ff'/></linearGradient>"
        f"<linearGradient id='accent' x1='0' y1='0' x2='1' y2='1'><stop data-ai-color='highlight' offset='0' stop-color='{safe_accent}'/><stop data-ai-color='highlight-soft' offset='1' stop-color='#1d9bf0'/></linearGradient>"
        f"<filter id='shadow' x='-20%' y='-20%' width='140%' height='140%'><feDropShadow dx='0' dy='28' stdDeviation='26' flood-color='#0f172a' flood-opacity='0.18'/></filter>"
        f"</defs>"
        f"<rect width='1200' height='800' fill='url(#bg)'/>"
        f"<circle data-ai-color='highlight' cx='970' cy='170' r='210' fill='{safe_accent}' opacity='0.13'/>"
        f"<circle data-ai-color='highlight-soft' cx='180' cy='650' r='250' fill='#1d9bf0' opacity='0.10'/>"
        f"<rect x='88' y='96' width='1024' height='608' rx='42' fill='#ffffff' opacity='0.78' filter='url(#shadow)'/>"
        f"<rect x='138' y='158' width='426' height='300' rx='30' fill='url(#accent)' opacity='0.95'/>"
        f"<path d='M188 388 C275 302 357 426 438 326 C482 272 524 278 560 238' fill='none' stroke='white' stroke-width='24' stroke-linecap='round' opacity='0.74'/>"
        f"<circle cx='256' cy='246' r='48' fill='white' opacity='0.85'/>"
        f"<rect data-ai-color='highlight' x='626' y='174' width='372' height='34' rx='17' fill='{safe_accent}' opacity='0.20'/>"
        f"<rect x='626' y='250' width='436' height='26' rx='13' fill='#0f172a' opacity='0.10'/>"
        f"<rect x='626' y='304' width='328' height='26' rx='13' fill='#0f172a' opacity='0.10'/>"
        f"<rect x='626' y='638' width='192' height='46' rx='23' fill='url(#accent)'/>"
        f"{subtitle_markup}"
        f"{label_markup}"
        f"{detail_markup}"
        f"</svg>"
    )
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def generate_gemini_images(prompts: List[str], accent: str) -> List[str]:
    if os.getenv("ENABLE_GEMINI_IMAGES", "false").lower() != "true":
        prompt_labels = [compact_text(prompt, f"Generated asset {index + 1}") for index, prompt in enumerate(prompts[:5])]
        while len(prompt_labels) < 5:
            prompt_labels.append(f"Generated asset {len(prompt_labels) + 1}")
        return [
            fallback_image_data_uri(prompt_labels[0], accent, "Generated hero", "Business-specific visual"),
            fallback_image_data_uri(prompt_labels[1], accent, "Service visual", "Practical support"),
            fallback_image_data_uri(prompt_labels[2], accent, "Service visual", "Customer experience"),
            fallback_image_data_uri(prompt_labels[3], accent, "Service visual", "Trust and quality"),
            fallback_image_data_uri(prompt_labels[4], accent, "Service visual", "Contact and booking"),
        ]

    api_key = require_env("GEMINI_API_KEY")
    model_name = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
    images: List[str] = []

    for prompt in [compact_text(prompt) for prompt in prompts[:5]]:
        try:
            response = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
                headers={
                    "x-goog-api-key": api_key,
                    "Content-Type": "application/json",
                },
                json={"contents": [{"parts": [{"text": prompt[:MODEL_CHUNK_CHARS]}]}]},
                timeout=60,
            )

            if response.status_code == 429:
                raise RuntimeError("Gemini image rate limit reached.")

            response.raise_for_status()
            data = response.json()
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])

            image_data = None
            mime_type = "image/png"

            for part in parts:
                inline_data = part.get("inlineData") or part.get("inline_data")
                if inline_data:
                    image_data = inline_data.get("data")
                    mime_type = inline_data.get("mimeType") or inline_data.get("mime_type") or mime_type
                    break

            if image_data:
                images.append(f"data:{mime_type};base64,{image_data}")

        except Exception as error:
            log_event(
                "warning",
                "provider.gemini_image.fallback",
                "Gemini image generation skipped for one prompt.",
                reason=str(error),
            )
            images.append(fallback_image_data_uri("Generated asset", accent))

    while len(images) < 5:
        images.append(fallback_image_data_uri(f"Generated asset {len(images) + 1}", accent))

    return images[:5]


def render_site_html(
    context: Dict[str, Any],
    site_content: Dict[str, Any],
    template: Dict[str, Any],
    images: List[str],
) -> str:
    accent = template.get("accent", "#0f766e")
    background = template.get("background", "#f8fafc")
    business_name = html.escape(compact_text(context.get("businessName"), "Local Business"))
    location = html.escape(compact_text(context.get("location"), "Local Area"))
    industry = html.escape(compact_text(context.get("industry"), "Services"))
    headline = html.escape(compact_text(site_content.get("headline"), business_name))
    subheadline = html.escape(compact_text(site_content.get("subheadline"), context.get("summary", "")))
    about = html.escape(compact_text(site_content.get("about"), context.get("summary", "")))
    cta_label = html.escape(compact_text(site_content.get("ctaLabel"), "Get in touch"))
    contact_intro = html.escape(compact_text(site_content.get("contactIntro"), "Reach out to learn more."))
    footer_text = html.escape(compact_text(site_content.get("footerText"), business_name))
    email = compact_text(context.get("email"))
    phone = compact_text(context.get("phone"))
    website = normalize_url(context.get("website"))
    address = html.escape(compact_text(context.get("address")))
    rating = compact_text(context.get("rating"))
    reviews_count = compact_text(context.get("reviewsCount"))
    source_label = html.escape(compact_text(context.get("source") or context.get("sourceLabel"), "public business listing"))
    source_url = normalize_url(context.get("sourceUrl"))
    main_image_url = normalize_url(context.get("mainImageUrl"))

    services_html = []
    services = site_content.get("services") or []
    for index, service in enumerate(services[:4]):
        title = html.escape(compact_text(service.get("title"), f"{industry} Service"))
        description = html.escape(compact_text(service.get("description"), "Practical support for local customers."))
        services_html.append(
            f"""
            <div class="col-md-6 col-xl-3">
              <article class="service-card card h-100">
                <div class="card-body">
                  <span class="service-number">Service 0{index + 1}</span>
                  <h3>{title}</h3>
                  <p>{description}</p>
                </div>
              </article>
            </div>
            """
        )

    contact_links = []
    contact_target = "#contact"
    contact_label = "Get in touch"
    if email:
        contact_links.append(f"<a href=\"mailto:{html.escape(email)}\">{html.escape(email)}</a>")
        contact_target = f"mailto:{email}"
        contact_label = "Email us"
    if phone:
        contact_links.append(f"<a href=\"tel:{html.escape(phone)}\">{html.escape(phone)}</a>")
        contact_target = f"tel:{phone}"
        contact_label = "Call now"
    if website:
        contact_links.append(f"<a href=\"{html.escape(website)}\" target=\"_blank\" rel=\"noreferrer\">Website</a>")
        if contact_target == "#contact":
            contact_target = website
            contact_label = "Visit website"
    if source_url:
        contact_links.append(f"<a href=\"{html.escape(source_url)}\" target=\"_blank\" rel=\"noreferrer\">Source listing</a>")
    contact_html = " ".join(contact_links) or "<span>Contact details available on request</span>"
    contact_attrs = ' target="_blank" rel="noreferrer"' if contact_target.startswith("http") else ""
    fact_items = [industry, location]
    if address:
        fact_items.append(address)
    if rating:
        rating_text = html.escape(f"{rating} rating" + (f" ({reviews_count} reviews)" if reviews_count else ""))
        fact_items.append(rating_text)
    else:
        fact_items.append(source_label)
    facts_html = "".join(f'<div class="col-md-3 col-sm-6"><div class="fact">{fact}</div></div>' for fact in fact_items[:4])
    hero_image_html = (
        f"""
        <div class="col-lg-6">
          <div class="hero-image">
            <img src="{html.escape(main_image_url, quote=True)}" alt="Main image for {business_name}" data-ai-business-main-image fetchpriority="high" loading="eager">
          </div>
        </div>
        """
        if main_image_url
        else ""
    )

    site_html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{business_name}</title>
  <meta name="description" content="{subheadline}">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet" integrity="sha384-sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB" crossorigin="anonymous">
  <style>
    :root {{
      --accent: {accent};
      --background: {background};
      --ink: #102033;
      --muted: #667085;
      --line: #d9e2ef;
      --surface: #ffffff;
      --navy: #071b33;
      --cyan: #1d9bf0;
      --teal: #0f9f96;
      --purple: #8b5cf6;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, Segoe UI, Arial, sans-serif;
      color: var(--ink);
      background: linear-gradient(180deg, #f8fbff 0%, var(--background) 100%);
      line-height: 1.6;
    }}
    a {{ color: inherit; }}
    .hero {{
      min-height: 720px;
      display: flex;
      align-items: center;
      padding: 88px 0 72px;
      color: var(--navy);
      background:
        radial-gradient(circle at top right, rgba(139, 92, 246, 0.28), transparent 32%),
        linear-gradient(135deg, #ecfeff 0%, #dff7ff 42%, #eef2ff 100%);
    }}
    .hero-content {{
      max-width: 680px;
    }}
    .eyebrow {{
      display: inline-flex;
      margin-bottom: 14px;
      padding: 8px 12px;
      border: 1px solid rgba(15, 159, 150, 0.28);
      border-radius: 8px;
      color: #0f766e;
      background: rgba(255, 255, 255, 0.72);
      font-size: 0.82rem;
      font-weight: 800;
      text-transform: uppercase;
    }}
    h1 {{
      margin: 0 0 18px;
      font-size: 4rem;
      line-height: 1;
      letter-spacing: 0;
      font-weight: 900;
    }}
    .hero-copy {{
      color: #40546a;
      font-size: 1.16rem;
    }}
    .hero-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-top: 28px;
    }}
    .btn-brand {{
      min-height: 50px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 8px;
      padding: 0 20px;
      background: linear-gradient(90deg, var(--teal), var(--cyan));
      color: #ffffff;
      font-weight: 800;
      text-decoration: none;
      box-shadow: 0 18px 40px rgba(29, 155, 240, 0.25);
      transition: transform 0.2s ease, box-shadow 0.2s ease;
    }}
    .btn-brand:hover {{
      color: #ffffff;
      transform: translateY(-2px);
      box-shadow: 0 22px 46px rgba(15, 159, 150, 0.3);
    }}
    .btn-ghost {{
      min-height: 50px;
      border: 1px solid rgba(16, 32, 51, 0.24);
      border-radius: 8px;
      padding: 0 20px;
      color: var(--navy);
      background: rgba(255, 255, 255, 0.65);
      font-weight: 800;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
    }}
    .hero-image {{
      overflow: hidden;
      border: 1px solid rgba(29, 155, 240, 0.22);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.68);
      box-shadow: 0 28px 70px rgba(29, 155, 240, 0.18);
    }}
    .hero-image img {{
      width: 100%;
      min-height: 440px;
      object-fit: cover;
      display: block;
      transition: transform 0.35s ease;
    }}
    .hero-image:hover img {{
      transform: scale(1.03);
    }}
    section {{
      padding: 76px 0;
    }}
    .section-head {{
      max-width: 760px;
      margin-bottom: 28px;
    }}
    .section-head h2 {{
      margin: 0 0 10px;
      font-size: 2.5rem;
      font-weight: 900;
      letter-spacing: 0;
    }}
    .section-head p {{
      color: var(--muted);
      font-size: 1.05rem;
    }}
    .service-card {{
      height: 100%;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: linear-gradient(180deg, #ffffff, #f8fbff);
      box-shadow: 0 16px 42px rgba(16, 32, 51, 0.08);
      transition: transform 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease;
    }}
    .service-card:hover {{
      border-color: var(--accent);
      transform: translateY(-4px);
      box-shadow: 0 22px 54px rgba(16, 32, 51, 0.13);
    }}
    .service-card img {{
      width: 100%;
      aspect-ratio: 4 / 3;
      object-fit: cover;
      display: block;
    }}
    .service-number {{
      color: var(--accent);
      font-size: 0.78rem;
      font-weight: 900;
      text-transform: uppercase;
    }}
    .service-card h3 {{
      margin: 8px 0 10px;
      font-size: 1.16rem;
      font-weight: 850;
    }}
    .service-card p {{
      color: var(--muted);
    }}
    .about-band {{
      background: #ffffff;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
    }}
    .fact {{
      padding: 16px 18px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: linear-gradient(180deg, #ffffff, #f7fbff);
      font-weight: 800;
    }}
    .contact-card {{
      border-radius: 8px;
      padding: 42px;
      color: var(--navy);
      background:
        radial-gradient(circle at top right, rgba(139, 92, 246, 0.24), transparent 28%),
        linear-gradient(135deg, #ecfeff, #e0f2fe 55%, #f5f3ff);
      box-shadow: 0 22px 60px rgba(29, 155, 240, 0.14);
    }}
    .contact-card p {{
      color: #40546a;
    }}
    .contact-links {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .contact-links a,
    .contact-links span {{
      border: 1px solid rgba(16, 32, 51, 0.16);
      border-radius: 8px;
      padding: 10px 12px;
      color: var(--navy);
      background: rgba(255, 255, 255, 0.72);
      text-decoration: none;
      font-weight: 750;
    }}
    footer {{
      padding: 32px 0;
      color: var(--muted);
      border-top: 1px solid var(--line);
    }}
    @media (max-width: 992px) {{
      .hero {{
        min-height: auto;
        padding: 64px 0;
      }}
      h1 {{
        font-size: 2.8rem;
      }}
      .hero-image img {{
        min-height: 320px;
      }}
      section {{
        padding: 56px 0;
      }}
    }}
    @media (max-width: 576px) {{
      h1 {{
        font-size: 2.25rem;
      }}
      .section-head h2 {{
        font-size: 1.8rem;
      }}
      .contact-card {{
        padding: 24px;
      }}
    }}
  </style>
</head>
<body>
  <header class="hero hero-section">
    <div class="container">
      <div class="row align-items-center g-5">
        <div class="col-lg-6 hero-content">
          <div class="eyebrow">{industry} in {location}</div>
          <h1>{headline}</h1>
          <p class="hero-copy">{subheadline}</p>
          <div class="hero-actions">
            <a class="btn-brand" href="{html.escape(contact_target)}"{contact_attrs}>{contact_label}</a>
            <a class="btn-ghost" href="#services">View services</a>
          </div>
        </div>
        {hero_image_html}
      </div>
    </div>
  </header>
  <main>
    <section id="services">
      <div class="container">
        <div class="section-head">
          <h2>Services built around {location}</h2>
          <p>{business_name} presents {industry.lower()} information with clear next steps, using details from {source_label}.</p>
        </div>
        <div class="row g-4">
          {''.join(services_html)}
        </div>
      </div>
    </section>
    <section class="about-band" id="about">
      <div class="container">
        <div class="row g-4 align-items-start">
          <div class="col-lg-5">
            <div class="section-head mb-0">
              <h2>About {business_name}</h2>
              <p>Clear information, local context, and a simple path for customers to reach out{f' from {address}' if address else ''}.</p>
            </div>
          </div>
          <div class="col-lg-7">
            <p class="lead">{about}</p>
            <div class="row g-3 mt-2">
              {facts_html}
            </div>
          </div>
        </div>
      </div>
    </section>
    <section id="contact">
      <div class="container">
        <div class="contact-card">
          <div class="row g-4 align-items-center">
            <div class="col-lg-7">
              <h2>{cta_label}</h2>
              <p>{contact_intro}</p>
            </div>
            <div class="col-lg-5">
              <div class="contact-links">{contact_html}</div>
            </div>
          </div>
        </div>
      </div>
    </section>
  </main>
  <footer>
    <div class="container d-flex flex-wrap justify-content-between gap-2">
      <span>{footer_text}</span>
      <span>Responsive landing page preview</span>
    </div>
  </footer>
  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/js/bootstrap.bundle.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/gsap@3.15/dist/gsap.min.js"></script>
  <script>
    window.addEventListener("DOMContentLoaded", function () {{
      if (window.gsap && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {{
        gsap.from(".hero-content > *", {{ y: 18, opacity: 0, duration: 0.75, ease: "power2.out", stagger: 0.08 }});
        gsap.from(".hero-image", {{ y: 24, opacity: 0, duration: 0.8, ease: "power2.out", delay: 0.15 }});
        gsap.from(".service-card, .about-band .row, .contact-card", {{ y: 22, opacity: 0, duration: 0.7, ease: "power2.out", stagger: 0.08, delay: 0.25 }});
      }}
    }});
  </script>
</body>
</html>"""
    return ensure_required_site_features(site_html, context)


def github_headers() -> Dict[str, str]:
    token = require_env("GITHUB_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "AI-Site-Factory",
    }


GITHUB_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def github_retry_attempts() -> int:
    try:
        configured = int(os.getenv("GITHUB_API_RETRY_ATTEMPTS", "5"))
    except (TypeError, ValueError):
        configured = 5
    return max(1, min(configured, 8))


def github_api_request(method: str, url: str, **kwargs: Any) -> requests.Response:
    """Retry transient GitHub transport and 5xx failures without changing the request."""
    attempts = github_retry_attempts()
    try:
        base_delay = float(os.getenv("GITHUB_API_RETRY_BASE_SECONDS", "1"))
    except (TypeError, ValueError):
        base_delay = 1.0
    base_delay = max(0.0, min(base_delay, 10.0))
    request_method = getattr(requests, method.lower())
    last_error: Optional[Exception] = None

    for attempt in range(1, attempts + 1):
        try:
            response = request_method(url, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as error:
            last_error = error
            if attempt >= attempts:
                raise
            status_code = None
            retry_after = None
        else:
            if response.status_code not in GITHUB_RETRYABLE_STATUS_CODES or attempt >= attempts:
                return response
            status_code = response.status_code
            retry_after = getattr(response, "headers", {}).get("Retry-After")

        delay = base_delay * (2 ** (attempt - 1))
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except (TypeError, ValueError):
                pass
        delay = min(delay, 30.0)
        log_event(
            "warning",
            "provider.github.transient_retry",
            "GitHub request failed transiently and will be retried.",
            method=method.upper(),
            path=urlparse(url).path,
            statusCode=status_code,
            attempt=attempt,
            maxAttempts=attempts,
            retryDelaySeconds=delay,
            errorType=last_error.__class__.__name__ if last_error else None,
        )
        if delay:
            time.sleep(delay)

    if last_error:
        raise last_error
    raise RuntimeError("GitHub request retry loop ended without a response.")


def github_repo_name(canonical_key: str, business_name: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    suffix = hashlib.sha1(canonical_key.encode("utf-8")).hexdigest()[:8]
    return f"ai-site-{slugify(business_name, 34)}-{timestamp}-{suffix}"


def github_readme(canonical_key: str, business_name: str, checksum: str) -> str:
    return (
        f"# {business_name}\n\n"
        "Generated by AI Site Factory.\n\n"
        f"- Canonical lead key: `{canonical_key}`\n"
        f"- HTML checksum: `{checksum}`\n"
        f"- Generated at: `{now_iso()}`\n"
    )


def latest_github_repo_for_lead(canonical_key: str) -> Optional[sqlite3.Row]:
    with get_pipeline_db() as db:
        return db.execute(
            "SELECT * FROM github_site_repos WHERE canonical_lead_key = ? AND export_status = 'EXPORTED'",
            (canonical_key,),
        ).fetchone()


def latest_github_repo_record_for_lead(canonical_key: str) -> Optional[sqlite3.Row]:
    with get_pipeline_db() as db:
        return db.execute(
            "SELECT * FROM github_site_repos WHERE canonical_lead_key = ? ORDER BY updated_at DESC LIMIT 1",
            (canonical_key,),
        ).fetchone()


def get_remote_github_repo(repo_full_name: str, headers: Dict[str, str]) -> Optional[Dict[str, Any]]:
    if "/" not in compact_text(repo_full_name):
        return None
    response = github_api_request(
        "get",
        f"https://api.github.com/repos/{repo_full_name}",
        headers=headers,
        timeout=30,
    )
    if response.status_code == 200:
        return response.json()
    if response.status_code != 404:
        response.raise_for_status()
    return None


def find_partial_github_repo_for_lead(
    canonical_key: str,
    business_name: str,
    headers: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    """Recover a repo created before a transient file-upload failure was persisted locally."""
    owner = require_env("GITHUB_OWNER")
    suffix = hashlib.sha1(canonical_key.encode("utf-8")).hexdigest()[:8]
    prefix = f"ai-site-{slugify(business_name, 34)}-"
    pattern = re.compile(rf"^{re.escape(prefix)}\d{{14}}-{re.escape(suffix)}(?:-[a-f0-9]{{4}})?$")
    response = github_api_request(
        "get",
        "https://api.github.com/user/repos",
        headers=headers,
        params={"affiliation": "owner", "sort": "created", "direction": "desc", "per_page": 100},
        timeout=30,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    matches = [
        repo
        for repo in (response.json() or [])
        if compact_text(repo.get("owner", {}).get("login"), owner).casefold() == owner.casefold()
        and pattern.fullmatch(compact_text(repo.get("name")))
    ]
    return matches[0] if matches else None


def get_github_content_sha(owner: str, repo: str, path: str, branch: str, headers: Dict[str, str]) -> Optional[str]:
    response = github_api_request(
        "get",
        f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
        headers=headers,
        params={"ref": branch},
        timeout=30,
    )
    if response.status_code == 200:
        return response.json().get("sha")
    if response.status_code != 404:
        response.raise_for_status()
    return None


def put_github_file(
    owner: str,
    repo: str,
    branch: str,
    path: str,
    content: str,
    message: str,
    headers: Dict[str, str],
) -> Dict[str, Any]:
    existing_sha = get_github_content_sha(owner, repo, path, branch, headers)

    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if existing_sha:
        payload["sha"] = existing_sha

    update_response = github_api_request(
        "put",
        f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
        headers=headers,
        json=payload,
        timeout=45,
    )
    update_response.raise_for_status()
    data = update_response.json()
    content_data = data.get("content", {}) or {}
    commit = data.get("commit", {}) or {}
    return {
        "path": path,
        "contentSha": content_data.get("sha"),
        "htmlUrl": content_data.get("html_url"),
        "commitSha": commit.get("sha"),
        "action": "UPDATED" if existing_sha else "CREATED",
    }


def create_github_repo(headers: Dict[str, str], repo_name: str, business_name: str) -> Dict[str, Any]:
    private_repo = os.getenv("GITHUB_REPO_PRIVATE", "false").lower() == "true"
    owner = require_env("GITHUB_OWNER")
    for attempt in range(3):
        candidate = repo_name if attempt == 0 else f"{repo_name}-{str(uuid4())[:4]}"
        response = github_api_request(
            "post",
            "https://api.github.com/user/repos",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "name": candidate,
                "description": f"AI Site Factory landing page for {business_name}",
                "private": private_repo,
                "auto_init": True,
                "has_issues": False,
                "has_projects": False,
                "has_wiki": False,
            },
            timeout=45,
        )
        if response.status_code == 422:
            recovered = get_remote_github_repo(f"{owner}/{candidate}", headers)
            if recovered:
                return recovered
            if attempt < 2:
                continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError("GitHub repository creation failed after retries.")


def save_github_export_record(canonical_key: str, export: Dict[str, Any], status: str, error: Optional[str] = None) -> None:
    timestamp = now_iso()
    existing = latest_github_repo_for_lead(canonical_key)
    if existing:
        export.setdefault("repoId", existing["repo_id"])
        export.setdefault("repoName", existing["repo_name"])
        export.setdefault("repository", existing["repo_full_name"])
        export.setdefault("repoUrl", existing["repo_url"])
        export.setdefault("branch", existing["default_branch"] or "main")
        export.setdefault("createdAt", existing["created_at"])
    else:
        owner = os.getenv("GITHUB_OWNER", "unknown")
        repo_name = export.get("repoName") or f"ai-site-{canonical_key[:8]}"
        export.setdefault("repoName", repo_name)
        export.setdefault("repository", f"{owner}/{repo_name}")
        export.setdefault("repoUrl", f"https://github.com/{owner}/{repo_name}")
    with get_pipeline_db() as db:
        db.execute(
            """
            INSERT INTO github_site_repos (
                canonical_lead_key, repo_id, repo_name, repo_full_name, repo_url,
                default_branch, private, index_content_sha, readme_content_sha,
                commit_sha, html_checksum, export_status, export_error, pipeline_id,
                approval_id, created_at, updated_at, exported_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_lead_key) DO UPDATE SET
                repo_id = excluded.repo_id,
                repo_name = excluded.repo_name,
                repo_full_name = excluded.repo_full_name,
                repo_url = excluded.repo_url,
                default_branch = excluded.default_branch,
                private = excluded.private,
                index_content_sha = excluded.index_content_sha,
                readme_content_sha = excluded.readme_content_sha,
                commit_sha = excluded.commit_sha,
                html_checksum = excluded.html_checksum,
                export_status = excluded.export_status,
                export_error = excluded.export_error,
                pipeline_id = excluded.pipeline_id,
                approval_id = excluded.approval_id,
                updated_at = excluded.updated_at,
                exported_at = excluded.exported_at
            """,
            (
                canonical_key,
                export.get("repoId"),
                export.get("repoName"),
                export.get("repository"),
                export.get("repoUrl"),
                export.get("branch", "main"),
                1 if export.get("private") else 0,
                export.get("indexContentSha"),
                export.get("readmeContentSha"),
                export.get("commitSha"),
                export.get("htmlChecksum"),
                status,
                sanitize_message(error) if error else None,
                export.get("pipelineId"),
                export.get("approvalId"),
                export.get("createdAt") or timestamp,
                timestamp,
                export.get("exportedAt"),
            ),
        )


def export_site_to_github(
    canonical_key: str,
    business_name: str,
    site_html: str,
    pipeline_id: Optional[str] = None,
    approval_id: Optional[str] = None,
) -> Dict[str, Any]:
    owner = require_env("GITHUB_OWNER")
    headers = github_headers()
    checksum = html_checksum(site_html)
    existing = latest_github_repo_for_lead(canonical_key)
    recovered_repo: Optional[Dict[str, Any]] = None

    if not existing:
        partial = latest_github_repo_record_for_lead(canonical_key)
        if partial and partial["repo_full_name"] and partial["repo_id"]:
            recovered_repo = get_remote_github_repo(partial["repo_full_name"], headers)
        if not recovered_repo:
            recovered_repo = find_partial_github_repo_for_lead(canonical_key, business_name, headers)

    if existing:
        repo_name = existing["repo_name"]
        repo_full_name = existing["repo_full_name"]
        repo_url = existing["repo_url"]
        branch = existing["default_branch"] or "main"
        repo_id = existing["repo_id"]
        private_repo = bool(existing["private"])
        export_action = "UPDATED"
    elif recovered_repo:
        repo_name = recovered_repo.get("name")
        repo_full_name = recovered_repo.get("full_name") or f"{owner}/{repo_name}"
        repo_url = recovered_repo.get("html_url") or f"https://github.com/{repo_full_name}"
        branch = recovered_repo.get("default_branch") or "main"
        repo_id = recovered_repo.get("id")
        private_repo = bool(recovered_repo.get("private"))
        export_action = "RECOVERED"
        log_event(
            "info",
            "provider.github.repo_recovered",
            "Recovered a partially created generated-site repository.",
            repository=repo_full_name,
            businessName=business_name,
        )
    else:
        repo_name = github_repo_name(canonical_key, business_name)
        log_event(
            "info",
            "provider.github.repo_create_start",
            "Creating generated site repository.",
            repository=repo_name,
            businessName=business_name,
        )
        repo_data = create_github_repo(headers, repo_name, business_name)
        repo_name = repo_data.get("name") or repo_name
        repo_full_name = repo_data.get("full_name") or f"{owner}/{repo_name}"
        repo_url = repo_data.get("html_url") or f"https://github.com/{repo_full_name}"
        branch = repo_data.get("default_branch") or "main"
        repo_id = repo_data.get("id")
        private_repo = bool(repo_data.get("private"))
        export_action = "CREATED"

    repo_owner = repo_full_name.split("/", 1)[0] if "/" in repo_full_name else owner
    partial_export = {
        "exportAction": export_action,
        "repository": repo_full_name,
        "repoName": repo_name,
        "repoId": repo_id,
        "repoUrl": repo_url,
        "private": private_repo,
        "branch": branch,
        "path": "index.html",
        "htmlChecksum": checksum,
        "pipelineId": pipeline_id,
        "approvalId": approval_id,
        "createdAt": existing["created_at"] if existing else compact_text((recovered_repo or {}).get("created_at")) or now_iso(),
    }
    if not existing:
        save_github_export_record(canonical_key, dict(partial_export), "EXPORTING")

    log_event(
        "info",
        "provider.github.export_start",
        "Exporting generated site files to GitHub repository.",
        repository=repo_full_name,
        branch=branch,
    )

    try:
        readme_result = put_github_file(
            repo_owner,
            repo_name,
            branch,
            "README.md",
            github_readme(canonical_key, business_name, checksum),
            f"Update generated site README for {business_name}",
            headers,
        )
        index_result = put_github_file(
            repo_owner,
            repo_name,
            branch,
            "index.html",
            site_html,
            f"Publish generated landing page for {business_name}",
            headers,
        )
    except Exception as error:
        save_github_export_record(canonical_key, dict(partial_export), "EXPORT_FAILED", str(error))
        raise

    result = {
        **partial_export,
        "indexContentSha": index_result.get("contentSha"),
        "readmeContentSha": readme_result.get("contentSha"),
        "commitSha": index_result.get("commitSha"),
        "htmlUrl": index_result.get("htmlUrl"),
        "readmeUrl": readme_result.get("htmlUrl"),
        "exportedAt": now_iso(),
    }
    save_github_export_record(canonical_key, result, "EXPORTED")
    log_event(
        "info",
        "provider.github.export_finish",
        "Generated site files exported to GitHub.",
        repository=result["repository"],
        branch=branch,
        path="index.html",
        exportAction=result["exportAction"],
    )
    return result


def deployment_mode_label(publish_mode: Optional[str]) -> str:
    if publish_mode == "direct-netlify":
        return "Direct Netlify"
    if publish_mode == "direct-netlify-fallback":
        return "Direct Netlify fallback"
    if publish_mode == "failed":
        return "Failed"
    return "GitHub \u2192 Netlify"


def zip_site_html(site_html: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("index.html", site_html)
        archive.writestr("_headers", "/*\n  Content-Type: text/html; charset=utf-8\n")
    return buffer.getvalue()


def resolve_netlify_github_installation(repo_full_name: str) -> Tuple[Optional[int], str]:
    repo_owner = compact_text(repo_full_name.split("/", 1)[0]).lower()
    configured = compact_text(os.getenv("NETLIFY_GITHUB_INSTALLATION_ID"))
    if configured:
        try:
            installation_id = int(configured)
        except ValueError as error:
            raise RuntimeError(
                "NETLIFY_GITHUB_INSTALLATION_ID must be a positive numeric GitHub App installation id."
            ) from error
        if installation_id <= 0:
            raise RuntimeError(
                "NETLIFY_GITHUB_INSTALLATION_ID must be a positive numeric GitHub App installation id."
            )
        return installation_id, "environment"

    installation_id = NETLIFY_GITHUB_INSTALLATION_DEFAULTS.get(repo_owner)
    if installation_id is not None:
        return installation_id, "owner_default"
    return None, "unmatched_owner"


def netlify_site_repo_link_matches(
    site: Dict[str, Any],
    repo_full_name: str,
    branch: str,
    installation_id: Optional[int],
) -> bool:
    candidates = [site.get("repo"), site.get("build_settings")]
    if site.get("provider") or site.get("repo_path"):
        candidates.append(site)
    expected_repo = compact_text(repo_full_name).lower()
    expected_branch = compact_text(branch).lower()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        provider = compact_text(candidate.get("provider")).lower()
        repo_path = compact_text(candidate.get("repo_path") or candidate.get("repo")).lower()
        repo_branch = compact_text(candidate.get("repo_branch") or candidate.get("branch")).lower()
        linked_installation = compact_text(candidate.get("installation_id"))
        if provider != "github" or repo_path != expected_repo:
            continue
        if repo_branch and repo_branch != expected_branch:
            continue
        if installation_id is not None and linked_installation != str(installation_id):
            continue
        return True
    return False


def netlify_current_build_from_list(
    builds: Any,
    commit_sha: str,
    previous_build_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not isinstance(builds, list):
        return None
    expected_sha = compact_text(commit_sha).lower()
    exact = next(
        (
            build
            for build in builds
            if isinstance(build, dict)
            and compact_text(build.get("id"))
            and compact_text(build.get("id")) != compact_text(previous_build_id)
            and compact_text(build.get("sha")).lower() == expected_sha
        ),
        None,
    )
    return exact


def enable_netlify_site(site_id: str, headers: Dict[str, str]) -> None:
    response = requests.put(
        f"https://api.netlify.com/api/v1/sites/{site_id}/enable",
        headers=headers,
        timeout=30,
    )
    if response.status_code not in {200, 204}:
        response.raise_for_status()


def netlify_site_is_disabled(site: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(site, dict):
        return False
    state = compact_text(site.get("state")).lower()
    return bool(site.get("disabled")) or state in {"disabled", "inactive"}


def netlify_site_matches_live_url(site: Dict[str, Any], live_url: str) -> bool:
    target_host = urlparse(compact_text(live_url)).netloc.lower().split(":", 1)[0]
    if not target_host:
        target_host = compact_text(live_url).lower().split("/", 1)[0].split(":", 1)[0]
    if not target_host:
        return False
    candidate_hosts = {
        urlparse(compact_text(site.get("ssl_url"))).netloc.lower().split(":", 1)[0],
        urlparse(compact_text(site.get("url"))).netloc.lower().split(":", 1)[0],
        compact_text(site.get("custom_domain")).lower().split(":", 1)[0],
        f"{compact_text(site.get('name')).lower()}.netlify.app",
    }
    candidate_hosts.update(
        compact_text(domain).lower().split(":", 1)[0]
        for domain in (site.get("domain_aliases") or [])
    )
    return target_host in {host for host in candidate_hosts if host}


def find_netlify_site_by_live_url(live_url: str, headers: Dict[str, str]) -> Optional[Dict[str, Any]]:
    if not compact_text(live_url):
        return None
    per_page = 100
    for page in range(1, 101):
        response = requests.get(
            "https://api.netlify.com/api/v1/sites",
            headers=headers,
            params={"per_page": per_page, "page": page},
            timeout=45,
        )
        response.raise_for_status()
        sites = response.json()
        if not isinstance(sites, list):
            return None
        matching = next(
            (site for site in sites if isinstance(site, dict) and netlify_site_matches_live_url(site, live_url)),
            None,
        )
        if matching:
            return matching
        if len(sites) < per_page:
            break
    return None


def cancel_netlify_site_for_lead(canonical_key: str, live_url: Optional[str] = None) -> Dict[str, Any]:
    token = require_env("NETLIFY_AUTH_TOKEN")
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "AI-Site-Factory",
    }
    with get_pipeline_db() as db:
        site = db.execute(
            "SELECT * FROM site_registry WHERE canonical_lead_key = ?",
            (canonical_key,),
        ).fetchone()

    recovered_from_url = False
    if not site and compact_text(live_url):
        recovered_site = find_netlify_site_by_live_url(compact_text(live_url), headers)
        if recovered_site and compact_text(recovered_site.get("id")):
            timestamp = now_iso()
            recovered_url = (
                compact_text(recovered_site.get("ssl_url"))
                or compact_text(recovered_site.get("url"))
                or compact_text(live_url)
            )
            with get_pipeline_db() as db:
                db.execute(
                    """
                    INSERT INTO site_registry (
                        canonical_lead_key, site_id, site_name, url, admin_url,
                        created_at, updated_at, last_deploy_state, deployment_count, publish_mode
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', 1, 'recovered-netlify')
                    ON CONFLICT(canonical_lead_key) DO UPDATE SET
                        site_id = excluded.site_id,
                        site_name = excluded.site_name,
                        url = excluded.url,
                        admin_url = excluded.admin_url,
                        updated_at = excluded.updated_at
                    """,
                    (
                        canonical_key,
                        compact_text(recovered_site.get("id")),
                        compact_text(recovered_site.get("name")),
                        recovered_url,
                        compact_text(recovered_site.get("admin_url")) or None,
                        timestamp,
                        timestamp,
                    ),
                )
                site = db.execute(
                    "SELECT * FROM site_registry WHERE canonical_lead_key = ?",
                    (canonical_key,),
                ).fetchone()
            recovered_from_url = True

    if not site:
        return {
            "status": "NO_SITE",
            "siteId": None,
            "previousUrl": compact_text(live_url) or None,
            "recoveredFromLiveUrl": False,
        }
    if compact_text(site["last_deploy_state"]).lower() in {"cancelled", "disabled"}:
        return {
            "status": "ALREADY_CANCELLED",
            "siteId": site["site_id"],
            "siteName": site["site_name"],
            "previousUrl": site["url"],
        }

    response = requests.put(
        f"https://api.netlify.com/api/v1/sites/{site['site_id']}/disable",
        headers=headers,
        params={"reason": "AI Site Factory deployment checkbox was unchecked in Zendesk."},
        timeout=30,
    )
    if response.status_code not in {200, 204, 404}:
        response.raise_for_status()

    timestamp = now_iso()
    history = latest_deployment_history_for_lead(canonical_key)
    with get_pipeline_db() as db:
        db.execute(
            """
            UPDATE site_registry
            SET url = NULL, last_deploy_state = 'cancelled', updated_at = ?
            WHERE canonical_lead_key = ?
            """,
            (timestamp, canonical_key),
        )
        if history:
            raw = safe_json_loads(history["raw_json"], {})
            if isinstance(raw, dict):
                raw.update({"state": "cancelled", "cancelledAt": timestamp})
            db.execute(
                """
                UPDATE deployment_history
                SET state = 'cancelled', approval_status = 'CANCELLED', raw_json = ?
                WHERE id = ?
                """,
                (json.dumps(raw, default=str), history["id"]),
            )

    return {
        "status": "CANCELLED" if response.status_code != 404 else "ALREADY_REMOVED",
        "siteId": site["site_id"],
        "siteName": site["site_name"],
        "previousUrl": site["url"],
        "cancelledAt": timestamp,
        "recoveredFromLiveUrl": recovered_from_url,
    }


def get_github_text_file(
    repo_full_name: str,
    path: str = "index.html",
    branch: str = "main",
) -> Dict[str, Any]:
    response = github_api_request(
        "get",
        f"https://api.github.com/repos/{repo_full_name}/contents/{path}",
        headers=github_headers(),
        params={"ref": branch},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    try:
        content = base64.b64decode(compact_text(payload.get("content")).replace("\n", "")).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError) as error:
        raise RuntimeError(f"GitHub {path} content could not be decoded.") from error
    if not content.strip():
        raise RuntimeError(f"GitHub {path} content is empty.")
    return {
        "content": content,
        "sha": payload.get("sha"),
        "htmlUrl": payload.get("html_url"),
    }


def deploy_github_repo_to_netlify_for_lead(
    canonical_key: str,
    business_name: str,
    pipeline_id: Optional[str],
    approval_id: Optional[str],
    approved_by: Optional[str],
    github_export: Dict[str, Any],
    regenerate_existing_site: bool = False,
) -> Dict[str, Any]:
    token = require_env("NETLIFY_AUTH_TOKEN")
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "AI-Site-Factory",
    }
    repo_full_name = compact_text(github_export.get("repository"))
    repo_url = compact_text(github_export.get("repoUrl")) or f"https://github.com/{repo_full_name}"
    branch = compact_text(github_export.get("branch"), "main")
    commit_sha = compact_text(github_export.get("commitSha"))
    checksum = compact_text(github_export.get("htmlChecksum"))
    if not repo_full_name or not commit_sha:
        raise RuntimeError("GitHub export metadata is incomplete; cannot deploy from Git.")

    with get_pipeline_db() as db:
        existing_site = db.execute(
            "SELECT * FROM site_registry WHERE canonical_lead_key = ?",
            (canonical_key,),
        ).fetchone()

    if (
        existing_site
        and not regenerate_existing_site
        and compact_text(existing_site["publish_mode"]) == "github-netlify"
        and compact_text(existing_site["github_repo_full_name"]).lower() == repo_full_name.lower()
        and existing_site["last_commit_sha"] == commit_sha
        and existing_site["last_deploy_state"] in {"ready", "building", "enqueued"}
    ):
        return {
            "deployAction": "REUSED",
            "siteCreated": False,
            "siteReused": True,
            "siteId": existing_site["site_id"],
            "siteName": existing_site["site_name"],
            "buildId": existing_site["last_build_id"],
            "deployId": existing_site["last_deploy_id"],
            "state": existing_site["last_deploy_state"] or "ready",
            "url": existing_site["url"],
            "adminUrl": existing_site["admin_url"],
            "mode": "production",
            "htmlChecksum": checksum,
            "deploymentHistoryId": None,
            "publishMode": "github-netlify",
            "deploymentMode": deployment_mode_label("github-netlify"),
            "githubExport": github_export,
            "githubRepoUrl": repo_url,
            "githubRepoFullName": repo_full_name,
            "commitSha": commit_sha,
        }

    netlify_installation_id, installation_source = resolve_netlify_github_installation(repo_full_name)
    log_event(
        "info",
        "provider.netlify.github_installation_selected",
        "Selected GitHub App installation for Netlify repository linkage.",
        repository=repo_full_name,
        installationId=netlify_installation_id,
        source=installation_source,
    )

    repo_settings = {
        "provider": "github",
        "repo_path": repo_full_name,
        "repo_branch": branch,
        "dir": "",
        "cmd": "",
        "public_repo": not bool(github_export.get("private")),
    }
    if netlify_installation_id is not None:
        repo_settings["installation_id"] = netlify_installation_id
    site_created = False
    site_reused = bool(existing_site)
    deploy_action = "REDEPLOYED" if existing_site else "CREATED"

    if existing_site:
        site_id = existing_site["site_id"]
        site_name = existing_site["site_name"]
        if compact_text(existing_site["last_deploy_state"]).lower() in {"cancelled", "disabled"}:
            enable_netlify_site(site_id, headers)
        site_response = requests.patch(
            f"https://api.netlify.com/api/v1/sites/{site_id}",
            headers={**headers, "Content-Type": "application/json"},
            json={"repo": repo_settings},
            timeout=45,
        )
        try:
            site_response.raise_for_status()
        except requests.HTTPError as error:
            raise RuntimeError(
                f"Netlify Git-linked site update failed ({site_response.status_code}): "
                f"{sanitize_message(site_response.text) or sanitize_message(str(error))}"
            ) from error
        site = site_response.json()
    else:
        site_name = f"ai-site-{slugify(business_name, 32)}-{canonical_key[:8]}"
        log_event("info", "provider.netlify.git_site_start", "Creating Netlify Git-linked site.", siteName=site_name, repository=repo_full_name)
        site_response = requests.post(
            "https://api.netlify.com/api/v1/sites",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "name": site_name,
                "processing_settings": {"html": {"pretty_urls": True}},
                "repo": repo_settings,
            },
            timeout=45,
        )
        try:
            site_response.raise_for_status()
        except requests.HTTPError as error:
            raise RuntimeError(
                f"Netlify Git-linked site creation failed ({site_response.status_code}): "
                f"{sanitize_message(site_response.text) or sanitize_message(str(error))}"
            ) from error
        site = site_response.json()
        site_id = site.get("id") or site.get("name")
        site_name = site.get("name") or site_name
        site_created = True
        if not site_id:
            raise RuntimeError("Netlify did not return a site id.")

    site_readback: Optional[Dict[str, Any]] = None

    def read_site() -> Dict[str, Any]:
        response = requests.get(
            f"https://api.netlify.com/api/v1/sites/{site_id}",
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    if not netlify_site_repo_link_matches(site, repo_full_name, branch, netlify_installation_id):
        site_readback = read_site()
        if not netlify_site_repo_link_matches(
            site_readback, repo_full_name, branch, netlify_installation_id
        ):
            raise RuntimeError(
                "Netlify did not confirm the requested GitHub repository and App installation linkage."
            )

    previous_build_id = existing_site["last_build_id"] if existing_site else None

    def list_site_builds() -> List[Dict[str, Any]]:
        response = requests.get(
            f"https://api.netlify.com/api/v1/sites/{site_id}/builds",
            headers=headers,
            params={"per_page": 10},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []

    build = netlify_current_build_from_list(
        list_site_builds(), commit_sha, previous_build_id
    )

    try:
        auto_build_wait = max(
            0.0,
            min(float(os.getenv("NETLIFY_AUTOBUILD_WAIT_SECONDS", "5")), 10.0),
        )
    except ValueError:
        auto_build_wait = 5.0
    auto_build_deadline = time.time() + auto_build_wait
    while build is None and time.time() < auto_build_deadline:
        time.sleep(min(0.5, max(0.0, auto_build_deadline - time.time())))
        build = netlify_current_build_from_list(
            list_site_builds(), commit_sha, previous_build_id
        )

    if build is None:
        log_event("info", "provider.netlify.git_build_start", "Triggering Netlify build from GitHub repository.", siteName=site_name, repository=repo_full_name, branch=branch)
        build_response = requests.post(
            f"https://api.netlify.com/api/v1/sites/{site_id}/builds",
            headers={**headers, "Content-Type": "application/json"},
            params={"branch": branch},
            json={},
            timeout=45,
        )
        build_response.raise_for_status()
        build = build_response.json()
    else:
        log_event(
            "info",
            "provider.netlify.git_auto_build_reused",
            "Reusing the build Netlify created while linking the GitHub repository.",
            siteName=site_name,
            repository=repo_full_name,
            buildId=build.get("id"),
        )

    site_details = {**site, **(site_readback or {})}
    build_id = build.get("id")
    deploy_id = build.get("deploy_id")
    build_error = build.get("error")
    state = "building"

    poll_until = time.time() + int(os.getenv("NETLIFY_DEPLOY_POLL_SECONDS", "45"))
    while build_id and not build.get("done") and time.time() < poll_until:
        build_poll = requests.get(
            f"https://api.netlify.com/api/v1/builds/{build_id}",
            headers=headers,
            timeout=30,
        )
        build_poll.raise_for_status()
        build = build_poll.json()
        deploy_id = build.get("deploy_id") or deploy_id
        build_error = build.get("error") or build_error
        if not build.get("done") and time.time() < poll_until:
            time.sleep(2)

    if build_error:
        raise RuntimeError(f"Netlify build failed: {build_error}")

    deploy = {}
    if deploy_id:
        state = "enqueued"
        while state not in {"ready", "error"} and time.time() < poll_until:
            deploy_poll = requests.get(
                f"https://api.netlify.com/api/v1/deploys/{deploy_id}",
                headers=headers,
                timeout=30,
            )
            deploy_poll.raise_for_status()
            deploy = deploy_poll.json()
            state = deploy.get("state") or state
            if state not in {"ready", "error"} and time.time() < poll_until:
                time.sleep(2)
        if state == "error":
            raise RuntimeError(deploy.get("error_message") or "Netlify deploy failed.")

    site_url = (
        deploy.get("ssl_url")
        or site_details.get("ssl_url")
        or site_details.get("url")
        or (existing_site["url"] if existing_site else None)
        or f"https://{site_name}.netlify.app"
    )
    admin_url = site_details.get("admin_url") or (existing_site["admin_url"] if existing_site else None)
    deployed_at = now_iso()
    deployment_history_id = str(uuid4())

    result = {
        "deployAction": deploy_action,
        "siteCreated": site_created,
        "siteReused": site_reused,
        "siteId": site_id,
        "siteName": site_name,
        "buildId": build_id,
        "deployId": deploy_id,
        "state": state or "unknown",
        "url": site_url,
        "adminUrl": admin_url,
        "deployedAt": deployed_at,
        "mode": "production",
        "htmlChecksum": checksum,
        "deploymentHistoryId": deployment_history_id,
        "publishMode": "github-netlify",
        "deploymentMode": deployment_mode_label("github-netlify"),
        "githubExport": github_export,
        "githubRepoUrl": repo_url,
        "githubRepoFullName": repo_full_name,
        "commitSha": commit_sha,
    }

    with get_pipeline_db() as db:
        db.execute(
            """
            INSERT INTO site_registry (
                canonical_lead_key, site_id, site_name, url, admin_url,
                github_repo_full_name, github_repo_url, last_commit_sha, last_build_id,
                created_at, updated_at, last_deploy_id, last_deploy_state, deployment_count, publish_mode
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'github-netlify')
            ON CONFLICT(canonical_lead_key) DO UPDATE SET
                site_id = excluded.site_id,
                site_name = excluded.site_name,
                url = excluded.url,
                admin_url = COALESCE(excluded.admin_url, site_registry.admin_url),
                github_repo_full_name = excluded.github_repo_full_name,
                github_repo_url = excluded.github_repo_url,
                last_commit_sha = excluded.last_commit_sha,
                last_build_id = excluded.last_build_id,
                updated_at = excluded.updated_at,
                last_deploy_id = excluded.last_deploy_id,
                last_deploy_state = excluded.last_deploy_state,
                publish_mode = excluded.publish_mode,
                deployment_count = site_registry.deployment_count + 1
            """,
            (
                canonical_key,
                site_id,
                site_name,
                site_url,
                admin_url,
                repo_full_name,
                repo_url,
                commit_sha,
                build_id,
                deployed_at,
                deployed_at,
                deploy_id,
                state or "unknown",
            ),
        )
        db.execute(
            """
            INSERT INTO deployment_history (
                id, canonical_lead_key, pipeline_id, approval_id, site_id, site_name,
                deploy_id, build_id, url, deploy_action, state, html_checksum, deployed_at,
                approved_by, approval_status, github_repo_full_name, github_repo_url,
                commit_sha, publish_mode, github_export_json, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                deployment_history_id,
                canonical_key,
                pipeline_id,
                approval_id,
                site_id,
                site_name,
                deploy_id,
                build_id,
                site_url,
                deploy_action,
                state or "unknown",
                checksum,
                deployed_at,
                approved_by,
                "APPROVED",
                repo_full_name,
                repo_url,
                commit_sha,
                "github-netlify",
                json.dumps(github_export, default=str),
                json.dumps(result, default=str),
            ),
        )

    log_event("info", "provider.netlify.git_deploy_finish", "Netlify Git deployment recorded.", siteName=site_name, state=result["state"], url=result["url"], repository=repo_full_name)
    return result



def deploy_direct_netlify_for_lead(
    canonical_key: str,
    business_name: str,
    site_html: str,
    pipeline_id: Optional[str],
    approval_id: Optional[str],
    approved_by: Optional[str],
    github_export: Dict[str, Any],
) -> Dict[str, Any]:
    """Deploy generated HTML directly to Netlify while retaining GitHub as the source archive."""
    result = deploy_direct_netlify_fallback_for_lead(
        canonical_key=canonical_key,
        business_name=business_name,
        site_html=site_html,
        pipeline_id=pipeline_id,
        approval_id=approval_id,
        approved_by=approved_by,
        github_export=github_export,
        git_error=RuntimeError("Direct Netlify deployment selected by pipeline configuration."),
    )
    result["publishMode"] = "direct-netlify"
    result["deploymentMode"] = "Direct Netlify"
    result.pop("fallbackReason", None)

    deployment_history_id = result.get("deploymentHistoryId")
    if deployment_history_id:
        with get_pipeline_db() as db:
            db.execute(
                "UPDATE deployment_history SET publish_mode = ?, raw_json = ? WHERE id = ?",
                ("direct-netlify", json.dumps(result, default=str), deployment_history_id),
            )
            db.execute(
                "UPDATE site_registry SET publish_mode = ?, updated_at = ? WHERE canonical_lead_key = ?",
                ("direct-netlify", now_iso(), canonical_key),
            )

    log_event(
        "info",
        "provider.netlify.direct_deploy_finish",
        "Direct Netlify deployment recorded.",
        siteName=result.get("siteName"),
        state=result.get("state"),
        url=result.get("url"),
    )
    return result

def deploy_direct_netlify_fallback_for_lead(
    canonical_key: str,
    business_name: str,
    site_html: str,
    pipeline_id: Optional[str],
    approval_id: Optional[str],
    approved_by: Optional[str],
    github_export: Dict[str, Any],
    git_error: Exception,
) -> Dict[str, Any]:
    token = require_env("NETLIFY_AUTH_TOKEN")
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "AI-Site-Factory",
    }
    repo_full_name = compact_text(github_export.get("repository"))
    repo_url = compact_text(github_export.get("repoUrl")) or (f"https://github.com/{repo_full_name}" if repo_full_name else "")
    commit_sha = compact_text(github_export.get("commitSha"))
    checksum = html_checksum(site_html)
    fallback_reason = sanitize_message(git_error)

    with get_pipeline_db() as db:
        existing_site = db.execute(
            "SELECT * FROM site_registry WHERE canonical_lead_key = ?",
            (canonical_key,),
        ).fetchone()

    verified_existing_site: Optional[Dict[str, Any]] = None
    account_migration = False
    if existing_site:
        verify_response = requests.get(
            f"https://api.netlify.com/api/v1/sites/{existing_site['site_id']}",
            headers=headers,
            timeout=30,
        )
        if verify_response.status_code == 200:
            verified_existing_site = verify_response.json()
            # Render can restart with a restored/partial SQLite registry while the
            # Netlify project remains disabled. Trust Netlify's current state as
            # well as our local state before publishing another deploy.
            if (
                compact_text(existing_site["last_deploy_state"]).lower() in {"cancelled", "disabled"}
                or netlify_site_is_disabled(verified_existing_site)
            ):
                enable_netlify_site(existing_site["site_id"], headers)
        elif verify_response.status_code in {403, 404}:
            account_migration = True
            log_event(
                "warning",
                "provider.netlify.site_account_changed",
                "Stored Netlify site is not available to the current token; a new site will be created.",
                siteId=existing_site["site_id"],
                siteName=existing_site["site_name"],
                statusCode=verify_response.status_code,
            )
            existing_site = None
        else:
            verify_response.raise_for_status()

    site_created = False
    site_reused = bool(existing_site)
    deploy_action = (
        "DIRECT_FALLBACK_REDEPLOYED"
        if existing_site
        else "DIRECT_FALLBACK_ACCOUNT_MIGRATED"
        if account_migration
        else "DIRECT_FALLBACK_CREATED"
    )

    if existing_site:
        site_id = existing_site["site_id"]
        site_name = verified_existing_site.get("name") or existing_site["site_name"]
        site = {
            "id": site_id,
            "name": site_name,
            "ssl_url": verified_existing_site.get("ssl_url") or existing_site["url"],
            "url": verified_existing_site.get("url") or existing_site["url"],
            "admin_url": verified_existing_site.get("admin_url") or existing_site["admin_url"],
        }
    else:
        site_name = f"ai-site-{slugify(business_name, 32)}-{canonical_key[:8]}"
        log_event(
            "warning",
            "provider.netlify.direct_fallback_site_start",
            "Creating direct Netlify fallback site after Git-linked deployment failed.",
            siteName=site_name,
            repository=repo_full_name,
            reason=fallback_reason,
        )
        create_response = requests.post(
            "https://api.netlify.com/api/v1/sites",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "name": site_name,
                "processing_settings": {"html": {"pretty_urls": True}},
            },
            timeout=45,
        )
        if create_response.status_code == 422:
            # A previous Git-linked attempt may have created the site but failed before
            # our database recorded it. Reuse that Netlify site instead of treating the
            # duplicate name as a second deployment failure.
            sites_response = requests.get(
                "https://api.netlify.com/api/v1/sites",
                headers=headers,
                params={"per_page": 100},
                timeout=45,
            )
            sites_response.raise_for_status()
            matching_site = next(
                (item for item in sites_response.json() if item.get("name") == site_name),
                None,
            )
            if not matching_site:
                account_suffix = hashlib.sha256(token.encode("utf-8")).hexdigest()[:6]
                site_name = f"{site_name[:56]}-{account_suffix}"
                create_response = requests.post(
                    "https://api.netlify.com/api/v1/sites",
                    headers={**headers, "Content-Type": "application/json"},
                    json={
                        "name": site_name,
                        "processing_settings": {"html": {"pretty_urls": True}},
                    },
                    timeout=45,
                )
                create_response.raise_for_status()
                site = create_response.json()
                site_created = True
            else:
                site = matching_site
                site_reused = True
                deploy_action = "DIRECT_FALLBACK_REDEPLOYED"
            site_id = site.get("id") or site.get("name")
            site_name = site.get("name") or site_name
            if site_reused and netlify_site_is_disabled(site):
                enable_netlify_site(site_id, headers)
        else:
            create_response.raise_for_status()
            site = create_response.json()
            site_id = site.get("id") or site.get("name")
            site_name = site.get("name") or site_name
            site_created = True
        if not site_id:
            raise RuntimeError("Netlify did not return a site id for direct deployment.")

    log_event(
        "warning",
        "provider.netlify.direct_fallback_deploy_start",
        "Uploading direct Netlify fallback deploy after Git-linked deployment failed.",
        siteName=site_name,
        repository=repo_full_name,
        reason=fallback_reason,
    )
    deploy_response = requests.post(
        f"https://api.netlify.com/api/v1/sites/{site_id}/deploys",
        headers={**headers, "Content-Type": "application/zip"},
        data=zip_site_html(site_html),
        timeout=90,
    )
    deploy_response.raise_for_status()
    deploy = deploy_response.json()
    deploy_id = deploy.get("id")
    state = deploy.get("state") or "uploaded"
    poll_until = time.time() + int(os.getenv("NETLIFY_DEPLOY_POLL_SECONDS", "45"))

    while deploy_id and state not in {"ready", "error"} and time.time() < poll_until:
        time.sleep(2)
        deploy_poll = requests.get(
            f"https://api.netlify.com/api/v1/deploys/{deploy_id}",
            headers=headers,
            timeout=30,
        )
        deploy_poll.raise_for_status()
        deploy = deploy_poll.json()
        state = deploy.get("state") or state

    if state == "error":
        raise RuntimeError(deploy.get("error_message") or "Netlify direct fallback deploy failed.")
    if state != "ready":
        raise RuntimeError(
            f"Netlify direct fallback deploy did not become ready before the polling timeout (state: {state})."
        )

    site_url = (
        deploy.get("ssl_url")
        or deploy.get("url")
        or site.get("ssl_url")
        or site.get("url")
        or (existing_site["url"] if existing_site else None)
        or f"https://{site_name}.netlify.app"
    )
    admin_url = site.get("admin_url") or (existing_site["admin_url"] if existing_site else None)
    deployed_at = now_iso()
    deployment_history_id = str(uuid4())

    result = {
        "deployAction": deploy_action,
        "siteCreated": site_created,
        "siteReused": site_reused,
        "siteId": site_id,
        "siteName": site_name,
        "buildId": None,
        "deployId": deploy_id,
        "state": state or "unknown",
        "url": site_url,
        "adminUrl": admin_url,
        "deployedAt": deployed_at,
        "mode": "production",
        "htmlChecksum": checksum,
        "deploymentHistoryId": deployment_history_id,
        "publishMode": "direct-netlify-fallback",
        "deploymentMode": deployment_mode_label("direct-netlify-fallback"),
        "githubExport": github_export,
        "githubRepoUrl": repo_url,
        "githubRepoFullName": repo_full_name,
        "commitSha": commit_sha,
        "fallbackReason": fallback_reason,
        "accountMigration": account_migration,
    }

    with get_pipeline_db() as db:
        db.execute(
            """
            INSERT INTO site_registry (
                canonical_lead_key, site_id, site_name, url, admin_url,
                github_repo_full_name, github_repo_url, last_commit_sha, last_build_id,
                created_at, updated_at, last_deploy_id, last_deploy_state, deployment_count, publish_mode
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'direct-netlify-fallback')
            ON CONFLICT(canonical_lead_key) DO UPDATE SET
                site_id = excluded.site_id,
                site_name = excluded.site_name,
                url = excluded.url,
                admin_url = COALESCE(excluded.admin_url, site_registry.admin_url),
                github_repo_full_name = excluded.github_repo_full_name,
                github_repo_url = excluded.github_repo_url,
                last_commit_sha = excluded.last_commit_sha,
                last_build_id = excluded.last_build_id,
                updated_at = excluded.updated_at,
                last_deploy_id = excluded.last_deploy_id,
                last_deploy_state = excluded.last_deploy_state,
                publish_mode = excluded.publish_mode,
                deployment_count = site_registry.deployment_count + 1
            """,
            (
                canonical_key,
                site_id,
                site_name,
                site_url,
                admin_url,
                repo_full_name,
                repo_url,
                commit_sha,
                None,
                deployed_at,
                deployed_at,
                deploy_id,
                state or "unknown",
            ),
        )
        db.execute(
            """
            INSERT INTO deployment_history (
                id, canonical_lead_key, pipeline_id, approval_id, site_id, site_name,
                deploy_id, build_id, url, deploy_action, state, html_checksum, deployed_at,
                approved_by, approval_status, github_repo_full_name, github_repo_url,
                commit_sha, publish_mode, github_export_json, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                deployment_history_id,
                canonical_key,
                pipeline_id,
                approval_id,
                site_id,
                site_name,
                deploy_id,
                None,
                site_url,
                deploy_action,
                state or "unknown",
                checksum,
                deployed_at,
                approved_by,
                "APPROVED",
                repo_full_name,
                repo_url,
                commit_sha,
                "direct-netlify-fallback",
                json.dumps(github_export, default=str),
                json.dumps(result, default=str),
            ),
        )

    log_event(
        "warning",
        "provider.netlify.direct_fallback_finish",
        "Direct Netlify fallback deployment recorded.",
        siteName=site_name,
        state=result["state"],
        url=result["url"],
        repository=repo_full_name,
    )
    return result


def create_zendesk_outreach_ticket(
    context: Dict[str, Any],
    deployment: Dict[str, Any],
    outreach: Dict[str, Any],
    pipeline_id: str,
) -> Dict[str, Any]:
    zendesk_subdomain = require_env("ZENDESK_SUBDOMAIN")
    zendesk_email = require_env("ZENDESK_EMAIL")
    zendesk_token = require_env("ZENDESK_API_TOKEN")

    auth = (f"{zendesk_email}/token", zendesk_token)
    base_url = f"https://{zendesk_subdomain}.zendesk.com/api/v2"
    headers = {"Content-Type": "application/json"}
    business_name = compact_text(context.get("businessName"), "AI Site Factory Lead")
    lead_email = normalize_email_identity(context.get("email"))
    lead_phone = compact_text(context.get("phone"))
    live_link = compact_text(deployment.get("url"))
    contact_type = outreach.get("contactType") or ("email" if lead_email else "phone" if lead_phone else "unknown")
    contact_tag = "ai_site_email_lead" if contact_type == "email" else "ai_site_phone_lead" if contact_type == "phone" else "ai_site_contact_unknown"
    tags = [
        "ai_site_factory",
        "ai_site_ready",
        "ai_site_no_website",
        "outreach_draft",
        "netlify_production_site",
        contact_tag,
    ]
    log_event(
        "info",
        "provider.zendesk.start",
        "Creating Zendesk private outreach ticket.",
        businessName=business_name,
        pipelineId=pipeline_id,
        contactType=contact_type,
        hasEmail=bool(lead_email),
        hasPhone=bool(lead_phone),
    )

    org_response = requests.post(
        f"{base_url}/organizations.json",
        json={
            "organization": {
                "name": business_name,
                "notes": (
                    f"Created from AI Site Factory pipeline.\n"
                    f"Pipeline ID: {pipeline_id}\n"
                    f"Industry: {context.get('industry')}\n"
                    f"Live link: {live_link}\n"
                    f"Contact type: {contact_type}"
                ),
                "tags": tags,
            }
        },
        auth=auth,
        headers=headers,
        timeout=30,
    )

    organization = {}
    if org_response.status_code == 422:
        search_response = requests.get(
            f"{base_url}/organizations/search.json",
            params={"name": business_name},
            auth=auth,
            headers=headers,
            timeout=30,
        )
        search_response.raise_for_status()
        organizations = search_response.json().get("organizations", [])
        organization = organizations[0] if organizations else {}
    else:
        org_response.raise_for_status()
        organization = org_response.json().get("organization", {})

    organization_id = organization.get("id")
    user: Dict[str, Any] = {}

    if lead_email:
        user_search_response = requests.get(
            f"{base_url}/users/search.json",
            params={"query": lead_email},
            auth=auth,
            headers=headers,
            timeout=30,
        )
        user_search_response.raise_for_status()
        users = user_search_response.json().get("users", [])

        if users:
            user = users[0]
        else:
            user_create_response = requests.post(
                f"{base_url}/users.json",
                json={
                    "user": {
                        "name": business_name,
                        "email": lead_email,
                        "organization_id": organization_id,
                        "role": "end-user",
                        "tags": tags,
                    }
                },
                auth=auth,
                headers=headers,
                timeout=30,
            )
            user_create_response.raise_for_status()
            user = user_create_response.json().get("user", {})

    ticket_payload: Dict[str, Any] = {
        "ticket": {
            "subject": outreach.get("subject") or f"Website preview for {business_name}",
            "comment": {
                "body": (
                    f"AI Site Factory private draft\n\n"
                    f"Pipeline ID: {pipeline_id}\n"
                    f"Business: {business_name}\n"
                    f"Industry: {context.get('industry')}\n"
                    f"Location: {context.get('location')}\n"
                    f"Contact type: {contact_type}\n"
                    f"Email: {lead_email or 'N/A'}\n"
                    f"Phone: {lead_phone or 'N/A'}\n"
                    f"Live link: {live_link}\n\n"
                    f"Draft:\n{outreach.get('body')}"
                ),
                "public": False,
            },
            "organization_id": organization_id,
            "priority": "normal",
            "type": "task",
            "status": "new",
            "tags": tags,
        }
    }
    ticket_payload["ticket"].update(zendesk_ticket_routing_fields(contact_type))
    if user.get("id"):
        ticket_payload["ticket"]["requester_id"] = user.get("id")

    ticket_response = requests.post(
        f"{base_url}/tickets.json",
        json=ticket_payload,
        auth=auth,
        headers=headers,
        timeout=30,
    )
    try:
        ticket_response.raise_for_status()
    except requests.RequestException as error:
        log_event("error", "provider.zendesk.error", "Zendesk ticket creation failed.", businessName=business_name, reason=str(error))
        raise
    ticket = ticket_response.json().get("ticket", {})

    result = {
        "syncStatus": "TICKET_CREATED",
        "ticketId": ticket.get("id"),
        "ticketUrl": f"https://{zendesk_subdomain}.zendesk.com/agent/tickets/{ticket.get('id')}",
        "organizationId": organization_id,
        "userId": user.get("id"),
        "userEmail": user.get("email"),
        "liveLink": live_link,
        "contactType": contact_type,
        "tags": tags,
        "status": ticket.get("status") or "new",
        "syncedAt": datetime.now().isoformat(),
    }
    log_event("info", "provider.zendesk.finish", "Zendesk ticket created.", businessName=business_name, ticketId=result["ticketId"])
    return result


def zendesk_channels_for_context(context: Dict[str, Any]) -> List[str]:
    channels: List[str] = []
    if normalize_email_identity(context.get("email")):
        channels.append("email")
    if compact_text(context.get("phone")):
        channels.append("phone")
    if not channels:
        channels.append("unknown")
    return channels


def zendesk_ticket_field_value_matches(expected: Any, actual: Any) -> bool:
    if isinstance(expected, bool):
        if isinstance(actual, bool):
            return actual is expected
        return compact_text(actual).lower() in ({"true", "1", "yes", "on"} if expected else {"false", "0", "no", "off"})
    return compact_text(actual) == compact_text(expected)


def ensure_zendesk_intake_requester(
    business_name: str,
    canonical_key: str,
    lead_email: Optional[str],
    lead_phone: Optional[str],
    organization_id: Optional[int],
    base_url: str,
    auth: Tuple[str, str],
    headers: Dict[str, str],
) -> Dict[str, Any]:
    """Create or reuse the business end user used as requester on new intake tickets."""
    requester_external_id = f"asf-requester-{hashlib.sha256(canonical_key.encode('utf-8')).hexdigest()[:48]}"
    user_payload: Dict[str, Any] = {
        "name": business_name,
        "external_id": requester_external_id,
        "organization_id": organization_id,
        "role": "end-user",
        "skip_verify_email": True,
    }
    if lead_email:
        user_payload["email"] = lead_email
    if lead_phone:
        user_payload["phone"] = lead_phone

    response = requests.post(
        f"{base_url}/users/create_or_update.json",
        json={"user": user_payload},
        auth=auth,
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    requester = response.json().get("user") or {}
    if not requester.get("id"):
        raise HTTPException(
            status_code=502,
            detail={
                "code": "ZENDESK_REQUESTER_NOT_CREATED",
                "message": "Zendesk did not return an end-user ID for the business requester.",
            },
        )
    return requester


def reconcile_zendesk_intake_ticket(
    ticket: Dict[str, Any],
    spec: Dict[str, Any],
    base_url: str,
    auth: Tuple[str, str],
    headers: Dict[str, str],
) -> Tuple[Dict[str, Any], bool, List[str]]:
    """Repair and verify the exact managed route/field contract on an existing ticket."""
    ticket_id = ticket.get("id")
    if not ticket_id:
        raise HTTPException(status_code=502, detail="Zendesk returned a ticket without an ID.")

    contract = spec["contract"]
    desired_fields = spec["customFields"]
    current_fields = {
        compact_text(item.get("id")): item.get("value")
        for item in (ticket.get("custom_fields") or [])
    }
    mismatched_field_ids = [
        compact_text(item.get("id"))
        for item in desired_fields
        if not zendesk_ticket_field_value_matches(
            item.get("value"), current_fields.get(compact_text(item.get("id")))
        )
    ]
    current_tags = {compact_text(tag) for tag in (ticket.get("tags") or []) if compact_text(tag)}
    desired_tags = {compact_text(tag) for tag in spec["tags"] if compact_text(tag)}
    route_mismatch = (
        compact_text(ticket.get("brand_id")) != compact_text(contract["brandId"])
        or compact_text(ticket.get("ticket_form_id")) != compact_text(contract["formId"])
        or compact_text(ticket.get("external_id")) != spec["externalId"]
    )
    tags_missing = not desired_tags.issubset(current_tags)
    repaired = bool(route_mismatch or mismatched_field_ids or tags_missing)

    if repaired:
        response = requests.put(
            f"{base_url}/tickets/{ticket_id}.json",
            json={
                "ticket": {
                    "external_id": spec["externalId"],
                    "brand_id": contract["brandId"],
                    "ticket_form_id": contract["formId"],
                    "tags": sorted(current_tags.union(desired_tags)),
                    "custom_fields": desired_fields,
                }
            },
            auth=auth,
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        ticket = response.json().get("ticket") or {}
        if not ticket.get("custom_fields"):
            detail_response = requests.get(
                f"{base_url}/tickets/{ticket_id}.json",
                auth=auth,
                headers=headers,
                timeout=30,
            )
            detail_response.raise_for_status()
            ticket = detail_response.json().get("ticket") or {}

    returned_fields = {
        compact_text(item.get("id")): item.get("value")
        for item in (ticket.get("custom_fields") or [])
    }
    remaining_fields = [
        compact_text(item.get("id"))
        for item in desired_fields
        if not zendesk_ticket_field_value_matches(
            item.get("value"), returned_fields.get(compact_text(item.get("id")))
        )
    ]
    routing_matches = (
        compact_text(ticket.get("brand_id")) == compact_text(contract["brandId"])
        and compact_text(ticket.get("ticket_form_id")) == compact_text(contract["formId"])
        and compact_text(ticket.get("external_id")) == spec["externalId"]
    )
    returned_tags = {compact_text(tag) for tag in (ticket.get("tags") or []) if compact_text(tag)}
    if not routing_matches or remaining_fields or not desired_tags.issubset(returned_tags):
        raise HTTPException(
            status_code=502,
            detail={
                "code": "ZENDESK_TICKET_CONTRACT_REJECTED",
                "message": "Zendesk did not preserve the requested brand, form, managed fields, and tags.",
                "ticketId": ticket_id,
                "channel": spec["contract"]["channel"],
                "routingMatches": routing_matches,
                "missingFieldIds": remaining_fields,
            },
        )
    return ticket, repaired, mismatched_field_ids


def create_zendesk_intake_tickets(
    approval_id: str,
    context: Dict[str, Any],
    pipeline_id: str,
    batch_id: Optional[str] = None,
    requested_channels: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    business_name = compact_text(context.get("businessName"))
    canonical_key = compact_text(context.get("canonicalLeadKey"))
    campaign_id = compact_text(context.get("campaignId"))
    campaign_name = compact_text(context.get("campaignName"))
    normalized_approval_id = compact_text(approval_id)
    normalized_pipeline_id = compact_text(pipeline_id)
    lead_email = normalize_email_identity(context.get("email"))
    lead_phone = compact_text(context.get("phone"))
    source_url = compact_text(context.get("sourceUrl"))
    source_label = compact_text(context.get("source") or context.get("sourceLabel"), "public business listing")
    source_tag = "asf_source_upload" if compact_text(context.get("source")).lower() == "uploaded-lead-data" else "asf_source_apify_google_maps"
    industry = compact_text(context.get("industry") or context.get("category"), "Local service")
    location = compact_text(context.get("location"), "South Africa")

    available_channels = zendesk_channels_for_context(context)
    requested = [compact_text(value).lower() for value in (requested_channels or available_channels)]
    channels = [channel for channel in available_channels if channel in requested]
    if not channels:
        return []

    structural_values = {
        "campaignId": campaign_id,
        "campaignName": campaign_name,
        "canonicalLeadKey": canonical_key,
        "pipelineId": normalized_pipeline_id,
        "approvalId": normalized_approval_id,
        "businessName": business_name,
    }
    missing_structural = [key for key, value in structural_values.items() if not value]
    if missing_structural:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "CAMPAIGN_TICKET_CONTEXT_REQUIRED",
                "message": "Zendesk intake tickets can only be created from a named campaign.",
                "missing": missing_structural,
            },
        )

    contracts = {channel: require_zendesk_ticket_contract(channel) for channel in channels}
    zendesk_subdomain = require_env("ZENDESK_SUBDOMAIN")
    zendesk_email = require_env("ZENDESK_EMAIL")
    zendesk_token = require_env("ZENDESK_API_TOKEN")
    auth = (f"{zendesk_email}/token", zendesk_token)
    base_url = f"https://{zendesk_subdomain}.zendesk.com/api/v2"
    headers = {"Content-Type": "application/json"}
    created: List[Dict[str, Any]] = []
    campaign_tag = f"asf_campaign_{slugify(campaign_name, 32)}"
    contact_name = compact_text(
        context.get("contactName")
        or first_present(context.get("rawLead") or {}, ["contactName", "ownerName", "name"])
    )

    channel_specs: Dict[str, Dict[str, Any]] = {}
    for channel in channels:
        if channel == "email" and not lead_email:
            raise HTTPException(status_code=400, detail="The email campaign lead has no email address.")
        if channel == "phone" and not lead_phone:
            raise HTTPException(status_code=400, detail="The phone campaign lead has no phone number.")
        external_id = f"asf:{campaign_id}:{canonical_key}:{channel}:intake"
        contact_value = lead_email if channel == "email" else lead_phone
        tags = [
            "ai_site_factory",
            "asf_managed",
            "asf_intake",
            f"ai_site_{channel}_lead",
            source_tag,
            "asf_stage_intake",
            "asf_deploy_pending",
            campaign_tag,
            f"asf_channel_{channel}",
        ]
        if channel == "email":
            tags.extend(["asf_form_email_lead", "asf_email_send_pending", "asf_can_deploy"])
        else:
            tags.extend(["asf_form_call_lead", "asf_call_pending", "asf_can_deploy"])
        field_values = {
            "campaignId": campaign_id,
            "campaignName": campaign_name,
            "canonicalLeadKey": canonical_key,
            "pipelineId": normalized_pipeline_id,
            "approvalId": normalized_approval_id,
            "batchId": batch_id,
            "businessName": business_name,
            "contactName": contact_name,
            "contactEmail": lead_email,
            "contactPhone": lead_phone,
            "industry": industry,
            "location": location,
            "address": compact_text(context.get("address")),
            "contactChannel": channel,
            "leadStatus": "AWAITING_DEPLOYMENT",
            "deployRequested": False,
            "emailSendRequested": False,
            "phoneCallStatus": "NEW" if channel == "phone" else None,
            "sourceUrl": source_url,
        }
        channel_specs[channel] = {
            "contract": contracts[channel],
            "externalId": external_id,
            "contact": contact_value,
            "tags": tags,
            "customFields": zendesk_custom_fields(field_values),
        }

    pending_channels: List[str] = []
    for channel in channels:
        spec = channel_specs[channel]
        existing = get_zendesk_ticket_link(normalized_approval_id, channel, "intake")
        if not existing:
            existing = get_zendesk_ticket_link_by_external_id(spec["externalId"])
        if existing and existing.get("ticketId"):
            detail_response = requests.get(
                f"{base_url}/tickets/{existing['ticketId']}.json",
                auth=auth,
                headers=headers,
                timeout=30,
            )
            if detail_response.status_code != 404:
                detail_response.raise_for_status()
                ticket = detail_response.json().get("ticket") or {}
                ticket, repaired, repaired_field_ids = reconcile_zendesk_intake_ticket(
                    ticket, spec, base_url, auth, headers
                )
                existing_payload = existing.get("payload") if isinstance(existing.get("payload"), dict) else {}
                created.append(
                    save_zendesk_ticket_link(
                        approval_id=normalized_approval_id,
                        canonical_key=canonical_key,
                        pipeline_id=normalized_pipeline_id,
                        channel=channel,
                        stage="intake",
                        ticket_id=ticket.get("id"),
                        ticket_url=f"https://{zendesk_subdomain}.zendesk.com/agent/tickets/{ticket.get('id')}",
                        status=ticket.get("status") or existing.get("status") or "new",
                        tags=ticket.get("tags") or spec["tags"],
                        payload={
                            **existing_payload,
                            "externalId": spec["externalId"],
                            "reconciledLocalLink": True,
                            "repaired": repaired,
                            "repairedFieldIds": repaired_field_ids,
                            "reconciledAt": now_iso(),
                        },
                        external_id=spec["externalId"],
                    )
                )
                continue

        lookup_response = requests.get(
            f"{base_url}/tickets.json",
            params={"external_id": spec["externalId"], "per_page": 10},
            auth=auth,
            headers=headers,
            timeout=30,
        )
        lookup_response.raise_for_status()
        remote_matches = [
            ticket
            for ticket in (lookup_response.json().get("tickets") or [])
            if compact_text(ticket.get("external_id")) == spec["externalId"]
        ]
        if remote_matches:
            ticket = sorted(remote_matches, key=lambda item: int(item.get("id") or 0))[0]
            ticket, repaired, repaired_field_ids = reconcile_zendesk_intake_ticket(
                ticket, spec, base_url, auth, headers
            )
            created.append(
                save_zendesk_ticket_link(
                    approval_id=normalized_approval_id,
                    canonical_key=canonical_key,
                    pipeline_id=normalized_pipeline_id,
                    channel=channel,
                    stage="intake",
                    ticket_id=ticket.get("id"),
                    ticket_url=f"https://{zendesk_subdomain}.zendesk.com/agent/tickets/{ticket.get('id')}",
                    status=ticket.get("status") or "new",
                    tags=ticket.get("tags") or spec["tags"],
                    payload={
                        "adoptedByExternalId": True,
                        "externalId": spec["externalId"],
                        "repaired": repaired,
                        "repairedFieldIds": repaired_field_ids,
                        "adoptedAt": now_iso(),
                    },
                    external_id=spec["externalId"],
                )
            )
            continue
        pending_channels.append(channel)

    if not pending_channels:
        return created

    org_response = requests.post(
        f"{base_url}/organizations.json",
        json={
            "organization": {
                "name": business_name,
                "notes": (
                    f"AI Site Factory intake organization.\n"
                    f"Pipeline ID: {pipeline_id}\n"
                    f"Approval ID: {approval_id}\n"
                    f"Industry: {industry}\n"
                    f"Location: {location}"
                ),
                "tags": ["ai_site_factory", "asf_managed", "asf_intake", source_tag, campaign_tag],
            }
        },
        auth=auth,
        headers=headers,
        timeout=30,
    )
    if org_response.status_code == 422:
        search_response = requests.get(
            f"{base_url}/organizations/search.json",
            params={"name": business_name},
            auth=auth,
            headers=headers,
            timeout=30,
        )
        search_response.raise_for_status()
        organization = (search_response.json().get("organizations") or [{}])[0]
    else:
        org_response.raise_for_status()
        organization = org_response.json().get("organization", {})
    organization_id = organization.get("id")

    requester = ensure_zendesk_intake_requester(
        business_name=business_name,
        canonical_key=canonical_key,
        lead_email=lead_email,
        lead_phone=lead_phone,
        organization_id=organization_id,
        base_url=base_url,
        auth=auth,
        headers=headers,
    )

    for channel in pending_channels:
        spec = channel_specs[channel]
        contract = spec["contract"]
        contact_value = spec["contact"]
        tags = spec["tags"]
        custom_fields = spec["customFields"]
        ticket_payload: Dict[str, Any] = {
            "ticket": {
                "external_id": spec["externalId"],
                "brand_id": contract["brandId"],
                "ticket_form_id": contract["formId"],
                "subject": f"AI Site Factory {channel.title()} Intake: {business_name}",
                "comment": {
                    "body": (
                        f"AI Site Factory intake ticket\n\n"
                        f"Business: {business_name}\n"
                        f"Campaign: {campaign_name}\n"
                        f"Channel: {channel}\n"
                        f"Contact: {contact_value}\n"
                        f"Contact name: {contact_name or 'Not supplied'}\n"
                        f"Industry: {industry}\n"
                        f"Location: {location}\n"
                        f"Pipeline ID: {pipeline_id}\n"
                        f"Approval ID: {approval_id}\n"
                        f"Canonical Lead Key: {canonical_key}\n"
                        f"Source: {source_label}{f' ({source_url})' if source_url else ''}\n\n"
                        "No AI site has been generated yet. Tick the deploy field to call the deploy_site webhook. "
                        "After deployment, this ticket receives the GitHub/Netlify link. Email-channel tickets can then call send_email."
                    ),
                    "public": False,
                },
                "organization_id": organization_id,
                "priority": "normal",
                "type": "task",
                "status": "new",
                "tags": tags,
                "custom_fields": custom_fields,
            }
        }
        ticket_payload["ticket"]["requester_id"] = requester["id"]

        idempotency_key = f"asf-{hashlib.sha256(spec['externalId'].encode('utf-8')).hexdigest()[:48]}"
        response = requests.post(
            f"{base_url}/tickets.json",
            json=ticket_payload,
            auth=auth,
            headers={**headers, "Idempotency-Key": idempotency_key},
            timeout=30,
        )
        response.raise_for_status()
        ticket = response.json().get("ticket", {})
        returned_fields = {compact_text(item.get("id")): item.get("value") for item in (ticket.get("custom_fields") or [])}
        routing_matches = (
            compact_text(ticket.get("brand_id")) == compact_text(contract["brandId"])
            and compact_text(ticket.get("ticket_form_id")) == compact_text(contract["formId"])
            and compact_text(ticket.get("external_id")) == spec["externalId"]
        )
        missing_values = [
            compact_text(item.get("id"))
            for item in custom_fields
            if not zendesk_ticket_field_value_matches(
                item.get("value"), returned_fields.get(compact_text(item.get("id")))
            )
        ]
        if not ticket.get("id") or not routing_matches or missing_values:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "ZENDESK_TICKET_CONTRACT_REJECTED",
                    "message": "Zendesk did not preserve the requested brand, form, and managed field values.",
                    "ticketId": ticket.get("id"),
                    "channel": channel,
                    "routingMatches": routing_matches,
                    "missingFieldIds": missing_values,
                },
            )
        if compact_text(ticket.get("requester_id")) != compact_text(requester["id"]):
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "ZENDESK_REQUESTER_NOT_ASSIGNED",
                    "message": "Zendesk created the intake ticket without the business requester.",
                    "ticketId": ticket.get("id"),
                    "channel": channel,
                },
            )
        created.append(
            save_zendesk_ticket_link(
                approval_id=normalized_approval_id,
                canonical_key=canonical_key,
                pipeline_id=normalized_pipeline_id,
                channel=channel,
                stage="intake",
                ticket_id=ticket.get("id"),
                ticket_url=f"https://{zendesk_subdomain}.zendesk.com/agent/tickets/{ticket.get('id')}",
                status=ticket.get("status") or "new",
                tags=tags,
                payload={
                    "organizationId": organization_id,
                    "userId": requester["id"],
                    "requesterName": business_name,
                    "contact": contact_value,
                    "customFields": custom_fields,
                    "brandId": contract["brandId"],
                    "formId": contract["formId"],
                    "externalId": spec["externalId"],
                    "createdAt": now_iso(),
                },
                external_id=spec["externalId"],
            )
        )

    log_event("info", "provider.zendesk.intake_finish", "Zendesk intake tickets created.", approvalId=approval_id, ticketCount=len(created))
    return created


def resume_failed_outreach_approval(
    lead: DiscoveredLead,
    row: sqlite3.Row,
    pipeline_id: str,
) -> PipelineLeadResult:
    canonical_key = row["canonical_lead_key"]
    deployment_row = get_deployment_history_row(row["deployment_history_id"]) or latest_deployment_history_for_lead(canonical_key)
    deployment = deployment_from_history(deployment_row)
    deployment_history = deployment_history_row_to_dict(deployment_row)
    if not deployment or not deployment.get("url"):
        raise RuntimeError("Cannot resume outreach because no completed deployment was found.")

    context = safe_json_loads(row["context_json"], {})
    site_content = safe_json_loads(row["site_content_json"], {})
    publish_mode = row["publish_mode"] or "github-netlify"
    github_export = safe_json_loads(row["github_export_json"], None) or deployment.get("githubExport")
    step_history: List[Dict[str, Any]] = [
        record_skipped_pipeline_step(
            pipeline_id,
            canonical_key,
            "netlify_deploy",
            "Reused the previously completed Netlify deployment.",
            provider="netlify",
            details={"approvalId": row["id"], "deploymentHistoryId": deployment.get("deploymentHistoryId")},
        )
    ]

    def run_resume_step(step: str, provider: str, callback):
        started = now_iso()
        try:
            result = callback()
        except Exception as step_error:
            snapshot = record_pipeline_step(
                pipeline_id=pipeline_id,
                canonical_key=canonical_key,
                step=step,
                status="FAILED",
                provider=provider,
                message=str(step_error),
                started_at=started,
                finished_at=now_iso(),
                retryable=True,
                details={"approvalId": row["id"], "errorType": step_error.__class__.__name__},
            )
            step_history.append(snapshot)
            raise

        snapshot = record_pipeline_step(
            pipeline_id=pipeline_id,
            canonical_key=canonical_key,
            step=step,
            status="COMPLETED",
            provider=provider,
            message=f"{step} completed.",
            started_at=started,
            finished_at=now_iso(),
            retryable=False,
            details={"approvalId": row["id"]},
        )
        step_history.append(snapshot)
        return result

    outreach = safe_json_loads(row["outreach_json"], None)
    if outreach:
        step_history.append(
            record_skipped_pipeline_step(
                pipeline_id,
                canonical_key,
                "groq_outreach",
                "Reused the previously generated outreach draft.",
                provider="groq",
                details={"approvalId": row["id"]},
            )
        )
    else:
        outreach = run_resume_step(
            "groq_outreach",
            "groq",
            lambda: generate_outreach_with_groq(context, deployment.get("url", "")),
        )

    zendesk = run_resume_step(
        "zendesk_ticket",
        "zendesk",
        lambda: create_zendesk_outreach_ticket(context, deployment, outreach, pipeline_id),
    )

    with get_pipeline_db() as db:
        db.execute(
            """
            UPDATE approval_records
            SET status = 'APPROVED', updated_at = ?, outreach_json = ?,
                zendesk_json = ?, errors_json = ?
            WHERE id = ?
            """,
            (
                now_iso(),
                json.dumps(outreach, default=str),
                json.dumps(zendesk, default=str),
                json.dumps([], default=str),
                row["id"],
            ),
        )

    refresh_pipeline_run_status_from_approvals(row["pipeline_id"])

    return PipelineLeadResult(
        leadKey=lead.leadKey,
        canonicalLeadKey=canonical_key,
        businessName=row["business_name"],
        status="COMPLETED_RESUMED",
        pipelineStatus="COMPLETED_RESUMED",
        currentStep="resumed_outreach",
        stepHistory=step_history,
        approvalStatus="APPROVED",
        pendingApprovalId=row["id"],
        cleanedLead=context,
        siteContent=site_content,
        outreachDraft=outreach,
        deployment=deployment,
        deploymentHistory=deployment_history,
        zendesk=zendesk,
        publishMode=publish_mode,
        githubExport=github_export,
    )


def run_probe_check(name: str, callback) -> ApiProbeCheck:
    started = time.perf_counter()
    try:
        details = callback() or {}
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        return ApiProbeCheck(
            name=name,
            status="VALID",
            message=f"{name} check passed.",
            durationMs=duration_ms,
            details=redact_value(details),
        )
    except Exception as error:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        log_event("error", "debug.probe.failed", f"{name} check failed.", check=name, reason=str(error))
        return ApiProbeCheck(
            name=name,
            status="INVALID",
            message=sanitize_message(error),
            durationMs=duration_ms,
            details={},
        )


def probe_environment() -> Dict[str, Any]:
    providers = provider_env_status()
    missing = [
        f"{provider}:{check['name']}"
        for provider, provider_status in providers.items()
        for check in provider_status["checks"]
        if not check["configured"]
    ]
    if missing:
        raise RuntimeError(f"Missing or placeholder environment values: {', '.join(missing)}")
    return {"providers": providers}


def probe_apify() -> Dict[str, Any]:
    token = require_env("APIFY_API_TOKEN")
    response = requests.get(
        "https://api.apify.com/v2/users/me",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json().get("data", {})
    return {"username": data.get("username"), "id": data.get("id")}


def probe_gemini() -> Dict[str, Any]:
    api_key = require_env("GEMINI_API_KEY")
    model_name = os.getenv("GEMINI_TEXT_MODEL", "gemini-2.0-flash")
    response = requests.get(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}",
        headers={"x-goog-api-key": api_key},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    return {"model": data.get("name") or model_name, "supportedMethods": data.get("supportedGenerationMethods", [])}


def probe_groq() -> Dict[str, Any]:
    api_key = require_env("GROQ_API_KEY")
    response = requests.get(
        "https://api.groq.com/openai/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    return {"modelCount": len(data.get("data", []))}


def probe_netlify() -> Dict[str, Any]:
    token = require_env("NETLIFY_AUTH_TOKEN")
    response = requests.get(
        "https://api.netlify.com/api/v1/user",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    return {"id": data.get("id"), "fullName": data.get("full_name"), "gitBuilds": "supported"}


def probe_github() -> Dict[str, Any]:
    owner = require_env("GITHUB_OWNER")
    response = requests.get(
        "https://api.github.com/user",
        headers=github_headers(),
        timeout=20,
    )
    response.raise_for_status()
    user = response.json()
    return {
        "login": user.get("login"),
        "configuredOwner": owner,
        "repoCreation": "requires Administration/write",
        "contents": "requires Contents/write",
    }


def probe_zendesk() -> Dict[str, Any]:
    zendesk_subdomain = require_env("ZENDESK_SUBDOMAIN")
    zendesk_email = require_env("ZENDESK_EMAIL")
    zendesk_token = require_env("ZENDESK_API_TOKEN")
    response = requests.get(
        f"https://{zendesk_subdomain}.zendesk.com/api/v2/users/me.json",
        auth=(f"{zendesk_email}/token", zendesk_token),
        timeout=20,
    )
    response.raise_for_status()
    user = response.json().get("user", {})
    return {"id": user.get("id"), "role": user.get("role"), "email": user.get("email")}


def api_safety_snapshot() -> Dict[str, Any]:
    provider_statuses = provider_env_status()
    providers = []
    for provider, status in provider_statuses.items():
        last_result = API_SAFETY_CHECK_RESULTS.get(provider)
        providers.append(
            {
                "provider": provider,
                "configured": status["configured"],
                "variables": [
                    {
                        "name": check["name"],
                        "configured": check["configured"],
                        "issue": check["issue"],
                    }
                    for check in status["checks"]
                ],
                "lastCheck": last_result,
            }
        )
    return {
        "providers": providers,
        "configuredCount": sum(1 for item in providers if item["configured"]),
        "totalCount": len(providers),
        "secretsExposed": False,
        "message": "Secret values remain server-side and are never returned to the browser.",
    }


@app.get("/api/auth/session")
def get_admin_auth_session(request: Request):
    settings = admin_auth_settings()
    session = read_admin_session(request.cookies.get(ADMIN_SESSION_COOKIE), settings) if settings["configured"] else None
    return {
        "authRequired": settings["configured"],
        "authenticated": bool(session),
        "username": session.get("username") if session else None,
        "configuredUsername": settings["username"] if settings["configured"] else None,
        "configurationSource": settings["source"],
        "sessionExpiresAt": session.get("expiresAt") if session else None,
    }


@app.post("/api/auth/login")
def admin_login(request: AdminLoginRequest, http_request: Request):
    settings = admin_auth_settings()
    if not settings["configured"]:
        raise HTTPException(
            status_code=503,
            detail="Administrator login is not configured. Add ADMIN_USERNAME and ADMIN_PASSWORD_HASH on the backend host.",
        )

    forwarded = compact_text(http_request.headers.get("x-forwarded-for")).split(",", 1)[0]
    client_ip = forwarded or compact_text(http_request.client.host if http_request.client else "unknown")
    login_key = f"{client_ip}:{compact_text(request.username).lower()}"
    window_started = time.time() - (15 * 60)
    attempts = [attempt for attempt in ADMIN_LOGIN_ATTEMPTS.get(login_key, []) if attempt >= window_started]
    if len(attempts) >= 5:
        raise HTTPException(status_code=429, detail="Too many failed login attempts. Try again in 15 minutes.")

    valid_username = hmac.compare_digest(compact_text(request.username), settings["username"])
    valid_password = verify_admin_password(request.password, settings)
    if not (valid_username and valid_password):
        attempts.append(time.time())
        ADMIN_LOGIN_ATTEMPTS[login_key] = attempts
        time.sleep(0.2)
        raise HTTPException(status_code=401, detail="The administrator username or password is incorrect.")

    ADMIN_LOGIN_ATTEMPTS.pop(login_key, None)
    token = issue_admin_session(settings["username"], settings)
    session = read_admin_session(token, settings) or {}
    response = JSONResponse(
        {
            "authenticated": True,
            "username": settings["username"],
            "sessionExpiresAt": session.get("expiresAt"),
        }
    )
    secure = admin_cookie_secure()
    response.set_cookie(
        key=ADMIN_SESSION_COOKIE,
        value=token,
        max_age=ADMIN_SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=secure,
        samesite="none" if secure else "lax",
        path="/",
    )
    return response


@app.post("/api/auth/logout")
def admin_logout():
    response = JSONResponse({"authenticated": False, "message": "Administrator session ended."})
    secure = admin_cookie_secure()
    response.delete_cookie(
        key=ADMIN_SESSION_COOKIE,
        path="/",
        secure=secure,
        samesite="none" if secure else "lax",
    )
    return response


@app.get("/api/settings/api-safety")
def get_api_safety_center():
    return api_safety_snapshot()


@app.post("/api/settings/api-safety/probe")
def probe_api_safety_provider(request: ApiSafetyProbeRequest):
    provider = compact_text(request.provider).lower()
    callbacks = {
        "apify": probe_apify,
        "gemini": probe_gemini,
        "groq": probe_groq,
        "github": probe_github,
        "netlify": probe_netlify,
        "zendesk": probe_zendesk,
    }
    if provider not in callbacks:
        raise HTTPException(status_code=400, detail="Choose a supported API provider.")
    check = run_probe_check(provider, callbacks[provider])
    result = {
        "status": check.status,
        "message": check.message,
        "durationMs": check.durationMs,
        "checkedAt": now_iso(),
    }
    API_SAFETY_CHECK_RESULTS[provider] = result
    return {"provider": provider, "result": result, "safety": api_safety_snapshot()}


@app.get("/api/debug/status")
def get_debug_status():
    providers = provider_env_status()
    configured_count = sum(1 for provider in providers.values() if provider["configured"])
    total_count = len(providers)
    status = "READY" if configured_count == total_count else "ACTION_REQUIRED"
    try:
        with get_pipeline_db() as db:
            persistent_counts = {
                "registeredLeads": db.execute("SELECT COUNT(*) AS count FROM lead_registry").fetchone()["count"],
                "pendingApprovals": db.execute("SELECT COUNT(*) AS count FROM approval_records WHERE status = 'PENDING'").fetchone()["count"],
                "deployments": db.execute("SELECT COUNT(*) AS count FROM deployment_history").fetchone()["count"],
                "githubRepos": db.execute("SELECT COUNT(*) AS count FROM github_site_repos WHERE export_status = 'EXPORTED'").fetchone()["count"],
                "gitDeployments": db.execute("SELECT COUNT(*) AS count FROM deployment_history WHERE publish_mode = 'github-netlify'").fetchone()["count"],
                "failedSteps": db.execute("SELECT COUNT(*) AS count FROM pipeline_steps WHERE status = 'FAILED'").fetchone()["count"],
            }
    except Exception as error:
        persistent_counts = {"error": str(error)}
    return {
        "status": status,
        "startedAt": STARTED_AT.isoformat(),
        "uptimeSeconds": int((datetime.now(timezone.utc) - STARTED_AT).total_seconds()),
        "providers": providers,
        "chunking": {
            "modelChunkChars": MODEL_CHUNK_CHARS,
            "modelMaxChunks": MODEL_MAX_CHUNKS,
        },
        "counts": {
            "leads": len(LEADS_DB),
            "discoveries": len(DISCOVERY_DB),
            "pipelines": len(PIPELINE_DB),
            "logsBuffered": len(LOG_BUFFER),
            **persistent_counts,
        },
    }


@app.get("/api/debug/logs")
def get_debug_logs(limit: int = 80):
    safe_limit = max(1, min(limit, 250))
    return {"logs": list(LOG_BUFFER)[:safe_limit], "count": len(LOG_BUFFER)}


@app.post("/api/debug/probe", response_model=ApiProbeResponse)
def run_debug_probe(request: ApiProbeRequest):
    requested = set(request.checks or [])
    checks: List[ApiProbeCheck] = [
        run_probe_check("environment", probe_environment),
        run_probe_check("backend", lambda: {"message": "Backend process is accepting API requests."}),
    ]

    external_checks = {
        "apify": probe_apify,
        "gemini": probe_gemini,
        "groq": probe_groq,
        "github": probe_github,
        "netlify": probe_netlify,
        "zendesk": probe_zendesk,
    }

    if request.includeExternal:
        for name, callback in external_checks.items():
            if not requested or name in requested:
                checks.append(run_probe_check(name, callback))

    overall = "VALID" if all(check.status == "VALID" for check in checks) else "INVALID"
    response = ApiProbeResponse(
        status=overall,
        generatedAt=datetime.now().isoformat(),
        checks=checks,
    )
    log_event("info", "debug.probe.finished", "API probe finished.", status=overall, external=request.includeExternal)
    return response


@app.get("/api/presets")
def get_lead_presets():
    return {"presets": LEAD_PRESETS}


@app.get("/api/templates")
def get_site_templates():
    return {"templates": [FREEFORM_SITE_SPEC], "deprecated": True}

def discover_leads_internal(
    request: DiscoverLeadsRequest,
    progress_callback: Optional[Callable[[str, float], None]] = None,
) -> DiscoverLeadsResponse:
    preset = resolve_lead_preset(request.presetId, request.industry, request.query)
    location = compact_text(request.location, "Durban, South Africa")
    limit = max(1, min(int(request.limit or 5), 10000))
    apify_limit = min(max(limit * 4, 20), 10000)
    primary_query = build_google_maps_query(preset, location, request.query)

    if not request.forceRefresh:
        cached = cached_discovery_response(preset, request.presetId, primary_query, location, limit)
        if cached:
            log_event(
                "info",
                "leads.discover.cache_hit",
                "Returning cached discovery results.",
                batchId=cached.batchId,
                presetId=request.presetId,
                location=location,
                query=primary_query,
                leadCount=len(cached.leads),
            )
            return cached

    warnings: List[str] = []
    duplicates_skipped = 0
    no_contact_skipped = 0
    websites_skipped = 0
    already_deployed_skipped = 0
    active_deployment_skipped = 0
    policy_excluded_skipped = 0
    reused_pending_or_failed = 0
    target_overflow_skipped = 0
    normalization_stats: Dict[str, int] = {}
    raw_fetched = 0
    provider_status = "UNKNOWN"
    provider_duration_seconds = 0.0
    search_variant_count = len(apify_google_maps_search_variants(primary_query, location))
    selected_leads: List[DiscoveredLead] = []
    eligible_leads: List[DiscoveredLead] = []

    province_stats: Dict[str, Any] = {
        location: {
            "rawItems": 0,
            "normalized": 0,
            "qualified": 0,
            "selected": 0,
            "duplicatesSkipped": 0,
            "websitesSkipped": 0,
            "noContactSkipped": 0,
            "currentSearchDuplicatesSkipped": 0,
            "alreadyDeployedSkipped": 0,
            "activeDeploymentSkipped": 0,
            "policyExcludedSkipped": 0,
            "locationSkipped": 0,
            "invalidRecordSkipped": 0,
            "reusedPendingOrFailed": 0,
            "targetOverflowSkipped": 0,
            "unqualifiedSkipped": 0,
            "eligible": 0,
            "emailLeads": 0,
            "phoneLeads": 0,
            "emailAndPhoneLeads": 0,
        }
    }

    log_event(
        "info",
        "leads.discover.start",
        "Focused lead discovery started.",
        presetId=request.presetId,
        location=location,
        query=primary_query,
        limit=limit,
        rawFetchLimit=apify_limit,
    )

    try:
        fetch_limit = apify_limit
        query_items = run_apify_google_maps(primary_query, fetch_limit, location)
        raw_fetched = len(query_items)
        provider_status = compact_text(getattr(query_items, "status", None), "UNKNOWN").upper()
        provider_duration_seconds = float(getattr(query_items, "duration_seconds", 0.0) or 0.0)
        search_variant_count = int(getattr(query_items, "search_variant_count", search_variant_count) or 0)
        province_stats[location]["rawItems"] = raw_fetched
        if progress_callback:
            progress_callback("VALIDATING_LEADS", 70)

        normalized = normalize_apify_items(
            query_items,
            preset["industry"],
            location,
            fetch_limit,
            normalization_stats,
        )
        province_stats[location]["normalized"] = len(normalized)
        province_stats[location]["locationSkipped"] = normalization_stats.get("locationSkipped", 0)
        province_stats[location]["invalidRecordSkipped"] = normalization_stats.get("invalidRecordSkipped", 0)
        duplicates_skipped += normalization_stats.get("currentSearchDuplicatesSkipped", 0)

        qualified = normalized
        province_stats[location]["qualified"] = len(qualified)
        seen_batch_keys = set()
        seen_batch_identities: Set[str] = set()
        usage_index = prior_lead_usage_index()

        for lead in qualified:
            canonical_key = canonical_lead_key_for_lead(lead)
            lead.canonicalLeadKey = canonical_key
            lead.location = location
            identity_keys = [identity_key for _identity_type, identity_key in lead_identity_pairs(lead)]

            if lead_has_website(lead):
                websites_skipped += 1
                province_stats[location]["websitesSkipped"] += 1
                continue

            if not lead_has_contact(lead):
                no_contact_skipped += 1
                province_stats[location]["noContactSkipped"] += 1
                continue

            if (
                canonical_key in seen_batch_keys
                or any(identity_key in seen_batch_identities for identity_key in identity_keys)
            ):
                duplicates_skipped += 1
                continue

            seen_batch_keys.add(canonical_key)
            seen_batch_identities.update(identity_keys)
            prior_usage = classify_prior_lead_usage(lead, canonical_key, usage_index)
            if prior_usage == "ALREADY_DEPLOYED":
                already_deployed_skipped += 1
                continue
            if prior_usage == "ACTIVE_DEPLOYMENT":
                active_deployment_skipped += 1
                continue
            if prior_usage == "POLICY_EXCLUDED":
                policy_excluded_skipped += 1
                continue
            if prior_usage == "REUSABLE_PENDING_OR_FAILED":
                reused_pending_or_failed += 1
            eligible_leads.append(lead)

        selected_leads = select_mixed_contact_leads(eligible_leads, limit)
        target_overflow_skipped = max(0, len(eligible_leads) - len(selected_leads))

    except Exception as error:
        message = sanitize_message(error)
        log_event(
            "error",
            "leads.discover.provider_failed",
            "Apify lead discovery failed. No fallback contacts were generated.",
            error=message,
            query=primary_query,
            location=location,
        )
        raise HTTPException(
            status_code=502,
            detail={
                "code": "APIFY_DISCOVERY_FAILED",
                "message": "Apify could not complete the lead search within two minutes. No contacts were invented; retry the search.",
                "providerError": message,
            },
        ) from error

    for lead in selected_leads:
        try:
            upsert_lead_registry(lead)
        except Exception as error:
            warnings.append(f"Could not save lead {lead.businessName}: {sanitize_message(error)}")

    province_stats[location]["selected"] = len(selected_leads)
    province_stats[location]["eligible"] = len(eligible_leads)
    province_stats[location]["duplicatesSkipped"] = duplicates_skipped
    province_stats[location]["currentSearchDuplicatesSkipped"] = duplicates_skipped
    province_stats[location]["alreadyDeployedSkipped"] = already_deployed_skipped
    province_stats[location]["activeDeploymentSkipped"] = active_deployment_skipped
    province_stats[location]["policyExcludedSkipped"] = policy_excluded_skipped
    province_stats[location]["reusedPendingOrFailed"] = reused_pending_or_failed
    province_stats[location]["targetOverflowSkipped"] = target_overflow_skipped
    province_stats[location]["emailLeads"] = sum(1 for lead in selected_leads if normalize_email_identity(lead.email))
    province_stats[location]["phoneLeads"] = sum(1 for lead in selected_leads if normalize_phone_identity(lead.phone))
    province_stats[location]["emailAndPhoneLeads"] = sum(
        1
        for lead in selected_leads
        if normalize_email_identity(lead.email) and normalize_phone_identity(lead.phone)
    )

    if not selected_leads:
        warnings.append("No contactable no-website leads were returned. Try a broader phrase or nearby city.")
    elif len(selected_leads) < limit:
        warnings.append(
            f"Requested {limit} leads but only found {len(selected_leads)} contactable no-website leads. "
            f"Skipped {websites_skipped} with websites, {no_contact_skipped} without email/phone, "
            f"{duplicates_skipped} duplicates in this search, {already_deployed_skipped} already live, "
            f"{active_deployment_skipped} actively deploying, and {policy_excluded_skipped} excluded by policy."
        )

    batch_id = str(uuid4())
    shortfall = max(0, limit - len(selected_leads))
    stop_reason = (
        "TARGET_MET"
        if not shortfall
        else "PROVIDER_DEADLINE"
        if provider_status == "TIMED-OUT" or provider_duration_seconds >= 105
        else "RESULTS_EXHAUSTED"
    )
    province_stats[location]["shortfall"] = shortfall
    province_stats[location]["stopReason"] = stop_reason
    province_stats[location]["searchVariantCount"] = search_variant_count
    province_stats[location]["providerStatus"] = provider_status
    province_stats[location]["providerDurationSeconds"] = provider_duration_seconds

    response = DiscoverLeadsResponse(
        batchId=batch_id,
        preset=preset,
        location=location,
        query=primary_query,
        leads=selected_leads,
        sourceStatus="READY" if selected_leads else "NO_RESULTS",
        warnings=warnings,
        provinceStats=province_stats,
        duplicatesSkipped=duplicates_skipped,
        requestedCount=limit,
        rawFetched=raw_fetched,
        eligibleReturned=len(selected_leads),
        websitesSkipped=websites_skipped,
        noContactSkipped=no_contact_skipped,
        generatedDuplicatesSkipped=already_deployed_skipped + active_deployment_skipped + policy_excluded_skipped,
        currentSearchDuplicatesSkipped=duplicates_skipped,
        alreadyDeployedSkipped=already_deployed_skipped,
        activeDeploymentSkipped=active_deployment_skipped,
        policyExcludedSkipped=policy_excluded_skipped,
        locationSkipped=normalization_stats.get("locationSkipped", 0),
        invalidRecordSkipped=normalization_stats.get("invalidRecordSkipped", 0),
        reusedPendingOrFailed=reused_pending_or_failed,
        targetOverflowSkipped=target_overflow_skipped,
        shortfall=shortfall,
        stopReason=stop_reason,
        searchVariantCount=search_variant_count,
        providerStatus=provider_status,
        providerDurationSeconds=provider_duration_seconds,
        emailLeads=province_stats[location]["emailLeads"],
        phoneLeads=province_stats[location]["phoneLeads"],
        emailAndPhoneLeads=province_stats[location]["emailAndPhoneLeads"],
        cached=False,
    )

    DISCOVERY_DB[batch_id] = response.model_dump()

    try:
        record_discovery_batch(
            batch_id=batch_id,
            preset_id=request.presetId,
            query=primary_query,
            location=location,
            lead_count=len(selected_leads),
            duplicates_skipped=duplicates_skipped,
            leads=selected_leads,
            province_stats=province_stats,
            warnings=warnings,
        )
    except Exception as error:
        log_event(
            "warning",
            "leads.discover.batch_save_failed",
            "Discovery batch could not be saved.",
            reason=str(error),
        )

    log_event(
        "info",
        "leads.discover.finish",
        "Lead discovery finished.",
        batchId=batch_id,
        leadCount=len(response.leads),
        duplicatesSkipped=duplicates_skipped,
        warningCount=len(warnings),
        status=response.sourceStatus,
        location=location,
    )

    return response


@app.post("/api/leads/discover", response_model=DiscoverLeadsResponse)
def discover_leads(request: DiscoverLeadsRequest):
    return discover_leads_internal(request)

@app.post("/api/pipeline/run", response_model=PipelineRunResponse)
def run_pipeline(request: PipelineRunRequest):
    if not env_enabled("ENABLE_LEGACY_PIPELINE_RUN"):
        raise HTTPException(
            status_code=410,
            detail={
                "code": "LEGACY_PIPELINE_DISABLED",
                "message": (
                    "Direct pipeline runs are disabled. Create a named campaign first, then let a Zendesk "
                    "agent request deployment from the managed intake ticket."
                ),
            },
        )
    template = dict(FREEFORM_SITE_SPEC)
    template_id = FREEFORM_TEMPLATE_ID
    if not request.leads:
        raise HTTPException(status_code=400, detail="Select at least one lead.")

    pipeline_id = str(uuid4())
    results: List[PipelineLeadResult] = []
    pipeline_warnings: List[str] = []
    seen_request_canonical_keys: Set[str] = set()
    seen_request_identity_keys: Set[str] = set()
    created_at = now_iso()
    save_pipeline_run(
        pipeline_id=pipeline_id,
        status="PROCESSING",
        template_id=template_id,
        source_batch_id=request.sourceBatchId,
        lead_count=len(request.leads),
        completed_count=0,
        pending_count=0,
        failed_count=0,
        warnings=pipeline_warnings,
        created_at=created_at,
    )
    log_event("info", "pipeline.start", "Pipeline run started.", pipelineId=pipeline_id, templateId=template_id, leadCount=len(request.leads))

    for lead in request.leads:
        errors: List[str] = []
        structured_errors: List[Dict[str, Any]] = []
        step_history: List[Dict[str, Any]] = []
        cleaned_context: Optional[Dict[str, Any]] = None
        site_content: Optional[Dict[str, Any]] = None
        pending_html: Optional[str] = None
        approval_id: Optional[str] = None
        github_export: Optional[Dict[str, Any]] = None
        canonical_key = canonical_lead_key_for_lead(lead)
        lead.canonicalLeadKey = canonical_key
        identity_keys = [identity_key for _identity_type, identity_key in lead_identity_pairs(lead)]
        status = "PROCESSING"
        current_step = "start"
        approval_status = None

        if lead_has_website(lead):
            results.append(
                skipped_pipeline_result(
                    lead,
                    canonical_key,
                    pipeline_id,
                    "SKIPPED_WEBSITE_PRESENT",
                    "Lead already has a website, so the no-website pipeline skipped it.",
                    {"website": lead.website, "domain": lead.domain},
                )
            )
            continue

        identity_conflicts = lead_identity_conflicts(lead, canonical_key)
        if (
            canonical_key in seen_request_canonical_keys
            or any(identity_key in seen_request_identity_keys for identity_key in identity_keys)
            or identity_conflicts
        ):
            results.append(
                skipped_pipeline_result(
                    lead,
                    canonical_key,
                    pipeline_id,
                    "SKIPPED_DUPLICATE",
                    "Lead matched a duplicate identity, so generation was skipped.",
                    {"identityConflicts": identity_conflicts, "identityKeys": identity_keys},
                )
            )
            continue

        seen_request_canonical_keys.add(canonical_key)
        seen_request_identity_keys.update(identity_keys)

        def run_step(step: str, provider: Optional[str], callback, retryable: bool = False):
            nonlocal current_step
            current_step = step
            started = now_iso()
            log_event(
                "info",
                f"pipeline.lead.{step}.start",
                f"Pipeline step {step} started.",
                pipelineId=pipeline_id,
                leadKey=lead.leadKey,
                canonicalLeadKey=canonical_key,
                provider=provider,
            )
            try:
                result = callback()
            except Exception as step_error:
                finished = now_iso()
                snapshot = record_pipeline_step(
                    pipeline_id=pipeline_id,
                    canonical_key=canonical_key,
                    step=step,
                    status="FAILED",
                    provider=provider,
                    message=str(step_error),
                    started_at=started,
                    finished_at=finished,
                    retryable=retryable,
                    details={"errorType": step_error.__class__.__name__},
                )
                step_history.append(snapshot)
                raise

            finished = now_iso()
            snapshot = record_pipeline_step(
                pipeline_id=pipeline_id,
                canonical_key=canonical_key,
                step=step,
                status="COMPLETED",
                provider=provider,
                message=f"{step} completed.",
                started_at=started,
                finished_at=finished,
                retryable=False,
            )
            step_history.append(snapshot)
            return result

        try:
            log_event("info", "pipeline.lead.start", "Processing pipeline lead.", pipelineId=pipeline_id, leadKey=lead.leadKey, businessName=lead.businessName)
            upsert_lead_registry(lead)

            if request.resumeExisting and not request.forceRegenerate:
                existing_approval = latest_reusable_approval_for_lead(canonical_key)
                if existing_approval:
                    log_event(
                        "info",
                        "pipeline.lead.resume_existing",
                        "Reusable pipeline output found for lead.",
                        pipelineId=pipeline_id,
                        leadKey=lead.leadKey,
                        canonicalLeadKey=canonical_key,
                        approvalId=existing_approval["id"],
                        approvalStatus=existing_approval["status"],
                    )
                    if existing_approval["status"] == "DEPLOYED_ZENDESK_FAILED":
                        current_step = "resume_outreach"
                        results.append(resume_failed_outreach_approval(lead, existing_approval, pipeline_id))
                    else:
                        results.append(pipeline_result_from_reused_approval(lead, existing_approval, pipeline_id))
                    continue

            contact_details = run_step("scrape_contact_details", "website", lambda: scrape_contact_details(lead), retryable=True)
            log_event("info", "pipeline.lead.scraped", "Contact details scraped.", pipelineId=pipeline_id, leadKey=lead.leadKey, hasEmail=bool(contact_details.get("email")), hasWebsite=bool(contact_details.get("website")))
            cleaned_context = build_public_lead_context(lead, contact_details, canonical_key)
            if cleaned_context.get("hasWebsite"):
                results.append(
                    skipped_pipeline_result(
                        lead,
                        canonical_key,
                        pipeline_id,
                        "SKIPPED_WEBSITE_PRESENT",
                        "Contact enrichment found a website, so the no-website pipeline skipped it.",
                        {"website": cleaned_context.get("website"), "domain": cleaned_context.get("domain")},
                    )
                )
                continue
            log_event("info", "pipeline.lead.context_ready", "Lead context prepared.", pipelineId=pipeline_id, leadKey=lead.leadKey)

            groq_brief = run_step(
                "groq_compact_lead",
                "groq",
                lambda: compact_lead_with_groq(cleaned_context),
                retryable=True,
            )
            final_html_result = run_step(
                "gemini_final_html",
                "gemini",
                lambda: generate_final_html_with_gemini(groq_brief),
                retryable=True,
            )
            quality_result = run_step(
                "seo_validation",
                "local",
                lambda: prepare_generated_site_artifact(final_html_result["html"], groq_brief),
                retryable=False,
            )
            pending_html = quality_result["html"]
            seo_validation = quality_result["seoValidation"]
            site_content = {
                "promptHeader": LANDING_PAGE_PROMPT_HEADER,
                "groqBrief": groq_brief,
                "geminiQaNotes": final_html_result.get("qaNotes"),
                "structureNotes": final_html_result.get("structureNotes"),
                "stylingLibraries": final_html_result.get("stylingLibraries"),
                "siteProfile": HIGHLY_INTERACTIVE_SITE_PROFILE,
                "seoValidation": seo_validation,
                "finalHtmlChecksum": html_checksum(pending_html),
            }
            approval_id = create_approval_record(
                pipeline_id=pipeline_id,
                canonical_key=canonical_key,
                lead_key=lead.leadKey,
                business_name=lead.businessName,
                site_html=pending_html,
                context=cleaned_context,
                site_content=site_content,
                template=template,
                status="EXPORTING",
            )
            if request.forceRegenerate:
                supersede_pending_approvals(canonical_key, approval_id, "pipeline force regenerate")

            try:
                github_export = run_step(
                    "github_export",
                    "github",
                    lambda: export_site_to_github(
                        canonical_key=canonical_key,
                        business_name=lead.businessName,
                        site_html=pending_html or "",
                        pipeline_id=pipeline_id,
                        approval_id=approval_id,
                    ),
                    retryable=True,
                )
            except Exception as export_error:
                errors.append(sanitize_message(export_error))
                structured_errors.append(structured_pipeline_error("github_export", export_error, provider="github", retryable=True))
                save_github_export_record(
                    canonical_key,
                    {
                        "repoName": github_repo_name(canonical_key, lead.businessName),
                        "htmlChecksum": html_checksum(pending_html or ""),
                        "pipelineId": pipeline_id,
                        "approvalId": approval_id,
                    },
                    "FAILED",
                    str(export_error),
                )
                with get_pipeline_db() as db:
                    db.execute(
                        """
                        UPDATE approval_records
                        SET status = ?, updated_at = ?, errors_json = ?
                        WHERE id = ?
                        """,
                        ("EXPORT_FAILED", now_iso(), json.dumps(structured_errors, default=str), approval_id),
                    )
                approval_status = "EXPORT_FAILED"
                current_step = "github_export"
                status = "EXPORT_FAILED"
                log_event("error", "pipeline.lead.export_failed", "GitHub export failed for generated site.", pipelineId=pipeline_id, leadKey=lead.leadKey, approvalId=approval_id, reason=str(export_error))
            else:
                with get_pipeline_db() as db:
                    db.execute(
                        """
                        UPDATE approval_records
                        SET status = ?, updated_at = ?, publish_mode = ?, github_export_json = ?
                        WHERE id = ?
                        """,
                        ("PENDING", now_iso(), "github-netlify", json.dumps(github_export, default=str), approval_id),
                    )
                approval_status = "PENDING"
                current_step = "approval"
                status = "PENDING_APPROVAL"
                log_event("info", "pipeline.lead.pending_approval", "Pipeline lead is pending manual approval.", pipelineId=pipeline_id, leadKey=lead.leadKey, approvalId=approval_id)

        except requests.RequestException as error:
            status = "FAILED"
            errors.append(sanitize_message(error))
            structured_errors.append(structured_pipeline_error(current_step, error, provider=None, retryable=True))
            log_event("error", "pipeline.lead.failed", "Pipeline lead failed during provider request.", pipelineId=pipeline_id, leadKey=lead.leadKey, reason=str(error))
        except RuntimeError as error:
            status = "FAILED"
            errors.append(sanitize_message(error))
            structured_errors.append(structured_pipeline_error(current_step, error, provider=None, retryable=False))
            log_event("error", "pipeline.lead.failed", "Pipeline lead failed at runtime.", pipelineId=pipeline_id, leadKey=lead.leadKey, reason=str(error))
        except Exception as error:
            status = "FAILED"
            errors.append(f"Unexpected pipeline error: {sanitize_message(error)}")
            structured_errors.append(structured_pipeline_error(current_step, error, provider=None, retryable=False))
            log_event("error", "pipeline.lead.failed", "Unexpected pipeline lead failure.", pipelineId=pipeline_id, leadKey=lead.leadKey, reason=str(error))

        results.append(
            PipelineLeadResult(
                leadKey=lead.leadKey,
                canonicalLeadKey=canonical_key,
                businessName=lead.businessName,
                status=status,
                pipelineStatus=status,
                currentStep=current_step,
                stepHistory=step_history,
                approvalStatus=approval_status,
                pendingApprovalId=approval_id,
                pendingPreviewHtml=pending_html,
                cleanedLead=cleaned_context,
                siteContent=site_content,
                outreachDraft=None,
                deployment=None,
                zendesk=None,
                publishMode="github-netlify",
                githubExport=github_export,
                structuredErrors=structured_errors,
                errors=errors,
            )
        )

    pending_count = sum(1 for result in results if result.status == "PENDING_APPROVAL")
    failed_count = sum(1 for result in results if result.status in {"FAILED", "EXPORT_FAILED", "SKIPPED_DUPLICATE", "SKIPPED_WEBSITE_PRESENT"})
    completed_count = sum(1 for result in results if result.status.startswith("COMPLETED"))
    if failed_count and (pending_count or completed_count):
        response_status = "PARTIAL_FAILURE"
    elif failed_count:
        response_status = "FAILED"
    elif pending_count and completed_count:
        response_status = "PARTIAL_PENDING"
    elif pending_count:
        response_status = "PENDING_APPROVAL"
    else:
        response_status = "COMPLETED"
    response = PipelineRunResponse(
        pipelineId=pipeline_id,
        status=response_status,
        templateId=template_id,
        createdAt=created_at,
        results=results,
        warnings=pipeline_warnings,
    )
    PIPELINE_DB[pipeline_id] = response.model_dump()
    save_pipeline_run(
        pipeline_id=pipeline_id,
        status=response_status,
        template_id=template_id,
        source_batch_id=request.sourceBatchId,
        lead_count=len(request.leads),
        completed_count=completed_count,
        pending_count=pending_count,
        failed_count=failed_count,
        warnings=pipeline_warnings,
        created_at=created_at,
    )
    log_event("info", "pipeline.finish", "Pipeline run finished.", pipelineId=pipeline_id, status=response_status, pendingCount=pending_count, failedCount=failed_count, leadCount=len(results))
    return response


@app.get("/api/approvals")
def list_approvals(status: Optional[str] = "PENDING", limit: int = 50):
    safe_limit = max(1, min(limit, 100))
    with get_pipeline_db() as db:
        if status and status.upper() != "ALL":
            rows = db.execute(
                """
                SELECT * FROM approval_records
                WHERE status = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (status.upper(), safe_limit),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT * FROM approval_records
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()

    return {"approvals": [approval_row_to_dict(row) for row in rows], "count": len(rows)}


@app.get("/api/approvals/{approval_id}")
def get_approval_detail(approval_id: str, includeHtml: bool = True):
    row = get_approval_or_404(approval_id)
    return approval_row_to_dict(row, include_html=includeHtml)


def contact_type_from_context(context: Dict[str, Any]) -> str:
    if normalize_email_identity(context.get("email")):
        return "email"
    if compact_text(context.get("phone")):
        return "phone"
    return "unknown"


def site_status_matches(status_filter: str, status_value: str) -> bool:
    normalized = compact_text(status_filter, "all").lower()
    status_upper = compact_text(status_value).upper()
    if normalized in {"all", ""}:
        return True
    if normalized == "pending":
        return status_upper in {"PENDING", "EXPORTING", "EXPORT_FAILED"}
    if normalized == "failed":
        return status_upper in {"FAILED", "EXPORT_FAILED", "DEPLOY_FAILED", "DEPLOYED_ZENDESK_FAILED"}
    if normalized in {"approved", "deployed", "live"}:
        return status_upper == "APPROVED"
    return status_upper == normalized.upper()


@app.get("/api/sites")
def list_sites(
    q: Optional[str] = None,
    status: str = "all",
    contactType: str = "all",
    noWebsiteOnly: bool = True,
    page: int = 1,
    pageSize: int = 20,
):
    safe_page = max(1, page)
    safe_page_size = max(1, min(pageSize, 100))
    query = compact_text(q).lower()
    contact_filter = compact_text(contactType, "all").lower()

    with get_pipeline_db() as db:
        rows = db.execute(
            """
            SELECT *
            FROM approval_records
            ORDER BY created_at DESC
            """
        ).fetchall()

    filtered: List[Dict[str, Any]] = []
    for row in rows:
        item = approval_row_to_dict(row)
        context = item.get("context") or {}
        deployment_history = item.get("deploymentHistory") or {}
        live_url = deployment_history.get("url")
        contact_type = contact_type_from_context(context)
        has_website = bool(normalize_url(context.get("website")) or normalize_domain(context.get("domain")))

        if noWebsiteOnly and has_website:
            continue
        if contact_filter != "all" and contact_type != contact_filter:
            continue
        if not site_status_matches(status, item.get("status", "")):
            continue

        searchable = " ".join(
            compact_text(value).lower()
            for value in [
                item.get("businessName"),
                item.get("status"),
                context.get("industry"),
                context.get("location"),
                context.get("email"),
                context.get("phone"),
                live_url,
            ]
        )
        if query and query not in searchable:
            continue

        item["liveUrl"] = live_url
        item["contactType"] = contact_type
        item["hasWebsite"] = has_website
        filtered.append(item)

    total = len(filtered)
    start = (safe_page - 1) * safe_page_size
    end = start + safe_page_size
    return {
        "sites": filtered[start:end],
        "count": len(filtered[start:end]),
        "total": total,
        "page": safe_page,
        "pageSize": safe_page_size,
        "totalPages": max(1, (total + safe_page_size - 1) // safe_page_size),
        "filters": {
            "q": q,
            "status": status,
            "contactType": contactType,
            "noWebsiteOnly": noWebsiteOnly,
        },
    }


@app.get("/api/settings/zendesk-fields")
def get_zendesk_fields():
    return {"fields": get_zendesk_field_settings(), "keys": ZENDESK_FIELD_KEYS, "updatedAt": now_iso()}


@app.put("/api/settings/zendesk-fields")
def put_zendesk_fields(request: ZendeskFieldSettingsRequest):
    return {"fields": save_zendesk_field_settings(request.fields), "keys": ZENDESK_FIELD_KEYS, "updatedAt": now_iso()}


def normalized_zendesk_subdomain(value: str) -> str:
    subdomain = compact_text(value).lower()
    subdomain = re.sub(r"^https?://", "", subdomain).split("/", 1)[0]
    return subdomain.removesuffix(".zendesk.com")


def zendesk_workspace_readiness() -> Dict[str, Any]:
    subdomain = compact_text(
        RUNTIME_INTEGRATION_OVERRIDES.get("ZENDESK_SUBDOMAIN", os.getenv("ZENDESK_SUBDOMAIN", ""))
    )
    username = compact_text(
        RUNTIME_INTEGRATION_OVERRIDES.get("ZENDESK_EMAIL", os.getenv("ZENDESK_EMAIL", ""))
    )
    api_token = compact_text(
        RUNTIME_INTEGRATION_OVERRIDES.get("ZENDESK_API_TOKEN", os.getenv("ZENDESK_API_TOKEN", ""))
    )
    connected = bool(subdomain and username and api_token)
    resources = list_zendesk_provisioned_resources()
    configuration = resources.get("configuration", {})
    metadata = configuration.get("metadata", {})
    configured_subdomain = compact_text(configuration.get("resourceId"))
    required_resources = ["form:email", "form:phone", *[f"field:{key}" for key in ZENDESK_FIELD_KEYS]]
    missing_resources = [
        key for key in required_resources
        if not compact_text(resources.get(key, {}).get("resourceId"))
    ]
    brand_id = compact_text(metadata.get("brandId"))
    same_instance = bool(connected and configured_subdomain == subdomain)
    workspace_ready = bool(connected and same_instance and brand_id and not missing_resources)
    missing_setup: List[str] = []
    if not connected:
        missing_setup.append("connection")
    if connected and not same_instance:
        missing_setup.append("instance_blueprint")
    if not brand_id:
        missing_setup.append("brand")
    if missing_resources:
        missing_setup.append("fields_and_forms")
    return {
        "workspaceReady": workspace_ready,
        "setupStatus": "READY" if workspace_ready else "CONNECTION_REQUIRED" if not connected else "PROVISIONING_REQUIRED",
        "missingSetup": list(dict.fromkeys(missing_setup)),
        "brandId": brand_id or None,
        "configuredSubdomain": configured_subdomain or None,
    }


def require_zendesk_workspace_ready() -> Dict[str, Any]:
    readiness = zendesk_workspace_readiness()
    if not readiness["workspaceReady"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ZENDESK_SETUP_REQUIRED",
                "message": (
                    "Connect Zendesk and provision the selected brand, fields, and two ticket forms "
                    "before creating or processing campaigns."
                ),
                **readiness,
            },
        )
    return readiness


def zendesk_connection_snapshot() -> Dict[str, Any]:
    subdomain = RUNTIME_INTEGRATION_OVERRIDES.get("ZENDESK_SUBDOMAIN", os.getenv("ZENDESK_SUBDOMAIN", ""))
    username = RUNTIME_INTEGRATION_OVERRIDES.get("ZENDESK_EMAIL", os.getenv("ZENDESK_EMAIL", ""))
    api_token = RUNTIME_INTEGRATION_OVERRIDES.get("ZENDESK_API_TOKEN", os.getenv("ZENDESK_API_TOKEN", ""))
    connected = bool(subdomain and username and api_token)
    return {
        "connected": connected,
        "subdomain": subdomain if connected else "",
        "username": username if connected else "",
        "tokenConfigured": bool(api_token),
        "maskedToken": mask_secret(api_token),
        "source": "session" if "ZENDESK_SUBDOMAIN" in RUNTIME_INTEGRATION_OVERRIDES else "environment" if connected else "none",
        "workspaceUrl": f"https://{subdomain}.zendesk.com" if connected else None,
        "updatedAt": now_iso(),
        **zendesk_workspace_readiness(),
    }


@app.get("/api/settings/zendesk-connection")
def get_zendesk_connection():
    return zendesk_connection_snapshot()


@app.put("/api/settings/zendesk-connection")
def put_zendesk_connection(request: ZendeskConnectionRequest):
    subdomain = normalized_zendesk_subdomain(request.subdomain)
    username = compact_text(request.username)
    api_token = compact_text(request.apiToken)
    if not subdomain or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", subdomain):
        raise HTTPException(status_code=400, detail="Enter a valid Zendesk subdomain, without the .zendesk.com suffix.")
    if not username or not api_token:
        raise HTTPException(status_code=400, detail="Zendesk username and API token are required.")

    if request.validateConnection:
        try:
            response = requests.get(
                f"https://{subdomain}.zendesk.com/api/v2/users/me.json",
                auth=(f"{username}/token", api_token),
                timeout=30,
            )
            response.raise_for_status()
        except Exception as error:
            raise HTTPException(status_code=401, detail=f"Zendesk connection failed: {sanitize_message(error)}")

    RUNTIME_INTEGRATION_OVERRIDES.update(
        {
            "ZENDESK_SUBDOMAIN": subdomain,
            "ZENDESK_EMAIL": username,
            "ZENDESK_API_TOKEN": api_token,
        }
    )
    log_event("info", "settings.zendesk.connected", "Zendesk session connection updated.", subdomain=subdomain)
    return zendesk_connection_snapshot()


@app.delete("/api/settings/zendesk-connection")
def delete_zendesk_connection():
    RUNTIME_INTEGRATION_OVERRIDES.update(
        {"ZENDESK_SUBDOMAIN": "", "ZENDESK_EMAIL": "", "ZENDESK_API_TOKEN": ""}
    )
    return zendesk_connection_snapshot()


def zendesk_setup_request_values(request: Optional[ZendeskSetupRequest] = None) -> Dict[str, Any]:
    persisted = list_zendesk_provisioned_resources().get("configuration", {}).get("metadata", {})
    source = request.model_dump() if request else persisted
    values = {**ZENDESK_SETUP_DEFAULTS, **{key: value for key, value in source.items() if value is not None}}
    values["brandId"] = compact_text(values.get("brandId")) or None
    values["createViews"] = bool(values.get("createViews", True))
    values["createAutomation"] = bool(values.get("createAutomation", False))
    values["webhookUrl"] = compact_text(values.get("webhookUrl")) or None
    return values


def zendesk_setup_resource_plan(values: Dict[str, Any]) -> Dict[str, Any]:
    resources = list_zendesk_provisioned_resources()
    field_ids = get_zendesk_field_settings()
    fields = []
    for definition in ZENDESK_FIELD_BLUEPRINT:
        saved = resources.get(f"field:{definition['key']}", {})
        resource_id = saved.get("resourceId") or field_ids.get(definition["key"])
        fields.append(
            {
                "key": definition["key"],
                "title": definition["title"],
                "type": definition["type"],
                "forms": definition["forms"],
                "description": definition["description"],
                "resourceId": resource_id,
                "status": "configured" if resource_id else "planned",
                "matchSource": "saved" if resource_id else None,
            }
        )

    def planned_resource(resource_key: str, resource_type: str, name: str) -> Dict[str, Any]:
        saved = resources.get(resource_key, {})
        return {
            "key": resource_key,
            "type": resource_type,
            "name": name,
            "resourceId": saved.get("resourceId"),
            "status": "configured" if saved.get("resourceId") else "planned",
            "matchSource": "saved" if saved.get("resourceId") else None,
        }

    return {
        "connected": zendesk_connection_snapshot()["connected"],
        "config": values,
        "fields": fields,
        "forms": [
            planned_resource("form:email", "ticket_form", values["emailFormName"]),
            planned_resource("form:phone", "ticket_form", values["callFormName"]),
        ],
        "views": [
            planned_resource("view:email", "view", values["emailViewName"]),
            planned_resource("view:phone", "view", values["callViewName"]),
            planned_resource("view:deployed", "view", values["deployedViewName"]),
        ],
        "automation": [
            planned_resource("webhook:actions", "webhook", values["webhookName"]),
            planned_resource("trigger:deploy_email", "trigger", "AI Site Factory - Deploy email lead"),
            planned_resource("trigger:deploy_phone", "trigger", "AI Site Factory - Deploy call lead"),
            planned_resource("trigger:cancel_email", "trigger", "AI Site Factory - Cancel email deployment"),
            planned_resource("trigger:cancel_phone", "trigger", "AI Site Factory - Cancel call deployment"),
            planned_resource("trigger:send_email", "trigger", "AI Site Factory - Send approved email"),
        ],
        "brands": ([
            {
                "id": compact_text(values.get("brandId")),
                "name": f"Configured brand (ID {compact_text(values.get('brandId'))})",
                "subdomain": zendesk_connection_snapshot().get("subdomain"),
                "default": False,
                "configured": True,
            }
        ] if compact_text(values.get("brandId")) else []),
        "tags": ZENDESK_SETUP_TAGS,
        "capabilities": {},
        "inspected": False,
        "disclaimer": [
            "Provisioning creates or reconciles custom ticket fields before it creates forms, views, and optional automation.",
            "No fields, forms, views, triggers, webhooks, brands, or tickets are deleted.",
            "Ticket forms require a Zendesk plan that supports multiple forms.",
            "Optional webhook triggers are created inactive and must be reviewed and enabled by a Zendesk administrator.",
        ],
    }


def zendesk_api_request(
    method: str,
    path_or_url: str,
    *,
    payload: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    subdomain, auth = zendesk_auth_context()
    url = path_or_url if path_or_url.startswith("http") else f"https://{subdomain}.zendesk.com/api/v2{path_or_url}"
    callback = getattr(requests, method.lower())
    kwargs: Dict[str, Any] = {"auth": auth, "headers": {"Content-Type": "application/json"}, "timeout": 30}
    if payload is not None:
        kwargs["json"] = payload
    if params is not None:
        kwargs["params"] = params
    response = callback(url, **kwargs)
    response.raise_for_status()
    try:
        return response.json() or {}
    except Exception:
        return {}


def zendesk_list_all(path: str, collection_key: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    next_url: Optional[str] = path
    pages = 0
    params: Optional[Dict[str, Any]] = {"per_page": 100}
    while next_url and pages < 10:
        payload = zendesk_api_request("get", next_url, params=params)
        items.extend(payload.get(collection_key) or [])
        links = payload.get("links") or {}
        next_url = payload.get("next_page") or links.get("next")
        params = None
        pages += 1
    return items


def inspect_zendesk_inventory() -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, str]]:
    specs = {
        "ticket_fields": ("/ticket_fields.json", "ticket_fields"),
        "ticket_forms": ("/ticket_forms.json", "ticket_forms"),
        "views": ("/views.json", "views"),
        "brands": ("/brands.json", "brands"),
        "webhooks": ("/webhooks", "webhooks"),
        "triggers": ("/triggers.json", "triggers"),
    }
    inventory: Dict[str, List[Dict[str, Any]]] = {}
    errors: Dict[str, str] = {}
    for key, (path, collection_key) in specs.items():
        try:
            inventory[key] = zendesk_list_all(path, collection_key)
        except Exception as error:
            inventory[key] = []
            errors[key] = sanitize_message(error)
    return inventory, errors


def reconcile_zendesk_field_settings_from_live_instance() -> Dict[str, Any]:
    """Replace stale ephemeral field IDs with marker-verified IDs from Zendesk."""
    if not zendesk_connection_snapshot()["connected"]:
        return {"reconciled": False, "reason": "zendesk_not_connected", "changed": {}}

    ticket_fields = zendesk_list_all("/ticket_fields.json", "ticket_fields")
    saved_settings = get_zendesk_field_settings()
    saved_resources = list_zendesk_provisioned_resources()
    resolved: Dict[str, Optional[str]] = {}
    changed: Dict[str, Dict[str, Optional[str]]] = {}
    missing: List[str] = []
    conflicts: List[str] = []

    for definition in ZENDESK_FIELD_BLUEPRINT:
        key = definition["key"]
        marker = f"[AI Site Factory key={key}]"
        saved_resource = saved_resources.get(f"field:{key}", {})
        match = next(
            (
                field
                for field in ticket_fields
                if marker in compact_text(field.get("agent_description"))
            ),
            None,
        )
        source: Optional[str] = "marker" if match else None
        if not match:
            match, source = zendesk_match_resource(
                ticket_fields,
                saved_resource.get("resourceId") or saved_settings.get(key),
                definition["title"],
                name_key="title",
            )
        if not match:
            missing.append(key)
            continue
        if not zendesk_field_types_compatible(definition["type"], compact_text(match.get("type"))):
            conflicts.append(key)
            continue

        field_id = compact_text(match.get("id"))
        if not field_id:
            missing.append(key)
            continue
        resolved[key] = field_id
        if compact_text(saved_settings.get(key)) != field_id:
            changed[key] = {
                "from": compact_text(saved_settings.get(key)) or None,
                "to": field_id,
                "matchSource": source,
            }
        save_zendesk_provisioned_resource(
            f"field:{key}",
            "ticket_field",
            field_id,
            definition["title"],
            {
                "type": compact_text(match.get("type"), definition["type"]),
                "blueprintType": definition["type"],
                "forms": definition["forms"],
                "reconciledFrom": source,
            },
        )

    if resolved:
        save_zendesk_field_settings(resolved)
    result = {
        "reconciled": not missing and not conflicts and len(resolved) == len(ZENDESK_FIELD_KEYS),
        "reason": "live_field_ids_reconciled" if not missing and not conflicts else "live_field_ids_partial",
        "resolvedCount": len(resolved),
        "changed": changed,
        "missing": missing,
        "conflicts": conflicts,
    }
    log_event(
        "info" if result["reconciled"] else "warning",
        "zendesk.field_ids_reconciled",
        "Managed Zendesk field IDs were reconciled against the live instance.",
        **result,
    )
    return result


def reconcile_zendesk_fields_on_startup() -> Dict[str, Any]:
    """Block application startup until live Zendesk field IDs are refreshed when configured."""
    if not env_enabled("ENABLE_ZENDESK_FIELD_RECONCILIATION", default=True):
        return {"reconciled": False, "reason": "zendesk_field_reconciliation_disabled", "changed": {}}
    try:
        return reconcile_zendesk_field_settings_from_live_instance()
    except Exception as error:
        message = sanitize_message(error)
        log_event(
            "error",
            "zendesk.field_ids_reconciliation_failed",
            "Live Zendesk field reconciliation failed; the last saved mapping remains in use.",
            error=message,
        )
        return {
            "reconciled": False,
            "reason": "zendesk_field_reconciliation_failed",
            "changed": {},
            "error": message,
        }


app.router.add_event_handler("startup", reconcile_zendesk_fields_on_startup)


def zendesk_match_resource(
    items: List[Dict[str, Any]],
    saved_id: Optional[Any],
    name: str,
    *,
    name_key: str = "name",
    marker: Optional[str] = None,
    marker_key: str = "description",
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    normalized_saved = compact_text(saved_id)
    if normalized_saved:
        match = next((item for item in items if compact_text(item.get("id")) == normalized_saved), None)
        if match:
            saved_name_matches = compact_text(match.get(name_key)).casefold() == compact_text(name).casefold()
            saved_marker_matches = bool(marker and marker in compact_text(match.get(marker_key)))
            if marker is None or saved_name_matches or saved_marker_matches:
                return match, "saved_id"
    if marker:
        match = next((item for item in items if marker in compact_text(item.get(marker_key))), None)
        if match:
            return match, "marker"
    normalized_name = compact_text(name).casefold()
    match = next((item for item in items if compact_text(item.get(name_key)).casefold() == normalized_name), None)
    return (match, "exact_name") if match else (None, None)


def zendesk_field_types_compatible(expected: str, existing: str) -> bool:
    if expected == existing:
        return True
    string_types = {"text", "textarea", "regexp"}
    if expected in string_types and existing in string_types:
        return True
    if expected == "tagger" and existing in string_types:
        return True
    return False


def build_zendesk_setup_inspection(
    values: Dict[str, Any],
    inventory: Dict[str, List[Dict[str, Any]]],
    errors: Dict[str, str],
) -> Dict[str, Any]:
    plan = zendesk_setup_resource_plan(values)
    resources = list_zendesk_provisioned_resources()
    fields = []
    for definition in ZENDESK_FIELD_BLUEPRINT:
        marker = f"[AI Site Factory key={definition['key']}]"
        saved = resources.get(f"field:{definition['key']}", {})
        match, source = zendesk_match_resource(
            inventory.get("ticket_fields", []),
            saved.get("resourceId") or get_zendesk_field_settings().get(definition["key"]),
            definition["title"],
            name_key="title",
            marker=marker,
            marker_key="agent_description",
        )
        status = "missing"
        if match:
            status = "ready" if zendesk_field_types_compatible(definition["type"], compact_text(match.get("type"))) else "conflict"
        fields.append(
            {
                "key": definition["key"],
                "title": definition["title"],
                "type": definition["type"],
                "forms": definition["forms"],
                "description": definition["description"],
                "resourceId": match.get("id") if match else None,
                "status": status,
                "matchSource": source,
                "existingType": match.get("type") if match else None,
                "adaptedType": bool(match and compact_text(match.get("type")) != definition["type"] and status == "ready"),
            }
        )
    plan["fields"] = fields

    named_specs = [
        ("forms", "form:email", "ticket_form", values["emailFormName"], "ticket_forms", "name"),
        ("forms", "form:phone", "ticket_form", values["callFormName"], "ticket_forms", "name"),
        ("views", "view:email", "view", values["emailViewName"], "views", "title"),
        ("views", "view:phone", "view", values["callViewName"], "views", "title"),
        ("views", "view:deployed", "view", values["deployedViewName"], "views", "title"),
        ("automation", "webhook:actions", "webhook", values["webhookName"], "webhooks", "name"),
        ("automation", "trigger:deploy_email", "trigger", "AI Site Factory - Deploy email lead", "triggers", "title"),
        ("automation", "trigger:deploy_phone", "trigger", "AI Site Factory - Deploy call lead", "triggers", "title"),
        ("automation", "trigger:cancel_email", "trigger", "AI Site Factory - Cancel email deployment", "triggers", "title"),
        ("automation", "trigger:cancel_phone", "trigger", "AI Site Factory - Cancel call deployment", "triggers", "title"),
        ("automation", "trigger:send_email", "trigger", "AI Site Factory - Send approved email", "triggers", "title"),
    ]
    grouped: Dict[str, List[Dict[str, Any]]] = {"forms": [], "views": [], "automation": []}
    for group, resource_key, resource_type, name, inventory_key, name_key in named_specs:
        saved = resources.get(resource_key, {})
        match, source = zendesk_match_resource(
            inventory.get(inventory_key, []), saved.get("resourceId"), name, name_key=name_key
        )
        grouped[group].append(
            {
                "key": resource_key,
                "type": resource_type,
                "name": name,
                "resourceId": match.get("id") if match else None,
                "status": "ready" if match else "missing",
                "matchSource": source,
                "active": match.get("active", match.get("status") == "active") if match else None,
            }
        )
    plan.update(grouped)
    plan["brands"] = [
        {"id": compact_text(brand.get("id")), "name": brand.get("name"), "subdomain": brand.get("subdomain"), "default": bool(brand.get("default"))}
        for brand in inventory.get("brands", [])
    ]
    plan["capabilities"] = {
        key: {"available": key not in errors, "message": errors.get(key)}
        for key in ["ticket_fields", "ticket_forms", "views", "brands", "webhooks", "triggers"]
    }
    plan["inspected"] = True
    plan["inspectedAt"] = now_iso()
    return plan


def zendesk_field_create_payload(definition: Dict[str, Any]) -> Dict[str, Any]:
    field: Dict[str, Any] = {
        "title": definition["title"],
        "type": definition["type"],
        "active": True,
        "required": False,
        "visible_in_portal": False,
        "editable_in_portal": False,
        "description": definition["description"],
        "agent_description": f"[AI Site Factory key={definition['key']}] {definition['description']}",
    }
    if definition.get("tag"):
        field["tag"] = definition["tag"]
    if definition.get("custom_field_options"):
        field["custom_field_options"] = definition["custom_field_options"]
    return {"ticket_field": field}


def zendesk_setup_webhook_url(value: Optional[str]) -> str:
    url = compact_text(value)
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.hostname in {"localhost", "127.0.0.1"}:
        raise HTTPException(status_code=400, detail="Automation requires a public HTTPS webhook URL; localhost preview URLs cannot receive Zendesk calls.")
    return url


def zendesk_upsert_named_resource(
    *,
    existing: Optional[Dict[str, Any]],
    resource_key: str,
    resource_type: str,
    name: str,
    create_path: str,
    update_path: str,
    root_key: str,
    payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], str]:
    if existing:
        response = zendesk_api_request("put", update_path.format(id=existing["id"]), payload=payload)
        item = response.get(root_key) or {**existing, **payload.get(root_key, {})}
        action = "updated"
    else:
        response = zendesk_api_request("post", create_path, payload=payload)
        item = response.get(root_key) or {}
        action = "created"
    resource_id = item.get("id") or (existing or {}).get("id")
    if not resource_id:
        raise RuntimeError(f"Zendesk did not return an ID for {name}.")
    save_zendesk_provisioned_resource(resource_key, resource_type, resource_id, name, metadata or {})
    return item, action


@app.get("/api/settings/zendesk-setup")
def get_zendesk_setup():
    plan = zendesk_setup_resource_plan(zendesk_setup_request_values())
    if not plan["connected"]:
        plan["brandsLoaded"] = False
        return plan
    try:
        live_brands = zendesk_list_all("/brands.json", "brands")
        plan["brands"] = [
            {
                "id": compact_text(brand.get("id")),
                "name": compact_text(brand.get("name"), f"Brand {brand.get('id')}"),
                "subdomain": brand.get("subdomain"),
                "default": bool(brand.get("default")),
                "active": bool(brand.get("active", True)),
            }
            for brand in live_brands
            if brand.get("id") is not None
        ]
        configured_brand_id = compact_text(plan.get("config", {}).get("brandId"))
        if configured_brand_id and not any(brand["id"] == configured_brand_id for brand in plan["brands"]):
            plan["brands"].append(
                {
                    "id": configured_brand_id,
                    "name": f"Previously configured brand (ID {configured_brand_id})",
                    "subdomain": zendesk_connection_snapshot().get("subdomain"),
                    "default": False,
                    "active": False,
                    "unavailable": True,
                }
            )
        plan["brandsLoaded"] = True
        plan["brandsLoadedAt"] = now_iso()
    except Exception as error:
        plan["brandsLoaded"] = False
        plan["brandLoadError"] = sanitize_message(error)
    return plan


@app.post("/api/settings/zendesk-setup/inspect")
def inspect_zendesk_setup(request: ZendeskSetupRequest):
    if not zendesk_connection_snapshot()["connected"]:
        raise HTTPException(status_code=409, detail="Connect a Zendesk instance before inspecting it.")
    values = zendesk_setup_request_values(request)
    inventory, errors = inspect_zendesk_inventory()
    if "ticket_fields" in errors:
        raise HTTPException(status_code=502, detail=f"Zendesk ticket fields could not be inspected: {errors['ticket_fields']}")
    return build_zendesk_setup_inspection(values, inventory, errors)


@app.post("/api/settings/zendesk-setup/provision")
def provision_zendesk_setup(request: ZendeskSetupRequest):
    if not request.confirm:
        raise HTTPException(status_code=400, detail="Confirm the Zendesk instance changes before provisioning.")
    if not zendesk_connection_snapshot()["connected"]:
        raise HTTPException(status_code=409, detail="Connect a Zendesk instance before provisioning it.")
    values = zendesk_setup_request_values(request)
    for key in ["emailFormName", "callFormName", "emailViewName", "callViewName", "deployedViewName", "webhookName"]:
        if not compact_text(values.get(key)):
            raise HTTPException(status_code=400, detail=f"{key} cannot be blank.")
    if values["emailFormName"].casefold() == values["callFormName"].casefold():
        raise HTTPException(status_code=400, detail="Email and call forms must have different names.")
    if not values["brandId"]:
        raise HTTPException(status_code=400, detail="Select an existing Zendesk brand before provisioning the campaign workspace.")

    inventory, errors = inspect_zendesk_inventory()
    required_capabilities = ["ticket_fields", "ticket_forms"]
    if values["createViews"]:
        required_capabilities.append("views")
    if values["brandId"]:
        required_capabilities.append("brands")
    if values["createAutomation"]:
        required_capabilities.extend(["webhooks", "triggers"])
    unavailable = [key for key in required_capabilities if key in errors]
    if unavailable:
        detail = "; ".join(f"{key}: {errors[key]}" for key in unavailable)
        raise HTTPException(status_code=409, detail=f"Zendesk setup cannot continue because required features are unavailable. {detail}")

    inspection = build_zendesk_setup_inspection(values, inventory, errors)
    conflicts = [field for field in inspection["fields"] if field["status"] == "conflict"]
    if conflicts:
        labels = ", ".join(f"{item['title']} ({item['existingType']} vs {item['type']})" for item in conflicts)
        raise HTTPException(status_code=409, detail=f"Resolve these same-name field type conflicts before provisioning: {labels}")
    if values["brandId"] and not any(brand["id"] == values["brandId"] for brand in inspection["brands"]):
        raise HTTPException(status_code=400, detail="The selected Zendesk brand no longer exists or is not accessible.")

    webhook_url = None
    webhook_secret = None
    if values["createAutomation"]:
        webhook_url = zendesk_setup_webhook_url(values["webhookUrl"])
        try:
            webhook_secret = require_env("ZENDESK_WEBHOOK_SECRET")
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error))

    actions: List[Dict[str, Any]] = []
    field_ids: Dict[str, str] = {}
    existing_fields_by_key = {item["key"]: item for item in inspection["fields"]}
    for definition in ZENDESK_FIELD_BLUEPRINT:
        inspected = existing_fields_by_key[definition["key"]]
        if inspected.get("resourceId"):
            field_id = compact_text(inspected["resourceId"])
            effective_type = compact_text(inspected.get("existingType"), definition["type"])
            action = "reused"
        else:
            response = zendesk_api_request("post", "/ticket_fields.json", payload=zendesk_field_create_payload(definition))
            created = response.get("ticket_field") or {}
            field_id = compact_text(created.get("id"))
            if not field_id:
                raise RuntimeError(f"Zendesk did not return an ID for {definition['title']}.")
            effective_type = definition["type"]
            action = "created"
            inventory["ticket_fields"].append(created)
        field_ids[definition["key"]] = field_id
        save_zendesk_provisioned_resource(
            f"field:{definition['key']}", "ticket_field", field_id, definition["title"],
            {"type": effective_type, "blueprintType": definition["type"], "forms": definition["forms"]},
        )
        actions.append({"resourceType": "ticket_field", "key": definition["key"], "name": definition["title"], "resourceId": field_id, "action": action})
    save_zendesk_field_settings(field_ids)

    system_type_order = ["subject", "description", "status", "priority", "tickettype", "group", "assignee"]
    system_ids: List[Any] = []
    for field_type in system_type_order:
        match = next((field for field in inventory["ticket_fields"] if field.get("type") == field_type), None)
        if match and match.get("id") not in system_ids:
            system_ids.append(match["id"])

    form_matches = {item["key"]: item for item in inspection["forms"]}
    form_objects: Dict[str, Dict[str, Any]] = {}
    for channel, resource_key, name in [
        ("email", "form:email", values["emailFormName"]),
        ("phone", "form:phone", values["callFormName"]),
    ]:
        custom_ids = [field_ids[item["key"]] for item in ZENDESK_FIELD_BLUEPRINT if channel in item["forms"]]
        ticket_field_ids: List[Any] = [*system_ids, *[int(value) if value.isdigit() else value for value in custom_ids]]
        form_payload: Dict[str, Any] = {
            "name": name,
            "display_name": name,
            "active": True,
            "end_user_visible": False,
            "in_all_brands": False,
            "ticket_field_ids": ticket_field_ids,
        }
        form_payload["restricted_brand_ids"] = [int(values["brandId"]) if values["brandId"].isdigit() else values["brandId"]]
        existing = next((item for item in inventory["ticket_forms"] if compact_text(item.get("id")) == compact_text(form_matches[resource_key].get("resourceId"))), None)
        form, action = zendesk_upsert_named_resource(
            existing=existing,
            resource_key=resource_key,
            resource_type="ticket_form",
            name=name,
            create_path="/ticket_forms.json",
            update_path="/ticket_forms/{id}.json",
            root_key="ticket_form",
            payload={"ticket_form": form_payload},
            metadata={"channel": channel, "brandId": values["brandId"], "fieldKeys": [item["key"] for item in ZENDESK_FIELD_BLUEPRINT if channel in item["forms"]]},
        )
        form_objects[channel] = form
        actions.append({"resourceType": "ticket_form", "key": resource_key, "name": name, "resourceId": compact_text(form.get("id")), "action": action})

    if values["createViews"]:
        view_matches = {item["key"]: item for item in inspection["views"]}
        view_specs = [
            ("view:email", values["emailViewName"], "asf_form_email_lead", ["businessName", "campaignName", "contactEmail", "leadStatus", "liveUrl"]),
            ("view:phone", values["callViewName"], "asf_form_call_lead", ["businessName", "campaignName", "contactPhone", "phoneCallStatus", "liveUrl"]),
            ("view:deployed", values["deployedViewName"], "asf_deployed", ["businessName", "campaignName", "contactChannel", "liveUrl"]),
        ]
        for resource_key, name, required_tag, column_keys in view_specs:
            columns: List[Any] = ["status", "description", "assignee", "updated"]
            columns.extend(int(field_ids[key]) if field_ids[key].isdigit() else field_ids[key] for key in column_keys)
            view_payload = {
                "title": name,
                "description": f"[AI Site Factory key={resource_key}] Managed queue. Filter tag: {required_tag}.",
                "active": True,
                "all": [{"field": "status", "operator": "less_than", "value": "solved"}],
                "any": [{"field": "current_tags", "operator": "includes", "value": required_tag}],
                "output": {"columns": columns[:10], "sort_by": "updated", "sort_order": "desc"},
            }
            view_payload["all"].append({"field": "brand_id", "operator": "is", "value": values["brandId"]})
            existing = next((item for item in inventory["views"] if compact_text(item.get("id")) == compact_text(view_matches[resource_key].get("resourceId"))), None)
            view, action = zendesk_upsert_named_resource(
                existing=existing, resource_key=resource_key, resource_type="view", name=name,
                create_path="/views.json", update_path="/views/{id}.json", root_key="view",
                payload={"view": view_payload}, metadata={"tag": required_tag, "columnKeys": column_keys},
            )
            actions.append({"resourceType": "view", "key": resource_key, "name": name, "resourceId": compact_text(view.get("id")), "action": action})

    if values["createAutomation"]:
        automation_matches = {item["key"]: item for item in inspection["automation"]}
        existing_webhook = next((item for item in inventory["webhooks"] if compact_text(item.get("id")) == compact_text(automation_matches["webhook:actions"].get("resourceId"))), None)
        webhook_payload = {
            "name": values["webhookName"],
            "description": "[AI Site Factory key=webhook:actions] Receives deploy and email approval actions.",
            "status": "active",
            "endpoint": webhook_url,
            "http_method": "POST",
            "request_format": "json",
            "subscriptions": ["conditional_ticket_events"],
            "authentication": {"type": "api_key", "data": {"name": "x-ai-site-factory-secret", "value": webhook_secret}, "add_position": "header"},
        }
        webhook, action = zendesk_upsert_named_resource(
            existing=existing_webhook, resource_key="webhook:actions", resource_type="webhook", name=values["webhookName"],
            create_path="/webhooks", update_path="/webhooks/{id}", root_key="webhook",
            payload={"webhook": webhook_payload}, metadata={"endpoint": webhook_url, "status": "active"},
        )
        webhook_id = compact_text(webhook.get("id"))
        actions.append({"resourceType": "webhook", "key": "webhook:actions", "name": values["webhookName"], "resourceId": webhook_id, "action": action})

        approval_placeholder = f"{{{{ticket.ticket_field_{field_ids['approvalId']}}}}}"
        canonical_placeholder = f"{{{{ticket.ticket_field_{field_ids['canonicalLeadKey']}}}}}"
        trigger_specs = [
            ("trigger:deploy_email", "AI Site Factory - Deploy email lead", "email", form_objects["email"]["id"], "deployRequested", "asf_deploy_email_fired", "deploy_site"),
            ("trigger:deploy_phone", "AI Site Factory - Deploy call lead", "phone", form_objects["phone"]["id"], "deployRequested", "asf_deploy_phone_fired", "deploy_site"),
            ("trigger:cancel_email", "AI Site Factory - Cancel email deployment", "email", form_objects["email"]["id"], "deployRequested", "asf_cancel_email_fired", "cancel_deployment"),
            ("trigger:cancel_phone", "AI Site Factory - Cancel call deployment", "phone", form_objects["phone"]["id"], "deployRequested", "asf_cancel_phone_fired", "cancel_deployment"),
            ("trigger:send_email", "AI Site Factory - Send approved email", "email", form_objects["email"]["id"], "emailSendRequested", "asf_email_send_fired", "send_email"),
        ]
        for resource_key, name, channel, form_id, checkbox_key, fired_tag, webhook_action in trigger_specs:
            existing = next(
                (
                    item
                    for item in inventory["triggers"]
                    if compact_text(item.get("id"))
                    == compact_text(automation_matches[resource_key].get("resourceId"))
                ),
                None,
            )
            # Zendesk administrators activate these triggers only after reviewing
            # their conditions and webhook contract. A later idempotent setup run
            # must not silently undo that production decision. Brand-new triggers
            # still start inactive, while existing resources retain their current
            # activation state.
            trigger_active = bool(existing.get("active")) if existing else False
            checkbox_value = "false" if webhook_action == "cancel_deployment" else "true"
            conditions = [
                {"field": "status", "operator": "less_than", "value": "solved"},
                {"field": "ticket_form_id", "operator": "is", "value": compact_text(form_id)},
                {"field": f"custom_fields_{field_ids[checkbox_key]}", "operator": "is", "value": checkbox_value},
                {"field": "current_tags", "operator": "not_includes", "value": fired_tag},
            ]
            if webhook_action in {"send_email", "cancel_deployment"}:
                conditions.append({"field": "current_tags", "operator": "includes", "value": "asf_deployed"})
            webhook_body = json.dumps(
                {"action": webhook_action, "approvalId": approval_placeholder, "canonicalLeadKey": canonical_placeholder, "zendeskTicketId": "{{ticket.id}}", "channel": channel},
                separators=(",", ":"),
            )
            trigger_payload = {
                "title": name,
                "description": (
                    f"[AI Site Factory key={resource_key}] Managed ticket-action trigger. "
                    "New triggers start inactive for administrator review; later setup runs preserve the current activation state."
                ),
                "active": trigger_active,
                "conditions": {"all": conditions, "any": []},
                "actions": [
                    {"field": "notification_webhook", "value": [webhook_id, webhook_body]},
                    {"field": "current_tags", "value": fired_tag},
                ],
            }
            trigger, trigger_action = zendesk_upsert_named_resource(
                existing=existing, resource_key=resource_key, resource_type="trigger", name=name,
                create_path="/triggers.json", update_path="/triggers/{id}.json", root_key="trigger",
                payload={"trigger": trigger_payload}, metadata={"active": trigger_active, "channel": channel, "action": webhook_action, "firedTag": fired_tag},
            )
            actions.append({"resourceType": "trigger", "key": resource_key, "name": name, "resourceId": compact_text(trigger.get("id")), "action": trigger_action})

    save_zendesk_provisioned_resource(
        "configuration", "configuration", zendesk_connection_snapshot()["subdomain"], "AI Site Factory Zendesk setup",
        {**values, "confirm": False, "provisionedAt": now_iso()},
    )
    refreshed_inventory, refreshed_errors = inspect_zendesk_inventory()
    result = build_zendesk_setup_inspection(values, refreshed_inventory, refreshed_errors)
    result["actions"] = actions
    result["provisionedAt"] = now_iso()
    result["message"] = "Zendesk fields, forms, and selected supporting resources are configured. Automation triggers remain inactive until an administrator enables them."
    return result


def campaign_request_idempotency_key(request: CampaignIntakeRequest, channels: List[str]) -> str:
    supplied = compact_text(request.idempotencyKey)
    if supplied:
        material = f"request:{supplied}"
    elif request.forceRefresh and "forceRefresh" in request.model_fields_set:
        material = f"force-refresh:{uuid4()}"
    else:
        material = json.dumps(
            {
                "campaignName": compact_text(request.campaignName).lower(),
                "presetId": compact_text(request.presetId).lower(),
                "industry": compact_text(request.industry).lower(),
                "location": compact_text(request.location).lower(),
                "query": compact_text(request.query).lower(),
                "limit": int(request.limit),
                "channels": sorted(channels),
                "autoGenerateMetadata": bool(request.autoGenerateMetadata),
                "forceRefresh": bool(request.forceRefresh),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    subdomain = compact_text(zendesk_connection_snapshot().get("subdomain")).lower()
    digest = hashlib.sha256(f"{subdomain}:{material}".encode("utf-8")).hexdigest()
    return f"campaign:{digest}"


def uploaded_campaign_idempotency_key(
    content: bytes,
    campaign_name: str,
    industry: str,
    location: str,
    channels: List[str],
) -> str:
    metadata = json.dumps(
        {
            "campaignName": compact_text(campaign_name).lower(),
            "industry": compact_text(industry).lower(),
            "location": compact_text(location).lower(),
            "channels": sorted(channels),
            "subdomain": compact_text(zendesk_connection_snapshot().get("subdomain")).lower(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(metadata + b"\0" + content).hexdigest()
    return f"upload:{digest}"


def campaign_summary_from_row(db: sqlite3.Connection, row: sqlite3.Row, include_leads: bool = False) -> Dict[str, Any]:
    email_rows = db.execute(
        "SELECT * FROM campaign_email_leads WHERE campaign_id = ? ORDER BY created_at DESC",
        (row["id"],),
    ).fetchall()
    call_rows = db.execute(
        "SELECT * FROM campaign_call_leads WHERE campaign_id = ? ORDER BY created_at DESC",
        (row["id"],),
    ).fetchall()
    deployment_rows = db.execute(
        "SELECT * FROM campaign_deployments WHERE campaign_id = ? ORDER BY created_at DESC",
        (row["id"],),
    ).fetchall()
    channel_rows = [*email_rows, *call_rows]
    total_channel_leads = len(channel_rows)
    live_channel_leads = sum(1 for item in channel_rows if item["status"] in {"DEPLOYED", "REUSED_DEPLOYMENT"})
    deployed = sum(1 for item in deployment_rows if item["status"] == "DEPLOYED")
    reused_deployments = sum(1 for item in deployment_rows if item["status"] == "REUSED_DEPLOYMENT")
    failed = sum(1 for item in deployment_rows if item["status"] in {"FAILED", "GENERATION_FAILED", "DEPLOY_FAILED"})
    deploy_requests = sum(1 for item in deployment_rows if item["requested_at"])
    ai_generations = sum(item["ai_generation_count"] or 0 for item in deployment_rows)
    repos_created = sum(item["repo_created"] or 0 for item in deployment_rows)
    pending = max(0, len(deployment_rows) - deployed - reused_deployments - failed)
    ticket_count = sum(1 for item in [*email_rows, *call_rows] if item["ticket_id"])
    persisted_status = compact_text(row["status"], "ACTIVE").upper()
    status = (
        "COMPLETED" if total_channel_leads and live_channel_leads == total_channel_leads
        else "NEEDS_ATTENTION" if failed
        else persisted_status if persisted_status in {
            "INTAKE_PENDING", "INTAKE_PROCESSING", "INTAKE_PARTIAL",
            "TICKET_SYNC_PENDING", "TICKET_SYNCING", "TICKET_SYNC_PARTIAL",
            "BLOCKED_SETUP", "IMPORT_QUEUED", "IMPORT_PROCESSING", "IMPORT_FAILED",
        }
        else persisted_status if not total_channel_leads
        else "ACTIVE"
    )

    result: Dict[str, Any] = {
        "campaignId": row["id"],
        "idempotencyKey": row["idempotency_key"],
        "campaignName": row["name"],
        "batchId": row["batch_id"],
        "presetId": row["preset_id"],
        "industry": row["industry"],
        "query": row["query"],
        "location": row["location"],
        "requestedCount": row["requested_count"],
        "discoveredLeads": row["discovered_count"],
        "channels": [value for value in compact_text(row["channel_filter"]).split(",") if value],
        "status": status,
        "warnings": safe_json_loads(row["warnings_json"], []),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "metrics": {
            "emailLeads": len(email_rows),
            "callLeads": len(call_rows),
            "channelLeads": total_channel_leads,
            "zendeskTickets": ticket_count,
            "deployRequests": deploy_requests,
            "aiGenerations": ai_generations,
            "reposCreated": repos_created,
            "deployed": deployed,
            "reusedDeployments": reused_deployments,
            "liveChannelLeads": live_channel_leads,
            "pending": pending,
            "failed": failed,
            "deploymentRate": round((deployed / total_channel_leads) * 100, 1) if total_channel_leads else 0,
        },
        "funnel": [
            {"label": "Discovered", "value": row["discovered_count"]},
            {"label": "Channel records", "value": total_channel_leads},
            {"label": "Zendesk", "value": ticket_count},
            {"label": "Deploy requested", "value": deploy_requests},
            {"label": "AI generated", "value": ai_generations},
            {"label": "Repos created", "value": repos_created},
            {"label": "Live", "value": deployed},
        ],
    }
    if include_leads:
        result["emailLeads"] = [
            {
                "leadId": item["id"],
                "approvalId": item["approval_id"],
                "canonicalLeadKey": item["canonical_lead_key"],
                "businessName": item["business_name"],
                "contactName": item["contact_name"],
                "email": item["email"],
                "sourceUrl": item["source_url"],
                "status": item["status"],
                "deployRequested": bool(item["deploy_requested"]),
                "ticketId": item["ticket_id"],
                "deploymentId": item["deployment_id"],
                "fields": safe_json_loads(item["fields_json"], {}),
            }
            for item in email_rows
        ]
        result["callLeads"] = [
            {
                "leadId": item["id"],
                "approvalId": item["approval_id"],
                "canonicalLeadKey": item["canonical_lead_key"],
                "businessName": item["business_name"],
                "contactName": item["contact_name"],
                "phone": item["phone"],
                "sourceUrl": item["source_url"],
                "status": item["status"],
                "deployRequested": bool(item["deploy_requested"]),
                "ticketId": item["ticket_id"],
                "deploymentId": item["deployment_id"],
                "fields": safe_json_loads(item["fields_json"], {}),
            }
            for item in call_rows
        ]
        result["deployments"] = [
            {
                "deploymentId": item["id"],
                "approvalId": item["approval_id"],
                "canonicalLeadKey": item["canonical_lead_key"],
                "channel": item["channel"],
                "status": item["status"],
                "aiGenerationCount": item["ai_generation_count"],
                "repoCreated": bool(item["repo_created"]),
                "repoUrl": item["repo_url"],
                "liveUrl": item["live_url"],
                "requestedAt": item["requested_at"],
                "completedAt": item["completed_at"],
                "error": item["error"],
            }
            for item in deployment_rows
        ]
    return result


@app.get("/api/campaigns")
def list_campaigns(limit: int = 50, includeLegacy: bool = True):
    safe_limit = max(1, min(limit, 200))
    with get_pipeline_db() as db:
        where_clause = "" if includeLegacy else "WHERE idempotency_key IS NOT NULL"
        rows = db.execute(
            f"SELECT * FROM campaigns {where_clause} ORDER BY created_at DESC LIMIT ?", (safe_limit,)
        ).fetchall()
        campaigns = [campaign_summary_from_row(db, row) for row in rows]
    totals = {
        "campaigns": len(campaigns),
        "leads": sum(item["metrics"]["channelLeads"] for item in campaigns),
        "deployments": sum(item["metrics"]["deployed"] for item in campaigns),
        "pending": sum(item["metrics"]["pending"] for item in campaigns),
        "aiGenerations": sum(item["metrics"]["aiGenerations"] for item in campaigns),
        "reposCreated": sum(item["metrics"]["reposCreated"] for item in campaigns),
    }
    return {"campaigns": campaigns, "totals": totals, "generatedAt": now_iso()}


@app.post("/api/campaigns/backfill")
def backfill_campaigns():
    if not env_enabled("ENABLE_LEGACY_CAMPAIGN_BACKFILL"):
        raise HTTPException(status_code=403, detail="Legacy campaign backfill is disabled.")
    stats = backfill_legacy_campaign_data()
    return {"backfill": stats, **list_campaigns(200)}


@app.post("/api/campaigns/restore-seed")
def restore_campaign_seed():
    """Manually retry the safe empty-database restore without overwriting live records."""
    if not pipeline_seed_restore_enabled():
        raise HTTPException(status_code=403, detail="Pipeline seed restoration is disabled.")
    result = restore_pipeline_seed_if_empty()
    return {"seed": result, **list_campaigns(200)}


def managed_zendesk_restore_candidates(
    request: ZendeskCampaignRestoreRequest,
) -> List[Dict[str, Any]]:
    tickets: List[Dict[str, Any]] = []
    next_url: Optional[str] = "/search.json"
    params: Optional[Dict[str, Any]] = {
        "query": "type:ticket tags:asf_managed tags:asf_intake",
        "per_page": 100,
        "sort_by": "created_at",
        "sort_order": "asc",
    }
    requested_ticket_ids = {int(ticket_id) for ticket_id in request.ticketIds}
    pages = 0
    while next_url and pages < 10 and len(tickets) < request.maxTickets:
        payload = zendesk_api_request("get", next_url, params=params)
        for ticket in payload.get("results") or []:
            ticket_id = int(ticket.get("id") or 0)
            tags = {compact_text(tag) for tag in (ticket.get("tags") or []) if compact_text(tag)}
            channel = (
                "email" if "asf_channel_email" in tags
                else "phone" if "asf_channel_phone" in tags
                else ""
            )
            if not ticket_id or channel not in {"email", "phone"}:
                continue
            if requested_ticket_ids and ticket_id not in requested_ticket_ids:
                continue
            if not request.includePendingIntake and not tags.intersection(ZENDESK_RESTORE_WORKFLOW_TAGS):
                continue
            tickets.append(ticket)
            if len(tickets) >= request.maxTickets:
                break
        links = payload.get("links") or {}
        next_url = payload.get("next_page") or links.get("next")
        params = None
        pages += 1

    if requested_ticket_ids:
        found = {int(ticket["id"]) for ticket in tickets}
        missing = sorted(requested_ticket_ids - found)
        if missing:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "ZENDESK_RESTORE_TICKETS_NOT_FOUND",
                    "message": "Some requested tickets were not eligible managed intake tickets.",
                    "ticketIds": missing,
                },
            )
    return tickets


def managed_zendesk_restore_preview(ticket: Dict[str, Any]) -> Dict[str, Any]:
    tags = {compact_text(tag) for tag in (ticket.get("tags") or []) if compact_text(tag)}
    channel = "email" if "asf_channel_email" in tags else "phone"
    contract = require_zendesk_ticket_contract(channel)
    field_values = {
        compact_text(item.get("id")): item.get("value")
        for item in (ticket.get("custom_fields") or [])
    }

    def managed_value(key: str) -> Any:
        return field_values.get(compact_text(contract["fieldIds"].get(key)))

    state = managed_zendesk_restore_state(
        tags,
        managed_value("deployRequested"),
        compact_text(managed_value("liveUrl")),
    )
    return {
        "ticketId": int(ticket["id"]),
        "campaignId": compact_text(managed_value("campaignId")) or None,
        "campaignName": compact_text(managed_value("campaignName")) or None,
        "approvalId": compact_text(managed_value("approvalId")) or None,
        "businessName": compact_text(managed_value("businessName")) or None,
        "channel": channel,
        "state": state["deploymentStatus"],
    }


@app.post("/api/campaigns/restore-zendesk")
def restore_campaigns_from_zendesk(request: ZendeskCampaignRestoreRequest):
    """Rebuild local campaign state from managed tickets without mutating Zendesk."""
    tickets = managed_zendesk_restore_candidates(request)
    previews: List[Dict[str, Any]] = []
    preview_errors: List[Dict[str, Any]] = []
    for ticket in tickets:
        try:
            previews.append(managed_zendesk_restore_preview(ticket))
        except Exception as error:
            preview_errors.append(
                {
                    "ticketId": int(ticket.get("id") or 0),
                    "error": sanitize_message(error),
                }
            )

    if not request.confirm:
        return {
            "status": "DRY_RUN",
            "readOnly": True,
            "candidateCount": len(tickets),
            "restorableCount": len(previews),
            "errorCount": len(preview_errors),
            "candidates": previews,
            "errors": preview_errors,
        }

    restored: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for ticket in tickets:
        tags = {compact_text(tag) for tag in (ticket.get("tags") or []) if compact_text(tag)}
        channel = "email" if "asf_channel_email" in tags else "phone"
        try:
            row = recover_managed_zendesk_webhook_approval(
                ZendeskWebhookRequest(
                    action="restore_managed_ticket",
                    zendeskTicketId=int(ticket["id"]),
                    channel=channel,
                    actor="Zendesk campaign recovery",
                ),
                ticket_override=ticket,
                restore_current_state=True,
            )
            restored.append(
                {
                    "ticketId": int(ticket["id"]),
                    "approvalId": row["id"],
                    "campaignId": safe_json_loads(row["context_json"], {}).get("campaignId"),
                    "businessName": row["business_name"],
                    "channel": channel,
                    "status": row["status"],
                }
            )
        except Exception as error:
            errors.append(
                {
                    "ticketId": int(ticket.get("id") or 0),
                    "error": sanitize_message(error),
                }
            )

    log_event(
        "warning",
        "campaigns.zendesk_restore",
        "Recovered local campaign state from managed Zendesk tickets without changing Zendesk.",
        candidateCount=len(tickets),
        restoredCount=len(restored),
        errorCount=len(errors),
    )
    return {
        "status": "RESTORED" if restored and not errors else "PARTIAL" if restored else "FAILED",
        "readOnlyZendesk": True,
        "candidateCount": len(tickets),
        "restoredCount": len(restored),
        "errorCount": len(errors),
        "restored": restored,
        "errors": errors,
        **list_campaigns(200),
    }


def managed_zendesk_startup_ticket_ids() -> List[int]:
    configured_ticket_ids = []
    for value in re.split(r"[,\s]+", compact_text(os.getenv("ZENDESK_CAMPAIGN_RECOVERY_TICKET_IDS"))):
        if value.isdigit() and int(value) > 0 and int(value) not in configured_ticket_ids:
            configured_ticket_ids.append(int(value))
    return configured_ticket_ids[:1000]


@app.on_event("startup")
def bootstrap_managed_zendesk_campaigns_on_startup() -> None:
    """Recover deploy/cancel workflow records when an ephemeral Render database starts empty."""
    configured = os.getenv("ENABLE_ZENDESK_CAMPAIGN_RECOVERY_ON_STARTUP")
    enabled = env_enabled("ENABLE_ZENDESK_CAMPAIGN_RECOVERY_ON_STARTUP") if configured is not None else env_enabled("RENDER")
    if not enabled:
        return
    with get_pipeline_db() as db:
        existing_campaigns = db.execute("SELECT COUNT(*) AS count FROM campaigns").fetchone()["count"]
    configured_ticket_ids = managed_zendesk_startup_ticket_ids()
    if existing_campaigns and not configured_ticket_ids:
        return
    try:
        results = []
        if not existing_campaigns:
            results.append(
                restore_campaigns_from_zendesk(
                    ZendeskCampaignRestoreRequest(confirm=True, includePendingIntake=False, maxTickets=200)
                )
            )
        if configured_ticket_ids:
            results.append(
                restore_campaigns_from_zendesk(
                    ZendeskCampaignRestoreRequest(
                        confirm=True,
                        includePendingIntake=True,
                        ticketIds=configured_ticket_ids,
                        maxTickets=max(200, len(configured_ticket_ids)),
                    )
                )
            )
        log_event(
            "info",
            "campaigns.zendesk_startup_recovery",
            "Checked managed Zendesk workflow tickets after starting with an empty campaign database.",
            restoredCount=sum(result.get("restoredCount", 0) for result in results),
            errorCount=sum(result.get("errorCount", 0) for result in results),
            configuredTicketCount=len(configured_ticket_ids),
        )
    except Exception as error:
        log_event(
            "warning",
            "campaigns.zendesk_startup_recovery_failed",
            str(error),
        )


def campaign_upload_dir() -> str:
    directory = os.getenv(
        "CAMPAIGN_UPLOAD_DIR",
        os.path.join(os.path.dirname(pipeline_db_path()), "uploads"),
    )
    os.makedirs(directory, exist_ok=True)
    return directory


def parse_uploaded_lead_file(content: bytes, file_name: str) -> Tuple[str, List[Dict[str, Any]]]:
    extension = os.path.splitext(file_name)[1].lower()
    if extension not in {".csv", ".json", ".jsonl"}:
        raise HTTPException(status_code=400, detail="Upload a .csv, .json, or .jsonl lead file.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("utf-16")
        except UnicodeDecodeError as error:
            raise HTTPException(status_code=400, detail="The lead file must use UTF-8 or UTF-16 text encoding.") from error

    rows: Any
    try:
        if extension == ".csv":
            rows = list(csv.DictReader(io.StringIO(text)))
        elif extension == ".jsonl":
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            payload = json.loads(text)
            if isinstance(payload, list):
                rows = payload
            elif isinstance(payload, dict):
                rows = payload.get("leads") or payload.get("items") or payload.get("results") or payload.get("data")
            else:
                rows = None
    except (csv.Error, json.JSONDecodeError) as error:
        raise HTTPException(status_code=400, detail=f"The lead file could not be parsed: {sanitize_message(error)}") from error
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=400, detail="The lead file does not contain any rows.")
    if not all(isinstance(row, dict) for row in rows):
        raise HTTPException(status_code=400, detail="Each uploaded lead must be an object or CSV row.")
    return extension.removeprefix("."), rows


def uploaded_row_value(row: Dict[str, Any], keys: List[str], fallback: Optional[str] = None) -> Optional[str]:
    normalized = {
        re.sub(r"[^a-z0-9]", "", compact_text(key).lower()): value
        for key, value in row.items()
    }
    for key in keys:
        value = normalized.get(re.sub(r"[^a-z0-9]", "", key.lower()))
        if isinstance(value, list) and value:
            value = value[0]
        text = compact_text(value)
        if text:
            return text
    return fallback


def normalize_uploaded_lead(
    row: Dict[str, Any],
    row_number: int,
    default_industry: str,
    default_location: str,
) -> DiscoveredLead:
    business_name = uploaded_row_value(
        row,
        ["businessName", "business_name", "companyName", "company_name", "title", "name"],
    )
    if not business_name:
        raise ValueError(f"Row {row_number} is missing a business name.")
    email = normalize_email_identity(
        uploaded_row_value(row, ["email", "contactEmail", "contact_email", "mail"])
        or extract_email_from_item(row)
    )
    phone_text = uploaded_row_value(
        row,
        [
            "phone",
            "phoneUnformatted",
            "phone_unformatted",
            "phoneNumber",
            "phone_number",
            "contactPhone",
            "contact_phone",
            "telephone",
        ],
    )
    phone = extract_phone_from_text(phone_text or "") or phone_text
    website = normalize_url(uploaded_row_value(row, ["website", "websiteUrl", "website_url"]))
    domain = normalize_domain(uploaded_row_value(row, ["domain"]) or domain_from_url(website))
    category = uploaded_row_value(
        row,
        ["industry", "category", "categoryName", "category_name", "businessCategory"],
        default_industry,
    )
    address = uploaded_row_value(row, ["address", "fullAddress", "full_address", "streetAddress"])
    location = uploaded_row_value(
        row,
        ["location", "city", "municipality", "province", "state"],
        default_location,
    )
    source_url = normalize_url(
        uploaded_row_value(row, ["sourceUrl", "source_url", "googleMapsUrl", "google_maps_url", "listingUrl", "url"])
    )
    source_id = uploaded_row_value(row, ["placeId", "place_id", "googlePlaceId", "google_id", "cid", "fid"])
    if source_id:
        canonical_key = stable_lead_key("place", source_id)
    elif domain:
        canonical_key = stable_lead_key("domain", business_name, domain)
    elif email:
        canonical_key = stable_lead_key("email", business_name, email)
    elif phone:
        canonical_key = stable_lead_key("phone", business_name, normalize_phone_identity(phone))
    elif address:
        canonical_key = stable_lead_key("address", business_name, address)
    else:
        canonical_key = stable_lead_key("business", business_name, location or default_location)
    lead_key = compact_text(source_id) or canonical_key
    return DiscoveredLead(
        leadKey=lead_key,
        canonicalLeadKey=canonical_key,
        businessName=business_name,
        email=email,
        phone=phone,
        website=website,
        domain=domain,
        category=category or default_industry or "Local service",
        address=address,
        location=location or default_location or "South Africa",
        province=uploaded_row_value(row, ["province", "state"]),
        source="uploaded-lead-data",
        sourceUrl=source_url,
        notes=uploaded_row_value(row, ["notes", "description", "summary"]),
        raw=row,
    )


def campaign_import_job_summary(row: sqlite3.Row, include_campaign: bool = True) -> Dict[str, Any]:
    total = row["total_rows"] or 0
    processed = row["processed_rows"] or 0
    result: Dict[str, Any] = {
        "jobId": row["id"],
        "campaignId": row["campaign_id"],
        "fileName": row["file_name"],
        "fileType": row["file_type"],
        "fileRetained": os.path.exists(row["file_path"]),
        "status": row["status"],
        "totalRows": total,
        "processedRows": processed,
        "succeededRows": row["succeeded_rows"] or 0,
        "skippedRows": row["skipped_rows"] or 0,
        "failedRows": row["failed_rows"] or 0,
        "progressPercent": round((processed / total) * 100, 1) if total else 0,
        "chunkSize": row["chunk_size"],
        "channels": safe_json_loads(row["channels_json"], []),
        "backgroundRequested": bool(row["background_requested"]),
        "error": row["error"],
        "errors": safe_json_loads(row["errors_json"], []),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "completedAt": row["completed_at"],
    }
    if include_campaign:
        with get_pipeline_db() as db:
            campaign = db.execute("SELECT * FROM campaigns WHERE id = ?", (row["campaign_id"],)).fetchone()
            result["campaign"] = campaign_summary_from_row(db, campaign, include_leads=True) if campaign else None
    return result


def get_campaign_import_job_or_404(job_id: str) -> sqlite3.Row:
    with get_pipeline_db() as db:
        row = db.execute("SELECT * FROM campaign_import_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Campaign import job not found.")
    return row


def claim_uploaded_campaign_lead_identity(
    campaign_id: str,
    lead: DiscoveredLead,
) -> Optional[Dict[str, Any]]:
    canonical_key = lead.canonicalLeadKey or canonical_lead_key_for_lead(lead)
    identities = lead_identity_pairs(lead)
    if not identities:
        identities = [("canonical", f"canonical:{canonical_key}")]
    identity_keys = [identity_key for _identity_type, identity_key in identities]
    placeholders = ",".join("?" for _ in identity_keys)
    timestamp = now_iso()

    with get_pipeline_db() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            f"""
            SELECT identity_key, canonical_lead_key, campaign_id
            FROM campaign_lead_identity_claims
            WHERE identity_key IN ({placeholders})
            """,
            tuple(identity_keys),
        ).fetchall()
        conflict = next(
            (
                row
                for row in existing
                if row["campaign_id"] != campaign_id or row["canonical_lead_key"] != canonical_key
            ),
            None,
        )
        if conflict:
            return {
                "canonicalLeadKey": conflict["canonical_lead_key"],
                "campaignIds": sorted({row["campaign_id"] for row in existing}),
            }
        for identity_type, identity_key in identities:
            db.execute(
                """
                INSERT OR IGNORE INTO campaign_lead_identity_claims (
                    identity_key, identity_type, canonical_lead_key, campaign_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (identity_key, identity_type, canonical_key, campaign_id, timestamp, timestamp),
            )
    return None


def prior_campaign_lead_match(campaign_id: str, lead: DiscoveredLead) -> Optional[Dict[str, Any]]:
    candidate_key = lead.canonicalLeadKey or canonical_lead_key_for_lead(lead)
    conflicts = generated_lead_identity_conflicts(lead, candidate_key)
    conflict_keys = sorted({compact_text(value) for value in conflicts.values() if compact_text(value)})
    if not conflict_keys:
        return None

    placeholders = ",".join("?" for _ in conflict_keys)
    with get_pipeline_db() as db:
        approvals = db.execute(
            f"""
            SELECT id, canonical_lead_key, pipeline_id
            FROM approval_records
            WHERE canonical_lead_key IN ({placeholders}) AND status <> 'SUPERSEDED'
            ORDER BY created_at ASC
            """,
            tuple(conflict_keys),
        ).fetchall()

        prior_approvals = [
            row
            for row in approvals
            if row["canonical_lead_key"] != candidate_key or row["pipeline_id"] != campaign_id
        ]
        if not prior_approvals:
            return None

        approval_ids = [row["id"] for row in prior_approvals]
        approval_placeholders = ",".join("?" for _ in approval_ids)
        email_rows = db.execute(
            f"SELECT campaign_id, approval_id, ticket_id FROM campaign_email_leads WHERE approval_id IN ({approval_placeholders})",
            tuple(approval_ids),
        ).fetchall()
        call_rows = db.execute(
            f"SELECT campaign_id, approval_id, ticket_id FROM campaign_call_leads WHERE approval_id IN ({approval_placeholders})",
            tuple(approval_ids),
        ).fetchall()
        link_rows = db.execute(
            f"SELECT approval_id, ticket_id FROM zendesk_ticket_links WHERE approval_id IN ({approval_placeholders}) AND stage = 'intake'",
            tuple(approval_ids),
        ).fetchall()

    ticket_ids = sorted(
        {
            int(row["ticket_id"])
            for row in [*email_rows, *call_rows, *link_rows]
            if row["ticket_id"] is not None
        }
    )
    campaign_ids = sorted(
        {
            compact_text(row["campaign_id"])
            for row in [*email_rows, *call_rows]
            if compact_text(row["campaign_id"])
        }
        or {compact_text(row["pipeline_id"]) for row in prior_approvals if compact_text(row["pipeline_id"])}
    )
    return {
        "canonicalLeadKey": prior_approvals[0]["canonical_lead_key"],
        "approvalIds": approval_ids,
        "ticketIds": ticket_ids,
        "campaignIds": campaign_ids,
    }


def ensure_uploaded_campaign_lead(
    campaign: sqlite3.Row,
    lead: DiscoveredLead,
    channels: List[str],
    *,
    sync_tickets: bool = True,
) -> Dict[str, Any]:
    if lead_has_website(lead):
        raise ValueError("The row has a website or domain and is not eligible for the no-website campaign.")
    canonical_key = lead.canonicalLeadKey or canonical_lead_key_for_lead(lead)
    pipeline_id = campaign["id"]
    timestamp = now_iso()
    contact_name = compact_text(uploaded_row_value(lead.raw or {}, ["contactName", "contact_name", "ownerName", "owner_name"]))
    context = build_public_lead_context(lead, {}, canonical_key)
    campaign_industry = compact_text(campaign["industry"])
    lead_industry = compact_text(lead.category)
    resolved_industry = (
        lead_industry
        if lead_industry and (is_generic_industry_label(campaign_industry) or not campaign_industry)
        else campaign_industry or lead_industry
    )
    context.update(
        {
            "campaignId": campaign["id"],
            "campaignName": campaign["name"],
            "batchId": None,
            "industry": resolved_industry,
            "category": lead_industry or resolved_industry,
            "serviceKeywords": [lead_industry or resolved_industry],
            "contactName": contact_name,
            "intakeDeferred": True,
        }
    )
    context["brandTheme"] = business_theme_for_context(context)
    context["businessProfile"] = personalized_business_profile(context)
    available: List[str] = []
    if normalize_email_identity(lead.email) and "email" in channels:
        available.append("email")
    if normalize_phone_identity(lead.phone) and "phone" in channels:
        available.append("phone")
    if not available:
        raise ValueError("The row has no email or phone value for the selected campaign channels.")

    prior = prior_campaign_lead_match(campaign["id"], lead)
    if prior:
        return {
            "canonicalLeadKey": prior["canonicalLeadKey"],
            "approvalIds": prior["approvalIds"],
            "ticketIds": prior["ticketIds"],
            "createdRecords": 0,
            "skippedDuplicate": True,
            "duplicateScope": "workspace",
            "duplicateCampaignIds": prior["campaignIds"],
        }

    claimed_by = claim_uploaded_campaign_lead_identity(campaign["id"], lead)
    if claimed_by:
        prior = prior_campaign_lead_match(campaign["id"], lead) or {}
        return {
            "canonicalLeadKey": prior.get("canonicalLeadKey") or claimed_by["canonicalLeadKey"],
            "approvalIds": prior.get("approvalIds") or [],
            "ticketIds": prior.get("ticketIds") or [],
            "createdRecords": 0,
            "skippedDuplicate": True,
            "duplicateScope": "workspace",
            "duplicateCampaignIds": prior.get("campaignIds") or claimed_by["campaignIds"],
        }

    upsert_lead_registry(lead)

    approval_ids: List[str] = []
    ticket_ids: List[int] = []
    created_records = 0
    for channel in available:
        table = "campaign_email_leads" if channel == "email" else "campaign_call_leads"
        with get_pipeline_db() as db:
            existing = db.execute(
                f"SELECT * FROM {table} WHERE campaign_id = ? AND canonical_lead_key = ?",
                (campaign["id"], canonical_key),
            ).fetchone()
        if existing:
            approval_id = existing["approval_id"]
        else:
            channel_context = {**context, "contactChannel": channel}
            approval_id = create_approval_record(
                pipeline_id=pipeline_id,
                canonical_key=canonical_key,
                lead_key=lead.leadKey,
                business_name=lead.businessName,
                site_html=None,
                context=channel_context,
                site_content={
                    "deferredGeneration": True,
                    "message": "AI generation starts only when the deploy_site webhook is requested.",
                },
                template=dict(FREEFORM_SITE_SPEC),
                status="AWAITING_DEPLOYMENT",
                approval_id=str(
                    uuid5(NAMESPACE_URL, f"asf:approval:{campaign['id']}:{canonical_key}:{channel}")
                ),
            )
            deployment_id = str(uuid4())
            fields = {
                "campaignId": campaign["id"],
                "campaignName": campaign["name"],
                "businessName": lead.businessName,
                "contactName": contact_name,
                "email": lead.email,
                "phone": lead.phone,
                "industry": campaign["industry"],
                "location": lead.location,
                "address": lead.address,
                "sourceUrl": lead.sourceUrl,
                "source": lead.source,
                "channel": channel,
            }
            with get_pipeline_db() as db:
                db.execute(
                    """
                    INSERT INTO campaign_deployments (
                        id, campaign_id, canonical_lead_key, approval_id, channel, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (deployment_id, campaign["id"], canonical_key, approval_id, channel, "AWAITING_DEPLOYMENT", timestamp, timestamp),
                )
                if channel == "email":
                    db.execute(
                        """
                        INSERT INTO campaign_email_leads (
                            id, campaign_id, canonical_lead_key, approval_id, business_name,
                            contact_name, email, source_url, status, deployment_id, fields_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid4()), campaign["id"], canonical_key, approval_id, lead.businessName,
                            contact_name or None, lead.email, lead.sourceUrl, "AWAITING_DEPLOYMENT",
                            deployment_id, json.dumps(fields, default=str), timestamp, timestamp,
                        ),
                    )
                else:
                    db.execute(
                        """
                        INSERT INTO campaign_call_leads (
                            id, campaign_id, canonical_lead_key, approval_id, business_name,
                            contact_name, phone, source_url, status, deployment_id, fields_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid4()), campaign["id"], canonical_key, approval_id, lead.businessName,
                            contact_name or None, lead.phone, lead.sourceUrl, "AWAITING_DEPLOYMENT",
                            deployment_id, json.dumps(fields, default=str), timestamp, timestamp,
                        ),
                    )
            created_records += 1

        approval_ids.append(approval_id)
        if not sync_tickets:
            continue

        approval = get_approval_or_404(approval_id)
        channel_context = safe_json_loads(approval["context_json"], {**context, "contactChannel": channel})
        tickets = create_zendesk_intake_tickets(
            approval_id=approval_id,
            context=channel_context,
            pipeline_id=pipeline_id,
            batch_id=None,
            requested_channels=[channel],
        )
        ticket_id = tickets[0].get("ticketId") if tickets else None
        if not ticket_id:
            raise RuntimeError(f"Zendesk did not return a ticket ID for the {channel} lead.")
        with get_pipeline_db() as db:
            db.execute(
                f"UPDATE {table} SET ticket_id = ?, status = ?, updated_at = ? WHERE approval_id = ?",
                (ticket_id, "TICKET_READY", now_iso(), approval_id),
            )
        ticket_ids.append(int(ticket_id))

    return {
        "canonicalLeadKey": canonical_key,
        "approvalIds": approval_ids,
        "ticketIds": ticket_ids,
        "createdRecords": created_records,
        "skippedDuplicate": created_records == 0,
    }


def refresh_campaign_import_job(job_id: str) -> Dict[str, Any]:
    timestamp = now_iso()
    with get_pipeline_db() as db:
        job = db.execute("SELECT * FROM campaign_import_jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(status_code=404, detail="Campaign import job not found.")
        counts = {
            row["status"]: row["count"]
            for row in db.execute(
                "SELECT status, COUNT(*) AS count FROM campaign_import_items WHERE job_id = ? GROUP BY status",
                (job_id,),
            ).fetchall()
        }
        processed = counts.get("COMPLETE", 0) + counts.get("SKIPPED", 0) + counts.get("FAILED", 0)
        failed = counts.get("FAILED", 0)
        pending = counts.get("PENDING", 0) + counts.get("PROCESSING", 0)
        retryable = db.execute(
            "SELECT COUNT(*) AS count FROM campaign_import_items WHERE job_id = ? AND status = 'FAILED' AND attempts < 3",
            (job_id,),
        ).fetchone()["count"]
        if pending:
            status = "PROCESSING"
        elif retryable:
            status = "RETRY_PENDING"
        elif failed:
            status = "FAILED"
        else:
            status = "COMPLETED"
        errors = [
            {"row": item["row_number"], "message": item["error"]}
            for item in db.execute(
                "SELECT row_number, error FROM campaign_import_items WHERE job_id = ? AND status = 'FAILED' ORDER BY row_number LIMIT 50",
                (job_id,),
            ).fetchall()
        ]
        completed_at = timestamp if status in {"COMPLETED", "FAILED"} else None
        db.execute(
            """
            UPDATE campaign_import_jobs
            SET status = ?, processed_rows = ?, succeeded_rows = ?, skipped_rows = ?,
                failed_rows = ?, errors_json = ?, updated_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (
                status, processed, counts.get("COMPLETE", 0), counts.get("SKIPPED", 0),
                failed, json.dumps(errors, default=str), timestamp, completed_at, job_id,
            ),
        )
        db.execute(
            "UPDATE campaigns SET status = ?, discovered_count = ?, updated_at = ? WHERE id = ?",
            (
                "TICKET_SYNC_PENDING" if status == "COMPLETED" else "IMPORT_FAILED" if status == "FAILED" else "IMPORT_PROCESSING",
                counts.get("COMPLETE", 0), timestamp, job["campaign_id"],
            ),
        )
    refreshed = get_campaign_import_job_or_404(job_id)
    if refreshed["status"] == "COMPLETED" and os.path.exists(refreshed["file_path"]):
        try:
            os.remove(refreshed["file_path"])
        except OSError as error:
            log_event("warning", "campaign.import.cleanup_failed", str(error), jobId=job_id)
    return campaign_import_job_summary(get_campaign_import_job_or_404(job_id))


@app.get("/api/campaigns/imports")
def list_campaign_imports(limit: int = 20):
    require_zendesk_workspace_ready()
    safe_limit = max(1, min(limit, 100))
    with get_pipeline_db() as db:
        rows = db.execute(
            "SELECT * FROM campaign_import_jobs ORDER BY created_at DESC LIMIT ?", (safe_limit,)
        ).fetchall()
    for row in rows:
        if row["background_requested"] and row["status"] not in {"COMPLETED", "FAILED"}:
            schedule_campaign_import_job(row["id"])
    return {"jobs": [campaign_import_job_summary(row, include_campaign=False) for row in rows]}


@app.get("/api/campaigns/imports/{job_id}")
def get_campaign_import(job_id: str):
    require_zendesk_workspace_ready()
    job = get_campaign_import_job_or_404(job_id)
    if job["background_requested"] and job["status"] not in {"COMPLETED", "FAILED"}:
        schedule_campaign_import_job(job_id)
    return campaign_import_job_summary(job)


@app.post("/api/campaigns/import")
async def create_campaign_import(
    file: UploadFile = File(...),
    campaignName: str = Form(""),
    industry: str = Form("Local service"),
    location: str = Form("South Africa"),
    channels: str = Form("email,phone"),
    chunkSize: int = Form(100),
    autoGenerateMetadata: bool = Form(False),
    background: bool = Form(False),
):
    require_zendesk_workspace_ready()
    campaign_name = compact_text(campaignName)
    if not campaign_name and not autoGenerateMetadata:
        raise HTTPException(status_code=400, detail="Campaign name is required.")
    selected_channels = ["email", "phone"]
    safe_chunk_size = max(1, min(int(chunkSize or 100), 500))
    content = await file.read()
    max_bytes = int(os.getenv("CAMPAIGN_UPLOAD_MAX_BYTES", str(50 * 1024 * 1024)))
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded lead file is empty.")
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"The lead file exceeds the {max_bytes // (1024 * 1024)} MB upload limit.")
    safe_file_name = os.path.basename(file.filename or "leads.csv")
    file_type, rows = parse_uploaded_lead_file(content, safe_file_name)
    preview_leads: List[DiscoveredLead] = []
    for row_number, raw_row in enumerate(rows, start=1):
        try:
            preview_lead = normalize_uploaded_lead(
                raw_row,
                row_number,
                compact_text(industry, "Local service"),
                compact_text(location, "South Africa"),
            )
            if not lead_has_website(preview_lead) and lead_has_contact(preview_lead):
                preview_leads.append(preview_lead)
        except ValueError:
            continue
    mixed_counts = require_contactable_leads(preview_leads, "The uploaded lead file")
    metadata_suggestion = suggest_campaign_metadata(
        preview_leads,
        compact_text(location, "South Africa"),
        compact_text(industry, "Mixed industries"),
    )
    if autoGenerateMetadata:
        campaign_name = metadata_suggestion["campaignName"]
        industry = metadata_suggestion["industry"]
    idempotency_key = uploaded_campaign_idempotency_key(
        content, campaign_name, industry, location, selected_channels
    )
    with get_pipeline_db() as db:
        existing_job = db.execute(
            """
            SELECT job.*
            FROM campaign_import_jobs job
            JOIN campaigns campaign ON campaign.id = job.campaign_id
            WHERE campaign.idempotency_key = ?
            ORDER BY job.created_at ASC LIMIT 1
            """,
            (idempotency_key,),
        ).fetchone()
    if existing_job:
        if background and not existing_job["background_requested"]:
            with get_pipeline_db() as db:
                db.execute(
                    "UPDATE campaign_import_jobs SET background_requested = 1, updated_at = ? WHERE id = ?",
                    (now_iso(), existing_job["id"]),
                )
            existing_job = get_campaign_import_job_or_404(existing_job["id"])
        result = campaign_import_job_summary(existing_job)
        result["idempotentReplay"] = True
        if background and existing_job["status"] not in {"COMPLETED", "FAILED"}:
            schedule_campaign_import_job(existing_job["id"])
        return result

    campaign_id = str(uuid5(NAMESPACE_URL, idempotency_key))
    job_id = str(uuid5(NAMESPACE_URL, f"{idempotency_key}:job"))
    extension = os.path.splitext(safe_file_name)[1].lower()
    stored_path = os.path.join(campaign_upload_dir(), f"{job_id}{extension}")
    with open(stored_path, "wb") as upload_file:
        upload_file.write(content)
    timestamp = now_iso()
    created_job = False
    with get_pipeline_db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """
            INSERT OR IGNORE INTO campaigns (
                id, idempotency_key, name, batch_id, preset_id, industry, query, location, requested_count,
                discovered_count, channel_filter, status, warnings_json, created_at, updated_at
            ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
            """,
            (
                campaign_id, idempotency_key, campaign_name, "uploaded-leads", compact_text(industry, "Local service"),
                safe_file_name, compact_text(location, "South Africa"), len(rows),
                ",".join(selected_channels), "IMPORT_QUEUED", "[]", timestamp, timestamp,
            ),
        )
        job_insert = db.execute(
            """
            INSERT OR IGNORE INTO campaign_import_jobs (
                id, campaign_id, file_name, file_path, file_type, status, total_rows,
                chunk_size, channels_json, background_requested, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'QUEUED', ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id, campaign_id, safe_file_name, stored_path, file_type, len(rows),
                safe_chunk_size, json.dumps(selected_channels), int(background), timestamp, timestamp,
            ),
        )
        created_job = job_insert.rowcount == 1
        if created_job:
            for index, raw_row in enumerate(rows, start=1):
                db.execute(
                    """
                    INSERT OR IGNORE INTO campaign_import_items (
                        id, job_id, row_number, raw_json, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'PENDING', ?, ?)
                    """,
                    (
                        str(uuid5(NAMESPACE_URL, f"{job_id}:row:{index}")),
                        job_id,
                        index,
                        json.dumps(raw_row, default=str),
                        timestamp,
                        timestamp,
                    ),
                )
    if not created_job:
        result = campaign_import_job_summary(get_campaign_import_job_or_404(job_id))
        result["idempotentReplay"] = True
        return result
    save_pipeline_run(
        pipeline_id=campaign_id,
        status="IMPORT_QUEUED",
        template_id=FREEFORM_TEMPLATE_ID,
        source_batch_id=None,
        lead_count=len(rows),
        completed_count=0,
        pending_count=len(rows),
        failed_count=0,
        warnings=[],
        created_at=timestamp,
    )
    log_event("info", "campaign.import.queued", "Uploaded campaign queued.", jobId=job_id, campaignId=campaign_id, rows=len(rows))
    result = campaign_import_job_summary(get_campaign_import_job_or_404(job_id))
    result["idempotentReplay"] = False
    result["metadataSuggestion"] = metadata_suggestion
    result["mixedChannelCounts"] = mixed_counts
    if background:
        schedule_campaign_import_job(job_id)
    return result


@app.post("/api/campaigns/imports/{job_id}/process")
def process_campaign_import(job_id: str):
    require_zendesk_workspace_ready()
    job = get_campaign_import_job_or_404(job_id)
    if job["status"] == "COMPLETED":
        return campaign_import_job_summary(job)
    with get_pipeline_db() as db:
        db.execute("BEGIN IMMEDIATE")
        stale_before = (datetime.now() - timedelta(minutes=5)).isoformat()
        db.execute(
            """
            UPDATE campaign_import_items
            SET status = 'PENDING', updated_at = ?
            WHERE job_id = ? AND status = 'PROCESSING' AND updated_at < ?
            """,
            (now_iso(), job_id, stale_before),
        )
        campaign = db.execute("SELECT * FROM campaigns WHERE id = ?", (job["campaign_id"],)).fetchone()
        items = db.execute(
            """
            SELECT * FROM campaign_import_items
            WHERE job_id = ? AND (status = 'PENDING' OR (status = 'FAILED' AND attempts < 3))
            ORDER BY row_number LIMIT ?
            """,
            (job_id, job["chunk_size"]),
        ).fetchall()
        item_ids = [item["id"] for item in items]
        if item_ids:
            placeholders = ",".join("?" for _ in item_ids)
            db.execute(
                f"UPDATE campaign_import_items SET status = 'PROCESSING', updated_at = ? WHERE id IN ({placeholders})",
                (now_iso(), *item_ids),
            )
        db.execute(
            "UPDATE campaign_import_jobs SET status = 'PROCESSING', updated_at = ? WHERE id = ?",
            (now_iso(), job_id),
        )
    channels_for_job = safe_json_loads(job["channels_json"], ["email", "phone"])
    for item in items:
        try:
            raw_row = safe_json_loads(item["raw_json"], {})
            lead = normalize_uploaded_lead(
                raw_row, item["row_number"], compact_text(campaign["industry"], "Local service"), compact_text(campaign["location"], "South Africa")
            )
            result = ensure_uploaded_campaign_lead(
                campaign,
                lead,
                channels_for_job,
                sync_tickets=False,
            )
            with get_pipeline_db() as db:
                db.execute(
                    """
                    UPDATE campaign_import_items
                    SET canonical_lead_key = ?, status = ?, attempts = attempts + 1,
                        error = NULL, approval_ids_json = ?, ticket_ids_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        result["canonicalLeadKey"], "SKIPPED" if result["skippedDuplicate"] else "COMPLETE",
                        json.dumps(result["approvalIds"]), json.dumps(result["ticketIds"]), now_iso(), item["id"],
                    ),
                )
        except Exception as error:
            validation_error = isinstance(error, ValueError)
            attempts = 3 if validation_error else (item["attempts"] or 0) + 1
            item_status = "SKIPPED" if validation_error else "FAILED"
            with get_pipeline_db() as db:
                db.execute(
                    "UPDATE campaign_import_items SET status = ?, attempts = ?, error = ?, updated_at = ? WHERE id = ?",
                    (item_status, attempts, sanitize_message(error), now_iso(), item["id"]),
                )
            log_event(
                "warning" if validation_error else "error",
                "campaign.import.row_skipped" if validation_error else "campaign.import.row_failed",
                str(error),
                jobId=job_id,
                row=item["row_number"],
                attempts=attempts,
            )
    return refresh_campaign_import_job(job_id)


@app.post("/api/campaigns/imports/{job_id}/retry")
def retry_campaign_import(job_id: str):
    require_zendesk_workspace_ready()
    job = get_campaign_import_job_or_404(job_id)
    if job["status"] == "COMPLETED":
        return campaign_import_job_summary(job)
    with get_pipeline_db() as db:
        db.execute(
            "UPDATE campaign_import_items SET status = 'PENDING', attempts = 0, error = NULL, updated_at = ? WHERE job_id = ? AND status = 'FAILED'",
            (now_iso(), job_id),
        )
        db.execute(
            "UPDATE campaign_import_jobs SET status = 'QUEUED', error = NULL, completed_at = NULL, updated_at = ? WHERE id = ?",
            (now_iso(), job_id),
        )
    refreshed = get_campaign_import_job_or_404(job_id)
    if refreshed["background_requested"]:
        schedule_campaign_import_job(job_id)
    return campaign_import_job_summary(refreshed)


def run_campaign_import_background(job_id: str) -> None:
    try:
        while True:
            job = get_campaign_import_job_or_404(job_id)
            if job["status"] in {"COMPLETED", "FAILED"}:
                return
            before = int(job["processed_rows"] or 0)
            result = process_campaign_import(job_id)
            if result["status"] == "COMPLETED":
                schedule_campaign_zendesk_sync(result.get("campaignId"))
                return
            if result["status"] == "FAILED":
                return
            if int(result.get("processedRows") or 0) == before:
                time.sleep(1.5)
    except Exception as error:
        message = sanitize_message(error)
        with get_pipeline_db() as db:
            db.execute(
                """
                UPDATE campaign_import_jobs
                SET status = 'FAILED', error = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (message, now_iso(), now_iso(), job_id),
            )
        log_event("error", "campaign.import.background_failed", message, jobId=job_id)
    finally:
        with BACKGROUND_JOB_LOCK:
            ACTIVE_CAMPAIGN_IMPORT_JOBS.discard(job_id)


def schedule_campaign_import_job(job_id: str) -> bool:
    with get_pipeline_db() as db:
        job = db.execute("SELECT * FROM campaign_import_jobs WHERE id = ?", (job_id,)).fetchone()
    if not job or not job["background_requested"] or job["status"] in {"COMPLETED", "FAILED"}:
        return False
    with BACKGROUND_JOB_LOCK:
        if job_id in ACTIVE_CAMPAIGN_IMPORT_JOBS:
            return False
        ACTIVE_CAMPAIGN_IMPORT_JOBS.add(job_id)
    worker = threading.Thread(
        target=run_campaign_import_background,
        args=(job_id,),
        name=f"campaign-import-{job_id[:8]}",
        daemon=True,
    )
    worker.start()
    return True


@app.get("/api/campaigns/{campaign_id}")
def get_campaign(campaign_id: str):
    with get_pipeline_db() as db:
        row = db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Campaign not found.")
        return campaign_summary_from_row(db, row, include_leads=True)


def campaign_discovery_plan(campaign: sqlite3.Row) -> Tuple[List[DiscoveredLead], Dict[str, Any]]:
    batch_id = compact_text(campaign["batch_id"])
    if not batch_id:
        raise HTTPException(status_code=409, detail="This uploaded campaign has no discovery batch to reconcile.")
    with get_pipeline_db() as db:
        batch = db.execute("SELECT * FROM discovery_batches WHERE batch_id = ?", (batch_id,)).fetchone()
    if not batch:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CAMPAIGN_DISCOVERY_PLAN_MISSING",
                "message": "The saved campaign lead plan is missing; intake cannot be resumed safely.",
                "campaignId": campaign["id"],
                "batchId": batch_id,
            },
        )
    leads = [DiscoveredLead(**value) for value in safe_json_loads(batch["leads_json"], [])]
    province_stats = safe_json_loads(batch["province_stats_json"], {})
    stat_rows = list(province_stats.values()) if isinstance(province_stats, dict) else []

    def total_stat(key: str) -> int:
        return sum(int((value or {}).get(key) or 0) for value in stat_rows if isinstance(value, dict))

    raw_fetched = total_stat("rawItems")
    websites_skipped = total_stat("websitesSkipped")
    no_contact_skipped = total_stat("noContactSkipped")
    duplicates_skipped = total_stat("duplicatesSkipped") or int(batch["duplicates_skipped"] or 0)
    current_search_duplicates = total_stat("currentSearchDuplicatesSkipped") or duplicates_skipped
    already_deployed_skipped = total_stat("alreadyDeployedSkipped")
    active_deployment_skipped = total_stat("activeDeploymentSkipped")
    policy_excluded_skipped = total_stat("policyExcludedSkipped")
    location_skipped = total_stat("locationSkipped")
    invalid_record_skipped = total_stat("invalidRecordSkipped")
    reused_pending_or_failed = total_stat("reusedPendingOrFailed")
    target_overflow_skipped = total_stat("targetOverflowSkipped")
    first_stats = next((value for value in stat_rows if isinstance(value, dict)), {})
    shortfall = max(0, int(campaign["requested_count"] or 0) - len(leads))
    snapshot = {
        "batchId": batch_id,
        "preset": resolve_lead_preset(campaign["preset_id"], campaign["industry"], campaign["query"]),
        "location": batch["location"],
        "query": batch["query"],
        "leads": [lead.model_dump() for lead in leads],
        "sourceStatus": "SAVED_CAMPAIGN_PLAN",
        "warnings": safe_json_loads(batch["warnings_json"], []),
        "provinceStats": province_stats,
        "duplicatesSkipped": duplicates_skipped,
        "requestedCount": campaign["requested_count"],
        "rawFetched": raw_fetched or int(batch["lead_count"] or 0) or len(leads),
        "eligibleReturned": len(leads),
        "websitesSkipped": websites_skipped,
        "noContactSkipped": no_contact_skipped,
        "generatedDuplicatesSkipped": (
            already_deployed_skipped + active_deployment_skipped + policy_excluded_skipped
        ),
        "currentSearchDuplicatesSkipped": current_search_duplicates,
        "alreadyDeployedSkipped": already_deployed_skipped,
        "activeDeploymentSkipped": active_deployment_skipped,
        "policyExcludedSkipped": policy_excluded_skipped,
        "locationSkipped": location_skipped,
        "invalidRecordSkipped": invalid_record_skipped,
        "reusedPendingOrFailed": reused_pending_or_failed,
        "targetOverflowSkipped": target_overflow_skipped,
        "shortfall": shortfall,
        "stopReason": first_stats.get("stopReason") or ("TARGET_MET" if not shortfall else "RESULTS_EXHAUSTED"),
        "searchVariantCount": int(first_stats.get("searchVariantCount") or 0),
        "providerStatus": first_stats.get("providerStatus") or "UNKNOWN",
        "providerDurationSeconds": float(first_stats.get("providerDurationSeconds") or 0),
        "cached": True,
    }
    return leads, snapshot


def ensure_discovered_campaign_channel_record(
    campaign: sqlite3.Row,
    lead: DiscoveredLead,
    channel: str,
) -> sqlite3.Row:
    campaign_id = campaign["id"]
    canonical_key = lead.canonicalLeadKey or canonical_lead_key_for_lead(lead)
    lead.canonicalLeadKey = canonical_key
    upsert_lead_registry(lead)
    table = "campaign_email_leads" if channel == "email" else "campaign_call_leads"
    with get_pipeline_db() as db:
        existing = db.execute(
            f"SELECT * FROM {table} WHERE campaign_id = ? AND canonical_lead_key = ?",
            (campaign_id, canonical_key),
        ).fetchone()
    if existing:
        return existing

    timestamp = now_iso()
    contact_name = compact_text(first_present(lead.raw or {}, ["contactName", "ownerName", "name"]))
    context = build_public_lead_context(lead, {}, canonical_key)
    context.update(
        {
            "campaignId": campaign_id,
            "campaignName": campaign["name"],
            "batchId": campaign["batch_id"],
            "industry": compact_text(campaign["industry"], context.get("industry")),
            "contactName": contact_name,
            "contactChannel": channel,
            "intakeDeferred": True,
        }
    )
    approval_id = str(uuid5(NAMESPACE_URL, f"asf:approval:{campaign_id}:{canonical_key}:{channel}"))
    deployment_id = str(uuid5(NAMESPACE_URL, f"asf:deployment:{campaign_id}:{canonical_key}:{channel}"))
    channel_lead_id = str(uuid5(NAMESPACE_URL, f"asf:channel-lead:{campaign_id}:{canonical_key}:{channel}"))
    create_approval_record(
        pipeline_id=campaign_id,
        canonical_key=canonical_key,
        lead_key=lead.leadKey,
        business_name=lead.businessName,
        site_html=None,
        context=context,
        site_content={
            "deferredGeneration": True,
            "message": "AI generation starts only when the deploy_site webhook is requested.",
        },
        template=dict(FREEFORM_SITE_SPEC),
        status="AWAITING_DEPLOYMENT",
        approval_id=approval_id,
    )
    fields = {
        "campaignId": campaign_id,
        "campaignName": campaign["name"],
        "businessName": lead.businessName,
        "contactName": contact_name,
        "email": lead.email,
        "phone": lead.phone,
        "industry": campaign["industry"],
        "location": lead.location,
        "address": lead.address,
        "sourceUrl": lead.sourceUrl,
        "channel": channel,
    }
    with get_pipeline_db() as db:
        db.execute(
            """
            INSERT OR IGNORE INTO campaign_deployments (
                id, campaign_id, canonical_lead_key, approval_id, channel, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'AWAITING_DEPLOYMENT', ?, ?)
            """,
            (deployment_id, campaign_id, canonical_key, approval_id, channel, timestamp, timestamp),
        )
        if channel == "email":
            db.execute(
                """
                INSERT OR IGNORE INTO campaign_email_leads (
                    id, campaign_id, canonical_lead_key, approval_id, business_name, contact_name,
                    email, source_url, status, deployment_id, fields_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'AWAITING_DEPLOYMENT', ?, ?, ?, ?)
                """,
                (
                    channel_lead_id, campaign_id, canonical_key, approval_id, lead.businessName,
                    contact_name or None, lead.email, lead.sourceUrl, deployment_id,
                    json.dumps(fields, default=str), timestamp, timestamp,
                ),
            )
        else:
            db.execute(
                """
                INSERT OR IGNORE INTO campaign_call_leads (
                    id, campaign_id, canonical_lead_key, approval_id, business_name, contact_name,
                    phone, source_url, status, deployment_id, fields_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'AWAITING_DEPLOYMENT', ?, ?, ?, ?)
                """,
                (
                    channel_lead_id, campaign_id, canonical_key, approval_id, lead.businessName,
                    contact_name or None, lead.phone, lead.sourceUrl, deployment_id,
                    json.dumps(fields, default=str), timestamp, timestamp,
                ),
            )
        row = db.execute(
            f"SELECT * FROM {table} WHERE campaign_id = ? AND canonical_lead_key = ?",
            (campaign_id, canonical_key),
        ).fetchone()
    if not row:
        raise RuntimeError(f"Could not reconcile the local {channel} campaign record.")
    return row


def reconcile_discovered_campaign_intake(
    campaign_id: str,
    *,
    idempotent_replay: bool,
    sync_tickets: bool = True,
) -> Dict[str, Any]:
    with get_pipeline_db() as db:
        campaign = db.execute(
            "SELECT * FROM campaigns WHERE id = ? AND idempotency_key IS NOT NULL", (campaign_id,)
        ).fetchone()
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found.")
        db.execute(
            "UPDATE campaigns SET status = 'INTAKE_PROCESSING', updated_at = ? WHERE id = ?",
            (now_iso(), campaign_id),
        )
    leads, discovery_snapshot = campaign_discovery_plan(campaign)
    channels = [value for value in compact_text(campaign["channel_filter"]).split(",") if value]
    warnings = [
        value
        for value in safe_json_loads(campaign["warnings_json"], [])
        if not compact_text(value).startswith("Zendesk intake failed for ")
    ]
    expected_count = 0
    ready_count = 0
    errors: List[str] = []
    synced = 0

    for lead in leads:
        available: List[str] = []
        if normalize_email_identity(lead.email) and "email" in channels:
            available.append("email")
        if compact_text(lead.phone) and "phone" in channels:
            available.append("phone")
        for channel in available:
            expected_count += 1
            row = ensure_discovered_campaign_channel_record(campaign, lead, channel)
            if not sync_tickets:
                continue
            approval = get_approval_or_404(row["approval_id"])
            context = safe_json_loads(approval["context_json"], {})
            table = "campaign_email_leads" if channel == "email" else "campaign_call_leads"
            try:
                tickets = create_zendesk_intake_tickets(
                    approval_id=row["approval_id"],
                    context=context,
                    pipeline_id=campaign_id,
                    batch_id=campaign["batch_id"],
                    requested_channels=[channel],
                )
                ticket_id = tickets[0].get("ticketId") if tickets else None
                if not ticket_id:
                    raise RuntimeError(f"Zendesk did not return a ticket ID for the {channel} lead.")
                with get_pipeline_db() as db:
                    db.execute(
                        f"UPDATE {table} SET ticket_id = ?, status = 'TICKET_READY', updated_at = ? WHERE id = ?",
                        (ticket_id, now_iso(), row["id"]),
                    )
                    db.execute(
                        "UPDATE campaign_deployments SET error = NULL, updated_at = ? WHERE approval_id = ?",
                        (now_iso(), row["approval_id"]),
                    )
                ready_count += 1
                if not row["ticket_id"]:
                    synced += 1
            except Exception as error:
                message = f"Zendesk intake failed for {lead.businessName} ({channel}): {sanitize_message(error)}"
                errors.append(message)
                with get_pipeline_db() as db:
                    db.execute(
                        "UPDATE campaign_deployments SET error = ?, updated_at = ? WHERE approval_id = ?",
                        (sanitize_message(error), now_iso(), row["approval_id"]),
                    )

    with get_pipeline_db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """
            UPDATE campaign_deployments
            SET error = NULL, updated_at = ?
            WHERE campaign_id = ? AND approval_id IN (
                SELECT approval_id FROM campaign_email_leads
                WHERE campaign_id = ? AND ticket_id IS NOT NULL
                UNION
                SELECT approval_id FROM campaign_call_leads
                WHERE campaign_id = ? AND ticket_id IS NOT NULL
            )
            """,
            (now_iso(), campaign_id, campaign_id, campaign_id),
        )
        ready_count = db.execute(
            """
            SELECT COUNT(*) AS count FROM (
                SELECT id FROM campaign_email_leads WHERE campaign_id = ? AND ticket_id IS NOT NULL
                UNION ALL
                SELECT id FROM campaign_call_leads WHERE campaign_id = ? AND ticket_id IS NOT NULL
            )
            """,
            (campaign_id, campaign_id),
        ).fetchone()["count"]
        unresolved = db.execute(
            """
            SELECT d.channel, d.error, COALESCE(e.business_name, c.business_name, d.canonical_lead_key) AS business_name
            FROM campaign_deployments d
            LEFT JOIN campaign_email_leads e ON e.approval_id = d.approval_id
            LEFT JOIN campaign_call_leads c ON c.approval_id = d.approval_id
            WHERE d.campaign_id = ? AND d.error IS NOT NULL
              AND COALESCE(e.ticket_id, c.ticket_id) IS NULL
            ORDER BY d.created_at
            """,
            (campaign_id,),
        ).fetchall()
        errors = [
            f"Zendesk intake failed for {row['business_name']} ({row['channel']}): {row['error']}"
            for row in unresolved
        ]
        warnings = [*warnings, *errors]
        pending_count = max(0, expected_count - ready_count)
        if pending_count == 0:
            status = "ACTIVE"
        elif sync_tickets:
            status = "INTAKE_PARTIAL"
        else:
            status = "TICKET_SYNC_PENDING"
        timestamp = now_iso()
        db.execute(
            """
            INSERT INTO pipeline_runs (
                pipeline_id, status, template_id, source_batch_id, created_at, updated_at,
                lead_count, completed_count, pending_count, failed_count, warnings_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pipeline_id) DO UPDATE SET
                status = excluded.status,
                updated_at = excluded.updated_at,
                lead_count = excluded.lead_count,
                completed_count = excluded.completed_count,
                pending_count = excluded.pending_count,
                failed_count = excluded.failed_count,
                warnings_json = excluded.warnings_json
            """,
            (
                campaign_id, status, FREEFORM_TEMPLATE_ID, campaign["batch_id"], campaign["created_at"],
                timestamp, expected_count, ready_count, pending_count, len(errors),
                json.dumps(warnings, default=str),
            ),
        )
        db.execute(
            "UPDATE campaigns SET status = ?, warnings_json = ?, updated_at = ? WHERE id = ?",
            (status, json.dumps(warnings, default=str), timestamp, campaign_id),
        )
        campaign = db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        result = campaign_summary_from_row(db, campaign, include_leads=True)
    result["discovery"] = discovery_snapshot
    result["idempotentReplay"] = idempotent_replay
    result["sync"] = {"synced": synced, "errors": errors, "pending": pending_count}
    return result


def create_campaign_intake_internal(
    request: CampaignIntakeRequest,
    *,
    sync_tickets: bool,
    progress_callback: Optional[Callable[[str, float], None]] = None,
) -> Dict[str, Any]:
    require_zendesk_workspace_ready()
    campaign_name = compact_text(request.campaignName)
    if not campaign_name and not request.autoGenerateMetadata:
        raise HTTPException(status_code=400, detail="Campaign name is required.")
    channels = ["email", "phone"]
    if not request.syncZendesk:
        raise HTTPException(
            status_code=400,
            detail="Campaigns cannot run in local-only mode. Zendesk ticket creation is required.",
        )

    if sync_tickets:
        verify_zendesk_ticket_contracts(channels)
    idempotency_key = campaign_request_idempotency_key(request, channels)
    with get_pipeline_db() as db:
        existing_campaign = db.execute(
            "SELECT * FROM campaigns WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
    if existing_campaign:
        if progress_callback:
            progress_callback("SAVING_CAMPAIGN", 85)
        return reconcile_discovered_campaign_intake(
            existing_campaign["id"],
            idempotent_replay=True,
            sync_tickets=sync_tickets,
        )

    discovery = discover_leads_internal(
        DiscoverLeadsRequest(
            presetId=request.presetId,
            industry=request.industry,
            location=request.location,
            query=request.query,
            limit=request.limit,
            forceRefresh=request.forceRefresh,
        ),
        progress_callback=progress_callback,
    )
    if not discovery.leads:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "NO_ELIGIBLE_LEADS",
                "message": (
                    "The two-minute search completed without an eligible no-website lead. "
                    "No contacts were invented; review the saved rejection breakdown."
                ),
                "discovery": discovery.model_dump(),
            },
        )
    mixed_counts = require_contactable_leads(discovery.leads, "The Apify search result")
    if progress_callback:
        progress_callback("SAVING_CAMPAIGN", 85)
    metadata_suggestion = suggest_campaign_metadata(
        discovery.leads,
        discovery.location,
        compact_text(request.industry, discovery.preset.get("industry", "Mixed industries")),
    )
    if request.autoGenerateMetadata:
        campaign_name = metadata_suggestion["campaignName"]
    campaign_id = str(uuid5(NAMESPACE_URL, idempotency_key))
    timestamp = now_iso()
    industry = (
        metadata_suggestion["industry"]
        if request.autoGenerateMetadata
        else compact_text(request.industry, discovery.preset.get("industry", "Local service"))
    )
    raced_campaign_id: Optional[str] = None
    with get_pipeline_db() as db:
        insert = db.execute(
            """
            INSERT OR IGNORE INTO campaigns (
                id, idempotency_key, name, batch_id, preset_id, industry, query, location, requested_count,
                discovered_count, channel_filter, status, warnings_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'INTAKE_PENDING', ?, ?, ?)
            """,
            (
                campaign_id, idempotency_key, campaign_name, discovery.batchId, request.presetId, industry,
                discovery.query, discovery.location, request.limit, len(discovery.leads), ",".join(channels),
                json.dumps(discovery.warnings, default=str), timestamp, timestamp,
            ),
        )
        if insert.rowcount == 0:
            existing_campaign = db.execute(
                "SELECT * FROM campaigns WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing_campaign:
                raced_campaign_id = existing_campaign["id"]
    if raced_campaign_id:
        return reconcile_discovered_campaign_intake(
            raced_campaign_id,
            idempotent_replay=True,
            sync_tickets=sync_tickets,
        )
    result = reconcile_discovered_campaign_intake(
        campaign_id,
        idempotent_replay=False,
        sync_tickets=sync_tickets,
    )
    result["metadataSuggestion"] = metadata_suggestion
    result["mixedChannelCounts"] = mixed_counts
    return result


@app.post("/api/campaigns/intake")
def create_campaign_intake(request: CampaignIntakeRequest):
    return create_campaign_intake_internal(request, sync_tickets=True)


def campaign_intake_job_summary(row: sqlite3.Row) -> Dict[str, Any]:
    request_payload = safe_json_loads(row["request_json"], {})
    result = safe_json_loads(row["result_json"], None)
    terminal = row["status"] in {"COMPLETED", "FAILED"}
    timing_end = row["completed_at"] if terminal else None
    provider_started = parse_api_datetime(row["started_at"])
    provider_deadline = (
        (provider_started + timedelta(seconds=120)).isoformat().replace("+00:00", "Z")
        if provider_started
        else None
    )
    return {
        "jobId": row["id"],
        "status": row["status"],
        "stage": row["stage"],
        "progressPercent": row["progress_percent"] or 0,
        "campaignId": row["campaign_id"],
        "campaign": result if row["status"] == "COMPLETED" else None,
        "failureDetails": result if row["status"] == "FAILED" else None,
        "error": row["error"],
        "request": {
            "campaignName": request_payload.get("campaignName"),
            "presetId": request_payload.get("presetId"),
            "industry": request_payload.get("industry"),
            "location": request_payload.get("location"),
            "query": request_payload.get("query"),
            "limit": request_payload.get("limit"),
            "forceRefresh": request_payload.get("forceRefresh"),
        },
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "startedAt": row["started_at"],
        "providerStartedAt": row["started_at"],
        "providerDeadlineAt": provider_deadline,
        "providerLimitSeconds": 120,
        "queueDurationSeconds": elapsed_seconds(row["created_at"], row["started_at"]) if row["started_at"] else 0,
        "providerElapsedSeconds": elapsed_seconds(row["started_at"], timing_end) if row["started_at"] else 0,
        "elapsedSeconds": elapsed_seconds(row["created_at"], timing_end),
        "completedAt": row["completed_at"],
    }


def get_campaign_intake_job_or_404(job_id: str) -> sqlite3.Row:
    with get_pipeline_db() as db:
        row = db.execute("SELECT * FROM campaign_intake_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Campaign intake job not found.")
    return row


def campaign_background_error_message(error: Exception) -> str:
    if isinstance(error, HTTPException):
        detail = error.detail
        if isinstance(detail, dict):
            return compact_text(detail.get("message"), sanitize_message(detail))
        return compact_text(detail, sanitize_message(error))
    return sanitize_message(error)


def update_campaign_intake_stage(job_id: str, stage: str, progress_percent: float) -> None:
    with get_pipeline_db() as db:
        db.execute(
            """
            UPDATE campaign_intake_jobs
            SET stage = ?, progress_percent = ?, updated_at = ?
            WHERE id = ? AND status = 'RUNNING'
            """,
            (stage, max(0, min(float(progress_percent), 99)), now_iso(), job_id),
        )


def run_campaign_intake_background(job_id: str) -> None:
    try:
        job = get_campaign_intake_job_or_404(job_id)
        request = CampaignIntakeRequest.model_validate(safe_json_loads(job["request_json"], {}))
        started_at = now_iso()
        with get_pipeline_db() as db:
            db.execute(
                """
                UPDATE campaign_intake_jobs
                SET status = 'RUNNING', stage = 'SEARCHING_APIFY',
                    progress_percent = 10, error = NULL, result_json = NULL, started_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (started_at, started_at, job_id),
            )
        result = create_campaign_intake_internal(
            request,
            sync_tickets=False,
            progress_callback=lambda stage, progress: update_campaign_intake_stage(job_id, stage, progress),
        )
        completed_at = now_iso()
        with get_pipeline_db() as db:
            db.execute(
                """
                UPDATE campaign_intake_jobs
                SET status = 'COMPLETED', stage = 'LEADS_READY', progress_percent = 100,
                    campaign_id = ?, result_json = ?, error = NULL, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    result.get("campaignId"),
                    json.dumps(result, default=str),
                    completed_at,
                    completed_at,
                    job_id,
                ),
            )
        schedule_campaign_zendesk_sync(result.get("campaignId"))
    except Exception as error:
        message = campaign_background_error_message(error)
        error_detail = error.detail if isinstance(error, HTTPException) and isinstance(error.detail, dict) else None
        failure_details = {
            "code": compact_text((error_detail or {}).get("code"), "CAMPAIGN_INTAKE_FAILED"),
            "message": message,
            "discovery": (error_detail or {}).get("discovery"),
        }
        completed_at = now_iso()
        with get_pipeline_db() as db:
            db.execute(
                """
                UPDATE campaign_intake_jobs
                SET status = 'FAILED', stage = 'FAILED', progress_percent = 100,
                    error = ?, result_json = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (message, json.dumps(failure_details, default=str), completed_at, completed_at, job_id),
            )
        log_event("error", "campaign.intake.background_failed", message, jobId=job_id)
    finally:
        with BACKGROUND_JOB_LOCK:
            ACTIVE_CAMPAIGN_INTAKE_JOBS.discard(job_id)


def schedule_campaign_intake_job(job_id: str) -> bool:
    job = get_campaign_intake_job_or_404(job_id)
    if job["status"] in {"COMPLETED", "FAILED"}:
        return False
    with BACKGROUND_JOB_LOCK:
        if job_id in ACTIVE_CAMPAIGN_INTAKE_JOBS:
            return False
        ACTIVE_CAMPAIGN_INTAKE_JOBS.add(job_id)
    worker = threading.Thread(
        target=run_campaign_intake_background,
        args=(job_id,),
        name=f"campaign-intake-{job_id[:8]}",
        daemon=True,
    )
    worker.start()
    return True


@app.post("/api/campaigns/intake/jobs")
def create_campaign_intake_job(request: CampaignIntakeRequest):
    require_zendesk_workspace_ready()
    campaign_name = compact_text(request.campaignName)
    if not campaign_name and not request.autoGenerateMetadata:
        raise HTTPException(status_code=400, detail="Campaign name is required.")
    job_id = str(uuid4())
    queued_request = request.model_copy(deep=True)
    if queued_request.forceRefresh and not compact_text(queued_request.idempotencyKey):
        queued_request.idempotencyKey = f"background:{job_id}"
    timestamp = now_iso()
    with get_pipeline_db() as db:
        db.execute(
            """
            INSERT INTO campaign_intake_jobs (
                id, request_json, status, stage, progress_percent, created_at, updated_at
            ) VALUES (?, ?, 'QUEUED', 'QUEUED', 0, ?, ?)
            """,
            (job_id, json.dumps(queued_request.model_dump(), default=str), timestamp, timestamp),
        )
    schedule_campaign_intake_job(job_id)
    return campaign_intake_job_summary(get_campaign_intake_job_or_404(job_id))


@app.get("/api/campaigns/intake/jobs")
def list_campaign_intake_jobs(limit: int = 20):
    require_zendesk_workspace_ready()
    safe_limit = max(1, min(limit, 100))
    with get_pipeline_db() as db:
        rows = db.execute(
            "SELECT * FROM campaign_intake_jobs ORDER BY created_at DESC LIMIT ?",
            (safe_limit,),
        ).fetchall()
    for row in rows:
        if row["status"] in {"QUEUED", "RUNNING"}:
            schedule_campaign_intake_job(row["id"])
    return {"jobs": [campaign_intake_job_summary(row) for row in rows]}


@app.get("/api/campaigns/intake/jobs/{job_id}")
def get_campaign_intake_job(job_id: str):
    require_zendesk_workspace_ready()
    job = get_campaign_intake_job_or_404(job_id)
    if job["status"] in {"QUEUED", "RUNNING"}:
        schedule_campaign_intake_job(job_id)
    return campaign_intake_job_summary(job)


@app.post("/api/campaigns/intake/jobs/{job_id}/retry")
def retry_campaign_intake_job(job_id: str):
    require_zendesk_workspace_ready()
    job = get_campaign_intake_job_or_404(job_id)
    if job["status"] != "FAILED":
        return campaign_intake_job_summary(job)
    timestamp = now_iso()
    with get_pipeline_db() as db:
        db.execute(
            """
            UPDATE campaign_intake_jobs
            SET status = 'QUEUED', stage = 'QUEUED', progress_percent = 0,
                error = NULL, result_json = NULL, campaign_id = NULL,
                updated_at = ?, started_at = NULL, completed_at = NULL
            WHERE id = ?
            """,
            (timestamp, job_id),
        )
    schedule_campaign_intake_job(job_id)
    return campaign_intake_job_summary(get_campaign_intake_job_or_404(job_id))


@app.on_event("startup")
def resume_campaign_background_jobs_on_startup() -> None:
    with get_pipeline_db() as db:
        intake_job_ids = [
            row["id"]
            for row in db.execute(
                "SELECT id FROM campaign_intake_jobs WHERE status IN ('QUEUED', 'RUNNING')"
            ).fetchall()
        ]
        import_job_ids = [
            row["id"]
            for row in db.execute(
                """
                SELECT id FROM campaign_import_jobs
                WHERE background_requested = 1 AND status NOT IN ('COMPLETED', 'FAILED')
                """
            ).fetchall()
        ]
        zendesk_sync_campaign_ids = [
            row["id"]
            for row in db.execute(
                """
                SELECT id FROM campaigns
                WHERE status IN ('TICKET_SYNC_PENDING', 'TICKET_SYNCING', 'TICKET_SYNC_PARTIAL')
                """
            ).fetchall()
        ]
    for job_id in intake_job_ids:
        schedule_campaign_intake_job(job_id)
    for job_id in import_job_ids:
        schedule_campaign_import_job(job_id)
    for campaign_id in zendesk_sync_campaign_ids:
        schedule_campaign_zendesk_sync(campaign_id)


@app.post("/api/campaigns/{campaign_id}/sync-zendesk")
def sync_campaign_to_zendesk(campaign_id: str):
    require_zendesk_workspace_ready()
    discovery_campaign = False
    with get_pipeline_db() as db:
        campaign = db.execute(
            "SELECT * FROM campaigns WHERE id = ? AND idempotency_key IS NOT NULL", (campaign_id,)
        ).fetchone()
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found.")
        campaign_channels = [value for value in compact_text(campaign["channel_filter"]).split(",") if value]
        discovery_campaign = bool(compact_text(campaign["batch_id"]))
        email_rows = db.execute(
            "SELECT * FROM campaign_email_leads WHERE campaign_id = ? AND ticket_id IS NULL",
            (campaign_id,),
        ).fetchall()
        call_rows = db.execute(
            "SELECT * FROM campaign_call_leads WHERE campaign_id = ? AND ticket_id IS NULL",
            (campaign_id,),
        ).fetchall()
    verify_zendesk_ticket_contracts(campaign_channels)

    if discovery_campaign:
        return reconcile_discovered_campaign_intake(campaign_id, idempotent_replay=True)

    synced = 0
    errors: List[str] = []
    for channel, rows, table in [
        ("email", email_rows, "campaign_email_leads"),
        ("phone", call_rows, "campaign_call_leads"),
    ]:
        for item in rows:
            approval = get_approval_or_404(item["approval_id"])
            context = safe_json_loads(approval["context_json"], {})
            try:
                tickets = create_zendesk_intake_tickets(
                    approval_id=item["approval_id"],
                    context=context,
                    pipeline_id=approval["pipeline_id"],
                    batch_id=campaign["batch_id"],
                    requested_channels=[channel],
                )
                ticket_id = tickets[0].get("ticketId") if tickets else None
                if ticket_id:
                    with get_pipeline_db() as db:
                        db.execute(
                            f"UPDATE {table} SET ticket_id = ?, status = ?, updated_at = ? WHERE id = ?",
                            (ticket_id, "TICKET_READY", now_iso(), item["id"]),
                        )
                    synced += 1
            except Exception as error:
                errors.append(f"{item['business_name']} ({channel}): {sanitize_message(error)}")

    detail = get_campaign(campaign_id)
    detail["sync"] = {"synced": synced, "errors": errors}
    return detail


def pending_campaign_ticket_count(campaign_id: str) -> int:
    with get_pipeline_db() as db:
        return int(
            db.execute(
                """
                SELECT (
                    SELECT COUNT(*) FROM campaign_email_leads
                    WHERE campaign_id = ? AND ticket_id IS NULL
                ) + (
                    SELECT COUNT(*) FROM campaign_call_leads
                    WHERE campaign_id = ? AND ticket_id IS NULL
                ) AS count
                """,
                (campaign_id, campaign_id),
            ).fetchone()["count"]
            or 0
        )


def run_campaign_zendesk_sync_background(campaign_id: str) -> None:
    last_error = ""
    try:
        for attempt in range(1, 4):
            if pending_campaign_ticket_count(campaign_id) == 0:
                with get_pipeline_db() as db:
                    db.execute(
                        "UPDATE campaigns SET status = 'ACTIVE', updated_at = ? WHERE id = ?",
                        (now_iso(), campaign_id),
                    )
                return

            with get_pipeline_db() as db:
                db.execute(
                    "UPDATE campaigns SET status = 'TICKET_SYNCING', updated_at = ? WHERE id = ?",
                    (now_iso(), campaign_id),
                )
            try:
                result = sync_campaign_to_zendesk(campaign_id)
                errors = (result.get("sync") or {}).get("errors") or []
                pending = pending_campaign_ticket_count(campaign_id)
                if pending == 0:
                    with get_pipeline_db() as db:
                        db.execute(
                            "UPDATE campaigns SET status = 'ACTIVE', updated_at = ? WHERE id = ?",
                            (now_iso(), campaign_id),
                        )
                    return
                last_error = "; ".join(compact_text(value) for value in errors if compact_text(value))
                if not last_error:
                    last_error = f"{pending} Zendesk ticket records remain pending."
            except Exception as error:
                last_error = campaign_background_error_message(error)

            log_event(
                "warning",
                "campaign.zendesk_sync.retry",
                "Zendesk ticket synchronization will retry.",
                campaignId=campaign_id,
                attempt=attempt,
                pending=pending_campaign_ticket_count(campaign_id),
                reason=last_error,
            )
            if attempt < 3:
                time.sleep(1.5 * attempt)

        with get_pipeline_db() as db:
            campaign = db.execute("SELECT warnings_json FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
            warnings = safe_json_loads(campaign["warnings_json"], []) if campaign else []
            warning = f"Zendesk synchronization remains pending after three attempts: {last_error}"
            if warning not in warnings:
                warnings.append(warning)
            db.execute(
                """
                UPDATE campaigns
                SET status = 'TICKET_SYNC_PARTIAL', warnings_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(warnings, default=str), now_iso(), campaign_id),
            )
    finally:
        with BACKGROUND_JOB_LOCK:
            ACTIVE_CAMPAIGN_ZENDESK_SYNCS.discard(campaign_id)


def schedule_campaign_zendesk_sync(campaign_id: Optional[str]) -> bool:
    normalized_campaign_id = compact_text(campaign_id)
    if not normalized_campaign_id or pending_campaign_ticket_count(normalized_campaign_id) == 0:
        return False
    with BACKGROUND_JOB_LOCK:
        if normalized_campaign_id in ACTIVE_CAMPAIGN_ZENDESK_SYNCS:
            return False
        ACTIVE_CAMPAIGN_ZENDESK_SYNCS.add(normalized_campaign_id)
    worker = threading.Thread(
        target=run_campaign_zendesk_sync_background,
        args=(normalized_campaign_id,),
        name=f"campaign-zendesk-{normalized_campaign_id[:8]}",
        daemon=True,
    )
    worker.start()
    return True


@app.get("/api/operations/groups")
def list_operation_groups(page: int = 1, pageSize: int = 10, status: str = "all", channel: str = "all"):
    safe_page = max(1, page)
    safe_page_size = max(1, min(pageSize, 50))
    status_filter = compact_text(status, "all").upper()
    channel_filter = compact_text(channel, "all").lower()

    with get_pipeline_db() as db:
        run_rows = db.execute(
            """
            SELECT *
            FROM pipeline_runs
            ORDER BY created_at DESC
            """
        ).fetchall()
        batch_rows = db.execute("SELECT * FROM discovery_batches").fetchall()
        approval_rows = db.execute("SELECT * FROM approval_records ORDER BY created_at DESC").fetchall()

    batches = {row["batch_id"]: row for row in batch_rows}
    approvals_by_run: Dict[str, List[Dict[str, Any]]] = {}
    for row in approval_rows:
        item = approval_row_to_dict(row)
        context = item.get("context") or {}
        item["contactType"] = contact_type_from_context(context)
        if status_filter != "ALL" and item.get("status") != status_filter:
            continue
        if channel_filter != "all" and item["contactType"] != channel_filter:
            continue
        approvals_by_run.setdefault(item["pipelineId"], []).append(item)

    groups: List[Dict[str, Any]] = []
    for run in run_rows:
        approvals = approvals_by_run.get(run["pipeline_id"], [])
        if (status_filter != "ALL" or channel_filter != "all") and not approvals:
            continue
        batch = batches.get(run["source_batch_id"])
        ticket_count = sum(len(approval.get("zendeskTickets") or []) for approval in approvals)
        email_count = sum(1 for approval in approvals if approval.get("contactType") == "email")
        phone_count = sum(1 for approval in approvals if approval.get("contactType") == "phone")
        live_count = sum(1 for approval in approvals if approval.get("status") == "APPROVED")
        failed_count = sum(1 for approval in approvals if site_status_matches("failed", approval.get("status", "")))
        deploy_requested = sum(
            1
            for approval in approvals
            for ticket in approval.get("zendeskTickets") or []
            if ticket.get("payload", {}).get("deployRequested")
        )
        province_stats = safe_json_loads(batch["province_stats_json"], {}) if batch else {}
        raw_fetched = sum((stats or {}).get("rawItems", 0) for stats in province_stats.values()) if isinstance(province_stats, dict) else run["lead_count"]
        eligible_count = sum((stats or {}).get("eligible", 0) for stats in province_stats.values()) if isinstance(province_stats, dict) else run["lead_count"]
        github_exported = sum(1 for approval in approvals if (approval.get("githubExport") or {}).get("commitSha"))
        groups.append(
            {
                "groupId": run["pipeline_id"],
                "pipelineId": run["pipeline_id"],
                "batchId": run["source_batch_id"],
                "createdAt": run["created_at"],
                "updatedAt": run["updated_at"],
                "status": run["status"],
                "query": batch["query"] if batch else None,
                "location": batch["location"] if batch else None,
                "leadCount": run["lead_count"],
                "duplicatesSkipped": batch["duplicates_skipped"] if batch else 0,
                "emailLeads": email_count,
                "phoneLeads": phone_count,
                "generated": len(approvals),
                "rawFetched": raw_fetched or run["lead_count"],
                "eligible": eligible_count or run["lead_count"],
                "githubExported": github_exported,
                "zendeskTickets": ticket_count,
                "zendeskPending": max(0, len(approvals) - ticket_count),
                "deployApproved": deploy_requested,
                "live": live_count,
                "failed": failed_count,
                "chartSteps": [
                    {"label": "Fetched", "value": raw_fetched or run["lead_count"]},
                    {"label": "Eligible", "value": eligible_count or run["lead_count"]},
                    {"label": "Generated", "value": len(approvals)},
                    {"label": "GitHub", "value": github_exported},
                    {"label": "Zendesk", "value": ticket_count},
                    {"label": "Pending", "value": max(0, len(approvals) - live_count - failed_count)},
                    {"label": "Live", "value": live_count},
                    {"label": "Failed", "value": failed_count},
                ],
                "approvals": approvals,
                "warnings": safe_json_loads(run["warnings_json"], []),
            }
        )

    total = len(groups)
    start = (safe_page - 1) * safe_page_size
    end = start + safe_page_size
    return {
        "groups": groups[start:end],
        "count": len(groups[start:end]),
        "total": total,
        "page": safe_page,
        "pageSize": safe_page_size,
        "totalPages": max(1, (total + safe_page_size - 1) // safe_page_size),
    }


def zendesk_auth_context() -> Tuple[str, Tuple[str, str]]:
    zendesk_subdomain = require_env("ZENDESK_SUBDOMAIN")
    zendesk_email = require_env("ZENDESK_EMAIL")
    zendesk_token = require_env("ZENDESK_API_TOKEN")
    return zendesk_subdomain, (f"{zendesk_email}/token", zendesk_token)


def update_zendesk_ticket_comment(ticket_id: int, body: str, public: bool, extra_ticket_fields: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    zendesk_subdomain, auth = zendesk_auth_context()
    payload = {"ticket": {"comment": {"body": body, "public": public}}}
    if extra_ticket_fields:
        payload["ticket"].update(extra_ticket_fields)
    response = requests.put(
        f"https://{zendesk_subdomain}.zendesk.com/api/v2/tickets/{ticket_id}.json",
        json=payload,
        auth=auth,
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("ticket", {})


def update_zendesk_ticket_tags(
    ticket_id: int,
    *,
    add: Optional[Iterable[str]] = None,
    remove: Optional[Iterable[str]] = None,
) -> List[str]:
    """Mutate managed tags without replacing tags owned by Zendesk administrators or triggers."""
    add_tags = list(dict.fromkeys(compact_text(tag) for tag in (add or []) if compact_text(tag)))
    remove_tags = list(dict.fromkeys(compact_text(tag) for tag in (remove or []) if compact_text(tag)))
    response: Dict[str, Any] = {}
    if remove_tags:
        response = zendesk_api_request(
            "delete",
            f"/tickets/{int(ticket_id)}/tags.json",
            payload={"tags": remove_tags},
        )
    if add_tags:
        response = zendesk_api_request(
            "put",
            f"/tickets/{int(ticket_id)}/tags.json",
            payload={"tags": add_tags},
        )
    tags = response.get("tags") if isinstance(response, dict) else None
    if not isinstance(tags, list):
        ticket = zendesk_api_request("get", f"/tickets/{int(ticket_id)}.json").get("ticket") or {}
        tags = ticket.get("tags") or []
    return list(dict.fromkeys(compact_text(tag) for tag in tags if compact_text(tag)))


EMAIL_CANCELLATION_MACRO_TITLE = "AI Site Factory::Email::10-day cancellation - notify customer"


def apply_zendesk_macro_to_ticket(ticket_id: int, macro_title: str) -> Dict[str, Any]:
    """Render an existing Zendesk macro against a ticket and persist only its actions."""
    macros = zendesk_list_all("/macros.json", "macros")
    macro = next(
        (
            item
            for item in macros
            if bool(item.get("active", True))
            and compact_text(item.get("title")).casefold() == compact_text(macro_title).casefold()
        ),
        None,
    )
    if not macro:
        raise RuntimeError(f"Zendesk macro '{macro_title}' is missing or inactive.")

    preview = zendesk_api_request(
        "get",
        f"/tickets/{int(ticket_id)}/macros/{macro['id']}/apply.json",
        params={"normalize_comment": "true"},
    )
    preview_ticket = ((preview.get("result") or {}).get("ticket") or {})
    preview_comment = preview_ticket.get("comment") or {}
    actions = macro.get("actions") or []

    add_tags: List[str] = []
    remove_tags: List[str] = ["asf_10_day_cancellation_due"]
    custom_fields: List[Dict[str, Any]] = []
    status = compact_text(preview_ticket.get("status"))
    public = str(preview_comment.get("public", "false")).lower() == "true"
    for action in actions:
        field = compact_text(action.get("field"))
        value = action.get("value")
        if field == "current_tags":
            add_tags.extend(compact_text(value).split())
        elif field == "remove_tags":
            remove_tags.extend(compact_text(value).split())
        elif field == "status":
            status = compact_text(value)
        elif field == "comment_mode_is_public":
            public = str(value).lower() == "true"
        elif field.startswith("custom_fields_"):
            field_id = field.removeprefix("custom_fields_")
            custom_fields.append(
                {
                    "id": int(field_id) if field_id.isdigit() else field_id,
                    "value": value,
                }
            )

    tags = update_zendesk_ticket_tags(
        int(ticket_id),
        remove=remove_tags,
        add=add_tags,
    )
    html_body = compact_text(preview_comment.get("html_body") or preview_comment.get("body"))
    if not html_body:
        raise RuntimeError(f"Zendesk macro '{macro_title}' did not render a ticket comment.")
    ticket_payload: Dict[str, Any] = {
        "comment": {"html_body": html_body, "public": public},
    }
    if status:
        ticket_payload["status"] = status
    if custom_fields:
        ticket_payload["custom_fields"] = custom_fields
    ticket = (
        zendesk_api_request(
            "put",
            f"/tickets/{int(ticket_id)}.json",
            payload={"ticket": ticket_payload},
        ).get("ticket")
        or {}
    )
    return {
        "macroId": int(macro["id"]),
        "macroTitle": macro.get("title"),
        "ticket": ticket,
        "tags": tags,
        "public": public,
        "status": ticket.get("status") or status,
    }


@app.get("/api/health")
def get_public_health():
    return {
        "status": "READY",
        "startedAt": STARTED_AT.isoformat(),
        "uptimeSeconds": int((datetime.now(timezone.utc) - STARTED_AT).total_seconds()),
    }


def update_campaign_workflow(
    approval_id: str,
    status: str,
    *,
    requested: bool = False,
    ai_generation_increment: int = 0,
    repo_created: Optional[bool] = None,
    repo_url: Optional[str] = None,
    live_url: Optional[str] = None,
    deployment_history_id: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    timestamp = now_iso()
    with get_pipeline_db() as db:
        deployment = db.execute(
            "SELECT * FROM campaign_deployments WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        if not deployment:
            return
        requested_at = deployment["requested_at"] or (timestamp if requested else None)
        completed_at = timestamp if status in {"DEPLOYED", "REUSED_DEPLOYMENT"} else deployment["completed_at"]
        db.execute(
            """
            UPDATE campaign_deployments
            SET status = ?, ai_generation_count = ?, repo_created = ?, repo_url = ?,
                live_url = ?, deployment_history_id = ?, requested_at = ?, completed_at = ?,
                error = ?, updated_at = ?
            WHERE approval_id = ?
            """,
            (
                status,
                (deployment["ai_generation_count"] or 0) + ai_generation_increment,
                deployment["repo_created"] if repo_created is None else (1 if repo_created else 0),
                repo_url or deployment["repo_url"],
                live_url or deployment["live_url"],
                deployment_history_id or deployment["deployment_history_id"],
                requested_at,
                completed_at,
                sanitize_message(error) if error else None,
                timestamp,
                approval_id,
            ),
        )
        table = "campaign_email_leads" if deployment["channel"] == "email" else "campaign_call_leads"
        db.execute(
            f"UPDATE {table} SET status = ?, deploy_requested = ?, updated_at = ? WHERE approval_id = ?",
            (status, 1 if requested_at else 0, timestamp, approval_id),
        )


def cancel_campaign_workflow(approval_id: str) -> None:
    timestamp = now_iso()
    with get_pipeline_db() as db:
        deployment = db.execute(
            "SELECT * FROM campaign_deployments WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        if not deployment:
            return
        db.execute(
            """
            UPDATE campaign_deployments
            SET status = 'CANCELLED', live_url = NULL, deployment_history_id = NULL,
                requested_at = NULL, completed_at = ?, error = NULL, updated_at = ?
            WHERE approval_id = ?
            """,
            (timestamp, timestamp, approval_id),
        )
        table = "campaign_email_leads" if deployment["channel"] == "email" else "campaign_call_leads"
        db.execute(
            f"UPDATE {table} SET status = 'CANCELLED', deploy_requested = 0, updated_at = ? WHERE approval_id = ?",
            (timestamp, approval_id),
        )


def campaign_outreach_template(context: Dict[str, Any], site_url: str) -> Dict[str, Any]:
    business_name = compact_text(context.get("businessName"), "your business")
    industry = compact_text(context.get("industry"), "business")
    location = compact_text(context.get("location"), "your area")
    return {
        "subject": f"A website concept for {business_name}",
        "body": (
            f"Hi {business_name} team,\n\n"
            f"We found {business_name} while researching {industry} businesses in {location}. "
            f"We created a mobile-friendly website concept using the public business information available in your listing.\n\n"
            f"Preview: {site_url}\n\n"
            "If you would like to use or adjust it, reply to this message and the assigned agent can help."
        ),
        "contactType": compact_text(context.get("contactChannel"), "email" if context.get("email") else "phone"),
        "generatedBy": "campaign-template",
    }


ZENDESK_LIFECYCLE_TAGS = {
    "asf_deploy_pending",
    "asf_deploy_requested",
    "asf_stage_intake",
    "asf_stage_generating",
    "asf_artifact_ready",
    "asf_repo_ready",
    "asf_stage_deploying",
    "asf_deployed",
    "asf_stage_live",
    "asf_deployment_cancelled",
    "asf_stage_cancelled",
    "asf_cancel_email_fired",
    "asf_cancel_phone_fired",
    "asf_10_day_cancellation_due",
    "asf_generation_failed",
    "asf_deploy_failed",
    "asf_stage_failed",
}


def update_zendesk_deployment_lifecycle(
    row: sqlite3.Row,
    ticket_id: Optional[int],
    status: str,
    message: str,
) -> Optional[Dict[str, Any]]:
    if not ticket_id:
        return None
    context = safe_json_loads(row["context_json"], {})
    channel = compact_text(context.get("contactChannel"), contact_type_from_context(context)).lower()
    link = get_zendesk_ticket_link(row["id"], channel, "intake", ticket_id)
    normalized = compact_text(status).upper()
    stage_tags = {
        "DEPLOY_REQUESTED": ["asf_deploy_requested", "asf_stage_generating"],
        "GENERATING": ["asf_deploy_requested", "asf_stage_generating"],
        "ARTIFACT_READY": ["asf_deploy_requested", "asf_artifact_ready", "asf_repo_ready"],
        "DEPLOYING": ["asf_deploy_requested", "asf_repo_ready", "asf_stage_deploying"],
        "GENERATION_FAILED": ["asf_generation_failed", "asf_stage_failed"],
        "DEPLOY_FAILED": ["asf_deploy_failed", "asf_stage_failed"],
        "FAILED": ["asf_deploy_failed", "asf_stage_failed"],
    }.get(normalized, ["asf_deploy_requested"])
    failed = normalized in {"FAILED", "GENERATION_FAILED", "DEPLOY_FAILED"}
    lead_status = "FAILED" if failed else "GENERATING"
    deploy_requested = not failed
    custom_fields = zendesk_custom_fields(
        {"deployRequested": deploy_requested, "leadStatus": lead_status, "contactChannel": channel}
    )
    extra_fields: Dict[str, Any] = {"status": "open"}
    if custom_fields:
        extra_fields["custom_fields"] = custom_fields
    removal_tags = set(ZENDESK_LIFECYCLE_TAGS)
    if failed:
        removal_tags.update({"asf_deploy_email_fired", "asf_deploy_phone_fired"})
        # Uncheck first. Removing the deploy trigger's fired tag while the field is
        # still checked would immediately start a second deployment attempt.
        ticket = update_zendesk_ticket_comment(
            int(ticket_id), message, public=False, extra_ticket_fields=extra_fields
        )
        tags = update_zendesk_ticket_tags(
            int(ticket_id),
            remove=removal_tags,
            add=stage_tags + ["asf_managed", f"asf_channel_{channel}"],
        )
    else:
        tags = update_zendesk_ticket_tags(
            int(ticket_id),
            remove=removal_tags,
            add=stage_tags + ["asf_managed", f"asf_channel_{channel}"],
        )
        ticket = update_zendesk_ticket_comment(
            int(ticket_id), message, public=False, extra_ticket_fields=extra_fields
        )
    payload = {
        **((link or {}).get("payload") or {}),
        "deployRequested": deploy_requested,
        "deploymentStage": normalized,
        "deploymentStageUpdatedAt": now_iso(),
    }
    return save_zendesk_ticket_link(
        row["id"], row["canonical_lead_key"], row["pipeline_id"], channel, "intake",
        int(ticket_id), (link or {}).get("ticketUrl"), ticket.get("status") or "open", tags, payload,
    )


def safe_update_zendesk_deployment_lifecycle(
    row: sqlite3.Row,
    ticket_id: Optional[int],
    status: str,
    message: str,
) -> Optional[Dict[str, Any]]:
    try:
        return update_zendesk_deployment_lifecycle(row, ticket_id, status, message)
    except Exception as error:
        log_event(
            "warning",
            "zendesk.lifecycle.update_failed",
            str(error),
            approvalId=row["id"],
            ticketId=ticket_id,
            lifecycleStatus=status,
        )
        return None


def cancel_approval_deployment(
    row: sqlite3.Row,
    ticket_id: Optional[int],
    channel: str,
    *,
    scheduled: bool = False,
) -> Dict[str, Any]:
    link = get_zendesk_ticket_link(row["id"], channel, "intake", ticket_id) if ticket_id else None
    link_payload = (link or {}).get("payload") or {}
    known_live_url = compact_text(link_payload.get("liveUrl"))
    if not known_live_url:
        live_url_field_id = compact_text(get_zendesk_field_settings().get("liveUrl"))
        known_live_url = compact_text(
            next(
                (
                    item.get("value")
                    for item in (link_payload.get("customFields") or [])
                    if compact_text(item.get("id")) == live_url_field_id
                ),
                "",
            )
        )
    cancellation = cancel_netlify_site_for_lead(row["canonical_lead_key"], known_live_url)
    if cancellation.get("status") == "NO_SITE":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "NETLIFY_SITE_NOT_FOUND_FOR_CANCELLATION",
                "message": "The deployed Netlify site could not be located from local state or the Zendesk live URL.",
                "ticketId": ticket_id,
            },
        )
    timestamp = now_iso()
    github_export = safe_json_loads(row["github_export_json"], {})
    next_status = "PENDING" if row["html"] and github_export.get("commitSha") else "AWAITING_DEPLOYMENT"

    with get_pipeline_db() as db:
        db.execute(
            """
            UPDATE approval_records
            SET status = ?, deployment_history_id = NULL, outreach_json = NULL,
                updated_at = ?, notes = ?
            WHERE id = ?
            """,
            (
                next_status,
                timestamp,
                "Deployment cancelled after the Zendesk deploy checkbox was unchecked.",
                row["id"],
            ),
        )
        db.execute("DELETE FROM deploy_webhook_claims WHERE approval_id = ?", (row["id"],))
    cancel_campaign_workflow(row["id"])

    ticket_result: Optional[Dict[str, Any]] = None
    cancellation_macro: Optional[Dict[str, Any]] = None
    if ticket_id:
        tags = update_zendesk_ticket_tags(
            int(ticket_id),
            remove=[
                *ZENDESK_LIFECYCLE_TAGS,
                "asf_deploy_email_fired",
                "asf_deploy_phone_fired",
            ],
            add=[
                "asf_managed",
                f"asf_channel_{channel}",
                "asf_deployment_cancelled",
                "asf_stage_cancelled",
            ],
        )
        custom_fields = zendesk_custom_fields(
            {
                "deployRequested": False,
                "leadStatus": "AWAITING_DEPLOYMENT",
                "contactChannel": channel,
            }
        )
        live_url_field_id = compact_text(get_zendesk_field_settings().get("liveUrl"))
        if live_url_field_id:
            custom_fields.append(
                {
                    "id": int(live_url_field_id) if live_url_field_id.isdigit() else live_url_field_id,
                    "value": None,
                }
            )
        extra_fields: Dict[str, Any] = {"status": "open"}
        if custom_fields:
            extra_fields["custom_fields"] = custom_fields
        ticket = update_zendesk_ticket_comment(
            int(ticket_id),
            (
                "AI Site Factory deployment cancelled\n\n"
                "The Netlify site has been disabled because the Deploy site checkbox was unchecked. "
                "The GitHub artifact remains available for audit. Rechecking the field will start a new deployment."
            ),
            public=False,
            extra_ticket_fields=extra_fields,
        )
        if channel == "email" and scheduled:
            cancellation_macro = apply_zendesk_macro_to_ticket(
                int(ticket_id),
                EMAIL_CANCELLATION_MACRO_TITLE,
            )
            ticket = cancellation_macro.get("ticket") or ticket
            tags = cancellation_macro.get("tags") or tags
        payload = {
            **((link or {}).get("payload") or {}),
            "deployRequested": False,
            "deploymentCancelled": True,
            "deploymentCancelledAt": timestamp,
            "previousLiveUrl": cancellation.get("previousUrl"),
            "liveUrl": None,
            "cancellationMacro": (
                {
                    "id": cancellation_macro.get("macroId"),
                    "title": cancellation_macro.get("macroTitle"),
                    "appliedAt": now_iso(),
                }
                if cancellation_macro
                else None
            ),
        }
        saved = save_zendesk_ticket_link(
            row["id"],
            row["canonical_lead_key"],
            row["pipeline_id"],
            channel,
            "intake",
            int(ticket_id),
            (link or {}).get("ticketUrl"),
            ticket.get("status") or "open",
            tags,
            payload,
        )
        ticket_result = {
            "ticketId": int(ticket_id),
            "ticketUrl": saved.get("ticketUrl"),
            "tags": tags,
        }

    return {
        "approvalId": row["id"],
        "status": "CANCELLED",
        "nextApprovalStatus": next_status,
        "scheduled": scheduled,
        "netlify": cancellation,
        "zendesk": ticket_result,
        "cancellationMacro": (
            {
                "id": cancellation_macro.get("macroId"),
                "title": cancellation_macro.get("macroTitle"),
                "public": cancellation_macro.get("public"),
                "status": cancellation_macro.get("status"),
            }
            if cancellation_macro
            else None
        ),
    }


def update_existing_intake_ticket(
    row: sqlite3.Row,
    deployment: Dict[str, Any],
    outreach: Dict[str, Any],
    ticket_id_override: Optional[int] = None,
) -> Dict[str, Any]:
    context = safe_json_loads(row["context_json"], {})
    channel = compact_text(context.get("contactChannel"), contact_type_from_context(context)).lower()
    contract = require_zendesk_ticket_contract(channel)
    link = get_zendesk_ticket_link(row["id"], channel, "intake", ticket_id_override)
    if ticket_id_override is not None and (
        not link
        or compact_text(link.get("ticketId")) != compact_text(ticket_id_override)
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ZENDESK_TICKET_LINK_MISMATCH",
                "message": "The invoking Zendesk ticket is not linked to this approval and channel.",
                "approvalId": row["id"],
                "channel": channel,
                "ticketId": ticket_id_override,
            },
        )
    ticket_id = (link or {}).get("ticketId")
    live_url = compact_text(deployment.get("url"))
    repo_url = compact_text(
        deployment.get("githubRepoUrl")
        or (deployment.get("githubExport") or {}).get("repoUrl")
        or (safe_json_loads(row["github_export_json"], {}) or {}).get("repoUrl")
    )
    if not ticket_id:
        return {
            "syncStatus": "LOCAL_ONLY",
            "ticketId": None,
            "liveLink": live_url,
            "contactType": channel,
            "message": "Deployment completed, but this campaign lead has no Zendesk ticket.",
        }
    if not live_url:
        raise HTTPException(status_code=502, detail="The completed deployment did not return a live URL for Zendesk.")

    custom_fields = zendesk_custom_fields(
        {
            "campaignId": context.get("campaignId"),
            "campaignName": context.get("campaignName"),
            "canonicalLeadKey": row["canonical_lead_key"],
            "approvalId": row["id"],
            "contactChannel": channel,
            "leadStatus": "DEPLOYED",
            "deployRequested": True,
            "liveUrl": live_url,
            "sourceUrl": context.get("sourceUrl"),
        }
    )
    extra_fields: Dict[str, Any] = {
        "status": "open",
        "brand_id": contract["brandId"],
        "ticket_form_id": contract["formId"],
    }
    if custom_fields:
        extra_fields["custom_fields"] = custom_fields
    body = (
        "AI Site Factory deployment completed\n\n"
        f"Business: {context.get('businessName')}\n"
        f"Campaign: {context.get('campaignName')}\n"
        f"Channel: {channel}\n"
        f"Live website: {live_url}\n"
        f"GitHub repository: {repo_url or 'Reused existing repository'}\n\n"
        f"Suggested outreach subject: {outreach.get('subject')}\n\n"
        f"Suggested outreach body:\n{outreach.get('body')}\n\n"
        "For email leads, review the draft and tick the email-send field. For call leads, use the live link during the call."
    )
    ticket = update_zendesk_ticket_comment(int(ticket_id), body, public=False, extra_ticket_fields=extra_fields)
    live_url_field_id = compact_text(contract["fieldIds"].get("liveUrl"))

    def live_url_is_confirmed(candidate: Dict[str, Any]) -> bool:
        values = {
            compact_text(item.get("id")): item.get("value")
            for item in (candidate.get("custom_fields") or [])
        }
        return (
            compact_text(candidate.get("brand_id")) == compact_text(contract["brandId"])
            and compact_text(candidate.get("ticket_form_id")) == compact_text(contract["formId"])
            and zendesk_ticket_field_value_matches(live_url, values.get(live_url_field_id))
        )

    if not live_url_is_confirmed(ticket):
        ticket = zendesk_api_request("get", f"/tickets/{ticket_id}.json").get("ticket") or {}
    if not live_url_is_confirmed(ticket):
        raise HTTPException(
            status_code=502,
            detail={
                "code": "ZENDESK_LIVE_URL_UPDATE_REJECTED",
                "message": "Zendesk did not preserve the managed brand, form, and live URL field after deployment.",
                "ticketId": ticket_id,
                "liveUrlFieldId": live_url_field_id,
            },
        )
    # Arm cancellation only after Zendesk has confirmed the checked deploy field and
    # live URL. Adding asf_deployed first can make a cancellation trigger observe the
    # ticket's old unchecked value and immediately disable a brand-new site.
    tags = update_zendesk_ticket_tags(
        int(ticket_id),
        remove=ZENDESK_LIFECYCLE_TAGS,
        add=[
            "asf_managed",
            "asf_deploy_requested",
            "asf_deployed",
            "asf_stage_live",
            "asf_repo_ready",
            f"asf_channel_{channel}",
        ],
    )
    payload = {
        **((link or {}).get("payload") or {}),
        "deployRequested": True,
        "deployedAt": now_iso(),
        "liveUrl": live_url,
        "repoUrl": repo_url,
        "outreach": outreach,
    }
    saved = save_zendesk_ticket_link(
        row["id"],
        row["canonical_lead_key"],
        row["pipeline_id"],
        channel,
        "intake",
        int(ticket_id),
        (link or {}).get("ticketUrl"),
        ticket.get("status") or "open",
        tags,
        payload,
    )
    return {
        "syncStatus": "TICKET_UPDATED",
        "ticketId": ticket_id,
        "ticketUrl": saved.get("ticketUrl"),
        "liveLink": live_url,
        "contactType": channel,
        "tags": tags,
    }


def deferred_lead_from_context(row: sqlite3.Row, context: Dict[str, Any]) -> DiscoveredLead:
    return DiscoveredLead(
        leadKey=compact_text(row["lead_key"], stable_lead_key(row["business_name"], row["canonical_lead_key"])),
        canonicalLeadKey=row["canonical_lead_key"],
        businessName=row["business_name"],
        email=normalize_email_identity(context.get("email")),
        phone=compact_text(context.get("phone")) or None,
        website=None,
        domain=None,
        category=compact_text(context.get("industry") or context.get("category"), "Local service"),
        address=compact_text(context.get("address")) or None,
        location=compact_text(context.get("location"), "South Africa"),
        province=compact_text(context.get("province")) or None,
        rating=context.get("rating"),
        reviewsCount=context.get("reviewsCount"),
        source=compact_text(context.get("source"), "apify-google-maps"),
        sourceUrl=normalize_url(context.get("sourceUrl")),
        notes=compact_text(context.get("notes")) or None,
        raw=context.get("rawLead") or {},
    )


def prepare_deferred_approval(approval_id: str) -> sqlite3.Row:
    row = get_approval_or_404(approval_id)
    current_export = safe_json_loads(row["github_export_json"], {})
    if row["html"] and current_export.get("commitSha"):
        return row
    context = safe_json_loads(row["context_json"], {})
    if not context.get("intakeDeferred"):
        return row

    update_campaign_workflow(approval_id, "GENERATING", requested=True)
    with get_pipeline_db() as db:
        reusable = db.execute(
            """
            SELECT * FROM approval_records
            WHERE canonical_lead_key = ? AND id != ? AND html IS NOT NULL
              AND github_export_json IS NOT NULL AND status IN ('PENDING', 'DEPLOY_FAILED')
            ORDER BY updated_at DESC LIMIT 1
            """,
            (row["canonical_lead_key"], approval_id),
        ).fetchone()
    if reusable:
        export = safe_json_loads(reusable["github_export_json"], {})
        with get_pipeline_db() as db:
            db.execute(
                """
                UPDATE approval_records
                SET status = 'PENDING', html = ?, html_checksum = ?, site_content_json = ?,
                    github_export_json = ?, publish_mode = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    reusable["html"], reusable["html_checksum"], reusable["site_content_json"],
                    reusable["github_export_json"], reusable["publish_mode"], now_iso(), approval_id,
                ),
            )
        record_skipped_pipeline_step(
            row["pipeline_id"], row["canonical_lead_key"], "ai_site_generation",
            "Reused an existing generated artifact for this lead.", provider="ai",
            details={"approvalId": approval_id, "reusedApprovalId": reusable["id"]},
        )
        update_campaign_workflow(
            approval_id, "ARTIFACT_READY", requested=True, repo_created=False,
            repo_url=export.get("repoUrl"),
        )
        return get_approval_or_404(approval_id)

    lead = deferred_lead_from_context(row, context)

    def run_deferred_step(step: str, provider: str, callback):
        started = now_iso()
        try:
            value = callback()
        except Exception as error:
            record_pipeline_step(
                row["pipeline_id"], row["canonical_lead_key"], step, "FAILED", provider,
                str(error), started, now_iso(), retryable=True, details={"approvalId": approval_id},
            )
            raise
        record_pipeline_step(
            row["pipeline_id"], row["canonical_lead_key"], step, "COMPLETED", provider,
            f"{step} completed.", started, now_iso(), details={"approvalId": approval_id},
        )
        return value

    site_html = (row["html"] or "").strip()
    generated_now = not bool(site_html)
    cleaned_context = context
    site_content = safe_json_loads(row["site_content_json"], {})

    try:
        if generated_now:
            cleaned_context = build_public_lead_context(lead, {}, row["canonical_lead_key"])
            cleaned_context.update(
                {key: value for key, value in context.items() if key in {"campaignId", "campaignName", "batchId", "contactChannel", "contactName", "intakeDeferred"}}
            )
            brief = run_deferred_step("groq_compact_lead", "groq", lambda: compact_lead_with_groq(cleaned_context))
            final_html_result = run_deferred_step("gemini_final_html", "gemini", lambda: generate_final_html_with_gemini(brief))
            quality_result = run_deferred_step(
                "seo_validation",
                "local",
                lambda: prepare_generated_site_artifact(final_html_result["html"], brief),
            )
            site_html = quality_result["html"]
            seo_validation = quality_result["seoValidation"]
            site_content = {
                "deferredGeneration": True,
                "generatedOnDeployRequest": True,
                "groqBrief": brief,
                "geminiQaNotes": final_html_result.get("qaNotes"),
                "stylingLibraries": final_html_result.get("stylingLibraries"),
                "siteProfile": HIGHLY_INTERACTIVE_SITE_PROFILE,
                "seoValidation": seo_validation,
                "finalHtmlChecksum": html_checksum(site_html),
            }
            # Persist the expensive AI artifact before GitHub I/O so a transient
            # repository failure can retry export without calling the models again.
            with get_pipeline_db() as db:
                db.execute(
                    """
                    UPDATE approval_records
                    SET html = ?, html_checksum = ?, context_json = ?, site_content_json = ?,
                        publish_mode = 'github-netlify', updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        site_html,
                        html_checksum(site_html),
                        json.dumps(cleaned_context, default=str),
                        json.dumps(site_content, default=str),
                        now_iso(),
                        approval_id,
                    ),
                )
        github_export = run_deferred_step(
            "github_export",
            "github",
            lambda: export_site_to_github(
                canonical_key=row["canonical_lead_key"],
                business_name=row["business_name"],
                site_html=site_html,
                pipeline_id=row["pipeline_id"],
                approval_id=approval_id,
            ),
        )
        with get_pipeline_db() as db:
            db.execute(
                """
                UPDATE approval_records
                SET status = 'PENDING', html = ?, html_checksum = ?, context_json = ?,
                    site_content_json = ?, github_export_json = ?, publish_mode = 'github-netlify',
                    updated_at = ?, errors_json = NULL
                WHERE id = ?
                """,
                (
                    site_html, html_checksum(site_html), json.dumps(cleaned_context, default=str),
                    json.dumps(site_content, default=str), json.dumps(github_export, default=str),
                    now_iso(), approval_id,
                ),
            )
        update_campaign_workflow(
            approval_id,
            "ARTIFACT_READY",
            requested=True,
            ai_generation_increment=1 if generated_now else 0,
            repo_created=compact_text(github_export.get("exportAction")).upper() == "CREATED",
            repo_url=github_export.get("repoUrl"),
        )
        return get_approval_or_404(approval_id)
    except Exception as error:
        failure_step = "github_export" if site_html else "deferred_generation"
        failure_status = "EXPORT_FAILED" if site_html else "GENERATION_FAILED"
        with get_pipeline_db() as db:
            db.execute(
                "UPDATE approval_records SET status = ?, errors_json = ?, updated_at = ? WHERE id = ?",
                (
                    failure_status,
                    json.dumps([structured_pipeline_error(failure_step, error, retryable=True)], default=str),
                    now_iso(),
                    approval_id,
                ),
            )
        update_campaign_workflow(approval_id, failure_status, requested=True, error=str(error))
        label = "site export" if failure_status == "EXPORT_FAILED" else "site generation"
        raise HTTPException(status_code=502, detail=f"Deferred {label} failed: {sanitize_message(error)}")


def reuse_existing_live_deployment(
    row: sqlite3.Row,
    actor: str,
    notes: Optional[str],
    ticket_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    history_row = latest_deployment_history_for_lead(row["canonical_lead_key"])
    deployment = deployment_from_history(history_row)
    if not history_row or not deployment or not deployment.get("url") or compact_text(deployment.get("state")).lower() != "ready":
        return None
    context = safe_json_loads(row["context_json"], {})
    outreach = campaign_outreach_template(context, deployment.get("url"))
    with get_pipeline_db() as db:
        db.execute(
            """
            UPDATE approval_records
            SET status = 'APPROVED', approved_by = ?, notes = ?, deployment_history_id = ?,
                outreach_json = ?, updated_at = ? WHERE id = ?
            """,
            (actor, notes, history_row["id"], json.dumps(outreach, default=str), now_iso(), row["id"]),
        )
    zendesk = update_existing_intake_ticket(row, deployment, outreach, ticket_id)
    update_campaign_workflow(
        row["id"], "REUSED_DEPLOYMENT", requested=True, live_url=deployment.get("url"),
        deployment_history_id=history_row["id"], repo_url=deployment.get("githubRepoUrl"),
    )
    return {"deployment": deployment, "outreach": outreach, "zendesk": zendesk, "reused": True}


def get_approval_if_present(approval_id: Optional[str]) -> Optional[sqlite3.Row]:
    normalized = compact_text(approval_id)
    if not normalized:
        return None
    with get_pipeline_db() as db:
        return db.execute("SELECT * FROM approval_records WHERE id = ?", (normalized,)).fetchone()


ZENDESK_RESTORE_WORKFLOW_TAGS = {
    "asf_deploy_requested",
    "asf_deployed",
    "asf_deployment_cancelled",
    "asf_generation_failed",
    "asf_deploy_failed",
    "asf_stage_failed",
}


def managed_zendesk_restore_state(
    ticket_tags: Set[str],
    deploy_requested: Any,
    live_url: str,
) -> Dict[str, Any]:
    """Derive local dashboard state from Zendesk without changing the ticket."""
    cancelled = "asf_deployment_cancelled" in ticket_tags or "asf_stage_cancelled" in ticket_tags
    deployed = "asf_deployed" in ticket_tags or "asf_stage_live" in ticket_tags
    generation_failed = "asf_generation_failed" in ticket_tags
    deploy_failed = "asf_deploy_failed" in ticket_tags or "asf_stage_failed" in ticket_tags
    requested = zendesk_ticket_field_value_matches(True, deploy_requested) or (
        "asf_deploy_requested" in ticket_tags
    )

    if cancelled:
        deployment_status = "CANCELLED"
        approval_status = "AWAITING_DEPLOYMENT"
        requested = False
    elif deployed and live_url:
        deployment_status = "DEPLOYED"
        approval_status = "APPROVED"
        requested = True
    elif generation_failed:
        deployment_status = "GENERATION_FAILED"
        approval_status = "GENERATION_FAILED"
    elif deploy_failed:
        deployment_status = "DEPLOY_FAILED"
        approval_status = "EXPORT_FAILED"
    elif requested:
        deployment_status = "DEPLOY_REQUESTED"
        approval_status = "AWAITING_DEPLOYMENT"
    else:
        deployment_status = "AWAITING_DEPLOYMENT"
        approval_status = "AWAITING_DEPLOYMENT"

    generated = deployed or cancelled or generation_failed or deploy_failed or bool(
        ticket_tags.intersection({"asf_artifact_ready", "asf_repo_ready", "asf_stage_deploying"})
    )
    repo_created = deployed or cancelled or "asf_repo_ready" in ticket_tags
    return {
        "approvalStatus": approval_status,
        "deploymentStatus": deployment_status,
        "requested": requested,
        "aiGenerationCount": 1 if generated else 0,
        "repoCreated": repo_created,
        "liveUrl": live_url if deployment_status == "DEPLOYED" else None,
    }


def managed_zendesk_restore_location(value: Any, address: Any) -> str:
    location = compact_text(value)
    if location and not (
        (location.startswith("{") or location.startswith("["))
        and "lat" in location.lower()
        and "lng" in location.lower()
    ):
        return location
    address_parts = [part.strip() for part in compact_text(address).split(",") if part.strip()]
    country = address_parts[-1] if address_parts else "South Africa"
    for part in reversed(address_parts[:-1]):
        if re.fullmatch(r"[\d\s-]+", part):
            continue
        if any(character.isdigit() for character in part):
            continue
        return f"{part}, {country}" if normalize_identity_text(part) != normalize_identity_text(country) else country
    return country or "South Africa"


def recover_managed_zendesk_webhook_approval(
    request: ZendeskWebhookRequest,
    *,
    ticket_override: Optional[Dict[str, Any]] = None,
    restore_current_state: bool = False,
) -> sqlite3.Row:
    """Rebuild a deferred campaign approval only from an exact managed Zendesk ticket contract."""
    ticket_id = request.zendeskTicketId
    requested_channel = compact_text(request.channel).lower()
    action = compact_text(request.action).lower().replace("-", "_")
    if not ticket_id or requested_channel not in {"email", "phone"}:
        raise HTTPException(status_code=404, detail="Could not resolve approval for Zendesk webhook.")
    deploy_recovery_actions = {"deploy", "deploy_site", "approve_deploy", "deploy_requested"}
    cancellation_recovery_actions = {
        "cancel_deployment",
        "cancel_deploy",
        "undeploy_site",
        "deployment_cancelled",
    }
    is_cancellation = action in cancellation_recovery_actions
    restore_actions = {"restore_managed_ticket"} if restore_current_state else set()
    if action not in deploy_recovery_actions | cancellation_recovery_actions | restore_actions:
        raise HTTPException(
            status_code=409,
            detail="Only a managed deploy or cancellation webhook can recover a missing deferred approval.",
        )

    try:
        contract = require_zendesk_ticket_contract(requested_channel)
    except HTTPException as error:
        detail = error.detail if isinstance(error.detail, dict) else {}
        if error.status_code != 409 or detail.get("code") != "ZENDESK_TICKET_CONTRACT_INVALID":
            raise
        reconciliation = reconcile_zendesk_field_settings_from_live_instance()
        if not reconciliation.get("reconciled"):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ZENDESK_FIELD_MAPPING_NOT_READY",
                    "message": "The live Zendesk field mapping could not be fully reconciled before ticket recovery.",
                    "missing": reconciliation.get("missing", []),
                    "conflicts": reconciliation.get("conflicts", []),
                },
            ) from error
        contract = require_zendesk_ticket_contract(requested_channel)
    ticket = ticket_override or zendesk_api_request("get", f"/tickets/{ticket_id}.json").get("ticket") or {}
    initial_field_values = {
        compact_text(item.get("id")): item.get("value")
        for item in (ticket.get("custom_fields") or [])
    }
    mapping_mismatch = (
        compact_text(request.approvalId)
        and compact_text(
            initial_field_values.get(compact_text(contract["fieldIds"].get("approvalId")))
        ) != compact_text(request.approvalId)
    ) or (
        compact_text(request.canonicalLeadKey)
        and compact_text(
            initial_field_values.get(compact_text(contract["fieldIds"].get("canonicalLeadKey")))
        ) != compact_text(request.canonicalLeadKey)
    )
    if mapping_mismatch:
        reconciliation = reconcile_zendesk_field_settings_from_live_instance()
        if not reconciliation.get("reconciled"):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ZENDESK_FIELD_MAPPING_NOT_READY",
                    "message": "The live Zendesk field mapping could not be fully reconciled before ticket recovery.",
                    "missing": reconciliation.get("missing", []),
                    "conflicts": reconciliation.get("conflicts", []),
                },
            )
        contract = require_zendesk_ticket_contract(requested_channel)
    ticket_tags = {compact_text(tag) for tag in (ticket.get("tags") or []) if compact_text(tag)}
    required_tags = {
        "ai_site_factory",
        "asf_managed",
        "asf_intake",
        f"asf_channel_{requested_channel}",
        "asf_form_email_lead" if requested_channel == "email" else "asf_form_call_lead",
    }
    if is_cancellation:
        required_tags.add("asf_deployed")
    if (
        compact_text(ticket.get("id")) != compact_text(ticket_id)
        or compact_text(ticket.get("brand_id")) != compact_text(contract["brandId"])
        or compact_text(ticket.get("ticket_form_id")) != compact_text(contract["formId"])
        or not required_tags.issubset(ticket_tags)
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ZENDESK_ORPHAN_TICKET_CONTRACT_INVALID",
                "message": "The Zendesk ticket cannot recover local state because its managed route or tags do not match.",
                "ticketId": ticket_id,
            },
        )

    field_values = {
        compact_text(item.get("id")): item.get("value")
        for item in (ticket.get("custom_fields") or [])
    }

    def managed_value(key: str) -> Any:
        return field_values.get(compact_text(contract["fieldIds"].get(key)))

    campaign_id = compact_text(managed_value("campaignId"))
    campaign_name = compact_text(managed_value("campaignName"))
    canonical_key = compact_text(managed_value("canonicalLeadKey"))
    pipeline_id = compact_text(managed_value("pipelineId"))
    approval_id = compact_text(managed_value("approvalId"))
    business_name = compact_text(managed_value("businessName"))
    channel_value = compact_text(managed_value("contactChannel")).lower()
    expected_channel_values = {
        requested_channel,
        f"asf_cf_channel_{requested_channel}",
    }
    required_values = {
        "campaignId": campaign_id,
        "campaignName": campaign_name,
        "canonicalLeadKey": canonical_key,
        "pipelineId": pipeline_id,
        "approvalId": approval_id,
        "businessName": business_name,
    }
    missing_values = [key for key, value in required_values.items() if not value]
    external_id = compact_text(ticket.get("external_id"))
    expected_external_id = f"asf:{campaign_id}:{canonical_key}:{requested_channel}:intake"
    deploy_requested = managed_value("deployRequested")
    identity_mismatch = (
        external_id != expected_external_id
        or channel_value not in expected_channel_values
        or (compact_text(request.approvalId) and compact_text(request.approvalId) != approval_id)
        or (compact_text(request.canonicalLeadKey) and compact_text(request.canonicalLeadKey) != canonical_key)
        or (
            not restore_current_state
            and not zendesk_ticket_field_value_matches(not is_cancellation, deploy_requested)
        )
    )
    if missing_values or identity_mismatch:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ZENDESK_ORPHAN_TICKET_IDENTITY_INVALID",
                "message": "The Zendesk ticket cannot recover local state because its managed identity is incomplete or inconsistent.",
                "ticketId": ticket_id,
                "missing": missing_values,
            },
        )

    contact_email = normalize_email_identity(managed_value("contactEmail"))
    contact_phone = compact_text(managed_value("contactPhone"))
    if requested_channel == "email" and not contact_email:
        raise HTTPException(status_code=409, detail="The managed email ticket has no recoverable contact email.")
    if requested_channel == "phone" and not contact_phone:
        raise HTTPException(status_code=409, detail="The managed phone ticket has no recoverable contact phone.")

    industry = compact_text(managed_value("industry"), "Local service")
    address = compact_text(managed_value("address"))
    location = managed_zendesk_restore_location(managed_value("location"), address)
    contact_name = compact_text(managed_value("contactName"))
    source_url = compact_text(managed_value("sourceUrl"))
    batch_id = compact_text(managed_value("batchId"))
    lead_key = stable_lead_key("zendesk-recovery", campaign_id, canonical_key)
    source = "uploaded-lead-data" if "asf_source_upload" in ticket_tags else "apify-google-maps"
    timestamp = now_iso()
    context = {
        "campaignId": campaign_id,
        "campaignName": campaign_name,
        "batchId": batch_id or None,
        "canonicalLeadKey": canonical_key,
        "leadKey": lead_key,
        "businessName": business_name,
        "contactName": contact_name or None,
        "email": contact_email,
        "phone": contact_phone or None,
        "industry": industry,
        "category": industry,
        "location": location,
        "address": address or None,
        "source": source,
        "sourceUrl": source_url or None,
        "contactChannel": requested_channel,
        "intakeDeferred": True,
        "noWebsiteLead": True,
        "hasWebsite": False,
        "recoveredFromZendeskTicketId": int(ticket_id),
    }
    fields_json = json.dumps(
        {
            "campaignId": campaign_id,
            "campaignName": campaign_name,
            "businessName": business_name,
            "contactName": contact_name or None,
            "email": contact_email,
            "phone": contact_phone or None,
            "industry": industry,
            "location": location,
            "address": address or None,
            "sourceUrl": source_url or None,
            "channel": requested_channel,
        },
        default=str,
    )

    subdomain = require_env("ZENDESK_SUBDOMAIN")
    deployment_id = f"recovered-deployment-{approval_id}"
    channel_table = "campaign_email_leads" if requested_channel == "email" else "campaign_call_leads"
    link_payload = {
        "recoveredFromManagedTicket": True,
        "recoveredAt": timestamp,
        "externalId": external_id,
        "liveUrl": compact_text(managed_value("liveUrl")) or None,
        "customFields": ticket.get("custom_fields") or [],
    }
    restore_state = managed_zendesk_restore_state(
        ticket_tags,
        deploy_requested,
        compact_text(managed_value("liveUrl")),
    )
    if restore_current_state and (
        {"asf_deployed", "asf_stage_live"}.intersection(ticket_tags)
        and not restore_state["liveUrl"]
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ZENDESK_ORPHAN_TICKET_IDENTITY_INVALID",
                "message": "A deployed managed ticket cannot be restored without its live site URL.",
                "ticketId": ticket_id,
                "missing": ["liveUrl"],
            },
        )

    def recovery_conflict(entity: str, message: str) -> None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ZENDESK_ORPHAN_RECOVERY_CONFLICT",
                "entity": entity,
                "message": message,
                "ticketId": ticket_id,
            },
        )

    with get_pipeline_db() as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                INSERT OR IGNORE INTO lead_registry (
                    canonical_lead_key, lead_key, business_name, email, phone, category, address,
                    location, source, source_url, status, raw_json, first_seen_at, last_seen_at, discovery_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'DISCOVERED', ?, ?, ?, 1)
                """,
                (
                    canonical_key, lead_key, business_name, contact_email, contact_phone or None, industry,
                    address or None, location, source, source_url or None,
                    json.dumps({"recoveredFromZendeskTicketId": ticket_id}), timestamp, timestamp,
                ),
            )
            db.execute(
                """
                UPDATE lead_registry
                SET email = CASE WHEN email IS NULL OR TRIM(email) = '' THEN ? ELSE email END,
                    phone = CASE WHEN phone IS NULL OR TRIM(phone) = '' THEN ? ELSE phone END,
                    last_seen_at = ?
                WHERE canonical_lead_key = ?
                """,
                (contact_email, contact_phone or None, timestamp, canonical_key),
            )
            db.execute(
                """
                INSERT OR IGNORE INTO campaigns (
                    id, name, preset_id, industry, query, location, requested_count, discovered_count,
                    channel_filter, status, warnings_json, created_at, updated_at
                ) VALUES (?, ?, 'zendesk-recovery', ?, 'Recovered managed Zendesk intake', ?, 1, 1, ?,
                          'INTAKE_READY', '[]', ?, ?)
                """,
                (campaign_id, campaign_name, industry, location, requested_channel, timestamp, timestamp),
            )
            db.execute(
                """
                INSERT OR IGNORE INTO pipeline_runs (
                    pipeline_id, status, template_id, source_batch_id, created_at, updated_at,
                    lead_count, completed_count, pending_count, failed_count, warnings_json
                ) VALUES (?, 'INTAKE_READY', ?, NULL, ?, ?, 1, 0, 1, 0, '[]')
                """,
                (pipeline_id, FREEFORM_TEMPLATE_ID, timestamp, timestamp),
            )
            db.execute(
                """
                INSERT OR IGNORE INTO approval_records (
                    id, pipeline_id, canonical_lead_key, lead_key, business_name, status, html,
                    context_json, site_content_json, template_json, created_at, updated_at,
                    publish_mode, errors_json
                ) VALUES (?, ?, ?, ?, ?, 'AWAITING_DEPLOYMENT', NULL, ?, ?, ?, ?, ?, 'github-netlify', '[]')
                """,
                (
                    approval_id, pipeline_id, canonical_key, lead_key, business_name,
                    json.dumps(context, default=str),
                    json.dumps({"deferredGeneration": True, "recoveredFromZendesk": True}, default=str),
                    json.dumps(dict(FREEFORM_SITE_SPEC), default=str), timestamp, timestamp,
                ),
            )
            db.execute(
                """
                INSERT OR IGNORE INTO campaign_deployments (
                    id, campaign_id, canonical_lead_key, approval_id, channel, status,
                    requested_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'DEPLOY_REQUESTED', ?, ?, ?)
                """,
                (
                    deployment_id, campaign_id, canonical_key, approval_id,
                    requested_channel, timestamp, timestamp, timestamp,
                ),
            )
            if requested_channel == "email":
                db.execute(
                    """
                    INSERT OR IGNORE INTO campaign_email_leads (
                        id, campaign_id, canonical_lead_key, approval_id, business_name, contact_name,
                        email, source_url, status, deploy_requested, ticket_id, deployment_id,
                        fields_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'DEPLOY_REQUESTED', 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"recovered-email-{approval_id}", campaign_id, canonical_key, approval_id,
                        business_name, contact_name or None, contact_email, source_url or None, ticket_id,
                        deployment_id, fields_json, timestamp, timestamp,
                    ),
                )
            else:
                db.execute(
                    """
                    INSERT OR IGNORE INTO campaign_call_leads (
                        id, campaign_id, canonical_lead_key, approval_id, business_name, contact_name,
                        phone, source_url, status, deploy_requested, ticket_id, deployment_id,
                        fields_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'DEPLOY_REQUESTED', 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"recovered-phone-{approval_id}", campaign_id, canonical_key, approval_id,
                        business_name, contact_name or None, contact_phone, source_url or None, ticket_id,
                        deployment_id, fields_json, timestamp, timestamp,
                    ),
                )
            db.execute(
                """
                INSERT OR IGNORE INTO zendesk_ticket_links (
                    id, approval_id, canonical_lead_key, pipeline_id, external_id, ticket_id, ticket_url,
                    channel, stage, status, tags_json, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'intake', ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()), approval_id, canonical_key, pipeline_id, external_id, int(ticket_id),
                    f"https://{subdomain}.zendesk.com/agent/tickets/{ticket_id}", requested_channel,
                    ticket.get("status") or "open", json.dumps(sorted(ticket_tags), default=str),
                    json.dumps(link_payload, default=str), timestamp, timestamp,
                ),
            )

            approval_row = db.execute(
                "SELECT * FROM approval_records WHERE id = ?", (approval_id,)
            ).fetchone()
            approval_context = safe_json_loads(approval_row["context_json"], {}) if approval_row else {}
            if not approval_row or (
                compact_text(approval_row["canonical_lead_key"]) != canonical_key
                or compact_text(approval_row["pipeline_id"]) != pipeline_id
                or normalize_identity_text(approval_row["business_name"]) != normalize_identity_text(business_name)
                or compact_text(approval_context.get("campaignId")) != campaign_id
                or compact_text(approval_context.get("contactChannel")).lower() != requested_channel
            ):
                recovery_conflict("approval", "The approval ID is already owned by different local state.")

            campaign_row = db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
            if not campaign_row or compact_text(campaign_row["name"]) != campaign_name:
                recovery_conflict("campaign", "The campaign ID is already owned by a different campaign.")

            lead_row = db.execute(
                "SELECT * FROM lead_registry WHERE canonical_lead_key = ?", (canonical_key,)
            ).fetchone()
            lead_contact_conflict = (
                requested_channel == "email"
                and normalize_email_identity(lead_row["email"] if lead_row else None) != contact_email
            ) or (
                requested_channel == "phone"
                and normalize_phone_identity(lead_row["phone"] if lead_row else None)
                != normalize_phone_identity(contact_phone)
            )
            if not lead_row or (
                normalize_identity_text(lead_row["business_name"]) != normalize_identity_text(business_name)
                or lead_contact_conflict
            ):
                recovery_conflict("lead", "The canonical lead key is already owned by a different lead.")

            channel_row = db.execute(
                f"SELECT * FROM {channel_table} WHERE campaign_id = ? AND canonical_lead_key = ?",
                (campaign_id, canonical_key),
            ).fetchone()
            channel_contact_conflict = (
                requested_channel == "email"
                and normalize_email_identity(channel_row["email"] if channel_row else None) != contact_email
            ) or (
                requested_channel == "phone"
                and normalize_phone_identity(channel_row["phone"] if channel_row else None)
                != normalize_phone_identity(contact_phone)
            )
            if not channel_row or (
                compact_text(channel_row["approval_id"]) != approval_id
                or normalize_identity_text(channel_row["business_name"]) != normalize_identity_text(business_name)
                or compact_text(channel_row["ticket_id"]) != compact_text(ticket_id)
                or compact_text(channel_row["deployment_id"]) != deployment_id
                or channel_contact_conflict
            ):
                recovery_conflict("channel", "The campaign lead is already owned by different intake state.")

            deployment_row = db.execute(
                "SELECT * FROM campaign_deployments WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if not deployment_row or (
                compact_text(deployment_row["id"]) != deployment_id
                or compact_text(deployment_row["campaign_id"]) != campaign_id
                or compact_text(deployment_row["canonical_lead_key"]) != canonical_key
                or compact_text(deployment_row["channel"]).lower() != requested_channel
            ):
                recovery_conflict("deployment", "The approval deployment is already owned by different state.")

            link_row = db.execute(
                """
                SELECT * FROM zendesk_ticket_links
                WHERE approval_id = ? AND channel = ? AND stage = 'intake'
                """,
                (approval_id, requested_channel),
            ).fetchone()
            external_owner = db.execute(
                "SELECT * FROM zendesk_ticket_links WHERE external_id = ?", (external_id,)
            ).fetchone()
            if not link_row or not external_owner or link_row["id"] != external_owner["id"] or (
                compact_text(link_row["canonical_lead_key"]) != canonical_key
                or compact_text(link_row["pipeline_id"]) != pipeline_id
                or compact_text(link_row["external_id"]) != external_id
                or compact_text(link_row["ticket_id"]) != compact_text(ticket_id)
            ):
                recovery_conflict("ticket_link", "The Zendesk ticket identity is already owned by different local state.")

            if restore_current_state:
                restored_requested_at = (
                    compact_text(ticket.get("updated_at"), timestamp)
                    if restore_state["requested"]
                    else None
                )
                restored_completed_at = (
                    compact_text(ticket.get("updated_at"), timestamp)
                    if restore_state["deploymentStatus"] in {"DEPLOYED", "CANCELLED"}
                    else None
                )
                restored_zendesk = {
                    "ticketId": int(ticket_id),
                    "ticketUrl": f"https://{subdomain}.zendesk.com/agent/tickets/{ticket_id}",
                    "channel": requested_channel,
                    "recoveredFromManagedTicket": True,
                }
                db.execute(
                    """
                    UPDATE approval_records
                    SET status = ?, zendesk_json = ?, notes = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        restore_state["approvalStatus"],
                        json.dumps(restored_zendesk, default=str),
                        "Recovered from the current managed Zendesk ticket state.",
                        timestamp,
                        approval_id,
                    ),
                )
                db.execute(
                    """
                    UPDATE campaign_deployments
                    SET status = ?, ai_generation_count = MAX(ai_generation_count, ?),
                        repo_created = MAX(repo_created, ?), live_url = ?, requested_at = ?,
                        completed_at = ?, error = NULL, updated_at = ?
                    WHERE approval_id = ?
                    """,
                    (
                        restore_state["deploymentStatus"],
                        restore_state["aiGenerationCount"],
                        1 if restore_state["repoCreated"] else 0,
                        restore_state["liveUrl"],
                        restored_requested_at,
                        restored_completed_at,
                        timestamp,
                        approval_id,
                    ),
                )
                db.execute(
                    f"""
                    UPDATE {channel_table}
                    SET status = ?, deploy_requested = ?, updated_at = ?
                    WHERE approval_id = ?
                    """,
                    (
                        restore_state["deploymentStatus"],
                        1 if restore_state["requested"] else 0,
                        timestamp,
                        approval_id,
                    ),
                )
                link_payload.update(
                    {
                        "restoredState": restore_state,
                        "restoredAt": timestamp,
                    }
                )
                db.execute(
                    """
                    UPDATE zendesk_ticket_links
                    SET status = ?, tags_json = ?, payload_json = ?, updated_at = ?
                    WHERE approval_id = ? AND channel = ? AND stage = 'intake'
                    """,
                    (
                        ticket.get("status") or "open",
                        json.dumps(sorted(ticket_tags), default=str),
                        json.dumps(link_payload, default=str),
                        timestamp,
                        approval_id,
                        requested_channel,
                    ),
                )

            recovered_campaign_leads = db.execute(
                """
                SELECT COUNT(*) AS count FROM (
                    SELECT canonical_lead_key FROM campaign_email_leads WHERE campaign_id = ?
                    UNION
                    SELECT canonical_lead_key FROM campaign_call_leads WHERE campaign_id = ?
                )
                """,
                (campaign_id, campaign_id),
            ).fetchone()["count"]
            recovered_pipeline_leads = db.execute(
                "SELECT COUNT(DISTINCT canonical_lead_key) AS count FROM approval_records WHERE pipeline_id = ?",
                (pipeline_id,),
            ).fetchone()["count"]
            db.execute(
                """
                UPDATE campaigns
                SET discovered_count = MAX(discovered_count, ?),
                    requested_count = MAX(requested_count, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (recovered_campaign_leads, recovered_campaign_leads, timestamp, campaign_id),
            )
            db.execute(
                """
                UPDATE pipeline_runs
                SET lead_count = MAX(lead_count, ?), updated_at = ?
                WHERE pipeline_id = ?
                """,
                (recovered_pipeline_leads, timestamp, pipeline_id),
            )
            campaign_row = db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
            pipeline_row = db.execute("SELECT * FROM pipeline_runs WHERE pipeline_id = ?", (pipeline_id,)).fetchone()
            if not campaign_row or int(campaign_row["discovered_count"]) < int(recovered_campaign_leads):
                recovery_conflict("campaign", "Recovered campaign lead totals could not be persisted.")
            if not pipeline_row or int(pipeline_row["lead_count"]) < int(recovered_pipeline_leads):
                recovery_conflict("pipeline", "Recovered pipeline lead totals could not be persisted.")
            db.commit()
        except HTTPException:
            db.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ZENDESK_ORPHAN_RECOVERY_CONFLICT",
                    "entity": "database",
                    "message": "The managed Zendesk ticket conflicts with existing local state.",
                    "ticketId": ticket_id,
                },
            ) from exc
        except Exception:
            db.rollback()
            raise
    log_event(
        "warning",
        "zendesk.webhook.approval_recovered",
        "Recovered missing deferred campaign state from a fully managed Zendesk ticket.",
        approvalId=approval_id,
        ticketId=ticket_id,
        campaignId=campaign_id,
    )
    return get_approval_or_404(approval_id)


def resolve_webhook_approval(request: ZendeskWebhookRequest) -> sqlite3.Row:
    requested_approval_id = compact_text(request.approvalId)
    requested_canonical_key = compact_text(request.canonicalLeadKey)
    ticket_links: List[sqlite3.Row] = []
    if request.zendeskTicketId:
        with get_pipeline_db() as db:
            ticket_links = db.execute(
                "SELECT * FROM zendesk_ticket_links WHERE ticket_id = ? ORDER BY created_at DESC",
                (request.zendeskTicketId,),
            ).fetchall()
        linked_approval_ids = {compact_text(link["approval_id"]) for link in ticket_links}
        if len(linked_approval_ids) > 1:
            raise HTTPException(status_code=409, detail="Zendesk ticket is linked to conflicting approvals.")
        linked_approval_id = next(iter(linked_approval_ids), "")
        if requested_approval_id and linked_approval_id and requested_approval_id != linked_approval_id:
            raise HTTPException(status_code=409, detail="Zendesk webhook approval and ticket identities do not match.")

    row = get_approval_if_present(requested_approval_id)
    if not row and ticket_links:
        row = get_approval_if_present(ticket_links[0]["approval_id"])
    if request.zendeskTicketId and (not row or not ticket_links):
        row = recover_managed_zendesk_webhook_approval(request)
        ticket_links = []
        with get_pipeline_db() as db:
            ticket_links = db.execute(
                "SELECT * FROM zendesk_ticket_links WHERE ticket_id = ? AND approval_id = ?",
                (request.zendeskTicketId, row["id"]),
            ).fetchall()

    if not row and requested_canonical_key:
        with get_pipeline_db() as db:
            candidates = db.execute(
                """
                SELECT * FROM approval_records
                WHERE canonical_lead_key = ?
                  AND status IN (
                    'AWAITING_DEPLOYMENT', 'GENERATION_FAILED', 'EXPORT_FAILED',
                    'PENDING', 'DEPLOY_FAILED', 'APPROVED', 'DEPLOYED_ZENDESK_FAILED'
                  )
                ORDER BY created_at DESC
                """,
                (requested_canonical_key,),
            ).fetchall()
        if len(candidates) > 1:
            raise HTTPException(status_code=409, detail="Canonical lead key matches multiple webhook approvals.")
        row = candidates[0] if candidates else None

    if not row:
        raise HTTPException(status_code=404, detail="Could not resolve approval for Zendesk webhook.")
    if requested_approval_id and requested_approval_id != compact_text(row["id"]):
        raise HTTPException(status_code=409, detail="Zendesk webhook approval identity does not match local state.")
    if requested_canonical_key and requested_canonical_key != compact_text(row["canonical_lead_key"]):
        raise HTTPException(status_code=409, detail="Zendesk webhook canonical lead identity does not match local state.")

    context = safe_json_loads(row["context_json"], {})
    linked_channels = {compact_text(link["channel"]).lower() for link in ticket_links}
    context_channel = compact_text(context.get("contactChannel")).lower()
    expected_channel = (
        context_channel
        or (next(iter(linked_channels)) if len(linked_channels) == 1 else "")
        or contact_type_from_context(context)
    ).lower()
    requested_channel = compact_text(request.channel, expected_channel).lower()
    if requested_channel != expected_channel or (linked_channels and requested_channel not in linked_channels):
        raise HTTPException(status_code=409, detail="Zendesk webhook channel does not match the managed campaign ticket.")
    return row


DEPLOY_WEBHOOK_ACTIONS = {"deploy", "deploy_site", "approve_deploy", "deploy_requested"}
CANCEL_DEPLOYMENT_WEBHOOK_ACTIONS = {
    "cancel_deployment",
    "cancel_deploy",
    "undeploy_site",
    "deployment_cancelled",
}


def deploy_webhook_lease_seconds() -> int:
    try:
        configured = int(os.getenv("DEPLOY_WEBHOOK_LEASE_SECONDS", "3600"))
    except (TypeError, ValueError):
        configured = 3600
    return max(60, min(configured, 24 * 60 * 60))


def public_deploy_webhook_claim(claim: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": claim["disposition"],
        "attempt": claim.get("attemptCount", 0),
        "leaseExpiresAtEpoch": claim.get("leaseExpiresAt"),
    }


def acquire_deploy_webhook_claim(approval_id: str) -> Dict[str, Any]:
    """Atomically claim the expensive deploy path for one approval."""
    token = str(uuid4())
    now_epoch = time.time()
    lease_expires_at = now_epoch + deploy_webhook_lease_seconds()
    timestamp = now_iso()

    with get_pipeline_db() as db:
        db.execute("BEGIN IMMEDIATE")
        approval = db.execute(
            "SELECT * FROM approval_records WHERE id = ?", (approval_id,)
        ).fetchone()
        if not approval:
            raise HTTPException(status_code=404, detail="Approval record not found.")

        existing = db.execute(
            "SELECT * FROM deploy_webhook_claims WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        if existing and compact_text(existing["state"]).upper() == "COMPLETED":
            return {
                "disposition": "ALREADY_PROCESSED",
                "attemptCount": existing["attempt_count"] or 0,
                "leaseExpiresAt": None,
                "result": safe_json_loads(existing["result_json"], {}),
            }

        if (
            existing
            and compact_text(existing["state"]).upper() == "IN_PROGRESS"
            and float(existing["lease_expires_at"] or 0) > now_epoch
        ):
            return {
                "disposition": "IN_PROGRESS",
                "attemptCount": existing["attempt_count"] or 0,
                "leaseExpiresAt": existing["lease_expires_at"],
                "result": {},
            }

        if approval["status"] == "APPROVED" and approval["deployment_history_id"]:
            history = db.execute(
                "SELECT * FROM deployment_history WHERE id = ?",
                (approval["deployment_history_id"],),
            ).fetchone()
            completed_result = {
                "approvalId": approval_id,
                "action": "deploy_site",
                "deployment": deployment_from_history(history),
            }
            attempt_count = existing["attempt_count"] if existing else 0
            db.execute(
                """
                INSERT INTO deploy_webhook_claims (
                    approval_id, state, claim_token, lease_expires_at, attempt_count,
                    claimed_at, completed_at, last_error, result_json, updated_at
                ) VALUES (?, 'COMPLETED', NULL, NULL, ?, NULL, ?, NULL, ?, ?)
                ON CONFLICT(approval_id) DO UPDATE SET
                    state = 'COMPLETED', claim_token = NULL, lease_expires_at = NULL,
                    completed_at = excluded.completed_at, last_error = NULL,
                    result_json = excluded.result_json, updated_at = excluded.updated_at
                """,
                (
                    approval_id,
                    attempt_count,
                    timestamp,
                    json.dumps(completed_result, default=str),
                    timestamp,
                ),
            )
            return {
                "disposition": "ALREADY_PROCESSED",
                "attemptCount": attempt_count,
                "leaseExpiresAt": None,
                "result": completed_result,
            }

        attempt_count = (existing["attempt_count"] or 0) + 1 if existing else 1
        if existing:
            updated = db.execute(
                """
                UPDATE deploy_webhook_claims
                SET state = 'IN_PROGRESS', claim_token = ?, lease_expires_at = ?,
                    attempt_count = ?, claimed_at = ?, completed_at = NULL,
                    last_error = NULL, result_json = NULL, updated_at = ?
                WHERE approval_id = ?
                  AND (state != 'IN_PROGRESS' OR lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (
                    token,
                    lease_expires_at,
                    attempt_count,
                    timestamp,
                    timestamp,
                    approval_id,
                    now_epoch,
                ),
            )
            if updated.rowcount != 1:
                current = db.execute(
                    "SELECT * FROM deploy_webhook_claims WHERE approval_id = ?", (approval_id,)
                ).fetchone()
                return {
                    "disposition": "IN_PROGRESS",
                    "attemptCount": current["attempt_count"] or 0,
                    "leaseExpiresAt": current["lease_expires_at"],
                    "result": {},
                }
        else:
            db.execute(
                """
                INSERT INTO deploy_webhook_claims (
                    approval_id, state, claim_token, lease_expires_at, attempt_count,
                    claimed_at, completed_at, last_error, result_json, updated_at
                ) VALUES (?, 'IN_PROGRESS', ?, ?, 1, ?, NULL, NULL, NULL, ?)
                """,
                (approval_id, token, lease_expires_at, timestamp, timestamp),
            )

    return {
        "disposition": "ACQUIRED",
        "attemptCount": attempt_count,
        "leaseExpiresAt": lease_expires_at,
        "token": token,
        "result": {},
    }


def renew_deploy_webhook_claim(approval_id: str, claim_token: str) -> bool:
    timestamp = now_iso()
    with get_pipeline_db() as db:
        updated = db.execute(
            """
            UPDATE deploy_webhook_claims
            SET lease_expires_at = ?, updated_at = ?
            WHERE approval_id = ? AND state = 'IN_PROGRESS' AND claim_token = ?
            """,
            (
                time.time() + deploy_webhook_lease_seconds(),
                timestamp,
                approval_id,
                claim_token,
            ),
        )
    return updated.rowcount == 1


def complete_deploy_webhook_claim(
    approval_id: str,
    claim_token: str,
    result: Dict[str, Any],
) -> bool:
    timestamp = now_iso()
    with get_pipeline_db() as db:
        updated = db.execute(
            """
            UPDATE deploy_webhook_claims
            SET state = 'COMPLETED', claim_token = NULL, lease_expires_at = NULL,
                completed_at = ?, last_error = NULL, result_json = ?, updated_at = ?
            WHERE approval_id = ? AND state = 'IN_PROGRESS' AND claim_token = ?
            """,
            (
                timestamp,
                json.dumps(result, default=str),
                timestamp,
                approval_id,
                claim_token,
            ),
        )
    return updated.rowcount == 1


def fail_deploy_webhook_claim(approval_id: str, claim_token: str, error: Any) -> bool:
    timestamp = now_iso()
    with get_pipeline_db() as db:
        updated = db.execute(
            """
            UPDATE deploy_webhook_claims
            SET state = 'FAILED', claim_token = NULL, lease_expires_at = NULL,
                completed_at = NULL, last_error = ?, updated_at = ?
            WHERE approval_id = ? AND state = 'IN_PROGRESS' AND claim_token = ?
            """,
            (
                sanitize_message(error),
                timestamp,
                approval_id,
                claim_token,
            ),
        )
    return updated.rowcount == 1


@app.post("/api/zendesk/webhook")
def zendesk_webhook(request: ZendeskWebhookRequest, http_request: Request):
    expected_secret = require_env("ZENDESK_WEBHOOK_SECRET")
    provided_secret = (
        http_request.headers.get("x-ai-site-factory-secret")
        or http_request.headers.get("x-webhook-secret")
        or http_request.headers.get("x-zendesk-webhook-secret")
    )
    payload = request.model_dump()
    if provided_secret != expected_secret:
        record_zendesk_webhook_event(request.action, "REJECTED", payload, message="Invalid webhook secret.")
        raise HTTPException(status_code=401, detail="Invalid Zendesk webhook secret.")

    action = compact_text(request.action).lower().replace("-", "_")
    row = resolve_webhook_approval(request)
    approval_id = row["id"]
    context = safe_json_loads(row["context_json"], {})
    channel = compact_text(request.channel, contact_type_from_context(context)).lower()
    ticket_link = get_zendesk_ticket_link(approval_id, channel, "intake", request.zendeskTicketId)
    ticket_id = request.zendeskTicketId or (ticket_link or {}).get("ticketId")
    result: Dict[str, Any] = {"approvalId": approval_id, "action": action}
    started = now_iso()
    deploy_claim_token: Optional[str] = None
    deploy_claim_attempt = 0

    try:
        if action in DEPLOY_WEBHOOK_ACTIONS:
            if channel not in {"email", "phone"}:
                raise HTTPException(status_code=409, detail="Deploy webhook requires an email or phone campaign channel.")
            claim = acquire_deploy_webhook_claim(approval_id)
            deploy_claim_attempt = claim.get("attemptCount", 0)
            if claim["disposition"] == "IN_PROGRESS":
                result["claim"] = public_deploy_webhook_claim(claim)
                record_zendesk_webhook_event(action, "IN_PROGRESS", payload, result)
                return {"status": "IN_PROGRESS", "result": result}
            if claim["disposition"] == "ALREADY_PROCESSED":
                result.update(claim.get("result") or {})
                result["claim"] = public_deploy_webhook_claim(claim)
                record_zendesk_webhook_event(action, "COMPLETED", payload, result)
                return {"status": "ALREADY_PROCESSED", "result": result}

            deploy_claim_token = claim["token"]
            result["claim"] = public_deploy_webhook_claim(claim)
            if context.get("intakeDeferred"):
                safe_update_zendesk_deployment_lifecycle(
                    row,
                    ticket_id,
                    "DEPLOY_REQUESTED",
                    "AI Site Factory deployment was requested. Site generation is starting now.",
                )
            update_campaign_workflow(approval_id, "DEPLOY_REQUESTED", requested=True)
            if not renew_deploy_webhook_claim(approval_id, deploy_claim_token):
                raise HTTPException(status_code=409, detail="Deployment claim was lost before processing started.")
            reused = reuse_existing_live_deployment(
                row,
                compact_text(request.actor, "Zendesk Webhook"),
                request.notes or "Deployment requested from Zendesk.",
                ticket_id,
            )
            if reused:
                result.update(reused)
            else:
                if row["status"] in {"AWAITING_DEPLOYMENT", "GENERATION_FAILED", "EXPORT_FAILED"}:
                    row = prepare_deferred_approval(approval_id)
                    if not renew_deploy_webhook_claim(approval_id, deploy_claim_token):
                        raise HTTPException(status_code=409, detail="Deployment claim was lost after site generation.")
                if row["status"] in {"PENDING", "DEPLOY_FAILED"}:
                    if context.get("intakeDeferred"):
                        safe_update_zendesk_deployment_lifecycle(
                            row,
                            ticket_id,
                            "ARTIFACT_READY",
                            "AI Site Factory generated the HTML and stored the artifact in GitHub. Netlify deployment is starting.",
                        )
                    update_campaign_workflow(approval_id, "DEPLOYING", requested=True)
                    if context.get("intakeDeferred"):
                        safe_update_zendesk_deployment_lifecycle(
                            row,
                            ticket_id,
                            "DEPLOYING",
                            "The GitHub artifact is ready and Netlify is deploying the site.",
                        )
                    if not renew_deploy_webhook_claim(approval_id, deploy_claim_token):
                        raise HTTPException(status_code=409, detail="Deployment claim was lost before Netlify deployment.")
                    deploy_response = approve_generated_site(
                        approval_id,
                        ApprovalActionRequest(
                            approvedBy=compact_text(request.actor, "Zendesk Webhook"),
                            notes=request.notes or "Deployment requested from Zendesk webhook.",
                            zendeskTicketId=ticket_id,
                        ),
                    )
                    result["deployment"] = deploy_response.model_dump()
                    if deploy_response.status == "DEPLOYED_ZENDESK_FAILED":
                        raise HTTPException(
                            status_code=502,
                            detail={
                                "code": "ZENDESK_LIVE_URL_UPDATE_FAILED",
                                "message": "The site deployed, but the managed Zendesk ticket did not accept the live URL update.",
                                "ticketId": ticket_id,
                            },
                        )
                else:
                    result["deployment"] = approval_row_to_dict(row)
            if ticket_link:
                latest_ticket_link = (
                    get_zendesk_ticket_link(approval_id, channel, "intake", ticket_id)
                    or ticket_link
                )
                payload_update = {
                    **latest_ticket_link.get("payload", {}),
                    "deployRequested": True,
                    "deployWebhookAt": now_iso(),
                }
                save_zendesk_ticket_link(
                    approval_id,
                    row["canonical_lead_key"],
                    row["pipeline_id"],
                    latest_ticket_link["channel"],
                    latest_ticket_link["stage"],
                    latest_ticket_link.get("ticketId"),
                    latest_ticket_link.get("ticketUrl"),
                    latest_ticket_link.get("status") or "deploy_requested",
                    latest_ticket_link.get("tags", []),
                    payload_update,
                )
            completed_result = {
                **result,
                "claim": {
                    "status": "COMPLETED",
                    "attempt": deploy_claim_attempt,
                    "leaseExpiresAtEpoch": None,
                },
            }
            if not complete_deploy_webhook_claim(approval_id, deploy_claim_token, completed_result):
                raise HTTPException(status_code=409, detail="Deployment claim could not be completed by its owner.")
            result = completed_result
        elif action in CANCEL_DEPLOYMENT_WEBHOOK_ACTIONS:
            if channel not in {"email", "phone"}:
                raise HTTPException(status_code=409, detail="Cancellation webhook requires an email or phone campaign channel.")
            scheduled = False
            if ticket_id:
                live_ticket = zendesk_api_request("get", f"/tickets/{int(ticket_id)}.json").get("ticket") or {}
                scheduled = "asf_10_day_cancellation_due" in {
                    compact_text(tag) for tag in (live_ticket.get("tags") or [])
                }
            result["cancellation"] = cancel_approval_deployment(
                row,
                ticket_id,
                channel,
                scheduled=scheduled,
            )
        elif action in {"send_email", "email_send", "send_approved_email", "email_send_requested"}:
            if channel != "email":
                raise HTTPException(status_code=409, detail="Email send webhook can only run for email-channel Zendesk tickets.")
            if not ticket_id:
                raise HTTPException(status_code=409, detail="No Zendesk ticket was found for email send.")
            deployment = deployment_from_history(get_deployment_history_row(row["deployment_history_id"]))
            site_url = (deployment or {}).get("url") or ""
            outreach = safe_json_loads(row["outreach_json"], None) or generate_outreach_with_groq(context, site_url)
            body = f"{outreach.get('body')}\n\n{request.notes or ''}".strip()
            custom_fields = zendesk_custom_fields({"emailSendRequested": True, "leadStatus": "EMAIL_SENT", "liveUrl": site_url})
            tags = update_zendesk_ticket_tags(
                int(ticket_id),
                add=["ai_site_factory", "ai_site_email_sent", "ai_site_email_lead", "asf_email_sent"],
            )
            extra_fields: Dict[str, Any] = {"status": "open"}
            if custom_fields:
                extra_fields["custom_fields"] = custom_fields
            ticket = update_zendesk_ticket_comment(int(ticket_id), body, public=True, extra_ticket_fields=extra_fields)
            with get_pipeline_db() as db:
                db.execute(
                    "UPDATE approval_records SET outreach_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(outreach, default=str), now_iso(), approval_id),
                )
            if ticket_link:
                payload_update = {**ticket_link.get("payload", {}), "emailSendRequested": True, "emailSentAt": now_iso()}
                save_zendesk_ticket_link(
                    approval_id,
                    row["canonical_lead_key"],
                    row["pipeline_id"],
                    ticket_link["channel"],
                    ticket_link["stage"],
                    ticket_id,
                    ticket_link.get("ticketUrl"),
                    ticket.get("status") or "open",
                    tags,
                    payload_update,
                )
            result["email"] = {"ticketId": ticket_id, "status": ticket.get("status") or "open"}
        elif action in {"phone_status", "update_phone_status", "call_status", "phone_call_status"}:
            if channel != "phone":
                raise HTTPException(status_code=409, detail="Phone status webhook can only run for phone-channel Zendesk tickets.")
            status_value = compact_text(request.value, request.notes or "UPDATED")
            if ticket_id:
                custom_fields = zendesk_custom_fields({"phoneCallStatus": status_value, "leadStatus": "PHONE_UPDATED"})
                extra_fields = {"status": "open"}
                if custom_fields:
                    extra_fields["custom_fields"] = custom_fields
                update_zendesk_ticket_comment(
                    int(ticket_id),
                    f"Phone/dialer status updated from AI Site Factory webhook: {status_value}",
                    public=False,
                    extra_ticket_fields=extra_fields,
                )
            if ticket_link:
                payload_update = {**ticket_link.get("payload", {}), "phoneCallStatus": status_value, "phoneStatusUpdatedAt": now_iso()}
                save_zendesk_ticket_link(
                    approval_id,
                    row["canonical_lead_key"],
                    row["pipeline_id"],
                    ticket_link["channel"],
                    ticket_link["stage"],
                    ticket_id,
                    ticket_link.get("ticketUrl"),
                    "phone_updated",
                    list(set(ticket_link.get("tags", []) + ["ai_site_phone_updated", "asf_phone_updated"])),
                    payload_update,
                )
            result["phone"] = {"ticketId": ticket_id, "status": status_value}
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported Zendesk webhook action: {request.action}")

        record_pipeline_step(
            pipeline_id=row["pipeline_id"],
            canonical_key=row["canonical_lead_key"],
            step=f"zendesk_webhook_{action}",
            status="COMPLETED",
            provider="zendesk",
            message=f"Zendesk webhook action {action} completed.",
            started_at=started,
            finished_at=now_iso(),
            retryable=False,
            details={"approvalId": approval_id, "ticketId": ticket_id, "channel": channel},
        )
        record_zendesk_webhook_event(action, "COMPLETED", payload, result)
        return {"status": "COMPLETED", "result": result}
    except HTTPException as error:
        if deploy_claim_token:
            fail_deploy_webhook_claim(approval_id, deploy_claim_token, error.detail)
        if action in DEPLOY_WEBHOOK_ACTIONS and ticket_id and context.get("intakeDeferred"):
            try:
                failure_status = "GENERATION_FAILED" if "generation" in compact_text(error.detail).lower() else "DEPLOY_FAILED"
                update_zendesk_deployment_lifecycle(
                    row, ticket_id, failure_status,
                    f"AI Site Factory could not complete this deployment: {sanitize_message(error.detail)}",
                )
            except Exception as callback_error:
                log_event("error", "zendesk.lifecycle.failure_callback", str(callback_error), approvalId=approval_id)
        record_zendesk_webhook_event(action, "FAILED", payload, message=str(error.detail))
        raise
    except Exception as error:
        if deploy_claim_token:
            fail_deploy_webhook_claim(approval_id, deploy_claim_token, error)
        if action in DEPLOY_WEBHOOK_ACTIONS and ticket_id and context.get("intakeDeferred"):
            try:
                update_zendesk_deployment_lifecycle(
                    row, ticket_id, "DEPLOY_FAILED",
                    f"AI Site Factory could not complete this deployment: {sanitize_message(error)}",
                )
            except Exception as callback_error:
                log_event("error", "zendesk.lifecycle.failure_callback", str(callback_error), approvalId=approval_id)
        record_pipeline_step(
            pipeline_id=row["pipeline_id"],
            canonical_key=row["canonical_lead_key"],
            step=f"zendesk_webhook_{action}",
            status="FAILED",
            provider="zendesk",
            message=str(error),
            started_at=started,
            finished_at=now_iso(),
            retryable=True,
            details={"approvalId": approval_id, "ticketId": ticket_id, "channel": channel},
        )
        record_zendesk_webhook_event(action, "FAILED", payload, message=str(error))
        raise HTTPException(status_code=500, detail=f"Zendesk webhook failed: {sanitize_message(error)}")


@app.post("/api/approvals/{approval_id}/retry-export", response_model=ApprovalActionResponse)
def retry_github_export(approval_id: str, request: ApprovalActionRequest):
    row = get_approval_or_404(approval_id)
    if row["status"] not in {"EXPORT_FAILED", "PENDING"}:
        raise HTTPException(status_code=409, detail=f"Approval is {row['status']}; GitHub export retry is only available for generated approvals.")
    if not row["html"]:
        raise HTTPException(status_code=409, detail="Approval record no longer contains generated HTML.")

    started = now_iso()
    try:
        github_export = export_site_to_github(
            canonical_key=row["canonical_lead_key"],
            business_name=row["business_name"],
            site_html=row["html"],
            pipeline_id=row["pipeline_id"],
            approval_id=approval_id,
        )
    except Exception as error:
        structured = [structured_pipeline_error("github_export", error, provider="github", retryable=True)]
        record_pipeline_step(
            pipeline_id=row["pipeline_id"],
            canonical_key=row["canonical_lead_key"],
            step="github_export",
            status="FAILED",
            provider="github",
            message=str(error),
            started_at=started,
            finished_at=now_iso(),
            retryable=True,
            details={"approvalId": approval_id, "errorType": error.__class__.__name__},
        )
        with get_pipeline_db() as db:
            db.execute(
                """
                UPDATE approval_records
                SET status = ?, updated_at = ?, errors_json = ?
                WHERE id = ?
                """,
                ("EXPORT_FAILED", now_iso(), json.dumps(structured, default=str), approval_id),
            )
        refresh_pipeline_run_status_from_approvals(row["pipeline_id"])
        raise HTTPException(status_code=502, detail=f"GitHub export failed: {sanitize_message(error)}")

    record_pipeline_step(
        pipeline_id=row["pipeline_id"],
        canonical_key=row["canonical_lead_key"],
        step="github_export",
        status="COMPLETED",
        provider="github",
        message="github_export completed.",
        started_at=started,
        finished_at=now_iso(),
        retryable=False,
        details={"approvalId": approval_id},
    )
    with get_pipeline_db() as db:
        db.execute(
            """
            UPDATE approval_records
            SET status = ?, updated_at = ?, publish_mode = ?, github_export_json = ?, errors_json = ?
            WHERE id = ?
            """,
            ("PENDING", now_iso(), "github-netlify", json.dumps(github_export, default=str), json.dumps([], default=str), approval_id),
        )
    refresh_pipeline_run_status_from_approvals(row["pipeline_id"])
    return ApprovalActionResponse(
        approvalId=approval_id,
        status="PENDING",
        leadKey=row["lead_key"],
        canonicalLeadKey=row["canonical_lead_key"],
        businessName=row["business_name"],
        publishMode="github-netlify",
        githubExport=github_export,
        errors=[],
    )


@app.get("/api/pipeline/runs")
def list_pipeline_runs(limit: int = 30):
    safe_limit = max(1, min(limit, 100))
    with get_pipeline_db() as db:
        rows = db.execute(
            """
            SELECT *
            FROM pipeline_runs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return {
        "runs": [
            {
                **dict(row),
                "warnings": safe_json_loads(row["warnings_json"], []),
            }
            for row in rows
        ],
        "count": len(rows),
    }


@app.get("/api/pipeline/runs/{pipeline_id}")
def get_pipeline_run_detail(pipeline_id: str):
    with get_pipeline_db() as db:
        run = db.execute(
            "SELECT * FROM pipeline_runs WHERE pipeline_id = ?",
            (pipeline_id,),
        ).fetchone()
        if not run:
            raise HTTPException(status_code=404, detail="Pipeline run not found.")

        steps = db.execute(
            """
            SELECT *
            FROM pipeline_steps
            WHERE pipeline_id = ?
            ORDER BY started_at ASC
            """,
            (pipeline_id,),
        ).fetchall()
        approvals = db.execute(
            """
            SELECT *
            FROM approval_records
            WHERE pipeline_id = ?
            ORDER BY created_at DESC
            """,
            (pipeline_id,),
        ).fetchall()

    return {
        "run": {**dict(run), "warnings": safe_json_loads(run["warnings_json"], [])},
        "steps": [
            {
                **dict(step),
                "details": safe_json_loads(step["details_json"], {}),
            }
            for step in steps
        ],
        "approvals": [approval_row_to_dict(approval) for approval in approvals],
    }


@app.post("/api/approvals/{approval_id}/approve", response_model=ApprovalActionResponse)
def approve_generated_site(approval_id: str, request: ApprovalActionRequest):
    row = get_approval_or_404(approval_id)
    if row["status"] not in {"PENDING", "DEPLOY_FAILED"}:
        raise HTTPException(
            status_code=409,
            detail=f"Approval is {row['status']}; only PENDING or DEPLOY_FAILED approvals can deploy.",
        )

    context = safe_json_loads(row["context_json"], {})
    for key in ["ownerName", "ownerEmail", "ownerStatus"]:
        context.pop(key, None)
    site_html = ensure_required_site_features(row["html"], context) if row["html"] else None
    approved_by = compact_text(request.approvedBy, "Dashboard Operator")
    errors: List[Dict[str, Any]] = []
    deployment: Optional[Dict[str, Any]] = None
    deployment_history: Optional[Dict[str, Any]] = None
    outreach: Optional[Dict[str, Any]] = None
    zendesk: Optional[Dict[str, Any]] = None
    github_export: Optional[Dict[str, Any]] = safe_json_loads(row["github_export_json"], None)
    publish_mode = normalize_publish_mode(request.publishMode)
    effective_publish_mode = publish_mode
    status = "APPROVED"

    if not site_html:
        raise HTTPException(status_code=409, detail="Approval record does not contain generated HTML.")
    if not github_export or not github_export.get("repository") or not github_export.get("commitSha"):
        raise HTTPException(
            status_code=409,
            detail=(
                "Approval does not have a successful GitHub export with repository and commit SHA. "
                "Retry Export before approving so Netlify can build from the generated GitHub repo."
            ),
        )

    def run_approval_step(step: str, provider: str, callback, retryable: bool = True):
        started = now_iso()
        try:
            result = callback()
        except Exception as step_error:
            record_pipeline_step(
                pipeline_id=row["pipeline_id"],
                canonical_key=row["canonical_lead_key"],
                step=step,
                status="FAILED",
                provider=provider,
                message=str(step_error),
                started_at=started,
                finished_at=now_iso(),
                retryable=retryable,
                details={"approvalId": approval_id, "errorType": step_error.__class__.__name__},
            )
            raise

        record_pipeline_step(
            pipeline_id=row["pipeline_id"],
            canonical_key=row["canonical_lead_key"],
            step=step,
            status="COMPLETED",
            provider=provider,
            message=f"{step} completed.",
            started_at=started,
            finished_at=now_iso(),
            retryable=False,
            details={"approvalId": approval_id},
        )
        return result

    try:
        if publish_mode == "direct-netlify":
            deployment = run_approval_step(
                "netlify_direct_deploy",
                "netlify",
                lambda: deploy_direct_netlify_for_lead(
                    canonical_key=row["canonical_lead_key"],
                    business_name=row["business_name"],
                    site_html=site_html,
                    pipeline_id=row["pipeline_id"],
                    approval_id=approval_id,
                    approved_by=approved_by,
                    github_export=github_export,
                ),
            )
        elif publish_mode == "direct-netlify-fallback":
            deployment = run_approval_step(
                "netlify_direct_fallback_deploy",
                "netlify",
                lambda: deploy_direct_netlify_fallback_for_lead(
                    canonical_key=row["canonical_lead_key"],
                    business_name=row["business_name"],
                    site_html=site_html,
                    pipeline_id=row["pipeline_id"],
                    approval_id=approval_id,
                    approved_by=approved_by,
                    github_export=github_export,
                    git_error=RuntimeError("Direct Netlify fallback explicitly requested by operator."),
                ),
            )
        else:
            try:
                deployment = run_approval_step(
                    "netlify_git_deploy",
                    "netlify",
                    lambda: deploy_github_repo_to_netlify_for_lead(
                        canonical_key=row["canonical_lead_key"],
                        business_name=row["business_name"],
                        pipeline_id=row["pipeline_id"],
                        approval_id=approval_id,
                        approved_by=approved_by,
                        github_export=github_export,
                        regenerate_existing_site=request.regenerateExistingSite,
                    ),
                )
            except Exception as git_error:
                errors.append(structured_pipeline_error("netlify_git_deploy", git_error, provider="netlify", retryable=True))
                deployment = run_approval_step(
                    "netlify_direct_fallback_deploy",
                    "netlify",
                    lambda: deploy_direct_netlify_fallback_for_lead(
                        canonical_key=row["canonical_lead_key"],
                        business_name=row["business_name"],
                        site_html=site_html,
                        pipeline_id=row["pipeline_id"],
                        approval_id=approval_id,
                        approved_by=approved_by,
                        github_export=github_export,
                        git_error=git_error,
                    ),
                )
        effective_publish_mode = deployment.get("publishMode", publish_mode)
        deployment_history = deployment_history_row_to_dict(get_deployment_history_row(deployment.get("deploymentHistoryId")))
    except Exception as error:
        status = "DEPLOY_FAILED"
        if not errors or errors[-1].get("message") != sanitize_message(error):
            errors.append(structured_pipeline_error("netlify_deploy", error, provider="netlify", retryable=True))
        with get_pipeline_db() as db:
            db.execute(
                """
                UPDATE approval_records
                SET status = ?, updated_at = ?, approved_by = ?, notes = ?,
                    publish_mode = ?, github_export_json = ?, errors_json = ?
                WHERE id = ?
                """,
                (
                    status,
                    now_iso(),
                    approved_by,
                    request.notes,
                    effective_publish_mode,
                    json.dumps(github_export, default=str) if github_export else None,
                    json.dumps(errors, default=str),
                    approval_id,
                ),
            )
        refresh_pipeline_run_status_from_approvals(row["pipeline_id"])
        update_campaign_workflow(approval_id, "DEPLOY_FAILED", requested=True, error=str(error))
        raise HTTPException(status_code=502, detail=f"Netlify deployment failed: {sanitize_message(error)}")

    try:
        if context.get("intakeDeferred"):
            outreach = run_approval_step(
                "campaign_outreach_template",
                "local",
                lambda: campaign_outreach_template(context, deployment.get("url", "")),
                retryable=False,
            )
            zendesk = run_approval_step(
                "zendesk_intake_update",
                "zendesk",
                lambda: update_existing_intake_ticket(
                    row, deployment, outreach or {}, request.zendeskTicketId
                ),
            )
        else:
            outreach = run_approval_step(
                "groq_outreach",
                "groq",
                lambda: generate_outreach_with_groq(context, deployment.get("url", "")),
            )
            zendesk = run_approval_step(
                "zendesk_ticket",
                "zendesk",
                lambda: create_zendesk_outreach_ticket(
                    context,
                    deployment,
                    outreach,
                    row["pipeline_id"],
                ),
            )
    except Exception as error:
        status = "DEPLOYED_ZENDESK_FAILED"
        errors.append(structured_pipeline_error("zendesk_ticket", error, provider="zendesk", retryable=True))
        log_event("error", "approval.zendesk.failed", "Zendesk failed after approved deployment.", approvalId=approval_id, reason=str(error))

    clear_pending_html = bool(
        deployment
        and deployment.get("state") == "ready"
        and github_export
        and deployment.get("deploymentHistoryId")
    )

    with get_pipeline_db() as db:
        db.execute(
            """
            UPDATE approval_records
            SET status = ?, updated_at = ?, approved_by = ?, notes = ?, html = ?,
                deployment_history_id = ?, outreach_json = ?, zendesk_json = ?,
                publish_mode = ?, github_export_json = ?, errors_json = ?
            WHERE id = ?
            """,
            (
                status,
                now_iso(),
                approved_by,
                request.notes,
                None if clear_pending_html else site_html,
                deployment.get("deploymentHistoryId") if deployment else None,
                json.dumps(outreach, default=str) if outreach else None,
                json.dumps(zendesk, default=str) if zendesk else None,
                effective_publish_mode,
                json.dumps(github_export, default=str) if github_export else None,
                json.dumps(errors, default=str),
                approval_id,
            ),
        )

    refresh_pipeline_run_status_from_approvals(row["pipeline_id"])
    if deployment:
        update_campaign_workflow(
            approval_id,
            "DEPLOYED" if compact_text(deployment.get("state")).lower() == "ready" else status,
            requested=True,
            repo_url=deployment.get("githubRepoUrl"),
            live_url=deployment.get("url"),
            deployment_history_id=deployment.get("deploymentHistoryId"),
        )
    log_event(
        "info",
        "approval.approve.finish",
        "Approval processed.",
        approvalId=approval_id,
        status=status,
        url=deployment.get("url") if deployment else None,
        zendeskTicketId=zendesk.get("ticketId") if zendesk else None,
    )

    return ApprovalActionResponse(
        approvalId=approval_id,
        status=status,
        leadKey=row["lead_key"],
        canonicalLeadKey=row["canonical_lead_key"],
        businessName=row["business_name"],
        deployment=deployment,
        deploymentHistory=deployment_history,
        zendesk=zendesk,
        outreachDraft=outreach,
        publishMode=effective_publish_mode,
        githubExport=github_export,
        errors=errors,
    )


@app.post("/api/approvals/{approval_id}/reject", response_model=ApprovalActionResponse)
def reject_generated_site(approval_id: str, request: ApprovalActionRequest):
    row = get_approval_or_404(approval_id)
    if row["status"] not in {"PENDING", "EXPORT_FAILED", "DEPLOY_FAILED"}:
        raise HTTPException(
            status_code=409,
            detail=f"Approval is {row['status']} and cannot be rejected.",
        )

    rejected_by = compact_text(request.rejectedBy, "Dashboard Operator")
    notes = compact_text(request.reason or request.notes, "Rejected from dashboard.")
    with get_pipeline_db() as db:
        db.execute(
            """
            UPDATE approval_records
            SET status = 'REJECTED', updated_at = ?, rejected_by = ?, notes = ?
            WHERE id = ?
            """,
            (now_iso(), rejected_by, notes, approval_id),
        )

    refresh_pipeline_run_status_from_approvals(row["pipeline_id"])
    log_event("info", "approval.reject.finish", "Approval rejected.", approvalId=approval_id, rejectedBy=rejected_by)
    return ApprovalActionResponse(
        approvalId=approval_id,
        status="REJECTED",
        leadKey=row["lead_key"],
        canonicalLeadKey=row["canonical_lead_key"],
        businessName=row["business_name"],
    )


@app.post("/api/approvals/{approval_id}/regenerate", response_model=ApprovalActionResponse)
def regenerate_generated_site(approval_id: str, request: ApprovalActionRequest):
    row = get_approval_or_404(approval_id)
    context = safe_json_loads(row["context_json"], {})
    template = dict(FREEFORM_SITE_SPEC)
    requested_by = compact_text(request.requestedBy, "Dashboard Operator")
    pipeline_id = str(uuid4())
    created_at = now_iso()
    step_errors: List[Dict[str, Any]] = []

    save_pipeline_run(
        pipeline_id=pipeline_id,
        status="PROCESSING",
        template_id=FREEFORM_TEMPLATE_ID,
        source_batch_id=None,
        lead_count=1,
        completed_count=0,
        pending_count=0,
        failed_count=0,
        warnings=[],
        created_at=created_at,
    )

    def run_regenerate_step(step: str, provider: str, callback):
        started = now_iso()
        try:
            result = callback()
        except Exception as step_error:
            record_pipeline_step(
                pipeline_id=pipeline_id,
                canonical_key=row["canonical_lead_key"],
                step=step,
                status="FAILED",
                provider=provider,
                message=str(step_error),
                started_at=started,
                finished_at=now_iso(),
                retryable=True,
                details={"sourceApprovalId": approval_id, "errorType": step_error.__class__.__name__},
            )
            raise

        record_pipeline_step(
            pipeline_id=pipeline_id,
            canonical_key=row["canonical_lead_key"],
            step=step,
            status="COMPLETED",
            provider=provider,
            message=f"{step} completed.",
            started_at=started,
            finished_at=now_iso(),
            retryable=False,
            details={"sourceApprovalId": approval_id},
        )
        return result

    try:
        groq_brief = run_regenerate_step(
            "groq_compact_lead",
            "groq",
            lambda: compact_lead_with_groq(context),
        )
        final_html_result = run_regenerate_step(
            "gemini_final_html",
            "gemini",
            lambda: generate_final_html_with_gemini(groq_brief),
        )
        quality_result = run_regenerate_step(
            "seo_validation",
            "local",
            lambda: prepare_generated_site_artifact(final_html_result["html"], groq_brief),
        )
        final_html = quality_result["html"]
        seo_validation = quality_result["seoValidation"]
        site_content = {
            "promptHeader": LANDING_PAGE_PROMPT_HEADER,
            "groqBrief": groq_brief,
            "geminiQaNotes": final_html_result.get("qaNotes"),
            "structureNotes": final_html_result.get("structureNotes"),
            "stylingLibraries": final_html_result.get("stylingLibraries"),
            "siteProfile": HIGHLY_INTERACTIVE_SITE_PROFILE,
            "seoValidation": seo_validation,
            "finalHtmlChecksum": html_checksum(final_html),
            "regeneratedFromApprovalId": approval_id,
            "requestedBy": requested_by,
        }
        new_approval_id = create_approval_record(
            pipeline_id=pipeline_id,
            canonical_key=row["canonical_lead_key"],
            lead_key=row["lead_key"],
            business_name=row["business_name"],
            site_html=final_html,
            context=context,
            site_content=site_content,
            template=template,
            status="EXPORTING",
        )
        try:
            github_export = run_regenerate_step(
                "github_export",
                "github",
                lambda: export_site_to_github(
                    canonical_key=row["canonical_lead_key"],
                    business_name=row["business_name"],
                    site_html=final_html,
                    pipeline_id=pipeline_id,
                    approval_id=new_approval_id,
                ),
            )
        except Exception as export_error:
            step_errors.append(structured_pipeline_error("github_export", export_error, provider="github", retryable=True))
            with get_pipeline_db() as db:
                db.execute(
                    """
                    UPDATE approval_records
                    SET status = ?, updated_at = ?, errors_json = ?
                    WHERE id = ?
                    """,
                    ("EXPORT_FAILED", now_iso(), json.dumps(step_errors, default=str), new_approval_id),
                )
            save_pipeline_run(
                pipeline_id=pipeline_id,
                status="FAILED",
                template_id=FREEFORM_TEMPLATE_ID,
                source_batch_id=None,
                lead_count=1,
                completed_count=0,
                pending_count=0,
                failed_count=1,
                warnings=[],
                created_at=created_at,
            )
            refresh_pipeline_run_status_from_approvals(pipeline_id)
            return ApprovalActionResponse(
                approvalId=new_approval_id,
                status="EXPORT_FAILED",
                leadKey=row["lead_key"],
                canonicalLeadKey=row["canonical_lead_key"],
                businessName=row["business_name"],
                publishMode="github-netlify",
                errors=step_errors,
            )

        with get_pipeline_db() as db:
            db.execute(
                """
                UPDATE approval_records
                SET status = 'SUPERSEDED', updated_at = ?, notes = ?
                WHERE id = ? AND status IN ('PENDING', 'EXPORT_FAILED')
                """,
                (now_iso(), f"Regenerated by {requested_by}. New approval: {new_approval_id}", approval_id),
            )
            db.execute(
                """
                UPDATE approval_records
                SET status = ?, updated_at = ?, publish_mode = ?, github_export_json = ?
                WHERE id = ?
                """,
                ("PENDING", now_iso(), "github-netlify", json.dumps(github_export, default=str), new_approval_id),
            )
        refresh_pipeline_run_status_from_approvals(row["pipeline_id"])
        save_pipeline_run(
            pipeline_id=pipeline_id,
            status="PENDING_APPROVAL",
            template_id=FREEFORM_TEMPLATE_ID,
            source_batch_id=None,
            lead_count=1,
            completed_count=0,
            pending_count=1,
            failed_count=0,
            warnings=[],
            created_at=created_at,
        )
        refresh_pipeline_run_status_from_approvals(pipeline_id)
    except Exception as error:
        step_errors.append(structured_pipeline_error("regenerate_html", error, provider=None, retryable=True))
        save_pipeline_run(
            pipeline_id=pipeline_id,
            status="FAILED",
            template_id=FREEFORM_TEMPLATE_ID,
            source_batch_id=None,
            lead_count=1,
            completed_count=0,
            pending_count=0,
            failed_count=1,
            warnings=[],
            created_at=created_at,
        )
        raise HTTPException(status_code=502, detail=f"Regeneration failed: {sanitize_message(error)}")

    log_event("info", "approval.regenerate.finish", "Approval regenerated.", approvalId=approval_id, newApprovalId=new_approval_id)
    return ApprovalActionResponse(
        approvalId=new_approval_id,
        status="PENDING",
        leadKey=row["lead_key"],
        canonicalLeadKey=row["canonical_lead_key"],
        businessName=row["business_name"],
        publishMode="github-netlify",
        githubExport=github_export,
        errors=step_errors,
    )


@app.post("/api/deployments/refresh-business-media")
def refresh_deployed_business_media(
    request: DeploymentMediaRefreshRequest,
    http_request: Request,
):
    expected_secret = require_env("ZENDESK_WEBHOOK_SECRET")
    provided_secret = (
        http_request.headers.get("x-ai-site-factory-secret")
        or http_request.headers.get("x-webhook-secret")
        or http_request.headers.get("x-zendesk-webhook-secret")
    )
    if provided_secret != expected_secret:
        raise HTTPException(status_code=401, detail="Invalid maintenance webhook secret.")

    main_image_url = normalize_url(request.mainImageUrl)
    if not main_image_url:
        raise HTTPException(status_code=400, detail="A valid public mainImageUrl is required.")
    try:
        image_probe = requests.get(
            main_image_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"
                )
            },
            timeout=30,
            stream=True,
        )
        image_probe.raise_for_status()
        content_type = compact_text(image_probe.headers.get("Content-Type")).lower()
        image_probe.close()
    except requests.RequestException as error:
        raise HTTPException(status_code=400, detail=f"The business image could not be reached: {sanitize_message(error)}") from error
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="The mainImageUrl did not return an image content type.")

    repo_full_name = compact_text(request.githubRepoFullName)
    github_owner = require_env("GITHUB_OWNER")
    if "/" not in repo_full_name or repo_full_name.split("/", 1)[0].casefold() != github_owner.casefold():
        raise HTTPException(status_code=409, detail="The GitHub repository is outside the configured AI Site Factory owner.")

    ticket_id = int(request.zendeskTicketId)
    ticket = zendesk_api_request("get", f"/tickets/{ticket_id}.json").get("ticket") or {}
    tags = {compact_text(tag) for tag in (ticket.get("tags") or []) if compact_text(tag)}
    if "asf_deployed" not in tags:
        raise HTTPException(status_code=409, detail="Only an actively deployed managed ticket can refresh its business image.")
    channel = "email" if "asf_channel_email" in tags else "phone" if "asf_channel_phone" in tags else ""
    if not channel:
        raise HTTPException(status_code=409, detail="The deployed ticket has no managed email or phone channel tag.")

    contract = require_zendesk_ticket_contract(channel)
    field_values = {
        compact_text(item.get("id")): item.get("value")
        for item in (ticket.get("custom_fields") or [])
    }

    def managed_value(key: str) -> Any:
        return field_values.get(compact_text(contract["fieldIds"].get(key)))

    approval_id = compact_text(managed_value("approvalId"))
    canonical_key = compact_text(managed_value("canonicalLeadKey"))
    business_name = compact_text(managed_value("businessName"))
    live_url = normalize_url(managed_value("liveUrl"))
    if not all((approval_id, canonical_key, business_name, live_url)):
        raise HTTPException(status_code=409, detail="The deployed ticket is missing its managed deployment identity.")
    if not zendesk_ticket_field_value_matches(True, managed_value("deployRequested")):
        raise HTTPException(status_code=409, detail="The deployed ticket's Deploy site checkbox is not checked.")

    row = resolve_webhook_approval(
        ZendeskWebhookRequest(
            action="deploy_site",
            approvalId=approval_id,
            canonicalLeadKey=canonical_key,
            zendeskTicketId=ticket_id,
            channel=channel,
            actor="AI Site Factory media refresh",
        )
    )
    repo = get_remote_github_repo(repo_full_name, github_headers())
    if not repo:
        raise HTTPException(status_code=404, detail="The managed GitHub repository could not be found.")
    branch = compact_text(repo.get("default_branch"), "main")
    get_github_text_file(repo_full_name, "index.html", branch)
    context = safe_json_loads(row["context_json"], {})
    context.update(
        {
            "businessName": business_name,
            "industry": compact_text(managed_value("industry"), context.get("industry") or "Local service"),
            "location": compact_text(managed_value("location"), context.get("location") or "South Africa"),
            "address": compact_text(managed_value("address"), context.get("address")),
            "mainImageUrl": main_image_url,
        }
    )
    context["brandTheme"] = business_theme_for_context(context)
    context["businessProfile"] = personalized_business_profile(context)
    refreshed_html = ensure_required_site_features(
        build_bootstrap_gsap_landing_html(context, dict(FREEFORM_SITE_SPEC)),
        context,
    )
    if main_image_url not in refreshed_html and html.escape(main_image_url, quote=True) not in refreshed_html:
        raise HTTPException(status_code=500, detail="The refreshed HTML did not preserve the requested business image.")

    owner, repo_name = repo_full_name.split("/", 1)
    github_update = put_github_file(
        owner,
        repo_name,
        branch,
        "index.html",
        refreshed_html,
        "Personalize business services, captions, image, and colour theme",
        github_headers(),
    )
    github_export = {
        "exportAction": github_update["action"],
        "repository": repo_full_name,
        "repoName": repo_name,
        "repoUrl": compact_text(repo.get("html_url"), f"https://github.com/{repo_full_name}"),
        "private": bool(repo.get("private")),
        "branch": branch,
        "path": "index.html",
        "htmlChecksum": html_checksum(refreshed_html),
        "indexContentSha": github_update.get("contentSha"),
        "commitSha": github_update.get("commitSha"),
        "htmlUrl": github_update.get("htmlUrl"),
        "pipelineId": row["pipeline_id"],
        "approvalId": row["id"],
        "exportedAt": now_iso(),
    }
    deployment = deploy_direct_netlify_fallback_for_lead(
        canonical_key=row["canonical_lead_key"],
        business_name=row["business_name"],
        site_html=refreshed_html,
        pipeline_id=row["pipeline_id"],
        approval_id=row["id"],
        approved_by="AI Site Factory media refresh",
        github_export=github_export,
        git_error=RuntimeError("Refreshing a previously deployed site's personalized business content and visual identity."),
    )
    if compact_text(deployment.get("state")).lower() != "ready":
        raise HTTPException(status_code=502, detail="The refreshed Netlify deployment did not become ready.")

    timestamp = now_iso()
    with get_pipeline_db() as db:
        db.execute(
            """
            UPDATE approval_records
            SET status = 'APPROVED', html = ?, html_checksum = ?, context_json = ?,
                github_export_json = ?, deployment_history_id = ?, approved_by = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                refreshed_html,
                html_checksum(refreshed_html),
                json.dumps(context, default=str),
                json.dumps(github_export, default=str),
                deployment.get("deploymentHistoryId"),
                "AI Site Factory media refresh",
                timestamp,
                row["id"],
            ),
        )
    custom_fields = zendesk_custom_fields(
        {"deployRequested": True, "leadStatus": "DEPLOYED", "liveUrl": deployment.get("url") or live_url}
    )
    extra_fields: Dict[str, Any] = {"status": ticket.get("status") or "open"}
    if custom_fields:
        extra_fields["custom_fields"] = custom_fields
    update_zendesk_ticket_comment(
        ticket_id,
        (
            "AI Site Factory refreshed the existing website with business-specific services and captions, "
            "the main listing image, and an industry-aligned colour palette. The live URL is unchanged."
        ),
        public=False,
        extra_ticket_fields=extra_fields,
    )
    final_tags = update_zendesk_ticket_tags(
        ticket_id,
        add=["asf_deployed", "asf_stage_live", "asf_repo_ready", "asf_deploy_requested", f"asf_channel_{channel}"],
    )
    return {
        "status": "REFRESHED",
        "ticketId": ticket_id,
        "approvalId": row["id"],
        "businessName": business_name,
        "mainImageUrl": main_image_url,
        "brandTheme": context["brandTheme"],
        "businessProfile": context["businessProfile"],
        "github": github_export,
        "deployment": deployment,
        "tags": final_tags,
    }


@app.get("/api/deployments/history")
def get_deployment_history(limit: int = 50):
    safe_limit = max(1, min(limit, 100))
    with get_pipeline_db() as db:
        rows = db.execute(
            """
            SELECT * FROM deployment_history
            ORDER BY deployed_at DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    history = []
    for row in rows:
        item = dict(row)
        item["raw"] = safe_json_loads(item.pop("raw_json", None), {})
        item["githubExport"] = safe_json_loads(item.pop("github_export_json", None), None)
        item["publishMode"] = item.pop("publish_mode", None) or "github-netlify"
        item["deploymentMode"] = deployment_mode_label(item["publishMode"])
        history.append(item)
    return {"deployments": history, "count": len(history)}


@app.get("/api/reporting/summary")
def get_reporting_summary():
    with get_pipeline_db() as db:
        leads_discovered = db.execute("SELECT COUNT(*) AS count FROM lead_registry").fetchone()["count"]
        duplicates_skipped = db.execute("SELECT COALESCE(SUM(duplicates_skipped), 0) AS count FROM discovery_batches").fetchone()["count"]
        pending_approvals = db.execute("SELECT COUNT(*) AS count FROM approval_records WHERE status = 'PENDING'").fetchone()["count"]
        approved_deployments = db.execute("SELECT COUNT(*) AS count FROM deployment_history").fetchone()["count"]
        github_repos = db.execute("SELECT COUNT(*) AS count FROM github_site_repos WHERE export_status = 'EXPORTED'").fetchone()["count"]
        git_deployments = db.execute("SELECT COUNT(*) AS count FROM deployment_history WHERE publish_mode = 'github-netlify'").fetchone()["count"]
        failed_steps = db.execute("SELECT COUNT(*) AS count FROM pipeline_steps WHERE status = 'FAILED'").fetchone()["count"]
        zendesk_tickets = db.execute("SELECT COUNT(*) AS count FROM approval_records WHERE zendesk_json IS NOT NULL").fetchone()["count"]
        pipeline_runs = db.execute("SELECT COUNT(*) AS count FROM pipeline_runs").fetchone()["count"]
        active_pipeline_runs = db.execute(
            "SELECT COUNT(*) AS count FROM pipeline_runs WHERE status IN ('PROCESSING', 'PENDING_APPROVAL', 'PARTIAL_PENDING')"
        ).fetchone()["count"]
        campaigns = db.execute("SELECT COUNT(*) AS count FROM campaigns").fetchone()["count"]
        campaign_pending = db.execute(
            "SELECT COUNT(*) AS count FROM campaign_deployments WHERE status NOT IN ('DEPLOYED', 'REUSED_DEPLOYMENT', 'FAILED', 'GENERATION_FAILED', 'DEPLOY_FAILED')"
        ).fetchone()["count"]
        campaign_ai_generations = db.execute(
            "SELECT COALESCE(SUM(ai_generation_count), 0) AS count FROM campaign_deployments"
        ).fetchone()["count"]
        campaign_repos_created = db.execute(
            "SELECT COALESCE(SUM(repo_created), 0) AS count FROM campaign_deployments"
        ).fetchone()["count"]
        status_rows = db.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM approval_records
            GROUP BY status
            """
        ).fetchall()

    return {
        "metrics": {
            "leadsDiscovered": leads_discovered,
            "duplicatesSkipped": duplicates_skipped,
            "pendingApprovals": pending_approvals,
            "approvedDeployments": approved_deployments,
            "githubRepos": github_repos,
            "gitDeployments": git_deployments,
            "failedSteps": failed_steps,
            "zendeskTickets": zendesk_tickets,
            "pipelineRuns": pipeline_runs,
            "activePipelineRuns": active_pipeline_runs,
            "campaigns": campaigns,
            "campaignPending": campaign_pending,
            "campaignAiGenerations": campaign_ai_generations,
            "campaignReposCreated": campaign_repos_created,
        },
        "approvalStatus": {row["status"]: row["count"] for row in status_rows},
        "generatedAt": now_iso(),
    }


@app.get("/")
def health_check():
    return {
        "message": "AI Site Factory Backend is running",
        "status": "online",
    }


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Avoid a noisy 404 when the backend root is opened in a browser."""
    return Response(status_code=204)


@app.post("/api/scrape/lead")
def scrape_lead(request: ScrapeRequest):
    url = request.url.strip()
    log_event("info", "scrape.start", "Scrape lead request started.", url=url)

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        response = requests.get(
            url,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
    except requests.RequestException:
        domain = urlparse(url).netloc.replace("www.", "")
        business_name = domain.split(".")[0].title()

        result = {
            "businessName": business_name,
            "email": f"info@{domain}" if domain else "info@example.com",
            "domain": domain,
            "category": "General Services",
            "location": detect_location_from_text("", domain, url),
            "notes": f"Could not fully scrape website. Lead created from domain {domain}.",
            "sourceType": "scraper-fallback",
        }
        log_event("warning", "scrape.fallback", "Scrape failed; returning fallback lead.", url=url, domain=domain)
        return result

    soup = BeautifulSoup(response.text, "html.parser")

    title = soup.title.string.strip() if soup.title and soup.title.string else ""

    meta_tag = soup.find("meta", attrs={"name": "description"})
    description = meta_tag.get("content", "").strip() if meta_tag else ""

    domain = urlparse(url).netloc.replace("www.", "")

    business_name = (
        title.split("|")[0].split("-")[0].strip()
        if title
        else domain.split(".")[0].title()
    )

    page_text = soup.get_text(" ", strip=True)

    email_match = re.search(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        page_text,
    )

    email = email_match.group(0) if email_match else f"info@{domain}"

    result = {
        "businessName": business_name,
        "email": email,
        "domain": domain,
        "category": "General Services",
        "location": detect_location_from_text(page_text, domain, url),
        "notes": description or f"Lead generated from {domain}",
        "sourceType": "real-scraper",
    }
    log_event("info", "scrape.finish", "Scrape lead request finished.", url=url, domain=domain, businessName=business_name)
    return result


@app.post("/api/leads/intake", response_model=IntakeResponse)
def intake_lead(request: IntakeRequest):
    validation_issues = []
    raw = request.rawLeadRow

    if not raw.businessName.strip():
        validation_issues.append("Business name is required.")

    if not raw.category.strip():
        validation_issues.append("Category is required.")

    lead_id = str(uuid4())

    LEADS_DB[lead_id] = {
        "leadId": lead_id,
        "rawLeadRow": raw.model_dump(),
        "sourceType": request.sourceType,
        "batchId": request.batchId,
        "createdAt": datetime.now().isoformat(),
        "validationIssues": validation_issues,
    }

    log_event("info", "lead.intake", "Lead intake created.", leadId=lead_id, businessName=raw.businessName, status="FLAGGED" if validation_issues else "INTAKE_CREATED", validationIssues=validation_issues)
    return IntakeResponse(
        leadId=lead_id,
        intakeStatus="FLAGGED" if validation_issues else "INTAKE_CREATED",
        validationIssues=validation_issues,
    )


@app.post("/api/leads/{lead_id}/clean", response_model=CleanedLead)
def clean_lead(lead_id: str):
    if lead_id not in LEADS_DB:
        log_event("warning", "lead.clean.not_found", "Clean lead failed because lead was not found.", leadId=lead_id)
        raise HTTPException(status_code=404, detail="Lead not found.")

    raw = LEADS_DB[lead_id]["rawLeadRow"]

    cleaned = CleanedLead(
        leadId=lead_id,
        businessName=raw["businessName"].strip(),
        email=raw["email"].lower(),
        domain=raw.get("domain", "Not provided").strip(),
        category=raw["category"].strip(),
        location=raw.get("location", "Not provided").strip(),
        sourceRef=LEADS_DB[lead_id].get("sourceType", "manual"),
        cleanSummary=raw.get("notes", "No additional notes provided.").strip(),
        cleanStatus="CLEAN",
        validationIssues=[],
    )

    LEADS_DB[lead_id]["cleanedLead"] = cleaned.model_dump()

    log_event("info", "lead.clean", "Lead cleaned.", leadId=lead_id, businessName=cleaned.businessName)
    return cleaned


@app.post("/api/content/generate", response_model=GenerationResponse)
def generate_content(request: GenerationRequest):
    lead = request.leadRecord

    content_packet = ContentPacket(
        headline=f"{lead.businessName} - {lead.category} Services in {lead.location}",
        summary=(
            f"{lead.businessName} provides reliable "
            f"{lead.category.lower()} services in {lead.location}. "
            f"{lead.cleanSummary}"
        ),
        serviceBlocks=[
            ServiceBlock(
                title=f"Professional {lead.category} Support",
                description=f"Reliable {lead.category.lower()} support tailored to customer needs.",
            ),
            ServiceBlock(
                title="Customer-Focused Service",
                description="Clear communication, practical assistance, and dependable service delivery.",
            ),
            ServiceBlock(
                title="Local Business Support",
                description=f"Serving customers in and around {lead.location}.",
            ),
        ],
        CTA=f"Contact {lead.businessName} today to learn more.",
        tone=request.toneProfile or "professional",
        brandNotes="Generated from cleaned lead data. No unsupported claims added.",
    )

    outreach_draft = OutreachDraft(
        subject=f"Website preview for {lead.businessName}",
        body=(
            f"Hi {lead.businessName},\n\n"
            f"We created a preview website concept based on your business profile. "
            f"It highlights your {lead.category.lower()} services and can be reviewed before any publishing or outreach action.\n\n"
            f"Kind regards,\nAI Site Factory Team"
        ),
        recipientEmail=lead.email,
        previewUrl=None,
        approvalStatus="Pending Review",
    )

    response = GenerationResponse(
        contentPacket=content_packet,
        outreachDraft=outreach_draft,
        generationStatus="GENERATED",
        generatedAt=datetime.now().isoformat(),
    )

    CONTENT_DB[lead.leadId] = response.model_dump()

    log_event("info", "content.generate", "Local content generated.", leadId=lead.leadId, businessName=lead.businessName, templateId=request.templateId)
    return response


@app.post("/api/site/build-preview", response_model=SiteBuildResponse)
def build_preview(request: SiteBuildRequest):
    build_reference = str(uuid4())

    preview_url = f"https://preview.ai-site-factory.local/{request.leadId}"

    response = SiteBuildResponse(
        previewUrl=preview_url,
        deploymentStatus="PREVIEW_READY",
        buildReference=build_reference,
        generatedAt=datetime.now().isoformat(),
        reviewStatus="PENDING_REVIEW",
        previewType="SIMULATED_PREVIEW_REFERENCE",
        limitationNote="Phase 1 returns a simulated preview URL reference. A real unique site deployment can be added in a later deployment automation step.",
    )

    PREVIEW_DB[request.leadId] = response.model_dump()

    log_event("info", "site.preview", "Local site preview reference created.", leadId=request.leadId, buildReference=build_reference)
    return response


@app.get("/api/leads/{lead_id}")
def get_lead(lead_id: str):
    if lead_id not in LEADS_DB:
        log_event("warning", "lead.get.not_found", "Get lead failed because lead was not found.", leadId=lead_id)
        raise HTTPException(status_code=404, detail="Lead not found.")

    return LEADS_DB[lead_id]


@app.post("/api/zendesk/sync-lead")
def sync_lead_to_zendesk(request: ZendeskSyncRequest):
    if not env_enabled("ENABLE_LEGACY_ZENDESK_SYNC"):
        raise HTTPException(
            status_code=410,
            detail={
                "code": "LEGACY_ZENDESK_SYNC_DISABLED",
                "message": "Use named campaign intake so every ticket has managed routing, fields, and idempotency.",
            },
        )
    log_event("info", "zendesk.sync.start", "Zendesk lead sync started.", leadId=request.leadId, businessName=request.businessName, email=request.email)
    zendesk_subdomain = os.getenv("ZENDESK_SUBDOMAIN")
    zendesk_email = os.getenv("ZENDESK_EMAIL")
    zendesk_token = os.getenv("ZENDESK_API_TOKEN")

    if not zendesk_subdomain or not zendesk_email or not zendesk_token:
        log_event("error", "zendesk.sync.config_missing", "Zendesk sync failed because environment variables are missing.", leadId=request.leadId)
        raise HTTPException(
            status_code=500,
            detail="Zendesk environment variables are missing.",
        )

    auth = (f"{zendesk_email}/token", zendesk_token)
    base_url = f"https://{zendesk_subdomain}.zendesk.com/api/v2"

    headers = {
        "Content-Type": "application/json",
    }

    try:
        # 1. Search for existing organization by business name
        org_search_response = requests.get(
            f"{base_url}/organizations/search.json",
            params={"name": request.businessName},
            auth=auth,
            headers=headers,
            timeout=15,
        )
        org_search_response.raise_for_status()

        organizations = org_search_response.json().get("organizations", [])

        if organizations:
            organization = organizations[0]
        else:
            # 2. Create organization if not found
            org_create_response = requests.post(
                f"{base_url}/organizations.json",
                json={
                    "organization": {
                        "name": request.businessName,
                        "notes": (
                            f"Created from AI Site Factory lead sync.\n"
                            f"Category: {request.category}\n"
                            f"Lead ID: {request.leadId}"
                        ),
                        "tags": [
                            "ai_site_factory",
                            "lead_organization",
                        ],
                    }
                },
                auth=auth,
                headers=headers,
                timeout=15,
            )
            org_create_response.raise_for_status()
            organization = org_create_response.json().get("organization", {})

        organization_id = organization.get("id")

        # 3. Search for existing user by email
        user_search_response = requests.get(
            f"{base_url}/users/search.json",
            params={"query": request.email},
            auth=auth,
            headers=headers,
            timeout=15,
        )
        user_search_response.raise_for_status()

        users = user_search_response.json().get("users", [])

        if users:
            user = users[0]

            # Update user organization if needed
            requests.put(
                f"{base_url}/users/{user.get('id')}.json",
                json={
                    "user": {
                        "organization_id": organization_id,
                        "tags": [
                            "ai_site_factory",
                            "lead_contact",
                        ],
                    }
                },
                auth=auth,
                headers=headers,
                timeout=15,
            )
        else:
            # 4. Create user if not found
            user_create_response = requests.post(
                f"{base_url}/users.json",
                json={
                    "user": {
                        "name": request.businessName,
                        "email": request.email,
                        "organization_id": organization_id,
                        "role": "end-user",
                        "notes": (
                            f"Lead contact created from AI Site Factory.\n"
                            f"Lead ID: {request.leadId}\n"
                            f"Category: {request.category}"
                        ),
                        "tags": [
                            "ai_site_factory",
                            "lead_contact",
                        ],
                    }
                },
                auth=auth,
                headers=headers,
                timeout=15,
            )
            user_create_response.raise_for_status()
            user = user_create_response.json().get("user", {})

        user_id = user.get("id")

        # 5. Create Zendesk ticket linked to user and organization
        ticket_response = requests.post(
            f"{base_url}/tickets.json",
            json={
                "ticket": {
                    "subject": f"Approved AI Site Factory Lead: {request.businessName}",
                    "comment": {
                        "body": (
                            f"Approved lead synced from AI Site Factory.\n\n"
                            f"Lead ID: {request.leadId}\n"
                            f"Business Name: {request.businessName}\n"
                            f"Email: {request.email}\n"
                            f"Category: {request.category}\n"
                            f"Preview Reference: {request.previewReference}\n"
                            f"Approval Status: {request.approvalStatus}\n"
                            f"Synced At: {datetime.now().isoformat()}"
                        )
                    },
                    "requester_id": user_id,
                    "organization_id": organization_id,
                    "tags": [
                        "ai_site_factory",
                        "phase_2",
                        "approved_lead",
                        "lead_tracking",
                    ],
                    "priority": "normal",
                    "type": "task",
                }
            },
            auth=auth,
            headers=headers,
            timeout=15,
        )
        ticket_response.raise_for_status()

    except requests.RequestException as error:
        log_event("error", "zendesk.sync.failed", "Zendesk sync failed.", leadId=request.leadId, reason=str(error))
        raise HTTPException(
            status_code=500,
            detail=f"Zendesk sync failed: {sanitize_message(error)}",
        )

    ticket_data = ticket_response.json().get("ticket", {})

    result = {
        "syncStatus": "SYNCED",
        "organizationId": organization_id,
        "organizationName": organization.get("name"),
        "userId": user_id,
        "userEmail": user.get("email"),
        "zendeskRecordId": ticket_data.get("id"),
        "ticketUrl": (
            f"https://{zendesk_subdomain}.zendesk.com/agent/tickets/"
            f"{ticket_data.get('id')}"
        ),
        "syncedAt": datetime.now().isoformat(),
        "message": (
            f"Lead {request.businessName} synced to Zendesk with organization, "
            f"user, and ticket."
        ),
    }
    log_event("info", "zendesk.sync.finish", "Zendesk lead sync finished.", leadId=request.leadId, ticketId=result["zendeskRecordId"])
    return result


@app.post("/api/outreach/generate", response_model=OutreachDraftResponse)
def generate_outreach(request: OutreachGenerateRequest):
    subject = f"Website preview for {request.businessName}"

    body = (
        f"Hi {request.businessName} Team,\n\n"
        f"We created a preview website concept for your business based on your public online information. "
        f"The preview highlights your {request.category.lower()} services and shows how your business could be presented in a simple, lead-focused format.\n\n"
        f"Preview Reference: {request.previewReference}\n\n"
        f"If this is something your team would like to review further, we would be happy to share more details.\n\n"
        f"Kind regards,\n"
        f"AI Site Factory Team"
    )

    log_event("info", "outreach.generate", "Outreach draft generated.", leadId=request.leadId, businessName=request.businessName, email=request.email)
    return OutreachDraftResponse(
        subject=subject,
        body=body,
        recipientEmail=request.email,
        status="DRAFT_GENERATED",
    )


@app.post("/api/outreach/send")
def send_outreach(request: OutreachSendRequest):
    log_event("info", "outreach.send.start", "Outreach send started.", zendeskTicketId=request.zendeskTicketId, recipientEmail=request.recipientEmail)
    zendesk_subdomain = os.getenv("ZENDESK_SUBDOMAIN")
    zendesk_email = os.getenv("ZENDESK_EMAIL")
    zendesk_token = os.getenv("ZENDESK_API_TOKEN")

    if not zendesk_subdomain or not zendesk_email or not zendesk_token:
        log_event("error", "outreach.send.config_missing", "Outreach send failed because Zendesk environment variables are missing.", zendeskTicketId=request.zendeskTicketId)
        raise HTTPException(
            status_code=500,
            detail="Zendesk environment variables are missing.",
        )

    auth = (f"{zendesk_email}/token", zendesk_token)
    base_url = f"https://{zendesk_subdomain}.zendesk.com/api/v2"

    payload = {
        "ticket": {
            "comment": {
                "body": (
                    f"Outbound outreach message sent through AI Site Factory.\n\n"
                    f"Subject: {request.subject}\n\n"
                    f"{request.body}"
                ),
                "public": False,
            },
            "status": "open",
            "tags": [
                "ai_site_factory",
                "outreach_sent",
                "phase_2",
            ],
        }
    }

    try:
        response = requests.put(
            f"{base_url}/tickets/{request.zendeskTicketId}.json",
            json=payload,
            auth=auth,
            timeout=15,
        )

        response.raise_for_status()

    except requests.RequestException as error:
        log_event("error", "outreach.send.failed", "Outreach send failed.", zendeskTicketId=request.zendeskTicketId, reason=str(error))
        raise HTTPException(
            status_code=500,
            detail=f"Outreach send failed: {sanitize_message(error)}",
        )

    result = {
        "sendStatus": "SENT",
        "zendeskTicketId": request.zendeskTicketId,
        "recipientEmail": request.recipientEmail,
        "sentAt": datetime.now().isoformat(),
        "message": "Outreach message added to Zendesk ticket as a private comment.",
    }
    log_event("info", "outreach.send.finish", "Outreach send finished.", zendeskTicketId=request.zendeskTicketId, recipientEmail=request.recipientEmail)
    return result
