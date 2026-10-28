"""Faza 1 eksportu przy dużych wolumenach: sonda (pierwsza strona metadanych)
rozstrzyga — do 250 faktur dotychczasowa lista, powyżej paczki eksportu
(metadane z `_metadata.json`, XML już w ręku). End-to-end: prawdziwy
run_export + KsefClient + Ledger na symulatorze KSeF (tests/sim_ksef.py)."""

import collections
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Profile
from app.exporter import Kind, run_export
from app.kontrahenci_nr import RejestrNumerow
from app.ledger import STATUS_DO_RECZNEJ, Ledger
from app.quota import QuotaStore
from sim_ksef import NIP, Faktura, SerwerKsef, Zegar, generuj_miesiac, pl, zainstaluj_zegar

LISTOPAD_5 = datetime(2026, 11, 5, 9, tzinfo=timezone.utc)
PAZ_OD, PAZ_DO = date(2026, 10, 1), date(2026, 10, 31)


class Swiat:
    """Jedna firma: symulator KSeF + rejestr pobranych + rejestr numerów kart;
    każdy eksport = świeży klient (jak w GUI), wspólna baza limitów."""

    def __init__(self, tmp: Path, monkeypatch, faktury, *, start=LISTOPAD_5, **kw):
        self.tmp = tmp
        self.zegar = Zegar(start)
        zainstaluj_zegar(self.zegar, monkeypatch)
        self.serwer = SerwerKsef(faktury, self.zegar, **kw)
        self.ledger = Ledger(tmp / "ledger.db")
        self.rej = RejestrNumerow(tmp / "rej.db")
        self.profile = Profile(name="T", nip=NIP, token="x", subfolder=NIP)
        self.n = 0

    def eksport(self, od=PAZ_OD, do=PAZ_DO, *, kiedy=None, only_new=True,
                kinds=(Kind.SALES,), **kw):
        if kiedy is not None:
            assert kiedy >= self.zegar.teraz
            self.zegar.teraz = kiedy
        self.n += 1
        cel = self.tmp / f"run{self.n:02d}"
        klient = self.serwer.klient(quota=QuotaStore(self.tmp / "quota.db"))
        try:
            rep = run_export(self.profile, klient, self.ledger, od, do, set(kinds), cel,
                             fmt="hmffk", only_new=only_new, rejestr_nr=self.rej, **kw)
        finally:
            klient.close()
        numery = []
        for plik in (cel / NIP).glob("hmffk_*.TXT"):
            tekst = plik.read_bytes().decode("cp1250")
            numery += [ln.split("=", 1)[1].strip() for ln in tekst.splitlines()
                       if ln.strip().startswith("KsefNumber =")]
        return rep, numery

    def zapytania_issue(self, subject="Subject1"):
        return [z for z in self.serwer.zapytania_metadanych
                if z["body"]["dateRange"]["dateType"] == "Issue"
                and z["body"]["subjectType"] == subject]

    def zamknij(self):
        self.ledger.close()
        self.rej.close()


@pytest.fixture
def swiat(tmp_path, monkeypatch):
    utworzone = []

    def _swiat(faktury, **kw):
        s = Swiat(tmp_path, monkeypatch, faktury, **kw)
        utworzone.append(s)
        return s

    yield _swiat
    for s in utworzone:
        s.zamknij()


def _w_zapasie():
    """Faktury z dni zapasu zapytania (30.09, 1–2.11) — w paczce, nie w pliku."""
    return [
        Faktura(f"{NIP}-20260930-Z1", "Subject1", date(2026, 9, 30), pl(date(2026, 9, 30), 12)),
        Faktura(f"{NIP}-20261101-Z2", "Subject1", date(2026, 11, 1), pl(date(2026, 11, 1), 12)),
        Faktura(f"{NIP}-20261102-Z3", "Subject1", date(2026, 11, 2), pl(date(2026, 11, 2), 12)),
    ]


# --------------------------------------------------------------------------- #
# (g) mały zakres — zachowanie jak dotąd


def test_maly_zakres_jak_dotad_jedno_zapytanie_na_rodzaj(swiat):
    sprzedaz = generuj_miesiac(2026, 10, 8, seed=1)                      # <= 10 -> pojedynczo
    koszt = generuj_miesiac(2026, 10, 150, seed=2, subject="Subject2", prefiks="K")  # stara paczka
    s = swiat(sprzedaz + koszt)
    rep, numery = s.eksport(kinds=(Kind.SALES, Kind.COST))
    assert sorted(numery) == sorted(f.ksef for f in sprzedaz + koszt)
    assert len(s.zapytania_issue("Subject1")) == 1
    assert len(s.zapytania_issue("Subject2")) == 1
    assert len(s.serwer.zlecenia) == 1                   # koszt: jedna paczka jak dotąd
    assert sorted(s.serwer.pobrania_pojedyncze) == sorted(f.ksef for f in sprzedaz)
    assert not rep.niepelny and rep.found == rep.exported == 158


