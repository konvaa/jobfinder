Job Agent – návod krok za krokem
================================

Job Agent každý den projde nabídky práce na jobs.cz, prace.cz a jobscontact.cz
podle hledání, která si nastavíš, a pošle ti e-mailem přehled toho, co je nové.
Nabídky seřadí podle slov, na kterých ti záleží, a vyřadí ty, které nechceš
(třeba směnný provoz nebo nízkou mzdu).

Program běží jen na tvém počítači. Čte veřejné stránky s nabídkami (stejně jako
prohlížeč, jen pomalu a ohleduplně) a posílá e-mail jen tobě. Nic nikam jinam
neodesílá.

Návod je dlouhý, ale každý krok je krátký. Stačí jít popořadě.


1. ROZBALENÍ – NEJDŘÍV TOHLE
----------------------------

Stáhl jsi soubor JobAgent-<verze>-windows.zip.

1. Klikni na zip PRAVÝM tlačítkem → Vlastnosti. Úplně dole na kartě Obecné
   je-li políčko „Odblokovat“, zaškrtni ho a dej OK.
   (Windows si pamatuje, že soubor přišel z internetu, a při spuštění by
   varoval. Odblokováním mu řekneš, že souboru věříš.)

2. Klikni na zip pravým tlačítkem → Extrahovat vše → Extrahovat.

3. Vznikne složka. Uvnitř je tenhle návod a složka JobAgent.
   Složku JobAgent přesuň, kam chceš mít program natrvalo – třeba do
   Dokumentů. VŽDY CELOU SLOŽKU.

POZOR – dvě časté chyby:

- Nespouštěj program přímo ze zipu (dvojklikem uvnitř zipu, bez rozbalení).
- Nevytahuj JobAgent.exe ze složky JobAgent (třeba na plochu).

V obou případech okno jen problikne a nic se nestane. Program potřebuje
všechno, co je s ním ve složce, hlavně složku _internal.


2. ZÁSTUPCE NA PLOCHU
---------------------

Chceš-li program spouštět z plochy, dej si tam ZÁSTUPCE, ne samotný program:

- Windows 11: klikni pravým tlačítkem na JobAgent.exe → Zobrazit další
  možnosti → Odeslat → Plocha (vytvořit zástupce).
- Windows 10: klikni pravým tlačítkem na JobAgent.exe → Odeslat → Plocha
  (vytvořit zástupce).

Zástupce si můžeš přejmenovat, jak chceš (třeba „Hledač práce“).

Ve složce JobAgent nic nepřejmenovávej ani nemaž. Hlavně ne
JobAgent-planovac.exe – ten spouští Windows každý den automaticky a podle
jména ho hledá.


3. PRVNÍ SPUŠTĚNÍ A VAROVÁNÍ WINDOWS
------------------------------------

Dvakrát klikni na JobAgent.exe (nebo na zástupce).

Pokud jsi zip v kroku 1 neodblokoval, ukáže se modré okno
„Systém Windows ochránil váš počítač“. Program není podepsaný drahým
certifikátem, proto ho Windows nezná. Klikni na „Další informace“ a pak na
„Přesto spustit“. Ukáže se jen poprvé.

Na některých počítačích s Windows 11 je zapnutá funkce Smart App Control
(najdeš ji v Zabezpečení Windows → Řízení aplikací a prohlížeče). Ta může
program zablokovat úplně, bez možnosti „Přesto spustit“. Pak se ozvi – tu
funkci jde vypnout, ale má to své nevýhody a je dobré to probrat.

Při prvním spuštění program vytvoří soubor config.toml (tvoje nastavení)
a otevře ho v Poznámkovém bloku. Okno programu počká, než stiskneš Enter.


4. NASTAVENÍ (config.toml)
--------------------------

V Poznámkovém bloku máš otevřený config.toml. Řádky začínající # jsou
vysvětlivky, ty program nečte. Vyplň dvě věci:

HLEDÁNÍ – co a kde hledat. Každé hledání vypadá takhle:

    [[hledani]]
    nazev = "IT v okolí Brna"
    url = "https://www.jobs.cz/prace/brno/?q%5B%5D=it&locality%5Bradius%5D=50"

Jak získat svou adresu (url):

1. V prohlížeči otevři jobs.cz, prace.cz nebo jobscontact.cz.
2. Nastav, co hledáš: obor nebo slovo, místo, vzdálenost.
3. Až vidíš seznam nabídek, které chceš, zkopíruj adresu z horního řádku
   prohlížeče (klikni do něj, Ctrl+A, Ctrl+C).
4. Vlož ji do config.toml mezi uvozovky za url = (Ctrl+V).

Ukázková hledání smaž nebo přepiš. Hledání může být víc, každé začíná
řádkem [[hledani]].

