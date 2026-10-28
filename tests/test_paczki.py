"""Paczki eksportu KSeF przy dużych wolumenach: części, `_metadata.json`,
ucięcie paczki (isTruncated), wygasłe URL-e, limity statusu, faza 1 eksportu
paczkami. Kontrakt wg dokumentacji MF (ksef-docs, open-api.json, 09.2026)."""

import base64
import json
import os
import random
from datetime import date

import httpx
import pytest
import respx

from app.ksef_client import InvoiceMetadata, KsefClient, KsefError
from sim_ksef import (
    Faktura,
    opis_czesci,
    potnij,
    surowe_metadane,
    szyfruj,
    zip_paczki,
)

BASE = "https://ksef.example/v2"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("app.ksef_client._sleep", lambda seconds, cancel=None: None)


def _klient_z_kluczem(ref="EXP-1"):
    client = KsefClient(BASE, "5252445719", "tok")
    client._access_token = "x"
    client._access_exp = 2**33
    klucz, iv = os.urandom(32), os.urandom(16)
    client._export_keys[ref] = (klucz, iv)
    return client, klucz, iv


def _faktury(n, dzien=date(2026, 10, 5)):
    from datetime import datetime, timezone

    return [
        Faktura(f"5252445719-{dzien:%Y%m%d}-A{i:05d}", "Subject1", dzien,
                datetime(2026, 10, 5, 10, i % 60, tzinfo=timezone.utc))
        for i in range(n)
    ]


def _xml(f):
    return f"<Faktura nr='{f.ksef}'>{'x' * 300}</Faktura>".encode()


def _zbuduj_pakiet(respx_mock, klucz, iv, faktury, *, rozmiar_czesci, seed=3,
                   wersja=1, odwroc=False, metadata_json=None, bez_metadanych=False):
    archiwum = zip_paczki(
        {f.ksef: _xml(f) for f in faktury},
        None if bez_metadanych else [surowe_metadane(f) for f in faktury],
        metadata_json=metadata_json,
    )
    jawne = potnij(archiwum, rozmiar_czesci, random.Random(seed))
    czesci = []
    for i, jawny in enumerate(jawne, start=1):
        blob = szyfruj(jawny, klucz, iv)
        url = f"https://blob.example/EXP-1/{i}?v={wersja}"
        respx_mock.get(url).respond(200, content=blob)
        czesci.append(opis_czesci(blob, jawny, i, url))
    if odwroc:
        czesci.reverse()
    return {"invoiceCount": len(faktury), "isTruncated": False, "parts": czesci}, archiwum


# --------------------------------------------------------------------------- #
# L1: części paczki — jedno archiwum cięte na kawałki, szyfrowane osobno


@respx.mock(assert_all_called=False)
def test_paczka_wieloczesciowa_cieta_w_przypadkowym_bajcie(respx_mock):
    """Części to kawałki JEDNEGO archiwum (granica w dowolnym bajcie) —
    odszyfrowujemy każdą, sklejamy wg ordinalNumber i rozpakowujemy raz."""
    client, klucz, iv = _klient_z_kluczem()
    faktury = _faktury(40)
    pakiet, _ = _zbuduj_pakiet(respx_mock, klucz, iv, faktury, rozmiar_czesci=2000, odwroc=True)
    assert len(pakiet["parts"]) >= 4

    with client.otworz_paczke("EXP-1", pakiet) as paczka:
        assert sorted(paczka.numery_xml()) == sorted(f.ksef for f in faktury)
        for f in faktury:
            assert paczka.xml(f.ksef) == _xml(f)
        meta = paczka.wszystkie_metadane()
    assert [m.ksef_number for m in meta] == [f.ksef for f in faktury]


@respx.mock(assert_all_called=False)
def test_iter_export_invoices_paczka_wieloczesciowa(respx_mock):
    """Dotychczasowe API (mała paczka w starej ścieżce) też skleja części."""
    client, klucz, iv = _klient_z_kluczem()
    faktury = _faktury(25)
    pakiet, _ = _zbuduj_pakiet(respx_mock, klucz, iv, faktury, rozmiar_czesci=1500)
    assert len(pakiet["parts"]) >= 3
    wynik = dict(client.iter_export_invoices("EXP-1", pakiet))
    assert wynik == {f.ksef: _xml(f) for f in faktury}


@respx.mock(assert_all_called=False)
def test_paczka_wygasly_url_czesci_odswieza_status_i_ponawia(respx_mock):
    """URL części ważny ok. 1 h, generowany przy każdym GET statusu: 403 ->
    ponowny GET statusu (nowe URL-e) i jedna ponowna próba tej części."""
    client, klucz, iv = _klient_z_kluczem()
    faktury = _faktury(30)
    pakiet, _ = _zbuduj_pakiet(respx_mock, klucz, iv, faktury, rozmiar_czesci=2000)
    nowy, _ = _zbuduj_pakiet(respx_mock, klucz, iv, faktury, rozmiar_czesci=2000, wersja=2)
    # druga część starego URL-a już wygasła
    stary_url = pakiet["parts"][1]["url"]
    respx_mock.get(stary_url).respond(403, text="AuthenticationFailed")
    status = respx_mock.get(f"{BASE}/invoices/exports/EXP-1").respond(
        200, json={"status": {"code": 200}, "package": nowy})

    with client.otworz_paczke("EXP-1", pakiet) as paczka:
        assert sorted(paczka.numery_xml()) == sorted(f.ksef for f in faktury)
        assert paczka.xml(faktury[-1].ksef) == _xml(faktury[-1])
    assert status.call_count == 1


