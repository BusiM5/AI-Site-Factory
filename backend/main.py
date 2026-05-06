from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr


app = FastAPI(title="AI Site Factory Backend")


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://ai-site-factory-frontend.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class LeadInput(BaseModel):
    businessName: str
    email: EmailStr
    category: str
    location: Optional[str] = "Not provided"
    notes: Optional[str] = "No additional notes provided."


class CleanedLead(BaseModel):
    businessName: str
    email: EmailStr
    category: str
    location: str
    cleanSummary: str
    status: str
    readyForAI: str


class ServiceBlock(BaseModel):
    title: str
    description: str


class ContentPacket(BaseModel):
    headline: str
    summary: str
    services: List[ServiceBlock]
    cta: str
    tone: str
    brandNotes: str
    generatedAt: str


@app.get("/")
def health_check():
    return {
        "message": "AI Site Factory Backend is running",
        "status": "online",
    }


@app.post("/api/leads/clean", response_model=CleanedLead)
def clean_lead(lead: LeadInput):
    return CleanedLead(
        businessName=lead.businessName.strip(),
        email=lead.email.lower(),
        category=lead.category.strip(),
        location=lead.location.strip() if lead.location else "Not provided",
        cleanSummary=lead.notes.strip()
        if lead.notes
        else "No additional notes provided.",
        status="CLEAN",
        readyForAI="YES",
    )


@app.post("/api/content/generate", response_model=ContentPacket)
def generate_content(cleaned_lead: CleanedLead):
    return ContentPacket(
        headline=f"{cleaned_lead.businessName} - {cleaned_lead.category} Services in {cleaned_lead.location}",
        summary=(
            f"{cleaned_lead.businessName} provides reliable "
            f"{cleaned_lead.category.lower()} services in {cleaned_lead.location}. "
            f"{cleaned_lead.cleanSummary}"
        ),
        services=[
            ServiceBlock(
                title=f"Professional {cleaned_lead.category} Support",
                description=f"Reliable {cleaned_lead.category.lower()} support tailored to customer needs.",
            ),
            ServiceBlock(
                title="Customer-Focused Service",
                description="Clear communication, practical assistance, and dependable service delivery.",
            ),
            ServiceBlock(
                title="Local Business Support",
                description=f"Serving customers in and around {cleaned_lead.location}.",
            ),
        ],
        cta=f"Contact {cleaned_lead.businessName} today to learn more.",
        tone="Professional and clear",
        brandNotes="Generated from cleaned lead data. No unsupported claims added.",
        generatedAt=datetime.now().isoformat(),
    )