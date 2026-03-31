# Webhook Placeholders for Twilio, SendGrid, and DocuSign

from fastapi import APIRouter, Request

router = APIRouter()

@router.post("/twilio")
async def twilio_webhook(request: Request):
    return {"status": "received"}

@router.post("/sendgrid")
async def sendgrid_webhook(request: Request):
    return {"status": "received"}

@router.post("/docusign")
async def docusign_webhook(request: Request):
    return {"status": "received"}