@respx.mock(assert_all_called=False)
def test_paczka_url_wygasly_takze_po_odswiezeniu_to_blad(respx_mock):
    client, klucz, iv = _klient_z_kluczem()
    faktury = _faktury(10)
    pakiet, _ = _zbuduj_pakiet(respx_mock, klucz, iv, faktury, rozmiar_czesci=10_000)
    respx_mock.get(pakiet["parts"][0]["url"]).respond(410)
    respx_mock.get(f"{BASE}/invoices/exports/EXP-1").respond(
        200, json={"status": {"code": 200}, "package": pakiet})
    with pytest.raises(KsefError, match="część"):
        client.otworz_paczke("EXP-1", pakiet)


@respx.mock(assert_all_called=False)
def test_paczka_zla_suma_kontrolna_czesci_to_blad(respx_mock):
    client, klucz, iv = _klient_z_kluczem()
    pakiet, _ = _zbuduj_pakiet(respx_mock, klucz, iv, _faktury(5), rozmiar_czesci=10_000)
    pakiet["parts"][0]["encryptedPartHash"] = base64.b64encode(b"0" * 32).decode()
    with pytest.raises(KsefError, match="[Ss]uma kontrolna"):
        client.otworz_paczke("EXP-1", pakiet)


@respx.mock(assert_all_called=False)
def test_paczka_uszkodzone_archiwum_to_blad_ksef(respx_mock):
    """Sklejenie w złej kolejności / brak części -> czytelny KsefError, nie BadZipFile."""
    client, klucz, iv = _klient_z_kluczem()
    pakiet, _ = _zbuduj_pakiet(respx_mock, klucz, iv, _faktury(30), rozmiar_czesci=2000)
    del pakiet["parts"][1]
    with pytest.raises(KsefError):
        client.otworz_paczke("EXP-1", pakiet)


@respx.mock(assert_all_called=False)
def test_paczka_niezgodna_liczba_faktur_to_blad(respx_mock):
    client, klucz, iv = _klient_z_kluczem()
    pakiet, _ = _zbuduj_pakiet(respx_mock, klucz, iv, _faktury(4), rozmiar_czesci=10_000)
    pakiet["invoiceCount"] = 5
    with pytest.raises(KsefError, match="invoiceCount"):
        client.otworz_paczke("EXP-1", pakiet)


def test_paczka_pusta_bez_czesci():
    """Okno bez faktur: KSeF może zwrócić paczkę z invoiceCount=0 bez części."""
    client, _, _ = _klient_z_kluczem()
    with client.otworz_paczke("EXP-1", {"invoiceCount": 0, "isTruncated": False, "parts": []}) as p:
        assert p.numery_xml() == []
        assert p.wszystkie_metadane() == []


@respx.mock(assert_all_called=False)
def test_paczka_bez_metadata_json_to_blad_przy_odczycie_metadanych(respx_mock):
    client, klucz, iv = _klient_z_kluczem()
    pakiet, _ = _zbuduj_pakiet(respx_mock, klucz, iv, _faktury(3), rozmiar_czesci=10_000,
                               bez_metadanych=True)
    with client.otworz_paczke("EXP-1", pakiet) as paczka:
        assert len(paczka.numery_xml()) == 3
        with pytest.raises(KsefError, match="_metadata.json"):
            paczka.wszystkie_metadane()


@respx.mock(assert_all_called=False)
def test_paczka_xml_brakujacej_faktury_to_blad_ksef(respx_mock):
    client, klucz, iv = _klient_z_kluczem()
    pakiet, _ = _zbuduj_pakiet(respx_mock, klucz, iv, _faktury(3), rozmiar_czesci=10_000)
    with client.otworz_paczke("EXP-1", pakiet) as paczka:
        with pytest.raises(KsefError):
            paczka.xml("NIE-MA-TAKIEJ")


# --------------------------------------------------------------------------- #
# (h) równoważność pól tej samej faktury z query/metadata i z `_metadata.json`


@respx.mock(assert_all_called=False)
def test_metadane_z_paczki_rowne_metadanym_z_query(respx_mock):
    from datetime import datetime, timezone

    f = Faktura("5252445719-20261031-ABCDEF123456-7F", "Subject2", date(2026, 10, 31),
                datetime(2026, 10, 31, 22, 30, 5, 123456, tzinfo=timezone.utc))
    surowe = surowe_metadane(f)
    surowe["acquisitionDate"] = "2026-10-31T22:30:04.1234567+00:00"   # 7 cyfr ułamka, jak KSeF
    surowe["formCode"] = {"systemCode": "FA (3)", "schemaVersion": "1-0E", "value": "FA"}

    client, klucz, iv = _klient_z_kluczem()
    respx_mock.post(f"{BASE}/invoices/query/metadata").respond(
        200, json={"hasMore": False, "isTruncated": False, "invoices": [surowe]})
    z_query = list(client.query_metadata("Subject2", date(2026, 10, 1), date(2026, 10, 31)))

    pakiet, _ = _zbuduj_pakiet(respx_mock, klucz, iv, [f], rozmiar_czesci=10_000,
                               metadata_json=json.dumps({"invoices": [surowe]}).encode())
    with client.otworz_paczke("EXP-1", pakiet) as paczka:
        z_paczki = paczka.wszystkie_metadane()

    assert len(z_query) == len(z_paczki) == 1
    q, p = z_query[0], z_paczki[0]
    assert isinstance(p, InvoiceMetadata)
    assert p == q
    assert p.acquisition_date == surowe["acquisitionDate"]
    assert p.permanent_storage_date == surowe["permanentStorageDate"]
    assert p.form_system_code == "FA (3)"
    # data wpływu liczona w czasie polskim: 22:30 UTC 31.10 (zima, +01) = 23:30 31.10
    assert p.ksef_date == q.ksef_date == "2026-10-31"


# --------------------------------------------------------------------------- #
# L4: status paczki — osobna pula limitów, odpytywanie z rosnącym interwałem


