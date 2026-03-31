"""
cortexbot/agents/email_parser.py

Agent — Email Classification & Parsing

Classifies and parses inbound emails. Handles:
  - Rate Confirmation emails (with PDF attachments)
  - Payment remittance emails
  - Carrier setup packets
  - Dispute / shortage claims
  - Compliance requests

Uses fast rule-based matching first, then LLM fallback for ambiguous
emails. Also extracts thread-association metadata (load_id, TMS ref,
broker load reference) so the webhook router can link to the right load.
"""

import logging
import re
from typing import Optional

from cortexbot.config import settings

logger = logging.getLogger("cortexbot.agents.email_parser")

# ─────────────────────────────────────────────────────────────
# Signal word sets (all lowercase)
# ─────────────────────────────────────────────────────────────
RC_SIGNALS = {
    "rate confirmation", "rate con", "rc#", "load confirmation",
    "rate confirm", "confirmation of rate", "carrier rate", "freight rate"
}
PAYMENT_SIGNALS = {
    "payment", "remittance", "ach deposit", "wire transfer",
    "check enclosed", "invoice paid", "payment confirmation",
    "payment advice", "funds transferred"
}
PACKET_SIGNALS = {
    "carrier packet", "carrier setup", "setup packet",
    "new carrier", "carrier info", "carrier onboarding",
    "w-9 request", "insurance request", "certificate of insurance"
}
DISPUTE_SIGNALS = {
    "dispute", "short pay", "claim", "shortage", "damaged",
    "missing freight", "cargo claim", "freight claim", "loss"
}
COMPLIANCE_SIGNALS = {
    "compliance", "authority", "insurance expiry", "expired insurance",
    "dot audit", "safety rating", "fmcsa", "operating authority"
}

# Regex patterns for common identifiers in email text
_LOAD_REF_PATTERNS = [
    r"(?:load|ref|reference|order|po)\s*#?\s*:?\s*([A-Z0-9\-]{3,20})",
    r"#\s*([A-Z0-9\-]{4,15})\b",
    r"\bTMS\-\d{4}\-\d{3,6}\b",
]
_TMS_REF_PATTERN = re.compile(r"TMS-\d{4}-\d{3,6}", re.IGNORECASE)
_EMAIL_PATTERN   = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_DOLLAR_PATTERN  = re.compile(r"\$\s*[\d,]+\.?\d{0,2}")
_MC_PATTERN      = re.compile(r"\bMC[\s\-#]?(\d{5,7})\b", re.IGNORECASE)


