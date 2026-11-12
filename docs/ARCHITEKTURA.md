# Architektura a rozhodnutí

Proč je Job Agent postavený tak, jak je. Každé rozhodnutí má důvod. Většina
vznikla z konkrétního problému na skutečných stránkách portálů (listopad 2026).

## 1. Zásady

| Rozhodnutí | Proč |
|---|---|
| Python 3.12, jen standardní knihovna (`urllib`, `sqlite3`, `smtplib`, `tomllib`) + `beautifulsoup4`. Claude API se volá přes REST (`urllib`), ne přes SDK. Každá další závislost se musí obhájit. | Menší exe, méně věcí, které se můžou rozbít, jednodušší build. |
| Závislosti v `requirements.txt` s přesnými verzemi včetně tranzitivních, vývoj ve `.venv`. | Opakovatelný build exe. |
| Dva TOML soubory: `config.toml` (sdílitelný) a `secrets.toml` (hesla, API klíč). V repu jsou jen `*.example.toml`. | Jeden formát pro všechno, žádná knihovna navíc. Config se dá poslat dál bez rizika úniku hesel. |
| Všechno osobní je v configu, nic v kódu. Config je hustě okomentovaný česky, aby ho upravil i neprogramátor v Poznámkovém bloku. | Nástroj má sloužit i kamarádům bez programování. |
| Hledání se zadává jako URL zkopírovaná z portálu. | Filtry portálu (místo, okruh, obor) se nemusí implementovat znovu a uživatel vidí na webu přesně to, co bude program procházet. |
| Collectory jako pluginy: jeden soubor na portál v `collectors/`, společné rozhraní, společný objekt `Job`. Registr je explicitní seznam (`collectors/__init__.py`), ne automatické hledání modulů. | Portály se rozbíjejí a přibývají. Explicitní seznam spolehlivě zabalí PyInstaller a jeho pořadí určuje přednost při duplicitě. |
| Cesty u exe: `sys.frozen` → složka vedle exe (`Path(sys.executable).parent`), nikdy `sys._MEIPASS`. Řeší jen `paths.py`. | `_MEIPASS` je dočasná rozbalená složka, config a DB by se ztratily. |
| Log do souboru vedle programu (rotace 1 MB × 5). | Při spuštění z Plánovače úloh není vidět konzole. |

## 2. Zdvořilé stahování

| Rozhodnutí | Proč |
|---|---|
| Prodleva 1–3 s mezi požadavky, limit stránek výpisu a detailů na běh, timeout, poctivý User-Agent s adresou projektu (bez e-mailu), bez `Accept-Encoding`. Spouštět nejvýš jednou denně. | Osobní nástroj nesmí zatěžovat portály. |
| robots.txt podle RFC 9309: 4xx = bez omezení (loguje se jako DEBUG), 5xx nebo chyba sítě po jednom opakování = web v tomto běhu přeskočit a uvést to ve stavovém řádku. | Nedostupný robots.txt neznamená souhlas. |
| Žádné přihlašování, žádná neveřejná API. Kariérní weby, které text inzerátu dotahují JavaScriptem z neveřejného API widgetu, zůstávají jen s daty z výpisu. | Žádná šedá zóna. |
| Cookies se drží v paměti po dobu běhu (`http.cookiejar`). | jobs.cz dělá A/B test a část požadavků přesměruje na beta.jobs.cz (jiná aplikace, inzeráty dotahuje JS, přesměrování zahodí okruh i stránkování). Bez cookies se los házel u každého požadavku. |
| `Collector.unsupported_hosts` + `fetch_page()`: odpověď z nepodporované verze webu se zahodí, session se vyčistí a zkusí se znovu (nejvýš 3×). Když beta vytrvá na stránce 2+, předchozí stránky zůstanou a v e-mailu je upozornění. | Jedna chybná odpověď nesmí zahodit celé hledání. |
| Varování „hledání je pravděpodobně oříznuté limitem stránek“, když smyčka skončí na limitu a portál nabízí další stránku. | Přesnější než odhad podle plné poslední stránky. |
| Prázdná stránka výpisu, na kterou vedl odkaz „další“, se stáhne ještě jednou. Teprve když je prázdná i napodruhé, uloží se do `samples/nezname/` a průchod je neúplný. Portál může dodat vysvětlení (`Collector.empty_listing_reason`). | jobs.cz občas jednorázově vrátí „Nenašli jsme žádné nabídky“, přestože jeho stránkování stránku uvádí. O chvíli později stejná adresa vrátí plnou stránku. |