# --------------------------------------------------------------------------- #
# duży zakres — paczki zamiast listy


def test_duzy_zakres_paczkami_domyka_miesiac_bez_strat_dubli_i_dociagania(swiat):
    faktury = generuj_miesiac(2026, 10, 1200, seed=3) + _w_zapasie()
    s = swiat(faktury, limit_paczki=500)
    rep, numery = s.eksport()
    oczekiwane = sorted(f.ksef for f in faktury if PAZ_OD <= f.issue <= PAZ_DO)
    assert sorted(numery) == oczekiwane                     # 0 utraconych, 0 dubli, 0 spoza zakresu
    assert len(s.zapytania_issue()) == 1                   # tylko sonda
    assert len(s.serwer.zlecenia) == 3                     # 1200 / 500 (+ nakładka)
    # (b) faza 2 niczego z zakresu nie dociąga pojedynczo — ucięcie niczego nie zgubiło
    assert [n for n in s.serwer.pobrania_pojedyncze if n in set(oczekiwane)] == []
    assert not rep.niepelny and not rep.failed and not rep.niepobrane
    assert rep.found == rep.exported == 1200
    assert s.ledger.is_covered(NIP, "Subject1", date(2026, 10, 15))   # miesiąc domknięty


def test_duzy_zakres_ponownie_tylko_nowe_nic_nie_dubluje(swiat):
    faktury = generuj_miesiac(2026, 10, 700, seed=4)
    s = swiat(faktury, limit_paczki=400)
    s.eksport()
    rep, numery = s.eksport(kiedy=LISTOPAD_5 + timedelta(hours=2))
    assert numery == []
    assert rep.skipped_dedup == 700 and rep.exported == 0 and not rep.niepelny


def test_duzy_zakres_ponowny_pelny_eksport_daje_caly_plik(swiat):
    faktury = generuj_miesiac(2026, 10, 700, seed=5)
    s = swiat(faktury, limit_paczki=400)
    s.eksport(only_new=False)
    rep, numery = s.eksport(kiedy=LISTOPAD_5 + timedelta(hours=2), only_new=False)
    assert sorted(numery) == sorted(f.ksef for f in faktury)
    assert rep.re_exported == 700


def test_druga_paczka_nie_gotowa_pierwsza_zostaje_ponowny_przebieg_dociaga(swiat):
    """(f) KSeF przygotowuje drugą paczkę 45 min: pierwszy przebieg — timeout,
    to, co z pierwszej paczki, trafia do pliku; rodzaj niepełny, bez zapisu
    zakresu i punktu; ponowny przebieg („Pomiń już pobrane”) odbiera zamówioną
    wcześniej drugą paczkę, dociąga resztę bez dubli i domyka miesiąc."""
    faktury = generuj_miesiac(2026, 10, 900, seed=6)
    s = swiat(faktury, limit_paczki=500)
    s.serwer.czas_przygotowania_nr = {2: timedelta(minutes=45)}
    rep1, numery1 = s.eksport()
    assert rep1.niepelny
    assert 0 < len(numery1) < 900
    assert any("30 min" in w for w in rep1.warnings)
    assert not s.ledger.is_covered(NIP, "Subject1", date(2026, 10, 15))
    assert s.ledger.sync_point(NIP, "Subject1") is None

    assert any("kolejny eksport odbierze" in w for w in rep1.warnings)
    od_drugiej = s.serwer.zlecenia[1]["dateRange"]["from"]

    rep2, numery2 = s.eksport(kiedy=LISTOPAD_5 + timedelta(hours=1))
    assert not rep2.niepelny
    wszystkie = collections.Counter(numery1 + numery2)
    assert sorted(wszystkie) == sorted(f.ksef for f in faktury)
    assert max(wszystkie.values()) == 1
    assert s.ledger.is_covered(NIP, "Subject1", date(2026, 10, 15))
    # druga paczka odebrana z poprzedniego zlecenia, nie zamówiona drugi raz
    assert [z["dateRange"]["from"] for z in s.serwer.zlecenia].count(od_drugiej) == 1


def test_limit_zlecen_paczek_w_polowie_niepelny_z_ostrzezeniem(swiat):
    faktury = generuj_miesiac(2026, 10, 900, seed=7)
    s = swiat(faktury, limit_paczki=500)
    s.serwer.limit_eksportu_od = 2
    rep, numery = s.eksport()
    assert rep.niepelny and 0 < len(numery) < 900
    assert any("Limit KSeF" in w for w in rep.warnings)
    assert not s.ledger.is_covered(NIP, "Subject1", date(2026, 10, 15))


