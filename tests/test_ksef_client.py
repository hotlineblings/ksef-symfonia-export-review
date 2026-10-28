"""Testy ksef_client na mockach HTTP (respx) — bez sieci."""

import base64
import hashlib
import io
import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
import respx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.ksef_client import (
    KsefAuthError,
    KsefClient,
    KsefInvoiceNotReady,
    split_date_range,
)

BASE = "https://ksef.example/v2"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("app.ksef_client._sleep", lambda seconds, cancel=None: None)


@pytest.fixture(scope="module")
def mf_cert_b64() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MF Test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()


def _fake_jwt(exp_offset_s: int = 900) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(datetime.now(timezone.utc).timestamp()) + exp_offset_s}).encode()
    ).rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.sig"


def _mock_auth_endpoints(router, mf_cert_b64, poll_codes=(100, 200)):
    router.get("/security/public-key-certificates").respond(
        200,
        json=[
            {
                "certificate": mf_cert_b64,
                "publicKeyId": "key-1",
                "usage": ["KsefTokenEncryption", "SymmetricKeyEncryption"],
            }
        ],
    )
    router.post("/auth/challenge").respond(
        200, json={"challenge": "ch-123", "timestampMs": 1750000000000}
    )
    router.post("/auth/ksef-token").respond(
        202,
        json={
            "referenceNumber": "ref-1",
            "authenticationToken": {"token": "auth-jwt", "validUntil": "2099-01-01"},
        },
    )
    codes = iter(poll_codes)
    router.get("/auth/ref-1").mock(
        side_effect=lambda request: httpx.Response(200, json={"status": {"code": next(codes)}})
    )
    router.post("/auth/token/redeem").respond(
        200,
        json={
            "accessToken": {"token": _fake_jwt(), "validUntil": "2099-01-01"},
            "refreshToken": {"token": "refresh-1", "validUntil": "2099-01-08"},
        },
    )


@respx.mock(base_url=BASE)
def test_auth_happy_path(respx_mock, mf_cert_b64):
    _mock_auth_endpoints(respx_mock, mf_cert_b64)
    client = KsefClient(BASE, "5252445719", "token-abc")
    client.authenticate()
    assert client._access_token
    assert client._refresh_token == "refresh-1"
    # sprawdź, że wysłaliśmy poprawny request logowania
    sent = json.loads(respx_mock.routes[2].calls[0].request.content)
    assert sent["challenge"] == "ch-123"
    assert sent["contextIdentifier"] == {"type": "Nip", "value": "5252445719"}
    assert sent["publicKeyId"] == "key-1"
    assert base64.b64decode(sent["encryptedToken"])  # poprawny Base64


@respx.mock(base_url=BASE, assert_all_called=False)
def test_auth_415_no_permission(respx_mock, mf_cert_b64):
    _mock_auth_endpoints(respx_mock, mf_cert_b64, poll_codes=(415,))
    client = KsefClient(BASE, "5252445719", "token-abc")
    with pytest.raises(KsefAuthError, match="InvoiceRead"):
        client.authenticate()


@respx.mock(base_url=BASE, assert_all_called=False)
def test_auth_450_crypto(respx_mock, mf_cert_b64):
    _mock_auth_endpoints(respx_mock, mf_cert_b64, poll_codes=(450,))
    client = KsefClient(BASE, "5252445719", "token-abc")
    with pytest.raises(KsefAuthError, match="450"):
        client.authenticate()


def _meta(n: int, day: int = 1) -> dict:
    return {
        "ksefNumber": f"5252445719-2026010{day}-AAAAAAAAAAA{n}-0{n}",
        "invoiceNumber": f"FV/{n}",
        "issueDate": f"2026-01-0{day}T00:00:00+00:00",
        "invoicingDate": f"2026-01-0{day}T12:00:00+00:00",
        "seller": {"nip": "1180012345", "name": "Dostawca"},
        "buyer": {"identifier": {"type": "Nip", "value": "5252445719"}, "name": "My"},
        "netAmount": 100.0,
        "grossAmount": 123.0,
        "vatAmount": 23.0,
        "currency": "PLN",
        "formCode": {"systemCode": "FA (3)", "schemaVersion": "1-0E"},
        "invoiceType": "Vat",
    }


