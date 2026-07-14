from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4
import base64
from collections import deque
import hashlib
import html
import io
import json
import logging
import re
import sqlite3
import time
import zipfile
from urllib.parse import urlparse
import os
from dotenv import load_dotenv

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
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

STARTED_AT = datetime.now()
LOG_BUFFER = deque(maxlen=int(os.getenv("APP_LOG_BUFFER_SIZE", "250")))
SENSITIVE_KEY_PATTERN = re.compile(r"(token|key|secret|password|authorization|auth|email)", re.IGNORECASE)
MODEL_CHUNK_CHARS = int(os.getenv("MODEL_CHUNK_CHARS", "1800"))
MODEL_MAX_CHUNKS = int(os.getenv("MODEL_MAX_CHUNKS", "4"))


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

FREEFORM_SITE_SPEC = {
    "id": FREEFORM_TEMPLATE_ID,
    "name": "Gemini Freeform",
    "description": "Gemini controls page structure and visual direction; backend enforces required libraries and safety features.",
    "accent": "#0f9f96",
    "background": "#f8fbff",
}

LANDING_PAGE_PROMPT_HEADER = """
You are creating a production-ready, single-file HTML landing page for a business with no current website.
Return strict JSON with keys: html, qaNotes, structureNotes, stylingLibraries.
The html value must be a complete <!doctype html> document.

Rules:
- Use only the supplied public lead/business information. Do not invent awards, prices, guarantees, team members, certifications, or unavailable services.
- Gemini may choose the page structure, copy hierarchy, and styling direction.
- The page must include Bootstrap 5.3.8 and at least two additional styling libraries. Prefer Bootstrap 5.3.8, Tailwind browser CDN, and Animate.css when unsure.
- Include a visible dynamic color/theme widget that lets visitors change site colors and persists selections with localStorage.
- Include accessible semantic sections, mobile-first responsive layout, clear contact options, and grounded calls to action.
- Include a prominent generated hero/banner image personalised to the business name, industry, location, and public lead context. Prefer an inline SVG or data URI so the page works as a single file; do not rely on stock-photo URLs.
- Personalise the copy with concrete supplied details such as business name, category/industry, city/location, address, rating/review count, source listing, phone, email, service keywords, differentiators, and proof points when they are present. Avoid generic filler when a supplied detail is available.
- CTA buttons must have working destinations: use mailto: for email leads, tel: for phone leads, the supplied website URL if present, or valid in-page anchors such as #services and #contact. Do not use empty hrefs, href="#", javascript:void(0), or non-functional buttons.
- Do not include external tracking pixels, forms that submit data, or claims of consent.
""".strip()

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
        secret = os.getenv(name)
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
            value = os.getenv(name)
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
    "canonicalLeadKey",
    "pipelineId",
    "approvalId",
    "batchId",
    "contactChannel",
    "leadStatus",
    "deployRequested",
    "emailSendRequested",
    "phoneCallStatus",
    "liveUrl",
    "sourceUrl",
]


class ZendeskFieldSettingsRequest(BaseModel):
    fields: Dict[str, Optional[str]] = Field(default_factory=dict)


class ZendeskWebhookRequest(BaseModel):
    action: str
    approvalId: Optional[str] = None
    canonicalLeadKey: Optional[str] = None
    zendeskTicketId: Optional[int] = None
    channel: Optional[str] = None
    value: Optional[Any] = None
    actor: Optional[str] = "Zendesk Webhook"
    notes: Optional[str] = None


class DiscoverLeadsRequest(BaseModel):
    presetId: str
    location: str = "South Africa"
    query: Optional[str] = None
    limit: int = 3
    forceRefresh: bool = False


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


def get_template_or_404(template_id: str) -> Dict[str, Any]:
    for template in SITE_TEMPLATES:
        if template["id"] == template_id:
            return template
    raise HTTPException(status_code=404, detail="Site template not found.")