class EmailParserAgent:
    """Classifies and extracts structured info from inbound freight emails."""

    async def classify_email(
        self,
        from_email: str,
        subject: str,
        body: str = "",
        attachments: Optional[list] = None,
    ) -> dict:
        """
        Classify an inbound email into a category and extract metadata.

        Returns:
            {
                "category":      "RC" | "CARRIER_PACKET" | "PAYMENT" | "DISPUTE" | "COMPLIANCE" | "OTHER",
                "confidence":    0.0–1.0,
                "extracted":     {load_reference, tms_ref, broker_mc, amount, ...},
                "has_pdf":       bool,
                "action_needed": "ATTACH_TO_LOAD" | "LOG_PAYMENT" | "FLAG_COMPLIANCE" | "ESCALATE" | "IGNORE",
            }
        """
        attachments = attachments or []
        subject_lower = (subject or "").lower()
        body_lower    = (body or "").lower()
        combined      = f"{subject_lower} {body_lower[:400]}"

        has_pdf = any(
            str(a.get("filename", "")).lower().endswith(".pdf")
            for a in attachments
        )

        # ── Fast rule-based classification ─────────────────────
        if any(s in combined for s in RC_SIGNALS):
            extracted = self._extract_identifiers(subject, body)
            return {
                "category":      "RC",
                "confidence":    0.95,
                "extracted":     extracted,
                "has_pdf":       has_pdf,
                "action_needed": "ATTACH_TO_LOAD",
            }

        if any(s in combined for s in PAYMENT_SIGNALS):
            extracted = self._extract_payment_info(subject, body)
            return {
                "category":      "PAYMENT",
                "confidence":    0.90,
                "extracted":     extracted,
                "has_pdf":       has_pdf,
                "action_needed": "LOG_PAYMENT",
            }

        if any(s in combined for s in PACKET_SIGNALS):
            return {
                "category":      "CARRIER_PACKET",
                "confidence":    0.90,
                "extracted":     {"from_email": from_email},
                "has_pdf":       has_pdf,
                "action_needed": "ESCALATE",
            }

        if any(s in combined for s in DISPUTE_SIGNALS):
            extracted = self._extract_identifiers(subject, body)
            return {
                "category":      "DISPUTE",
                "confidence":    0.85,
                "extracted":     extracted,
                "has_pdf":       has_pdf,
                "action_needed": "ESCALATE",
            }

        if any(s in combined for s in COMPLIANCE_SIGNALS):
            return {
                "category":      "COMPLIANCE",
                "confidence":    0.80,
                "extracted":     {},
                "has_pdf":       has_pdf,
                "action_needed": "FLAG_COMPLIANCE",
            }

        # ── LLM fallback for ambiguous emails ──────────────────
        try:
            result = await self._llm_classify(from_email, subject, body[:500])
            result["has_pdf"] = has_pdf
            # Ensure action_needed is always set
            if "action_needed" not in result:
                result["action_needed"] = _category_to_action(result.get("category", "OTHER"))
            return result
        except Exception as e:
            logger.warning(f"LLM email classification failed: {e}")
            return {
                "category":      "OTHER",
                "confidence":    0.50,
                "extracted":     {},
                "has_pdf":       has_pdf,
                "action_needed": "IGNORE",
            }

    def _extract_identifiers(self, subject: str, body: str) -> dict:
        """Pull load reference, TMS ref, broker MC, and emails from text."""
        combined = f"{subject} {body[:1000]}"
        result   = {}

        # TMS Reference (our numbering: TMS-2025-0042)
        tms_match = _TMS_REF_PATTERN.search(combined)
        if tms_match:
            result["tms_ref"] = tms_match.group(0).upper()

        # Generic load / order reference
        for pattern in _LOAD_REF_PATTERNS:
            match = re.search(pattern, combined, re.IGNORECASE)
            if match and not match.group(0).upper().startswith("TMS"):
                result["load_reference"] = match.group(1).strip()
                break

        # MC numbers (broker or carrier)
        mc_matches = _MC_PATTERN.findall(combined)
        if mc_matches:
            result["mc_numbers"] = list(set(mc_matches))

        # Reply-to / sender emails mentioned in body
        emails = _EMAIL_PATTERN.findall(combined)
        if emails:
            result["emails_found"] = list(set(emails))[:3]

        return result

    def _extract_payment_info(self, subject: str, body: str) -> dict:
        """Extract payment amounts, reference numbers, and dates."""
        combined   = f"{subject} {body[:1000]}"
        result     = self._extract_identifiers(subject, body)

        # Dollar amounts
        amounts = _DOLLAR_PATTERN.findall(combined)
        if amounts:
            # Clean and convert to floats
            cleaned = []
            for a in amounts:
                try:
                    cleaned.append(float(a.replace("$", "").replace(",", "").strip()))
                except ValueError:
                    pass
            if cleaned:
                result["amounts"] = cleaned
                result["largest_amount"] = max(cleaned)

        # ACH / check reference
        ach_match = re.search(r"(?:ach|check|wire)\s*#?\s*:?\s*([A-Z0-9\-]{4,20})", combined, re.IGNORECASE)
        if ach_match:
            result["payment_reference"] = ach_match.group(1)

        return result

    async def _llm_classify(self, from_email: str, subject: str, body: str) -> dict:
        """
        Use GPT-4o-mini for ambiguous email classification.
        Returns full classification dict with extracted identifiers.
        """
        from openai import AsyncOpenAI
        import json

        client = AsyncOpenAI(api_key=settings.openai_api_key)

        system_prompt = (
            "You are an email classifier for a freight dispatch company. "
            "Classify emails into ONE of: RC, CARRIER_PACKET, PAYMENT, DISPUTE, COMPLIANCE, OTHER. "
            "Also try to extract: load_reference, tms_ref, amount (number or null). "
            "Reply with JSON ONLY:\n"
            '{"category": "RC", "extracted": {"load_reference": null, "tms_ref": null, "amount": null}}'
        )
        user_content = f"From: {from_email}\nSubject: {subject}\nBody snippet: {body}"

        response = await client.chat.completions.create(
            model=settings.openai_model,
            max_tokens=120,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_content},
            ],
        )

        raw = response.choices[0].message.content.strip()
        try:
            parsed = json.loads(raw)
        except Exception:
            # Try to salvage category from raw text
            for cat in ("RC", "PAYMENT", "DISPUTE", "CARRIER_PACKET", "COMPLIANCE"):
                if cat in raw.upper():
                    return {"category": cat, "confidence": 0.70, "extracted": {}}
            return {"category": "OTHER", "confidence": 0.50, "extracted": {}}

        category = parsed.get("category", "OTHER").upper()
        valid = {"RC", "CARRIER_PACKET", "PAYMENT", "DISPUTE", "COMPLIANCE", "OTHER"}
        if category not in valid:
            category = "OTHER"

        return {
            "category":   category,
            "confidence": 0.75,
            "extracted":  parsed.get("extracted", {}),
        }


def _category_to_action(category: str) -> str:
    return {
        "RC":             "ATTACH_TO_LOAD",
        "PAYMENT":        "LOG_PAYMENT",
        "CARRIER_PACKET": "ESCALATE",
        "DISPUTE":        "ESCALATE",
        "COMPLIANCE":     "FLAG_COMPLIANCE",
        "OTHER":          "IGNORE",
    }.get(category, "IGNORE")


# Module-level singleton
email_parser_agent = EmailParserAgent()