def _authed_client(respx_mock, mf_cert_b64) -> KsefClient:
    _mock_auth_endpoints(respx_mock, mf_cert_b64)
    client = KsefClient(BASE, "5252445719", "token-abc")
    client.authenticate()
    return client


@respx.mock(base_url=BASE)
def test_query_pagination_has_more(respx_mock, mf_cert_b64):
    client = _authed_client(respx_mock, mf_cert_b64)
    pages = [
        {"hasMore": True, "isTruncated": False, "invoices": [_meta(1), _meta(2)]},
        {"hasMore": False, "isTruncated": False, "invoices": [_meta(3)]},
    ]
    calls = []

    def responder(request):
        calls.append(dict(request.url.params))
        return httpx.Response(200, json=pages[len(calls) - 1])

    respx_mock.post("/invoices/query/metadata").mock(side_effect=responder)
    result = list(client.query_metadata("Subject2", date(2026, 1, 1), date(2026, 1, 31)))
    assert [m.invoice_number for m in result] == ["FV/1", "FV/2", "FV/3"]
    assert calls[0]["pageOffset"] == "0" and calls[1]["pageOffset"] == "1"
    assert result[0].ksef_date == "2026-01-01"


@respx.mock(base_url=BASE)
def test_query_truncated_narrows_window(respx_mock, mf_cert_b64):
    client = _authed_client(respx_mock, mf_cert_b64)
    pages = [
        {"hasMore": True, "isTruncated": True, "invoices": [_meta(1, day=2)]},
        {"hasMore": False, "isTruncated": False, "invoices": [_meta(2, day=3)]},
    ]
    bodies = []

    def responder(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=pages[len(bodies) - 1])

    respx_mock.post("/invoices/query/metadata").mock(side_effect=responder)
    result = list(client.query_metadata("Subject2", date(2026, 1, 1), date(2026, 1, 31)))
    assert len(result) == 2
    # zawężenie: drugi request zaczyna od daty ostatniego rekordu pierwszego
    assert bodies[1]["dateRange"]["from"] == "2026-01-02T00:00:00+00:00"


@respx.mock(base_url=BASE)
def test_query_truncated_issue_date_bez_czasu_daje_pelny_date_time(respx_mock, mf_cert_b64):
    """KSeF zwraca issueDate jako samą datę (format: date); zawężony `from`
    musi być pełnym date-time z offsetem, a nie gołą datą."""
    client = _authed_client(respx_mock, mf_cert_b64)
    pierwszy = _meta(1, day=2)
    pierwszy["issueDate"] = "2026-01-02"
    drugi = _meta(2, day=3)
    drugi["issueDate"] = "2026-01-03"
    pages = [
        {"hasMore": True, "isTruncated": True, "invoices": [pierwszy]},
        {"hasMore": False, "isTruncated": False, "invoices": [drugi]},
    ]
    bodies = []

    def responder(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=pages[len(bodies) - 1])

    respx_mock.post("/invoices/query/metadata").mock(side_effect=responder)
    result = list(client.query_metadata("Subject2", date(2026, 1, 1), date(2026, 1, 31)))
    assert len(result) == 2
    assert bodies[1]["dateRange"]["from"] == "2026-01-02T00:00:00+00:00"


@respx.mock(base_url=BASE)
def test_query_zapas_doklejany_do_kazdego_okna_bez_dodatkowych_zapytan(respx_mock, mf_cert_b64):
    client = _authed_client(respx_mock, mf_cert_b64)
    bodies = []

    def responder(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"hasMore": False, "isTruncated": False, "invoices": []})

    respx_mock.post("/invoices/query/metadata").mock(side_effect=responder)
    list(client.query_metadata("Subject2", date(2026, 1, 1), date(2026, 6, 30), margin=timedelta(days=1)))
    zakresy = [(b["dateRange"]["from"], b["dateRange"]["to"]) for b in bodies]
    assert zakresy == [
        ("2025-12-31T00:00:00+00:00", "2026-04-01T23:59:59+00:00"),
        ("2026-03-31T00:00:00+00:00", "2026-07-01T23:59:59+00:00"),
    ]