def test_duzy_zakres_tryb_kontrahentow_filtr_po_fakcie(swiat):
    faktury = generuj_miesiac(2026, 10, 600, seed=8)
    for f in faktury[::50]:
        f.kontrahent = "7010012345"
    s = swiat(faktury)
    rep, numery = s.eksport(contractor_filter=["7010012345"])
    assert sorted(numery) == sorted(f.ksef for f in faktury[::50])
    assert rep.contractors_found == 1 and not rep.failed
    assert s.serwer.pobrania_pojedyncze == []


def test_duzy_zakres_tryb_numerow_paczkami(swiat):
    faktury = generuj_miesiac(2026, 10, 600, seed=9)
    s = swiat(faktury)
    szukane = faktury[123]
    rep, numery = s.eksport(invoice_numbers=[f"FV/{szukane.ksef[-9:]}"])
    assert numery == [szukane.ksef]
    assert rep.numbers_found == 1 and not rep.failed
    assert len(s.zapytania_issue()) == 1


def test_duzy_zakres_ostrzezenie_nie_fa3(swiat):
    faktury = generuj_miesiac(2026, 10, 300, seed=10)
    faktury[7].form = "FA (2)"
    s = swiat(faktury)
    rep, _ = s.eksport()
    assert any(faktury[7].ksef in w and "FA (2)" in w for w in rep.warnings)


def test_duzy_zakres_lista_do_recznej_obslugi_wycieta(swiat):
    faktury = generuj_miesiac(2026, 10, 300, seed=11)
    s = swiat(faktury)
    reczna = faktury[42]
    s.ledger.record_late(NIP, "Subject1", reczna.ksef, status=STATUS_DO_RECZNEJ,
                         invoice_number="FV/x", issue_date=reczna.issue.isoformat(),
                         acquisition_date=None, permanent_storage_date=reczna.ps.isoformat(),
                         range_from=date(2026, 11, 1), range_to=date(2026, 11, 5), error="test")
    rep, numery = s.eksport(only_new=False)
    assert reczna.ksef not in numery and len(numery) == 299
    assert any("Pominięto" in w and reczna.ksef in w for w in rep.warnings)


def test_gammatest_pelna_skala_jeden_przebieg_domyka_miesiac(swiat):
    """(a) ~13,5 tys. faktur sprzedaży w miesiącu, prawdziwy limit paczki
    10 000, data wystawienia porównywana jako północ PL: jeden przebieg
    domyka miesiąc — 0 utraconych, 0 dubli, 0 spoza zakresu; lista
    metadanych to tylko sonda; faza 2 nie dociąga niczego z zakresu."""
    faktury = generuj_miesiac(2026, 10, 13_500, seed=21) + _w_zapasie()
    s = swiat(faktury, czas_przygotowania=timedelta(minutes=3))
    rep, numery = s.eksport()
    oczekiwane = sorted(f.ksef for f in faktury if PAZ_OD <= f.issue <= PAZ_DO)
    assert len(numery) == len(set(numery)) == 13_500
    assert sorted(numery) == oczekiwane
    assert len(s.zapytania_issue()) == 1
    assert len(s.serwer.zlecenia) == 2
    e1 = s.serwer.eksporty["EXP-0001"]
    assert e1.obciety and len(e1.faktury) == 10_000
    assert s.serwer.pobrania_pojedyncze == []
    assert not rep.niepelny and rep.found == rep.exported == 13_500
    assert s.ledger.is_covered(NIP, "Subject1", date(2026, 10, 1))


def test_przerwanie_w_trakcie_paczki_bez_domkniecia(swiat):
    import threading

    faktury = generuj_miesiac(2026, 10, 800, seed=22)
    s = swiat(faktury, limit_paczki=500)
    cancel = threading.Event()

    def postep(i, n, nr):
        if i == 100:
            cancel.set()

    rep, numery = s.eksport(cancel=cancel, on_progress=postep)
    assert rep.cancelled
    assert not s.ledger.is_covered(NIP, "Subject1", date(2026, 10, 1))
    assert s.ledger.sync_point(NIP, "Subject1") is None
    assert len(s.serwer.zlecenia) == 1      # po przerwaniu nie zamawiamy kolejnej paczki


# --------------------------------------------------------------------------- #
# runda poprawek po przeglądzie adwersarialnym


def _zapytania_ps(s, subject="Subject1"):
    return [z for z in s.serwer.zapytania_metadanych
            if z["body"]["dateRange"]["dateType"] == "PermanentStorage"
            and z["body"]["subjectType"] == subject]