## 3. Detail inzerátu

| Rozhodnutí | Proč |
|---|---|
| Tři vrstvy: JSON-LD (`schema.org/JobPosting`) → CSS selektory portálu → hlavní text stránky. | JSON-LD je pro Google Jobs a mění se méně často než CSS třídy. Nový portál má být co nejméně kódu. |
| Hlavní text se vezme, jen když je na stránce název pozice (aspoň 60 % jeho slov) a má aspoň 300 znaků. Jinak `bez-textu`. | Kariérní weby s JS vracely jako „popis“ patičku nebo text „Naše vize a mise“. |
| Selektory tam, kde obecná vrstva selhává (jobscontact.cz: `<article>` je upoutávka na blog, v boku „Další pozice v oboru“). | Obecná heuristika by vzala cizí text. |
| V DB se ukládá SUROVÝ text. Čistí se vždy při použití přes `Job.text` (`cleaning.py`): filtry, bodování, `vysvetli`, AI. | Vylepšení pravidel čištění se projeví i u starších inzerátů bez nového stahování. |
| Čištění odstraňuje šablonové řádky: blok klíčových slov agentur („vhodná pro pozice: …“), souhlas se zpracováním osobních údajů, odkaz na další nabídky. Samotné „GDPR“ zůstává. | Šablona agentury přidávala stavbyvedoucímu body za C#, .NET a SQL. „Znalost GDPR“ bývá skutečný požadavek. |
| `Job.text` má vlastní mezipaměť podle obsahu popisu, ne `functools.cached_property`. | Popis se během běhu mění (výpis → detail). `cached_property` by vrátila zastaralý prázdný text. |

## 4. Filtry a bodování

| Rozhodnutí | Proč |
|---|---|
| Inzerát bez uvedené mzdy se nikdy nevyřadí. U rozpětí rozhoduje horní hranice, hodinová mzda × `hodin_mesicne`, roční / 12. Při nejistotě ponechat. | Chybějící mzda neznamená nízkou mzdu. |
| `max_stari_dni` a pro první ostrý běh každého hledání (podle URL) delší `max_stari_prvni_beh`. Dry-run se nepočítá. | Nové hledání a nový portál nesmí přijít o starší nabídky. |
| Čtyři sekce slov: `[hodnoceni.slova]` (± body kdekoli), `[hodnoceni.slova_v_nazvu]` (jen název), `filtry.vyradit`, `filtry.vyradit_v_nazvu`. Záporné body místo zvláštní „penalizace“. Starý klíč vyvolá varování s návodem. | Čtyři srozumitelné sekce místo šesti, každé slovo má vlastní srážku. |
| Porovnání bez diakritiky a velikosti písmen, celá slova. `*` kdekoli ve slově. Úvodní `*` jen u úseků se 4+ znaky, kratší se vynechá s varováním. | Skloňování („Pythonu“, „dvousměnný“). „*it*“ by našlo „digitalizace“ i „kvalita“. |
| Skupiny synonym `"a \| b \| c"`: v inzerátu se počítají nejvýš jednou, na nejlepším místě (název × násobek > štítek > popis). | Synonyma se sčítala (např. pružná doba dvakrát). |
| Delší shoda má přednost u konkrétního výskytu: kratší zápis se nezapočítá, když jeho shoda leží celá uvnitř shody delšího. Jinde v textu platí normálně. Jen bodování, ne filtry. | „python vývojář*“ (20) a „python*“ (15) nemají dát 35. |
| Zdůvodnění skóre uvádí nalezenou variantu a místo (název / štítek / popis). | Bez toho nešlo poznat, že většina bodů za „z domova“ pochází ze štítku „Možnost občasné práce z domova“. |
| Pořadí stahování detailů: skóre z výpisu, při shodě novější datum, pak uvedená mzda. | Při limitu detailů rozhodovalo pořadí ve výpisu mezi desítkami inzerátů se stejným skóre. |
| Parametry portálu, které vyřadí inzeráty bez štítku (např. „pružná pracovní doba“), se z URL odstraňují a benefit se jen boduje. | Filtr ubíral zhruba třetinu nabídek a závisí na tom, jestli firma benefit zaškrtla. |

## 5. Skupiny nabídek v e-mailu

