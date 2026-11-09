# Ukázky stránek pro testy

Testy běží offline nad uloženými stránkami portálů. Tady je, odkud se vzaly
a co je v nich změněné.

| Soubory | Původ |
|---|---|
| `syntetika_*.html` | ručně napsané podle struktury skutečných stránek, žádná skutečná data |
| `jobs_cz_detail_*_zkraceny.html` | ručně zkrácené skutečné detaily (bez osobních údajů) |
| ostatní (`jobs_cz_vypis_*`, `prace_cz_*`, `jobscontact_*`) | **anonymizované** kopie skutečných stránek |

## Anonymizace

Kopie vyrábí `tools/anonymize_samples.py` z originálů v `samples/private/`.
Originály ani soukromá mapa náhrad (`samples/private/anonymize_map.json`) nejsou
v gitu. Obsahují plné texty inzerátů, jména a kontakty personalistů a lokalitu
hledání, ze kterého stránky pocházejí.

V kopiích:

- **zůstává struktura**, kterou čtou parsery: tagy, třídy, `data-testid`,
  JSON-LD, ID inzerátů, odkazy stránkování, názvy pozic, firmy, místa a mzdy
  z výpisu (veřejné údaje firem),
- **text inzerátů** je nahrazený výplní podobné délky. Zůstaly jen nadpisy
  sekcí a pár frází, na které se ptají testy,
- **jména, telefony a e-maily** jsou smyšlené (`+420 000 000 000`,
  `kontakt@example.com`),
- **lokalita hledání** je změněná na Brno a **částka filtru mzdy** na 50 000,
- **skripty a styly** jsou odstraněné. Data, která parser prace.cz čte
  z vloženého JSON (ID jobs.cz, data zveřejnění), zůstala doslovně.

Že anonymizace nerozbila strukturu, ověřuje `tests/test_anonymized_samples.py`:
kopie se musí parsovat stejně jako originál (ID, názvy, firmy, místa, mzdy,
data, další stránka, způsob čtení detailu). Bez originálů se tento test
přeskočí. Kontrola, že v kopiích nezůstal telefon ani e-mail, běží vždy.

## Nová ukázka

1. Ulož stránku: `python -m jobagent sample <url> --name <název>`. Uloží se
   rovnou do `samples/private/`, mimo git.
2. Přidej ho do `SPEC` v `tools/anonymize_samples.py` a případně doplň mapu
   náhrad a `KEEP_PHRASES`.
3. Spusť `python tools/anonymize_samples.py` a `python -m unittest`.
4. Do gitu jde jen kopie v `samples/`. Před commitem ji projdi očima.

`samples/nezname/` sbírá stránky, které program za běhu neuměl přečíst. Je to
podklad k prozkoumání, není v gitu a může obsahovat osobní údaje.