def test_pelna_skala_druga_paczka_nigdy_gotowa_niepelny_bez_domkniecia(swiat):
    """(3) liveness oznaczenia „niepełny” po błędzie paczki — niezależnie od
    fazy 2: rodzaj niepełny nie jest w ogóle sprawdzany „od punktu” (gdyby był,
    mógłby przypadkiem dociągnąć brak pojedynczo i ukryć błąd)."""
    faktury = generuj_miesiac(2026, 10, 13_500, seed=50)
    s = swiat(faktury)
    s.serwer.nigdy_gotowe = {2}
    rep, numery = s.eksport()
    assert rep.niepelny and len(numery) == 10_000
    assert not s.ledger.is_covered(NIP, "Subject1", date(2026, 10, 25))
    assert s.ledger.sync_point(NIP, "Subject1") is None
    assert _zapytania_ps(s) == []                 # faza 2 nie ruszyła niepełnego rodzaju
    assert s.serwer.pobrania_pojedyncze == []


def test_brak_pliku_xml_jednej_faktury_w_paczce_niepelny_bez_domkniecia(swiat):
    """(3) wpis w `_metadata.json` jest, pliku XML nie ma: faktura niepobrana,
    rodzaj niepełny, bez domknięcia i bez sprawdzania „od punktu”."""
    faktury = generuj_miesiac(2026, 10, 600, seed=51)
    s = swiat(faktury)
    brak = faktury[77].ksef
    s.serwer.usun_xml = {brak}
    rep, numery = s.eksport()
    assert brak not in numery and len(numery) == 599
    assert [nr for nr, _ in rep.niepobrane] == [brak]
    assert rep.niepelny
    assert not s.ledger.is_covered(NIP, "Subject1", date(2026, 10, 15))
    assert _zapytania_ps(s) == []


def test_A1_powtorzony_wpis_w_metadata_json_bez_dubla_w_pliku(swiat):
    faktury = generuj_miesiac(2026, 10, 300, seed=31)
    s = swiat(faktury)
    s.serwer.psuj_metadane = lambda m: m + [m[5]]
    rep, numery = s.eksport(only_new=False)
    assert len(numery) == len(set(numery))
    assert rep.niepelny
    assert not s.ledger.is_covered(NIP, "Subject1", date(2026, 10, 15))
    assert any("powtórzony numer KSeF" in w for w in rep.warnings)


def test_A2_pelna_paczka_bez_isTruncated_pelna_skala(swiat):
    faktury = generuj_miesiac(2026, 10, 13_500, seed=32)
    s = swiat(faktury)
    s.serwer.ukryj_uciecie = True
    rep, numery = s.eksport()
    assert sorted(numery) == sorted(f.ksef for f in faktury)
    assert not rep.niepelny
    assert s.ledger.is_covered(NIP, "Subject1", date(2026, 10, 15))


def _dni(od, do, na_dzien, prefiks="D", subject="Subject1"):
    out, d = [], od
    while d <= do:
        out += [Faktura(f"{NIP}-{d:%Y%m%d}-{prefiks}{i:04d}", subject, d, pl(d, 8 + (i % 10), i % 60))
                for i in range(na_dzien)]
        d += timedelta(days=1)
    return out


def _paz(faktury):
    return sorted(f.ksef for f in faktury if PAZ_OD <= f.issue <= PAZ_DO)


@pytest.mark.parametrize("interpretacja", ["pl", "utc"])
def test_A3_uciecie_dokladnie_na_ostatniej_fakturze_dnia(swiat, interpretacja):
    faktury = _dni(date(2026, 9, 29), date(2026, 11, 2), 12)
    s = swiat(faktury, limit_paczki=48, interpretacja=interpretacja)    # 4 pełne dni
    rep, numery = s.eksport()
    assert sorted(numery) == _paz(faktury)
    assert not rep.niepelny


@pytest.mark.parametrize("w_zakresie,w_zapasie", [(250, 0), (249, 1), (250, 1), (251, 0)])
def test_A4_sonda_na_granicy_250(swiat, w_zakresie, w_zapasie):
    faktury = generuj_miesiac(2026, 10, w_zakresie, seed=34)
    faktury += [Faktura(f"{NIP}-20260930-Z{i}", "Subject1", date(2026, 9, 30), pl(date(2026, 9, 30), 12))
                for i in range(w_zapasie)]
    s = swiat(faktury)
    rep, numery = s.eksport()
    assert sorted(numery) == _paz(faktury)
    assert len(s.zapytania_issue()) == 1 and len(s.serwer.zlecenia) == 1
    assert not rep.niepelny and s.serwer.pobrania_pojedyncze == []


