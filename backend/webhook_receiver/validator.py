# backend/webhook_receiver/validator.py
#
# HMAC-SHA256 signature validation for GitHub webhooks.
#
# WHY THIS EXISTS:
# Our webhook endpoint is publicly accessible on the internet.
# Anyone who knows the URL can send a POST request pretending to be GitHub.
# Without validation, an attacker could trigger agent reviews on arbitrary code,
# drain our LLM budget, or inject malicious data into our pipeline.
#
# HOW GITHUB SIGNS WEBHOOKS:
# When you create a webhook on GitHub, you set a secret string.
# GitHub computes: HMAC-SHA256(secret, raw_request_body)
# GitHub sends this as the X-Hub-Signature-256 header on every request.
# Format: "sha256=<hex_digest>"
#
# Our job: recompute the same HMAC and compare.
# If they match: the request genuinely came from GitHub with our secret.
# If they don't match: reject with 401.
#
# CRITICAL: we must use hmac.compare_digest() for the comparison, NOT ==
# The reason: == short-circuits. It stops comparing as soon as it finds a mismatch.
# This leaks timing information that attackers can use to guess the signature
# one character at a time (timing attack).
# hmac.compare_digest() always takes the same time regardless of where mismatch is.

import hashlib
import hmac

from backend.core.exceptions import WebhookValidationError


def validate_github_signature(
    payload_bytes: bytes,
    signature_header: str | None,
    secret: str,
) -> None:
    """
    Validates the HMAC-SHA256 signature on a GitHub webhook request.

    This function either returns None (validation passed) or raises
    WebhookValidationError (validation failed). The caller returns 401 on failure.

    Args:
        payload_bytes:
            The raw request body as bytes, exactly as received from GitHub.
            IMPORTANT: must be the raw bytes before any JSON parsing.
            Parsing changes whitespace and key ordering, which changes the HMAC.

        signature_header:
            The value of the X-Hub-Signature-256 header sent by GitHub.
            Format: "sha256=<64 hex characters>"
            None if the header was not present in the request.

        secret:
            Our webhook secret string (from settings.github_webhook_secret).
            Must match the secret configured in the GitHub webhook settings.

    Raises:
        WebhookValidationError: if signature is missing, malformed, or does not match.

    Example:
        validate_github_signature(
            payload_bytes=b'{"action": "opened", ...}',
            signature_header="sha256=abc123...",
            secret="my-webhook-secret",
        )
        # Returns None if valid, raises WebhookValidationError if not
    """

    # Step 1: Check the header is present
    if not signature_header:
        raise WebhookValidationError(
            "Missing X-Hub-Signature-256 header. "
            "Request did not come from GitHub or secret is not configured."
        )

    # Step 2: Check the header has the expected format
    # GitHub always sends "sha256=<hex>", never just the hex.
    if not signature_header.startswith("sha256="):
        raise WebhookValidationError(
            f"Malformed signature header: '{signature_header}'. "
            "Expected format: 'sha256=<hex_digest>'"
        )

    # Step 3: Extract just the hex digest part (everything after "sha256=")
    received_signature = signature_header[len("sha256="):]

    # Step 4: Compute our own HMAC-SHA256
    # We use the secret as the key and the raw payload bytes as the message.
    # The secret must be encoded to bytes for the hmac function.
    expected_signature = hmac.new(
        key=secret.encode("utf-8"),
        msg=payload_bytes,
        digestmod=hashlib.sha256,
    ).hexdigest()

    # Step 5: Compare using timing-safe comparison
    # hmac.compare_digest prevents timing attacks (see module docstring above).
    if not hmac.compare_digest(expected_signature, received_signature):
        raise WebhookValidationError(
            "Signature mismatch. The request body or webhook secret is incorrect. "
            "Verify that GITHUB_WEBHOOK_SECRET matches the secret in GitHub webhook settings."
        )

    # If we reach here, validation passed. Return None implicitly.
