<?xml version="1.0" encoding="utf-8"?>
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
<xsl:output omit-xml-declaration="yes" method="text"/>

<!-- Ignoruj nieznane tagi-->
<xsl:template match="text()" />

<!-- Element główny -->
<!-- Kontrahent atrybuty -->
<xsl:template match="/">
  INFO{
  Nazwa programu ='Sage Symfonia XSLT' Sage Symfonia Finanse Księgowość
  Wersja programu =93
  Wersja szablonu =3.1
  Kontrahent{
&#009;&#009;kod =<xsl:value-of select="/FormatXML/DokumentHandlowy[position()=1]/Odbiorca/Kod"/>
&#009;&#009;nazwa =<xsl:value-of select="/FormatXML/DokumentHandlowy[position()=1]/Odbiorca/Nazwa"/>
&#009;&#009;miejscowosc =<xsl:value-of select="/FormatXML/DokumentHandlowy[position()=1]/Odbiorca/Miejscowosc"/>
&#009;&#009;ulica =<xsl:value-of select="/FormatXML/DokumentHandlowy[position()=1]/Odbiorca/Ulica"/>
&#009;&#009;dom =<xsl:value-of select="/FormatXML/DokumentHandlowy[position()=1]/Odbiorca/NumerDomu"/>
&#009;&#009;lokal =<xsl:value-of select="/FormatXML/DokumentHandlowy[position()=1]/Odbiorca/NumerLokalu"/>
&#009;&#009;kodpocz =<xsl:value-of select="/FormatXML/DokumentHandlowy[position()=1]/Odbiorca/KodPocztowy"/>
&#009;&#009;rejon =<xsl:value-of select="/FormatXML/DokumentHandlowy[position()=1]/Odbiorca/Wojewodztwo"/>
&#009;&#009;nip =<xsl:value-of select="/FormatXML/DokumentHandlowy[position()=1]/Odbiorca/NIP"/>
&#009;&#009;tel1 =<xsl:value-of select="/FormatXML/DokumentHandlowy[position()=1]/Odbiorca/Telefon1"/>
&#009;&#009;tel2 =<xsl:value-of select="/FormatXML/DokumentHandlowy[position()=1]/Odbiorca/Telefon2"/>
&#009;&#009;fax =<xsl:value-of select="/FormatXML/DokumentHandlowy[position()=1]/Odbiorca/Fax"/>
&#009;&#009;email =<xsl:value-of select="/FormatXML/DokumentHandlowy[position()=1]/Odbiorca/Email"/>
&#009;&#009;PodatnikVAT =<xsl:value-of select="/FormatXML/DokumentHandlowy[position()=1]/Odbiorca/PodatnikVAT"/>
&#009;&#009;jednostkaPowiazana =<xsl:value-of select="/FormatXML/DokumentHandlowy[position()=1]/Odbiorca/jednostkaPowiazana"/>
&#009;&#009;MPP =<xsl:value-of select="/FormatXML/DokumentHandlowy[position()=1]/Odbiorca/MPP"/>
  <xsl:choose>
    <xsl:when test = "string-length(/FormatXML/DokumentHandlowy[position()=1]/Odbiorca/GUIDEDS) &gt; 0">
&#009;&#009;eguid =<xsl:value-of select="/FormatXML/DokumentHandlowy[position()=1]/Odbiorca/GUIDEDS"/>
    </xsl:when>
    <xsl:otherwise>
&#009;&#009;eguid =<xsl:value-of select="/FormatXML/DokumentHandlowy[position()=1]/Odbiorca/GUID"/>
    </xsl:otherwise>
  </xsl:choose>
  }
}
<xsl:apply-templates/>
</xsl:template>

<!-- Ignoruj e-Faktura -->
<xsl:template match="e-Faktura"/>

<!-- Kontrahent nieużywane -->
	<xsl:template match="Wystawca/VIES"/>
	<xsl:template match="Wystawca/VATUE"/>
	<xsl:template match="Wystawca/NumerRachunkuBankowego"/>

<!-- Odbiorca nieużywane -->
	<xsl:template match="Odbiorca/VIES"/>
	<xsl:template match="Odbiorca/VATUE"/>
	<xsl:template match="Odbiorca/NumerRachunkuBankowego"/>