def test_split_date_range():
    wins = split_date_range(date(2026, 1, 1), date(2026, 7, 15))
    assert wins == [
        (date(2026, 1, 1), date(2026, 3, 31)),
        (date(2026, 4, 1), date(2026, 6, 30)),
        (date(2026, 7, 1), date(2026, 7, 15)),
    ]
    assert split_date_range(date(2026, 1, 1), date(2026, 1, 1)) == [
        (date(2026, 1, 1), date(2026, 1, 1))
    ]
    with pytest.raises(ValueError):
        split_date_range(date(2026, 2, 1), date(2026, 1, 1))


@respx.mock(base_url=BASE)
def test_download_21165_retry_then_success(respx_mock, mf_cert_b64):
    client = _authed_client(respx_mock, mf_cert_b64)
    responses = iter(
        [
            httpx.Response(400, json={"exception": {"codes": [21165]}, "detail": "21165"}),
            httpx.Response(200, content=b"<Faktura/>"),
        ]
    )
    respx_mock.get("/invoices/ksef/NR-1").mock(side_effect=lambda r: next(responses))
    assert client.download_invoice("NR-1") == b"<Faktura/>"


@respx.mock(base_url=BASE)
def test_download_21165_gives_up(respx_mock, mf_cert_b64):
    client = _authed_client(respx_mock, mf_cert_b64)
    respx_mock.get("/invoices/ksef/NR-2").respond(400, text='{"code": 21165}')
    with pytest.raises(KsefInvoiceNotReady):
        client.download_invoice("NR-2")


@respx.mock(base_url=BASE)
def test_download_429_then_success(respx_mock, mf_cert_b64):
    client = _authed_client(respx_mock, mf_cert_b64)
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, content=b"<Faktura/>"),
        ]
    )
    respx_mock.get("/invoices/ksef/NR-3").mock(side_effect=lambda r: next(responses))
    assert client.download_invoice("NR-3") == b"<Faktura/>"


@respx.mock(base_url=BASE)
def test_download_429_long_retry_after_raises_and_persists(respx_mock, mf_cert_b64, tmp_path):
    """429 z Retry-After ~30 min (jak na produkcji): czytelny wyjątek zamiast zwisu,
    a blokada zapamiętana — kolejne uruchomienie nie uderza w serwer."""
    from app.ksef_client import KsefQuotaExceeded
    from app.quota import QuotaStore

    quota = QuotaStore(tmp_path / "q.db")
    _mock_auth_endpoints(respx_mock, mf_cert_b64)
    client = KsefClient(BASE, "5252445719", "token-abc", quota=quota)
    client.authenticate()
    route = respx_mock.get("/invoices/ksef/NR-4").respond(
        429, headers={"Retry-After": "1826"}
    )
    with pytest.raises(KsefQuotaExceeded):
        client.download_invoice("NR-4")
    calls_after_first = route.call_count

    # "drugie uruchomienie": świeży klient z tą samą bazą quota
    client2 = KsefClient(BASE, "5252445719", "token-abc", quota=QuotaStore(tmp_path / "q.db"))
    client2._access_token = "x"  # nie potrzebujemy auth — blokada zadziała wcześniej
    client2._access_exp = 2**33
    with pytest.raises(KsefQuotaExceeded):
        client2.download_invoice("NR-4")
    assert route.call_count == calls_after_first  # zero nowych żądań HTTP
    assert client2.download_budget_remaining() == 0


