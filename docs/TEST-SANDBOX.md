# Ověření zipu ve Windows Sandbox

Před každým vydáním: vyzkoušet zip na čistém Windows bez Pythonu, přesně jak
ho dostane kamarád. Windows Sandbox je dočasný čistý Windows v okně; po
zavření z něj nezůstane nic.

## Příprava (jednou)

1. Windows 11 Pro/Enterprise/Education (Home Sandbox nemá).
2. Virtualizace zapnutá v BIOSu: Správce úloh → Výkon → Procesor →
   „Virtualizace: Povoleno“.
3. Start → „Zapnout nebo vypnout funkce systému Windows“ → zaškrtnout
   **Windows Sandbox** → OK → restart.

## Spuštění

`tools\build.py` vytvoří `dist\test-sandbox.wsb`. Dvojklik na něj spustí
Sandbox se složkou `dist` připojenou **jen pro čtení** na ploše
(`Desktop\dist`) a se zapnutou sítí.

Sandbox má obvykle anglické Windows – níže jsou české i anglické názvy.
Anglické Windows (cp1252) zároveň ověří, že program nezávisí na češtině
systému ani nečte lokalizovaný výstup `schtasks`.

Heslo: v Sandboxu je bezpečnější použít zvlášť vytvořené heslo pro aplikace
a po testu ho v účtu Seznam/Google zrušit.

## A. Jako kamarád, který zip neodblokuje

1. Zkopíruj zip z `Desktop\dist` na plochu Sandboxu.
2. **Simulace stažení z internetu.** Soubory z připojené složky nemají značku
   „staženo z internetu“ (Mark of the Web), takže by se SmartScreen
   neukázal. V PowerShellu v Sandboxu:
   ```powershell
   Set-Content "$env:USERPROFILE\Desktop\JobAgent-0.2.0-windows.zip" -Stream Zone.Identifier -Value "[ZoneTransfer]`r`nZoneId=3"
   ```
   Kontrola: zip → Vlastnosti (Properties) má dole políčko Odblokovat (Unblock).
3. Pravým → **Extrahovat vše** (Extract All). Ve vzniklé složce má být
   `NAVOD.txt` a složka `JobAgent`. Uvnitř `JobAgent` nahoře jen: `_internal`,
   `JobAgent.exe`, `JobAgent-planovac.exe`, `NAVOD.txt`, `config.example.toml`.
4. Otevři `NAVOD.txt` dvojklikem – diakritika musí být správně.
5. Dvojklik na `JobAgent.exe` → SmartScreen „Systém Windows ochránil váš
   počítač“ (Windows protected your PC) → Další informace (More info) →
   Přesto spustit (Run anyway).
6. Okno ukáže uvítání, otevře se Poznámkový blok s `config.toml`, okno čeká na
   Enter. Vyplň jedno hledání (url) a `[email]`, ulož.
7. Ve složce `JobAgent`: adresní řádek → `cmd` → Enter. Pak:
   - `JobAgent.exe nastav-heslo` → vlož heslo (Ctrl+V i pravým tlačítkem –
     musí fungovat obojí) → „Přihlášení … OK“, „Heslo uloženo“.
     Ovládací panely → Správce přihlašovacích údajů (Credential Manager) →
     Přihlašovací údaje systému Windows → obecné `JobAgent/…`.
   - `JobAgent.exe test-email` → „heslo: uložené (Správce pověření Windows)“,
     e-mail dorazí.
   - `JobAgent.exe run --dry-run` → otevře se náhled v Edge; ve `vystup` je
     `.html` + `.eml`, v `logs` log.
   - `JobAgent.exe run` → e-mail dorazí, v těle je sekce „Ostatní“.
8. `JobAgent.exe install-schedule 8:00` → `taskschd.msc` → Knihovna (Task
   Scheduler Library) → Job Agent → Vlastnosti:
   - Spustit, jen když je uživatel přihlášen; účet WDAGUtilityAccount;
   - denně 8:00, zpoždění 15 min;
   - akce `"…\JobAgent-planovac.exe"`, argument `run`, Spustit v: složka;
   - Podmínky: obě volby o baterii vypnuté;
   - Nastavení: spustit co nejdříve po zmeškání, limit 2 h.

   Pravým → Spustit (Run): **žádné okno nesmí problikout**, v logu přibude
   běh, Výsledek posledního spuštění (Last Run Result) `0x0`, e-mail dorazí.
   To ověří i to, že exe se značkou z internetu Plánovač spustí bez
   SmartScreen dotazu.
9. Dvojklik na `JobAgent-planovac.exe` → okno se zprávou „Tohle spouští
   Plánovač úloh…“.
10. Chyby, které návod popisuje (kontrola, že se chovají, jak píše):
    - zkopíruj `JobAgent.exe` samotné na plochu a spusť → okno problikne;
    - zkopíruj `JobAgent.exe` ve složce jako `Hledač práce.exe` →
      `"Hledač práce.exe" --help` funguje, dvojklik radí příkazy s tímhle jménem.
11. Pravým na složku `JobAgent` → Prohledat programem Microsoft Defender
    (Scan with Microsoft Defender) → žádné hrozby.
12. `JobAgent.exe uninstall-schedule`, `JobAgent.exe smaz-heslo`.

## B. Jako kamarád, který zip odblokuje

Smaž rozbalenou složku, na zipu zopakuj krok 2 (značka), pak Vlastnosti →
Odblokovat → OK → Extrahovat vše → dvojklik na `JobAgent.exe`: **bez**
SmartScreen dotazu.

## Co Sandbox neověří

- **Zmeškaný start** (Sandbox po zavření zmizí): doma nastav úlohu na čas za
  5 minut, uspi počítač přes ten čas a po probuzení má běh proběhnout zhruba
  do 10 minut.
- **Bez práv správce**: uživatel v Sandboxu je správce. `install-schedule`
  ověřuj doma z obyčejného okna.
- **Smart App Control**: v Sandboxu není zapnutý.
- **České Windows (cp1250)**: Sandbox je anglický (cp1252). Chybějící
  `encoding=` chytí obojí stejně; české texty ve výstupu konzole ověř doma.

Volitelně: zip lze nahrát na virustotal.com (je stejně veřejný na GitHubu)
a podívat se, které antiviry hlásí falešný poplach.