E-MAIL – v části [email] přepiš na těchto třech řádcích tvuj.email@seznam.cz
na svou adresu:

    uzivatel = "tvuj.email@seznam.cz"      ← adresa, ze které se posílá
    odesilatel = "tvuj.email@seznam.cz"    ← obvykle stejná
    prijemci = ["tvuj.email@seznam.cz"]    ← kam přehled chodí

Máš-li Gmail, změň ještě smtp_server = "smtp.gmail.com".

Soubor ulož (Ctrl+S) a Poznámkový blok zavři.

Pokud by program psal, že soubor „není uložený v kódování UTF-8“: v Poznámkovém
bloku dej Soubor → Uložit jako → dole Kódování: UTF-8 → Uložit.

Zbytek nastavení (filtry, mzda, slova a body) můžeš upravit kdykoli později,
u každé položky je vysvětlivka.


5. PŘÍKAZOVÝ ŘÁDEK
------------------

Další kroky se píšou do příkazového řádku. Otevřeš ho takhle:

1. Otevři složku JobAgent (tu, kde je JobAgent.exe).
2. Klikni nahoře do adresního řádku Průzkumníka (kde je cesta ke složce).
3. Napiš  cmd  a stiskni Enter.

Otevře se černé okno, které už „stojí“ ve složce programu. Příkazy z návodu
do něj opisuj a potvrzuj Enterem.


6. HESLO K E-MAILU
------------------

Program potřebuje heslo, aby mohl e-mail odeslat. NEPIŠ ho do config.toml.

Seznam: pokud máš zapnuté dvoufázové ověření (doporučujeme), vytvoř si
„heslo pro aplikace“: na seznam.cz se přihlas → Účet Seznam → Zabezpečení
účtu → Dvoufázové ověření → Heslo pro aplikace. Bez dvoufázového ověření
použij běžné heslo.

Gmail: vždy je potřeba „heslo aplikace“: Účet Google → Zabezpečení →
Dvoufázové ověření (musí být zapnuté) → Hesla aplikací.

V příkazovém řádku napiš:

    JobAgent.exe nastav-heslo

Program se zeptá na heslo. Při psaní ani vkládání (Ctrl+V nebo pravým
tlačítkem myši) heslo NENÍ VIDĚT – to je v pořádku. Potvrď Enterem.

Program se zkusí přihlásit (nic neodešle) a když to projde, uloží heslo
zašifrované ve Windows (Správce přihlašovacích údajů). Pokud napíše, že se
přihlásit nepovedlo, zkontroluj heslo a adresu v config.toml a zkus to znovu.


7. VYZKOUŠENÍ
-------------

    JobAgent.exe test-email

Přijde krátký zkušební e-mail. Nepřišel? Podívej se do spamu a označ ho jako
„není spam“.

    JobAgent.exe run --dry-run

Program projde hledání a ukáže v prohlížeči, co by ti poslal. Nic neodešle.
První běh trvá i několik minut – program se ptá portálů pomalu, aby je
nezatěžoval.


8. KAŽDODENNÍ BĚH
-----------------

    JobAgent.exe install-schedule

Windows bude program spouštět každý den v 7:30 (s náhodným zpožděním do
15 minut). Jiný čas zadáš takhle:

    JobAgent.exe install-schedule 18:05

Co je dobré vědět:

- Program běží, jen když jsi přihlášený do Windows.
- Je-li počítač v tu dobu vypnutý, proběhne zhruba do 10 minut po zapnutí.
- Běží na pozadí, žádné okno se neukáže. Výsledek přijde e-mailem.
- Přesuneš-li složku JobAgent jinam, spusť install-schedule znovu
  (program tě na to v e-mailu upozorní).

Zrušení každodenního běhu:

    JobAgent.exe uninstall-schedule


9. KDYŽ NĚCO NEJDE
------------------

- Nepřišel e-mail: podívej se do spamu. Pak otevři ve složce JobAgent
  soubor logs\jobagent.log (Poznámkovým blokem) – na konci je, co se dělo.
- Každý přehled se ukládá i do složky vystup (soubor .html se otevře
  v prohlížeči). Starší než 30 dní program sám maže.
- Okno po spuštění jen problikne: program je vytažený ze své složky nebo
  spuštěný ze zipu (viz krok 1).
- Chceš upravit vzhled e-mailu: příkaz  JobAgent.exe sablony  vytvoří
  složku templates se šablonami k úpravě. Pro pokročilé.
- Všechny příkazy:  JobAgent.exe --help


10. NOVÁ VERZE
--------------

Rozbal nový zip a obsah jeho složky JobAgent zkopíruj přes svou starou
složku JobAgent (Windows se zeptá na nahrazení souborů – Nahradit). Tvoje
nastavení, heslo, historie nabídek i upravené šablony zůstanou – v zipu
nejsou, takže je nic nepřepíše.


11. ODINSTALACE
---------------

V příkazovém řádku ve složce JobAgent:

    JobAgent.exe uninstall-schedule
    JobAgent.exe smaz-heslo

Pak celou složku JobAgent smaž.