@respx.mock(base_url=BASE)
def test_batch_export_roundtrip(respx_mock, mf_cert_b64):
    """Eksport wsadowy: zlecenie -> status -> pobranie części -> AES -> ZIP -> XML-e."""
    import zipfile as zf
    from cryptography.hazmat.primitives import padding as sym_padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    client = _authed_client(respx_mock, mf_cert_b64)
    respx_mock.post("/invoices/exports").respond(201, json={"referenceNumber": "EXP-1"})
    reference = client.request_export("Subject1", date(2026, 7, 1), date(2026, 7, 6))
    assert reference == "EXP-1"
    sent = json.loads(respx_mock.routes[-1].calls[0].request.content)
    assert sent["filters"]["subjectType"] == "Subject1"
    assert len(base64.b64decode(sent["encryption"]["initializationVector"])) == 16

    # zbuduj paczkę: ZIP z 2 fakturami, zaszyfrowany kluczem, który wygenerował klient
    key, iv = client._export_keys["EXP-1"]
    buf = io.BytesIO()
    with zf.ZipFile(buf, "w") as archive:
        archive.writestr("NR-0001.xml", b"<Faktura>1</Faktura>")
        archive.writestr("NR-0002.xml", b"<Faktura>2</Faktura>")
        archive.writestr("_metadata.json", b"{}")
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(buf.getvalue()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    blob = encryptor.update(padded) + encryptor.finalize()
    part_url = "https://blob.example/part1.zip.aes"
    respx_mock.get(part_url).respond(200, content=blob)
    package = {
        "invoiceCount": 2,
        "size": len(blob),
        "isTruncated": False,
        "parts": [
            {
                "ordinalNumber": 1,
                "partName": "part1.zip.aes",
                "method": "GET",
                "url": part_url,
                "encryptedPartHash": base64.b64encode(
                    hashlib.sha256(blob).digest()
                ).decode(),
            }
        ],
    }
    statuses = iter(
        [
            {"status": {"code": 100, "description": "w toku"}},
            {"status": {"code": 200, "description": "ok"}, "package": package},
        ]
    )
    respx_mock.get("/invoices/exports/EXP-1").mock(
        side_effect=lambda r: httpx.Response(200, json=next(statuses))
    )
    got_package = client.wait_for_export("EXP-1")
    invoices = dict(client.iter_export_invoices("EXP-1", got_package))
    assert invoices == {
        "NR-0001": b"<Faktura>1</Faktura>",
        "NR-0002": b"<Faktura>2</Faktura>",
    }


def _fake_clock(monkeypatch, start: float = 1000.0):
    """Wspólny sztuczny zegar dla ksef_client i quota (testy okien czasowych)."""
    from types import SimpleNamespace

    import app.ksef_client as kc
    import app.quota as q

    clock = {"t": start}
    fake = SimpleNamespace(
        time=lambda: clock["t"],
        monotonic=lambda: clock["t"],
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
    )
    monkeypatch.setattr(kc, "time", fake)
    monkeypatch.setattr(q, "time", fake)
    slept: list[float] = []

    def fake_sleep(seconds, cancel=None):
        slept.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(kc, "_sleep", fake_sleep)
    return clock, slept


def _limiter(tmp_path, cancel=None):
    import app.ksef_client as kc
    from app.quota import QuotaStore

    return kc._RateLimiter(
        [(1, 8), (60, 16)], QuotaStore(tmp_path / "q.db"),
        "1234567890", "download", "pobierania faktur", cancel,
    )


def test_limiter_enforces_minute_window(monkeypatch, tmp_path):
    """16 pobrań w minucie wyczerpuje okno 16/min — 17. wywołanie musi odczekać."""
    clock, slept = _fake_clock(monkeypatch)
    limiter = _limiter(tmp_path)
    for _ in range(16):
        limiter.acquire()      # 16 wywołań, co sekundę
        clock["t"] += 1.0
    assert slept == []         # w limicie — bez czekania

    limiter.acquire()          # 17. w tej samej minucie
    assert 40 <= sum(slept) <= 60  # czekał do zwolnienia okna minutowego


def test_limiter_enforces_second_window(monkeypatch, tmp_path):
    """9 pobrań w tej samej sekundzie — dziewiąte czeka ułamek sekundy."""
    clock, slept = _fake_clock(monkeypatch, start=500.0)
    limiter = _limiter(tmp_path)
    for _ in range(8):
        limiter.acquire()
    assert slept == []
    limiter.acquire()          # 9. w tej samej sekundzie
    assert 0 < sum(slept) <= 1.0


def test_limiter_state_survives_new_instance(monkeypatch, tmp_path):
    """Limity liczone TRWALE: nowy klient (nowe uruchomienie) widzi zużyty budżet."""
    clock, slept = _fake_clock(monkeypatch)
    first = _limiter(tmp_path)
    for _ in range(16):
        first.acquire()
        clock["t"] += 1.0

    second = _limiter(tmp_path)   # "nowe uruchomienie" — ta sama baza quota
    assert second.remaining_in_window(60) == 0
    second.acquire()
    assert sum(slept) >= 40        # musiał czekać mimo świeżej instancji


def test_limiter_long_wait_raises_quota_exceeded(monkeypatch, tmp_path):
    """Blokada serwera dłuższa niż 3 min -> KsefQuotaExceeded zamiast zwisu."""
    import app.ksef_client as kc

    _fake_clock(monkeypatch)
    limiter = _limiter(tmp_path)
    limiter.set_block(1800)        # jak Retry-After: 1826 s z produkcji
    with pytest.raises(kc.KsefQuotaExceeded):
        limiter.acquire()
    assert limiter.remaining_in_window(60) == 0


@pytest.mark.live
def test_smoke_prod():
    """W3: smoke test produkcyjnego base URL (bez tokena). Uruchamiany ręcznie: pytest -m live"""
    from app.ksef_client import PROD_BASE_URL

    client = KsefClient(PROD_BASE_URL, "0000000000", "no-token")
    certs = client.smoke_test()
    assert any("KsefTokenEncryption" in str(c.get("usage")) for c in certs)


# --------------------------------------------------------------------------- #
# synchronizacja przyrostowa po dacie trwałego zapisu (PermanentStorage + HWM)
# — kontrakt wg docs MF: przyrostowe-pobieranie-faktur.md, hwm.md, OpenAPI
#   (dateRange.restrictToPermanentStorageHwmDate, permanentStorageHwmDate,
#   InvoiceMetadata.permanentStorageDate, zakres maks. 100 dni w UTC)


def _meta_ps(n: int, issue: str, stored: str) -> dict:
    m = _meta(n)
    m["issueDate"] = issue
    m["permanentStorageDate"] = stored
    return m


def _odpowiedz(invoices, hwm, *, has_more=False, truncated=False):
    return {
        "hasMore": has_more,
        "isTruncated": truncated,
        "permanentStorageHwmDate": hwm,
        "invoices": invoices,
    }


@respx.mock(base_url=BASE)
def test_query_since_od_punktu_do_teraz_ograniczone_do_hwm(respx_mock, mf_cert_b64):
    client = _authed_client(respx_mock, mf_cert_b64)
    bodies, params = [], []

    def responder(request):
        bodies.append(json.loads(request.content))
        params.append(dict(request.url.params))
        return httpx.Response(200, json=_odpowiedz(
            [_meta_ps(1, "2026-06-28", "2026-08-03T08:00:00.5+00:00")],
            "2026-08-05T10:00:00.1234567+00:00",
        ))

    respx_mock.post("/invoices/query/metadata").mock(side_effect=responder)
    now = datetime(2026, 8, 5, 10, 5, tzinfo=timezone.utc)
    wynik = client.query_metadata_since("Subject2", "2026-08-01T00:00:00+02:00", now=now)

    assert len(bodies) == 1
    zakres = bodies[0]["dateRange"]
    assert bodies[0]["subjectType"] == "Subject2"
    assert zakres["dateType"] == "PermanentStorage"
    assert zakres["from"] == "2026-08-01T00:00:00+02:00"  # punkt wysłany bez przeliczania
    assert "to" not in zakres  # KSeF bierze „teraz” (UTC)...
    assert zakres["restrictToPermanentStorageHwmDate"] is True  # ...przycięte do HWM
    assert params[0]["sortOrder"] == "Asc"
    assert wynik.hwm == "2026-08-05T10:00:00.1234567+00:00"
    assert wynik.since == "2026-08-01T00:00:00+02:00"
    assert not wynik.clamped
    [meta] = wynik.invoices
    assert meta.permanent_storage_date == "2026-08-03T08:00:00.5+00:00"
    assert meta.issue_date == "2026-06-28"


@respx.mock(base_url=BASE)
def test_query_since_paginacja_obciecie_i_hwm_z_ostatniej_odpowiedzi(respx_mock, mf_cert_b64):
    client = _authed_client(respx_mock, mf_cert_b64)
    h1, h2 = "2026-08-05T10:00:00+00:00", "2026-08-05T10:01:00+00:00"
    s2 = "2026-08-04T12:00:00.7654321+00:00"
    strony = [
        _odpowiedz([_meta_ps(1, "2026-07-01", "2026-08-02T08:00:00+00:00")], h1, has_more=True),
        _odpowiedz([_meta_ps(2, "2026-07-02", s2)], h1, has_more=True, truncated=True),
        _odpowiedz([_meta_ps(2, "2026-07-02", s2), _meta_ps(3, "2026-07-03", s2)], h2),
    ]
    bodies, params = [], []

    def responder(request):
        bodies.append(json.loads(request.content))
        params.append(dict(request.url.params))
        return httpx.Response(200, json=strony[len(bodies) - 1])

    respx_mock.post("/invoices/query/metadata").mock(side_effect=responder)
    now = datetime(2026, 8, 5, 10, 5, tzinfo=timezone.utc)
    wynik = client.query_metadata_since("Subject1", "2026-08-01T00:00:00+02:00", now=now)

    assert [p["pageOffset"] for p in params] == ["0", "1", "0"]
    # isTruncated: nowe zapytanie od daty trwałego zapisu ostatniego rekordu
    assert bodies[2]["dateRange"]["from"] == s2
    assert "to" not in bodies[2]["dateRange"]
    assert bodies[2]["dateRange"]["restrictToPermanentStorageHwmDate"] is True
    assert [m.invoice_number for m in wynik.invoices] == ["FV/1", "FV/2", "FV/3"]  # bez duplikatu
    assert wynik.hwm == h2  # HWM ostatniego zapytania, nie pierwszego


@respx.mock(base_url=BASE)
def test_query_since_punkt_starszy_niz_100_dni_dzieli_na_przylegajace_okna(respx_mock, mf_cert_b64):
    from app.ksef_client import _parse_ksef_datetime

    client = _authed_client(respx_mock, mf_cert_b64)
    bodies = []
    ostatni_hwm = "2026-09-28T11:59:00+00:00"

    def responder(request):
        body = json.loads(request.content)
        bodies.append(body)
        hwm = body["dateRange"].get("to") or ostatni_hwm  # dla starych okien HWM == to
        return httpx.Response(200, json=_odpowiedz([], hwm))

    respx_mock.post("/invoices/query/metadata").mock(side_effect=responder)
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    since = "2026-03-01T00:00:00+01:00"  # ~211 dni wstecz
    wynik = client.query_metadata_since("Subject2", since, now=now)

    zakresy = [(b["dateRange"]["from"], b["dateRange"].get("to")) for b in bodies]
    assert len(zakresy) == 3
    assert zakresy[0][0] == since
    assert zakresy[-1][1] is None  # ostatnie okno do „teraz” (HWM)
    for (_, do), (nastepne_od, _) in zip(zakresy, zakresy[1:]):
        assert do == nastepne_od  # okna przylegają — bez luk
    for od, do in zakresy:
        koniec = _parse_ksef_datetime(do) if do else now
        assert koniec - _parse_ksef_datetime(od) <= timedelta(days=100)
    assert all(b["dateRange"]["restrictToPermanentStorageHwmDate"] is True for b in bodies)
    assert all(b["dateRange"]["dateType"] == "PermanentStorage" for b in bodies)
    assert wynik.hwm == ostatni_hwm
    assert not wynik.clamped


@respx.mock(base_url=BASE)
def test_query_since_punkt_starszy_niz_limit_wstecz_przyciety(respx_mock, mf_cert_b64):
    from app.ksef_client import _parse_ksef_datetime

    client = _authed_client(respx_mock, mf_cert_b64)
    bodies = []

    def responder(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json=_odpowiedz([], body["dateRange"].get("to") or "2026-09-28T11:59:00+00:00"))

    respx_mock.post("/invoices/query/metadata").mock(side_effect=responder)
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

    wynik = client.query_metadata_since("Subject2", "2024-01-01T00:00:00+01:00", now=now)
    assert wynik.clamped
    assert _parse_ksef_datetime(wynik.since) == now - timedelta(days=360)
    assert bodies[0]["dateRange"]["from"] == wynik.since
    assert len(bodies) == 4  # 360 dni = 4 okna po <= 90 dni

    bodies.clear()
    wynik = client.query_metadata_since(
        "Subject2", "2026-01-01T00:00:00+01:00", max_lookback=timedelta(days=90), now=now
    )
    assert wynik.clamped
    assert _parse_ksef_datetime(wynik.since) == now - timedelta(days=90)
    assert len(bodies) == 1


@respx.mock(base_url=BASE)
def test_query_since_liczy_sie_do_limitu_zapytan_o_metadane(respx_mock, mf_cert_b64):
    client = _authed_client(respx_mock, mf_cert_b64)
    respx_mock.post("/invoices/query/metadata").respond(
        200, json=_odpowiedz([], "2026-09-28T11:59:00+00:00")
    )
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    client.query_metadata_since("Subject2", "2026-09-01T00:00:00+02:00", now=now)
    # to samo trwałe okno limitu co zwykłe zapytanie o metadane (20/h per NIP)
    assert len(client._quota.recent("5252445719", "metadata", 3600)) == 1


def test_query_since_nierozpoznany_punkt_to_blad_ksef():
    from app.ksef_client import KsefError

    client = KsefClient(BASE, "5252445719", "token-abc")
    with pytest.raises(KsefError, match="punkt"):
        client.query_metadata_since("Subject2", "wczoraj")
    with pytest.raises(KsefError, match="punkt"):
        client.query_metadata_since("Subject2", "2026-09-01T00:00:00")  # bez strefy


@respx.mock(base_url=BASE)
def test_budzet_metadanych_po_zapytaniu(respx_mock, mf_cert_b64):
    client = _authed_client(respx_mock, mf_cert_b64)
    assert client.metadata_budget_remaining() == 20
    respx_mock.post("/invoices/query/metadata").respond(
        200, json=_odpowiedz([], "2026-09-28T11:59:00+00:00")
    )
    client.query_metadata_since(
        "Subject2", "2026-09-01T00:00:00+02:00",
        now=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc),
    )
    assert client.metadata_budget_remaining() == 19


