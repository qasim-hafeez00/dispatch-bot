"""
cortexbot/agents/document_ocr.py

Document OCR Intelligence — Agent M

Extracts structured data from PDF documents (RC, BOL, COI, W-9).
Uses AWS Textract as primary, Claude Vision as enhancement for
complex layouts or low-confidence fields.
Also includes discrepancy-checking logic to compare OCR'd fields
against expected values (e.g., agreed rate) to catch bait-and-switch.
"""

import base64
import json
import logging
import io
from typing import Optional, List, Dict

import boto3

from cortexbot.config import settings
from cortexbot.core.redis_client import get_redis

logger = logging.getLogger("cortexbot.agents.document_ocr")


async def extract_rc_fields(s3_url: str) -> dict:
    """
    Extract all 25+ fields from a Rate Confirmation PDF.

    Args:
        s3_url: S3 URL like "s3://bucket-name/path/to/file.pdf"

    Returns:
        {"fields": {...}, "quality_score": 0.92, "quality_issues": [...]}
    """
    from cortexbot.mocks import MOCKS_ENABLED
    if MOCKS_ENABLED:
        from cortexbot.mocks.ocr_mock import mock_extract_rc_fields
        return await mock_extract_rc_fields(s3_url)

    logger.info(f"📄 OCR extracting RC from {s3_url}")

    # Download from S3
    pdf_bytes = await _download_from_s3(s3_url)

    # Try AWS Textract first
    textract_fields = await _textract_extract(pdf_bytes)

    # Enhance with Claude Vision for complex fields
    vision_fields = await _claude_vision_extract(pdf_bytes, "RC")

    # Merge: prefer Textract for machine-printed text, Claude for context
    merged = _merge_results(textract_fields, vision_fields)

    # Validate
    quality_issues = _validate_rc_fields(merged)

    return {
        "fields":         merged,
        "quality_issues": quality_issues,
        "quality_score":  1.0 - (len(quality_issues) * 0.1),
    }


def compare_rc_to_expected(extracted: dict, expected: dict) -> List[str]:
    """
    Compare OCR'd fields from the RC against what we agreed to on the call.
    Returns a list of discrepancy warnings. Empty list means NO discrepancies.
    """
    discrepancies = []
    
    # 1. Compare Rate
    # Rate can be flat or per mile, need to check total payout
    agreed_cpm = expected.get("agreed_rate_cpm")
    loaded_miles = expected.get("loaded_miles")
    
    if agreed_cpm and loaded_miles:
        expected_flat = float(agreed_cpm) * float(loaded_miles)
    else:
        # Fallback if flat rate was given directly
        expected_flat = expected.get("agreed_flat_rate")

    actual_flat = extracted.get("flat_rate")
    actual_cpm = extracted.get("rate_per_mile")
    
    if expected_flat and actual_flat:
        # Allow $5 wiggle room for rounding
        if float(actual_flat) < (float(expected_flat) - 5.0):
            discrepancies.append(f"RATE MISMATCH: RC shows ${actual_flat}, but expected ~${expected_flat:.2f}")
    elif agreed_cpm and actual_cpm:
        if float(actual_cpm) < float(agreed_cpm):
            discrepancies.append(f"RATE MISMATCH: RC shows ${actual_cpm}/mi, but expected ${agreed_cpm}/mi")
    elif not actual_flat and not actual_cpm:
        discrepancies.append("RATE MISSING: Could not find rate on RC")

    # 2. Compare Accessorials (if locked)
    locked_accessorials = expected.get("locked_accessorials", {})
    if locked_accessorials:
        # Check detention
        expected_det_rate = locked_accessorials.get("detention_rate_per_hour")
        actual_det_rate = extracted.get("detention_rate_per_hour")
        if expected_det_rate and actual_det_rate:
            if float(actual_det_rate) < float(expected_det_rate):
                discrepancies.append(f"DETENTION MISMATCH: RC shows ${actual_det_rate}/hr, expected ${expected_det_rate}/hr")
        
        # Check TONU
        expected_tonu = locked_accessorials.get("tonu_amount")
        actual_tonu = extracted.get("tonu_amount")
        if expected_tonu and actual_tonu:
            if float(actual_tonu) < float(expected_tonu):
                discrepancies.append(f"TONU MISMATCH: RC shows ${actual_tonu}, expected ${expected_tonu}")

    # 3. Compare Weights
    expected_weight = expected.get("weight_lbs")
    actual_weight = extracted.get("weight_lbs")
    if expected_weight and actual_weight:
        # If the RC weight is more than 500 lbs heavier than stated
        if float(actual_weight) > float(expected_weight) + 500:
            discrepancies.append(f"WEIGHT MISMATCH: RC shows {actual_weight} lbs, but was told {expected_weight} lbs")

    return discrepancies


async def _download_from_s3(s3_url: str) -> bytes:
    """Download file from S3 URL."""
    from cortexbot.mocks import MOCKS_ENABLED
    if MOCKS_ENABLED:
        from cortexbot.mocks.s3_mock import mock_download
        return await mock_download(s3_url)

    # Parse s3://bucket/key
    without_prefix = s3_url.replace("s3://", "")
    bucket, key    = without_prefix.split("/", 1)

    s3 = boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )

    import asyncio
    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(None, lambda: s3.get_object(Bucket=bucket, Key=key))
    return await loop.run_in_executor(None, lambda: response["Body"].read())