def require_env(name: str) -> str:
    value = os.getenv(name)
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
    return email if email and "@" in email else None


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
    "za",
    "zaf",
    ".co.za",
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
    return datetime.now().isoformat()


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
                deployment_count INTEGER NOT NULL DEFAULT 0
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

            CREATE TABLE IF NOT EXISTS zendesk_field_settings (
                field_key TEXT PRIMARY KEY,
                field_id TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS zendesk_ticket_links (
                id TEXT PRIMARY KEY,
                approval_id TEXT NOT NULL,
                canonical_lead_key TEXT NOT NULL,
                pipeline_id TEXT,
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

    place_id = first_present(raw, ["placeId", "place_id", "googlePlaceId", "googleId", "cid", "fid", "id"])
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
    return bool(normalize_email_identity(lead.email) or compact_text(lead.phone))


def lead_contact_bucket(lead: DiscoveredLead) -> str:
    has_email = bool(normalize_email_identity(lead.email))
    has_phone = bool(compact_text(lead.phone))
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
    leads = [
        lead
        for lead in cached_leads
        if not lead_has_website(lead) and lead_has_contact(lead)
    ][:limit]
    if not leads:
        return None

    email_count = sum(1 for lead in leads if normalize_email_identity(lead.email))
    phone_count = sum(1 for lead in leads if compact_text(lead.phone))

    return DiscoverLeadsResponse(
        batchId=row["batch_id"],
        preset=preset,
        location=row["location"],
        query=row["query"],
        leads=leads,
        sourceStatus="CACHE",
        warnings=safe_json_loads(row["warnings_json"], []),
        provinceStats=safe_json_loads(row["province_stats_json"], {}),
        duplicatesSkipped=row["duplicates_skipped"],
        requestedCount=limit,
        rawFetched=len(cached_leads),
        eligibleReturned=len(leads),
        websitesSkipped=sum(1 for lead in cached_leads if lead_has_website(lead)),
        noContactSkipped=sum(1 for lead in cached_leads if not lead_has_contact(lead)),
        generatedDuplicatesSkipped=row["duplicates_skipped"],
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
        pending_count = counts.get("PENDING", 0)
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
    site_html: str,
    context: Dict[str, Any],
    site_content: Dict[str, Any],
    template: Dict[str, Any],
    status: str = "PENDING",
) -> str:
    approval_id = str(uuid4())
    timestamp = now_iso()
    with get_pipeline_db() as db:
        db.execute(
            """
            INSERT INTO approval_records (
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
                html_checksum(site_html),
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
    cleaned = {
        key: compact_text(fields.get(key)) or None
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


def zendesk_custom_fields(values: Dict[str, Any]) -> List[Dict[str, Any]]:
    settings = get_zendesk_field_settings()
    fields: List[Dict[str, Any]] = []
    for key, value in values.items():
        field_id = compact_text(settings.get(key))
        if not field_id or value in (None, ""):
            continue
        try:
            parsed_id: Any = int(field_id)
        except ValueError:
            parsed_id = field_id
        fields.append({"id": parsed_id, "value": value})
    return fields


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
) -> Dict[str, Any]:
    timestamp = now_iso()
    link_id = str(uuid4())

    with get_pipeline_db() as db:
        db.execute(
            """
            INSERT INTO zendesk_ticket_links (
                id, approval_id, canonical_lead_key, pipeline_id, ticket_id, ticket_url,
                channel, stage, status, tags_json, payload_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(approval_id, channel, stage) DO UPDATE SET
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
        approval["pendingPreviewHtml"] = ensure_required_site_features(row["html"]) if row["html"] else None
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


init_pipeline_db()


def infer_country_code(location: str) -> Optional[str]:
    location_lower = compact_text(location).lower()
    if "south africa" in location_lower or any(term in location_lower for term in SOUTH_AFRICA_TERMS):
        return "za"
    return None


def build_google_maps_query(preset: Dict[str, Any], location: str, custom_query: Optional[str] = None) -> str:
    """Build a Google Maps search query with clear geographic intent."""
    query_term = compact_text(custom_query) or compact_text(preset.get("query", ""))
    if not query_term:
        query_term = preset.get("industry", "services")

    location_term = compact_text(location, "South Africa")
    return f"{query_term} in {location_term}"


def run_apify_google_maps(query: str, limit: int, location: str = "South Africa") -> List[Dict[str, Any]]:
    token = require_env("APIFY_API_TOKEN")
    actor_id = os.getenv("APIFY_GOOGLE_MAPS_ACTOR_ID", "compass/crawler-google-places").replace("/", "~")
    max_items = max(limit, 5)
    country_code = infer_country_code(location)

    log_event(
        "info",
        "provider.apify.start",
        "Starting Apify Google Maps discovery.",
        query=query,
        location=location,
        limit=max_items,
        actorId=actor_id,
        countryCode=country_code,
    )

    url = (
        f"https://api.apify.com/v2/actors/{actor_id}/run-sync-get-dataset-items"
        f"?clean=true&format=json&timeout=180&maxItems={max_items}"
    )

    payload = {
        "searchStringsArray": [query],
        "language": "en",
        "maxCrawledPlacesPerSearch": max_items,
        "includeWebResults": True,
    }

    # Some Google Maps actors support country/location fields; if unsupported, we retry safely below.
    if country_code:
        payload["countryCode"] = country_code
        payload["locationQuery"] = location

    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=540,
    )

    if response.status_code == 400:
        log_event(
            "warning",
            "provider.apify.retry_minimal_payload",
            "Apify rejected the extended payload. Retrying with minimal payload.",
            query=query,
            statusCode=response.status_code,
            responseText=response.text[:500],
        )
        minimal_payload = {
            "searchStringsArray": [query],
            "language": "en",
            "maxCrawledPlacesPerSearch": max_items,
            "includeWebResults": True,
        }
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=minimal_payload,
            timeout=540,
        )

    response.raise_for_status()

    log_event(
        "info",
        "provider.apify.finish",
        "Apify returned Google Maps items.",
        statusCode=response.status_code,
    )

    data = response.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    return []


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

    if "south africa" in requested or any(term in requested for term in SOUTH_AFRICA_TERMS):
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
) -> List[DiscoveredLead]:
    leads: List[DiscoveredLead] = []
    seen = set()
    skipped_location = 0

    for item in items:
        business_name = first_present(
            item,
            ["title", "name", "businessName", "placeName", "companyName"],
        )
        if not business_name:
            continue

        website = normalize_url(first_present(item, ["website", "site", "homepage"]))
        domain = domain_from_url(website)
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

    log_event(
        "info",
        "leads.normalize.finish",
        "Lead normalization finished.",
        returned=len(leads),
        skippedLocation=skipped_location,
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
    return {
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
        "notes": contact_details.get("notes") or lead.notes,
        "summary": contact_details.get("notes") or lead.notes or f"{lead.businessName} is a local {lead.category} business.",
        "targetCustomers": "Local customers",
        "differentiators": [],
        "serviceKeywords": [lead.category],
        "sourceNote": "Public Google Maps, business listing, and website context.",
        "rawLead": lead.raw or {},
        "noWebsiteLead": not bool(normalize_url(website) or normalize_domain(lead.domain)),
    }


def compact_lead_with_groq(context: Dict[str, Any]) -> Dict[str, Any]:
    prompt = (
        "Compact this public lead into a concise business brief for Gemini to build a landing page. "
        "Use as much of the lead as is useful, including raw listing fields, but remove repetition. "
        "Return strict JSON with keys: businessName, industry, location, address, email, phone, "
        "summary, serviceKeywords, differentiators, proofPoints, sourceLabel, sourceUrl, noWebsiteLead, "
        "contactType, designHints, complianceNotes. Arrays must be arrays. "
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
    brief.setdefault("industry", context.get("industry") or context.get("category"))
    brief.setdefault("location", context.get("location"))
    brief.setdefault("address", context.get("address"))
    brief.setdefault("email", context.get("email"))
    brief.setdefault("phone", context.get("phone"))
    brief.setdefault("summary", context.get("summary"))
    brief.setdefault("sourceLabel", context.get("source") or "public business listing")
    brief.setdefault("sourceUrl", context.get("sourceUrl"))
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
    lower_html = html_value.lower()
    contact_href, contact_label = contact_cta_for_context(context)

    if "<img" not in lower_html and "<svg" not in lower_html:
        business_name = compact_text(context.get("businessName"), "Local Business")
        industry = compact_text(context.get("industry"), "Local Service")
        location = compact_text(context.get("location"), "South Africa")
        hero_image = fallback_image_data_uri(
            business_name,
            "#0f9f96",
            f"{industry} in {location}",
            compact_text(context.get("address") or context.get("sourceLabel") or context.get("source"), "Generated for this business"),
        )
        hero_markup = f"""
<section class="ai-generated-hero-image" aria-label="Generated business banner">
  <div>
    <span>{html.escape(industry)} in {html.escape(location)}</span>
    <h2>{html.escape(business_name)}</h2>
    <p>Generated visual based on the supplied public business details.</p>
  </div>
  <img src="{hero_image}" alt="Generated banner image for {html.escape(business_name)}">
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
            "Gemini final HTML was unavailable. Using the local Bootstrap/GSAP renderer.",
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
            "stylingLibraries": ["Bootstrap", "GSAP"],
            "promptHeader": LANDING_PAGE_PROMPT_HEADER,
            "fallbackReason": sanitize_message(error),
        }

    site_html = result.get("html") or result.get("siteHtml") or result.get("finalHtml")
    if not site_html:
        raise RuntimeError("Gemini did not return an html field.")
    final_html = ensure_required_site_features(ensure_generated_hero_and_working_links(str(site_html), lead_brief))
    return {
        "html": final_html,
        "qaNotes": result.get("qaNotes") or "Gemini generated final HTML; backend enforced required assets and color widget.",
        "structureNotes": result.get("structureNotes"),
        "stylingLibraries": result.get("stylingLibraries") or ["Bootstrap", "Tailwind CSS", "Animate.css"],
        "promptHeader": LANDING_PAGE_PROMPT_HEADER,
    }


def generate_page_prompt_with_gemini(context: Dict[str, Any], template: Dict[str, Any]) -> Dict[str, Any]:
    prompt = (
        "Create a production-ready prompt for a single-file HTML landing page. "
        "Return strict JSON with keys: pagePrompt, designNotes, contentGuardrails, imageDirection. "
        "The pagePrompt must preserve: hero, four service cards, about section, contact section, footer. "
        "The page must use Bootstrap 5.3.8 CSS from https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css and GSAP 3.15 from https://cdn.jsdelivr.net/npm/gsap@3.15/dist/gsap.min.js for responsive layout/components and entry animations. "
        "Require an animated hero section, strong CTA buttons, modern service cards, polished gradients, spacing, shadows, hover effects, and responsive layout. "
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
            "contact, and footer. Use Bootstrap 5.3.8 CSS CDN, GSAP 3.15 CDN animations, accessible semantic HTML, strong CTA buttons, modern cards, polished gradients, spacing, shadows, hover effects, and grounded claims only."
        ),
    )
    result.setdefault("designNotes", f"Use accent {template.get('accent')} and background {template.get('background')}.")
    result.setdefault("contentGuardrails", "Use only the provided public lead context.")
    result.setdefault("imageDirection", "Use tasteful CSS gradients or placeholder imagery.")
    return result

def build_bootstrap_gsap_landing_html(context: Dict[str, Any], template: Dict[str, Any]) -> str:
    business_name_raw = compact_text(context.get("businessName"), "Local Business")
    industry_raw = compact_text(context.get("industry"), "Local Service")
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
    summary_raw = compact_text(
        context.get("summary"),
        f"{business_name_raw} provides reliable {industry_raw.lower()} services for customers in {location_raw}."
    )
    summary = html.escape(
        compact_text(
            summary_raw,
            f"{business_name_raw} provides reliable {industry_raw.lower()} services for customers in {location_raw}."
        )
    )

    email = compact_text(context.get("email"))
    phone = compact_text(context.get("phone"))
    website = normalize_url(context.get("website"))
    source_url = normalize_url(context.get("sourceUrl"))

    accent = compact_text(template.get("accent"), "#00AEEF")
    background = compact_text(template.get("background"), "#F7FAFC")

    keywords = context.get("serviceKeywords")
    if not isinstance(keywords, list) or not keywords:
        keywords = [industry_raw]
    clean_keywords = [compact_text(keyword, industry_raw) for keyword in keywords[:4]]
    while len(clean_keywords) < 4:
        clean_keywords.append(industry_raw)

    differentiators = context.get("differentiators")
    if not isinstance(differentiators, list):
        differentiators = []
    proof_points = context.get("proofPoints")
    if not isinstance(proof_points, list):
        proof_points = []

    detail_bits = [industry_raw]
    if address_raw:
        detail_bits.append(address_raw)
    else:
        detail_bits.append(location_raw)
    if rating_raw:
        rating_label = f"{rating_raw} rating"
        if reviews_raw:
            rating_label += f" from {reviews_raw} reviews"
        detail_bits.append(rating_label)
    elif reviews_raw:
        detail_bits.append(f"{reviews_raw} public reviews")

    hero_image = fallback_image_data_uri(
        business_name_raw,
        accent,
        f"{industry_raw} in {location_raw}",
        " | ".join(detail_bits[:3]),
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

    service_descriptions = [
        f"{business_name_raw} presents {clean_keywords[0].lower()} information clearly for customers in {location_raw}.",
        f"Contact options are surfaced up front so visitors can reach {business_name_raw} without hunting through the page.",
        f"The page uses public listing context from {source_raw} to keep the message grounded and factual.",
        f"Customers can quickly review the business focus, location, and available contact route before taking action.",
    ]
    if differentiators:
        service_descriptions[1] = compact_text(differentiators[0], service_descriptions[1])
    if proof_points:
        service_descriptions[2] = compact_text(proof_points[0], service_descriptions[2])

    default_services = [
        {"title": clean_keywords[0], "description": service_descriptions[0]},
        {"title": clean_keywords[1], "description": service_descriptions[1]},
        {"title": clean_keywords[2], "description": service_descriptions[2]},
        {"title": clean_keywords[3], "description": service_descriptions[3]},
    ]

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
        f"{reviews_raw} reviews" if reviews_raw else "Public lead context",
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
          <span class="hero-badge hero-animate">Local {industry}</span>
          <h1 class="hero-title hero-animate">{business_name}</h1>
          <p class="hero-text hero-animate">{summary}</p>
          <div class="hero-actions hero-animate">
            <a href="{html.escape(contact_target)}" class="btn btn-light btn-lg rounded-pill px-4 shadow"{hero_cta_attrs}>{html.escape(contact_label)}</a>
            <a href="#services" class="btn btn-outline-light btn-lg rounded-pill px-4">View services</a>
          </div>
        </div>

        <div class="col-lg-5">
          <div class="hero-card hero-visual">
            <img src="{hero_image}" alt="Generated banner image for {business_name}">
            <div class="hero-card-body">
              <p class="fw-bold mb-3">Serving {location}</p>
              {proof_chips_html}
              <hr class="border-light opacity-25 my-4">
              <p class="mb-0">Generated for {business_name} from {source} details so customers can quickly understand the business and use the right contact route.</p>
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
          <h2 class="section-title">Services designed for local customers</h2>
          <p class="text-secondary mx-auto" style="max-width: 720px;">Clear, useful information about {business_name}, its {industry.lower()} focus, and its presence in {location}.</p>
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
                <h2 class="section-title">Built around trust, clarity, and service.</h2>
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
          <p class="contact-text text-white-50 mx-auto" style="max-width: 680px;">Use the options below to get in touch, ask a question, or request more information.</p>
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
      if (!window.gsap) return;

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
    return ensure_required_site_features(site_html)

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
    return re.sub(pattern, replacement, site_html, count=1, flags=re.IGNORECASE | re.DOTALL)


def ensure_required_site_features(site_html: str) -> str:
    html_value = site_html
    lower_html = html_value.lower()

    bootstrap_css = (
        '<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" '
        'rel="stylesheet" '
        'integrity="sha384-sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB" crossorigin="anonymous">'
    )
    tailwind_js = '<script src="https://cdn.tailwindcss.com"></script>'
    animate_css = '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/animate.css/4.1.1/animate.min.css">'
    bootstrap_js = (
        '<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/js/bootstrap.bundle.min.js"></script>'
    )
    gsap_js = '<script src="https://cdn.jsdelivr.net/npm/gsap@3.15/dist/gsap.min.js"></script>'
    widget_css = """
<style id="ai-site-theme-widget-style">
  :root {
    --ai-text: #102033;
    --ai-background: #f8fbff;
    --ai-highlight: #0f9f96;
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
    background-color: var(--ai-highlight) !important;
    border-color: var(--ai-highlight) !important;
  }
  .hero,
  .about-gradient,
  .cta-band,
  .ai-generated-hero-image {
    background-color: var(--ai-highlight);
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
    widget_html = """
<aside class="ai-site-theme-widget" data-ai-site-theme-widget aria-label="Site color controls">
  <strong>Site colors</strong>
  <div class="ai-site-theme-controls">
    <label>Text<input type="color" data-theme-color="text" value="#102033"></label>
    <label>Background<input type="color" data-theme-color="background" value="#f8fbff"></label>
    <label>Highlights<input type="color" data-theme-color="highlight" value="#0f9f96"></label>
  </div>
  <button class="ai-site-theme-reset" type="button" data-theme-reset>Reset colors</button>
</aside>"""
    widget_js = """
<script id="ai-site-theme-widget-script">
  window.addEventListener("DOMContentLoaded", function () {
    var storageKey = "ai-site-factory-theme-v2";
    var defaults = { text: "#102033", background: "#f8fbff", highlight: "#0f9f96" };
    var root = document.documentElement;
    function readTheme() {
      try { return Object.assign({}, defaults, JSON.parse(localStorage.getItem(storageKey) || "{}")); }
      catch (error) { return Object.assign({}, defaults); }
    }
    function applyTheme(theme) {
      var text = theme.text || defaults.text;
      var background = theme.background || defaults.background;
      var highlight = theme.highlight || defaults.highlight;
      root.style.setProperty("--ai-text", text);
      root.style.setProperty("--ai-background", background);
      root.style.setProperty("--ai-highlight", highlight);
      root.style.setProperty("--ink", text);
      root.style.setProperty("--dark", text);
      root.style.setProperty("--background", background);
      root.style.setProperty("--template-bg", background);
      root.style.setProperty("--primary", highlight);
      root.style.setProperty("--secondary", highlight);
      root.style.setProperty("--accent", highlight);
      root.style.setProperty("--template-accent", highlight);
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
    animation_js = """
<script>
  window.addEventListener("DOMContentLoaded", function () {
    if (window.gsap) {
      gsap.from("header, .hero, main section, .card, .service-card", {
        y: 24,
        opacity: 0,
        duration: 0.75,
        ease: "power2.out",
        stagger: 0.08
      });
    }
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
    if "cdn.tailwindcss.com" not in lower_html:
        html_value = inject_before_closing_tag(html_value, "head", f"  {tailwind_js}")

    lower_html = html_value.lower()
    if "animate.css" not in lower_html and "animate.min.css" not in lower_html:
        html_value = inject_before_closing_tag(html_value, "head", f"  {animate_css}")

    lower_html = html_value.lower()
    if "ai-site-theme-widget-style" not in lower_html:
        html_value = inject_before_closing_tag(html_value, "head", widget_css)

    lower_html = html_value.lower()
    scripts = []
    if "bootstrap@5.3.8/dist/js/bootstrap.bundle.min.js" not in lower_html:
        scripts.append(bootstrap_js)
    if "gsap@3.15/dist/gsap.min.js" not in lower_html:
        scripts.append(gsap_js)
    if "gsap.from" not in lower_html:
        scripts.append(animation_js)
    if "data-ai-site-theme-widget" not in lower_html:
        scripts.append(widget_html)
    if "ai-site-theme-widget-script" not in lower_html:
        scripts.append(widget_js)

    if scripts:
        injection = "\n".join(scripts)
        html_value = inject_before_closing_tag(html_value, "body", injection)

    return html_value


def ensure_bootstrap_gsap_assets(site_html: str) -> str:
    return ensure_required_site_features(site_html)


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


def fallback_image_data_uri(label: str, accent: str, subtitle: str = "", detail: str = "") -> str:
    safe_label = html.escape(compact_text(label, "Business"))
    safe_subtitle = html.escape(compact_text(subtitle, "Local service"))
    safe_detail = html.escape(compact_text(detail, "Customer focused"))
    safe_accent = compact_text(accent, "#0f9f96")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", safe_accent):
        safe_accent = "#0f9f96"
    svg = (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='1200' height='800' viewBox='0 0 1200 800'>"
        f"<defs>"
        f"<linearGradient id='bg' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='#ecfeff'/><stop offset='0.55' stop-color='#f8fafc'/><stop offset='1' stop-color='#eef2ff'/></linearGradient>"
        f"<linearGradient id='accent' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='{safe_accent}'/><stop offset='1' stop-color='#1d9bf0'/></linearGradient>"
        f"<filter id='shadow' x='-20%' y='-20%' width='140%' height='140%'><feDropShadow dx='0' dy='28' stdDeviation='26' flood-color='#0f172a' flood-opacity='0.18'/></filter>"
        f"</defs>"
        f"<rect width='1200' height='800' fill='url(#bg)'/>"
        f"<circle cx='970' cy='170' r='210' fill='{safe_accent}' opacity='0.13'/>"
        f"<circle cx='180' cy='650' r='250' fill='#1d9bf0' opacity='0.10'/>"
        f"<rect x='88' y='96' width='1024' height='608' rx='42' fill='#ffffff' opacity='0.78' filter='url(#shadow)'/>"
        f"<rect x='138' y='158' width='426' height='300' rx='30' fill='url(#accent)' opacity='0.95'/>"
        f"<path d='M188 388 C275 302 357 426 438 326 C482 272 524 278 560 238' fill='none' stroke='white' stroke-width='24' stroke-linecap='round' opacity='0.74'/>"
        f"<circle cx='256' cy='246' r='48' fill='white' opacity='0.85'/>"
        f"<rect x='626' y='174' width='372' height='34' rx='17' fill='{safe_accent}' opacity='0.20'/>"
        f"<rect x='626' y='250' width='436' height='26' rx='13' fill='#0f172a' opacity='0.10'/>"
        f"<rect x='626' y='304' width='328' height='26' rx='13' fill='#0f172a' opacity='0.10'/>"
        f"<rect x='626' y='544' width='192' height='58' rx='29' fill='url(#accent)'/>"
        f"<text x='626' y='197' font-family='Arial, sans-serif' font-size='22' font-weight='800' fill='{safe_accent}'>{safe_subtitle}</text>"
        f"<text x='626' y='408' font-family='Arial, sans-serif' font-size='54' font-weight='800' fill='#102033'>{safe_label}</text>"
        f"<text x='626' y='472' font-family='Arial, sans-serif' font-size='28' font-weight='600' fill='#475467'>{safe_detail}</text>"
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
    if not images:
        images = [fallback_image_data_uri(compact_text(context.get("businessName"), "Local Business"), accent, f"{industry} in {location}", "Generated hero visual")]

    services_html = []
    services = site_content.get("services") or []
    for index, service in enumerate(services[:4]):
        title = html.escape(compact_text(service.get("title"), f"{industry} Service"))
        description = html.escape(compact_text(service.get("description"), "Practical support for local customers."))
        image = images[(index + 1) % len(images)]
        services_html.append(
            f"""
            <div class="col-md-6 col-xl-3">
              <article class="service-card card h-100">
                <img src="{image}" alt="{title}">
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
        <div class="col-lg-6">
          <div class="hero-image">
            <img src="{images[0]}" alt="{business_name}">
          </div>
        </div>
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
      if (window.gsap) {{
        gsap.from(".hero-content > *", {{ y: 18, opacity: 0, duration: 0.75, ease: "power2.out", stagger: 0.08 }});
        gsap.from(".hero-image", {{ y: 24, opacity: 0, duration: 0.8, ease: "power2.out", delay: 0.15 }});
        gsap.from(".service-card, .about-band .row, .contact-card", {{ y: 22, opacity: 0, duration: 0.7, ease: "power2.out", stagger: 0.08, delay: 0.25 }});
      }}
    }});
  </script>
</body>
</html>"""
    return ensure_required_site_features(site_html)


def github_headers() -> Dict[str, str]:
    token = require_env("GITHUB_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "AI-Site-Factory",
    }


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


def get_github_content_sha(owner: str, repo: str, path: str, branch: str, headers: Dict[str, str]) -> Optional[str]:
    response = requests.get(
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

    update_response = requests.put(
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
    for attempt in range(3):
        candidate = repo_name if attempt == 0 else f"{repo_name}-{str(uuid4())[:4]}"
        response = requests.post(
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
        if response.status_code == 422 and attempt < 2:
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

    if existing:
        repo_name = existing["repo_name"]
        repo_full_name = existing["repo_full_name"]
        repo_url = existing["repo_url"]
        branch = existing["default_branch"] or "main"
        repo_id = existing["repo_id"]
        private_repo = bool(existing["private"])
        export_action = "UPDATED"
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

    log_event(
        "info",
        "provider.github.export_start",
        "Exporting generated site files to GitHub repository.",
        repository=repo_full_name,
        branch=branch,
    )

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

    result = {
        "exportAction": export_action,
        "repository": repo_full_name,
        "repoName": repo_name,
        "repoId": repo_id,
        "repoUrl": repo_url,
        "private": private_repo,
        "branch": branch,
        "path": "index.html",
        "htmlChecksum": checksum,
        "indexContentSha": index_result.get("contentSha"),
        "readmeContentSha": readme_result.get("contentSha"),
        "commitSha": index_result.get("commitSha"),
        "htmlUrl": index_result.get("htmlUrl"),
        "readmeUrl": readme_result.get("htmlUrl"),
        "pipelineId": pipeline_id,
        "approvalId": approval_id,
        "createdAt": existing["created_at"] if existing else now_iso(),
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

    netlify_installation_id_raw = compact_text(os.getenv("NETLIFY_GITHUB_INSTALLATION_ID"))
    netlify_installation_id: Optional[int] = None
    if netlify_installation_id_raw:
        try:
            netlify_installation_id = int(netlify_installation_id_raw)
        except ValueError as error:
            raise RuntimeError(
                "NETLIFY_GITHUB_INSTALLATION_ID must be a numeric Netlify GitHub installation id."
            ) from error

    repo_settings = {
        "provider": "github",
        "repo_path": repo_full_name,
        "repo_branch": branch,
        "repo_url": f"https://github.com/{repo_full_name}.git",
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
        site_response = requests.patch(
            f"https://api.netlify.com/api/v1/sites/{site_id}",
            headers={**headers, "Content-Type": "application/json"},
            json={"repo": repo_settings, "build_settings": repo_settings},
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
                "build_settings": repo_settings,
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
    build_id = build.get("id")
    deploy_id = build.get("deploy_id")
    build_error = build.get("error")
    state = "building"

    poll_until = time.time() + int(os.getenv("NETLIFY_DEPLOY_POLL_SECONDS", "45"))
    while build_id and not build.get("done") and time.time() < poll_until:
        time.sleep(2)
        build_poll = requests.get(
            f"https://api.netlify.com/api/v1/builds/{build_id}",
            headers=headers,
            timeout=30,
        )
        build_poll.raise_for_status()
        build = build_poll.json()
        deploy_id = build.get("deploy_id") or deploy_id
        build_error = build.get("error") or build_error

    if build_error:
        raise RuntimeError(f"Netlify build failed: {build_error}")

    deploy = {}
    if deploy_id:
        state = "enqueued"
        while state not in {"ready", "error"} and time.time() < poll_until:
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
            raise RuntimeError(deploy.get("error_message") or "Netlify deploy failed.")

    site_url = (
        deploy.get("ssl_url")
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
                created_at, updated_at, last_deploy_id, last_deploy_state, deployment_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
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
                created_at, updated_at, last_deploy_id, last_deploy_state, deployment_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
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


def create_zendesk_intake_tickets(
    approval_id: str,
    context: Dict[str, Any],
    pipeline_id: str,
    batch_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    zendesk_subdomain = require_env("ZENDESK_SUBDOMAIN")
    zendesk_email = require_env("ZENDESK_EMAIL")
    zendesk_token = require_env("ZENDESK_API_TOKEN")
    auth = (f"{zendesk_email}/token", zendesk_token)
    base_url = f"https://{zendesk_subdomain}.zendesk.com/api/v2"
    headers = {"Content-Type": "application/json"}
    business_name = compact_text(context.get("businessName"), "AI Site Factory Lead")
    canonical_key = compact_text(context.get("canonicalLeadKey"))
    lead_email = normalize_email_identity(context.get("email"))
    lead_phone = compact_text(context.get("phone"))
    source_url = compact_text(context.get("sourceUrl"))
    source_label = compact_text(context.get("source") or context.get("sourceLabel"), "public business listing")
    industry = compact_text(context.get("industry") or context.get("category"), "Local service")
    location = compact_text(context.get("location"), "South Africa")

    channels = zendesk_channels_for_context(context)
    created: List[Dict[str, Any]] = []

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
                "tags": ["ai_site_factory", "ai_site_intake"],
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

    user: Dict[str, Any] = {}
    if lead_email:
        user_search = requests.get(
            f"{base_url}/users/search.json",
            params={"query": lead_email},
            auth=auth,
            headers=headers,
            timeout=30,
        )
        user_search.raise_for_status()
        users = user_search.json().get("users", [])
        if users:
            user = users[0]
        else:
            user_create = requests.post(
                f"{base_url}/users.json",
                json={
                    "user": {
                        "name": business_name,
                        "email": lead_email,
                        "organization_id": organization_id,
                        "role": "end-user",
                        "tags": ["ai_site_factory", "ai_site_email_contact"],
                    }
                },
                auth=auth,
                headers=headers,
                timeout=30,
            )
            user_create.raise_for_status()
            user = user_create.json().get("user", {})

    for channel in channels:
        existing = get_zendesk_ticket_link(approval_id, channel, "intake")
        if existing and existing.get("ticketId"):
            created.append(existing)
            continue

        contact_value = lead_email if channel == "email" else lead_phone if channel == "phone" else "No contact found"
        tags = [
            "ai_site_factory",
            "ai_site_intake",
            f"ai_site_{channel}_lead",
            "ai_site_generated_site",
        ]
        if channel == "email":
            tags.extend(["ai_site_email_approval_needed", "ai_site_can_deploy"])
        elif channel == "phone":
            tags.extend(["ai_site_phone_dialer", "ai_site_call_needed"])
        else:
            tags.append("ai_site_contact_unknown")

        custom_fields = zendesk_custom_fields(
            {
                "canonicalLeadKey": canonical_key,
                "pipelineId": pipeline_id,
                "approvalId": approval_id,
                "batchId": batch_id,
                "contactChannel": channel,
                "leadStatus": "GENERATED",
                "deployRequested": False,
                "emailSendRequested": False,
                "phoneCallStatus": "NEW" if channel == "phone" else None,
                "sourceUrl": source_url,
            }
        )
        ticket_payload: Dict[str, Any] = {
            "ticket": {
                "subject": f"AI Site Factory {channel.title()} Intake: {business_name}",
                "comment": {
                    "body": (
                        f"AI Site Factory intake ticket\n\n"
                        f"Business: {business_name}\n"
                        f"Channel: {channel}\n"
                        f"Contact: {contact_value}\n"
                        f"Industry: {industry}\n"
                        f"Location: {location}\n"
                        f"Pipeline ID: {pipeline_id}\n"
                        f"Approval ID: {approval_id}\n"
                        f"Canonical Lead Key: {canonical_key}\n"
                        f"Source: {source_label}{f' ({source_url})' if source_url else ''}\n\n"
                        "Webhook actions available: deploy generated site, send approved email, or update phone call status."
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
        if custom_fields:
            ticket_payload["ticket"]["custom_fields"] = custom_fields
        if channel == "email" and user.get("id"):
            ticket_payload["ticket"]["requester_id"] = user.get("id")

        response = requests.post(
            f"{base_url}/tickets.json",
            json=ticket_payload,
            auth=auth,
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        ticket = response.json().get("ticket", {})
        created.append(
            save_zendesk_ticket_link(
                approval_id=approval_id,
                canonical_key=canonical_key,
                pipeline_id=pipeline_id,
                channel=channel,
                stage="intake",
                ticket_id=ticket.get("id"),
                ticket_url=f"https://{zendesk_subdomain}.zendesk.com/agent/tickets/{ticket.get('id')}",
                status=ticket.get("status") or "new",
                tags=tags,
                payload={
                    "organizationId": organization_id,
                    "userId": user.get("id") if channel == "email" else None,
                    "contact": contact_value,
                    "customFields": custom_fields,
                    "createdAt": now_iso(),
                },
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
        "uptimeSeconds": int((datetime.now() - STARTED_AT).total_seconds()),
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

@app.post("/api/leads/discover", response_model=DiscoverLeadsResponse)
def discover_leads(request: DiscoverLeadsRequest):
    preset = get_preset_or_404(request.presetId)
    location = compact_text(request.location, "Durban, South Africa")
    limit = max(1, min(request.limit or 5, 200))
    apify_limit = min(max(limit * 5, 5), 500)
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
    raw_fetched = 0
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
        province_stats[location]["rawItems"] = raw_fetched

        normalized = normalize_apify_items(
            query_items,
            preset["industry"],
            location,
            fetch_limit,
        )
        province_stats[location]["normalized"] = len(normalized)

        qualified = normalized
        province_stats[location]["qualified"] = len(qualified)
        seen_batch_keys = set()
        seen_batch_identities: Set[str] = set()

        for lead in qualified:
            canonical_key = canonical_lead_key_for_lead(lead)
            lead.canonicalLeadKey = canonical_key
            lead.location = location
            identity_keys = [identity_key for _identity_type, identity_key in lead_identity_pairs(lead)]
            identity_conflicts = generated_lead_identity_conflicts(lead, canonical_key)

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
                or identity_conflicts
                or any(identity_key in seen_batch_identities for identity_key in identity_keys)
            ):
                duplicates_skipped += 1
                province_stats[location]["duplicatesSkipped"] += 1
                continue

            seen_batch_keys.add(canonical_key)
            seen_batch_identities.update(identity_keys)
            eligible_leads.append(lead)

        selected_leads = select_mixed_contact_leads(eligible_leads, limit)

    except Exception as error:
        warnings.append(f"Apify failed, demo fallback leads were used: {sanitize_message(error)}")

        demo_category = preset.get("industry", "Local Service")
        demo_businesses = [
            f"{location.split(',')[0]} {demo_category} Co",
            f"Reliable {demo_category} Durban",
            f"Quick Help {demo_category}",
        ]

        for index, business_name in enumerate(demo_businesses[:limit], start=1):
            lead = DiscoveredLead(
                leadKey=stable_lead_key(business_name, location, demo_category),
                canonicalLeadKey=stable_lead_key("demo", business_name, location, demo_category),
                businessName=business_name,
                email=f"demo{index}@example.com",
                phone="+27 31 000 0000",
                website=None,
                domain=None,
                category=demo_category,
                address=location,
                location=location,
                province=None,
                rating=None,
                reviewsCount=None,
                source="demo-fallback",
                sourceUrl=None,
                notes="Demo fallback lead used because the live Apify request failed or timed out.",
                raw={"fallback": True},
            )
            selected_leads.append(lead)
        raw_fetched = len(selected_leads)
        eligible_leads = selected_leads[:]

    for lead in selected_leads:
        try:
            upsert_lead_registry(lead)
        except Exception as error:
            warnings.append(f"Could not save lead {lead.businessName}: {sanitize_message(error)}")

    province_stats[location]["selected"] = len(selected_leads)
    province_stats[location]["eligible"] = len(eligible_leads)
    province_stats[location]["emailLeads"] = sum(1 for lead in selected_leads if normalize_email_identity(lead.email))
    province_stats[location]["phoneLeads"] = sum(1 for lead in selected_leads if compact_text(lead.phone))
    province_stats[location]["emailAndPhoneLeads"] = sum(
        1
        for lead in selected_leads
        if normalize_email_identity(lead.email) and compact_text(lead.phone)
    )

    if not selected_leads:
        warnings.append("No contactable no-website leads were returned. Try a broader phrase or nearby city.")
    elif len(selected_leads) < limit:
        warnings.append(
            f"Requested {limit} leads but only found {len(selected_leads)} contactable no-website leads. "
            f"Skipped {websites_skipped} with websites, {no_contact_skipped} without email/phone, "
            f"and {duplicates_skipped} already generated or duplicate leads."
        )

    batch_id = str(uuid4())

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
        generatedDuplicatesSkipped=duplicates_skipped,
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

@app.post("/api/pipeline/run", response_model=PipelineRunResponse)
def run_pipeline(request: PipelineRunRequest):
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
            pending_html = ensure_required_site_features(final_html_result["html"])
            site_content = {
                "promptHeader": LANDING_PAGE_PROMPT_HEADER,
                "groqBrief": groq_brief,
                "geminiQaNotes": final_html_result.get("qaNotes"),
                "structureNotes": final_html_result.get("structureNotes"),
                "stylingLibraries": final_html_result.get("stylingLibraries"),
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
            try:
                intake_tickets = run_step(
                    "zendesk_intake_tickets",
                    "zendesk",
                    lambda: create_zendesk_intake_tickets(
                        approval_id=approval_id,
                        context=cleaned_context or groq_brief,
                        pipeline_id=pipeline_id,
                        batch_id=request.sourceBatchId,
                    ),
                    retryable=True,
                )
                site_content["zendeskIntakeTickets"] = intake_tickets
                with get_pipeline_db() as db:
                    db.execute(
                        """
                        UPDATE approval_records
                        SET site_content_json = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (json.dumps(site_content, default=str), now_iso(), approval_id),
                    )
            except Exception as intake_error:
                pipeline_warnings.append(f"Zendesk intake ticket creation failed for {lead.businessName}: {sanitize_message(intake_error)}")
                structured_errors.append(structured_pipeline_error("zendesk_intake_tickets", intake_error, provider="zendesk", retryable=True))
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


def resolve_webhook_approval(request: ZendeskWebhookRequest) -> sqlite3.Row:
    if request.approvalId:
        return get_approval_or_404(request.approvalId)
    if request.zendeskTicketId:
        with get_pipeline_db() as db:
            link = db.execute(
                "SELECT approval_id FROM zendesk_ticket_links WHERE ticket_id = ? ORDER BY created_at DESC LIMIT 1",
                (request.zendeskTicketId,),
            ).fetchone()
        if link:
            return get_approval_or_404(link["approval_id"])
    if request.canonicalLeadKey:
        existing = latest_reusable_approval_for_lead(request.canonicalLeadKey)
        if existing:
            return existing
    raise HTTPException(status_code=404, detail="Could not resolve approval for Zendesk webhook.")


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

    try:
        if action in {"deploy", "deploy_site", "approve_deploy", "deploy_requested"}:
            if channel != "email":
                raise HTTPException(
                    status_code=409,
                    detail="Deploy webhook can only run for email-channel Zendesk tickets. Use phone_status for phone leads.",
                )
            if row["status"] in {"PENDING", "DEPLOY_FAILED"}:
                deploy_response = approve_generated_site(
                    approval_id,
                    ApprovalActionRequest(
                        approvedBy=compact_text(request.actor, "Zendesk Webhook"),
                        notes=request.notes or "Deployment approved from Zendesk webhook.",
                    ),
                )
                result["deployment"] = deploy_response.model_dump()
            else:
                result["deployment"] = approval_row_to_dict(row)
            if ticket_link:
                payload_update = {**ticket_link.get("payload", {}), "deployRequested": True, "deployWebhookAt": now_iso()}
                save_zendesk_ticket_link(
                    approval_id,
                    row["canonical_lead_key"],
                    row["pipeline_id"],
                    ticket_link["channel"],
                    ticket_link["stage"],
                    ticket_id,
                    ticket_link.get("ticketUrl"),
                    "deploy_requested",
                    ticket_link.get("tags", []),
                    payload_update,
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
            extra_fields: Dict[str, Any] = {"status": "open", "tags": ["ai_site_factory", "ai_site_email_sent", "ai_site_email_lead"]}
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
                    list(set(ticket_link.get("tags", []) + ["ai_site_email_sent"])),
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
                    list(set(ticket_link.get("tags", []) + ["ai_site_phone_updated"])),
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
        record_zendesk_webhook_event(action, "FAILED", payload, message=str(error.detail))
        raise
    except Exception as error:
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
    site_html = ensure_required_site_features(row["html"]) if row["html"] else None
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
        raise HTTPException(status_code=502, detail=f"Netlify deployment failed: {sanitize_message(error)}")

    try:
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
        final_html = ensure_required_site_features(final_html_result["html"])
        site_content = {
            "promptHeader": LANDING_PAGE_PROMPT_HEADER,
            "groqBrief": groq_brief,
            "geminiQaNotes": final_html_result.get("qaNotes"),
            "structureNotes": final_html_result.get("structureNotes"),
            "stylingLibraries": final_html_result.get("stylingLibraries"),
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
