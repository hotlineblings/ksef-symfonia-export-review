<?xml version="1.0" encoding="utf-8"?>
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
    <xsl:output omit-xml-declaration="yes" method="text"/>

    <!-- Ignoruj nieznane tagi-->
    <xsl:template match="text()" />

    <!-- Element główny -->
    <!-- Kontrahent atrybuty -->
    <xsl:template match="/">
        INFO{
        Nazwa programu ='Symfonia XSLT' Symfonia Finanse Księgowość
        Wersja programu =93
        Wersja szablonu =3.1
        }
        <xsl:apply-templates/>
    </xsl:template>

    <!-- Dokument -->
    <xsl:template match="ksefInvoice">
        Dokument{
        <xsl:choose>
            <xsl:when test ="invoiceBound='EXTERNAL'">
                rodzajDok = zakupu<xsl:apply-templates/>
                <!-- docelowo 2(FVZ), FKZ, 3(FVS), FKS - w reakcji na <InvoiceType> -->
                wzorzec = 2
            </xsl:when>
            <xsl:otherwise>
                rodzajDok = sprzedaży<xsl:apply-templates/>
                wzorzec = 3
            </xsl:otherwise>
        </xsl:choose>
        <xsl:choose>
            <xsl:when test="invoice/Naglowek/KodWaluty">
                kurs = <xsl:value-of select="invoice//Wiersze/Wiersz[position()=1]/KursWaluty"/>
            </xsl:when>
            <xsl:otherwise>
                kurs = <xsl:value-of select="invoiceV2//Wiersze/WierszV2[position()=1]/KursWaluty"/>
            </xsl:otherwise>
        </xsl:choose>
        <xsl:if test="invoice">
            <xsl:choose>
                <xsl:when test = "invoice/Naglowek/KodWaluty = 'PLN'">
                    Zapis{
                    strona =WN
                    kwota =<xsl:value-of select="sum(./invoice/PodsumowanieVat/PodsumowanieVat/WartoscNetto)"/>
                    }
                    Zapis{
                    strona =MA
                    kwota =<xsl:value-of select="sum(./invoice/PodsumowanieVat/PodsumowanieVat/WartoscNetto) + sum(./invoice/PodsumowanieVat/PodsumowanieVat/KwotaVat)"/>
                    }
                    Zapis{
                    strona =WN
                    kwota =<xsl:value-of select="sum(./invoice/PodsumowanieVat/PodsumowanieVat/KwotaVat)"/>
                    }
                </xsl:when>
                <xsl:otherwise>
                    Zapis{
                    strona =WN
                    kwota =<xsl:value-of select="sum(./invoice/PodsumowanieVat/PodsumowanieVat/WartoscNetto) * invoice//Wiersze/Wiersz[position()=1]/KursWaluty"/>
                    wkwota =<xsl:value-of select="sum(./invoice/PodsumowanieVat/PodsumowanieVat/WartoscNetto)"/>
                    kurs = <xsl:value-of select="invoice//Wiersze/Wiersz[position()=1]/KursWaluty"/>
                    waluta = <xsl:value-of select="invoice/Naglowek/KodWaluty"/>
                    }
                    Zapis{
                    strona =MA
                    kwota =<xsl:value-of select="sum(./invoice/PodsumowanieVat/PodsumowanieVat/WartoscNetto) * invoice//Wiersze/Wiersz[position()=1]/KursWaluty + sum(./invoice/PodsumowanieVat/PodsumowanieVat/KwotaVatKurs)"/>
                    wkwota =<xsl:value-of select="sum(./invoice/PodsumowanieVat/PodsumowanieVat/WartoscNetto) + sum(./invoice/PodsumowanieVat/PodsumowanieVat/KwotaVat)"/>
                    kurs = <xsl:value-of select="invoice//Wiersze/Wiersz[position()=1]/KursWaluty"/>
                    waluta = <xsl:value-of select="invoice/Naglowek/KodWaluty"/>
                    }
                    Zapis{
                    strona =WN
                    kwota =<xsl:value-of select="sum(./invoice/PodsumowanieVat/PodsumowanieVat/KwotaVatKurs)"/>
                    wkwota =<xsl:value-of select="sum(./invoice/PodsumowanieVat/PodsumowanieVat/KwotaVat)"/>
                    kurs = <xsl:value-of select="invoice//Wiersze/Wiersz[position()=1]/KursWaluty"/>
                    waluta = <xsl:value-of select="invoice/Naglowek/KodWaluty"/>
                    }
                </xsl:otherwise>
            </xsl:choose>
        </xsl:if>

        <xsl:if test="invoiceV2">
            <xsl:choose>
                <xsl:when test = "invoiceV2/Naglowek/KodWaluty = 'PLN' and invoiceBound='EXTERNAL'">
                    Zapis{
                    strona =WN
                    kwota =<xsl:value-of select="sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/WartoscNetto)"/>
                    }
                    Zapis{
                    strona =MA
                    kwota =<xsl:value-of select="sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/WartoscNetto) + sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/KwotaVat)"/>
                    }
                    Zapis{
                    strona =WN
                    kwota =<xsl:value-of select="sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/KwotaVat)"/>
                    }
                </xsl:when>
                <xsl:when test = "invoiceV2/Naglowek/KodWaluty = 'PLN' and invoiceBound='INTERNAL'">
                    Zapis{
                    strona =WN
                    kwota =<xsl:value-of select="sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/WartoscNetto) + sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/KwotaVat)"/>
                    }
                    Zapis{
                    strona =MA
                    kwota =<xsl:value-of select="sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/WartoscNetto)"/>
                    }
                    Zapis{
                    strona =MA
                    kwota =<xsl:value-of select="sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/KwotaVat)"/>
                    }
                </xsl:when>
                <xsl:when test = "invoiceV2/Naglowek/KodWaluty != 'PLN' and invoiceBound='EXTERNAL'">
                    Zapis{
                    strona =WN
                    kwota =<xsl:value-of select="sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/WartoscNetto) * invoiceV2//Wiersze/WierszV2[position()=1]/KursWaluty"/>
                    wkwota =<xsl:value-of select="sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/WartoscNetto)"/>
                    kurs = <xsl:value-of select="invoiceV2//Wiersze/WierszV2[position()=1]/KursWaluty"/>
                    waluta = <xsl:value-of select="invoiceV2/Naglowek/KodWaluty"/>
                    }
                    Zapis{
                    strona =MA
                    kwota =<xsl:value-of select="sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/WartoscNetto) * invoiceV2//Wiersze/WierszV2[position()=1]/KursWaluty + sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/KwotaVatKurs)"/>
                    wkwota =<xsl:value-of select="sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/WartoscNetto) + sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/KwotaVat)"/>
                    kurs = <xsl:value-of select="invoiceV2//Wiersze/WierszV2[position()=1]/KursWaluty"/>
                    waluta = <xsl:value-of select="invoiceV2/Naglowek/KodWaluty"/>
                    }
                    Zapis{
                    strona =WN
                    kwota =<xsl:value-of select="sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/KwotaVatKurs)"/>
                    wkwota =<xsl:value-of select="sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/KwotaVat)"/>
                    kurs = <xsl:value-of select="invoiceV2//Wiersze/WierszV2[position()=1]/KursWaluty"/>
                    waluta = <xsl:value-of select="invoiceV2/Naglowek/KodWaluty"/>
                    }

                </xsl:when>
                <xsl:otherwise>
                    Zapis{
                    strona =WN
                    kwota =<xsl:value-of select="sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/WartoscNetto) * invoiceV2//Wiersze/WierszV2[position()=1]/KursWaluty + sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/KwotaVatKurs)"/>
                    wkwota =<xsl:value-of select="sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/WartoscNetto) + sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/KwotaVat)"/>
                    kurs = <xsl:value-of select="invoiceV2//Wiersze/WierszV2[position()=1]/KursWaluty"/>
                    waluta = <xsl:value-of select="invoiceV2/Naglowek/KodWaluty"/>
                    }
                    Zapis{
                    strona =MA
                    kwota =<xsl:value-of select="sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/WartoscNetto) * invoiceV2//Wiersze/WierszV2[position()=1]/KursWaluty"/>
                    wkwota =<xsl:value-of select="sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/WartoscNetto)"/>
                    kurs = <xsl:value-of select="invoiceV2//Wiersze/WierszV2[position()=1]/KursWaluty"/>
                    waluta = <xsl:value-of select="invoiceV2/Naglowek/KodWaluty"/>
                    }
                    Zapis{
                    strona =MA
                    kwota =<xsl:value-of select="sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/KwotaVatKurs)"/>
                    wkwota =<xsl:value-of select="sum(./invoiceV2/PodsumowanieVat/PodsumowanieVat/KwotaVat)"/>
                    kurs = <xsl:value-of select="invoiceV2//Wiersze/WierszV2[position()=1]/KursWaluty"/>
                    waluta = <xsl:value-of select="invoiceV2/Naglowek/KodWaluty"/>
                    }
                </xsl:otherwise>
            </xsl:choose>
        </xsl:if>

        <!--  dane kontrahenta dla formatu FA(2), wybierane na podstawie invoiceBound -->
        <xsl:if test="invoiceV2">
            <xsl:choose>
                <xsl:when test="invoiceBound ='EXTERNAL'">
                    &#10;	Kontrahent{
                    &#009;	NIP = <xsl:value-of select="invoiceV2/Sprzedawca/DaneIdentyfikacyjne/NIP"/>
                    &#009;	PelnaNazwa = <xsl:value-of select="invoiceV2/Sprzedawca/DaneIdentyfikacyjne/PelnaNazwa"/>
                    &#009;	BrakId = <xsl:value-of select="invoiceV2/Sprzedawca/DaneIdentyfikacyjne/BrakId"/>
                    &#009;	KodKraju = <xsl:value-of select="invoiceV2/Sprzedawca/Adres/KodKraju"/>
                    &#009;	AdresL1 = <xsl:value-of select="invoiceV2/Sprzedawca/Adres/AdresL1"/>
                    &#009;	AdresL2 = <xsl:value-of select="invoiceV2/Sprzedawca/Adres/AdresL2"/>
                    <!-- pola strukturalne adresu (rozszerzenie: pełna karta kontrahenta FK) -->
                    <xsl:if test="invoiceV2/Sprzedawca/Adres/Ulica">&#10;                    &#009;	Ulica = <xsl:value-of select="invoiceV2/Sprzedawca/Adres/Ulica"/></xsl:if>
                    <xsl:if test="invoiceV2/Sprzedawca/Adres/NrDomu">&#10;                    &#009;	NrDomu = <xsl:value-of select="invoiceV2/Sprzedawca/Adres/NrDomu"/></xsl:if>
                    <xsl:if test="invoiceV2/Sprzedawca/Adres/NrLokalu">&#10;                    &#009;	NrLokalu = <xsl:value-of select="invoiceV2/Sprzedawca/Adres/NrLokalu"/></xsl:if>
                    <xsl:if test="invoiceV2/Sprzedawca/Adres/KodPocztowy">&#10;                    &#009;	KodPocztowy = <xsl:value-of select="invoiceV2/Sprzedawca/Adres/KodPocztowy"/></xsl:if>
                    <xsl:if test="invoiceV2/Sprzedawca/Adres/Miejscowosc">&#10;                    &#009;	Miejscowosc = <xsl:value-of select="invoiceV2/Sprzedawca/Adres/Miejscowosc"/></xsl:if>
                    &#009;	Email = <xsl:value-of select="invoiceV2/Sprzedawca/DaneKontaktowe/TDaneKontaktoweV2/Email"/>
                    &#009;	Telefon = <xsl:value-of select="invoiceV2/Sprzedawca/DaneKontaktowe/TDaneKontaktoweV2/Telefon"/>
                    &#009;}
                </xsl:when>
                <xsl:otherwise>
                    &#10;	Kontrahent{
                    &#009;	NIP = <xsl:value-of select="invoiceV2/Nabywca/DaneIdentyfikacyjne/NIP"/>
                    &#009;	PelnaNazwa = <xsl:value-of select="invoiceV2/Nabywca/DaneIdentyfikacyjne/PelnaNazwa"/>
                    &#009;	BrakId = <xsl:value-of select="invoiceV2/Nabywca/DaneIdentyfikacyjne/BrakId"/>
                    &#009;	KodKraju = <xsl:value-of select="invoiceV2/Nabywca/Adres/KodKraju"/>
                    &#009;	AdresL1 = <xsl:value-of select="invoiceV2/Nabywca/Adres/AdresL1"/>
                    &#009;	AdresL2 = <xsl:value-of select="invoiceV2/Nabywca/Adres/AdresL2"/>
                    <!-- pola strukturalne adresu (rozszerzenie: pełna karta kontrahenta FK) -->
                    <xsl:if test="invoiceV2/Nabywca/Adres/Ulica">&#10;                    &#009;	Ulica = <xsl:value-of select="invoiceV2/Nabywca/Adres/Ulica"/></xsl:if>
                    <xsl:if test="invoiceV2/Nabywca/Adres/NrDomu">&#10;                    &#009;	NrDomu = <xsl:value-of select="invoiceV2/Nabywca/Adres/NrDomu"/></xsl:if>
                    <xsl:if test="invoiceV2/Nabywca/Adres/NrLokalu">&#10;                    &#009;	NrLokalu = <xsl:value-of select="invoiceV2/Nabywca/Adres/NrLokalu"/></xsl:if>
                    <xsl:if test="invoiceV2/Nabywca/Adres/KodPocztowy">&#10;                    &#009;	KodPocztowy = <xsl:value-of select="invoiceV2/Nabywca/Adres/KodPocztowy"/></xsl:if>
                    <xsl:if test="invoiceV2/Nabywca/Adres/Miejscowosc">&#10;                    &#009;	Miejscowosc = <xsl:value-of select="invoiceV2/Nabywca/Adres/Miejscowosc"/></xsl:if>
                    &#009;	Email = <xsl:value-of select="invoiceV2/Nabywca/DaneKontaktowe/TDaneKontaktoweV2/Email"/>
                    &#009;	Telefon = <xsl:value-of select="invoiceV2/Nabywca/DaneKontaktowe/TDaneKontaktoweV2/Telefon"/>
                    &#009;}
                </xsl:otherwise>
            </xsl:choose>
        </xsl:if>


        <xsl:choose>
            <xsl:when test="count(invoiceV2/PodsumowanieVat/PodsumowanieVat)>0 ">
                <xsl:for-each select="invoiceV2/PodsumowanieVat/PodsumowanieVat">
                    &#10;	Rejestr{
                    &#009;	StawkaPodatku = <xsl:value-of select="StawkaPodatku"/>
                    &#009;	WartoscNetto = <xsl:value-of select="WartoscNetto"/>
                    &#009;	KwotaVat = <xsl:value-of select="KwotaVat"/>
                    &#009;	KwotaVatKurs = <xsl:value-of select="KwotaVatKurs"/>
                    &#009;}
                </xsl:for-each>
            </xsl:when>
            <!--  jeżeli rodzaj faktury = ROZ oraz kwota netto = 0, oraz brak wierszy w invoiceV2/PodsumowanieVat/PodsumowanieVat dodajemy zerowy rejestr -->
            <xsl:otherwise>
                <xsl:if test="invoiceV2/Naglowek/RodzajFaktury ='ROZ'">
                    <xsl:for-each select="invoiceV2/Wiersze/WierszV2">
                        &#10;	Rejestr{
                        &#009;	StawkaPodatku = <xsl:value-of select="StawkaPodatku"/>
                        &#009;	WartoscNetto = 0
                        &#009;	KwotaVat = 0
                        &#009;	KwotaVatKurs = 0
                        &#009;}
                    </xsl:for-each>
                </xsl:if>
            </xsl:otherwise>
        </xsl:choose>
        }
    </xsl:template>

    <xsl:template match="ksefInvoice/invoice/Naglowek/RodzajFaktury">
        &#10;	RodzajFaktury = <xsl:value-of select="."/>
    </xsl:template>
    <xsl:template match="ksefInvoice/invoice/Naglowek/DataWystawienia">
        &#10;	DataWystawienia = <xsl:value-of select="."/>
    </xsl:template>
    <xsl:template match="ksefInvoice/invoice/Naglowek/DataDostawy">
        &#10;	DataDostawy = <xsl:value-of select="."/>
    </xsl:template>
    <xsl:template match="ksefInvoice/invoice/Naglowek/Numer">
        &#10;	Numer = <xsl:value-of select="."/>
    </xsl:template>
    <xsl:template match="ksefInvoice/invoice/Naglowek/KodWaluty">
        &#10;	KodWaluty = <xsl:value-of select="."/>
    </xsl:template>
    <xsl:template match="ksefInvoice/invoice/Adnotacje/PodzielonaPlatnosc">
        &#10;	mppFlags = <xsl:value-of select="."/>
    </xsl:template>
    <xsl:template match="ksefInvoice/invoiceV2/Naglowek/RodzajFaktury">
        &#10;	RodzajFaktury = <xsl:value-of select="."/>
    </xsl:template>
    <xsl:template match="ksefInvoice/invoiceV2/Naglowek/DataWystawienia">
        &#10;	DataWystawienia = <xsl:value-of select="."/>
    </xsl:template>
    <xsl:template match="ksefInvoice/invoiceV2/Naglowek/DataDostawy">
        &#10;	DataDostawy = <xsl:value-of select="."/>
    </xsl:template>
    <xsl:template match="ksefInvoice/invoiceV2/Naglowek/Numer">
        &#10;	Numer = <xsl:value-of select="."/>
    </xsl:template>
    <xsl:template match="ksefInvoice/invoiceV2/Naglowek/KodWaluty">
        &#10;	KodWaluty = <xsl:value-of select="."/>
    </xsl:template>
    <xsl:template match="ksefInvoice/invoiceV2/Adnotacje/PodzielonaPlatnosc">
        &#10;	mppFlags = <xsl:value-of select="."/>
    </xsl:template>

    <xsl:template match="ksefInvoice/invoiceV2/Naglowek/OkresFaktury">
        OkresFaktury{
        DataOd = <xsl:value-of select="DataOd"/>
        DataDo = <xsl:value-of select="DataDo"/>
        }
    </xsl:template>

    <xsl:template match="ksefInvoice/eArchiveId">
        &#10;	eArchiveId = <xsl:value-of select="."/>
    </xsl:template>
    <xsl:template match="ksefInvoice/KsefNumber">
        &#10;	KsefNumber = <xsl:value-of select="."/>
    </xsl:template>
    <xsl:template match="ksefInvoice/KSeFStatus">
        &#10;	KSeFStatus = <xsl:value-of select="."/>
    </xsl:template>
    <xsl:template match="ksefInvoice/KSeFDate">
        &#10;	KSeFDate = <xsl:value-of select="."/>
    </xsl:template>

    <xsl:template match="ksefInvoice/invoice/Naglowek/DodatkowyOpis">
        &#10;	DodatkowyOpis{
        <xsl:for-each select="DodatkowyOpisType">
            DodatkowyOpisType{
            Key = <xsl:value-of select="Key"/>
            Value = <xsl:value-of select="Value"/>
            }
        </xsl:for-each>
        }
    </xsl:template>

    <xsl:template match="ksefInvoice/invoice/Sprzedawca">
        &#10;	Kontrahent{
        &#009;	NIP = <xsl:value-of select="DaneIdentyfikacyjne/NIP"/>
        &#009;	PelnaNazwa = <xsl:value-of select="DaneIdentyfikacyjne/PelnaNazwa"/>
        &#009;	ImiePierwsze = <xsl:value-of select="DaneIdentyfikacyjne/ImiePierwsze"/>
        &#009;	Nazwisko = <xsl:value-of select="DaneIdentyfikacyjne/Nazwisko"/>
        &#009;	KodKraju = <xsl:value-of select="Adres/KodKraju"/>
        &#009;	Miejscowosc = <xsl:value-of select="Adres/Miejscowosc"/>
        &#009;	Wojewodztwo = <xsl:value-of select="Adres/Wojewodztwo"/>
        &#009;	Ulica = <xsl:value-of select="Adres/Ulica"/>
        &#009;	Gmina = <xsl:value-of select="Adres/Gmina"/>
        &#009;	NrDomu = <xsl:value-of select="Adres/NrDomu"/>
        &#009;	NrLokalu = <xsl:value-of select="Adres/NrLokalu"/>
        &#009;	KodPocztowy = <xsl:value-of select="Adres/KodPocztowy"/>
        &#009;	Poczta = <xsl:value-of select="Adres/Poczta"/>
        &#009;	Email = <xsl:value-of select="Email"/>
        &#009;	Telefon = <xsl:value-of select="Telefon"/>
        &#009;}
    </xsl:template>


    <xsl:template match="ksefInvoice/invoice/PodsumowanieVat/PodsumowanieVat">
        &#10;	Rejestr{
        &#009;	StawkaPodatku = <xsl:value-of select="StawkaPodatku"/>
        &#009;	WartoscNetto = <xsl:value-of select="WartoscNetto"/>
        &#009;	KwotaVat = <xsl:value-of select="KwotaVat"/>
        &#009;	KwotaVatKurs = <xsl:value-of select="KwotaVatKurs"/>
        &#009;}
    </xsl:template>
 
    <xsl:template match="ksefInvoice/invoice/Korekta/FaKorygowana/DaneFaKorygowanej">
        &#10;	dokument korygowany{
        &#009;	kod = <xsl:value-of select="NrFaKorygowanej"/>
        &#009;	data = <xsl:value-of select="DataWystFaKorygowanej"/>
        &#009;	numerKsef = <xsl:value-of select="NrKSeFFaKorygowanej"/>
        &#009;}
    </xsl:template>

    <xsl:template match="ksefInvoice/invoiceV2/Korekta/FaKorygowana/DaneFaKorygowanejV2">
        &#10;	dokument korygowany{
        &#009;	kod = <xsl:value-of select="NrFaKorygowanej"/>
        &#009;	data = <xsl:value-of select="DataWystFaKorygowanej"/>
        &#009;	numerKsef = <xsl:value-of select="NrKSeFFaKorygowanej"/>
        &#009;}
    </xsl:template>

</xsl:stylesheet>