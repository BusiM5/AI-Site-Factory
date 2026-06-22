from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4
import base64
from collections import deque
import hashlib
import html
import json
import logging
import re
import sqlite3
import time
from urllib.parse import urlparse
import os
from dotenv import load_dotenv

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
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
    ],
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

REQUIRED_PROVIDER_ENV = {
    "apify": ["APIFY_API_TOKEN"],
    "gemini": ["GEMINI_API_KEY"],
    "groq": ["GROQ_API_KEY"],
    "netlify": ["NETLIFY_AUTH_TOKEN"],
    "zendesk": ["ZENDESK_SUBDOMAIN", "ZENDESK_EMAIL", "ZENDESK_API_TOKEN"],
    "github": ["GITHUB_OWNER", "GITHUB_TOKEN"],
}

VALID_PUBLISH_MODES = {"github-netlify"}

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
    cached: bool = False


class PipelineRunRequest(BaseModel):
    leads: List[DiscoveredLead]
    templateId: str = "default-service"
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
        return direct.lower()

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

    leads = [
        DiscoveredLead(**lead)
        for lead in safe_json_loads(row["leads_json"], [])[:limit]
    ]
    if not leads:
        return None

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


def approval_row_to_dict(row: sqlite3.Row, include_html: bool = False) -> Dict[str, Any]:
    context = safe_json_loads(row["context_json"], {})
    for key in ["ownerName", "ownerEmail", "ownerStatus"]:
        context.pop(key, None)
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
        "publishMode": row["publish_mode"] or "github-netlify",
        "githubExport": safe_json_loads(row["github_export_json"], None),
        "zendesk": safe_json_loads(row["zendesk_json"], None),
        "errors": safe_json_loads(row["errors_json"], []),
    }
    if include_html:
        approval["pendingPreviewHtml"] = row["html"]
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
    return item


def deployment_from_history(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    history = deployment_history_row_to_dict(row)
    if not history:
        return None
    raw = history.get("raw")
    if isinstance(raw, dict) and raw:
        raw.setdefault("deploymentHistoryId", history.get("id"))
        raw.setdefault("publishMode", history.get("publishMode"))
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

        website = normalize_url(first_present(item, ["website", "url", "site", "homepage"]))
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

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.35,
        },
    }

    for attempt in range(5):
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
                wait_time = 10 * (attempt + 1)
                log_event(
                    "warning",
                    "provider.gemini.rate_limited",
                    f"Gemini rate limited. Retrying in {wait_time}s.",
                    model=model_name,
                    attempt=attempt + 1,
                )
                time.sleep(wait_time)
                continue

            response.raise_for_status()

            data = response.json()
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            text = "".join(part.get("text", "") for part in parts)

            if not text:
                raise RuntimeError("Gemini returned an empty response.")

            return parse_json_response(text)

        except requests.RequestException as error:
            if attempt == 4:
                log_event(
                    "error",
                    "provider.gemini_text.error",
                    "Gemini text request failed after retries.",
                    model=model_name,
                    reason=error.__class__.__name__,
                )
                raise RuntimeError("Gemini request failed after retries.")

            time.sleep(5 * (attempt + 1))

    raise RuntimeError("Gemini request failed after retries.")

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


def generate_page_prompt_with_gemini(context: Dict[str, Any], template: Dict[str, Any]) -> Dict[str, Any]:
    prompt = (
        "Create a production-ready prompt for a single-file HTML landing page. "
        "Return strict JSON with keys: pagePrompt, designNotes, contentGuardrails, imageDirection. "
        "The pagePrompt must preserve: hero, four service cards, about section, contact section, footer. "
        "The page must use Bootstrap 5 from a CDN for responsive layout/components and GSAP from a CDN for entry animations. "
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
            "contact, and footer. Use Bootstrap 5 CDN assets, GSAP CDN animations, accessible semantic HTML, polished CSS, and grounded claims only."
        ),
    )
    result.setdefault("designNotes", f"Use accent {template.get('accent')} and background {template.get('background')}.")
    result.setdefault("contentGuardrails", "Use only the provided public lead context.")
    result.setdefault("imageDirection", "Use tasteful CSS gradients or placeholder imagery.")
    return result