def _blad_21183():
    return {
        "exception": {
            "exceptionDetailList": [{
                "exceptionCode": 21183,
                "exceptionDescription": "Zakres filtrowania wykracza poza dostępny zakres danych.",
                "details": ["dateRange.from jest późniejsze niż PermanentStorageHwmDate."],
            }],
            "referenceNumber": "20260928-EE-0000000000-0000000000-00",
            "serviceCode": "x",
        }
    }


@respx.mock(base_url=BASE)
def test_query_since_od_pozniejszy_niz_hwm_to_nic_nowego(respx_mock, mf_cert_b64):
    """KSeF 21183 (dateRange.from > HWM): np. pierwszy eksport tuż po północy
    z „Od” = dziś — punkt startu jest późniejszy niż HWM. To nie błąd, tylko
    „nic nowego”: pusta lista, a nowy punkt = początek sprawdzenia."""
    client = _authed_client(respx_mock, mf_cert_b64)
    respx_mock.post("/invoices/query/metadata").respond(400, json=_blad_21183())
    now = datetime(2026, 9, 27, 22, 1, tzinfo=timezone.utc)

    wynik = client.query_metadata_since("Subject2", "2026-09-28T00:00:00+02:00", now=now)

    assert wynik.invoices == []
    assert wynik.hwm == "2026-09-28T00:00:00+02:00"
    assert wynik.since == "2026-09-28T00:00:00+02:00"


