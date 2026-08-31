import logging

from django.http import JsonResponse

logger = logging.getLogger(__name__)

# Anything larger than this isn't a payload this API produces, and scanning
# it would mean holding it in memory to no purpose. Requests above the cap
# skip the body check; the path/query check still applies.
MAX_SCANNED_BODY_BYTES = 1_000_000


class RejectNullBytesMiddleware:
    """Turn NUL bytes in a request into a 400 instead of a 500.

    PostgreSQL cannot carry a NUL (0x00) inside a string literal, so
    psycopg2 raises ValueError *before* the query is sent. Nothing in the
    stack catches that, so any endpoint that puts request data into a
    filter or a model field returns a 500 — reachable unauthenticated on
    /api/restaurants/, /api/search/, /api/orders/<code>/ and every login
    endpoint just by putting %00 in a parameter.

    It is not a data or auth bug: the query never runs, so nothing leaks
    and nothing is written. But it is a trivially reachable unhandled
    error on public endpoints, which means noisy logs, wasted error
    budget, and a finding on the first scan anyone points at the domain.

    Done as middleware rather than per-view validation on purpose. The
    problem isn't specific to any one parameter — it applies to every
    value that reaches the database — so fixing it at each call site would
    mean remembering to do it again on every endpoint added later. A NUL
    byte is never valid in a URL or in this API's JSON, so rejecting the
    whole request outright is both correct and the smallest rule.
    """

    NUL = "\x00"
    NUL_BYTE = b"\x00"
    # A NUL almost never travels as a literal 0x00. In a URL it is
    # percent-encoded, and JSON encodes it as an escape — so the raw bytes
    # look clean and the NUL only appears once something decodes them.
    # Both encodings have to be checked, not just the byte.
    JSON_ESCAPED_NUL = b"\\u0000"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._has_null_byte(request):
            logger.warning("Rejected request with NUL byte: %s", request.path[:200])
            return JsonResponse({"detail": "Malformed request."}, status=400)
        return self.get_response(request)

    def _has_null_byte(self, request):
        if self.NUL in request.path:
            return True

        # request.GET is the decoded form, so this catches %00 without
        # having to pattern-match the encoding (and without rejecting a
        # legitimately double-encoded %2500, which is just text).
        try:
            for values in request.GET.lists():
                if any(self.NUL in v for v in values[1]):
                    return True
        except Exception:
            pass

        if request.method not in ("POST", "PUT", "PATCH"):
            return False

        # Reading request.body here is safe: Django caches it, so the view
        # (and RazorpayWebhookView's signature check, which needs the raw
        # bytes) still sees exactly the same content afterwards.
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except (TypeError, ValueError):
            length = 0
        if length > MAX_SCANNED_BODY_BYTES:
            return False
        try:
            body = request.body
        except Exception:
            # An unreadable body isn't this middleware's problem — let the
            # view or parser produce its own error for it.
            return False
        return self.NUL_BYTE in body or self.JSON_ESCAPED_NUL in body.lower()