def generate_draft_html_with_groq(
    context: Dict[str, Any],
    template: Dict[str, Any],
    page_prompt: Dict[str, Any],
) -> Dict[str, Any]:
    prompt = (
        "Generate a complete standalone responsive HTML document for a small-business landing page. "
        "Return strict JSON with keys: html, notes. The html value must include <!doctype html>, <html>, <head>, CSS, and <body>. "
        "Preserve this original structure exactly: hero, four service cards, about section, contact section, footer. "
        "Use Bootstrap 5 CDN CSS/JS and GSAP CDN for modern responsive layout and tasteful entry animations. "
        "No markdown fences. Do not invent private information, guarantees, prices, awards, or unsupported services.\n\n"
        f"Gemini page prompt: {model_safe_json(page_prompt)}\n"
        f"Template: {model_safe_json(template)}\n"
        f"Lead context: {model_safe_json(context)}"
    )

    result = groq_chat_json(
        prompt,
        "You generate deployable single-file HTML for ethical small-business website previews. Return valid JSON only.",
    )
    html_value = compact_text(result.get("html"))
    if not html_value:
        raise RuntimeError("Groq did not return HTML.")
    result["html"] = html_value
    result.setdefault("notes", "Groq draft HTML generated.")
    return result


def finalize_html_with_gemini(
    context: Dict[str, Any],
    template: Dict[str, Any],
    page_prompt: Dict[str, Any],
    draft_html: str,
) -> Dict[str, Any]:
    prompt = (
        "Finalize this single-file HTML website for deployment. Return strict JSON with keys: html, qaNotes. "
        "Keep the structure: hero, four service cards, about, contact, footer. "
        "Ensure Bootstrap 5 CDN assets and GSAP CDN animations are present. Fix malformed HTML/CSS and keep claims grounded.\n\n"
        f"Template: {model_safe_json(template)}\n"
        f"Lead context: {model_safe_json(context)}\n"
        f"Page prompt: {model_safe_json(page_prompt)}\n"
        f"Draft HTML: {draft_html[:MODEL_CHUNK_CHARS * MODEL_MAX_CHUNKS]}"
    )

    try:
        result = gemini_text_json(prompt)
        html_value = compact_text(result.get("html"))

        if html_value and "<html" in html_value.lower() and "</html>" in html_value.lower():
            result["html"] = html_value
            result.setdefault("qaNotes", "Gemini finalized the HTML.")
            return result

    except Exception as error:
        log_event(
            "warning",
            "provider.gemini_final_html.fallback",
            "Gemini final HTML failed. Using Groq draft HTML.",
            reason=str(error),
        )

    return {
        "html": draft_html,
        "qaNotes": "Gemini finalization skipped. Groq draft HTML used as deployable fallback.",
    }


def ensure_bootstrap_gsap_assets(site_html: str) -> str:
    html_value = site_html
    lower_html = html_value.lower()

    bootstrap_css = (
        '<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" '
        'rel="stylesheet" '
        'integrity="sha384-QWTKZyjpPEjISv5WaRU9Oer+R4m9GdeM0IhJ7rZbXvWgW8G0gWZb8gWZQvG2iU6q" crossorigin="anonymous">'
    )
    bootstrap_js = (
        '<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js" '
        'integrity="sha384-YvpcrYf0tY3lHB60NNkmXc5s9fDVZLESaAA55NDzOxhy9GkcIdslK1eN7N6jIeHz" crossorigin="anonymous"></script>'
    )
    gsap_js = '<script src="https://cdn.jsdelivr.net/npm/gsap@3.12.5/dist/gsap.min.js"></script>'
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

    if "bootstrap@5" not in lower_html and "bootstrap.min.css" not in lower_html:
        if "</head>" in lower_html:
            html_value = re.sub(r"</head>", f"  {bootstrap_css}\n</head>", html_value, count=1, flags=re.IGNORECASE)
        else:
            html_value = f"{bootstrap_css}\n{html_value}"

    lower_html = html_value.lower()
    scripts = []
    if "bootstrap.bundle.min.js" not in lower_html and "bootstrap.min.js" not in lower_html:
        scripts.append(bootstrap_js)
    if "gsap" not in lower_html:
        scripts.append(gsap_js)
    if "gsap.from" not in lower_html:
        scripts.append(animation_js)

    if scripts:
        injection = "\n".join(scripts)
        if "</body>" in lower_html:
            html_value = re.sub(r"</body>", f"{injection}\n</body>", html_value, count=1, flags=re.IGNORECASE)
        else:
            html_value = f"{html_value}\n{injection}"

    return html_value