@pytest.mark.parametrize("ile_s,ile_k", [(0, 12_000), (12_000, 0)])
def test_A5_pusty_rodzaj_obok_dwunastu_tysiecy(swiat, ile_s, ile_k):
    faktury = generuj_miesiac(2026, 10, ile_s, seed=35) + generuj_miesiac(
        2026, 10, ile_k, seed=36, subject="Subject2", prefiks="K")
    s = swiat(faktury)
    rep, numery = s.eksport(kinds=(Kind.SALES, Kind.COST))
    assert sorted(numery) == _paz(faktury) and not rep.niepelny
    assert s.ledger.is_covered(NIP, "Subject1", date(2026, 10, 15))
    assert s.ledger.is_covered(NIP, "Subject2", date(2026, 10, 15))


def test_A8_czesci_zamienione_miejscami_rodzaj_niepelny(swiat):
    faktury = generuj_miesiac(2026, 10, 600, seed=39)
    s = swiat(faktury, rozmiar_czesci=20_000)

    def zamien(nr):
        e = s.serwer.eksporty[f"EXP-{nr:04d}"]
        assert len(e.czesci) >= 3
        e.czesci[1], e.czesci[2] = e.czesci[2], e.czesci[1]
        e.czesci_jawne[1], e.czesci_jawne[2] = e.czesci_jawne[2], e.czesci_jawne[1]

    s.serwer.po_zleceniu = zamien
    rep, numery = s.eksport()
    assert rep.niepelny and numery == []
    assert not s.ledger.is_covered(NIP, "Subject1", date(2026, 10, 15))


@pytest.mark.parametrize("interpretacja", ["pl", "utc"])
def test_A9_dwa_okna_uciecia_przy_granicy_okien(swiat, interpretacja):
    faktury = _dni(date(2026, 6, 29), date(2026, 11, 2), 7)
    s = swiat(faktury, limit_paczki=100, interpretacja=interpretacja,
              start=datetime(2026, 11, 20, 9, tzinfo=timezone.utc))
    rep, numery = s.eksport(date(2026, 7, 1), date(2026, 10, 31))
    assert sorted(numery) == sorted(f.ksef for f in faktury
                                    if date(2026, 7, 1) <= f.issue <= date(2026, 10, 31))
    assert not rep.niepelny


def test_A11_przerwanie_w_trakcie_pobierania_czesci(swiat):
    import threading

    faktury = generuj_miesiac(2026, 10, 800, seed=41)
    s = swiat(faktury, limit_paczki=500, rozmiar_czesci=20_000)
    cancel = threading.Event()
    stary, licz = s.serwer._blob, [0]

    def blob(req):
        licz[0] += 1
        if licz[0] == 3:
            cancel.set()
        return stary(req)

    s.serwer._blob = blob
    rep, _ = s.eksport(cancel=cancel)
    assert rep.cancelled
    assert not s.ledger.is_covered(NIP, "Subject1", date(2026, 10, 15))


# (7) postęp: podczas czekania na kolejną paczkę pasek nie może pokazywać 100%


def test_postep_nie_pokazuje_100_procent_przed_ostatnia_paczka(swiat):
    faktury = generuj_miesiac(2026, 10, 900, seed=52)
    s = swiat(faktury, limit_paczki=500)
    wywolania = []
    rep, numery = s.eksport(on_progress=lambda i, n, nr: wywolania.append((i, n, nr)))
    assert len(numery) == 900
    czekanie = [k for k, (_, _, nr) in enumerate(wywolania) if "kolejną paczkę" in nr]
    assert czekanie, "brak komunikatu o czekaniu na kolejną paczkę"
    przed_ostatnia = wywolania[:czekanie[-1] + 1]
    assert all(i < n for i, n, _ in przed_ostatnia), "100% przed ostatnią paczką"
    i, n, _ = wywolania[-1]
    assert i == n == 900


# (9) błąd mapowania faktury z GŁÓWNEGO zakresu: lista do ręcznej obsługi


def test_A7_blad_mapowania_z_zakresu_nie_wraca_jako_spozniona(swiat):
    """Faktura z błędem mapowania w kompletnym przebiegu domknięcia trafia na
    listę do ręcznej obsługi — kolejne eksporty (faza 2 „spóźnione”) nie
    zgłaszają jej drugi raz."""
    faktury = generuj_miesiac(2026, 10, 6000, seed=38)
    zla = faktury[-1]
    zla.xml = b"<a/>"
    s = swiat(faktury)
    rep1, _ = s.eksport()
    [(nr, komunikat)] = [(n, m) for n, m in rep1.failed if n == zla.ksef]
    assert "zapisana na liście do ręcznej obsługi" in komunikat
    assert "kolejne eksporty nie zgłoszą jej ponownie" in komunikat
    assert [r.ksef_number for r in s.ledger.manual_list(NIP)] == [zla.ksef]
    for h in range(1, 10):
        r, numery = s.eksport(date(2026, 11, 1), date(2026, 11, 4), kiedy=LISTOPAD_5 + timedelta(hours=h))
        assert zla.ksef not in [n for n, _ in r.failed]
        assert zla.ksef not in numery
        if not any("Sprawdzanie spóźnionych" in w for w in r.warnings):
            break
    assert not any("Sprawdzanie spóźnionych" in w for w in r.warnings), "faza 2 nie doszła do faktury"


