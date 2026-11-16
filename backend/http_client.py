"""לקוחות httpx משותפים: חיבור פתוח (keep-alive) לגוגל ול-Moodle חוסך לחיצת יד TLS בכל קריאה (עשרות עד מאות ms כל פעם)."""
import httpx

_clients: dict = {}


class _Shared:
    def __init__(self, client):
        self._c = client

    async def __aenter__(self):
        return self._c

    async def __aexit__(self, *exc):
        return False          # לא סוגרים: הלקוח חי לאורך כל חיי השרת


def shared_client(timeout: float = 30.0) -> _Shared:
    c = _clients.get(timeout)
    if c is None or c.is_closed:
        c = httpx.AsyncClient(timeout=timeout, limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=60.0))
        _clients[timeout] = c
    return _Shared(c)