<!-- nieużywana sekcja StawkaVAT -->
<xsl:template match="StawkaVAT"/>
<!-- StawkaVAT atrybuty -->
<xsl:template match="StawkaVAT/Kod"/>
<xsl:template match="StawkaVAT/Nazwa"/>
<xsl:template match="StawkaVAT/Wartosc"/>
<xsl:template match="StawkaVAT/ObowiazujeOd"/>
<xsl:template match="StawkaVAT/ObowiazujeDo"/>
<xsl:template match="StawkaVAT/StawkaRR"/>
<xsl:template match="StawkaVAT/Typ"/>

<!-- Towar -->
<xsl:template match="Towar" />
<!--
Towar{
	<xsl:apply-templates/>
}&#10;</xsl:template>

Towar atrybuty 
  <xsl:template match="JednostkaMiary/Ewidencyjna">	jm =<xsl:value-of select="."/>&#10;&#009;</xsl:template>
  <xsl:template match="JednostkaMiary/Przelicznik1">	przeljmdod1 =<xsl:value-of select="."/>&#10;&#009;</xsl:template>
  <xsl:template match="JednostkaMiary/Przelicznik2">	przeljmdod2 =<xsl:value-of select="."/>&#10;&#009;</xsl:template>
  <xsl:template match="Rodzaj">	NazwaRodzaju =<xsl:value-of select="."/>&#10;&#009;</xsl:template>
  <xsl:template match="Towar/StawkaVAT">	stawkaVAT =<xsl:value-of select="."/>&#10;&#009;</xsl:template>
-->

<!-- Dokument -->
<xsl:template match="DokumentHandlowy">Dokument{
	rodzaj_dok =zakupu<xsl:apply-templates/>
	Zapis{
		strona =WN
		kwota =<xsl:value-of select="sum(./Stopka/KwotaVAT/Netto)"/>
		kwotaFWAL =<xsl:value-of select="sum(./Stopka/KwotaVAT/Netto) * KursCITPIT div KursVAT"/>
		waluta =<xsl:value-of select="Waluta/Kod"/>
		kurs =<xsl:value-of select="KursCITPIT"/>
		wkwota =<xsl:value-of select="(sum(./Stopka/KwotaVAT/Netto)) div KursCITPIT"/>
		wkwotaFWAL =<xsl:value-of select="(sum(./Stopka/KwotaVAT/Netto)) div KursVAT"/>
	}
	Zapis{
		strona =MA
		kwota =<xsl:value-of select="sum(./Stopka/KwotaVAT/Netto) + sum(./Stopka/KwotaVAT/VAT)"/>
		kwotaFWAL =<xsl:value-of select="(sum(./Stopka/KwotaVAT/Netto) + sum(./Stopka/KwotaVAT/VAT)) * KursCITPIT div KursVAT"/>
		waluta =<xsl:value-of select="Waluta/Kod"/>
		kurs =<xsl:value-of select="KursCITPIT"/>
		wkwota =<xsl:value-of select="(sum(./Stopka/KwotaVAT/Netto) + sum(./Stopka/KwotaVAT/VAT)) div KursCITPIT"/>
		wkwotaFWAL =<xsl:value-of select="(sum(./Stopka/KwotaVAT/Netto) + sum(./Stopka/KwotaVAT/VAT)) div KursVAT"/>
	}
	Zapis{
		strona =WN
		kwota =<xsl:value-of select="sum(./Stopka/KwotaVAT/VAT)"/>
		waluta =<xsl:value-of select="Waluta/Kod"/>
		kurs =<xsl:value-of select="KursVAT"/>
		wkwota =<xsl:value-of select="(sum(./Stopka/KwotaVAT/VAT)) div KursVAT"/>
	}
	<!-- kursy potrzebne dla faktur walutowych -->
	AtrybutWaluta{
		dane =kurs VAT
		waluta =<xsl:value-of select="Waluta/Kod"/>
		kurs =<xsl:value-of select="KursVAT"/>
	}
	AtrybutWaluta{
		dane =kurs CIT/PIT
		waluta =<xsl:value-of select="Waluta/Kod"/>
		kurs =<xsl:value-of select="KursCITPIT"/>
	}
	}
</xsl:template>

