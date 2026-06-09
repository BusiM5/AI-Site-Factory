# AI-Site-Factory-and-Outreach-Pipeline
This project is an end-to-end automation system that collects business data, cleans and structures it, and uses AI to generate high-quality website content and personalized outreach messages. The system streamlines the entire workflow from raw data ingestion to publish-ready content and communication.

## Tech stack
### Frontend
- React
- Tailwind CSS
- Vercel (Deployment)
- https://ai-site-factory-frontend.vercel.app/ 
### Backend
- Python(FastAPI)
- Render(Deployment)
- https://ai-site-factory-backend.onrender.com/docs
### Database
- PostgreSQL(/SQLite)

## AI Layer
- Google GeminiAPI(Vertex AI)

## Deployment
- Netlify

## Lead Pipeline API
- `GET /api/presets` returns the five Google Maps business examples.
- `GET /api/templates` returns the three landing-page templates.
- `POST /api/leads/discover` runs the Apify Google Maps actor and returns normalized leads.
- `POST /api/pipeline/run` enriches selected leads with Gemini, generates copy with GroqCloud, creates Gemini image assets, deploys production static sites to Netlify, and creates Zendesk outreach tickets.

## Environment
Create `backend/.env` from `backend/.env.example`. Use rotated API tokens before production because the original tokens were shared outside a secret manager.
## CRM Tracking
- Zendesk API

## Email Sending
-

## File Handling
- Pandas for CSV cleaning

## Hosting
- Render/Railway
- AWS later

  

  