async def _textract_extract(pdf_bytes: bytes) -> dict:
    """Run AWS Textract on the PDF and extract key-value pairs."""
    try:
        import asyncio

        textract = boto3.client(
            "textract",
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            region_name=settings.aws_region,
        )

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: textract.analyze_document(
                Document={"Bytes": pdf_bytes},
                FeatureTypes=["FORMS", "TABLES"],
            )
        )

        return _parse_textract_response(response)

    except Exception as e:
        logger.warning(f"Textract failed: {e}")
        return {}


def _parse_textract_response(response: dict) -> dict:
    """Extract key-value pairs from Textract response."""
    fields = {}
    blocks = response.get("Blocks", [])

    block_map = {b["Id"]: b for b in blocks}

    for block in blocks:
        if block["BlockType"] == "KEY_VALUE_SET" and "KEY" in block.get("EntityTypes", []):
            key_text   = _get_text(block, block_map)
            value_block = _get_value_block(block, block_map)
            if value_block:
                value_text = _get_text(value_block, block_map)
                if key_text and value_text:
                    key_norm = key_text.lower().replace(" ", "_").replace(":", "").strip("_")
                    fields[key_norm] = value_text.strip()

    return fields


def _get_text(block: dict, block_map: dict) -> str:
    if block.get("BlockType") == "WORD":
        return block.get("Text", "")

    text_parts = []
    for rel in block.get("Relationships", []):
        if rel["Type"] in ("CHILD", "VALUE"):
            for child_id in rel.get("Ids", []):
                child = block_map.get(child_id, {})
                text_parts.append(_get_text(child, block_map))

    return " ".join(filter(None, text_parts))


def _get_value_block(key_block: dict, block_map: dict) -> Optional[dict]:
    for rel in key_block.get("Relationships", []):
        if rel["Type"] == "VALUE":
            for value_id in rel.get("Ids", []):
                return block_map.get(value_id)
    return None


RC_EXTRACT_PROMPT = """Extract ALL 25 standard fields from this Rate Confirmation document.
Return ONLY valid JSON — no explanation, no code fences.

{
  "carrier_mc_number": "MC-XXXXXX or null",
  "broker_mc_number": "MC-XXXXXX or null",
  "broker_company_name": "string or null",
  "load_reference": "string or null",
  "pickup_full_address": "full address or null",
  "pickup_date": "YYYY-MM-DD or null",
  "pickup_appointment_time": "HH:MM or null",
  "delivery_full_address": "full address or null",
  "delivery_date": "YYYY-MM-DD or null",
  "delivery_appointment_time": "HH:MM or null",
  "commodity": "string or null",
  "weight_lbs": number_or_null,
  "piece_count": number_or_null,
  "equipment_type": "string or null",
  "rate_per_mile": number_or_null,
  "flat_rate": number_or_null,
  "fuel_surcharge_included": true/false/null,
  "detention_free_hours": number_or_null,
  "detention_rate_per_hour": number_or_null,
  "tonu_amount": number_or_null,
  "layover_rate": number_or_null,
  "lumper_payer": "broker" or "carrier" or null,
  "tracking_method": "string or null",
  "payment_terms_days": number_or_null,
  "quick_pay_pct": number_or_null,
  "factoring_allowed": true/false/null,
  "invoice_email": "email or null"
}

Rate Confirmation text:
"""


async def _claude_vision_extract(pdf_bytes: bytes, doc_type: str = "RC") -> dict:
    """Use Claude Vision to extract fields from document image."""
    try:
        import sys
        
        # Determine appropriate LLM client
        # Although settings say Claude, we support flexible model providers based on what's available
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        
        # Render all pages (up to 3) so multi-page RCs are fully covered.
        import fitz
        pdf_doc  = fitz.open(stream=pdf_bytes, filetype="pdf")
        mat      = fitz.Matrix(2, 2)
        content: list = []
        for page_idx in range(min(len(pdf_doc), 3)):
            pix       = pdf_doc[page_idx].get_pixmap(matrix=mat)
            img_b64   = base64.standard_b64encode(pix.tobytes("jpeg")).decode()
            content.append({
                "type": "image",
                "source": {
                    "type":       "base64",
                    "media_type": "image/jpeg",
                    "data":       img_b64,
                },
            })
        pdf_doc.close()
        content.append({"type": "text", "text": RC_EXTRACT_PROMPT})

        response = await client.messages.create(
            model=settings.claude_model,
            max_tokens=1500,
            messages=[{"role": "user", "content": content}],
        )

        json_text = response.content[0].text.strip()
        if json_text.startswith("```"):
            lines = json_text.split("\n")
            json_text = "\n".join(lines[1:-1])
        if json_text.startswith("json"):
            json_text = json_text[4:].strip()

        return json.loads(json_text)

    except Exception as e:
        logger.warning(f"Claude Vision extraction failed: {e}")
        return {}


def _merge_results(textract: dict, vision: dict) -> dict:
    merged = {}
    merged.update(vision)
    for key, value in textract.items():
        if key not in merged or merged[key] is None:
            merged[key] = value
    return merged


def _validate_rc_fields(fields: dict) -> list:
    issues = []
    required = ["pickup_full_address", "delivery_full_address", "pickup_date", "delivery_date"]
    for field in required:
        if not fields.get(field):
            issues.append(f"Missing required field: {field}")

    if not fields.get("rate_per_mile") and not fields.get("flat_rate"):
        issues.append("No rate found (rate_per_mile or flat_rate)")

    return issues