def generate_outreach_with_groq(context: Dict[str, Any], site_url: str) -> Dict[str, Any]:
    prompt = (
        "Create a concise outreach email draft for a business owner. Return strict JSON with keys subject and body. "
        "Mention that the business was found through public Google Maps/business listing research, include the live "
        "preview website URL, and position the offer as web design and marketing support. Keep it professional and "
        "do not imply an existing relationship.\n\n"
        f"Live site URL: {site_url}\n"
        f"Lead context: {model_safe_json(context)}"
    )

    outreach = groq_chat_json(
        prompt,
        "You write ethical B2B outreach drafts. Return valid JSON only.",
    )
    outreach.setdefault("subject", f"Website preview for {context.get('businessName')}")
    outreach.setdefault(
        "body",
        (
            f"Hi {context.get('businessName')} Team,\n\n"
            f"We found your business through public Google Maps research and created a website preview here: {site_url}\n\n"
            "We help local businesses with web design and marketing support.\n\n"
            "Kind regards,\nAI Site Factory Team"
        ),
    )
    outreach["recipientEmail"] = context.get("email")
    outreach["siteUrl"] = site_url
    return outreach


def fallback_image_data_uri(label: str, accent: str) -> str:
    safe_label = html.escape(compact_text(label, "Business"))
    svg = (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='1200' height='800' viewBox='0 0 1200 800'>"
        f"<rect width='1200' height='800' fill='#f8fafc'/>"
        f"<rect x='80' y='80' width='1040' height='640' rx='36' fill='{accent}' opacity='0.12'/>"
        f"<circle cx='920' cy='230' r='150' fill='{accent}' opacity='0.18'/>"
        f"<path d='M160 560 C320 430 460 630 650 490 C790 390 910 430 1040 300' "
        f"fill='none' stroke='{accent}' stroke-width='28' stroke-linecap='round' opacity='0.52'/>"
        f"<text x='120' y='190' font-family='Arial, sans-serif' font-size='54' font-weight='700' fill='#111827'>{safe_label}</text>"
        f"</svg>"
    )
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def generate_gemini_images(prompts: List[str], accent: str) -> List[str]:
    if os.getenv("ENABLE_GEMINI_IMAGES", "false").lower() != "true":
        return [
            fallback_image_data_uri("Hero image", accent),
            fallback_image_data_uri("Service 1", accent),
            fallback_image_data_uri("Service 2", accent),
            fallback_image_data_uri("Service 3", accent),
            fallback_image_data_uri("Service 4", accent),
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

    services_html = []
    services = site_content.get("services") or []
    for index, service in enumerate(services[:4]):
        title = html.escape(compact_text(service.get("title"), f"{industry} Service"))
        description = html.escape(compact_text(service.get("description"), "Practical support for local customers."))
        image = images[(index + 1) % len(images)]
        services_html.append(
            f"""
            <article class="service-card">
              <img src="{image}" alt="">
              <span>0{index + 1}</span>
              <h3>{title}</h3>
              <p>{description}</p>
            </article>
            """
        )

    contact_links = []
    if email:
        contact_links.append(f"<a href=\"mailto:{html.escape(email)}\">{html.escape(email)}</a>")
    if phone:
        contact_links.append(f"<a href=\"tel:{html.escape(phone)}\">{html.escape(phone)}</a>")
    if website:
        contact_links.append(f"<a href=\"{html.escape(website)}\" target=\"_blank\" rel=\"noreferrer\">Website</a>")
    contact_html = " ".join(contact_links) or "<span>Contact details available on request</span>"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{business_name}</title>
  <meta name="description" content="{subheadline}">
  <style>
    :root {{
      --accent: {accent};
      --background: {background};
      --ink: #111827;
      --muted: #5b6472;
      --line: #d9dee7;
      --surface: #ffffff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Arial, Helvetica, sans-serif;
      color: var(--ink);
      background: var(--background);
      line-height: 1.55;
    }}
    a {{ color: inherit; }}
    header {{
      min-height: 78vh;
      display: grid;
      grid-template-columns: minmax(0, 1.05fr) minmax(320px, 0.95fr);
      align-items: center;
      gap: 44px;
      padding: clamp(28px, 5vw, 72px);
    }}
    .eyebrow {{
      color: var(--accent);
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0;
      font-size: 0.82rem;
    }}
    h1 {{
      margin: 14px 0 18px;
      font-size: clamp(2.3rem, 5vw, 5.4rem);
      line-height: 0.96;
      letter-spacing: 0;
    }}
    .hero-copy p {{
      max-width: 680px;
      font-size: clamp(1.02rem, 1.6vw, 1.32rem);
      color: var(--muted);
    }}
    .hero-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-top: 28px;
    }}
    .button {{
      display: inline-flex;
      min-height: 48px;
      align-items: center;
      justify-content: center;
      padding: 0 18px;
      border: 1px solid var(--accent);
      border-radius: 8px;
      background: var(--accent);
      color: #fff;
      text-decoration: none;
      font-weight: 700;
    }}
    .button.secondary {{
      background: transparent;
      color: var(--accent);
    }}
    .hero-image {{
      min-height: 420px;
      border-radius: 8px;
      overflow: hidden;
      box-shadow: 0 24px 70px rgba(17, 24, 39, 0.16);
      animation: lift 0.7s ease both;
    }}
    .hero-image img {{
      width: 100%;
      height: 100%;
      min-height: 420px;
      object-fit: cover;
      display: block;
    }}
    main {{ padding-bottom: 48px; }}
    section {{
      padding: 56px clamp(20px, 5vw, 72px);
    }}
    .section-head {{
      max-width: 760px;
      margin-bottom: 26px;
    }}
    .section-head h2 {{
      font-size: clamp(1.8rem, 3vw, 3rem);
      margin: 0 0 10px;
      letter-spacing: 0;
    }}
    .services {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 18px;
    }}
    .service-card {{
      min-height: 100%;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      animation: rise 0.55s ease both;
    }}
    .service-card img {{
      width: 100%;
      aspect-ratio: 4 / 3;
      object-fit: cover;
      display: block;
    }}
    .service-card span {{
      display: block;
      color: var(--accent);
      font-weight: 700;
      padding: 18px 18px 0;
    }}
    .service-card h3 {{
      margin: 8px 18px;
      font-size: 1.08rem;
    }}
    .service-card p {{
      margin: 0;
      padding: 0 18px 20px;
      color: var(--muted);
    }}
    .about-band {{
      background: #fff;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
    }}
    .about-grid {{
      display: grid;
      grid-template-columns: 0.8fr 1.2fr;
      gap: 36px;
      align-items: start;
    }}
    .facts {{
      display: grid;
      gap: 10px;
    }}
    .fact {{
      padding: 14px 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--background);
      font-weight: 700;
    }}
    .contact {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 24px;
      background: var(--ink);
      color: #fff;
      border-radius: 8px;
      margin: 0 clamp(20px, 5vw, 72px) 48px;
      padding: clamp(24px, 4vw, 48px);
    }}
    .contact p {{ color: #d1d5db; }}
    .contact-links {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .contact-links a,
    .contact-links span {{
      border: 1px solid rgba(255, 255, 255, 0.22);
      border-radius: 8px;
      padding: 10px 12px;
      text-decoration: none;
    }}
    footer {{
      padding: 28px clamp(20px, 5vw, 72px);
      color: var(--muted);
      border-top: 1px solid var(--line);
    }}
    @keyframes lift {{
      from {{ opacity: 0; transform: translateY(18px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    @keyframes rise {{
      from {{ opacity: 0; transform: translateY(10px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    @media (max-width: 980px) {{
      header,
      .about-grid {{
        grid-template-columns: 1fr;
      }}
      .services {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      .contact {{
        align-items: flex-start;
        flex-direction: column;
      }}
    }}
    @media (max-width: 620px) {{
      header {{
        min-height: auto;
      }}
      .hero-image,
      .hero-image img {{
        min-height: 280px;
      }}
      .services {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="hero-copy">
      <div class="eyebrow">{industry} in {location}</div>
      <h1>{headline}</h1>
      <p>{subheadline}</p>
      <div class="hero-actions">
        <a class="button" href="#contact">{cta_label}</a>
        <a class="button secondary" href="#services">View services</a>
      </div>
    </div>
    <div class="hero-image">
      <img src="{images[0]}" alt="">
    </div>
  </header>
  <main>
    <section id="services">
      <div class="section-head">
        <h2>Services</h2>
        <p>{business_name} presents a practical, customer-focused service experience for local customers.</p>
      </div>
      <div class="services">
        {''.join(services_html)}
      </div>
    </section>
    <section class="about-band">
      <div class="about-grid">
        <div class="section-head">
          <h2>About {business_name}</h2>
        </div>
        <div>
          <p>{about}</p>
          <div class="facts">
            <div class="fact">{industry}</div>
            <div class="fact">{location}</div>
            <div class="fact">Built from public business information</div>
          </div>
        </div>
      </div>
    </section>
    <section id="contact" class="contact">
      <div>
        <h2>{cta_label}</h2>
        <p>{contact_intro}</p>
      </div>
      <div class="contact-links">{contact_html}</div>
    </section>
  </main>
  <footer>{footer_text}</footer>
</body>
</html>"""


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
            "githubExport": github_export,
            "githubRepoUrl": repo_url,
            "githubRepoFullName": repo_full_name,
            "commitSha": commit_sha,
        }

    repo_settings = {
        "provider": "github",
        "repo_path": repo_full_name,
        "repo_branch": branch,
        "repo_url": repo_url,
        "dir": "",
        "cmd": "",
        "public_repo": not bool(github_export.get("private")),
    }
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
        site_response.raise_for_status()
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
        site_response.raise_for_status()
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
    lead_email = compact_text(context.get("email"))
    if not lead_email:
        domain = domain_from_url(context.get("website")) or "example.com"
        lead_email = f"info@{domain}"
    log_event("info", "provider.zendesk.start", "Creating Zendesk outreach ticket.", businessName=business_name, pipelineId=pipeline_id, email=lead_email)

    org_response = requests.post(
        f"{base_url}/organizations.json",
        json={
            "organization": {
                "name": business_name,
                "notes": (
                    f"Created from AI Site Factory pipeline.\n"
                    f"Pipeline ID: {pipeline_id}\n"
                    f"Industry: {context.get('industry')}\n"
                    f"Website: {deployment.get('url')}"
                ),
                "tags": ["ai_site_factory", "pipeline_lead"],
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
                    "tags": ["ai_site_factory", "lead_contact"],
                }
            },
            auth=auth,
            headers=headers,
            timeout=30,
        )
        user_create_response.raise_for_status()
        user = user_create_response.json().get("user", {})

    ticket_response = requests.post(
        f"{base_url}/tickets.json",
        json={
            "ticket": {
                "subject": outreach.get("subject") or f"Website preview for {business_name}",
                "comment": {
                    "body": (
                        f"AI Site Factory pipeline result\n\n"
                        f"Pipeline ID: {pipeline_id}\n"
                        f"Business: {business_name}\n"
                        f"Industry: {context.get('industry')}\n"
                        f"Location: {context.get('location')}\n"
                        f"Live Netlify URL: {deployment.get('url')}\n\n"
                        f"Outreach Draft:\n{outreach.get('body')}"
                    ),
                    "public": False,
                },
                "requester_id": user.get("id"),
                "organization_id": organization_id,
                "priority": "normal",
                "type": "task",
                "tags": [
                    "ai_site_factory",
                    "outreach_draft",
                    "netlify_production_site",
                ],
            }
        },
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
        "syncedAt": datetime.now().isoformat(),
    }
    log_event("info", "provider.zendesk.finish", "Zendesk ticket created.", businessName=business_name, ticketId=result["ticketId"])
    return result


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
        f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}?key={api_key}",
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
    return {"templates": SITE_TEMPLATES}

@app.post("/api/leads/discover", response_model=DiscoverLeadsResponse)
def discover_leads(request: DiscoverLeadsRequest):
    preset = get_preset_or_404(request.presetId)
    location = compact_text(request.location, "Durban, South Africa")
    limit = max(1, min(request.limit or 3, 5))
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
    selected_leads: List[DiscoveredLead] = []

    province_stats: Dict[str, Any] = {
        location: {
            "rawItems": 0,
            "normalized": 0,
            "selected": 0,
            "duplicatesSkipped": 0,
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
    )

    try:
        query_items = run_apify_google_maps(primary_query, limit, location)
        province_stats[location]["rawItems"] = len(query_items)

        normalized = normalize_apify_items(
            query_items,
            preset["industry"],
            location,
            limit,
        )
        province_stats[location]["normalized"] = len(normalized)

        candidate_keys = [canonical_lead_key_for_lead(lead) for lead in normalized]
        existing_keys = existing_canonical_lead_keys(candidate_keys)
        seen_batch_keys = set()

        for lead in normalized:
            canonical_key = canonical_lead_key_for_lead(lead)
            lead.canonicalLeadKey = canonical_key
            lead.location = location

            if canonical_key in existing_keys or canonical_key in seen_batch_keys:
                duplicates_skipped += 1
                province_stats[location]["duplicatesSkipped"] += 1
                continue

            seen_batch_keys.add(canonical_key)
            selected_leads.append(lead)

            if len(selected_leads) >= limit:
                break

    except Exception as error:
        warnings.append(f"Apify failed, demo fallback leads were used: {sanitize_message(error)}")

        demo_category = preset.get("industry", "Local Service")
        demo_businesses = [
            f"{location.split(',')[0]} {demo_category} Co",
            f"Reliable {demo_category} Durban",
            f"Quick Help {demo_category}",
        ]

        for business_name in demo_businesses[:limit]:
            lead = DiscoveredLead(
                leadKey=stable_lead_key(business_name, location, demo_category),
                canonicalLeadKey=stable_lead_key("demo", business_name, location, demo_category),
                businessName=business_name,
                email="info@example.com",
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

    for lead in selected_leads:
        try:
            upsert_lead_registry(lead)
        except Exception as error:
            warnings.append(f"Could not save lead {lead.businessName}: {sanitize_message(error)}")

    province_stats[location]["selected"] = len(selected_leads)

    if not selected_leads:
        warnings.append("No leads were returned. Try a smaller city search such as Durban, South Africa.")

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
    template = get_template_or_404(request.templateId)
    if not request.leads:
        raise HTTPException(status_code=400, detail="Select at least one lead.")

    pipeline_id = str(uuid4())
    results: List[PipelineLeadResult] = []
    pipeline_warnings: List[str] = []
    created_at = now_iso()
    save_pipeline_run(
        pipeline_id=pipeline_id,
        status="PROCESSING",
        template_id=request.templateId,
        source_batch_id=request.sourceBatchId,
        lead_count=len(request.leads),
        completed_count=0,
        pending_count=0,
        failed_count=0,
        warnings=pipeline_warnings,
        created_at=created_at,
    )
    log_event("info", "pipeline.start", "Pipeline run started.", pipelineId=pipeline_id, templateId=request.templateId, leadCount=len(request.leads))

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
        status = "PROCESSING"
        current_step = "start"
        approval_status = None

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
            cleaned_context = {
                "canonicalLeadKey": canonical_key,
                "businessName": lead.businessName,
                "industry": lead.category,
                "location": lead.location,
                "province": lead.province,
                "email": contact_details.get("email") or lead.email,
                "phone": contact_details.get("phone") or lead.phone,
                "website": contact_details.get("website") or lead.website,
                "summary": contact_details.get("notes") or lead.notes or f"{lead.businessName} is a local {lead.category} business.",
                "targetCustomers": "Local customers",
                "differentiators": [],
                "serviceKeywords": [lead.category],
                "sourceNote": "Public Google Maps, business listing, and website context.",
            }
            log_event("info", "pipeline.lead.context_ready", "Lead context prepared.", pipelineId=pipeline_id, leadKey=lead.leadKey)

            page_prompt = run_step(
                "gemini_page_prompt",
                "gemini",
                lambda: generate_page_prompt_with_gemini(cleaned_context, template),
                retryable=True,
            )
            groq_draft = run_step(
                "groq_draft_html",
                "groq",
                lambda: generate_draft_html_with_groq(cleaned_context, template, page_prompt),
                retryable=True,
            )
            final_html_result = run_step(
                "gemini_final_html",
                "gemini",
                lambda: finalize_html_with_gemini(cleaned_context, template, page_prompt, groq_draft["html"]),
                retryable=True,
            )
            pending_html = ensure_bootstrap_gsap_assets(final_html_result["html"])
            site_content = {
                "pagePrompt": page_prompt,
                "groqDraftNotes": groq_draft.get("notes"),
                "groqDraftHtmlChecksum": html_checksum(groq_draft["html"]),
                "geminiQaNotes": final_html_result.get("qaNotes"),
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
    failed_count = sum(1 for result in results if result.status in {"FAILED", "EXPORT_FAILED"})
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
        templateId=request.templateId,
        createdAt=created_at,
        results=results,
        warnings=pipeline_warnings,
    )
    PIPELINE_DB[pipeline_id] = response.model_dump()
    save_pipeline_run(
        pipeline_id=pipeline_id,
        status=response_status,
        template_id=request.templateId,
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
    if row["status"] != "PENDING":
        raise HTTPException(status_code=409, detail=f"Approval is {row['status']}, not PENDING.")

    context = safe_json_loads(row["context_json"], {})
    for key in ["ownerName", "ownerEmail", "ownerStatus"]:
        context.pop(key, None)
    site_html = row["html"]
    approved_by = compact_text(request.approvedBy, "Dashboard Operator")
    errors: List[Dict[str, Any]] = []
    deployment: Optional[Dict[str, Any]] = None
    deployment_history: Optional[Dict[str, Any]] = None
    outreach: Optional[Dict[str, Any]] = None
    zendesk: Optional[Dict[str, Any]] = None
    github_export: Optional[Dict[str, Any]] = safe_json_loads(row["github_export_json"], None)
    publish_mode = normalize_publish_mode(request.publishMode)
    status = "APPROVED"

    if not site_html:
        raise HTTPException(status_code=409, detail="Approval record does not contain generated HTML.")
    if not github_export or not github_export.get("repository") or not github_export.get("commitSha"):
        raise HTTPException(status_code=409, detail="Approval does not have a successful GitHub export. Retry export before approving.")

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
        deployment = run_approval_step(
            "netlify_deploy",
            "netlify",
            lambda: deploy_github_repo_to_netlify_for_lead(
                canonical_key=row["canonical_lead_key"],
                business_name=row["business_name"],
                pipeline_id=row["pipeline_id"],
                approval_id=approval_id,
                approved_by=approved_by,
                regenerate_existing_site=request.regenerateExistingSite,
                github_export=github_export,
            ),
        )
        deployment_history = deployment_history_row_to_dict(get_deployment_history_row(deployment.get("deploymentHistoryId")))
    except Exception as error:
        status = "DEPLOY_FAILED"
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
                    publish_mode,
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
                None if deployment and deployment.get("state") == "ready" else site_html,
                deployment.get("deploymentHistoryId") if deployment else None,
                json.dumps(outreach, default=str) if outreach else None,
                json.dumps(zendesk, default=str) if zendesk else None,
                publish_mode,
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
        publishMode=publish_mode,
        githubExport=github_export,
        errors=errors,
    )


@app.post("/api/approvals/{approval_id}/reject", response_model=ApprovalActionResponse)
def reject_generated_site(approval_id: str, request: ApprovalActionRequest):
    row = get_approval_or_404(approval_id)
    if row["status"] != "PENDING":
        raise HTTPException(status_code=409, detail=f"Approval is {row['status']}, not PENDING.")

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
    template = safe_json_loads(row["template_json"], {})
    requested_by = compact_text(request.requestedBy, "Dashboard Operator")
    pipeline_id = str(uuid4())
    created_at = now_iso()
    step_errors: List[Dict[str, Any]] = []

    save_pipeline_run(
        pipeline_id=pipeline_id,
        status="PROCESSING",
        template_id=template.get("id", "default-service"),
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
        page_prompt = run_regenerate_step(
            "gemini_page_prompt",
            "gemini",
            lambda: generate_page_prompt_with_gemini(context, template),
        )
        groq_draft = run_regenerate_step(
            "groq_draft_html",
            "groq",
            lambda: generate_draft_html_with_groq(context, template, page_prompt),
        )
        final_html_result = run_regenerate_step(
            "gemini_final_html",
            "gemini",
            lambda: finalize_html_with_gemini(context, template, page_prompt, groq_draft["html"]),
        )
        final_html = ensure_bootstrap_gsap_assets(final_html_result["html"])
        site_content = {
            "pagePrompt": page_prompt,
            "groqDraftNotes": groq_draft.get("notes"),
            "groqDraftHtmlChecksum": html_checksum(groq_draft["html"]),
            "geminiQaNotes": final_html_result.get("qaNotes"),
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
                template_id=template.get("id", "default-service"),
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
            template_id=template.get("id", "default-service"),
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
            template_id=template.get("id", "default-service"),
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
