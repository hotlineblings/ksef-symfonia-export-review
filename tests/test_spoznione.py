"""Spóźnione faktury (decyzja właściciela: „dołączać z ostrzeżeniem”).

Faktura z datą wystawienia w miesiącu M, która dostaje numer KSeF PO eksporcie
M (tryb offline, spóźniony wystawca), nie trafia do eksportu po dacie
wystawienia żadnego późniejszego zakresu. Przy każdym eksporcie aplikacja pyta
KSeF, co wpłynęło (data trwałego zapisu, PermanentStorage) od jej ostatniego
pobrania dla tej firmy i rodzaju, i dokłada do TEGO SAMEGO pliku faktury
z wcześniejszych okresów, których nie ma w rejestrze pobranych — wypisując je
osobno w ostrzeżeniach i w podsumowaniu. Punkt kontynuacji (HWM z KSeF) jest
trwały per (NIP, rodzaj) i przesuwa się tylko po udanym przebiegu.

Bez sieci: stub klienta KSeF.
"""

import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Profile
from app.exporter import ExportReport, Kind, SpoznionaFaktura, run_export
from app.kontrahenci_nr import RejestrNumerow
from app.ksef_client import (
    InvoiceMetadata,
    KsefError,
    KsefQuotaExceeded,
    PermanentStorageQuery,
)
from app.ledger import Ledger

FIXTURES = Path(__file__).resolve().parent / "fixtures"
XML = (FIXTURES / "fa3_basic_23.xml").read_bytes()
NIP = "5252445719"
HWM = "2026-08-05T10:00:00.1234567+00:00"


def _meta(nr, *, issue="2026-07-15", stored="2026-07-15T09:00:00+00:00", invoice_number=None):
    return InvoiceMetadata(
        ksef_number=nr,
        invoice_number=invoice_number or f"FV/{nr}",
        issue_date=issue,
        invoicing_date=None,
        acquisition_date=stored,
        seller_nip="1180012345",
        seller_name="Dostawca Testowy Sp. z o.o.",
        buyer_id=NIP,
        buyer_name="Nasza Firma Sp. z o.o.",
        net=None, gross=None, vat=None,
        currency="PLN",
        form_system_code="FA (3)",
        invoice_type="Vat",
        permanent_storage_date=stored,
    )


class _SyncStub:
    """KSeF: główne zapytanie (po dacie wystawienia) + zapytanie „od punktu”
    (po dacie trwałego zapisu) — osobne listy per rodzaj (subject_type)."""

    def __init__(self, glowne, od_punktu, *, hwm=HWM, clamped=False, blad_since=None,
                 bledy_pobrania=None, on_download=None):
        self._glowne = glowne
        self._od_punktu = od_punktu
        self._hwm = hwm
        self._clamped = clamped
        self._blad_since = blad_since
        self._bledy_pobrania = bledy_pobrania or {}
        self._on_download = on_download
        self.since_calls = []
        self.pobrane = []

    def authenticate(self):
        pass

    def query_metadata(self, subject_type, date_from, date_to, *, margin=timedelta(0)):
        yield from self._glowne.get(subject_type, [])

    def query_metadata_since(self, subject_type, since, *, max_lookback=None):
        self.since_calls.append((subject_type, since))
        if self._blad_since is not None:
            raise self._blad_since
        hwm = self._hwm.get(subject_type) if isinstance(self._hwm, dict) else self._hwm
        return PermanentStorageQuery(
            invoices=list(self._od_punktu.get(subject_type, [])),
            hwm=hwm,
            since=since,
            clamped=self._clamped,
        )

    def download_invoice(self, ksef_number):
        self.pobrane.append(ksef_number)
        if self._on_download is not None:
            self._on_download(ksef_number)
        if ksef_number in self._bledy_pobrania:
            blad = self._bledy_pobrania[ksef_number]
            if isinstance(blad, bytes):
                return blad  # treść faktury podana wprost (np. niemapowalna)
            raise blad
        return XML

    def download_budget_remaining(self):
        return 64


