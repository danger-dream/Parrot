"""Retired AG image adapter. Accounts, chat and historical media are unaffected."""
from fastapi.responses import JSONResponse


def is_antigravity_image_model(model: str | None) -> bool:
    return False


def looks_like_antigravity_image_model(model: str | None) -> bool:
    return False


async def handle_image(*args, **kwargs):
    return JSONResponse({'error': {'type': 'unsupported_image_provider',
        'message': 'Antigravity image generation has been removed'}}, status_code=410)