def test_blad_mapowania_z_zakresu_po_naprawie_mappera_nie_trafia_do_pliku(swiat):
    faktury = generuj_miesiac(2026, 10, 300, seed=53)
    zla = faktury[10]
    zla.xml = b"<a/>"
    s = swiat(faktury)
    rep1, numery1 = s.eksport()
    assert zla.ksef in [n for n, _ in rep1.failed] and zla.ksef not in numery1
    zla.xml = None                                     # „aktualizacja naprawiła mapper”
    for only_new in (True, False):
        rep, numery = s.eksport(only_new=only_new, kiedy=s.zegar.teraz + timedelta(hours=1))
        assert zla.ksef not in numery and not rep.failed
        assert any("Pominięto 1 fakturę oznaczoną do ręcznej obsługi" in w for w in rep.warnings)
    rep, numery = s.eksport(date(2026, 11, 1), date(2026, 11, 4), kiedy=s.zegar.teraz + timedelta(hours=1))
    assert zla.ksef not in numery and not rep.failed


def test_blad_mapowania_w_przebiegu_bez_zapisu_zakresu_nie_trafia_na_liste(swiat):
    """Przebieg niepełny (brak zapisu zakresu) — faktura z błędem mapowania nie
    trafia na listę; zgłosi ją ponownie przebieg kompletny."""
    faktury = generuj_miesiac(2026, 10, 300, seed=54)
    zla = faktury[10]
    zla.xml = b"<a/>"
    s = swiat(faktury)
    s.serwer.usun_xml = {faktury[20].ksef}             # rodzaj niepełny
    rep1, _ = s.eksport()
    assert rep1.niepelny
    [(_, komunikat)] = [(n, m) for n, m in rep1.failed if n == zla.ksef]
    assert "zapisana na liście" not in komunikat
    assert s.ledger.manual_list(NIP) == []
    s.serwer.usun_xml = set()
    rep2, _ = s.eksport(kiedy=s.zegar.teraz + timedelta(hours=1))
    assert not rep2.niepelny
    assert [r.ksef_number for r in s.ledger.manual_list(NIP)] == [zla.ksef]


def test_blad_mapowania_w_trybie_wyszukiwania_nie_trafia_na_liste(swiat):
    faktury = generuj_miesiac(2026, 10, 300, seed=55)
    zla = faktury[10]
    zla.xml = b"<a/>"
    s = swiat(faktury)
    rep, _ = s.eksport(invoice_numbers=[f"FV/{zla.ksef[-9:]}"])
    [(_, komunikat)] = [(n, m) for n, m in rep.failed if n == zla.ksef]
    assert "nie księguj jej jeszcze ręcznie" in komunikat          # (P2)
    assert s.ledger.manual_list(NIP) == []
    # już na liście (po kompletnym eksporcie) — wyszukiwanie mówi, że zgłoszona wcześniej
    s.eksport(kiedy=s.zegar.teraz + timedelta(hours=1))
    rep, _ = s.eksport(invoice_numbers=[f"FV/{zla.ksef[-9:]}"], kiedy=s.zegar.teraz + timedelta(hours=1))
    [(_, komunikat)] = [(n, m) for n, m in rep.failed if n == zla.ksef]
    assert "jest już na liście do ręcznej obsługi" in komunikat


# (4) paczka odebrana z poprzedniego przebiegu jest stanem z chwili zamówienia:
# to, co wpłynęło później, musi sprawdzić dodatkowe zapytanie „od zamówienia”


def _wolna_druga_paczka_i_spozniony_wplyw(swiat, ile=13_500, **kw):
    """Pełna skala: sprawdzenie „od punktu” (faza 2, ok. 15 stron) nie sięga
    wpływów z dnia eksportu — nie może przypadkiem ukryć braku."""
    faktury = generuj_miesiac(2026, 10, ile, seed=56)
    s = swiat(faktury, **kw)
    s.serwer.czas_przygotowania_nr = {2: timedelta(minutes=45)}
    rep1, numery1 = s.eksport()
    assert rep1.niepelny
    # po zamówieniu drugiej paczki (przed kolejnym eksportem) wpływa faktura
    # z jej zakresu dat — w zamówionej paczce jej nie ma
    nowa = Faktura(f"{NIP}-20261029-NOWA", "Subject1", date(2026, 10, 29),
                   LISTOPAD_5 + timedelta(minutes=50))
    s.serwer.faktury.append(nowa)            # ta sama lista co `faktury`
    return s, list(s.serwer.faktury), nowa, numery1