| Skupina | Kam | Pravidla |
|---|---|---|
| 1. S textem detailu | hlavní tabulka / „Ostatní“ podle `souhrn.hranice_skore` | `min_skore` platí jen tady. Jediná skupina pro AI. |
| 2. Trvale bez textu | „Zajímavé podle názvu – bez popisu“ (body v názvu > 0), jinak „Ostatní“ | Trvale = stránka s JS (`bez-textu`) nebo stažení selhalo ve 3 různých bězích (počítadlo v DB). |
| 3. Dočasně bez textu (limit, chyba) | v e-mailu není, stavový řádek „čeká na detail N“ | Neoznačí se jako odeslané, zařadí se po stažení detailu. Když počet roste 3 ostré běhy po sobě, přijde varování. |

Proč: skóre bez popisu se se skórem s popisem nedá srovnat. „Ostatní“ jsou
jednořádkové záznamy pod obyčejným nadpisem. Tělo e-mailu `<details>` nepoužívá:
webmail Seznamu ho při čištění HTML odstraní i s obsahem (sekce by zmizela)
a Gmail ani Outlook ho stejně nesbalí. Sbalitelná je „Ostatní“ jen v příloze
`job-agent-souhrn.html`, která se otevírá v prohlížeči.

Stavový řádek sčítá beze zbytku: nalezeno = unikátní + opakované (stejné ID ve
víc hledáních); unikátní = nové + čeká + vyřazené + dříve vyřazené + duplicity
+ už odeslané. Hlídá `RunReport.balance_error()`.

## 6. Deduplikace mezi portály

| Rozhodnutí | Proč |
|---|---|
| Slučuje se **před** stahováním detailů. | Pro jeden inzerát se nestahují dva detaily. |
| Pořadí shody: 1) ID jobs.cz (prace.cz ho nese ve vloženém JSON výpisu, kariérní weby v odkazu `firma.jobs.cz/pd/ID`), 2) název + firma + **obec**, jen když jedné verzi ID chybí. Dvě verze s různými ID se neslučují. | ID je spolehlivé. Agentury mívají stejně pojmenované pozice v různých městech. |
| **Při nejistotě neslučovat:** bez firmy, bez obce, s krajem / „Česko“ / víc místy klíč neexistuje. | Duplicita v e-mailu je menší zlo než ztracený inzerát. |
| Normalizace názvu odstraní diakritiku, rod („/ka“, „(m/ž)“), interpunkci a **mzdu** („(40.000 - 50.000 Kč)“, „\| až 90.000,-“, „- až 200 tis Kč“…). Číslo bez „Kč“ nebo „,-“ („13. plat“, „#3094“) zůstává. | jobs.cz u inzerátů agentur připojuje mzdu k názvu, na webu agentury chybí. Ověřeno na skutečných párech: „odstranit jen mzdu“ dává shody bez falešných, „uříznout vše za \|“ i falešné (za svislítkem bývá to, čím se inzeráty liší). |
| `town()`: „Ostrava-město“ → ostrava, „Hlavní město Praha“ → praha, „…-venkov / -východ / -západ“ = nevíme. | Web agentury uvádí okres, ne obec. „Brno-venkov“ není Brno. |
| Přednost má portál výš v `COLLECTORS` (jobs.cz): odkaz, ID, historie. Chybějící údaje se doplní z duplicity, v e-mailu „jobs.cz + prace.cz“. Výjimka: když verze z jobs.cz skončí bez textu a duplicita na vlastním webu portálu text má, použije se text duplicity. | Jedna verze, ale co nejúplnější. |
| DB si pamatuje i ID jobs.cz z jiných portálů. Inzerát, který už odešel z jiného portálu, se nepošle znovu. | Deduplikace i napříč běhy. |

## 7. Skrytá mzda

jobs.cz a prace.cz filtrují i podle mzdy, kterou firma zadala a nezveřejnila.
Filtr mzdy v URL tak vyřadí i inzeráty, které mzdu vůbec neukazují (ověřeno:
všechny vyřazené inzeráty v testovaných hledáních byly bez viditelné mzdy).

| Rozhodnutí | Proč |
|---|---|
| Collector má `salary_param` (jobs.cz `salary`, prace.cz `salaryMin`). Úpravu URL a průchod dělá základní třída. | Nový portál se stejným principem = jeden řádek. |
| Každé hledání: 1) bez filtru mzdy (inzeráty, detaily), 2) s `min_mzda`, jen stránky výpisu. Limit detailů se nečerpá. | Inzerát bez mzdy se nesmí ztratit, ale signál portálu je užitečný. |
| Parametr mzdy z URL v configu se vždy odstraní (s varováním). Úprava URL je textová, ostatní parametry zůstanou přesně. | Jedna hodnota minima na jednom místě. |
| Jen u inzerátů **bez viditelné mzdy**: neprošel filtrem → „skrytá, podle <portál> pravděpodobně pod minimem“ + srážka `srazka_skryta_mzda` (i pro body v názvu). Prošel → „≥ <minimum> Kč“, bez bodů. Nevyřazuje se. | Jistota to není, proto srážka. |
| **Nejistota → nic:** „neprošel“ jen z úplného druhého průchodu (bez chyby, bez oříznutí, bez prázdné stránky). „Prošel“ platí i z neúplného. Víc hledání: „prošel“ kdekoli vyhrává. Rozdílná odpověď dvou portálů → nic. | Inzerát mohl být na nepřečtené stránce. |
| Detail doplní viditelnou mzdu → skrytá mzda se zruší a mzdu hlídá vlastní filtr. | – |
| jobscontact.cz má `jobFilterSalary`, ale ten filtruje jen podle viditelné mzdy → žádný druhý průchod. | Skrytou mzdu nezná. |