def _zegar(monkeypatch, start=10_000.0):
    from types import SimpleNamespace

    import app.ksef_client as kc
    import app.quota as q

    clock = {"t": start}
    fake = SimpleNamespace(time=lambda: clock["t"], monotonic=lambda: clock["t"],
                           sleep=lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(kc, "time", fake)
    monkeypatch.setattr(q, "time", fake)
    slept = []

    def fake_sleep(seconds, cancel=None):
        slept.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(kc, "_sleep", fake_sleep)
    return clock, slept


def _klient_quota(tmp_path):
    from app.quota import QuotaStore

    quota = QuotaStore(tmp_path / "q.db")
    client = KsefClient(BASE, "5252445719", "tok", quota=quota)
    client._access_token = "x"
    client._access_exp = 2**40
    client._export_keys["EXP-1"] = (os.urandom(32), os.urandom(16))
    return client, quota


@respx.mock(assert_all_called=False)
def test_429_na_statusie_nie_blokuje_zamawiania_paczek(respx_mock, monkeypatch, tmp_path):
    """Status paczki ma własną pulę (10/s, 60/min, 600/h) — długa blokada 429
    na statusie nie może blokować zlecania kolejnych paczek (pula 20/h)."""
    from app.ksef_client import KsefQuotaExceeded

    _zegar(monkeypatch)
    client, quota = _klient_quota(tmp_path)
    respx_mock.get(f"{BASE}/invoices/exports/EXP-1").respond(429, headers={"Retry-After": "1826"})
    with pytest.raises(KsefQuotaExceeded):
        client.wait_for_export("EXP-1")
    assert quota.block_remaining("5252445719", "export") == 0
    assert quota.block_remaining("5252445719", "export_status") > 1800


@respx.mock(assert_all_called=False)
def test_zlecenie_paczki_liczone_do_limitu_20_na_godzine(respx_mock, monkeypatch, tmp_path, mf_cert):
    """Zlecenie eksportu: pula 8/s, 16/min, 20/h per NIP liczona trwale —
    21. zlecenie w godzinie nie idzie do KSeF, tylko zgłasza limit."""
    from app.ksef_client import KsefQuotaExceeded

    clock, _ = _zegar(monkeypatch)
    client, _ = _klient_quota(tmp_path)
    respx_mock.get(f"{BASE}/security/public-key-certificates").respond(200, json=mf_cert)
    zlec = respx_mock.post(f"{BASE}/invoices/exports").respond(201, json={"referenceNumber": "EXP-9"})
    for _ in range(20):
        client.request_export("Subject1", date(2026, 10, 1), date(2026, 10, 31))
        clock["t"] += 61
    with pytest.raises(KsefQuotaExceeded):
        client.request_export("Subject1", date(2026, 10, 1), date(2026, 10, 31))
    assert zlec.call_count == 20


@respx.mock(assert_all_called=False)
def test_status_odpytywany_co_5_s_potem_rzadziej(respx_mock, monkeypatch, tmp_path):
    _, slept = _zegar(monkeypatch)
    client, _ = _klient_quota(tmp_path)
    odpowiedzi = iter([{"status": {"code": 100}}] * 20 + [
        {"status": {"code": 200}, "package": {"invoiceCount": 0, "parts": [], "isTruncated": False}}])
    respx_mock.get(f"{BASE}/invoices/exports/EXP-1").mock(
        side_effect=lambda r: httpx.Response(200, json=next(odpowiedzi)))
    client.wait_for_export("EXP-1")
    assert len(slept) == 20
    # pierwsza minuta co 5 s, potem co 15 s
    pierwsza_minuta = []
    t = 0.0
    for s in slept:
        if t < 60:
            pierwsza_minuta.append(s)
        t += s
    assert set(pierwsza_minuta) == {5.0}
    assert set(slept[len(pierwsza_minuta):]) == {15.0}


@respx.mock(assert_all_called=False)
def test_paczka_nie_gotowa_po_30_min_to_czytelny_blad(respx_mock, monkeypatch, tmp_path):
    clock, _ = _zegar(monkeypatch)
    start = clock["t"]
    client, _ = _klient_quota(tmp_path)
    status = respx_mock.get(f"{BASE}/invoices/exports/EXP-1").respond(
        200, json={"status": {"code": 100}})
    with pytest.raises(KsefError, match="30 min"):
        client.wait_for_export("EXP-1")
    assert 1800 <= clock["t"] - start <= 1800 + 20
    # rosnący interwał: ~12 odpytań w 1. minucie + ~116 w kolejnych 29 min
    assert status.call_count < 140


@pytest.fixture(scope="module")
def mf_cert():
    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MF Test")])
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(1)
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    der = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()
    return [{"certificate": der, "publicKeyId": "k1", "usage": ["SymmetricKeyEncryption"]}]


# --------------------------------------------------------------------------- #
# L2: zakres paczkami — generator per paczka, kontynuacja po ucięciu, okna,
# dedup po numerze KSeF; end-to-end przez prawdziwego klienta i symulator KSeF

from datetime import datetime, timedelta, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from sim_ksef import SerwerKsef, Zegar, generuj_miesiac, pl, zainstaluj_zegar  # noqa: E402

ZAPAS = timedelta(days=1)


@pytest.fixture
def swiat(monkeypatch, tmp_path):
    from app.quota import QuotaStore

    def _swiat(faktury, *, start=datetime(2026, 11, 5, 9, tzinfo=timezone.utc), **kw):
        zegar = Zegar(start)
        zainstaluj_zegar(zegar, monkeypatch)
        serwer = SerwerKsef(faktury, zegar, **kw)
        klient = serwer.klient(quota=QuotaStore(tmp_path / "q.db"))
        klient.authenticate()
        return SimpleNamespace(zegar=zegar, serwer=serwer, klient=klient)

    return _swiat


def _zbierz(klient, od, do, subject="Subject1"):
    paczki = []
    for p in klient.pobierz_paczkami(subject, od, do, margin=ZAPAS):
        for m in p.metadane:
            assert p.xml(m.ksef_number)          # XML dostępny leniwie z archiwum
        paczki.append([m.ksef_number for m in p.metadane])
    return paczki


@pytest.mark.parametrize("interpretacja", ["pl", "utc"])
def test_zakres_paczkami_uciecie_kontynuacja_bez_strat_i_dubli(swiat, interpretacja):
    faktury = (generuj_miesiac(2026, 10, 180, seed=2)
               + generuj_miesiac(2026, 9, 30, seed=3, prefiks="W")
               + generuj_miesiac(2026, 11, 8, seed=4, prefiks="N"))
    s = swiat(faktury, limit_paczki=50, interpretacja=interpretacja)
    paczki = _zbierz(s.klient, date(2026, 10, 1), date(2026, 10, 31))

    numery = [n for p in paczki for n in p]
    assert len(numery) == len(set(numery)), "dubel między paczkami"
    oczekiwane = {f.ksef for f in faktury if date(2026, 10, 1) <= f.issue <= date(2026, 10, 31)}
    assert oczekiwane <= set(numery), f"utracone: {sorted(oczekiwane - set(numery))[:5]}"
    assert len(paczki) >= 4
    assert all(z["dateRange"]["dateType"] == "Issue" for z in s.serwer.zlecenia)


def test_uciecie_w_srodku_dnia_data_wystawienia_jako_polnoc_pl(swiat):
    """(c) KSeF porównuje datę wystawienia jako północ czasu polskiego (22:00
    UTC dnia poprzedniego): kontynuacja od `lastIssueDate` bez zapasu
    zgubiłaby resztę dnia, na którym paczkę ucięto."""
    faktury = []
    for dzien in range(1, 11):
        d = date(2026, 10, dzien)
        faktury += [Faktura(f"5252445719-{d:%Y%m%d}-D{i:03d}", "Subject1", d, pl(d, 8 + i))
                    for i in range(12)]
    s = swiat(faktury, limit_paczki=30, interpretacja="pl")
    paczki = _zbierz(s.klient, date(2026, 10, 1), date(2026, 10, 10))
    numery = [n for p in paczki for n in p]
    assert sorted(numery) == sorted(f.ksef for f in faktury)
    # pierwsza paczka ucięta w środku 3.10 (30 = 2,5 dnia); drugie zlecenie
    # od dnia wcześniej (zapas), ten sam koniec
    e1 = s.serwer.eksporty["EXP-0001"]
    assert e1.obciety and e1.faktury[-1].issue == date(2026, 10, 3)
    z1, z2 = s.serwer.zlecenia[0]["dateRange"], s.serwer.zlecenia[1]["dateRange"]
    assert z2["from"] == "2026-10-02T00:00:00+00:00"
    assert z2["to"] == z1["to"]


def test_ponad_limit_paczki_w_jednym_dniu_to_czytelny_blad(swiat):
    d = date(2026, 10, 10)
    faktury = [Faktura(f"5252445719-20261010-X{i:03d}", "Subject1", d, pl(d, 9)) for i in range(25)]
    s = swiat(faktury, limit_paczki=10)
    with pytest.raises(KsefError, match="jednego dnia"):
        _zbierz(s.klient, date(2026, 10, 1), date(2026, 10, 31))
    assert len(s.serwer.zlecenia) <= 3   # bezpiecznik, nie pętla bez końca


def test_zakres_ponad_3_miesiace_okna_do_100_dni_i_dedup_na_granicy(swiat):
    faktury = []
    for mies in (7, 8, 9, 10, 11):
        faktury += generuj_miesiac(2026, mies, 20, seed=mies, prefiks=f"M{mies}")
    # faktury na granicy okien (30.09 / 1.10) — w zapasie obu okien
    faktury += [Faktura("5252445719-20260930-GR1", "Subject1", date(2026, 9, 30), pl(date(2026, 9, 30), 10)),
                Faktura("5252445719-20261001-GR2", "Subject1", date(2026, 10, 1), pl(date(2026, 10, 1), 10))]
    s = swiat(faktury, start=datetime(2026, 12, 20, 9, tzinfo=timezone.utc))
    paczki = _zbierz(s.klient, date(2026, 7, 1), date(2026, 11, 30))
    numery = [n for p in paczki for n in p]
    assert len(numery) == len(set(numery))
    assert {f.ksef for f in faktury} <= set(numery)
    assert len(s.serwer.zlecenia) == 2
    for z in s.serwer.zlecenia:
        od = datetime.fromisoformat(z["dateRange"]["from"])
        do = datetime.fromisoformat(z["dateRange"]["to"])
        assert do - od <= timedelta(days=100)


def test_zakres_paczkami_wiele_czesci_i_wygasly_url(swiat):
    """(d)+(e) paczka w wielu częściach ciętych w przypadkowym bajcie, URL
    jednej części wygasa — odświeżenie statusu i ponowienie."""
    faktury = generuj_miesiac(2026, 10, 120, seed=9)
    s = swiat(faktury, limit_paczki=100, rozmiar_czesci=6000)
    s.serwer.wygas_raz = {(1, 2), (2, 1)}
    paczki = _zbierz(s.klient, date(2026, 10, 1), date(2026, 10, 31))
    numery = [n for p in paczki for n in p]
    assert sorted(numery) == sorted(f.ksef for f in faktury)
    assert len(s.serwer.eksporty["EXP-0001"].czesci) >= 5
    assert not s.serwer.wygas_raz        # oba wygasłe URL-e trafione i obsłużone


def test_druga_paczka_nie_gotowa_pierwsza_juz_oddana(swiat):
    """(f) timeout drugiej paczki: pierwsza została oddana wołającemu (per
    paczka), potem czytelny błąd; w tym przebiegu nie zamawiamy kolejnej."""
    faktury = generuj_miesiac(2026, 10, 90, seed=5)
    s = swiat(faktury, limit_paczki=50)
    s.serwer.nigdy_gotowe = {2}
    oddane = []
    with pytest.raises(KsefError, match="30 min"):
        for p in s.klient.pobierz_paczkami("Subject1", date(2026, 10, 1), date(2026, 10, 31), margin=ZAPAS):
            oddane.append(len(p.metadane))
    assert oddane == [50]
    assert len(s.serwer.zlecenia) == 2


def test_paczka_z_xml_bez_wpisu_w_metadata_json_to_blad(swiat):
    faktury = generuj_miesiac(2026, 10, 20, seed=6)
    s = swiat(faktury)
    s.serwer.psuj_metadane = lambda m: m[1:]
    with pytest.raises(KsefError, match="_metadata.json"):
        _zbierz(s.klient, date(2026, 10, 1), date(2026, 10, 31))


# --------------------------------------------------------------------------- #
# L3: sonda — pierwsza strona metadanych rozstrzyga: lista (<= 250) czy paczki


def _zapytania_issue(serwer):
    return [z for z in serwer.zapytania_metadanych if z["body"]["dateRange"]["dateType"] == "Issue"]


def test_sonda_maly_zakres_pelny_wynik_jednym_zapytaniem(swiat):
    faktury = generuj_miesiac(2026, 10, 200, seed=11)
    s = swiat(faktury)
    sonda = {}
    wynik = list(s.klient.query_metadata("Subject1", date(2026, 10, 1), date(2026, 10, 31),
                                         margin=ZAPAS, sonda=sonda))
    assert not sonda.get("wiecej")
    assert sorted(m.ksef_number for m in wynik) == sorted(f.ksef for f in faktury)
    assert len(_zapytania_issue(s.serwer)) == 1


def test_sonda_duzy_zakres_konczy_po_pierwszej_stronie(swiat):
    faktury = generuj_miesiac(2026, 10, 600, seed=12)
    s = swiat(faktury)
    sonda = {}
    list(s.klient.query_metadata("Subject1", date(2026, 10, 1), date(2026, 10, 31),
                                 margin=ZAPAS, sonda=sonda))
    assert sonda.get("wiecej") is True
    assert len(_zapytania_issue(s.serwer)) == 1


def test_sonda_wiele_okien_konczy_na_pierwszym_duzym(swiat):
    faktury = (generuj_miesiac(2026, 7, 30, seed=13, prefiks="L")
               + generuj_miesiac(2026, 10, 400, seed=14))
    s = swiat(faktury, start=datetime(2026, 12, 20, 9, tzinfo=timezone.utc))
    sonda = {}
    list(s.klient.query_metadata("Subject1", date(2026, 7, 1), date(2026, 11, 30),
                                 margin=ZAPAS, sonda=sonda))
    assert sonda.get("wiecej") is True
    assert len(_zapytania_issue(s.serwer)) == 2   # okno lip–wrz (1 strona) + 1. strona paź–lis


def test_sonda_bez_obslugi_w_nadpisanym_oknie_to_zwykla_lista():
    """Symulatory/zaślepki nadpisujące `_query_window` bez nowego argumentu:
    sonda ignorowana, pełna lista jak dotąd (bez wyjątku)."""

    class Stary(KsefClient):
        def __init__(self):
            pass

        def _query_window(self, subject_type, iso_from, iso_to, date_type, seen, *,
                          restrict_hwm=False, stan=None, przed_zapytaniem=None):
            yield from ()

    sonda = {}
    assert list(Stary().query_metadata("Subject1", date(2026, 10, 1), date(2026, 10, 31),
                                       sonda=sonda)) == []
    assert not sonda.get("wiecej")


@pytest.mark.parametrize("last_issue", [
    lambda d: (d + timedelta(days=2)).isoformat(),   # KSeF podaje datę za późną
    lambda d: None,                                  # KSeF nie podaje daty
])
def test_kontynuacja_od_najwczesniejszej_z_lastissuedate_i_metadanych(swiat, last_issue):
    faktury = generuj_miesiac(2026, 10, 200, seed=15)
    s = swiat(faktury, limit_paczki=60)
    s.serwer.last_issue_date = last_issue
    paczki = _zbierz(s.klient, date(2026, 10, 1), date(2026, 10, 31))
    numery = [n for p in paczki for n in p]
    assert sorted(numery) == sorted(f.ksef for f in faktury)


# --------------------------------------------------------------------------- #
# runda poprawek po przeglądzie adwersarialnym


def test_powtorzony_numer_w_metadata_json_to_paczka_niespojna(swiat):
    """(A1) ta sama faktura dwa razy w `_metadata.json` — paczka niespójna:
    nie wiadomo, który wpis jest prawdziwy; błąd zamiast dubla w pliku."""
    faktury = generuj_miesiac(2026, 10, 30, seed=41)
    s = swiat(faktury)
    s.serwer.psuj_metadane = lambda m: m + [m[5]]
    with pytest.raises(KsefError, match="powtórzon"):
        _zbierz(s.klient, date(2026, 10, 1), date(2026, 10, 31))


@respx.mock(assert_all_called=False)
def test_powtorzona_nazwa_pliku_xml_w_archiwum_to_blad(respx_mock):
    import io
    import warnings
    import zipfile

    client, klucz, iv = _klient_z_kluczem()
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("NR-1.xml", b"<a>1</a>")
            z.writestr("NR-1.xml", b"<a>2</a>")
            z.writestr("_metadata.json", b'{"invoices": []}')
    blob = szyfruj(buf.getvalue(), klucz, iv)
    respx_mock.get("https://blob.example/d/1").respond(200, content=blob)
    pakiet = {"invoiceCount": 2, "parts": [opis_czesci(blob, buf.getvalue(), 1, "https://blob.example/d/1")]}
    with pytest.raises(KsefError, match="powtórzon"):
        client.otworz_paczke("EXP-1", pakiet)


def test_pelna_paczka_bez_isTruncated_traktowana_jak_ucieta(swiat, monkeypatch):
    """(A2) paczka z liczbą faktur równą limitowi (10 000), ale bez isTruncated
    i lastIssueDate — kontynuacja od najpóźniejszej daty w `_metadata.json`
    zamiast cichej straty reszty zakresu."""
    import app.ksef_client as kc

    monkeypatch.setattr(kc, "PACZKA_PELNA_FAKTUR", 50)
    faktury = generuj_miesiac(2026, 10, 180, seed=42)
    s = swiat(faktury, limit_paczki=50)
    s.serwer.ukryj_uciecie = True
    paczki = _zbierz(s.klient, date(2026, 10, 1), date(2026, 10, 31))
    numery = [n for p in paczki for n in p]
    assert sorted(numery) == sorted(f.ksef for f in faktury)
    assert len(s.serwer.zlecenia) >= 4


def test_paczka_blisko_limitu_rozmiaru_bez_isTruncated_traktowana_jak_ucieta(swiat, monkeypatch):
    """Paczka ucięta na limicie rozmiaru (1 GB) bez isTruncated — ta sama reguła."""
    import app.ksef_client as kc

    faktury = generuj_miesiac(2026, 10, 180, seed=43)
    s = swiat(faktury)
    _zbierz(s.klient, date(2026, 10, 1), date(2026, 10, 31))          # pomiar rozmiaru
    rozmiar = len(b"".join(s.serwer.eksporty["EXP-0001"].czesci_jawne))
    assert len(s.serwer.zlecenia) == 1
    # próg = rozmiar paczki całego miesiąca: uznana za pełną, kontynuacja od
    # przedostatniego dnia (mała paczka, poniżej progu) kończy pętlę
    monkeypatch.setattr(kc, "PACZKA_PELNA_BAJTOW", rozmiar)
    paczki = _zbierz(s.klient, date(2026, 10, 1), date(2026, 10, 31))
    assert sorted(n for p in paczki for n in p) == sorted(f.ksef for f in faktury)
    assert len(s.serwer.zlecenia) == 3
    assert s.serwer.zlecenia[2]["dateRange"]["from"] == "2026-10-30T00:00:00+00:00"


def test_uszkodzony_deflate_w_metadata_json_to_blad_ksef():
    """(5) zlib.error przy czytaniu `_metadata.json` -> KsefError (paczka
    niespójna, rodzaj niepełny), nie awaria całego eksportu."""
    import io
    import zipfile

    from app.ksef_client import PaczkaEksportu

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("_metadata.json", json.dumps({"invoices": [{"ksefNumber": f"N{i}"} for i in range(200)]}))
    dane = bytearray(buf.getvalue())
    info = zipfile.ZipFile(io.BytesIO(bytes(dane))).infolist()[0]
    start = info.header_offset + 30 + len(info.filename.encode())
    dane[start:start + 8] = b"\xff" * 8                   # nieprawidłowy blok deflate
    plik = io.BytesIO(bytes(dane))
    with PaczkaEksportu("EXP-1", {}, plik, zipfile.ZipFile(plik)) as p:
        with pytest.raises(KsefError):
            p.wszystkie_metadane()


@respx.mock(assert_all_called=False)
def test_czesci_zamienione_miejscami_wykrywa_kontrola_crc(respx_mock):
    """(6) części z poprawnymi sumami, ale sklejone w złej kolejności: katalog
    ZIP się otwiera (zipfile toleruje przesunięcie) — wykrywa to dopiero pełna
    kontrola CRC archiwum."""
    client, klucz, iv = _klient_z_kluczem()
    faktury = _faktury(40)
    archiwum = zip_paczki({f.ksef: _xml(f) for f in faktury}, [surowe_metadane(f) for f in faktury])
    jawne = potnij(archiwum, 2000, random.Random(5))
    assert len(jawne) >= 4
    jawne[1], jawne[2] = jawne[2], jawne[1]
    czesci = []
    for i, jawny in enumerate(jawne, start=1):
        blob = szyfruj(jawny, klucz, iv)
        url = f"https://blob.example/zam/{i}"
        respx_mock.get(url).respond(200, content=blob)
        czesci.append(opis_czesci(blob, jawny, i, url))
    with pytest.raises(KsefError, match="archiwum"):
        client.otworz_paczke("EXP-1", {"invoiceCount": 40, "parts": czesci})


@pytest.mark.parametrize("numeracja", [[1, 2, 4], [1, 2, 2, 3]])
@respx.mock(assert_all_called=False)
def test_niepelna_albo_zdublowana_numeracja_czesci_to_blad(respx_mock, numeracja):
    """(6) numeracja części niezgodna z protokołem (luka, dubel) — nie ufamy
    liście, nawet gdy sklejone archiwum dałoby się otworzyć."""
    client, klucz, iv = _klient_z_kluczem()
    faktury = _faktury(30)
    archiwum = zip_paczki({f.ksef: _xml(f) for f in faktury}, [surowe_metadane(f) for f in faktury])
    trzecia = len(archiwum) // 3 + 7          # granice w dowolnym bajcie
    jawne = [archiwum[:trzecia], archiwum[trzecia:2 * trzecia], archiwum[2 * trzecia:]]
    kawalki = {1: jawne[0], 2: jawne[1], 3: jawne[2], 4: jawne[2]}
    czesci = []
    for i, nr in enumerate(numeracja):
        blob = szyfruj(kawalki[nr], klucz, iv)
        url = f"https://blob.example/num/{i}"
        respx_mock.get(url).respond(200, content=blob)
        czesci.append(opis_czesci(blob, kawalki[nr], nr, url))
    with pytest.raises(KsefError, match="części"):
        client.otworz_paczke("EXP-1", {"invoiceCount": 30, "parts": czesci})


def test_paczka_bez_czesci_przy_niezerowym_invoiceCount_to_blad():
    client, _, _ = _klient_z_kluczem()
    with pytest.raises(KsefError, match="invoiceCount"):
        client.otworz_paczke("EXP-1", {"invoiceCount": 5, "isTruncated": False, "parts": []})


# --------------------------------------------------------------------------- #
# (4) niedokończone zlecenie paczki pamiętane między przebiegami


def test_magazyn_niedokonczonych_zlecen_zapis_odczyt_wygasanie(monkeypatch, tmp_path):
    from app.quota import QuotaStore

    clock, _ = _zegar(monkeypatch, start=1_000_000.0)
    q = QuotaStore(tmp_path / "q.db")
    q.save_pending_export("5252445719", "Subject1|Issue|2026-09-30|2026-11-01", "EXP-7", b"k" * 32, b"i" * 16)
    assert tuple(q.pending_export("5252445719", "Subject1|Issue|2026-09-30|2026-11-01"))[:4] == (
        "EXP-7", b"k" * 32, b"i" * 16, 1_000_000.0)
    assert q.pending_export("1111111111", "Subject1|Issue|2026-09-30|2026-11-01") is None
    assert q.pending_export("5252445719", "Subject2|Issue|2026-09-30|2026-11-01") is None
    q.close()
    q = QuotaStore(tmp_path / "q.db")                    # nowe uruchomienie aplikacji
    assert q.pending_export("5252445719", "Subject1|Issue|2026-09-30|2026-11-01")[0] == "EXP-7"
    clock["t"] += 6 * 3600 + 1                          # wpis żyje najwyżej 6 h
    assert q.pending_export("5252445719", "Subject1|Issue|2026-09-30|2026-11-01") is None
    q.save_pending_export("5252445719", "K", "EXP-8", b"k" * 32, b"i" * 16)
    q.drop_pending_export("5252445719", "K")
    assert q.pending_export("5252445719", "K") is None


def _nowy_przebieg(s, tmp_path, cancel=None):
    from app.quota import QuotaStore

    klient = s.serwer.klient(quota=QuotaStore(tmp_path / "q.db"), cancel=cancel)
    klient.authenticate()
    return klient


def test_paczka_nie_gotowa_w_30_min_odbierana_w_kolejnym_przebiegu(swiat, tmp_path):
    """KSeF przygotowuje paczkę dłużej niż 30 min: pierwszy przebieg kończy się
    timeoutem, a kolejny odbiera TO SAMO zlecenie zamiast zamawiać nowe (inaczej
    każdy przebieg zamawiałby paczkę od nowa i nigdy by jej nie dostał)."""
    faktury = generuj_miesiac(2026, 10, 300, seed=44)
    s = swiat(faktury)
    s.serwer.czas_przygotowania_nr = {1: timedelta(minutes=45)}
    with pytest.raises(KsefError, match="30 min"):
        _zbierz(s.klient, date(2026, 10, 1), date(2026, 10, 31))
    s.zegar.teraz += timedelta(minutes=20)
    klient2 = _nowy_przebieg(s, tmp_path)
    paczki = _zbierz(klient2, date(2026, 10, 1), date(2026, 10, 31))
    assert sorted(n for p in paczki for n in p) == sorted(f.ksef for f in faktury)
    assert len(s.serwer.zlecenia) == 1
    klucz = "Subject1|Issue|2026-09-30|2026-11-01"
    assert klient2._quota.pending_export("5252445719", klucz) is None   # odebrane -> usunięte


def test_zapamietane_zlecenie_wygasle_w_ksef_zamawia_nowa_paczke(swiat, tmp_path):
    faktury = generuj_miesiac(2026, 10, 300, seed=45)
    s = swiat(faktury)
    s.serwer.nigdy_gotowe = {1}
    with pytest.raises(KsefError, match="30 min"):
        _zbierz(s.klient, date(2026, 10, 1), date(2026, 10, 31))
    s.serwer.wygasle = {1}
    paczki = _zbierz(_nowy_przebieg(s, tmp_path), date(2026, 10, 1), date(2026, 10, 31))
    assert sorted(n for p in paczki for n in p) == sorted(f.ksef for f in faktury)
    assert len(s.serwer.zlecenia) == 2


def test_zapamietane_zlecenie_starsze_niz_doba_nie_jest_odbierane(swiat, tmp_path):
    """Zlecenie wciąż „w toku” po dobie traktujemy jak zawieszone — nowe
    zamówienie zamiast czekania 30 min w każdym eksporcie przez 7 dni."""
    faktury = generuj_miesiac(2026, 10, 300, seed=46)
    s = swiat(faktury)
    s.serwer.nigdy_gotowe = {1}
    with pytest.raises(KsefError, match="30 min"):
        _zbierz(s.klient, date(2026, 10, 1), date(2026, 10, 31))
    s.zegar.teraz += timedelta(hours=25)
    _zbierz(_nowy_przebieg(s, tmp_path), date(2026, 10, 1), date(2026, 10, 31))
    assert len(s.serwer.zlecenia) == 2
    assert s.serwer.wywolania["GET /invoices/exports/{ref}"] > 0


@pytest.mark.parametrize("awaria", ["wygasla", "zla_suma"])
def test_blad_odbioru_inny_niz_timeout_usuwa_zapamietane_zlecenie(swiat, tmp_path, awaria):
    """Zlecenie, którego nie da się odebrać (eksport wygasł w KSeF, uszkodzona
    część), nie może zostać do odbioru — kolejny eksport zamawia nową paczkę."""
    faktury = generuj_miesiac(2026, 10, 300, seed=47)
    s = swiat(faktury)
    if awaria == "wygasla":
        s.serwer.wygasle = {1}
    else:
        s.serwer.po_zleceniu = lambda nr: s.serwer.eksporty[f"EXP-{nr:04d}"].czesci.__setitem__(
            0, b"x" * 32)
    with pytest.raises(KsefError):
        _zbierz(s.klient, date(2026, 10, 1), date(2026, 10, 31))
    assert s.klient._quota.pending_export("5252445719", "Subject1|Issue|2026-09-30|2026-11-01") is None


# --------------------------------------------------------------------------- #
# runda 3: zawieszone zlecenie (R7), zegar zamówienia z KSeF (R3), błąd sieci (R-a)

KLUCZ_PAZ = "Subject1|Issue|2026-09-30|2026-11-01"


def test_zawieszone_zlecenie_ponowny_timeout_usuwa_wpis_dopiero_po_3_5_h(swiat, tmp_path):
    """(R7/P1) stare zlecenie znów niegotowe przy odbiorze: młodsze niż 3,5 h
    (zegar KSeF) — wpis zostaje (wolny KSeF ma czas przygotować paczkę);
    starsze — uznane za zawieszone, wpis usunięty, kolejny przebieg zamawia
    nową paczkę."""
    faktury = generuj_miesiac(2026, 10, 300, seed=71)
    s = swiat(faktury)
    s.serwer.nigdy_gotowe = {1}
    with pytest.raises(KsefError, match="30 min"):
        _zbierz(s.klient, date(2026, 10, 1), date(2026, 10, 31))
    for _ in range(2):                                   # przebiegi co godzinę, do 2,5 h
        s.zegar.teraz += timedelta(minutes=30)
        k = _nowy_przebieg(s, tmp_path)
        with pytest.raises(KsefError, match="kolejny eksport odbierze|30 min"):
            _zbierz(k, date(2026, 10, 1), date(2026, 10, 31))
        assert k._quota.pending_export("5252445719", KLUCZ_PAZ) is not None
    s.zegar.teraz += timedelta(minutes=30)               # odbiór kończy się po 3,5 h od zamówienia
    k = _nowy_przebieg(s, tmp_path)
    with pytest.raises(KsefError, match="zawieszone"):
        _zbierz(k, date(2026, 10, 1), date(2026, 10, 31))
    assert k._quota.pending_export("5252445719", KLUCZ_PAZ) is None
    assert len(s.serwer.zlecenia) == 1
    s.zegar.teraz += timedelta(minutes=30)
    paczki = _zbierz(_nowy_przebieg(s, tmp_path), date(2026, 10, 1), date(2026, 10, 31))
    assert sorted(n for p in paczki for n in p) == sorted(f.ksef for f in faktury)
    assert len(s.serwer.zlecenia) == 2


def test_zlecenie_starsze_niz_6_godzin_nie_jest_odbierane(swiat, tmp_path):
    faktury = generuj_miesiac(2026, 10, 300, seed=72)
    s = swiat(faktury)
    s.serwer.czas_przygotowania_nr = {1: timedelta(hours=8)}
    with pytest.raises(KsefError, match="30 min"):
        _zbierz(s.klient, date(2026, 10, 1), date(2026, 10, 31))
    s.zegar.teraz += timedelta(hours=5, minutes=31)       # 6 h 1 min od zamówienia
    _zbierz(_nowy_przebieg(s, tmp_path), date(2026, 10, 1), date(2026, 10, 31))
    assert len(s.serwer.zlecenia) == 2


def test_zerwane_polaczenie_przy_statusie_zostawia_wpis_zlecenia(swiat):
    """(R-a) błąd sieci to nie błąd paczki — zlecenie żyje w KSeF i czeka na
    odbiór w kolejnym przebiegu."""
    import httpx

    faktury = generuj_miesiac(2026, 10, 300, seed=73)
    s = swiat(faktury)
    s.serwer.zerwij_status = {1}
    with pytest.raises(httpx.TransportError):
        _zbierz(s.klient, date(2026, 10, 1), date(2026, 10, 31))
    assert s.klient._quota.pending_export("5252445719", KLUCZ_PAZ) is not None


@pytest.mark.parametrize("przesuniecie_h", [2, -2])
def test_moment_zamowienia_z_zegara_ksef_nie_komputera(swiat, tmp_path, monkeypatch, przesuniecie_h):
    """(R3) paczka odebrana w kolejnym przebiegu niesie moment zamówienia wg
    zegara KSeF (nagłówek Date odpowiedzi na zlecenie) — zegar komputera
    przesunięty o ±2 h nie zmienia go."""
    import app.ksef_client as kc
    import app.quota as q

    faktury = generuj_miesiac(2026, 10, 300, seed=74)
    s = swiat(faktury)
    s.serwer.czas_przygotowania_nr = {1: timedelta(minutes=45)}
    zamowiono = s.zegar.teraz
    przes = przesuniecie_h * 3600.0
    from types import SimpleNamespace

    zly = SimpleNamespace(time=lambda: s.zegar.time() + przes, monotonic=s.zegar.monotonic,
                          sleep=s.zegar.sleep)
    monkeypatch.setattr(kc, "time", zly)
    monkeypatch.setattr(q, "time", zly)
    with pytest.raises(KsefError, match="30 min"):
        _zbierz(s.klient, date(2026, 10, 1), date(2026, 10, 31))
    s.zegar.teraz += timedelta(minutes=30)
    k2 = _nowy_przebieg(s, tmp_path)
    paczki = list(k2.pobierz_paczkami("Subject1", date(2026, 10, 1), date(2026, 10, 31), margin=ZAPAS))
    [p] = paczki
    assert p.zamowiona_zegar_ksef
    assert abs((p.zamowiona - zamowiono).total_seconds()) <= 2
