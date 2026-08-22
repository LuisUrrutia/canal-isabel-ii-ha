"""Stable interface for the Canal customer portal."""

from .client import (
    CanalAuthenticationError,
    CanalCaptchaCredentialsError,
    CanalCaptchaError,
    CanalClient,
    CanalConnectionError,
    CanalCredentials,
    CanalError,
    CanalInvalidResponseError,
    CaptchaSolver,
)

__all__ = [
    "CanalAuthenticationError",
    "CanalCaptchaCredentialsError",
    "CanalCaptchaError",
    "CanalClient",
    "CanalConnectionError",
    "CanalCredentials",
    "CanalError",
    "CanalInvalidResponseError",
    "CaptchaSolver",
]