## 8. Portály

| Portál | Zvláštnosti |
|---|---|
| **jobs.cz** | Klasická šablona: karty `article.SearchResultCard`, detail `/rpd/ID/` se stabilními `data-test="jd-…"`. A/B test na beta.jobs.cz (viz 2). Kariérní weby firem (`*.jobs.cz`) jsou často jen JS. |
| **prace.cz** | Nová platforma Alma Career (stejná jako beta.jobs.cz), obecný parser `collectors/alma_next.py`: čte podle `article#advert-<uuid>`, `data-testid` a skrytých popisků, ne podle generovaných CSS tříd. ID jobs.cz a data zveřejnění z vloženého JSON. Detail má JSON-LD. |
| **jobscontact.cz** | Web personální agentury. Okruh `jobFilterRadius` v URL, `itemsPerPage=100` přidává collector sám (1 požadavek místo 4–5). Firma ani datum na webu nejsou: firma je pevně agentura (kvůli deduplikaci), filtr stáří se neuplatní. Místo je okres; víc míst z tooltipu, spojené čárkou. |

## 9. Databáze

SQLite s `PRAGMA user_version` a migracemi, které jdou spustit opakovaně
(sloupec se přidá, jen když chybí). Přerušená migrace se tak dokončí při
dalším běhu. Pamatuje si odeslané a vyřazené inzeráty, stav detailu a počet
pokusů, ID z jiných portálů, historii hledání a skrytou mzdu (jen pro
`vysvetli`; denní běh ji vždy počítá znovu).

## 10. Odesílání e-mailu

Obecné SMTP ze standardní knihovny (`smtplib`, `email.message`). Výchozí
hodnoty jsou pro Seznam: `smtp.seznam.cz`, port 465, `zabezpeceni = "ssl"`
(587 = `"starttls"`). Starý klíč `ssl = true/false` se ještě přečte, s varováním.

- **Zpráva:** `multipart/alternative` s textovou a HTML verzí (textová je
  pro filtry spamu i pro čtečky bez HTML). Předmět nese počty („5 nových
  (2 v hlavní tabulce)“, „dnes nic nového“), na začátku „⚠“, když některý
  portál selhal nebo se přeskočil.
- **Velikost:** Gmail usekává HTML tělo kolem 100 kB. Nad `BODY_LIMIT_BYTES`
  zůstane v těle prvních 20 „ostatních“ a celý souhrn jde jako příloha
  `job-agent-souhrn.html`.
- **Pořadí „odeslat → označit“:** nabídky a příznak `runs.delivered` se zapíší
  v jedné transakci až po úspěšném odeslání. Pád mezi tím znamená duplicitu
  v dalším e-mailu, nikdy ztracený inzerát. Za ostrý běh (delší limit stáří
  u prvního běhu hledání) se počítá jen doručený běh.
- **Selhání:** přechodná chyba (síť, časový limit, kód 4xx) se zkusí jednou
  znovu po 60 s; špatné heslo ani odmítnutý příjemce se neopakují. Chyby se
  překládají na radu (heslo pro aplikace, port × zabezpečení, antivir
  kontrolující poštu). Neodeslaný souhrn se uloží do `vystup/` jako
  `…_NEODESLANO.html` a `.eml`, návratový kód 3.