@respx.mock(base_url=BASE)
def test_query_since_inny_blad_400_nadal_jest_bledem(respx_mock, mf_cert_b64):
    from app.ksef_client import KsefError

    client = _authed_client(respx_mock, mf_cert_b64)
    respx_mock.post("/invoices/query/metadata").respond(
        400, json={"exception": {"exceptionDetailList": [{"exceptionCode": 21405}]}}
    )
    with pytest.raises(KsefError, match="21405"):
        client.query_metadata_since(
            "Subject2", "2026-09-01T00:00:00+02:00",
            now=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc),
        )


# --------------------------------------------------------------------------- #
# runda 4 (N3-LIMIT): sprawdzenie przerywane budżetem stron — wynik częściowy


@respx.mock(base_url=BASE)
def test_query_since_przed_strona_przerywa_i_zwraca_postep(respx_mock, mf_cert_b64):
    """Hook `przed_strona` pytany przed KAŻDYM zapytaniem (stroną); False =
    koniec bez zapytania. Wynik częściowy: faktury z odebranych stron,
    complete=False, progress = data trwałego zapisu ostatniej odebranej faktury
    (dateRange.from jest włącznie — faktury z tą samą datą wrócą przy
    następnym sprawdzeniu i odpadną w rejestrze)."""
    client = _authed_client(respx_mock, mf_cert_b64)
    s1, s2 = "2026-08-02T08:00:00+00:00", "2026-08-03T09:30:00.1234567+00:00"
    strony = [
        _odpowiedz([_meta_ps(1, "2026-07-01", s1), _meta_ps(2, "2026-07-02", s2)],
                   "2026-08-05T10:00:00+00:00", has_more=True),
        _odpowiedz([_meta_ps(3, "2026-07-03", "2026-08-04T08:00:00+00:00")], "2026-08-05T10:00:00+00:00"),
    ]
    bodies = []

    def responder(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=strony[len(bodies) - 1])

    respx_mock.post("/invoices/query/metadata").mock(side_effect=responder)
    pytania = []

    def przed_strona():
        pytania.append(1)
        return len(pytania) <= 1  # tylko jedna strona

    wynik = client.query_metadata_since(
        "Subject1", "2026-08-01T00:00:00+02:00",
        now=datetime(2026, 8, 5, 10, 5, tzinfo=timezone.utc), przed_strona=przed_strona,
    )
    assert len(bodies) == 1
    assert [m.invoice_number for m in wynik.invoices] == ["FV/1", "FV/2"]
    assert wynik.complete is False
    assert wynik.progress == s2
    assert wynik.hwm == "2026-08-05T10:00:00+00:00"  # z ostatniej odebranej strony


