"""Wspolne pomoce dla skryptow diagnostycznych edge-case (symulacja/edgecase).

NIE importowane przez app/ ani tests/ -- wylacznie narzedzie do tego przegladu.
Uzywa WYLACZNIE realnego API: app.hmffk.map_fa3_to_hmffk, app.kontrahenci_nr.RejestrNumerow,
app.exporter.run_export (ze stubem klienta jak w tests/test_exporter.py).
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.config import Profile  # noqa: E402
from app.exporter import Kind, run_export  # noqa: E402
from app.fa_mapper import InvoiceContext  # noqa: E402
from app.hmffk import map_fa3_to_hmffk  # noqa: E402
from app.kontrahenci_nr import RejestrNumerow  # noqa: E402
from app.ksef_client import InvoiceMetadata  # noqa: E402
from app.ledger import Ledger  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"

WLASNY_NIP = "5252445719"  # "Nasza Firma" -- jak w tests/test_exporter.py


def meta(nr: str, day: str = "2026-01-15", invoice_number: str = "FV/2026/01/0007") -> InvoiceMetadata:
    return InvoiceMetadata(
        ksef_number=nr,
        invoice_number=invoice_number,
        issue_date=f"{day}T00:00:00+00:00",
        invoicing_date=f"{day}T12:00:00+00:00",
        acquisition_date=None,
        seller_nip="1180012345",
        seller_name="Dostawca Testowy Sp. z o.o.",
        buyer_id=WLASNY_NIP,
        buyer_name="Nasza Firma Sp. z o.o.",
        net=None, gross=None, vat=None,
        currency="PLN",
        form_system_code="FA (3)",
        invoice_type="Vat",
    )


class StubClient:
    """Identyczny stub jak w tests/test_exporter.py -- bez sieci."""

    def __init__(self, metas, xml_by_nr):
        self._metas = metas
        self._xml = xml_by_nr
        self.auth_called = False

    def authenticate(self):
        self.auth_called = True

    def query_metadata(self, subject_type, date_from, date_to):
        yield from self._metas

    def download_invoice(self, ksef_number):
        return self._xml[ksef_number]

    def download_budget_remaining(self):
        return 64


def fa3_koszt_od(nazwa_sprzedawcy: str, nip_sprzedawcy: str = "", adres_l1: str = "",
                  adres_l2: str = "", kod_kraju: str = "PL", numer: str = "FV/TEST/0001",
                  data: str = "2026-01-15") -> bytes:
    """Buduje minimalna, poprawna FA(3) (faktura kosztowa z perspektywy naszej
    firmy -- Podmiot1 = obcy sprzedawca, Podmiot2 = my). Parametryzowana pod
    Podmiot1 (sprzedawca), zeby symulowac roznych/identycznych kontrahentow."""
    nip_tag = f"<NIP>{nip_sprzedawcy}</NIP>" if nip_sprzedawcy else ""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Faktura xmlns="http://crd.gov.pl/wzor/2025/06/25/13775/">
  <Naglowek>
    <KodFormularza kodSystemowy="FA (3)" wersjaSchemy="1-0E">FA</KodFormularza>
    <WariantFormularza>3</WariantFormularza>
    <DataWytworzeniaFa>{data}T10:00:00Z</DataWytworzeniaFa>
  </Naglowek>
  <Podmiot1>
    <DaneIdentyfikacyjne>
      {nip_tag}
      <Nazwa>{nazwa_sprzedawcy}</Nazwa>
    </DaneIdentyfikacyjne>
    <Adres>
      <KodKraju>{kod_kraju}</KodKraju>
      <AdresL1>{adres_l1}</AdresL1>
      <AdresL2>{adres_l2}</AdresL2>
    </Adres>
  </Podmiot1>
  <Podmiot2>
    <DaneIdentyfikacyjne>
      <NIP>{WLASNY_NIP}</NIP>
      <Nazwa>Nasza Firma Sp. z o.o.</Nazwa>
    </DaneIdentyfikacyjne>
    <Adres>
      <KodKraju>PL</KodKraju>
      <AdresL1>ul. Nasza 1</AdresL1>
      <AdresL2>02-002 Warszawa</AdresL2>
    </Adres>
  </Podmiot2>
  <Fa>
    <KodWaluty>PLN</KodWaluty>
    <P_1>{data}</P_1>
    <P_2>{numer}</P_2>
    <P_6>{data}</P_6>
    <P_13_1>1000.00</P_13_1>
    <P_14_1>230.00</P_14_1>
    <P_15>1230.00</P_15>
    <RodzajFaktury>VAT</RodzajFaktury>
    <FaWiersz>
      <NrWierszaFa>1</NrWierszaFa>
      <P_7>Usluga testowa</P_7>
      <P_9A>1000.00</P_9A>
      <P_11>1000.00</P_11>
      <P_12>23</P_12>
    </FaWiersz>
  </Fa>
</Faktura>
""".encode("utf-8")


def fa3_sprzedaz_uproszczona_bez_nabywcy(numer: str = "PAR/2026/01/0001",
                                          data: str = "2026-01-15") -> bytes:
    """Faktura uproszczona (paragon fiskalny z NIP nabywcy do kwoty 450 PLN wg
    ustawy o VAT bywa markowany jako FA -- tu: BRAK jakichkolwiek danych
    identyfikacyjnych nabywcy poza pustym Podmiot2/DaneIdentyfikacyjne).
    My jestesmy sprzedawca (Podmiot1)."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Faktura xmlns="http://crd.gov.pl/wzor/2025/06/25/13775/">
  <Naglowek>
    <KodFormularza kodSystemowy="FA (3)" wersjaSchemy="1-0E">FA</KodFormularza>
    <WariantFormularza>3</WariantFormularza>
    <DataWytworzeniaFa>{data}T10:00:00Z</DataWytworzeniaFa>
  </Naglowek>
  <Podmiot1>
    <DaneIdentyfikacyjne>
      <NIP>{WLASNY_NIP}</NIP>
      <Nazwa>Nasza Firma Sp. z o.o.</Nazwa>
    </DaneIdentyfikacyjne>
    <Adres>
      <KodKraju>PL</KodKraju>
      <AdresL1>ul. Nasza 1</AdresL1>
      <AdresL2>02-002 Warszawa</AdresL2>
    </Adres>
  </Podmiot1>
  <Podmiot2>
    <DaneIdentyfikacyjne>
    </DaneIdentyfikacyjne>
  </Podmiot2>
  <Fa>
    <KodWaluty>PLN</KodWaluty>
    <P_1>{data}</P_1>
    <P_2>{numer}</P_2>
    <P_6>{data}</P_6>
    <P_13_1>100.00</P_13_1>
    <P_14_1>23.00</P_14_1>
    <P_15>123.00</P_15>
    <RodzajFaktury>VAT</RodzajFaktury>
    <FaWiersz>
      <NrWierszaFa>1</NrWierszaFa>
      <P_7>Towar detaliczny</P_7>
      <P_9A>100.00</P_9A>
      <P_11>100.00</P_11>
      <P_12>23</P_12>
    </FaWiersz>
  </Fa>
</Faktura>
""".encode("utf-8")


def ctx(nr: str, subject_type: str = "Subject2", data: str = "2026-01-15") -> InvoiceContext:
    return InvoiceContext(ksef_number=nr, ksef_date=f"{data}T10:00:00Z", subject_type=subject_type)


def sep(tytul: str) -> None:
    print("\n" + "=" * 78)
    print(tytul)
    print("=" * 78)
