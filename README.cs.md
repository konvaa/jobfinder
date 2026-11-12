# Job Agent

Osobní nástroj, který jednou denně projde české pracovní portály, vyřadí
nevhodné nabídky, zbylé ohodnotí podle tvých klíčových slov a připraví souhrn
e-mailem.

*English: [README.md](README.md).*

> **Stav:** rozpracováno. Sběr, filtry, bodování a odesílání souhrnu e-mailem
> fungují. Volitelné AI hodnocení (fáze 5) a samostatný `.exe` pro Windows
> (fáze 6) zatím nejsou.
> Návod pro kamarády krok za krokem (exe, bez instalace Pythonu) přibude
> ve fázi 6. Tohle je zatím návod pro spuštění ze zdrojáků.

## Co dělá

1. Projde tvá hledání. Hledání je adresa zkopírovaná z portálu poté, co si tam
   nastavíš filtry (místo, okruh, obor).
2. Stáhne stránky výpisu a v rámci limitu na běh i detaily inzerátů.
3. Filtry: maximální stáří, minimální mzda (inzerát **bez** uvedené mzdy se
   nikdy nevyřadí), slova, která inzerát vyřadí, a slova, která ho vyřadí jen
   v názvu.
4. Boduje podle tvých slov (± body, skupiny synonym, hvězdičky, slova jen
   v názvu) a u každého bodu napíše proč („nalezeno X v názvu“).
5. Sloučí stejný inzerát z více portálů (podle ID, nebo název + firma + obec;
   při nejistotě neslučuje).
6. Pamatuje si inzeráty v SQLite (viděné, vyřazené, stažené detaily). Až bude
   hotové odesílání, inzerát se označí jako odeslaný až po úspěšném odeslání,
   takže budeš dostávat jen nové.
7. Připraví HTML souhrn: hlavní tabulka, „zajímavé podle názvu“, ostatní
   a vyřazené s důvodem.

Podporované portály: **jobs.cz**, **prace.cz**, **jobscontact.cz**. Každý
portál je jeden soubor v `jobagent/collectors/`.

## Zásady

- **Osobní použití.** Nástroj pro jednoho člověka, který hledá práci, ne
  robot na sběr dat. Inzeráty z něj dál nezveřejňuj.
- **robots.txt** se kontroluje u každého webu (RFC 9309). Když se kvůli chybě
  serveru nebo sítě načíst nedá, web se v tom běhu přeskočí.
- **Ohleduplné tempo:** 1–3 s mezi požadavky, limit stránek a detailů na běh,
  časový limit a poctivý User-Agent s odkazem na tento repozitář.
- **Spouštět nejvýš jednou denně.**
- Žádné přihlašování a žádná neveřejná API. U stránek, které text inzerátu
  dotahují JavaScriptem z neveřejného API, zůstanou jen údaje z výpisu.
- Projekt nemá s portály nic společného.

## Co potřebuješ

- Windows (vývoj jde i jinde, ale plánovač a budoucí `.exe` míří na Windows)
- Python 3.12 (spouštěný přes `py -3.12`)
- `beautifulsoup4` (přesné verze včetně závislostí v `requirements.txt`)

## Instalace

```bat
git clone https://github.com/konvaa/jobfinder.git
cd jobfinder

:: 1) Virtuální prostředí – jednou
py -3.12 -m venv .venv

:: 2) Aktivace – v každém novém okně příkazového řádku
.venv\Scripts\activate

:: 3) Závislosti v přesných verzích – jednou a po každé změně requirements.txt
python -m pip install -r requirements.txt

:: 4) Nastavení – jednou
copy config.example.toml config.toml
python -m jobagent nastav-heslo
```

Proč `.venv`: knihovny se instalují jen pro tento projekt, ne do celého
systému. Verze z `requirements.txt` jsou pak přesně ty, se kterými se bude
balit exe.

Po aktivaci stačí `python` (ukazuje do `.venv`). Bez aktivace funguje
`.venv\Scripts\python.exe -m jobagent ...`. PowerShell může aktivaci zablokovat
(„running scripts is disabled“). Pak použij `cmd`, nebo jednou spusť
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

## Nastavení

`config.toml` uprav v Poznámkovém bloku. Je hustě okomentovaný: hledání,
filtry, slova a body, souhrn e-mailem a volitelné AI. Hesla do něj nepatří.
Config není v gitu.

E-mail: v `[email]` vyplň server, `uzivatel` (celá adresa) a `prijemci`. Heslo
ulož příkazem `nastav-heslo`: program se nejdřív zkusí přihlásit (nic neodešle)
a heslo uloží zašifrované do **Správce pověření Windows** (Ovládací panely →
Správce přihlašovacích údajů → Přihlašovací údaje systému Windows → položka
`JobAgent/…`). Smazání: `smaz-heslo`. Mimo Windows patří heslo do `secrets.toml`
(vzor `secrets.example.toml`, není v gitu). U Seznamu se zapnutým dvoufázovým
ověřením a u Gmailu je to **heslo pro aplikace**, ne heslo k účtu. Doporučení:
na odesílání si založ samostatnou schránku, ať případně uniklé heslo neotevře
tvou hlavní poštu. Nastavení ověříš příkazem `test-email`.

