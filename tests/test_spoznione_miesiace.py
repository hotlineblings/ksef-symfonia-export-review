"""Regresja spóźnionych faktur na symulatorze KSeF (przegląd 28.09, runda 2).

Symulator podmienia TYLKO warstwę HTTP klienta (`_query_window`, logowanie,
pobieranie, paczki) i ma wirtualny zegar — prawdziwe są: podział na okna
i zapas zapytania po dacie wystawienia (`query_metadata`), okna 90 dni,
przycinanie i HWM synchronizacji po dacie trwałego zapisu
(`query_metadata_since`), a także cały `run_export`, `Ledger` i
`RejestrNumerow`. Zero wywołań prawdziwego KSeF.

Scenariusze (z materiału przeglądu: s1_miesiace, s2_krawedzie, s3_ponowny):
praca „na bieżąco + domknięcie miesiąca” przez 4 miesiące (730 faktur),
eksport starszego okresu po nowszym, błąd paczki jednego rodzaju, przerwanie,
pierwszy eksport tuż po północy, ponowny pełny eksport tego samego zakresu.
"""

from __future__ import annotations

import calendar
import collections
import random
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Profile
from app.exporter import Kind, run_export
from app.kontrahenci_nr import RejestrNumerow
from app.ksef_client import InvoiceMetadata, KsefClient, KsefError, _parse_ksef_datetime
from app.ledger import Ledger

XML = (Path(__file__).resolve().parent / "fixtures" / "fa3_basic_23.xml").read_bytes()
NIP = "5252445719"


@dataclass
class Inv:
    ksef: str
    subject: str
    issue: date
    ps: datetime  # data trwałego zapisu w KSeF (UTC)


def pl(d: date, h: int, m: int = 0) -> datetime:
    """Czas polski -> UTC (lato +02, zima +01 od 25.10.2026 03:00)."""
    off = 2 if datetime(d.year, d.month, d.day, h, m) < datetime(2026, 10, 25, 3) else 1
    return datetime(d.year, d.month, d.day, h, m,
                    tzinfo=timezone(timedelta(hours=off))).astimezone(timezone.utc)


class FakeKsef(KsefClient):
    """KSeF z wirtualnym zegarem; HWM = teraz − 3 min."""

    HWM_LAG = timedelta(minutes=3)

    def __init__(self, invoices, nip, *, fail_batch=()):
        self._nip = nip
        self._cancel = None
        self.invoices = invoices
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.fail_batch = set(fail_batch)
        self._refs = {}

    def close(self):
        pass

    def authenticate(self):
        pass

    def metadata_budget_remaining(self):
        return 20

    def download_budget_remaining(self):
        return 64

    def _query_window(self, subject_type, iso_from, iso_to, date_type, seen, *,
                      restrict_hwm=False, stan=None, przed_zapytaniem=None):
        if przed_zapytaniem is not None and not przed_zapytaniem():
            if stan is not None:
                stan["przerwano"] = True
            return
        f = _parse_ksef_datetime(iso_from)
        widoczne = [i for i in self.invoices if i.subject == subject_type and i.ps <= self.now]
        if date_type == "Issue":
            t = _parse_ksef_datetime(iso_to)
            wyn = sorted((i for i in widoczne if f.date() <= i.issue <= t.date()),
                         key=lambda i: i.issue)
        else:
            hwm = self.now - self.HWM_LAG
            if restrict_hwm and f > hwm:
                raise KsefError(
                    'query metadata: HTTP 400: {"exception":{"exceptionDetailList":'
                    '[{"exceptionCode":21183}]}}'
                )
            t = _parse_ksef_datetime(iso_to) if iso_to else self.now
            if restrict_hwm:
                t = min(t, hwm)
            wyn = sorted((i for i in widoczne if f <= i.ps <= t), key=lambda i: i.ps)
            if stan is not None:
                stan["hwm"] = t.isoformat()
        for i in wyn:
            if i.ksef in seen:
                continue
            seen.add(i.ksef)
            yield InvoiceMetadata(
                ksef_number=i.ksef, invoice_number=f"FV/{i.ksef}",
                issue_date=i.issue.isoformat(), invoicing_date=i.ps.isoformat(),
                acquisition_date=i.ps.isoformat(), seller_nip="1180012345",
                seller_name="Dostawca", buyer_id=NIP, buyer_name="My",
                net=None, gross=None, vat=None, currency="PLN",
                form_system_code="FA (3)", invoice_type="Vat",
                permanent_storage_date=i.ps.isoformat(),
            )

    def query_metadata_since(self, subject_type, since, *, max_lookback=None, now=None,
                             przed_strona=None):
        kw = {} if max_lookback is None else {"max_lookback": max_lookback}
        return super().query_metadata_since(subject_type, since, now=self.now,
                                            przed_strona=przed_strona, **kw)


    def download_invoice(self, ksef_number):
        return XML

    def request_export(self, subject_type, date_from, date_to, *, date_type="Issue"):
        ref = f"REF{len(self._refs)}"
        self._refs[ref] = (subject_type, date_from, date_to)
        if subject_type in self.fail_batch:
            raise KsefError("eksport wsadowy: HTTP 500 (symulacja)")
        return ref

    def wait_for_export(self, reference, **kw):
        return {"parts": []}

    def iter_export_invoices(self, reference, package):
        subject_type, d1, d2 = self._refs[reference]
        for i in self.invoices:
            if i.subject == subject_type and i.ps <= self.now and d1 <= i.issue <= d2:
                yield i.ksef, XML