@respx.mock(base_url=BASE)
def test_query_since_bez_przerwania_complete(respx_mock, mf_cert_b64):
    client = _authed_client(respx_mock, mf_cert_b64)
    respx_mock.post("/invoices/query/metadata").respond(
        200, json=_odpowiedz([], "2026-09-28T11:59:00+00:00")
    )
    wynik = client.query_metadata_since(
        "Subject2", "2026-09-01T00:00:00+02:00",
        now=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc), przed_strona=lambda: True,
    )
    assert wynik.complete is True and wynik.progress is None


@respx.mock(base_url=BASE)
def test_query_since_przerwanie_na_granicy_okien_postep_to_koniec_okna(respx_mock, mf_cert_b64):
    """Punkt starszy niż 100 dni: okna przylegające. Pierwsze okno przeczytane
    w całości, drugie przerwane przed pierwszą stroną — postęp = koniec
    pierwszego okna (wszystko przed nim sprawdzone)."""
    client = _authed_client(respx_mock, mf_cert_b64)
    bodies = []

    def responder(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json=_odpowiedz([], body["dateRange"].get("to") or "x"))

    respx_mock.post("/invoices/query/metadata").mock(side_effect=responder)
    pytania = []

    def przed_strona():
        pytania.append(1)
        return len(pytania) <= 1

    wynik = client.query_metadata_since(
        "Subject2", "2026-05-01T00:00:00+00:00",
        now=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc), przed_strona=przed_strona,
    )
    assert len(bodies) == 1
    assert wynik.complete is False
    assert wynik.progress == bodies[0]["dateRange"]["to"]