def test_odebrana_stara_paczka_uzupelniona_o_wplywy_po_zamowieniu(swiat):
    s, faktury, nowa, numery1 = _wolna_druga_paczka_i_spozniony_wplyw(swiat)
    rep2, numery2 = s.eksport(kiedy=LISTOPAD_5 + timedelta(hours=1))
    assert not rep2.niepelny
    assert nowa.ksef in numery2
    assert sorted(numery1 + numery2) == sorted(f.ksef for f in faktury)
    assert s.ledger.is_covered(NIP, "Subject1", date(2026, 10, 15))


def test_odebrana_stara_paczka_bez_sprawdzenia_wplywow_to_niepelny(swiat):
    """Sprawdzenie „od zamówienia” nie przeszło (limit zapytań o metadane) —
    rodzaj niepełny, miesiąc NIE domknięty (nie wiadomo, czego brakuje)."""
    s, faktury, nowa, _ = _wolna_druga_paczka_i_spozniony_wplyw(swiat, ile=900, limit_paczki=500)

    def wyczerpany_limit(nr):
        s.serwer.stemple["metadata"] = [s.zegar.time()] * 20

    s.serwer.po_zleceniu = wyczerpany_limit
    rep2, numery2 = s.eksport(kiedy=LISTOPAD_5 + timedelta(hours=1))
    assert rep2.niepelny
    assert not s.ledger.is_covered(NIP, "Subject1", date(2026, 10, 15))
    assert any("wpłynęły po zamówieniu" in w for w in rep2.warnings)


# --------------------------------------------------------------------------- #
# runda 3


def _do_skutku(s, n, odstep):
    """Przebiegi „Pomiń już pobrane” co `odstep` (None — od razu po poprzednim)
    aż do kompletnego domknięcia; (liczba przebiegów, numery ze wszystkich plików)."""
    wszystkie = []
    for i in range(n):
        kiedy = s.zegar.teraz if odstep is None else max(LISTOPAD_5 + i * odstep, s.zegar.teraz)
        rep, numery = s.eksport(kiedy=kiedy)
        wszystkie += numery
        if s.ledger.is_covered(NIP, "Subject1", date(2026, 10, 15)) and not rep.niepelny:
            return i + 1, wszystkie
    return None, wszystkie


def test_R7_zawieszone_zlecenie_blokuje_najwyzej_ok_4_h(swiat):
    faktury = generuj_miesiac(2026, 10, 600, seed=71)
    s = swiat(faktury)
    s.serwer.nigdy_gotowe = {1}
    ile, wszystkie = _do_skutku(s, 8, timedelta(hours=1))
    assert ile is not None and ile <= 5, f"domknięcie dopiero w przebiegu {ile}"
    assert s.zegar.teraz - LISTOPAD_5 <= timedelta(hours=4, minutes=30)
    assert sorted(wszystkie) == sorted(f.ksef for f in faktury)


@pytest.mark.parametrize("przygotowanie_min,odstep", [
    (100, timedelta(hours=1)), (130, timedelta(hours=1)), (65, None), (95, None)])
def test_P1_wolny_ksef_zbiega_do_kompletnego(swiat, przygotowanie_min, odstep):
    """(P1) KSeF przygotowuje każdą paczkę dłużej niż 30 min (do ~2 h): kolejne
    przebiegi odbierają zamówione paczki i domykają miesiąc bez strat i dubli."""
    faktury = generuj_miesiac(2026, 10, 900, seed=81)
    s = swiat(faktury, limit_paczki=500, czas_przygotowania=timedelta(minutes=przygotowanie_min))
    ile, wszystkie = _do_skutku(s, 12, odstep)
    assert ile is not None, "brak zbieżności w 12 przebiegach"
    assert sorted(wszystkie) == sorted(f.ksef for f in faktury)