<!-- Dokument atrybuty -->
	<xsl:template match="DokumentHandlowy/GUID">&#10;	eguid =<xsl:value-of select="."/></xsl:template>
	<xsl:template match="DokumentHandlowy/Nazwa">&#10;	nazwa =<xsl:value-of select="."/></xsl:template>
	<xsl:template match="DokumentHandlowy/NumerDokumentu">&#10;	FK nazwa =<xsl:value-of select="."/></xsl:template>
  <xsl:template match="DokumentHandlowy/Charakter"/>
  <xsl:template match="DokumentHandlowy/Status"/>
  <xsl:template match="DokumentHandlowy/Typ">&#10;	typ_dk =<xsl:value-of select="."/></xsl:template>
  <xsl:template match="DokumentHandlowy/Seria">&#10;	seria =<xsl:value-of select="."/></xsl:template>
  <xsl:template match="DokumentHandlowy/Wystawca">&#10;	Dane sprzedawcy{
&#009;	kod =<xsl:value-of select="Kod"/>
&#009;	nazwa =<xsl:value-of select="Nazwa"/>
&#009;	ulica =<xsl:value-of select="Ulica"/>
&#009;	dom =<xsl:value-of select="NumerDomu"/>
&#009;	lokal =<xsl:value-of select="NumerLokalu"/>
&#009;	miejscowosc =<xsl:value-of select="Miejscowosc"/>
&#009;	kodpocz =<xsl:value-of select="KodPocztowy"/>
&#009;	nip =<xsl:value-of select="NIP"/>
&#009;	regon =<xsl:value-of select="REGON"/>
&#009;	pesel =<xsl:value-of select="PESEL"/>
&#009;	krajkod =<xsl:value-of select="Kraj"/>
&#009;	rejon =<xsl:value-of select="Wojewodztwo"/>
&#009;	tel1 =<xsl:value-of select="Telefon1"/>
&#009;	tel2 =<xsl:value-of select="Telefon2"/>
&#009;	fax =<xsl:value-of select="Fax"/>
&#009;	email =<xsl:value-of select="Email"/>
&#009;	PodatnikVAT =<xsl:value-of select="PodatnikVAT"/>
&#009;	jednostkaPowiazana =<xsl:value-of select="jednostkaPowiazana"/>
&#009;	MPP =<xsl:value-of select="MPP"/>
&#009;	numerrachunkubankowego =<xsl:value-of select="NumerRachunkuBankowego"/>
    <xsl:choose>
      <xsl:when test = "string-length(GUIDEDS) &gt; 0">
&#009;	khguid =<xsl:value-of select="GUIDEDS"/>
&#009;	guid =<xsl:value-of select="GUIDEDS"/>
      </xsl:when>
      <xsl:otherwise>
&#009;	khguid =<xsl:value-of select="GUID"/>
&#009;	guid =<xsl:value-of select="GUID"/>
      </xsl:otherwise>
    </xsl:choose>
&#009;}</xsl:template>
	<xsl:template match="DokumentHandlowy/Rejestr">&#10;	nazwa_rejestru_vat =<xsl:value-of select="."/></xsl:template>
  <xsl:template match="DokumentHandlowy/RodzajCeny">&#10;	grupacen =<xsl:value-of select="translate(.,'NBetoru','32')"/></xsl:template>
  <xsl:template match="DokumentHandlowy/NaliczanieVAT">&#10;	flag =<xsl:value-of select="translate(.,'NBetoru','32')"/></xsl:template>
  <xsl:template match="DokumentHandlowy/DataWystawienia">&#10;	data =<xsl:value-of select="."/></xsl:template>
  <xsl:template match="DokumentHandlowy/DataSprzedazy">&#10;	datasp =<xsl:value-of select="."/>&#10;	okres =<xsl:value-of select="substring(.,1,8)"/>01</xsl:template>
  <xsl:template match="DokumentHandlowy/RejestrPlatnosci">&#10;	rejestr_platnosci =<xsl:value-of select="."/></xsl:template>
  <xsl:template match="DokumentHandlowy/FormaPlatnosci">&#10;	forma_platnosci =<xsl:value-of select="."/></xsl:template>
  <xsl:template match="DokumentHandlowy/TerminPlatnosci"/>
  <xsl:template match="DokumentHandlowy/DataPlatnosci"/>
  <xsl:template match="DokumentHandlowy/DataWplywu">&#10;	datawpl =<xsl:value-of select="."/></xsl:template>
  <xsl:template match="DokumentHandlowy/DataOdprawy">&#10;	dataOdprawy =<xsl:value-of select="."/></xsl:template>
  <xsl:template match="DokumentHandlowy/Waluta">&#10;	waluta =<xsl:value-of select="Kod"/></xsl:template>
  <xsl:template match="DokumentHandlowy/KursVAT">&#10;	kurs =<xsl:value-of select="."/></xsl:template>
  <xsl:template match="DokumentHandlowy/KursCITPIT">&#10;	kursDoch =<xsl:value-of select="."/></xsl:template>
  <xsl:template match="DokumentHandlowy/OdebranyPrzez">&#10;	odebrane =<xsl:value-of select="."/></xsl:template>
  <xsl:template match="DokumentHandlowy/MPP">&#10;	mppFlags =<xsl:value-of select="."/></xsl:template>
  <xsl:template match="DokumentHandlowy/EArchiveGuid">&#10;	guidEarch =<xsl:value-of select="."/></xsl:template>
  <xsl:template match="DokumentHandlowy/KsefNumber">&#10;	numerKsef =<xsl:value-of select="."/></xsl:template>
  <xsl:template match="DokumentHandlowy/KsefIssueDate">&#10;	dataWystawieniaKsef =<xsl:value-of select="."/></xsl:template>

	<!-- Stopka/KwotaVAT Rejestr Dokumentu -->