class _SyncBatchStub(_SyncStub):
    """Jw. + eksport wsadowy (paczka po dacie wystawienia) dla > 10 faktur."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.paczki = []

    def request_export(self, subject_type, date_from, date_to, *, date_type="Issue"):
        self.paczki.append((subject_type, date_from, date_to, date_type))
        return "EXP-1"

    def wait_for_export(self, reference):
        return {"parts": [], "isTruncated": False}

    def iter_export_invoices(self, reference, package):
        subject_type = self.paczki[-1][0]
        for m in self._glowne.get(subject_type, []):
            yield m.ksef_number, XML


def _profil():
    return Profile(name="Test", nip=NIP, token="x", subfolder="test")


def _eksport(tmp_path, client, *, od=date(2026, 7, 1), do=date(2026, 7, 31),
             kinds=frozenset({Kind.COST}), ledger=None, fmt="hmffk", cel=None, **kwargs):
    led = ledger or Ledger(tmp_path / "ledger.db")
    rej = RejestrNumerow(tmp_path / "rej.db")
    try:
        rep = run_export(_profil(), client, led, od, do, set(kinds), cel or tmp_path, fmt=fmt,
                         rejestr_nr=rej, **kwargs)
        return rep, led
    finally:
        rej.close()


def _plik_hmffk(tmp_path):
    [plik] = sorted((tmp_path / "test").glob("hmffk_*.TXT"))
    return plik.read_bytes().decode("cp1250")


def _numery_w_pliku(katalog):
    """Numery KSeF ze wszystkich plików hmffk w katalogu eksportu (z powtórzeniami)."""
    numery = []
    for plik in sorted((katalog / "test").glob("hmffk_*.TXT")):
        for linia in plik.read_bytes().decode("cp1250").splitlines():
            if linia.strip().startswith("KsefNumber ="):
                numery.append(linia.split("=", 1)[1].strip())
    return numery


def _ledger_z_czerwcem(tmp_path, *rodzaje):
    """Rejestr, w którym czerwiec był już wyeksportowany (W2: tylko faktura
    z datą w wyeksportowanym okresie jest spóźniona)."""
    led = Ledger(tmp_path / "ledger.db")
    for rodzaj in rodzaje or ("Subject2",):
        led.record_range(NIP, rodzaj, date(2026, 6, 1), date(2026, 6, 30))
    return led


# --------------------------------------------------------------------------- #
# 1. dołączanie spóźnionych do bieżącego pliku


def test_spozniona_faktura_z_poprzedniego_miesiaca_trafia_do_pliku_z_ostrzezeniem(tmp_path):
    led = _ledger_z_czerwcem(tmp_path)
    led.mark_done(NIP, "CZERWIEC-JUZ-W-REJESTRZE", "czerwiec.TXT")  # eksport czerwca sprzed tygodni
    w_lipcu = _meta("W-LIPCU", issue="2026-07-10")
    spozniona = _meta("SPOZNIONA", issue="2026-06-28", stored="2026-08-03T08:00:00+00:00")
    juz_byla = _meta("CZERWIEC-JUZ-W-REJESTRZE", issue="2026-06-20", stored="2026-07-02T08:00:00+00:00")
    client = _SyncStub(
        {"Subject2": [w_lipcu]},
        {"Subject2": [w_lipcu, spozniona, juz_byla]},
    )

    rep, led = _eksport(tmp_path, client, ledger=led)

    tekst = _plik_hmffk(tmp_path)
    assert "KsefNumber =W-LIPCU" in tekst
    assert "KsefNumber =SPOZNIONA" in tekst            # dołączona do TEGO SAMEGO pliku
    assert "CZERWIEC-JUZ-W-REJESTRZE" not in tekst     # już wyeksportowana — bez dubla
    assert (rep.found, rep.exported, rep.failed) == (2, 2, [])
    assert sorted(client.pobrane) == ["SPOZNIONA", "W-LIPCU"]  # każda pobrana raz
    assert rep.spoznione == [SpoznionaFaktura(
        ksef_number="SPOZNIONA", invoice_number="FV/SPOZNIONA",
        issue_date="2026-06-28", rodzaj="koszt",
    )]
    assert rep.warnings[0].startswith("Dołączono 1 spóźnion")
    assert "FV/SPOZNIONA" in rep.warnings[0] and "2026-06-28" in rep.warnings[0]
    assert "spóźnione" in rep.summary()
    assert led.is_done(NIP, "SPOZNIONA")
    assert led.sync_point(NIP, "Subject2") == HWM
    led.close()


def test_faktura_z_zakresu_ktorej_nie_bylo_w_glownym_zapytaniu_trafia_jako_zwykla(tmp_path):
    """Wyścig: faktura z zakresu zapisana w KSeF między głównym zapytaniem
    a zapytaniem „od punktu” — trafia do pliku jako zwykła, nie jako spóźniona."""
    pozno = _meta("LIPIEC-POZNO", issue="2026-07-30", stored="2026-08-05T09:59:00+00:00")
    client = _SyncStub({"Subject2": []}, {"Subject2": [pozno]})

    rep, led = _eksport(tmp_path, client)

    assert (rep.found, rep.exported) == (1, 1)
    assert rep.spoznione == []
    assert led.is_done(NIP, "LIPIEC-POZNO")
    assert not any("spóźnion" in w for w in rep.warnings)
    led.close()


def test_sksef_spozniona_dostaje_wlasny_plik(tmp_path):
    spozniona = _meta("SPOZNIONA", issue="2026-06-28", stored="2026-08-03T08:00:00+00:00")
    client = _SyncStub({"Subject2": [_meta("W-LIPCU")]}, {"Subject2": [spozniona]})

    rep, led = _eksport(tmp_path, client, fmt="sksef", ledger=_ledger_z_czerwcem(tmp_path))

    assert sorted(p.name for p in (tmp_path / "test").glob("*.TXT")) == ["SPOZNIONA.TXT", "W-LIPCU.TXT"]
    assert [s.ksef_number for s in rep.spoznione] == ["SPOZNIONA"]
    assert led.sync_point(NIP, "Subject2") == HWM
    led.close()


def test_glowne_paczka_a_spoznione_pobierane_pojedynczo(tmp_path):
    """Paczka eksportu idzie po dacie wystawienia z zakresu — spóźnionej w niej
    nie ma, więc jest pobierana pojedynczym GET-em (zwykle 0–kilka sztuk)."""
    glowne = [_meta(f"W-{i:02d}", issue="2026-07-10") for i in range(11)]  # > próg 10
    spozniona = _meta("SPOZNIONA", issue="2026-06-28", stored="2026-08-03T08:00:00+00:00")
    client = _SyncBatchStub({"Subject2": glowne}, {"Subject2": glowne + [spozniona]})

    rep, led = _eksport(tmp_path, client, ledger=_ledger_z_czerwcem(tmp_path))

    assert client.paczki == [("Subject2", date(2026, 6, 30), date(2026, 8, 1), "Issue")]
    assert client.pobrane == ["SPOZNIONA"]
    assert (rep.found, rep.exported, rep.failed) == (12, 12, [])
    assert "KsefNumber =SPOZNIONA" in _plik_hmffk(tmp_path)
    assert [s.ksef_number for s in rep.spoznione] == ["SPOZNIONA"]
    led.close()


def test_sprzedaz_i_koszt_maja_osobne_punkty(tmp_path):
    h1, h2 = "2026-08-05T10:00:00+00:00", "2026-08-05T10:00:30+00:00"
    client = _SyncStub(
        {"Subject1": [_meta("S-1")], "Subject2": [_meta("K-1")]},
        {"Subject1": [_meta("S-SPOZN", issue="2026-06-30", stored="2026-08-01T08:00:00+00:00")],
         "Subject2": []},
        hwm={"Subject1": h1, "Subject2": h2},
    )

    rep, led = _eksport(tmp_path, client, kinds={Kind.SALES, Kind.COST},
                        ledger=_ledger_z_czerwcem(tmp_path, "Subject1", "Subject2"))

    assert sorted(client.since_calls) == [
        ("Subject1", "2026-07-01T00:00:00+02:00"),
        ("Subject2", "2026-07-01T00:00:00+02:00"),
    ]
    assert led.sync_point(NIP, "Subject1") == h1
    assert led.sync_point(NIP, "Subject2") == h2
    assert [(s.ksef_number, s.rodzaj) for s in rep.spoznione] == [("S-SPOZN", "sprzedaż")]
    led.close()


# --------------------------------------------------------------------------- #
# 2. punkt kontynuacji: pierwszy raz, trwałość, przesuwanie tylko po sukcesie


@pytest.mark.parametrize("od, oczekiwany", [
    (date(2026, 7, 1), "2026-07-01T00:00:00+02:00"),    # lato
    (date(2026, 1, 1), "2026-01-01T00:00:00+01:00"),    # zima
    (date(2026, 3, 29), "2026-03-29T00:00:00+01:00"),   # dzień zmiany na letni (o 02:00)
    (date(2026, 3, 30), "2026-03-30T00:00:00+02:00"),
    (date(2026, 10, 25), "2026-10-25T00:00:00+02:00"),  # dzień zmiany na zimowy (o 03:00)
    (date(2026, 10, 26), "2026-10-26T00:00:00+01:00"),
])
def test_pierwszy_eksport_sprawdza_od_poczatku_zakresu_w_czasie_polskim(tmp_path, od, oczekiwany):
    client = _SyncStub({}, {})
    rep, led = _eksport(tmp_path, client, od=od, do=od + timedelta(days=5))
    assert client.since_calls == [("Subject2", oczekiwany)]
    led.close()


def test_punkt_z_poprzedniego_eksportu_uzyty_po_ponownym_uruchomieniu(tmp_path):
    client = _SyncStub({"Subject2": [_meta("W-LIPCU")]}, {"Subject2": []})
    _, led = _eksport(tmp_path, client)
    led.close()  # księgowa zamyka aplikację

    client2 = _SyncStub({}, {}, hwm="2026-09-02T07:00:00+00:00")
    _, led2 = _eksport(tmp_path, client2, od=date(2026, 8, 1), do=date(2026, 8, 31),
                       ledger=Ledger(tmp_path / "ledger.db"))
    assert client2.since_calls == [("Subject2", HWM)]
    assert led2.sync_point(NIP, "Subject2") == "2026-09-02T07:00:00+00:00"
    led2.close()


def test_faktura_z_pozniejszego_okresu_pominieta_i_wstrzymuje_punkt(tmp_path):
    """Faktura wystawiona PO końcu zakresu i jeszcze niewyeksportowana nie trafia
    do pliku (należy do przyszłego zakresu), a punkt nie może jej przeskoczyć —
    inaczej przepadłaby, gdyby tamten zakres był już wyeksportowany (eksport
    starszego miesiąca po nowszym). Przy następnym eksporcie z późniejszym
    zakresem wraca jako spóźniona."""
    przyszla_stored = "2026-08-03T08:00:00+00:00"
    przyszla = _meta("SIERPIEN", issue="2026-08-02", stored=przyszla_stored)
    wyeksportowana = _meta("SIERPIEN-JUZ", issue="2026-08-01", stored="2026-08-02T08:00:00+00:00")
    led = Ledger(tmp_path / "ledger.db")
    led.mark_done(NIP, "SIERPIEN-JUZ", "x.TXT")
    client = _SyncStub({"Subject2": [_meta("W-LIPCU")]}, {"Subject2": [przyszla, wyeksportowana]})

    rep, led = _eksport(tmp_path, client, ledger=led)

    assert "SIERPIEN" not in client.pobrane
    assert rep.spoznione == [] and rep.exported == 1
    assert led.sync_point(NIP, "Subject2") == przyszla_stored  # < HWM

    # później: eksport września — sierpień jeszcze NIEwyeksportowany (W2), więc
    # sierpniowa faktura dalej czeka na eksport swojego okresu, punkt stoi
    client2 = _SyncStub({}, {"Subject2": [przyszla]}, hwm="2026-10-02T07:00:00+00:00")
    rep2, led = _eksport(tmp_path, client2, od=date(2026, 9, 1), do=date(2026, 9, 30), ledger=led,
                         cel=tmp_path / "wrzesien")
    assert client2.since_calls == [("Subject2", przyszla_stored)]
    assert rep2.spoznione == [] and "SIERPIEN" not in client2.pobrane
    assert led.sync_point(NIP, "Subject2") == przyszla_stored

    # sierpień domknięty (eksport całego miesiąca) — następny eksport dołącza
    # ją jako spóźnioną i punkt idzie dalej
    led.record_range(NIP, "Subject2", date(2026, 8, 1), date(2026, 8, 31))
    client3 = _SyncStub({}, {"Subject2": [przyszla]}, hwm="2026-10-03T07:00:00+00:00")
    rep3, led = _eksport(tmp_path, client3, od=date(2026, 10, 1), do=date(2026, 10, 2), ledger=led,
                         cel=tmp_path / "pazdziernik")
    assert [s.ksef_number for s in rep3.spoznione] == ["SIERPIEN"]
    assert led.sync_point(NIP, "Subject2") == "2026-10-03T07:00:00+00:00"
    led.close()


def test_limit_przy_pobieraniu_spoznionej_nie_przesuwa_punktu(tmp_path):
    stary = "2026-07-20T00:00:00+00:00"
    led = _ledger_z_czerwcem(tmp_path)
    led.set_sync_point(NIP, "Subject2", stary)
    spozniona = _meta("SPOZNIONA", issue="2026-06-28", stored="2026-08-03T08:00:00+00:00")
    client = _SyncStub(
        {"Subject2": [_meta("W-LIPCU")]}, {"Subject2": [spozniona]},
        bledy_pobrania={"SPOZNIONA": KsefQuotaExceeded("pobierania faktur", time.time() + 1800)},
    )

    rep, led = _eksport(tmp_path, client, ledger=led)

    assert rep.niepelny
    assert any("Pozostało 1 faktur" in w for w in rep.warnings)
    assert led.sync_point(NIP, "Subject2") == stary  # następny eksport ponowi
    assert not led.is_done(NIP, "SPOZNIONA")
    assert rep.spoznione == []
    led.close()


def test_przerwanie_nie_przesuwa_punktu(tmp_path):
    cancel = threading.Event()
    client = _SyncStub(
        {"Subject2": [_meta("W-1"), _meta("W-2")]},
        {"Subject2": [_meta("SPOZNIONA", issue="2026-06-28", stored="2026-08-03T08:00:00+00:00")]},
        on_download=lambda nr: cancel.set(),
    )

    rep, led = _eksport(tmp_path, client, cancel=cancel, ledger=_ledger_z_czerwcem(tmp_path))

    assert rep.cancelled
    assert led.sync_point(NIP, "Subject2") is None
    led.close()


def test_blad_pobrania_spoznionej_wstrzymuje_punkt(tmp_path):
    """Błąd KSeF przy pobraniu spóźnionej (np. 21165 — jeszcze niedostępna):
    faktura na liście niepobranych (uruchom ponownie — NIE „zaksięguj ręcznie”),
    eksport niepełny, a punkt jej nie przeskakuje — następny eksport ponowi."""
    stary = "2026-07-20T00:00:00+00:00"
    led = _ledger_z_czerwcem(tmp_path)
    led.set_sync_point(NIP, "Subject2", stary)
    spozniona = _meta("SPOZNIONA", issue="2026-06-28", stored="2026-08-03T08:00:00+00:00")
    client = _SyncStub(
        {"Subject2": [_meta("W-LIPCU")]}, {"Subject2": [spozniona]},
        bledy_pobrania={"SPOZNIONA": KsefError("Faktura SPOZNIONA wciąż niedostępna (21165)")},
    )

    rep, led = _eksport(tmp_path, client, ledger=led)

    assert rep.failed == []
    assert [nr for nr, _ in rep.niepobrane] == ["SPOZNIONA"]
    assert rep.niepelny
    assert led.sync_point(NIP, "Subject2") == stary
    led.close()


@pytest.mark.parametrize("blad", [
    KsefQuotaExceeded("zapytań o metadane", time.time() + 1800),
    KsefError("query metadata: HTTP 400: 21405"),
])
def test_blad_zapytania_o_spoznione_nie_blokuje_eksportu_zakresu(tmp_path, blad):
    stary = "2026-07-20T00:00:00+00:00"
    led = Ledger(tmp_path / "ledger.db")
    led.set_sync_point(NIP, "Subject2", stary)
    client = _SyncStub({"Subject2": [_meta("W-LIPCU")]}, {}, blad_since=blad)

    rep, led = _eksport(tmp_path, client, ledger=led)

    assert (rep.found, rep.exported) == (1, 1)
    assert not rep.niepelny  # zakres wyeksportowany w całości
    assert any("spóźnion" in w and "następnym eksporcie" in w for w in rep.warnings)
    assert led.sync_point(NIP, "Subject2") == stary
    led.close()


def test_malo_limitu_metadanych_sprawdzenie_spoznionych_ustepuje_glownym_zapytaniom(tmp_path):
    """Zapytanie o spóźnione bierze z tej samej puli 20/h co główne zapytania.
    Gdy w godzinie zostało mało, sprawdzenie spóźnionych sprzedaży ustępuje
    głównemu zapytaniu o koszty (inaczej koszty skończyłyby się „niepełnym”)."""

    class _MaloLimitu(_SyncStub):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.budzet = 2

        def query_metadata(self, subject_type, date_from, date_to, *, margin=timedelta(0)):
            self.budzet -= 1
            yield from super().query_metadata(subject_type, date_from, date_to, margin=margin)

        def query_metadata_since(self, subject_type, since, *, max_lookback=None):
            self.budzet -= 1
            return super().query_metadata_since(subject_type, since, max_lookback=max_lookback)

        def metadata_budget_remaining(self):
            return self.budzet

    client = _MaloLimitu({"Subject1": [_meta("S-1")], "Subject2": [_meta("K-1")]}, {})

    rep, led = _eksport(tmp_path, client, kinds={Kind.SALES, Kind.COST})

    assert client.since_calls == []  # po sprzedaży zostało 1 = tyle, ile trzeba na koszty
    assert (rep.found, rep.exported) == (2, 2)
    assert not rep.niepelny
    assert sum("spóźnion" in w and "następnym eksporcie" in w for w in rep.warnings) == 2
    assert led.sync_point(NIP, "Subject1") is None
    assert led.sync_point(NIP, "Subject2") is None
    led.close()


def test_punkt_zapisywany_dopiero_po_zapisie_pliku(tmp_path, monkeypatch):
    """hmffk zapisuje plik na końcu przebiegu — jeśli zapis padnie, punkt nie
    może się przesunąć (spóźniona nie trafiła do żadnego pliku)."""
    import app.exporter as exporter_mod

    def zapis_pada(path, data):
        raise OSError("dysk pełny")

    monkeypatch.setattr(exporter_mod, "write_txt_atomic", zapis_pada)
    spozniona = _meta("SPOZNIONA", issue="2026-06-28", stored="2026-08-03T08:00:00+00:00")
    client = _SyncStub({"Subject2": [_meta("W-LIPCU")]}, {"Subject2": [spozniona]})
    led = _ledger_z_czerwcem(tmp_path)

    with pytest.raises(OSError):
        _eksport(tmp_path, client, ledger=led)
    assert led.sync_point(NIP, "Subject2") is None
    led.close()


def test_przyciety_stary_punkt_daje_ostrzezenie(tmp_path):
    led = Ledger(tmp_path / "ledger.db")
    led.set_sync_point(NIP, "Subject2", "2024-01-01T00:00:00+01:00")
    client = _SyncStub({"Subject2": []}, {"Subject2": []}, clamped=True)

    rep, led = _eksport(tmp_path, client, ledger=led)

    assert any("spóźnion" in w and "wcześniej" in w for w in rep.warnings)
    assert led.sync_point(NIP, "Subject2") == HWM
    led.close()


def test_pierwszy_eksport_przyciety_bez_ostrzezenia(tmp_path):
    """Pierwszy raz (brak punktu) i dawny zakres: klient przycina start z
    założenia (bez historii sprzed 90 dni) — to nie jest powód do alarmu."""
    client = _SyncStub({"Subject2": []}, {"Subject2": []}, clamped=True)
    rep, led = _eksport(tmp_path, client, od=date(2025, 1, 1), do=date(2025, 1, 31))
    assert not any("spóźnion" in w for w in rep.warnings)
    led.close()


# --------------------------------------------------------------------------- #
# 3. tryby wyszukiwania: bez spóźnionych i bez ruszania punktu


@pytest.mark.parametrize("tryb", [
    {"invoice_numbers": ["FV/W-LIPCU"]},
    {"contractor_filter": ["1180012345"]},
])
def test_szukanie_po_numerze_i_kontrahencie_bez_sprawdzania_spoznionych(tmp_path, tryb):
    spozniona = _meta("SPOZNIONA", issue="2026-06-28", stored="2026-08-03T08:00:00+00:00")
    client = _SyncStub({"Subject2": [_meta("W-LIPCU")]}, {"Subject2": [spozniona]})

    rep, led = _eksport(tmp_path, client, **tryb)

    assert client.since_calls == []
    assert client.pobrane == ["W-LIPCU"]
    assert rep.spoznione == []
    assert led.sync_point(NIP, "Subject2") is None
    led.close()


# --------------------------------------------------------------------------- #
# 4. raport i CLI


def test_summary_wspomina_liczbe_spoznionych():
    rep = ExportReport(found=3, exported=3)
    assert "spóźnione" not in rep.summary()
    rep.spoznione = [
        SpoznionaFaktura("A", "FV/1", "2026-06-28", "koszt"),
        SpoznionaFaktura("B", "FV/2", "2026-06-29", "koszt"),
    ]
    assert "spóźnione z wcześniejszych okresów 2" in rep.summary()


def test_cli_wypisuje_ostrzezenie_o_spoznionych(tmp_path, monkeypatch, capsys):
    import app.cli as cli_mod

    spozniona = _meta("SPOZNIONA", issue="2026-06-28", stored="2026-08-03T08:00:00+00:00")
    client = _SyncStub({"Subject2": [_meta("W-LIPCU")]}, {"Subject2": [spozniona]})
    monkeypatch.setenv("KSEF_TOKEN", "test-token")
    monkeypatch.setattr(cli_mod, "KsefClient", lambda *a, **k: client)
    led = Ledger()  # ten sam plik co w CLI (%APPDATA% podmienione w conftest)
    led.record_range(NIP, "Subject2", date(2026, 6, 1), date(2026, 6, 30))
    led.close()

    kod = cli_mod.main([
        "--nip", NIP, "--od", "2026-07-01", "--do", "2026-07-31",
        "--rodzaj", "koszt", "--cel", str(tmp_path),
    ])

    out = capsys.readouterr().out
    assert kod == 0
    assert "UWAGA: Dołączono 1 spóźnion" in out
    assert "spóźnione z wcześniejszych okresów 1" in out


# --------------------------------------------------------------------------- #
# 5. W2 (decyzja 28.09): spóźniona = data wystawienia w okresie JUŻ wyeksportowanym


def test_w2_eksport_zapamietuje_zakres_per_rodzaj(tmp_path):
    client = _SyncStub({"Subject2": [_meta("W-LIPCU")]}, {})
    rep, led = _eksport(tmp_path, client)
    assert led.ranges(NIP, "Subject2") == [(date(2026, 7, 1), date(2026, 7, 31))]
    assert led.ranges(NIP, "Subject1") == []  # sprzedaży nie eksportowano
    led.close()
    # trwale — przeżywa zamknięcie aplikacji
    led = Ledger(tmp_path / "ledger.db")
    assert led.is_covered(NIP, "Subject2", date(2026, 7, 15))
    led.close()


def test_w2_przerwany_eksport_nie_zapisuje_zakresu(tmp_path):
    cancel = threading.Event()
    client = _SyncStub({"Subject2": [_meta("W-1"), _meta("W-2")]}, {},
                       on_download=lambda nr: cancel.set())
    rep, led = _eksport(tmp_path, client, cancel=cancel)
    assert rep.cancelled
    assert led.ranges(NIP, "Subject2") == []
    led.close()


@pytest.mark.parametrize("tryb", [
    {"invoice_numbers": ["FV/W-LIPCU"]},
    {"contractor_filter": ["1180012345"]},
])
def test_w2_wyszukiwanie_nie_zapisuje_zakresu(tmp_path, tryb):
    client = _SyncStub({"Subject2": [_meta("W-LIPCU")]}, {})
    rep, led = _eksport(tmp_path, client, **tryb)
    assert led.ranges(NIP, "Subject2") == []
    led.close()


def test_w2_faktura_z_nieeksportowanego_okresu_czeka_i_trafia_do_eksportu_swojego_okresu(tmp_path):
    """Sierpień 01–15 wyeksportowany, 16–31 jeszcze nie. Faktura z 29.08, która
    wpłynęła 04.09, NIE trafia do eksportu 01–07.09 (nie jest spóźniona — jej
    okres czeka na eksport), punkt jej nie przeskakuje, a domknięcie sierpnia
    z „Pomiń już pobrane” zabiera ją do pliku sierpnia. Bez utraty, bez dubla."""
    led = Ledger(tmp_path / "ledger.db")
    led.record_range(NIP, "Subject2", date(2026, 8, 1), date(2026, 8, 15))
    y_stored = "2026-09-04T10:00:00+00:00"
    y = _meta("Y-2908", issue="2026-08-29", stored=y_stored)
    wrzesien = _meta("W-0902", issue="2026-09-02", stored="2026-09-02T10:00:00+00:00")

    c1 = _SyncStub({"Subject2": [wrzesien]}, {"Subject2": [wrzesien, y]},
                   hwm="2026-09-08T07:00:00+00:00")
    rep1, led = _eksport(tmp_path, c1, od=date(2026, 9, 1), do=date(2026, 9, 7), ledger=led,
                         only_new=True, cel=tmp_path / "e1")
    assert "Y-2908" not in c1.pobrane and rep1.spoznione == []
    assert led.sync_point(NIP, "Subject2") == y_stored

    c2 = _SyncStub({"Subject2": [y]}, {"Subject2": [y]}, hwm="2026-09-12T10:00:00+00:00")
    rep2, led = _eksport(tmp_path, c2, od=date(2026, 8, 1), do=date(2026, 8, 31), ledger=led,
                         only_new=True, cel=tmp_path / "e2")
    assert _numery_w_pliku(tmp_path / "e2") == ["Y-2908"]
    assert rep2.spoznione == []  # zwykła faktura swojego okresu
    assert _numery_w_pliku(tmp_path / "e1") == ["W-0902"]
    assert led.sync_point(NIP, "Subject2") == "2026-09-12T10:00:00+00:00"
    led.close()


def test_w2_zakres_czesciowy_nie_domyka_miesiaca_faktura_czeka_na_domkniecie(tmp_path):
    """01–07.09 wyeksportowane 08.09; faktura z 03.09 wpływa 10.09. Zakres
    „na bieżąco” nie domyka września, więc NIE trafia do eksportu 08–15.09
    (punkt jej nie przeskakuje) — bierze ją domknięcie 01–30.09 głównym
    zapytaniem, także z „Pomiń już pobrane” (nie ma jej w rejestrze)."""
    led = Ledger(tmp_path / "ledger.db")
    a = _meta("A-0902", issue="2026-09-02", stored="2026-09-02T10:00:00+00:00")
    x = _meta("X-0903", issue="2026-09-03", stored="2026-09-10T10:00:00+00:00")
    b = _meta("B-0910", issue="2026-09-10", stored="2026-09-10T11:00:00+00:00")

    c1 = _SyncStub({"Subject2": [a]}, {"Subject2": [a]}, hwm="2026-09-08T07:00:00+00:00")
    _, led = _eksport(tmp_path, c1, od=date(2026, 9, 1), do=date(2026, 9, 7), ledger=led,
                      only_new=True, cel=tmp_path / "e1")
    c2 = _SyncStub({"Subject2": [b]}, {"Subject2": [x, b]}, hwm="2026-09-16T07:00:00+00:00")
    rep2, led = _eksport(tmp_path, c2, od=date(2026, 9, 8), do=date(2026, 9, 15), ledger=led,
                         only_new=True, cel=tmp_path / "e2")
    assert rep2.spoznione == [] and "X-0903" not in c2.pobrane
    assert _numery_w_pliku(tmp_path / "e2") == ["B-0910"]
    assert led.sync_point(NIP, "Subject2") == x.permanent_storage_date
    c3 = _SyncStub({"Subject2": [a, x, b]}, {"Subject2": [x, b]}, hwm="2026-10-12T07:00:00+00:00")
    rep3, led = _eksport(tmp_path, c3, od=date(2026, 9, 1), do=date(2026, 9, 30), ledger=led,
                         only_new=True, cel=tmp_path / "e3")
    assert _numery_w_pliku(tmp_path / "e3") == ["X-0903"]
    assert rep3.spoznione == []  # we własnym miesiącu to zwykła faktura

    wszystkie = sum((_numery_w_pliku(tmp_path / e) for e in ("e1", "e2", "e3")), [])
    assert sorted(wszystkie) == ["A-0902", "B-0910", "X-0903"]  # każda dokładnie raz
    assert (rep3.exported, rep3.skipped_dedup) == (1, 2)
    assert led.sync_point(NIP, "Subject2") == "2026-10-12T07:00:00+00:00"
    led.close()


def test_w2_faktura_z_1408_po_eksporcie_0115_czeka_na_domkniecie_sierpnia(tmp_path):
    """Eksport 01–15.08, potem faktura z 14.08 wpływa 06.09 — przed eksportem
    całego sierpnia. Nie trafia do pliku 01–07.09, punkt jej nie przeskakuje;
    domknięcie sierpnia z „Pomiń już pobrane” bierze ją głównym zapytaniem."""
    led = Ledger(tmp_path / "ledger.db")
    led.record_range(NIP, "Subject2", date(2026, 8, 1), date(2026, 8, 15))
    f = _meta("F-1408", issue="2026-08-14", stored="2026-09-06T10:00:00+00:00")
    w = _meta("W-0902", issue="2026-09-02", stored="2026-09-02T10:00:00+00:00")

    c1 = _SyncStub({"Subject2": [w]}, {"Subject2": [w, f]}, hwm="2026-09-08T07:00:00+00:00")
    rep1, led = _eksport(tmp_path, c1, od=date(2026, 9, 1), do=date(2026, 9, 7), ledger=led,
                         only_new=True, cel=tmp_path / "e1")
    assert _numery_w_pliku(tmp_path / "e1") == ["W-0902"]
    assert rep1.spoznione == [] and "F-1408" not in c1.pobrane
    assert led.sync_point(NIP, "Subject2") == f.permanent_storage_date

    c2 = _SyncStub({"Subject2": [f]}, {"Subject2": [w, f]}, hwm="2026-09-12T10:00:00+00:00")
    rep2, led = _eksport(tmp_path, c2, od=date(2026, 8, 1), do=date(2026, 8, 31), ledger=led,
                         only_new=True, cel=tmp_path / "e2")
    assert _numery_w_pliku(tmp_path / "e2") == ["F-1408"]
    assert rep2.spoznione == []
    assert led.is_covered(NIP, "Subject2", date(2026, 8, 14))  # sierpień domknięty
    led.close()


def test_w2_faktura_po_domknieciu_miesiaca_jest_spozniona(tmp_path):
    """Sierpień domknięty eksportem 01–31.08; sierpniowa faktura, która wpłynęła
    PO domknięciu, dołącza do najbliższego eksportu jako spóźniona."""
    led = Ledger(tmp_path / "ledger.db")
    led.record_range(NIP, "Subject2", date(2026, 8, 1), date(2026, 8, 31))
    f = _meta("F-2808", issue="2026-08-28", stored="2026-09-13T10:00:00+00:00")
    c = _SyncStub({"Subject2": []}, {"Subject2": [f]}, hwm="2026-09-16T07:00:00+00:00")
    rep, led = _eksport(tmp_path, c, od=date(2026, 9, 8), do=date(2026, 9, 15), ledger=led,
                        only_new=True)
    assert [s.ksef_number for s in rep.spoznione] == ["F-2808"]
    assert _numery_w_pliku(tmp_path) == ["F-2808"]
    assert led.sync_point(NIP, "Subject2") == "2026-09-16T07:00:00+00:00"
    led.close()


def test_w2_eksport_wielomiesieczny_domyka_kazdy_objety_miesiac(tmp_path):
    """Jeden eksport 01.07–30.09 domyka lipiec, sierpień i wrzesień —
    sierpniowa faktura, która wpłynęła później, jest spóźniona."""
    c1 = _SyncStub({"Subject2": [_meta("W-LIPCU")]}, {}, hwm="2026-10-01T07:00:00+00:00")
    _, led = _eksport(tmp_path, c1, od=date(2026, 7, 1), do=date(2026, 9, 30), cel=tmp_path / "e1")
    f = _meta("F-2008", issue="2026-08-20", stored="2026-10-03T10:00:00+00:00")
    c2 = _SyncStub({"Subject2": []}, {"Subject2": [f]}, hwm="2026-10-08T07:00:00+00:00")
    rep2, led = _eksport(tmp_path, c2, od=date(2026, 10, 1), do=date(2026, 10, 7), ledger=led,
                         cel=tmp_path / "e2")
    assert [s.ksef_number for s in rep2.spoznione] == ["F-2008"]
    led.close()


def test_w2_faktura_po_zakresie_z_wyeksportowanego_okresu_czeka_na_pozniejszy_eksport(tmp_path):
    """Październik domknięty (01–31.10), potem (późno) domknięcie września.
    Faktura z 05.10, która wpłynęła po domknięciu października, nie trafia do
    pliku września (spóźnione dołączamy tylko do eksportu późniejszego okresu)
    — punkt jej nie przeskakuje i dołącza ją eksport 08–15.10."""
    led = Ledger(tmp_path / "ledger.db")
    led.record_range(NIP, "Subject2", date(2026, 10, 1), date(2026, 10, 31))
    paz_stored = "2026-10-10T10:00:00+00:00"
    paz = _meta("PAZ-0510", issue="2026-10-05", stored=paz_stored)

    c1 = _SyncStub({"Subject2": []}, {"Subject2": [paz]}, hwm="2026-10-12T07:00:00+00:00")
    rep1, led = _eksport(tmp_path, c1, od=date(2026, 9, 1), do=date(2026, 9, 30), ledger=led,
                         only_new=True, cel=tmp_path / "e1")
    assert rep1.spoznione == [] and c1.pobrane == []
    assert led.sync_point(NIP, "Subject2") == paz_stored

    c2 = _SyncStub({"Subject2": []}, {"Subject2": [paz]}, hwm="2026-10-16T07:00:00+00:00")
    rep2, led = _eksport(tmp_path, c2, od=date(2026, 10, 8), do=date(2026, 10, 15), ledger=led,
                         only_new=True, cel=tmp_path / "e2")
    assert [s.ksef_number for s in rep2.spoznione] == ["PAZ-0510"]
    led.close()


def test_w2_bezpiecznik_60_dni_od_wplywu(tmp_path):
    """Faktura z okresu, którego aplikacja nie eksportowała (np. zrobionego
    starszą wersją), czeka — ale nie w nieskończoność: po 60 dniach od wpływu
    do KSeF dołącza jako spóźniona z osobnym ostrzeżeniem, a punkt rusza."""
    led = Ledger(tmp_path / "ledger.db")
    led.set_sync_point(NIP, "Subject2", "2026-05-01T00:00:00+00:00")  # obserwacja od maja
    led.set_sync_point(NIP, "Subject2", "2026-07-01T00:00:00+00:00")
    hwm = "2026-09-05T10:00:00+00:00"
    stary = _meta("STARY", issue="2026-06-20", stored="2026-07-01T08:00:00+00:00")   # 66 dni
    mlody_stored = "2026-07-10T08:00:00+00:00"
    mlody = _meta("MLODY", issue="2026-06-25", stored=mlody_stored)                   # 57 dni
    client = _SyncStub({"Subject2": []}, {"Subject2": [stary, mlody]}, hwm=hwm)

    rep, led = _eksport(tmp_path, client, od=date(2026, 9, 1), do=date(2026, 9, 5), ledger=led)

    assert client.pobrane == ["STARY"]
    [s] = rep.spoznione
    assert (s.ksef_number, s.bezpiecznik) == ("STARY", True)
    [w] = [w for w in rep.warnings if "60 dni" in w]
    assert "FV/STARY" in w
    # D4: bez twierdzenia, którego aplikacja nie wie (miesiąc mógł domknąć
    # eksport starszej wersji albo domknięcie się spóźnia)
    assert "nie eksportowała" not in w and "domknięcie swojego miesiąca" in w
    assert w.startswith("Dołączono 1 fakturę, która czekała ponad 60 dni")
    assert "czy nie jest już zaksięgowana" in w
    assert led.sync_point(NIP, "Subject2") == mlody_stored  # MŁODY dalej wstrzymuje punkt
    led.close()


def test_w2_bezpiecznik_liczy_czas_od_poczatku_obserwacji(tmp_path):
    """Pierwszy eksport po instalacji sięga do 90 dni wstecz — faktury, które
    wpłynęły dawno, a których okres nie był eksportowany (nadrabianie
    miesięcy po kolei), nie mogą od razu wpaść do bieżącego pliku. Czekanie
    liczy się od późniejszej z dat: wpływ do KSeF / początek obserwacji."""
    stary = _meta("STARY", issue="2026-06-20", stored="2026-07-01T08:00:00+00:00")
    hwm = "2026-09-05T10:00:00+00:00"

    # pierwszy eksport (brak punktu) — nic nie dołącza się z bezpiecznika
    # (nadrabianie: pierwszy eksport to 25–26.06, sprawdzenie od północy 25.06)
    c1 = _SyncStub({"Subject2": []}, {"Subject2": [stary]}, hwm=hwm)
    rep1, led = _eksport(tmp_path, c1, od=date(2026, 6, 25), do=date(2026, 6, 26), cel=tmp_path / "e1")
    assert rep1.spoznione == [] and c1.pobrane == []
    # punkt cofnięty do wstrzymanej faktury, ale początek obserwacji = „teraz”
    # pierwszego sprawdzenia (HWM), nie ten cofnięty punkt — inaczej następny
    # eksport od razu dołączyłby ją z bezpiecznika
    assert led.sync_point(NIP, "Subject2") == stary.permanent_storage_date
    assert led.watch_from(NIP, "Subject2") == hwm
    c1b = _SyncStub({"Subject2": []}, {"Subject2": [stary]}, hwm="2026-09-06T10:00:00+00:00")
    rep1b, led = _eksport(tmp_path, c1b, od=date(2026, 9, 6), do=date(2026, 9, 6), ledger=led,
                          cel=tmp_path / "e1b")
    assert rep1b.spoznione == [] and c1b.pobrane == []

    # obserwacja od 01.08 — faktura czeka dopiero 35 dni
    led2 = Ledger(tmp_path / "ledger2.db")
    led2.set_sync_point(NIP, "Subject2", "2026-08-01T00:00:00+00:00")
    c2 = _SyncStub({"Subject2": []}, {"Subject2": [stary]}, hwm=hwm)
    rep2, led2 = _eksport(tmp_path, c2, od=date(2026, 9, 1), do=date(2026, 9, 5), ledger=led2,
                          cel=tmp_path / "e2")
    assert rep2.spoznione == [] and c2.pobrane == []
    led.close()
    led2.close()


# --------------------------------------------------------------------------- #
# 6. W3: ponowny PEŁNY eksport tego samego zakresu dokłada spóźnione znowu


def test_w3_ponowny_pelny_eksport_daje_ten_sam_plik(tmp_path):
    led = _ledger_z_czerwcem(tmp_path)
    w_lipcu = _meta("W-LIPCU")
    spozniona = _meta("SPOZNIONA", issue="2026-06-28", stored="2026-08-03T08:00:00+00:00")
    c1 = _SyncStub({"Subject2": [w_lipcu]}, {"Subject2": [w_lipcu, spozniona]})
    rep1, led = _eksport(tmp_path, c1, ledger=led, cel=tmp_path / "e1")
    assert sorted(_numery_w_pliku(tmp_path / "e1")) == ["SPOZNIONA", "W-LIPCU"]

    # import w Symfonii się nie udał — ten sam zakres, pełny eksport (bez „Pomiń”);
    # punkt już dalej, więc sprawdzenie „od punktu” spóźnionej nie widzi
    c2 = _SyncStub({"Subject2": [w_lipcu]}, {"Subject2": []}, hwm="2026-08-06T10:00:00+00:00")
    rep2, led = _eksport(tmp_path, c2, ledger=led, cel=tmp_path / "e2")

    assert sorted(_numery_w_pliku(tmp_path / "e2")) == ["SPOZNIONA", "W-LIPCU"]
    assert sorted(c2.pobrane) == ["SPOZNIONA", "W-LIPCU"]
    assert [s.ksef_number for s in rep2.spoznione] == ["SPOZNIONA"]
    assert (rep2.found, rep2.exported, rep2.re_exported) == (2, 2, 2)
    led.close()


def test_w3_z_pomin_juz_pobrane_nie_dokłada_ponownie(tmp_path):
    led = _ledger_z_czerwcem(tmp_path)
    w_lipcu = _meta("W-LIPCU")
    spozniona = _meta("SPOZNIONA", issue="2026-06-28", stored="2026-08-03T08:00:00+00:00")
    _, led = _eksport(tmp_path, _SyncStub({"Subject2": [w_lipcu]}, {"Subject2": [spozniona]}),
                      ledger=led, cel=tmp_path / "e1")
    c2 = _SyncStub({"Subject2": [w_lipcu]}, {"Subject2": []}, hwm="2026-08-06T10:00:00+00:00")
    rep2, led = _eksport(tmp_path, c2, ledger=led, only_new=True, cel=tmp_path / "e2")
    assert c2.pobrane == []
    assert rep2.spoznione == [] and rep2.skipped_dedup == 1
    led.close()


def test_w3_pelne_domkniecie_miesiaca_obejmuje_spoznione_z_zakresow_czesciowych(tmp_path):
    led = Ledger(tmp_path / "ledger.db")
    led.record_range(NIP, "Subject2", date(2026, 8, 1), date(2026, 8, 31))
    x = _meta("X-SIERPIEN", issue="2026-08-28", stored="2026-09-10T10:00:00+00:00")
    b = _meta("B-0910", issue="2026-09-10", stored="2026-09-10T11:00:00+00:00")
    _, led = _eksport(tmp_path, _SyncStub({"Subject2": [b]}, {"Subject2": [x, b]}),
                      od=date(2026, 9, 8), do=date(2026, 9, 15), ledger=led, cel=tmp_path / "e1")
    assert sorted(_numery_w_pliku(tmp_path / "e1")) == ["B-0910", "X-SIERPIEN"]

    # pełne domknięcie 01–30.09 zawiera to, co pliki częściowe (w tym spóźnioną)
    c2 = _SyncStub({"Subject2": [b]}, {"Subject2": []}, hwm="2026-10-12T10:00:00+00:00")
    _, led = _eksport(tmp_path, c2, od=date(2026, 9, 1), do=date(2026, 9, 30), ledger=led,
                      cel=tmp_path / "e2")
    assert sorted(_numery_w_pliku(tmp_path / "e2")) == ["B-0910", "X-SIERPIEN"]

    # zakres, który nie obejmuje całego zakresu dołączenia — bez niej
    c3 = _SyncStub({"Subject2": [b]}, {"Subject2": []}, hwm="2026-10-12T11:00:00+00:00")
    _, led = _eksport(tmp_path, c3, od=date(2026, 9, 10), do=date(2026, 9, 30), ledger=led,
                      cel=tmp_path / "e3")
    assert _numery_w_pliku(tmp_path / "e3") == ["B-0910"]
    led.close()


def test_w3_pelny_eksport_wlasnego_okresu_spoznionej_bez_podwojenia_w_pliku(tmp_path):
    """Pełny eksport zakresu, który obejmuje i datę wystawienia spóźnionej, i
    zakres, do którego ją dołączono — w pliku raz."""
    led = Ledger(tmp_path / "ledger.db")
    led.record_range(NIP, "Subject2", date(2026, 8, 1), date(2026, 8, 31))
    x = _meta("X-SIERPIEN", issue="2026-08-28", stored="2026-09-10T10:00:00+00:00")
    _, led = _eksport(tmp_path, _SyncStub({"Subject2": []}, {"Subject2": [x]}),
                      od=date(2026, 9, 8), do=date(2026, 9, 15), ledger=led, cel=tmp_path / "e1")
    c2 = _SyncStub({"Subject2": [x]}, {"Subject2": []}, hwm="2026-10-12T10:00:00+00:00")
    rep2, led = _eksport(tmp_path, c2, od=date(2026, 8, 1), do=date(2026, 9, 30), ledger=led,
                         cel=tmp_path / "e2")
    assert _numery_w_pliku(tmp_path / "e2") == ["X-SIERPIEN"]
    assert c2.pobrane == ["X-SIERPIEN"]
    assert rep2.spoznione == []  # we własnym okresie to zwykła faktura
    led.close()


# --------------------------------------------------------------------------- #
# 7. (9) spóźniona z błędem mapowania: trwała lista do ręcznej obsługi


def test_spozniona_z_bledem_mapowania_na_trwalej_liscie_punkt_idzie_dalej(tmp_path):
    led = _ledger_z_czerwcem(tmp_path)
    spozniona = _meta("SPOZNIONA", issue="2026-06-28", stored="2026-08-03T08:00:00+00:00")
    client = _SyncStub({"Subject2": [_meta("W-LIPCU")]}, {"Subject2": [spozniona]},
                       bledy_pobrania={"SPOZNIONA": b"<Inna/>"})

    rep, led = _eksport(tmp_path, client, ledger=led)

    [(nr, komunikat)] = rep.failed
    assert nr == "SPOZNIONA" and "ręcznej obsługi" in komunikat
    assert rep.niepobrane == [] and not rep.niepelny
    [rec] = led.manual_list(NIP)
    assert (rec.ksef_number, rec.issue_date, rec.status) == ("SPOZNIONA", "2026-06-28", "do_recznej_obslugi")
    assert (rec.range_from, rec.range_to) == (date(2026, 7, 1), date(2026, 7, 31))
    assert led.sync_point(NIP, "Subject2") == HWM  # punkt przeskakuje ją ze śladem
    assert rep.spoznione == []
    led.close()


# --------------------------------------------------------------------------- #
# 8. W6a / (11): niepobrane to nie „zaksięguj ręcznie”; niepełny per rodzaj


class _PaczkaSprzedazyPada(_SyncBatchStub):
    def request_export(self, subject_type, date_from, date_to, *, date_type="Issue"):
        ref = super().request_export(subject_type, date_from, date_to, date_type=date_type)
        if subject_type == "Subject1":
            raise KsefError("eksport wsadowy: HTTP 500 (symulacja)")
        return ref


def test_w6a_blad_paczki_to_niepobrane_a_nie_bledy_i_nie_blokuje_punktu_kosztow(tmp_path):
    sprzedaz = [_meta(f"S-{i:02d}", issue="2026-07-10") for i in range(11)]
    koszt = _meta("K-1", issue="2026-07-10")
    h1, h2 = "2026-08-05T10:00:00+00:00", "2026-08-05T10:00:30+00:00"
    client = _PaczkaSprzedazyPada(
        {"Subject1": sprzedaz, "Subject2": [koszt]}, {"Subject1": sprzedaz, "Subject2": [koszt]},
        hwm={"Subject1": h1, "Subject2": h2},
    )

    rep, led = _eksport(tmp_path, client, kinds={Kind.SALES, Kind.COST})

    assert rep.failed == []  # nic do ręcznego księgowania
    assert sorted(nr for nr, _ in rep.niepobrane) == [m.ksef_number for m in sprzedaz]
    assert rep.niepelny
    assert "nie pobrano 11" in rep.summary()
    assert _numery_w_pliku(tmp_path) == ["K-1"]
    # (11) koszty przetworzone w całości — ich punkt i zakres idą dalej
    assert led.sync_point(NIP, "Subject2") == h2
    assert led.ranges(NIP, "Subject2") == [(date(2026, 7, 1), date(2026, 7, 31))]
    assert led.sync_point(NIP, "Subject1") is None
    assert led.ranges(NIP, "Subject1") == []
    led.close()


def test_w6a_faktury_brak_w_paczce_to_niepobrane(tmp_path):
    glowne = [_meta(f"W-{i:02d}", issue="2026-07-10") for i in range(11)]

    class _Brak(_SyncBatchStub):
        def iter_export_invoices(self, reference, package):
            for nr, xml in super().iter_export_invoices(reference, package):
                if nr != "W-03":
                    yield nr, xml

    rep, led = _eksport(tmp_path, _Brak({"Subject2": glowne}, {"Subject2": glowne}))
    assert rep.failed == []
    assert [nr for nr, _ in rep.niepobrane] == ["W-03"]
    assert rep.niepelny and rep.exported == 10
    assert led.sync_point(NIP, "Subject2") is None
    led.close()


# --------------------------------------------------------------------------- #
# 9. (10) ostrzeżenie o nowych kontrahentach nie ginie, gdy zapis pliku padnie


def test_nowi_kontrahenci_nie_gina_gdy_zapis_pliku_padnie(tmp_path, monkeypatch):
    import app.exporter as exporter_mod

    prawdziwy_zapis = exporter_mod.write_txt_atomic

    def zapis_pada(path, data):
        raise OSError("dysk pełny")

    monkeypatch.setattr(exporter_mod, "write_txt_atomic", zapis_pada)
    led = Ledger(tmp_path / "ledger.db")
    with pytest.raises(OSError):
        _eksport(tmp_path, _SyncStub({"Subject2": [_meta("W-LIPCU")]}, {}), ledger=led)

    # numer karty został nadany w rejestrze, choć plik nie powstał; przy
    # następnym (udanym) eksporcie kontrahent nie jest już „nowy”, ale
    # ostrzeżenie i tak musi trafić do raportu
    monkeypatch.setattr(exporter_mod, "write_txt_atomic", prawdziwy_zapis)
    rep, led = _eksport(tmp_path, _SyncStub({"Subject2": [_meta("W-LIPCU")]}, {}), ledger=led)
    assert [(n[1], n[2]) for n in rep.nowi_kontrahenci] == [("1180012345", 1)]
    assert any(w.startswith("Nowi kontrahenci w pliku") for w in rep.warnings)

    rep3, led = _eksport(tmp_path, _SyncStub({"Subject2": [_meta("W-LIPCU")]}, {}), ledger=led,
                         cel=tmp_path / "e3")
    assert rep3.nowi_kontrahenci == []  # zgłoszone raz
    led.close()


# --------------------------------------------------------------------------- #
# 10. (4) CLI: kod wyjścia przy eksporcie niepełnym / przerwanym


def _cli(tmp_path, monkeypatch, client):
    import app.cli as cli_mod

    monkeypatch.setenv("KSEF_TOKEN", "test-token")
    monkeypatch.setattr(cli_mod, "KsefClient", lambda *a, **k: client)
    return cli_mod.main([
        "--nip", NIP, "--od", "2026-07-01", "--do", "2026-07-31",
        "--rodzaj", "koszt", "--cel", str(tmp_path),
    ])


def test_cli_kod_3_gdy_eksport_niepelny(tmp_path, monkeypatch, capsys):
    client = _SyncStub({"Subject2": [_meta("W-1"), _meta("W-2")]}, {}, bledy_pobrania={
        "W-2": KsefQuotaExceeded("pobierania faktur", time.time() + 1800),
    })
    assert _cli(tmp_path, monkeypatch, client) == 3
    assert "NIEPEŁNY" in capsys.readouterr().out


def test_cli_kod_3_gdy_nie_pobrano_faktur(tmp_path, monkeypatch, capsys):
    client = _SyncStub({"Subject2": [_meta("W-1")]}, {}, bledy_pobrania={
        "W-1": KsefError("Faktura W-1 wciąż niedostępna (21165)"),
    })
    assert _cli(tmp_path, monkeypatch, client) == 3
    out = capsys.readouterr().out
    assert "NIE POBRANO W-1" in out


def test_cli_kod_3_gdy_przerwano(tmp_path, monkeypatch):
    from app.ksef_client import KsefCancelled

    client = _SyncStub({"Subject2": [_meta("W-1")]}, {}, bledy_pobrania={
        "W-1": KsefCancelled("przerwano"),
    })
    assert _cli(tmp_path, monkeypatch, client) == 3


def test_cli_kod_1_gdy_tylko_bledy_mapowania(tmp_path, monkeypatch):
    client = _SyncStub({"Subject2": [_meta("W-1"), _meta("W-2")]}, {},
                       bledy_pobrania={"W-2": b"<Inna/>"})
    assert _cli(tmp_path, monkeypatch, client) == 1


def test_cli_kod_0_gdy_wszystko_w_pliku(tmp_path, monkeypatch):
    assert _cli(tmp_path, monkeypatch, _SyncStub({"Subject2": [_meta("W-1")]}, {})) == 0


# --------------------------------------------------------------------------- #
# 11. (3) KSeF 21183 („od” późniejsze niż HWM) = nic nowego, bez ostrzeżenia


def test_od_pozniejsze_niz_hwm_bez_ostrzezenia_i_z_punktem(tmp_path):
    """Pierwszy eksport tuż po północy z „Od” = dziś: KSeF odrzuca zapytanie
    (21183). Klient zwraca wtedy pusty wynik z HWM = początek sprawdzenia;
    eksport nie ostrzega, a punkt startuje od północy — faktura z wczoraj,
    która wpłynie rano, nie zginie."""
    start = "2026-09-28T00:00:00+02:00"
    client = _SyncStub({"Subject2": []}, {"Subject2": []}, hwm=start)
    rep, led = _eksport(tmp_path, client, od=date(2026, 9, 28), do=date(2026, 9, 30))
    assert not any("spóźnion" in w for w in rep.warnings)
    assert led.sync_point(NIP, "Subject2") == start
    led.close()


def test_w3_nie_ponawia_spoznionej_z_listy_do_recznej_obslugi(tmp_path):
    """Za pierwszym razem spóźnionej z błędem mapowania nie było w pliku (miała
    być zaksięgowana ręcznie) — pełny eksport tego samego zakresu też jej nie
    dokłada, nawet gdy teraz dałoby się ją przetworzyć (inaczej dubel
    z ręcznym księgowaniem)."""
    led = _ledger_z_czerwcem(tmp_path)
    spozniona = _meta("SPOZNIONA", issue="2026-06-28", stored="2026-08-03T08:00:00+00:00")
    c1 = _SyncStub({"Subject2": [_meta("W-LIPCU")]}, {"Subject2": [spozniona]},
                   bledy_pobrania={"SPOZNIONA": b"<Inna/>"})
    _, led = _eksport(tmp_path, c1, ledger=led, cel=tmp_path / "e1")
    c2 = _SyncStub({"Subject2": [_meta("W-LIPCU")]}, {"Subject2": [spozniona]},
                   hwm="2026-08-06T10:00:00+00:00")  # teraz mapuje się poprawnie
    rep2, led = _eksport(tmp_path, c2, ledger=led, cel=tmp_path / "e2")
    assert _numery_w_pliku(tmp_path / "e2") == ["W-LIPCU"]
    assert "SPOZNIONA" not in c2.pobrane
    assert rep2.failed == [] and rep2.spoznione == []
    assert [r.ksef_number for r in led.manual_list(NIP)] == ["SPOZNIONA"]
    led.close()


# --------------------------------------------------------------------------- #
# 12. runda 3: N2 — zakres zapisany najwyżej do dnia przed momentem kompletności


def test_n2_eksport_w_trakcie_miesiaca_z_do_koniec_miesiaca_nie_domyka_go(tmp_path):
    """Eksport „na bieżąco” 01–31.10 zrobiony 08.10 (pole „Do” trzyma koniec
    miesiąca): zapisany zakres to 01–07.10 (dzień HWM − 1), więc październik
    NIE jest domknięty — faktura z 05.10, która wpłynie później, czeka na
    domknięcie zamiast iść jako spóźniona do pliku 08–15.10."""
    c1 = _SyncStub({"Subject2": [_meta("W-0102", issue="2026-10-02")]}, {},
                   hwm="2026-10-08T07:00:00+00:00")
    _, led = _eksport(tmp_path, c1, od=date(2026, 10, 1), do=date(2026, 10, 31), cel=tmp_path / "e1")
    assert led.ranges(NIP, "Subject2") == [(date(2026, 10, 1), date(2026, 10, 7))]
    assert not led.is_covered(NIP, "Subject2", date(2026, 10, 5))

    f = _meta("F-0510", issue="2026-10-05", stored="2026-10-10T10:00:00+00:00")
    c2 = _SyncStub({"Subject2": []}, {"Subject2": [f]}, hwm="2026-10-16T07:00:00+00:00")
    rep2, led = _eksport(tmp_path, c2, od=date(2026, 10, 8), do=date(2026, 10, 15), ledger=led,
                         only_new=True, cel=tmp_path / "e2")
    assert rep2.spoznione == [] and c2.pobrane == []
    assert led.sync_point(NIP, "Subject2") == f.permanent_storage_date
    led.close()


def test_n2_hwm_w_dniu_przed_poczatkiem_zakresu_nic_nie_zapisuje(tmp_path):
    """Eksport tuż po północy z „Od” = dziś (21183: HWM = początek sprawdzenia)
    — żaden dzień zakresu nie jest kompletny, zakres się nie zapisuje."""
    c = _SyncStub({"Subject2": []}, {"Subject2": []}, hwm="2026-10-01T00:00:00+02:00")
    _, led = _eksport(tmp_path, c, od=date(2026, 10, 1), do=date(2026, 10, 31))
    assert led.ranges(NIP, "Subject2") == []
    led.close()


def test_n2_bez_hwm_granica_to_wczoraj_w_polsce(tmp_path, monkeypatch):
    """Sprawdzenie „od punktu” pominięte (brak HWM) — granica z zegara:
    wczoraj w czasie polskim."""
    import app.exporter as exporter_mod

    monkeypatch.setattr(exporter_mod, "_teraz_utc",
                        lambda: datetime(2026, 10, 8, 22, 30, tzinfo=timezone.utc))  # 09.10 00:30 PL
    c = _SyncStub({"Subject2": [_meta("W-0102", issue="2026-10-02")]}, {},
                  blad_since=KsefError("query metadata: HTTP 500"))
    _, led = _eksport(tmp_path, c, od=date(2026, 10, 1), do=date(2026, 10, 31))
    assert led.ranges(NIP, "Subject2") == [(date(2026, 10, 1), date(2026, 10, 8))]
    led.close()


# --------------------------------------------------------------------------- #
# 13. runda 3: N3 — przejście ze starszej wersji (pierwsze sprawdzenie)


def _stara_baza(sciezka, wiersze):
    import sqlite3

    conn = sqlite3.connect(str(sciezka))
    conn.execute(
        "CREATE TABLE exports(nip TEXT NOT NULL, ksef_number TEXT NOT NULL, "
        "txt_path TEXT NOT NULL, exported_at TEXT NOT NULL, PRIMARY KEY (nip, ksef_number))"
    )
    conn.executemany("INSERT INTO exports VALUES (?, ?, ?, ?)", wiersze)
    conn.commit()
    conn.close()


def test_n3_pierwsze_sprawdzenie_od_najstarszego_eksportu_starszej_wersji(tmp_path, caplog):
    """Lipiec domknięty starszą wersją 07.08. Faktura lipcowa, która wpłynęła
    10.08 (po starym domknięciu, przed pierwszym eksportem nowej wersji
    08.10), musi zostać wykryta: pierwsze sprawdzenie startuje od najstarszego
    eksportu starszej wersji (nie od północy „Od”). Faktura z czerwca (sprzed
    pracy aplikacji — przed najstarszym zakresem starszej wersji) jest
    pomijana: nie do pliku, nie wstrzymuje punktu, bez ostrzeżenia w oknie."""
    import logging

    k = "1180012345"
    _stara_baza(tmp_path / "ledger.db", [
        (NIP, f"{k}-20260710-AAAA-01", r"C:\eks\test\hmffk_test_2026-07-01_2026-07-15_20260716-100000.TXT",
         "2026-07-16T08:00:00+00:00"),
        (NIP, f"{k}-20260720-BBBB-02", r"C:\eks\test\hmffk_test_2026-07-01_2026-07-31_20260807-100000.TXT",
         "2026-08-07T08:00:00+00:00"),
        (NIP, f"{k}-20260905-CCCC-03", r"C:\eks\test\hmffk_test_2026-09-01_2026-09-15_20260916-100000.TXT",
         "2026-09-16T08:00:00+00:00"),
    ])
    led = Ledger(tmp_path / "ledger.db")
    lipcowa = _meta("LIPCOWA", issue="2026-07-20", stored="2026-08-10T10:00:00+00:00")
    czerwcowa = _meta("CZERWCOWA", issue="2026-06-28", stored="2026-07-20T10:00:00+00:00")
    hwm = "2026-10-08T07:00:00+00:00"
    client = _SyncStub({"Subject2": []}, {"Subject2": [czerwcowa, lipcowa]}, hwm=hwm)

    with caplog.at_level(logging.INFO, logger="app.exporter"):
        rep, led = _eksport(tmp_path, client, od=date(2026, 10, 1), do=date(2026, 10, 7), ledger=led,
                            only_new=True)

    assert client.since_calls == [("Subject2", "2026-07-16T08:00:00+00:00")]
    assert [s.ksef_number for s in rep.spoznione] == ["LIPCOWA"]
    assert "CZERWCOWA" not in client.pobrane
    assert not any("CZERWCOWA" in w or "sprzed pracy" in w for w in rep.warnings)
    assert led.sync_point(NIP, "Subject2") == hwm  # czerwcowa nie wstrzymuje punktu
    assert any("sprzed pracy aplikacji" in r.getMessage() and "1" in r.getMessage()
               for r in caplog.records)
    led.close()


def test_n3_faktura_sprzed_pracy_aplikacji_nie_wpada_przez_bezpiecznik(tmp_path):
    """Zaległość sprzed obserwacji (wpływ przed pierwszym sprawdzeniem) z miesiąca
    sprzed horyzontu — pomijana, choć czeka ponad 60 dni."""
    k = "1180012345"
    _stara_baza(tmp_path / "ledger.db", [
        (NIP, f"{k}-20260720-BBBB-02", r"C:\eks\test\hmffk_test_2026-07-01_2026-07-31_20260807-100000.TXT",
         "2026-08-07T08:00:00+00:00"),
    ])
    led = Ledger(tmp_path / "ledger.db")
    led.set_sync_point(NIP, "Subject2", "2026-05-01T00:00:00+00:00",
                       observed_at="2026-08-10T08:00:00+00:00")  # obserwacja od 10.08
    stara = _meta("MAJOWA", issue="2026-05-20", stored="2026-06-01T08:00:00+00:00")  # >60 dni
    client = _SyncStub({"Subject2": []}, {"Subject2": [stara]}, hwm="2026-10-08T07:00:00+00:00")
    rep, led = _eksport(tmp_path, client, od=date(2026, 10, 1), do=date(2026, 10, 7), ledger=led)
    assert rep.spoznione == [] and client.pobrane == []
    assert led.sync_point(NIP, "Subject2") == "2026-10-08T07:00:00+00:00"
    led.close()


def test_n3_swieza_instalacja_bez_zmian_pierwsze_sprawdzenie_od_polnocy(tmp_path):
    led = Ledger(tmp_path / "ledger.db")
    led.mark_done(NIP, "COS", "hmffk_test_2026-06-01_2026-06-30_20260701-100000.TXT")  # wpis TEJ wersji
    client = _SyncStub({}, {})
    _, led = _eksport(tmp_path, client, od=date(2026, 7, 1), do=date(2026, 7, 31), ledger=led)
    assert client.since_calls == [("Subject2", "2026-07-01T00:00:00+02:00")]
    led.close()


# --------------------------------------------------------------------------- #
# 14. runda 3: D1 — lista do ręcznej obsługi także w głównym zapytaniu


@pytest.mark.parametrize("only_new", [True, False])
def test_d1_do_recznej_pominieta_w_ponownym_eksporcie_jej_miesiaca(tmp_path, only_new):
    """Spóźniona z błędem mapowania trafiła na listę do ręcznej obsługi
    (księgowa zaksięgowała ją ręcznie). Ponowny eksport JEJ miesiąca — także
    po poprawce mappera — nie może jej dołożyć do pliku (dubel z ręcznym)."""
    led = _ledger_z_czerwcem(tmp_path)
    spozniona = _meta("SPOZNIONA", issue="2026-06-28", stored="2026-08-03T08:00:00+00:00")
    c1 = _SyncStub({"Subject2": [_meta("W-LIPCU")]}, {"Subject2": [spozniona]},
                   bledy_pobrania={"SPOZNIONA": b"<Inna/>"})
    _, led = _eksport(tmp_path, c1, ledger=led, cel=tmp_path / "e1")
    assert [r.ksef_number for r in led.manual_list(NIP)] == ["SPOZNIONA"]

    czerwcowa = _meta("CZERWIEC-OK", issue="2026-06-10", stored="2026-06-10T08:00:00+00:00")
    c2 = _SyncStub({"Subject2": [czerwcowa, spozniona]}, {"Subject2": [spozniona]},
                   hwm="2026-08-06T10:00:00+00:00")  # mapper poprawiony: XML dobry
    rep2, led = _eksport(tmp_path, c2, od=date(2026, 6, 1), do=date(2026, 6, 30), ledger=led,
                         only_new=only_new, cel=tmp_path / "e2")

    assert "SPOZNIONA" not in _numery_w_pliku(tmp_path / "e2")
    assert "SPOZNIONA" not in c2.pobrane
    [w] = [w for w in rep2.warnings if "do ręcznej obsługi" in w]
    assert w.startswith("Pominięto 1 fakturę oznaczoną do ręcznej obsługi")
    assert "FV/SPOZNIONA" in w and "2026-06-28" in w
    assert rep2.failed == [] and not rep2.niepelny
    led.close()


def test_n3h_faktura_sprzed_horyzontu_wplywajaca_po_starcie_obserwacji_idzie_zwykla_sciezka(tmp_path):
    """N3-H: horyzont „sprzed pracy aplikacji” dotyczy tylko zaległości (wpływ
    przed początkiem obserwacji). Faktura z czerwca, która wpłynie PO starcie
    obserwacji, czeka (miesiąc niedomknięty — punkt jej nie przeskakuje),
    a po 60 dniach dołącza z bezpiecznika z ostrzeżeniem."""
    k = "1180012345"
    _stara_baza(tmp_path / "ledger.db", [
        (NIP, f"{k}-20260720-BBBB-02", r"C:\eks\test\hmffk_test_2026-07-01_2026-07-31_20260807-100000.TXT",
         "2026-08-07T08:00:00+00:00"),
    ])
    led = Ledger(tmp_path / "ledger.db")
    start = "2026-10-08T07:00:00+00:00"
    led.set_sync_point(NIP, "Subject2", start, observed_at=start)  # obserwacja od 08.10
    ps = "2026-10-15T08:00:00+00:00"
    czerwcowa = _meta("CZERWCOWA", issue="2026-06-20", stored=ps)

    c1 = _SyncStub({"Subject2": []}, {"Subject2": [czerwcowa]}, hwm="2026-10-16T07:00:00+00:00")
    rep1, led = _eksport(tmp_path, c1, od=date(2026, 10, 8), do=date(2026, 10, 15), ledger=led,
                         cel=tmp_path / "e1")
    assert rep1.spoznione == [] and c1.pobrane == []
    assert led.sync_point(NIP, "Subject2") == ps  # wstrzymuje punkt

    c2 = _SyncStub({"Subject2": []}, {"Subject2": [czerwcowa]}, hwm="2026-12-16T07:00:00+00:00")
    rep2, led = _eksport(tmp_path, c2, od=date(2026, 12, 1), do=date(2026, 12, 15), ledger=led,
                         cel=tmp_path / "e2")
    [s] = rep2.spoznione
    assert (s.ksef_number, s.bezpiecznik) == ("CZERWCOWA", True)
    assert any("60 dni" in w for w in rep2.warnings)
    led.close()


# --------------------------------------------------------------------------- #
# 15. runda 4: N3-LIMIT — kolejność, budżet stron, wznawialny punkt


class _Stronicowany(_SyncStub):
    """KSeF z limitem zapytań o metadane i stronicowaniem sprawdzenia „od
    punktu” (`strona` faktur na zapytanie, rosnąco po dacie trwałego zapisu);
    honoruje `przed_strona` jak prawdziwy klient."""

    def __init__(self, *a, budzet=20, strona=2, **k):
        super().__init__(*a, **k)
        self.budzet = budzet
        self.strona = strona
        self.zdarzenia = []

    def metadata_budget_remaining(self):
        return self.budzet

    def query_metadata(self, subject_type, date_from, date_to, *, margin=timedelta(0)):
        self.budzet -= 1
        self.zdarzenia.append(("glowne", subject_type))
        yield from super().query_metadata(subject_type, date_from, date_to, margin=margin)

    def download_invoice(self, ksef_number):
        self.zdarzenia.append(("pobranie", ksef_number))
        return super().download_invoice(ksef_number)

    def query_metadata_since(self, subject_type, since, *, max_lookback=None, przed_strona=None):
        self.since_calls.append((subject_type, since))
        self.zdarzenia.append(("od_punktu", subject_type))
        od = _parse(since)
        wszystkie = sorted(
            (m for m in self._od_punktu.get(subject_type, []) if _parse(m.permanent_storage_date) >= od),
            key=lambda m: _parse(m.permanent_storage_date),
        )
        hwm = self._hwm.get(subject_type) if isinstance(self._hwm, dict) else self._hwm
        wynik = []
        for i in range(0, max(1, len(wszystkie)), self.strona):
            if przed_strona is not None and not przed_strona():
                postep = wynik[-1].permanent_storage_date if wynik else since
                return PermanentStorageQuery(invoices=wynik, hwm=hwm if wynik else None, since=since,
                                             clamped=False, complete=False, progress=postep)
            self.budzet -= 1
            wynik += wszystkie[i:i + self.strona]
        return PermanentStorageQuery(invoices=wynik, hwm=hwm, since=since, clamped=False)


def _parse(s):
    from app.ksef_client import _parse_ksef_datetime
    return _parse_ksef_datetime(s)


def test_limit_najpierw_glowne_zapytania_i_pobieranie_potem_sprawdzenia(tmp_path):
    """(1) Główne zapytania i pobieranie WSZYSTKICH rodzajów przed jakimkolwiek
    sprawdzeniem „od punktu” — sprawdzenie nie może zjeść limitu głównemu
    zapytaniu kosztów."""
    led = _ledger_z_czerwcem(tmp_path, "Subject1", "Subject2")
    client = _Stronicowany(
        {"Subject1": [_meta("S-1")], "Subject2": [_meta("K-1")]},
        {"Subject1": [_meta("S-SPOZN", issue="2026-06-28", stored="2026-08-03T08:00:00+00:00")],
         "Subject2": []},
    )
    rep, led = _eksport(tmp_path, client, kinds={Kind.SALES, Kind.COST}, ledger=led)
    zd = client.zdarzenia
    pierwsze_sprawdzenie = zd.index(("od_punktu", "Subject1"))
    assert zd[:pierwsze_sprawdzenie] == [
        ("glowne", "Subject1"), ("pobranie", "S-1"), ("glowne", "Subject2"), ("pobranie", "K-1"),
    ]
    assert ("pobranie", "S-SPOZN") in zd[pierwsze_sprawdzenie:]
    assert [s.ksef_number for s in rep.spoznione] == ["S-SPOZN"]
    led.close()


def _duzo_sprzedazy(n, dzien="2026-08-03", ledger=None):
    """n faktur sprzedaży z czerwca (miesiąc domknięty), które wpłynęły kolejno
    i są już w rejestrze pobranych — ruch, który sprawdzenie „od punktu” musi
    przejrzeć (strony), choć nic z niego nie trafia do pliku."""
    wynik = [_meta(f"S-{dzien[-2:]}-{i:03d}", issue="2026-06-15",
                   stored=f"{dzien}T08:{i // 60:02d}:{i % 60:02d}+00:00") for i in range(n)]
    if ledger is not None:
        for m in wynik:
            ledger.mark_done(NIP, m.ksef_number, "czerwiec.TXT")
    return wynik


def test_limit_budzet_stron_przerywa_sprawdzenie_bez_niepelnego_i_wznawia(tmp_path):
    """(2)+(3) Sprawdzenie sprzedaży ma 5 stron, a w godzinie zostało mało
    zapytań: przerywa się z rezerwą, eksport NIE jest niepełny (zakres
    kompletny), ostrzeżenie mówi, że dokończy się przy następnym eksporcie,
    a punkt zapisuje się na postępie — następny eksport kontynuuje."""
    led = _ledger_z_czerwcem(tmp_path, "Subject1", "Subject2")
    spozn = _meta("S-SPOZN", issue="2026-06-28", stored="2026-08-03T09:30:00+00:00")  # na 5. stronie
    od_punktu = _duzo_sprzedazy(8, ledger=led) + [spozn]
    hwm = "2026-08-05T10:00:00+00:00"
    c1 = _Stronicowany({"Subject1": [], "Subject2": [_meta("K-1")]},
                       {"Subject1": od_punktu, "Subject2": []}, hwm=hwm, budzet=6, strona=2)

    rep1, led = _eksport(tmp_path, c1, kinds={Kind.SALES, Kind.COST}, ledger=led, cel=tmp_path / "e1")

    assert not rep1.niepelny and rep1.niepobrane == []
    assert _numery_w_pliku(tmp_path / "e1") == ["K-1"]  # koszty w pliku
    assert any("spóźnion" in w and "dokończy się przy następnym eksporcie" in w for w in rep1.warnings)
    assert c1.budzet >= 2  # rezerwa zostaje
    punkt1 = led.sync_point(NIP, "Subject1")
    assert punkt1 is not None and _parse(punkt1) < _parse(spozn.permanent_storage_date)
    assert led.sync_point(NIP, "Subject2") == hwm  # koszty sprawdzone w całości

    # następny eksport (nowa godzina): kontynuacja od punktu, spóźniona wykryta
    c2 = _Stronicowany({"Subject1": [], "Subject2": []},
                       {"Subject1": od_punktu, "Subject2": []}, hwm="2026-08-06T10:00:00+00:00",
                       budzet=20, strona=2)
    rep2, led = _eksport(tmp_path, c2, kinds={Kind.SALES, Kind.COST}, ledger=led, cel=tmp_path / "e2")
    assert c2.since_calls[0] == ("Subject1", punkt1)
    assert [s.ksef_number for s in rep2.spoznione] == ["S-SPOZN"]
    assert led.sync_point(NIP, "Subject1") == "2026-08-06T10:00:00+00:00"
    led.close()


def test_limit_punkt_po_przerwaniu_nie_przeskakuje_wstrzymanej(tmp_path):
    """Po przerwaniu punkt = min(postęp, wpływ najwcześniejszej wstrzymanej)."""
    led = _ledger_z_czerwcem(tmp_path, "Subject1")
    wstrzymana = _meta("SIERPIEN", issue="2026-08-02", stored="2026-08-03T08:00:05+00:00")
    od_punktu = (_duzo_sprzedazy(3, ledger=led) + [wstrzymana]
                 + _duzo_sprzedazy(6, dzien="2026-08-04", ledger=led))
    # budżet 6: główne 1, rezerwa 2 -> 3 strony po 2; przerwanie PO wstrzymanej
    c = _Stronicowany({"Subject1": []}, {"Subject1": od_punktu}, budzet=6, strona=2)
    rep, led = _eksport(tmp_path, c, kinds={Kind.SALES}, ledger=led)
    assert not rep.niepelny
    assert any("przerwane" in w for w in rep.warnings)
    assert led.sync_point(NIP, "Subject1") == wstrzymana.permanent_storage_date
    led.close()


def test_limit_bez_budzetu_na_sprawdzenie_pomija_je_bez_zapisu_punktu(tmp_path):
    c = _Stronicowany({"Subject2": [_meta("K-1")]}, {"Subject2": []}, budzet=3)
    rep, led = _eksport(tmp_path, c)
    assert c.since_calls == []  # 3 − główne 1 = 2 = rezerwa
    assert not rep.niepelny
    assert any("spóźnion" in w and "następnym eksporcie" in w for w in rep.warnings)
    assert led.sync_point(NIP, "Subject2") is None
    led.close()


# --------------------------------------------------------------------------- #
# 16. runda 4: D-N2 — domknięcie przycięte, bo dane KSeF nie są jeszcze kompletne


def test_dn2_domkniecie_ostatniego_dnia_wieczorem_ostrzega(tmp_path):
    c = _SyncStub({"Subject2": [_meta("IX-30", issue="2026-09-30")]}, {},
                  hwm="2026-09-30T18:00:00+00:00")  # 30.09 20:00 PL
    rep, led = _eksport(tmp_path, c, od=date(2026, 9, 1), do=date(2026, 9, 30))
    assert rep.niedomkniete == [
        "Miesiąc wrzesień 2026 nie został jeszcze domknięty (dane KSeF kompletne do "
        "29.09.2026) — powtórz domknięcie jutro."
    ]
    assert rep.niedomkniete[0] in rep.warnings
    assert not led.is_covered(NIP, "Subject2", date(2026, 9, 15))
    led.close()


def test_dn2_eksport_calego_miesiaca_w_jego_trakcie_ostrzega_inaczej(tmp_path):
    c = _SyncStub({"Subject2": []}, {}, hwm="2026-10-08T07:00:00+00:00")
    rep, _ = _eksport(tmp_path, c, od=date(2026, 10, 1), do=date(2026, 10, 31))
    [w] = rep.niedomkniete
    assert w.startswith("Miesiąc październik 2026 nie został jeszcze domknięty (dane KSeF "
                        "kompletne do 07.10.2026)")
    assert "po jego zakończeniu" in w


def test_dn2_domkniecie_po_koncu_miesiaca_bez_ostrzezenia(tmp_path):
    c = _SyncStub({"Subject2": []}, {}, hwm="2026-10-01T07:00:00+00:00")  # 01.10 09:00 PL
    rep, led = _eksport(tmp_path, c, od=date(2026, 9, 1), do=date(2026, 10, 31))
    # wrzesień domknięty; październik (w pełni w zakresie) — jeszcze nie
    assert [w.split(" nie ")[0] for w in rep.niedomkniete] == ["Miesiąc październik 2026"]
    assert led.is_covered(NIP, "Subject2", date(2026, 9, 15))
    led.close()


# --------------------------------------------------------------------------- #
# 17. runda 4: D-N3 — punkt startu ze starszej wersji bez mikrosekund


def test_dn3_legacy_start_bez_mikrosekund(tmp_path):
    k = "1180012345"
    _stara_baza(tmp_path / "ledger.db", [
        (NIP, f"{k}-20260710-AAAA-01", r"C:\eks\test\hmffk_test_2026-07-01_2026-07-15_20260716-100000.TXT",
         "2026-07-16T08:00:00.654321+00:00"),
    ])
    led = Ledger(tmp_path / "ledger.db")
    client = _SyncStub({}, {}, hwm="2026-10-08T07:00:00+00:00")
    _, led = _eksport(tmp_path, client, od=date(2026, 10, 1), do=date(2026, 10, 7), ledger=led)
    assert client.since_calls == [("Subject2", "2026-07-16T08:00:00+00:00")]
    led.close()


def test_budzet_stron_pierwszy_rodzaj_zostawia_po_jednej_stronie_pozostalym():
    """Koszty zwykle potrzebują jednej strony — równy podział marnował połowę
    budżetu, a sprawdzanie sprzedaży przy dużym wolumenie nie nadążało."""
    from app.exporter import REZERWA_ZAPYTAN, ExportReport, Kind, _budzet_stron

    class _Klient:
        def __init__(self, zostalo):
            self.zostalo = zostalo

        def metadata_budget_remaining(self):
            return self.zostalo

    klient = _Klient(20)
    przed_strona = _budzet_stron(klient, 2, Kind.SALES, ExportReport())
    strony = 0
    while przed_strona():
        strony += 1
        klient.zostalo -= 1
    assert strony == 20 - REZERWA_ZAPYTAN - 1
    assert klient.zostalo == REZERWA_ZAPYTAN + 1  # jedna strona czeka na koszty
