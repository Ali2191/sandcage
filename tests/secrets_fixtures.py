"""Fake secrets for tests, assembled at runtime.

No secret-shaped literal is written in the repository (GitHub push protection and secret
scanners would flag them), so every sample is built from parts here.
"""

from __future__ import annotations

from typing import Final

ALNUM: Final = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
UPPER_DIGITS: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def fake(prefix: str, length: int, alphabet: str = ALNUM) -> str:
    """A deterministic fake token: ``prefix`` followed by ``length`` characters."""
    body = (alphabet * (length // len(alphabet) + 1))[:length]
    return prefix + body


ANTHROPIC_KEY: Final = fake("sk-" + "ant-api03-", 40)
OPENAI_KEY: Final = fake("sk-" + "proj-", 40)
STRIPE_KEY: Final = "sk_" + "live_" + "FAKE" + fake("", 20)
GROQ_KEY: Final = fake("gsk" + "_", 40)
AWS_ACCESS_KEY: Final = "AKIA" + "IOSFODNN7EXAMPLE"
GITHUB_TOKEN: Final = fake("gh" + "p_", 36)
SLACK_TOKEN: Final = "xox" + "b-1234567890-abcdefghij"
GOOGLE_API_KEY: Final = fake("AI" + "za", 35)
HUGGINGFACE_TOKEN: Final = fake("hf" + "_", 34)
JWT: Final = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0In0" + ".abcDEF123_-xyz"

TOKEN_SAMPLES: Final[dict[str, str]] = {
    "anthropic_key": ANTHROPIC_KEY,
    "openai_key": OPENAI_KEY,
    "stripe_key": STRIPE_KEY,
    "groq_key": GROQ_KEY,
    "aws_access_key": AWS_ACCESS_KEY,
    "github_token": GITHUB_TOKEN,
    "slack_token": SLACK_TOKEN,
    "google_api_key": GOOGLE_API_KEY,
    "huggingface_token": HUGGINGFACE_TOKEN,
    "jwt": JWT,
}

PRIVATE_KEY_BLOCK: Final = (
    "-----BEGIN " + "RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEAfakefakefakefakefakefake\n"
    "abcdefabcdefabcdefabcdefabcdefabcdef==\n"
    "-----END " + "RSA PRIVATE KEY-----"
)