class LimitowanyKsef(FakeKsef):
    """Model limitu KSeF: 20 zapytań o metadane na godzinę (per NIP), strona
    = `strona` faktur = 1 zapytanie; przekroczenie -> KsefQuotaExceeded.
    Zapytania główne i „od punktu” z tej samej puli; `przed_zapytaniem`
    pytany przed każdą stroną jak w prawdziwym kliencie."""

    LIMIT = 20

    def __init__(self, *a, strona=250, **k):
        super().__init__(*a, **k)
        self.strona = strona
        self.stemple = []

    def _uzyte(self):
        return [t for t in self.stemple if t > self.now - timedelta(hours=1)]

    def metadata_budget_remaining(self):
        return max(0, self.LIMIT - len(self._uzyte()))

    def _query_window(self, subject_type, iso_from, iso_to, date_type, seen, *,
                      restrict_hwm=False, stan=None, przed_zapytaniem=None):
        from app.ksef_client import KsefQuotaExceeded

        wyniki = list(super()._query_window(subject_type, iso_from, iso_to, date_type, set(),
                                            restrict_hwm=restrict_hwm, stan=stan))
        for i in range(max(1, -(-len(wyniki) // self.strona))):
            if przed_zapytaniem is not None and not przed_zapytaniem():
                if stan is not None:
                    stan["przerwano"] = True
                return
            if len(self._uzyte()) >= self.LIMIT:
                raise KsefQuotaExceeded("zapytań o metadane", 0)
            self.stemple.append(self.now)
            for m in wyniki[i * self.strona:(i + 1) * self.strona]:
                if m.ksef_number not in seen:
                    seen.add(m.ksef_number)
                    yield m


def generuj(seed, od, do, dziennie=3, subjects=("Subject1", "Subject2")):
    """70% faktur wpływa w dniu wystawienia, 20% po 1–3 dniach, 8% po 4–10,
    2% po 15–40 dniach (tryb offline, spóźniony wystawca)."""
    rnd = random.Random(seed)
    out, n, d = [], 0, od
    while d <= do:
        for s in subjects:
            for _ in range(dziennie):
                n += 1
                r = rnd.random()
                base = pl(d, rnd.randint(8, 20), rnd.randint(0, 59))
                if r < 0.70:
                    ps = base
                elif r < 0.90:
                    ps = base + timedelta(days=rnd.randint(1, 3))
                elif r < 0.98:
                    ps = base + timedelta(days=rnd.randint(4, 10))
                else:
                    ps = base + timedelta(days=rnd.randint(15, 40))
                out.append(Inv(f"{s[-1]}-{d:%m%d}-{n:05d}", s, d, ps))
        d += timedelta(days=1)
    return out


class Swiat:
    """Jedna firma: rejestr pobranych + rejestr numerów + historia eksportów."""

    def __init__(self, tmp: Path, invoices, nip=NIP, ledger=None, **fake_kw):
        self.tmp = tmp
        self.nip = nip
        self.client = FakeKsef(invoices, nip, **fake_kw)
        self.ledger = ledger or Ledger(tmp / f"ledger-{nip}.db")
        self.rej = RejestrNumerow(tmp / f"rej-{nip}.db")
        self.profile = Profile(name="T", nip=nip, token="x", subfolder=nip)
        self.runs = []

    def eksport(self, kiedy, od, do, *, only_new=True, kinds=(Kind.SALES, Kind.COST), **kw):
        import app.exporter as exporter_mod

        self.client.now = kiedy
        cel = self.tmp / f"run{len(self.runs) + 1:03d}-{self.nip}"
        # zegar eksportu (granica zakresu, gdy brak HWM) = zegar symulacji
        zegar = exporter_mod._teraz_utc
        exporter_mod._teraz_utc = lambda: self.client.now
        try:
            rep = run_export(self.profile, self.client, self.ledger, od, do, set(kinds), cel,
                             fmt="hmffk", only_new=only_new, rejestr_nr=self.rej, **kw)
        finally:
            exporter_mod._teraz_utc = zegar
        numery = []
        for plik in (cel / self.nip).glob("hmffk_*.TXT"):
            numery += [
                linia.split("=", 1)[1].strip()
                for linia in plik.read_bytes().decode("cp1250").splitlines()
                if linia.strip().startswith("KsefNumber =")
            ]
        self.runs.append(dict(kiedy=kiedy, od=od, do=do, rep=rep, numery=numery))
        return rep

    def w_plikach(self):
        return {nr for r in self.runs for nr in r["numery"]}

    def close(self):
        self.ledger.close()
        self.rej.close()


@pytest.fixture
def swiat_factory(tmp_path):
    swiaty = []

    def zrob(invoices, **kw):
        sw = Swiat(tmp_path, invoices, **kw)
        swiaty.append(sw)
        return sw

    yield zrob
    for sw in swiaty:
        sw.close()


# --------------------------------------------------------------------------- #
# s1: 4 miesiące „na bieżąco + domknięcie” (sprzedaż + koszt, tylko nowe)


def _harmonogram(do_koniec_miesiaca=False):
    """`do_koniec_miesiaca` — eksport „na bieżąco” 16. dnia z „Do” = koniec
    miesiąca (pole „Do” w GUI trzyma koniec miesiąca) — scenariusz N2."""
    h = []
    for m, nast in [(8, 9), (9, 10), (10, 11), (11, 12)]:
        koniec = 31 if m in (8, 10, 12) else 30
        h.append((pl(date(2026, m, 16), 10), date(2026, m, 1),
                  date(2026, m, koniec if do_koniec_miesiaca else 15)))
        h.append((pl(date(2026, nast, 1), 10), date(2026, m, 16), date(2026, m, koniec)))
        h.append((pl(date(2026, nast, 8), 10), date(2026, nast, 1), date(2026, nast, 7)))
    for m in (8, 9, 10, 11):  # domknięcia miesięcy (12. następnego miesiąca)
        koniec = 31 if m in (8, 10) else 30
        h.append((pl(date(2026, m + 1, 12), 12), date(2026, m, 1), date(2026, m, koniec)))
    return sorted(h)


def _domyka(r0, d):
    """Czy przebieg r0 FAKTYCZNIE domknął miesiąc daty d: zakres obejmuje cały
    miesiąc, a eksport zrobiono po jego końcu (dzień eksportu w PL − 1 ≥
    ostatni dzień miesiąca) — ta sama reguła co N2."""
    ostatni = calendar.monthrange(d.year, d.month)[1]
    dzien_pl = (r0["kiedy"] + timedelta(hours=2)).date()
    return (r0["od"] <= d.replace(day=1) and r0["do"] >= d.replace(day=ostatni)
            and dzien_pl - timedelta(days=1) >= d.replace(day=ostatni))


@pytest.mark.parametrize("do_koniec_miesiaca", [False, True], ids=["s1", "s1_do_koniec"])
def test_s1_miesiace_bez_utraty_bez_dubli_przeniesienia_tylko_prawdziwie_spoznione(
    swiat_factory, do_koniec_miesiaca,
):
    inv = generuj(seed=7, od=date(2026, 7, 20), do=date(2026, 12, 20))
    po_nr = {i.ksef: i for i in inv}
    sw = swiat_factory(inv)
    for kiedy, od, do in _harmonogram(do_koniec_miesiaca):
        rep = sw.eksport(kiedy, od, do)
        assert not rep.niepelny and not rep.failed and not rep.niepobrane

    wystapienia = collections.Counter(nr for r in sw.runs for nr in r["numery"])
    ostatni = sw.runs[-1]["kiedy"]
    oczekiwane = [i for i in inv if date(2026, 8, 1) <= i.issue <= date(2026, 11, 30)
                  and i.ps <= ostatni]
    assert len(oczekiwane) == 730
    assert [i.ksef for i in oczekiwane if wystapienia[i.ksef] == 0] == []  # utracone
    assert [i.ksef for i in oczekiwane if wystapienia[i.ksef] > 1] == []   # duble

    # W2: faktura w pliku zakresu, który nie obejmuje jej daty wystawienia,
    # musi być spóźniona względem DOMKNIĘCIA swojego miesiąca (eksport całego
    # miesiąca po jego końcu, przed jej wpływem) albo dołączona z bezpiecznika
    # — i zgłoszona w raporcie tego eksportu. Zakresy „na bieżąco” — także
    # z „Do” = koniec miesiąca zrobione w trakcie miesiąca (N2) — nie domykają.
    przedwczesne, prawdziwe, bezpiecznik = [], [], []
    for n, r in enumerate(sw.runs):
        zgloszone = {s.ksef_number: s for s in r["rep"].spoznione}
        for nr in r["numery"]:
            i = po_nr[nr]
            if r["od"] <= i.issue <= r["do"]:
                continue
            assert nr in zgloszone, f"{nr} poza zakresem pliku, a nie zgłoszona jako spóźniona"
            assert i.issue < r["od"] or zgloszone[nr].bezpiecznik  # tylko do późniejszego okresu
            if zgloszone[nr].bezpiecznik:
                bezpiecznik.append(nr)
            elif any(_domyka(r0, i.issue) and r0["kiedy"] < i.ps for r0 in sw.runs[:n]):
                prawdziwe.append(nr)
            else:
                przedwczesne.append(nr)
    assert przedwczesne == []
    assert prawdziwe  # mechanizm działa (w tym zestawie danych: kilka)
    # bezpiecznik dotyczy tylko okresu, którego ta firma nigdy nie eksportowała (lipiec)
    assert all(po_nr[nr].issue < date(2026, 8, 1) for nr in bezpiecznik)


# --------------------------------------------------------------------------- #
# s2: krawędzie


def test_s2_starszy_okres_po_nowszym_nie_trafia_do_pliku_nowszego(swiat_factory):
    """Pierwsze użycie nowej wersji: wrzesień zrobiony starszą wersją (bez
    zakresów w rejestrze, nazwa pliku bez dat), najpierw eksport 01–07.10,
    potem domknięcie września. Wrześniowe faktury, które wpłynęły po 30.09,
    nie mogą wpaść do pliku październikowego — bierze je domknięcie."""
    inv = generuj(seed=3, od=date(2026, 9, 1), do=date(2026, 10, 20))
    sw = swiat_factory(inv)
    stan_30 = pl(date(2026, 9, 30), 20)
    for i in inv:
        if i.issue <= date(2026, 9, 30) and i.ps <= stan_30:
            sw.ledger.mark_done(sw.nip, i.ksef, "stara-wersja.TXT")
    sw.eksport(pl(date(2026, 10, 8), 9), date(2026, 10, 1), date(2026, 10, 7))
    rep2 = sw.eksport(pl(date(2026, 10, 12), 9), date(2026, 9, 1), date(2026, 9, 30))

    po_nr = {i.ksef: i for i in inv}
    assert [nr for nr in sw.runs[0]["numery"] if po_nr[nr].issue.month == 9] == []
    spoznione_wrzesniowe = {i.ksef for i in inv
                            if i.issue.month == 9 and stan_30 < i.ps <= sw.runs[1]["kiedy"]}
    assert spoznione_wrzesniowe and spoznione_wrzesniowe <= set(sw.runs[1]["numery"])
    assert rep2.spoznione == []  # we własnym okresie to zwykłe faktury


def test_s2_blad_paczki_sprzedazy_niepobrane_i_punkt_kosztow_idzie(swiat_factory):
    inv = generuj(seed=5, od=date(2026, 7, 1), do=date(2026, 9, 30), dziennie=4)
    sw = swiat_factory(inv, fail_batch={"Subject1"})
    rep = sw.eksport(pl(date(2026, 9, 20), 9), date(2026, 7, 1), date(2026, 9, 30), only_new=False)

    assert rep.failed == []  # nic do ręcznego księgowania
    sprzedaz = {i.ksef for i in inv if i.subject == "Subject1" and i.ps <= sw.client.now
                and date(2026, 7, 1) <= i.issue <= date(2026, 9, 30)}
    assert {nr for nr, _ in rep.niepobrane} == sprzedaz
    assert rep.niepelny
    assert sw.ledger.sync_point(sw.nip, "Subject1") is None
    assert sw.ledger.sync_point(sw.nip, "Subject2") is not None
    assert sw.ledger.ranges(sw.nip, "Subject1") == []
    # N2: eksport z 20.09 obejmuje naprawdę dni do 19.09 (wrzesień niedomknięty)
    assert sw.ledger.ranges(sw.nip, "Subject2") == [(date(2026, 7, 1), date(2026, 9, 19))]

    # ponowny eksport z „Pomiń już pobrane” dociąga sprzedaż bez dubli
    sw.client.fail_batch.clear()
    rep2 = sw.eksport(pl(date(2026, 9, 20), 10), date(2026, 7, 1), date(2026, 9, 30))
    assert not rep2.niepelny and rep2.niepobrane == []
    wystapienia = collections.Counter(nr for r in sw.runs for nr in r["numery"])
    assert sprzedaz <= set(wystapienia) and max(wystapienia.values()) == 1


def test_s2_przerwanie_i_ponowny_eksport_bez_brakow(swiat_factory):
    inv = generuj(seed=9, od=date(2026, 8, 25), do=date(2026, 9, 10), dziennie=1)
    sw = swiat_factory(inv)
    cancel = threading.Event()
    licznik = {"n": 0}
    oryginal = sw.client.download_invoice

    def pobierz(nr):
        licznik["n"] += 1
        if licznik["n"] == 3:
            cancel.set()
        return oryginal(nr)

    sw.client.download_invoice = pobierz
    rep = sw.eksport(pl(date(2026, 9, 11), 9), date(2026, 9, 1), date(2026, 9, 10), cancel=cancel)
    assert rep.cancelled
    w_rejestrze = {i.ksef for i in inv if sw.ledger.is_done(sw.nip, i.ksef)}
    assert w_rejestrze == sw.w_plikach()
    assert sw.ledger.sync_point(sw.nip, "Subject1") is None
    assert sw.ledger.ranges(sw.nip, "Subject1") == []

    sw.client.download_invoice = oryginal
    sw.eksport(pl(date(2026, 9, 11), 10), date(2026, 9, 1), date(2026, 9, 10))
    oczekiwane = {i.ksef for i in inv if i.ps <= pl(date(2026, 9, 11), 10)
                  and date(2026, 9, 1) <= i.issue <= date(2026, 9, 10)}
    assert oczekiwane - sw.w_plikach() == set()


def test_s2_dwie_firmy_wspolny_rejestr_osobne_punkty_i_zakresy(tmp_path, swiat_factory):
    wspolny = Ledger(tmp_path / "ledger.db")  # jeden %APPDATA%\\ledger.db jak w aplikacji
    a = swiat_factory(generuj(seed=11, od=date(2026, 8, 20), do=date(2026, 9, 30), dziennie=1),
                      nip="5252445719", ledger=wspolny)
    b = swiat_factory(generuj(seed=12, od=date(2026, 8, 20), do=date(2026, 9, 30), dziennie=1),
                      nip="1180012345", ledger=wspolny)
    a.eksport(pl(date(2026, 9, 16), 9), date(2026, 9, 1), date(2026, 9, 15))
    assert wspolny.sync_point(b.nip, "Subject2") is None
    assert wspolny.ranges(b.nip, "Subject2") == []
    b.eksport(pl(date(2026, 9, 16), 9, 5), date(2026, 9, 1), date(2026, 9, 15))
    assert wspolny.sync_point(b.nip, "Subject2") is not None
    b.ledger = None  # zamyka go `a`
    b.close = lambda: b.rej.close()


def test_s2_pierwszy_eksport_tuz_po_polnocy_bez_ostrzezenia(swiat_factory):
    """„Od” = dziś, eksport o 00:01 czasu polskiego: początek sprawdzenia jest
    późniejszy niż HWM (KSeF 21183) — nic nowego, bez ostrzeżenia; punkt
    startuje od północy, więc faktura z domkniętego sierpnia, która wpłynie
    rano, trafi do następnego eksportu jako spóźniona."""
    inv = generuj(seed=13, od=date(2026, 9, 1), do=date(2026, 9, 28), dziennie=1)
    sierpniowa = Inv("2-0831-SPOZN", "Subject2", date(2026, 8, 31), pl(date(2026, 9, 28), 9))
    inv.append(sierpniowa)
    sw = swiat_factory(inv)
    sw.ledger.record_range(sw.nip, "Subject2", date(2026, 8, 1), date(2026, 8, 31))
    rep = sw.eksport(datetime(2026, 9, 27, 22, 1, tzinfo=timezone.utc),
                     date(2026, 9, 28), date(2026, 9, 30))
    assert not any("spóźnion" in w for w in rep.warnings)
    assert sw.ledger.sync_point(sw.nip, "Subject2") == "2026-09-28T00:00:00+02:00"

    rep2 = sw.eksport(pl(date(2026, 9, 29), 10), date(2026, 9, 29), date(2026, 9, 29))
    assert "2-0831-SPOZN" in {s.ksef_number for s in rep2.spoznione}


# --------------------------------------------------------------------------- #
# s3: ponowny pełny eksport tego samego zakresu (W3)


def test_s3_ponowny_pelny_eksport_tego_samego_zakresu_daje_ten_sam_plik(swiat_factory):
    inv = [
        Inv("SIERPIEN-OK", "Subject2", date(2026, 8, 20), pl(date(2026, 8, 20), 10)),
        Inv("SIERPIEN-SPOZN", "Subject2", date(2026, 8, 28), pl(date(2026, 9, 10), 10)),
        Inv("WRZ-1", "Subject2", date(2026, 9, 5), pl(date(2026, 9, 5), 10)),
    ]
    sw = swiat_factory(inv)
    koszt = (Kind.COST,)
    sw.eksport(pl(date(2026, 9, 2), 9), date(2026, 8, 1), date(2026, 8, 31), only_new=False, kinds=koszt)
    sw.eksport(pl(date(2026, 9, 16), 9), date(2026, 9, 1), date(2026, 9, 15), only_new=False, kinds=koszt)
    pierwszy = sorted(sw.runs[-1]["numery"])
    assert pierwszy == ["SIERPIEN-SPOZN", "WRZ-1"]
    # import w Symfonii się nie udał — ten sam zakres, pełny eksport
    rep = sw.eksport(pl(date(2026, 9, 16), 11), date(2026, 9, 1), date(2026, 9, 15),
                     only_new=False, kinds=koszt)
    assert sorted(sw.runs[-1]["numery"]) == pierwszy
    assert [s.ksef_number for s in rep.spoznione] == ["SIERPIEN-SPOZN"]
    # z „Pomiń już pobrane” — nic
    sw.eksport(pl(date(2026, 9, 16), 12), date(2026, 9, 1), date(2026, 9, 15), kinds=koszt)
    assert sw.runs[-1]["numery"] == []


# --------------------------------------------------------------------------- #
# s4: pierwszy dzień po wdrożeniu — rejestr starszej wersji (N2/N3)

DOSTAWCY = ["1180012345", "9100010385", "9100010391", "9542750223"]


def _generuj_s4(seed, od, do, dziennie=3):
    """Jak `generuj`, ale numery KSeF realistyczne (NIP sprzedawcy na początku),
    bo migracja zakresów rozpoznaje po nich rodzaj."""
    rnd = random.Random(seed)
    out, d = [], od
    while d <= do:
        for s in ("Subject1", "Subject2"):
            for _ in range(dziennie):
                r = rnd.random()
                base = pl(d, rnd.randint(8, 20), rnd.randint(0, 59))
                if r < 0.70:
                    ps = base
                elif r < 0.90:
                    ps = base + timedelta(days=rnd.randint(1, 3))
                elif r < 0.97:
                    ps = base + timedelta(days=rnd.randint(4, 10))
                else:
                    ps = base + timedelta(days=rnd.randint(15, 40))
                sprzedawca = NIP if s == "Subject1" else rnd.choice(DOSTAWCY)
                nr = f"{sprzedawca}-{ps:%Y%m%d}-{rnd.getrandbits(48):012X}-{rnd.randint(0, 255):02X}"
                out.append(Inv(nr, s, d, ps))
        d += timedelta(days=1)
    return out


def _stara_wersja(sciezka: Path, inv, przebiegi):
    """Eksporty starszej wersji: rejestr z samą tabelą exports (jak na main),
    ścieżki Windows hmffk_<podfolder>_<od>_<do>_<stempel czasu PL>.TXT."""
    import sqlite3

    conn = sqlite3.connect(str(sciezka))
    conn.execute(
        "CREATE TABLE exports(nip TEXT NOT NULL, ksef_number TEXT NOT NULL, "
        "txt_path TEXT NOT NULL, exported_at TEXT NOT NULL, PRIMARY KEY (nip, ksef_number))"
    )
    runs = []
    for kiedy, od, do, only_new in przebiegi:
        stempel = kiedy.astimezone(timezone(timedelta(hours=2))).strftime("%Y%m%d-%H%M%S")
        plik = f"C:\\Users\\Ksiegowa\\KSeF\\{NIP}\\hmffk_{NIP}_{od}_{do}_{stempel}.TXT"
        wybrane = [i for i in inv if od <= i.issue <= do and i.ps <= kiedy - FakeKsef.HWM_LAG]
        if only_new:
            juz = {r[0] for r in conn.execute("SELECT ksef_number FROM exports WHERE nip=?", (NIP,))}
            wybrane = [i for i in wybrane if i.ksef not in juz]
        conn.executemany("INSERT OR REPLACE INTO exports VALUES (?, ?, ?, ?)",
                         [(NIP, i.ksef, plik, kiedy.isoformat()) for i in wybrane])
        runs.append(dict(kiedy=kiedy, od=od, do=do, numery=[i.ksef for i in wybrane], rep=None))
    conn.commit()
    conn.close()
    return runs


def _s4(tmp_path, wariant):
    inv = _generuj_s4(21, date(2026, 6, 20), date(2026, 11, 20))
    stare = [
        (pl(date(2026, 7, 16), 10), date(2026, 7, 1), date(2026, 7, 15), False),
        (pl(date(2026, 8, 7), 10), date(2026, 7, 1), date(2026, 7, 31), True),    # domknięcie VII
        (pl(date(2026, 8, 16), 10), date(2026, 8, 1), date(2026, 8, 15), False),
        (pl(date(2026, 9, 1), 10), date(2026, 8, 16), date(2026, 8, 31), False),
        (pl(date(2026, 9, 10), 10), date(2026, 8, 1), date(2026, 8, 31), True),   # domknięcie VIII
        (pl(date(2026, 9, 16), 10), date(2026, 9, 1), date(2026, 9, 15), False),  # ostatni stary
    ]
    if wariant == "D":  # ostatni stary eksport „na bieżąco” z „Do” = koniec miesiąca
        stare[-1] = (pl(date(2026, 9, 16), 10), date(2026, 9, 1), date(2026, 9, 30), False)
    start = pl(date(2026, 10, 8), 9)
    x_biez = (start, date(2026, 10, 1), date(2026, 10, 7))
    ix_dom = (start + timedelta(minutes=30), date(2026, 9, 1), date(2026, 9, 30))
    if wariant == "B":  # najpierw domknięcie września
        ix_dom = (start, date(2026, 9, 1), date(2026, 9, 30))
        x_biez = (start + timedelta(minutes=30), date(2026, 10, 1), date(2026, 10, 7))
    if wariant == "C":  # jednorazowe nadrobienie VII–IX
        ix_dom = (start + timedelta(minutes=30), date(2026, 7, 1), date(2026, 9, 30))
    nowe = sorted([x_biez, ix_dom]) + [
        (pl(date(2026, 10, 16), 10), date(2026, 10, 1), date(2026, 10, 15)),
        (pl(date(2026, 11, 1), 10), date(2026, 10, 16), date(2026, 10, 31)),
        (pl(date(2026, 11, 12), 12), date(2026, 10, 1), date(2026, 10, 31)),  # domknięcie X
        (pl(date(2026, 11, 16), 10), date(2026, 11, 1), date(2026, 11, 15)),
    ]
    runs = _stara_wersja(tmp_path / "ledger.db", inv, stare)
    sw = Swiat(tmp_path, inv, ledger=Ledger(tmp_path / "ledger.db"))
    for kiedy, od, do in nowe:
        sw.eksport(kiedy, od, do)
    wszystkie = runs + sw.runs
    po_nr = {i.ksef: i for i in inv}
    koniec = wszystkie[-1]["kiedy"]
    wyst = collections.Counter(nr for r in wszystkie for nr in r["numery"])
    ocz = [i for i in inv if date(2026, 7, 1) <= i.issue <= date(2026, 11, 15)
           and i.ps <= koniec - FakeKsef.HWM_LAG]
    przedwczesne = []
    for n, r in enumerate(wszystkie):
        for nr in r["numery"]:
            i = po_nr[nr]
            if r["od"] <= i.issue <= r["do"]:
                continue
            if not any(_domyka(r0, i.issue) and r0["kiedy"] < i.ps for r0 in wszystkie[:n]):
                przedwczesne.append(nr)
    wynik = dict(
        oczekiwane=len(ocz),
        utracone=[i.ksef for i in ocz if wyst[i.ksef] == 0],
        duble=[i.ksef for i in ocz if wyst[i.ksef] > 1],
        przedwczesne=przedwczesne,
        czerwcowe_w_nowych=[nr for r in sw.runs for nr in r["numery"] if po_nr[nr].issue.month == 6],
        sw=sw,
    )
    return wynik


@pytest.mark.parametrize("wariant", ["A", "B", "C", "D"])
def test_s4_pierwszy_dzien_po_wdrozeniu_bez_utraty_i_dubli(tmp_path, wariant):
    """A: październik na bieżąco, potem domknięcie września; B: odwrotnie;
    C: jednorazowe nadrobienie VII–IX; D: ostatni stary eksport 01–30.09
    zrobiony 16.09. Spóźnione z miesięcy domkniętych starszą wersją, które
    wpłynęły między starym domknięciem a pierwszym eksportem nowej wersji,
    trafiają do plików (N3); faktury z czerwca (sprzed pracy aplikacji) — nie."""
    w = _s4(tmp_path, wariant)
    try:
        assert w["oczekiwane"] == 821
        assert w["utracone"] == []
        assert w["duble"] == []
        assert w["przedwczesne"] == []
        assert w["czerwcowe_w_nowych"] == []
        if wariant == "D":  # N2 w migracji: stary plik 01–30.09 z 16.09 nie domyka września
            migrowane = [z for z in w["sw"].ledger.ranges(NIP, "Subject2") if z[0] == date(2026, 9, 1)]
            # 01–15.09 ze starego pliku + 01–30.09 z domknięcia nowej wersji (08.10)
            assert migrowane == [(date(2026, 9, 1), date(2026, 9, 15)), (date(2026, 9, 1), date(2026, 9, 30))]
    finally:
        w["sw"].close()


# --------------------------------------------------------------------------- #
# s5: lista do ręcznej obsługi (D1)


def test_s5_do_recznej_po_poprawce_mappera_nie_trafia_do_pliku(swiat_factory):
    zla = b'<?xml version="1.0"?><Faktura xmlns="http://crd.gov.pl/wzor/2025/06/25/13775/"><Naglowek/></Faktura>'
    inv = [
        Inv("SIERPIEN-OK", "Subject2", date(2026, 8, 20), pl(date(2026, 8, 20), 10)),
        Inv("SIERPIEN-ZLA", "Subject2", date(2026, 8, 28), pl(date(2026, 9, 10), 10)),
        Inv("WRZ-1", "Subject2", date(2026, 9, 5), pl(date(2026, 9, 5), 10)),
    ]
    sw = swiat_factory(inv)
    stan = {"naprawione": False}
    sw.client.download_invoice = (
        lambda nr: XML if (nr != "SIERPIEN-ZLA" or stan["naprawione"]) else zla
    )
    koszt = (Kind.COST,)
    sw.eksport(pl(date(2026, 9, 2), 9), date(2026, 8, 1), date(2026, 8, 31), kinds=koszt)
    r1 = sw.eksport(pl(date(2026, 9, 16), 9), date(2026, 9, 1), date(2026, 9, 15), kinds=koszt)
    assert [nr for nr, _ in r1.failed] == ["SIERPIEN-ZLA"]
    assert [r.ksef_number for r in sw.ledger.manual_list(sw.nip)] == ["SIERPIEN-ZLA"]
    # księgowa zaksięgowała ręcznie; ponowny eksport sierpnia z „Pomiń” — błąd trwały
    r2 = sw.eksport(pl(date(2026, 9, 20), 9), date(2026, 8, 1), date(2026, 8, 31), kinds=koszt)
    assert sw.runs[-1]["numery"] == [] and r2.failed == []
    # poprawka mappera w nowej wersji — nadal nie do pliku (byłby dubel z ręcznym)
    stan["naprawione"] = True
    for only_new in (True, False):
        r3 = sw.eksport(pl(date(2026, 10, 20), 9), date(2026, 8, 1), date(2026, 8, 31),
                        kinds=koszt, only_new=only_new)
        assert "SIERPIEN-ZLA" not in sw.runs[-1]["numery"]
        assert any(w.startswith("Pominięto 1 fakturę oznaczoną do ręcznej obsługi")
                   for w in r3.warnings)


# --------------------------------------------------------------------------- #
# limit: pierwsze sprawdzenie „od punktu” przy dużej sprzedaży (N3-LIMIT)
# Skala 1:10 względem modelu recenzenta (limit.py: strona 250, 65–300
# sprzedaży/dzień, 5 kosztów/dzień): strona 25, 6–30 sprzedaży/dzień i koszt co
# drugi dzień — ta sama liczba stron na dzień, 10× mniej faktur w teście.


def _limit(tmp_path, dziennie):
    rnd = random.Random(5)
    inv = []
    d = date(2026, 6, 25)
    while d <= date(2026, 11, 20):
        for j in range(dziennie):
            inv.append(Inv(f"{NIP}-{d:%Y%m%d}-S{j:05d}", "Subject1", d,
                           pl(d, 8 + j % 12) + timedelta(days=(3 if rnd.random() < 0.05 else 0))))
        if d.toordinal() % 2 == 0:
            inv.append(Inv(f"1180012345-{d:%Y%m%d}-K000", "Subject2", d, pl(d, 9)))
        d += timedelta(days=1)
    # spóźnione do wykrycia: sierpniowa sprzedaż po starym domknięciu sierpnia
    # (10.09), przed pierwszym eksportem nowej wersji; wrześniowy koszt po
    # domknięciu września (08.10) nową wersją
    inv.append(Inv(f"{NIP}-20260920-SPOZN-VIII", "Subject1", date(2026, 8, 20), pl(date(2026, 9, 20), 10)))
    inv.append(Inv("1180012345-20261015-SPOZN-IX", "Subject2", date(2026, 9, 25), pl(date(2026, 10, 15), 10)))

    stare = [(pl(date(2026, 8, 7), 10), date(2026, 7, 1), date(2026, 7, 31), False),
             (pl(date(2026, 9, 10), 10), date(2026, 8, 1), date(2026, 8, 31), False)]
    runs = _stara_wersja(tmp_path / "ledger.db", inv, stare)
    sw = Swiat(tmp_path, inv, ledger=Ledger(tmp_path / "ledger.db"))
    sw.client = LimitowanyKsef(inv, NIP, strona=25)
    harm = [(pl(date(2026, 10, 8), 9), date(2026, 10, 1), date(2026, 10, 7)),
            (pl(date(2026, 10, 8), 11), date(2026, 9, 1), date(2026, 9, 30)),
            (pl(date(2026, 10, 16), 9), date(2026, 10, 1), date(2026, 10, 15)),
            (pl(date(2026, 11, 2), 9), date(2026, 10, 16), date(2026, 10, 31)),
            (pl(date(2026, 11, 12), 12), date(2026, 10, 1), date(2026, 10, 31))]
    for dzien in range(13, 21):  # dalej na bieżąco — sprawdzenie nadrabia zaległość
        harm.append((pl(date(2026, 11, dzien), 9), date(2026, 11, 1), date(2026, 11, dzien - 1)))
    for kiedy, od, do in harm:
        sw.eksport(kiedy, od, do)
    return inv, runs, sw


@pytest.mark.parametrize("dziennie", [6, 7, 10, 13, 15, 30])  # ≈ 65/70/100/130/150/300 przy stronie 250
def test_limit_duza_sprzedaz_bez_niepelnych_koszty_w_pliku_spoznione_wykryte(tmp_path, dziennie):
    inv, stare, sw = _limit(tmp_path, dziennie)
    try:
        po_nr = {i.ksef: i for i in inv}
        for r in sw.runs:
            rep = r["rep"]
            # główne zapytanie idzie z zapasem ±1 dzień (QUERY_MARGIN)
            widoczne = [i for i in inv
                        if r["od"] - timedelta(days=1) <= i.issue <= r["do"] + timedelta(days=1)
                        and i.ps <= r["kiedy"]]
            glowne_strony = sum(
                max(1, -(-sum(1 for i in widoczne if i.subject == st) // 25))
                for st in ("Subject1", "Subject2")
            )
            if glowne_strony > LimitowanyKsef.LIMIT:
                # samo główne zapytanie (domknięcie miesiąca przy 300/dzień) nie
                # mieści się w 20/h — ograniczenie wolumenu, nie sprawdzenia
                assert rep.niepelny
                continue
            assert not rep.niepelny, (r["od"], r["do"], rep.warnings[:2])
            assert rep.niepobrane == []
            # koszty z zakresu tego eksportu (już w KSeF) są w pliku albo były wcześniej
            koszty = {i.ksef for i in inv if i.subject == "Subject2" and r["od"] <= i.issue <= r["do"]
                      and i.ps <= r["kiedy"] - FakeKsef.HWM_LAG}
            assert koszty <= {nr for x in stare + sw.runs[:sw.runs.index(r) + 1] for nr in x["numery"]}
        wszystkie = stare + sw.runs
        wyst = collections.Counter(nr for x in wszystkie for nr in x["numery"])
        koniec = sw.runs[-1]["kiedy"]
        ocz = [i for i in inv if date(2026, 7, 1) <= i.issue <= date(2026, 10, 31)
               and i.ps <= koniec - FakeKsef.HWM_LAG]
        # spóźnione: nic nie ginie — w pliku (raz) albo punkt jej rodzaju jest
        # przed nią (kolejne sprawdzenie ją zobaczy); przy ≤130/dzień wykryte
        spoznione = {s.ksef_number for x in sw.runs for s in x["rep"].spoznione}
        for nr, st in ((f"{NIP}-20260920-SPOZN-VIII", "Subject1"),
                       ("1180012345-20261015-SPOZN-IX", "Subject2")):
            if nr in spoznione:
                assert wyst[nr] == 1
            else:
                assert dziennie > 13, f"{nr} niewykryta przy {dziennie}/dzień"
                punkt = _parse_ksef_datetime(sw.ledger.sync_point(NIP, st))
                assert punkt is None or punkt <= po_nr[nr].ps
        # z oczekiwanych wyłączam to, na co czeka niedomknięty miesiąc (przy
        # dużej sprzedaży domknięcie nie mieści się w limicie — patrz wyżej)
        ocz = [i for i in ocz if wyst[i.ksef] or not (
            i.ksef.endswith(("SPOZN-VIII", "SPOZN-IX"))
            or not sw.ledger.is_covered(NIP, i.subject, i.issue)
        )]
        if dziennie >= 13:  # przy dużej sprzedaży sprawdzenie naprawdę się przerywa i wznawia
            assert any(any("przerwane" in w for w in x["rep"].warnings) for x in sw.runs)
        assert all(po_nr[nr].issue.month != 6 for x in sw.runs for nr in x["numery"])
    finally:
        sw.close()


# --------------------------------------------------------------------------- #
# horyzont (N3-H): faktura sprzed horyzontu, która wpływa PO starcie obserwacji


def test_horyzont_faktura_sprzed_pracy_aplikacji_po_przejsciu_dolacza_z_bezpiecznika(tmp_path):
    inv = [
        Inv("1180012345-20260705-LIPIEC-01", "Subject2", date(2026, 7, 5), pl(date(2026, 7, 5), 10)),
        Inv("1180012345-20260620-CZERWIEC-PO-PRZEJSCIU", "Subject2", date(2026, 6, 20),
            pl(date(2026, 10, 15), 10)),
        Inv("1180012345-20260625-CZERWIEC-W-LUCE", "Subject2", date(2026, 6, 25), pl(date(2026, 8, 20), 10)),
        Inv("1180012345-20260705-LIPIEC-PO-PRZEJSCIU", "Subject2", date(2026, 7, 10),
            pl(date(2026, 10, 15), 10)),
    ]
    _stara_wersja(tmp_path / "ledger.db", inv[:1], [(pl(date(2026, 8, 7), 10), date(2026, 7, 1),
                                                     date(2026, 7, 31), False)])
    sw = Swiat(tmp_path, inv, ledger=Ledger(tmp_path / "ledger.db"))
    try:
        harm = [(pl(date(2026, 10, 8), 9), date(2026, 10, 1), date(2026, 10, 7))]
        d = date(2026, 10, 16)
        while d < date(2027, 1, 31):
            harm.append((pl(d, 9), d - timedelta(days=7), d - timedelta(days=1)))
            d += timedelta(days=14)
        for kiedy, od, do in harm:
            sw.eksport(kiedy, od, do, kinds=(Kind.COST,))
        gdzie = {nr: x for x in sw.runs for nr in x["numery"]}
        assert "1180012345-20260625-CZERWIEC-W-LUCE" not in gdzie  # zaległość sprzed obserwacji
        assert "1180012345-20260705-LIPIEC-PO-PRZEJSCIU" in gdzie   # lipiec domknięty starszą wersją
        x = gdzie["1180012345-20260620-CZERWIEC-PO-PRZEJSCIU"]      # po starcie obserwacji
        [s] = [s for s in x["rep"].spoznione if "CZERWIEC" in s.ksef_number]
        assert s.bezpiecznik
        assert any("60 dni" in w and "CZERWIEC-PO-PRZEJSCIU" in w for w in x["rep"].warnings)
        assert x["kiedy"] - pl(date(2026, 10, 15), 10) > timedelta(days=60)
    finally:
        sw.close()