def test_query_since_limit_w_trakcie_to_wynik_czesciowy(monkeypatch):
    """Limit KSeF (KsefQuotaExceeded) przy kolejnej stronie: nie wyjątek, tylko
    wynik częściowy — zakres wybrany przez księgową nie ucierpi."""
    from app.ksef_client import InvoiceMetadata, KsefQuotaExceeded

    client = KsefClient(BASE, "5252445719", "token-abc")

    def okno(subject_type, iso_from, iso_to, date_type, seen, *, restrict_hwm=False,
             stan=None, przed_zapytaniem=None):
        stan["hwm"] = "2026-08-05T10:00:00+00:00"
        yield InvoiceMetadata(
            ksef_number="K1", invoice_number="FV/1", issue_date="2026-07-01",
            invoicing_date=None, acquisition_date=None, seller_nip="", seller_name="",
            buyer_id="", buyer_name="", net=None, gross=None, vat=None, currency="",
            form_system_code="", invoice_type="",
            permanent_storage_date="2026-08-02T08:00:00+00:00",
        )
        raise KsefQuotaExceeded("zapytań o metadane", 0)

    monkeypatch.setattr(client, "_query_window", okno)
    wynik = client.query_metadata_since(
        "Subject2", "2026-08-01T00:00:00+00:00",
        now=datetime(2026, 8, 5, 10, 5, tzinfo=timezone.utc),
    )
    assert [m.ksef_number for m in wynik.invoices] == ["K1"]
    assert wynik.complete is False
    assert wynik.progress == "2026-08-02T08:00:00+00:00"
