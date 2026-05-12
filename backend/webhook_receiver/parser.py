# backend/webhook_receiver/parser.py
#
# Parses a raw GitHub webhook payload into our typed WebhookEvent model.
#
# WHY A SEPARATE PARSER?
# The router (router.py) handles HTTP concerns: headers, status codes, responses.
# The parser handles data concerns: parsing, validation, transformation.
# Keeping them separate means each can be tested independently.
# You can test parse_webhook_payload() without needing an HTTP request at all.
#
# This follows the Single Responsibility Principle:
#   router.py has one job: handle the HTTP request/response cycle.
#   parser.py has one job: turn raw JSON into a validated Python object.

import json

from backend.core.exceptions import WebhookParseError
from backend.models.enums import PullRequestAction, WebhookEventType
from backend.models.webhook import WebhookEvent


def parse_webhook_payload(
    raw_body: bytes,
    event_type_header: str | None,
) -> WebhookEvent | None:
    """
    Parses a raw GitHub webhook payload into a WebhookEvent model.

    Returns a WebhookEvent if this is an event we should process.
    Returns None if this is an event type we should silently ignore.
    Raises WebhookParseError if the payload is malformed or missing required fields.

    Args:
        raw_body:
            The raw request body as bytes (same bytes we used for HMAC validation).

        event_type_header:
            The value of the X-GitHub-Event header.
            e.g. "pull_request", "push", "star", "fork"
            None if the header was not present.

    Returns:
        WebhookEvent if this is a pull_request event we should process.
        None if this is an event type we should ignore (push, star, fork, etc.)

    Raises:
        WebhookParseError: if the payload cannot be parsed or is missing required fields.

    Example:
        event = parse_webhook_payload(
            raw_body=b'{...}',
            event_type_header="pull_request",
        )
        if event is None:
            return  # silently ignore
        # proceed with event
    """

    # Step 1: Check if we care about this event type
    # We only process pull_request events. Everything else is silently ignored.
    # Returning None (not raising an error) because ignoring an event is not an error.
    if event_type_header != WebhookEventType.PULL_REQUEST:
        return None

    # Step 2: Parse the JSON body
    # We do this before Pydantic validation to give a clearer error message.
    try:
        payload_dict = json.loads(raw_body)
    except json.JSONDecodeError as e:
        raise WebhookParseError(
            f"Request body is not valid JSON: {e}"
        ) from e

    # Step 3: Check the action field
    # We only trigger a review on: opened, synchronize, reopened.
    # Ignore: closed, labeled, assigned, milestoned, etc.
    action = payload_dict.get("action")
    if action not in {a.value for a in PullRequestAction}:
        # This is a pull_request event but not an action we care about.
        # e.g. "closed", "labeled" - silently ignore.
        return None

    # Step 4: Validate the payload shape with Pydantic
    # Pydantic will raise a ValidationError if any required field is missing
    # or has the wrong type. We catch that and re-raise as our own error type.
    try:
        event = WebhookEvent.model_validate(payload_dict)
    except Exception as e:
        raise WebhookParseError(
            f"GitHub webhook payload is missing required fields or has unexpected format. "
            f"Details: {e}"
        ) from e

    return event
