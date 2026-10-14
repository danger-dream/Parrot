"""Synthetic PAT material; no real credentials or upstream IO."""
import base64
import json
import time


def pat(key_id="fixture", tag="signature"):
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return ".".join((part({"alg": "HS256", "typ": "JWT", "sign_type": "SIGN"}),
                     part({"token_use": "project_access", "api_key": key_id}), tag))


def envelope(key_id="fixture", **changes):
    return {"code": 200, "data": {"accessToken": pat(key_id), "tokenType": "Bearer",
        "expiresIn": 3600, "expiresAt": time.time() + 3600, **changes}}