- **Heslo:** jen v `secrets.toml`, nikdy se nevypisuje (ani délka). Doporučené
  je heslo pro aplikace a samostatná odesílací schránka.
- **Kopie ve `vystup/` (`outputs.py`):** každý běh uloží `email_….html` (celý
  souhrn pro prohlížeč) a `email_….eml` (celá zpráva: tělo, text, příloha).
  Zpráva se sestaví i u `--dry-run`, jen se neodešle. Proč `.eml`: tělo se liší
  od celého souhrnu (zkrácené, bez `<details>`) a chyby typu „sekce v poště
  chybí“ jdou ověřit jen na skutečné zprávě. `test-email --nahled` posílá
  poslední `.eml` znovu s předmětem „TEST – …“, novým `Message-ID` (Gmail
  zprávu se známým ID tiše zahodí) a příjemci z aktuálního configu.
- **Úklid:** na začátku každého běhu se smažou vlastní soubory `email_*`
  přímo ve `vystup/` starší než `[obecne] uchovat_vystup_dni` (podle času
  změny; 0 = nemazat). Cizí soubory a podsložky se nemažou nikdy.

## 11. Exe a soubory vedle programu (fáze 6)

- **Dvě místa:** `paths.app_dir()` = složka, kde leží exe (config, DB, logy,
  `vystup/`, upravené šablony; ze zdrojáků kořen projektu). `paths.bundled_dir()`
  = soubory přibalené k programu (`_internal\`): vzor configu vždy podle verze
  programu.
- **První spuštění** (`firstrun.py`): bez `config.toml` vznikne kopie vzoru,
  program česky řekne, co vyplnit, otevře ho v Poznámkovém bloku a skončí
  kódem 2. Existující config se nikdy nepřepíše. `secrets.toml` nevzniká,
  heslo patří do Správce pověření.
- **Dvojklik** (`console.py`): když okno konzole patří jen programu
  (`GetConsoleProcessList` = 1), program před koncem počká na Enter; jinak by
  okno zmizelo dřív, než jde cokoli přečíst. Spolehlivé jen u onedir buildu
  (onefile má dva procesy).
- **Šablony** (`templating.py`): výchozí v balíčku `jobagent/templates/`.
  `sablony` je vyexportuje do `templates/` vedle programu (nepřepíše
  existující); použije se každý soubor, který tam je, jinak výchozí. Upravené
  šablony program nikdy nepřepíše, jen porovná zástupné symboly s výchozí
  verzí: chybějící (šablona ze starší verze) a neznámé (překlep) hlásí v logu
  i v e-mailu. Žádné otisky ani automatické nahrazování: jednoduché pravidlo
  „co je v templates/, je tvoje“.

## 12. Testy a ukázky

Testy běží offline nad uloženými stránkami v `samples/`. Skutečné stránky jsou
anonymizované (`tools/anonymize_samples.py`, viz `samples/README.md`): struktura
HTML zůstává, texty inzerátů a kontakty jsou nahrazené. Test
`test_anonymized_samples` u autora ověřuje, že se kopie parsují stejně jako
originály.

## 13. Další fáze

- **AI hodnocení (fáze 5):** volitelné (Claude Haiku), jen skupina 1, dostává
  očištěný `Job.text`. Cache podle ID inzerátu + hashe hodnoceného textu.
  Opakované hodnocení vyřazených nesmí znovu platit za AI ani stahovat detail.
  Při chybě (klíč, kredit, síť) program pokračuje s bodováním podle slov
  a v e-mailu to uvede.
- **Exe a Plánovač úloh (fáze 6):** PyInstaller, `install-schedule` se
  spuštěním po zmeškaném startu, návod krok za krokem. Kandidát: heslo
  ve Správci pověření Windows (`CredReadW` přes `ctypes`, bez další
  knihovny) místo čistého textu v `secrets.toml`.
- **Filtr vzdálenosti:** přibalený seznam obcí a okresů se souřadnicemi
  (offline). Domov + `max_km`, vzdušná čára. U okresu je vzdálenost rozpětí
  (těžiště ± poloměr): jistě v okruhu, jistě mimo (vyřadit), nejisté (ponechat
  s poznámkou). Poloměr z rozlohy zkresluje protáhlé okresy. Kdyby nejistých
  bylo moc, přesnější je vzdálenost k nejbližší obci okresu. Tabulka obec →
  okres umožní i lepší deduplikaci s webem agentury.
- **Skrytá mzda pro pravidla míst:** průchod pro každou odlišnou částku
  z `[[filtry.mista]]`.