@pytest.mark.parametrize("naglowek_date", [True, False])
def test_R3_zegar_komputera_2h_do_przodu_nie_gubi_wplywu_po_zamowieniu(swiat, tmp_path, naglowek_date):
    """(R3) moment zamówienia wg zegara KSeF (nagłówek Date); bez nagłówka —
    zegar komputera z większym zapasem. Faktura, która wpłynęła 20 min po
    zamówieniu, trafia do pliku domknięcia mimo zegara komputera +2 h."""
    import sqlite3

    faktury = generuj_miesiac(2026, 10, 13_500, seed=63)
    s = swiat(faktury)
    s.serwer.naglowek_date = naglowek_date
    s.serwer.czas_przygotowania_nr = {2: timedelta(minutes=50)}
    rep1, _ = s.eksport()
    assert rep1.niepelny
    con = sqlite3.connect(tmp_path / "quota.db")
    [(zamowiono,)] = con.execute("select ordered_at from pending_exports").fetchall()
    con.execute("update pending_exports set ordered_at = ordered_at + 7200")   # zegar komputera +2 h
    con.commit()
    con.close()
    nowa = Faktura(f"{NIP}-20261028-SKEW1", "Subject1", date(2026, 10, 28),
                   datetime.fromtimestamp(zamowiono, timezone.utc) + timedelta(minutes=20))
    s.serwer.faktury.append(nowa)
    rep2, numery2 = s.eksport(kiedy=LISTOPAD_5 + timedelta(hours=1))
    assert s.ledger.is_covered(NIP, "Subject1", date(2026, 10, 28))
    assert nowa.ksef in numery2


def test_R_b_blad_mapowania_w_przebiegu_niepelnym_nie_ksieguj_jeszcze(swiat):
    faktury = generuj_miesiac(2026, 10, 300, seed=75)
    zla = faktury[10]
    zla.xml = b"<a/>"
    s = swiat(faktury)
    s.serwer.usun_xml = {faktury[20].ksef}             # rodzaj niepełny
    rep, _ = s.eksport()
    assert rep.niepelny
    [(_, komunikat)] = [(n, m) for n, m in rep.failed if n == zla.ksef]
    assert "eksport niepełny" in komunikat and "nie księguj jej jeszcze ręcznie" in komunikat


def test_R3_zegar_komputera_daleko_w_przyszlosci_bez_date_niepelny(swiat, tmp_path):
    """Bez nagłówka Date, zegar komputera +15 h (ponad zapas 12 h): początek
    sprawdzenia wpływów późniejszy niż „teraz” KSeF — nie wiadomo, co wpłynęło
    po zamówieniu, więc rodzaj niepełny i BEZ domknięcia (a nie „nic nowego”)."""
    import sqlite3

    faktury = generuj_miesiac(2026, 10, 900, seed=76)
    s = swiat(faktury, limit_paczki=500)
    s.serwer.naglowek_date = False
    s.serwer.czas_przygotowania_nr = {2: timedelta(minutes=50)}
    s.eksport()
    con = sqlite3.connect(tmp_path / "quota.db")
    con.execute("update pending_exports set ordered_at = ordered_at + 15 * 3600")
    con.commit()
    con.close()
    # wpis „z przyszłości” nie wygasa — odbiór i sprawdzenie od (zamówienie + 15 h − 12 h)
    rep2, _ = s.eksport(kiedy=LISTOPAD_5 + timedelta(hours=1))
    assert rep2.niepelny
    assert not s.ledger.is_covered(NIP, "Subject1", date(2026, 10, 15))
    assert any("wpłynęły po zamówieniu" in w for w in rep2.warnings)


def test_P2_zakres_dzisiejszy_kompletny_bez_zapisu_zakresu_nie_ksieguj_jeszcze(swiat):
    """(P2) eksport „z dziś” jest kompletny, ale zakres nie jest zapisywany
    (dane KSeF kompletne do wczoraj) — faktura z błędem mapowania nie trafia
    na listę, więc komunikat nie może kazać jej księgować ręcznie: domknięcie
    miesiąca zgłosi ją ponownie (i dopiero wtedy „zaksięguj ręcznie”)."""
    d = date(2026, 11, 5)
    faktury = [Faktura(f"{NIP}-20261105-T{i:03d}", "Subject1", d,
                       datetime(2026, 11, 5, 6, i, tzinfo=timezone.utc)) for i in range(5)]
    faktury[2].xml = b"<a/>"
    s = swiat(faktury)
    rep, _ = s.eksport(d, d, kiedy=LISTOPAD_5)
    assert not rep.niepelny and not s.ledger.is_covered(NIP, "Subject1", d)
    [(_, komunikat)] = [(n, m) for n, m in rep.failed if n == faktury[2].ksef]
    assert "nie księguj jej jeszcze ręcznie" in komunikat
    assert "zaksięguj ją ręcznie" not in komunikat
    rep2, _ = s.eksport(date(2026, 11, 1), date(2026, 11, 30),
                        kiedy=datetime(2026, 12, 3, 9, tzinfo=timezone.utc))
    [(_, komunikat2)] = [(n, m) for n, m in rep2.failed if n == faktury[2].ksef]
    assert "zapisana na liście do ręcznej obsługi, zaksięguj ją ręcznie" in komunikat2