<xsl:template match="Stopka/KwotaVAT">
&#009;Rejestr{<xsl:apply-templates/>
<!-- &#009;&#009;okres =<xsl:value-of select="substring(../../DataWplywu,1,8)"/>01 logika wyboru okresu rejestru vat powinna zostać przeniesiona do aplikacji-->
&#009;}</xsl:template>

<!-- Stopka/KwotaVAT Rejestr Dokumentu atrybuty -->
	<xsl:template match="Stopka/KwotaVAT/StawkaVAT">&#10;&#009;	stawka =<xsl:choose>
		<xsl:when test=".='ZW'">-1</xsl:when>
		<xsl:when test=".='NP'">-2</xsl:when>
		<xsl:otherwise><xsl:value-of select="translate(.,'0123456789% ','0123456789')"/>.00</xsl:otherwise>
	</xsl:choose></xsl:template>
	<xsl:template match="Stopka/KwotaVAT/Netto">&#10;&#009;	netto =<xsl:value-of select="."/>
		nettoWaluta =<xsl:value-of select=". div ../../../KursVAT"/></xsl:template>
	<xsl:template match="Stopka/KwotaVAT/VAT">&#10;&#009;	vat =<xsl:value-of select="."/>&#10;&#009;	brutto =<xsl:value-of select=". + ../Netto"/>
		vatWaluta = <xsl:value-of select=". div ../../../KursVAT"/>&#10;&#009;	bruttoWaluta =<xsl:value-of select="(. + ../Netto) div ../../../KursVAT"/></xsl:template>


<!-- Pozycja Dokumentu -->
<xsl:template match="PozycjaDokumentu">
&#009;Pozycja dokumentu{<xsl:apply-templates/>
&#009;}</xsl:template>

<!-- Pozycja Dokumentu atrybuty -->
	<xsl:template match="PozycjaDokumentu/Lp"/>
	<xsl:template match="PozycjaDokumentu/KodTowaru">&#10;&#009;	kod =<xsl:value-of select="."/></xsl:template>
	<xsl:template match="PozycjaDokumentu/NazwaTowaru/Opis">&#10;&#009;	opis_pozycji =<xsl:value-of select="."/></xsl:template>
	<xsl:template match="PozycjaDokumentu/JednostkaMiary">&#10;&#009;	jm =<xsl:value-of select="."/>&#10;&#009;&#009;jmwp =<xsl:value-of select="."/></xsl:template>
	<xsl:template match="PozycjaDokumentu/Ilosc">&#10;&#009;	ilosc =<xsl:value-of select="."/>&#10;&#009;&#009;iloscwp =<xsl:value-of select="."/></xsl:template>
	<xsl:template match="PozycjaDokumentu/Cena">&#10;&#009;	cena =<xsl:value-of select="."/></xsl:template>
	<xsl:template match="PozycjaDokumentu/StawkaVAT">&#10;&#009;	stawkaVAT =<xsl:value-of select="translate(.,'0123456789%','0123456789')"/>.00</xsl:template>
	<xsl:template match="PozycjaDokumentu/Wartosc">&#10;&#009;	wartNetto =<xsl:value-of select="."/></xsl:template>
	<xsl:template match="PozycjaDokumentu/WartoscVAT">&#10;&#009;	wartVat =<xsl:value-of select="."/></xsl:template>

<!-- Magazyn -->
<xsl:template match="Magazyn" />

</xsl:stylesheet>