Co Správce pověření chrání: heslo neleží v souboru, který se dá omylem poslat
dál nebo zálohovat. Před škodlivým programem spuštěným pod tvým účtem ale
nechrání, ten si heslo přečte stejně jako Job Agent.

## Příkazy

```bat
python -m jobagent run                 :: ostrý běh: sběr, hodnocení, odeslání souhrnu
python -m jobagent run --dry-run       :: běh bez odeslání, náhled e-mailu se otevře v prohlížeči
python -m jobagent test-email          :: krátký zkušební e-mail (ověří server, heslo, příjemce)
python -m jobagent test-email --nahled :: znovu pošle poslední uloženou zprávu z vystup\ (předmět „TEST – …“)
python -m jobagent sablony             :: vytvoří templates\ s šablonami e-mailu k úpravě
python -m jobagent nastav-heslo        :: heslo k e-mailu do Správce pověření Windows (ověří přihlášení)
python -m jobagent smaz-heslo          :: smaže ho odtud
python -m jobagent install-schedule    :: denní běh v Plánovači úloh (výchozí 7:30; jiný čas: install-schedule 18:05)
python -m jobagent uninstall-schedule  :: zruší ho
python -m jobagent run --dry-run -v    :: totéž s podrobným výpisem
python -m jobagent vysvetli <url>      :: proč má inzerát takové skóre: zdroj textu, odstraněné řádky,
                                       :: text, filtry, nalezená slova s místem, body a úryvkem
python -m jobagent vysvetli soubor.html  :: totéž offline nad uloženou stránkou
python -m jobagent sample <url>        :: uloží HTML stránky do samples\private\ (není v gitu)
python -m unittest -v                  :: testy (offline)
```

Návratové kódy (Plánovač úloh je ukazuje ve sloupci „Výsledek posledního
spuštění“): 0 = OK, 1 = chyba (např. selhala všechna hledání), 2 = chyba
v nastavení, 3 = e-mail se nepodařilo odeslat. Při kódu 3 se nic neoznačí jako
odeslané, příští běh pošle tytéž nabídky znovu a neodeslaný souhrn leží ve
`vystup\` (`…_NEODESLANO.html` a `.eml`).

Složka `vystup\`: každý běh (i `--dry-run`) uloží `email_…html` (celý souhrn
k otevření v prohlížeči) a `email_…eml` (přesně zpráva, která odešla nebo by
odešla: tělo, textová verze i příloha; otevře ji Outlook i Thunderbird).
Soubory starší než `[obecne] uchovat_vystup_dni` (výchozí 30, 0 = nemazat)
se při dalším běhu smažou. Maže se jen `email_*`, jiné soubory ve složce zůstanou.

### Denní běh (Plánovač úloh)

`install-schedule` vytvoří v Plánovači úloh úlohu „Job Agent“:
- **denně v zadaný čas** (výchozí 7:30) plus náhodně až 15 minut, aby se
  všichni uživatelé neptali portálů ve stejnou vteřinu;
- **jen když jsi přihlášený** – bez uloženého hesla Windows a bez práv
  správce; díky tomu funguje i heslo ve Správci pověření;
- **zmeškaný start** (vypnutý počítač) proběhne zhruba do 10 minut po
  zapnutí a přihlášení;
- běží **i na baterii**, nejvýš 2 hodiny, nikdy dvakrát najednou;
- **bez okna**: u exe spouští `JobAgent-planovac.exe`, ze zdrojáků
  `pythonw.exe -m jobagent run` (Python bez konzole).

Opakované `install-schedule` úlohu přepíše (nový čas, nová složka). Když
složku s programem přesuneš, ostrý běh to pozná a v e-mailu poradí spustit
`install-schedule` znovu (jen u exe).

## Kde co je

| Cesta | Obsah |
|---|---|
| `jobagent/` | zdrojáky, popis v hlavičce každého souboru |
| `jobagent/collectors/` | jeden soubor na portál + registr v `__init__.py` |
| `jobagent/templates/` | výchozí šablony e-mailu (součást programu) |
| `templates/` | tvoje upravené šablony; vytvoří je `sablony`, nejsou v gitu |
| `samples/` | anonymizované stránky portálů = testovací data, viz `samples/README.md` |
| `tools/anonymize_samples.py` | anonymizace skutečných stránek pro testy |
| `docs/ARCHITEKTURA.md` | rozhodnutí a jejich důvody |
| `config.toml`, `secrets.toml`, `jobagent.db`, `logs/`, `vystup/` | osobní a běhová data (nejsou v gitu) |

## Licence

[MIT](LICENSE) © 2026 Libor Konvičný
