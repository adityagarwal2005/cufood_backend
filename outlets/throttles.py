import hashlib

from rest_framework.throttling import SimpleRateThrottle


class LoginIdentifierThrottle(SimpleRateThrottle):
    """Rate-limits login attempts per targeted account, not per caller IP.

    DRF's built-in throttles key unauthenticated requests on the client's
    IP address. That is the wrong unit for this app: students order from
    campus wifi, so the whole campus shares one public IP and therefore
    one bucket — a 5/min login limit becomes five attempts per minute for
    everybody, and logins fail for legitimate users long before they slow
    an attacker down.

    Keying on the submitted identifier inverts that. One account can only
    be guessed at N/min no matter how many hosts the guesses come from,
    while a hall full of students logging into their own accounts never
    contend with each other.

    The IP-keyed scope is kept alongside this (see the login views) as a
    coarse ceiling on a single host churning through many usernames; this
    class is the control that actually protects an individual account.

    The identifier is hashed rather than used directly so usernames and
    email addresses aren't sitting in cache keys.
    """

    scope = "login_identifier"

    # Student/admin endpoints post "identifier" (username or email); the
    # restaurant-owner login posts "username". Both are the account being
    # targeted, so both key this throttle — reading only one of them would
    # silently leave the other endpoint unprotected.
    IDENTITY_FIELDS = ("identifier", "username", "email")

    def get_cache_key(self, request, view):
        identifier = ""
        for field in self.IDENTITY_FIELDS:
            value = request.data.get(field)
            if value:
                identifier = str(value).strip().lower()
                break
        if not identifier:
            # Nothing to attribute the attempt to — the view will reject it
            # on its own; don't collapse every malformed request into one
            # shared bucket.
            return None
        digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
        return self.cache_format % {"scope": self.scope, "ident": digest}


class OrderStatusThrottle(SimpleRateThrottle):
    """Rate-limits order lookups per ORDER, not per caller IP.

    order-status.html polls while a payment is confirming. The endpoint is
    deliberately unauthenticated — a pickup code is all a student needs —
    so DRF's default keying falls back to IP, and on campus wifi that is
    one bucket for thousands of people:

        5,000 students polling every 3s = 100,000 req/min from one IP
        against a shared per-IP limit, which breaks at about 30 of them.

    Keying on the order code instead makes the limit mean what it should:
    one order can be polled N times a minute. Two students watching two
    different orders never contend, no matter how many share a network,
    while a single code still can't be hammered — which is the only thing
    the limit was protecting against (enumeration is already hopeless
    against 36^6 codes drawn from secrets).

    Falls back to the IP-keyed default when there's no order code in the
    URL, so nothing is left unlimited.
    """

    scope = "order_status"

    def get_cache_key(self, request, view):
        code = (view.kwargs or {}).get("order_code")
        if not code:
            ident = self.get_ident(request)
            return self.cache_format % {"scope": self.scope, "ident": ident}
        return self.cache_format % {"scope": self.scope, "ident": str(code).upper()[:16]}
