import anthropic
import base64
import json
import re

SCAN_PROMPT = """Ez egy blokkfotó (vagy több, egymást esetleg átfedő fotó ugyanarról a blokkról).
Kérlek, olvasd be az összes tételt és a végösszeget.

Ha több fotó van és átfednek, ne duplikáld a tételeket.

Válaszolj KIZÁRÓLAG valid JSON formátumban, így:
{
  "vegosszeg": 12345,
  "tetelek": [
    {"description": "Tej", "amount": 450, "category_hint": "Élelmiszer"},
    {"description": "Kenyér", "amount": 380, "category_hint": "Élelmiszer"}
  ]
}

A category_hint legyen az egyik: Élelmiszer, Lakás, Közlekedés, Szórakozás, Egészség, Egyéb kiadás,
vagy üres string ha nem egyértelmű.
Csak a JSON-t add vissza, semmi mást."""


def scan_receipt_images(images):
    """
    images: list of (bytes, media_type) tuples
    Returns dict with 'vegosszeg' and 'tetelek' keys
    """
    client = anthropic.Anthropic()
    content = []

    for img_bytes, media_type in images:
        if media_type not in ('image/jpeg', 'image/png', 'image/gif', 'image/webp'):
            media_type = 'image/jpeg'
        img_b64 = base64.standard_b64encode(img_bytes).decode('utf-8')
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": img_b64
            }
        })

    content.append({"type": "text", "text": SCAN_PROMPT})

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": content}]
    )

    text = message.content[0].text.strip()

    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        text = match.group(0)

    return json.loads(text)
