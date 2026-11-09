"""Fáze 3: collector jobscontact.cz a párování s jobs.cz podle názvu.

Ukázky jobscontact_*.html jsou skutečné stránky z 8. 11. 2026 (uložené
příkazem sample, anonymizované: lokalita hledání Brno, texty inzerátů
a kontakty nahrazeny): hledání IT, okruh 40 km (38 nabídek), stránka 2 a 4 při
stránkování po 11, detail „IT Support pro výrobu“ a detail se dvěma místy.

Páry názvů pocházejí ze srovnání 94 inzerátů Jobs Contact na jobs.cz s 1649
inzeráty na jobscontact.cz (8. 11. 2026).
"""

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from jobagent import collectors, pipeline
from jobagent.collectors.jobscontact_cz import COMPANY, JobsContactCzCollector
from jobagent.config import Search, load_config
from jobagent.dedup import dedup_key, group_duplicates, normalize_title, town
from jobagent.models import Job
from jobagent.storage import Storage

from tests.helpers import SAMPLES, FakeFetcher, sample

ROOT = SAMPLES.parent
TODAY = date(2026, 11, 8)
SEARCH = "https://www.jobscontact.cz/prace/informacni-technologie/brno?jobFilterRadius=40"
IT_SUPPORT = "https://www.jobscontact.cz/prace/it-support-pro-vyrobu-pipe-moderni-automotive-pipe-15-dot-mzda_id12631"
MULTI = "https://www.jobscontact.cz/prace/plc-programator-pipe-pruzna-pracovni-doba-pipe-home-office_id12617"

# Skutečné páry (jobs.cz, jobscontact.cz): stejný inzerát.
SAME_REAL = [
    ("Zkušený projektant | Strojírenství & průmysl | 50-70.000 Kč", "Zkušený projektant | Strojírenství & průmysl"),
    ("Samostatný projektant komunikací a HTÚ pro průmyslové stavby | 50-65.000 Kč", "Samostatný projektant komunikací a HTÚ pro průmyslové stavby"),
    ("Technický obchodní zástupce | Strojírenství | 40-80.000 Kč", "Technický obchodní zástupce | Strojírenství"),
    ("STROJAŘ S AJ | pojďte se k nám posouvat! (až 55.000 Kč)", "STROJAŘ S AJ | pojďte se k nám posouvat!"),
    ("Mistr ve strojírenství | aktivní účast na projektech (50.000-60.000 Kč)", "Mistr ve strojírenství |aktivní účast na projektech"),
    ("IT technik s elektro vyhláškou | Dukovany | 33-45.000 Kč", "IT technik s elektro vyhláškou | Dukovany"),
    ("Projektový manažer | bezpečnostních technologie (60.000 - 80.000 Kč)", "Projektový manažer | bezpečnostních technologie"),
    ("Asistentka do kanceláře – možnost zkráceného úvazku (až 32.000 Kč) #3094", "Asistentka do kanceláře – možnost zkráceného úvazku #3094"),
    ("Senior Inženýr elektrických pohonů NN | Home-office | 80-100.000 Kč", "Senior Inženýr elektrických pohonů NN | Home-office"),
    ("ELEKTRO KONSTRUKTÉR – elektromotory | junior i senior (až 55 000 Kč)", "ELEKTRO KONSTRUKTÉR – elektromotory | junior i senior"),
    ("Elektroprojektant silnoproudu | Vhodné i pro juniory | 40-60.000 Kč", "Elektroprojektant silnoproudu | Vhodné i pro juniory"),
    ("Zkušený TECHNOLOG, co chce vést tým | AJ (od 70.000 Kč)", "Zkušený TECHNOLOG, co chce vést tým | AJ"),
    ("PLC programátor - bez cestování, 13. a 14. plat! (až 80.000 Kč)", "PLC programátor - bez cestování, 13. a 14. plat!"),
    ("Programátor ROBOTŮ - moderní projekt + 13. a 14. plat!", "Programátor ROBOTŮ - moderní projekt + 13. a 14. plat!"),
    ("Servisní technik s přesahem do IT (40-60.000 Kč)", "Servisní technik s přesahem do IT"),
    ("Máte přirozenou autoritu? Hledáme vedoucího elektromontérů! (až 60.000,- + bonusy)", "Máte přirozenou autoritu? Hledáme vedoucího elektromontérů!"),
    ("Senior Software Architect | .NET, Azure (až 120.000,-)", "Senior Software Architect | .NET, Azure"),
    ("Zkušený rozpočtář pozemních staveb | Flexibilita, bonusy a až 90.000,-", "Zkušený rozpočtář pozemních staveb | Flexibilita, bonusy"),
    ("IT Admin / L1-L2 Support | až 45 000 Kč", "IT Admin / L1-L2 Support"),
    ("Mistr slaboproudu – veďte vlastní montážní tým | 65 000 Kč", "Mistr slaboproudu – veďte vlastní montážní tým"),
    ("Industry Engineer | Nové projekty | 13.+14. mzda", "Industry Engineer | Nové projekty |13.+14. mzda"),
    ("Junior elektroprojektant | Absolvent VŠ | Ostrava (45-55 tis. Kč)", "Junior elektroprojektant | Absolvent VŠ | Ostrava"),
    ("Projektant MaR | Full REMOTE | pro juniory i zkušené (až 90.000 Kč)", "Projektant MaR | Full REMOTE | pro juniory i zkušené"),
    ("Inženýr zákaznické kvality | Automotive | až 85 000 Kč", "Inženýr zákaznické kvality | Automotive"),
    ("B2B obchodník se zaměřením na strojírenskou výrobu (mzda až 80.000 Kč)", "B2B obchodník se zaměřením na strojírenskou výrobu"),
    ("Kontrolor kvality ve strojírenství | 1 směna (45.000 Kč)", "Kontrolor kvality ve strojírenství | 1 směna"),
    ("Specialista výrobní logistiky | 13.+14. mzda (až 55.000 Kč + bonusy)", "Specialista výrobní logistiky | 13.+14. mzda"),
    ("Projektant pozemních staveb | Energetika | až 90 000 Kč", "Projektant pozemních staveb | Energetika | až 90 000 Kč"),
    ("Koordinátor/ka | Pravá ruka vedení #3076", "Koordinátor/ka | Pravá ruka vedení #3076"),
]
# Podoby mzdy v názvu, které se na webu agentury objevily, ale skutečný
# protějšek se od jobs.cz liší i jinde. Ověřuje se jen odstranění mzdy.
SALARY_FORMS = [
    ("Plant Director / Ředitel – až 200 tis Kč", "Plant Director / Ředitel"),
    ("PŘÍPRAVÁŘ / ROZPOČTÁŘ STAVEB | AŽ 85000,-", "PŘÍPRAVÁŘ / ROZPOČTÁŘ STAVEB"),
    ("PROJEKTOVÝ VEDOUCÍ | POZEMNÍ STAVBY | 150 000kč", "PROJEKTOVÝ VEDOUCÍ | POZEMNÍ STAVBY"),
    ("HR recruiter do personální agentury + B2B (35.000 - 65.000)", "HR recruiter do personální agentury + B2B"),
    ("🏗️ Stavbyvedoucí dopravních staveb", "Stavbyvedoucí dopravních staveb"),
    ("Zámečník / Svářeč 1 směna (až 280 Kč/hod.) #3099", "Zámečník / Svářeč 1 směna #3099"),
]
# Skoro-shody a falešné páry varianty „uříznout vše za |“: NESMÍ se shodovat.
DIFFERENT = [
    ("Rozpočtář pozemních staveb -HO (52-79.000 Kč)", "Rozpočtář pozemních staveb"),
    ("PLC programátor | Home office | Málo cestování (až 80.000,-)", "PLC programátor | Home office | (až 80.000,-)"),
    ("Konstruktér ocelových konstrukcí, pružná pracovní doba, i ABSOLVENTI (45.000 - 50.000 Kč)", "Konstruktér ocelových konstrukcí, pružná pracovní doba"),
    ("Stavbyvedoucí! I bez autorizace - zde je vaše nová příležitost! (až 90.000,-)", "Stavbyvedoucí bez autorizace - vaše nová příležitost!"),
    ("Hledá se vedoucí stavby! / HPP i IČO / Zajímavé projekty / až 120.000,-", "Hledá se vedoucí stavby! / IČO / Zajímavé projekty"),
    ("Senior Software Architect | .NET, Azure (až 120.000,-)", "Senior Software Architect | .NET, C#, Azure"),
    ("PŘÍPRAVÁŘ / ROZPOČTÁŘ STAVEB | AŽ 85000,-", "Přípravář staveb / Rozpočtář"),
    ("Elektroprojektant | NN/VN | Až 60 000 Kč", "elektroprojektant NN | 2dny HO"),
    ("Elektro projektant | AutoCAD | Stačí praxe z VŠ nebo 1 rok zkušeností z praxe", "Elektro projektant | Měření a regulace | Stačí rok praxe"),
    ("Projektant pozemních staveb | Velké průmyslové stavby | až 50 000 Kč", "Projektant pozemních staveb | Energetika | až 90 000 Kč"),
    ("Stavbyvedoucí pozemních staveb | až 100 000 Kč | HPP | České Budějovice", "Stavbyvedoucí pozemních staveb"),
    ("Stavbyvedoucí dopravních staveb", "Stavbyvedoucí dopravních staveb | Auto + dovolená navíc"),
    ("PLC programátor | Pružná pracovní doba", "PLC programátor | Pružná pracovní doba | Home office"),
    ("Plant Director / Ředitel výrobního závodu – strategické vedení businessu - až 200 tis Kč", "Plant Director / Ředitel výrobního závodu –strategická role"),
]


class ListingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        c = JobsContactCzCollector()
        cls.page = c.parse_listing(sample("jobscontact_vypis_it_40km.html"), SEARCH + "&itemsPerPage=100", TODAY)
        cls.by_id = {j.native_id: j for j in cls.page.jobs}

    def test_all_cards_on_one_page(self):
        self.assertEqual(len(self.page.jobs), 38)
        self.assertEqual(len(self.by_id), 38)
        self.assertIsNone(self.page.next_url, "itemsPerPage=100 → bez stránkování")

    def test_fields(self):
        job = self.by_id["12631"]
        self.assertEqual(job.portal, "jobscontact.cz")
        self.assertEqual(job.title, "IT Support pro výrobu | moderní Automotive | 15. mzda")
        self.assertEqual(job.url, IT_SUPPORT)
        self.assertEqual(job.company, COMPANY)
        self.assertEqual(job.salary_text, "35 000 - 40 000 Kč")
        self.assertEqual(job.location, "Frýdek-Místek")
        self.assertEqual(job.tags, ["Junior", "13. plat"])
        self.assertIsNone(job.posted)

    def test_more_places_from_tooltip(self):
        self.assertEqual(self.by_id["12617"].location, "Nový Jičín, Moravskoslezský kraj")
        self.assertEqual(self.by_id["9407"].location, "Hlavní město Praha, Ostrava-město, Brno-město")
        self.assertEqual(town(self.by_id["12617"].location), "", "víc míst → obec nevíme, neslučovat")

    def test_pagination(self):
        c = JobsContactCzCollector()
        page2 = c.parse_listing(sample("jobscontact_vypis_it_40km_strana2.html"), SEARCH + "&page=2", TODAY)
        self.assertEqual(len(page2.jobs), 11)
        self.assertEqual(page2.next_url, SEARCH + "&do=visualPaginator-showPage&page=3")
        last = c.parse_listing(sample("jobscontact_vypis_it_40km_posledni.html"), SEARCH + "&page=4", TODAY)
        self.assertEqual((len(last.jobs), last.next_url), (5, None))
        # Stránky 2 a 4 jsou podmnožinou výpisu se 100 kartami.
        self.assertLessEqual({j.native_id for j in page2.jobs + last.jobs}, set(self.by_id))

    def test_first_page_url(self):
        c = JobsContactCzCollector()
        self.assertEqual(c.first_page_url(SEARCH), SEARCH + "&itemsPerPage=100")
        self.assertEqual(c.first_page_url(SEARCH + "&itemsPerPage=21"), SEARCH + "&itemsPerPage=21")

    def test_registry_ids_and_no_salary_pass(self):
        c = collectors.for_url(SEARCH)
        self.assertIsInstance(c, JobsContactCzCollector)
        self.assertEqual(c.native_id_from_url(IT_SUPPORT + "#form-detail"), "12631")
        self.assertEqual(c.canonical_url(IT_SUPPORT + "?do=favorite-12631-toggle#x"), IT_SUPPORT)
        self.assertIsNone(c.salary_param, "jobFilterSalary filtruje jen viditelnou mzdu")
        self.assertEqual(collectors.supported_portals()[-1], "jobscontact.cz")

    def test_no_salary_text(self):
        html = sample("jobscontact_vypis_it_40km.html").replace("35 000 - 40 000 Kč", "Neuvedeno", 1)
        job = JobsContactCzCollector().parse_listing(html, SEARCH, TODAY).jobs[0]
        self.assertEqual(job.salary_text, "")


class DetailTests(unittest.TestCase):
    def parse(self, name, title):
        return JobsContactCzCollector().parse_detail(sample(name), TODAY, title)

    def test_it_support(self):
        d = self.parse("jobscontact_detail_it_support.html", "IT Support pro výrobu | moderní Automotive | 15. mzda")
        self.assertEqual(d.method, "selektory")
        self.assertTrue(d.description.startswith("Do mezinárodní firmy z oblasti automotive"))
        for part in ("Náplň práce", "Požadavky", "Nabízíme", "L1 IT podporu"):
            self.assertIn(part, d.description)
        # Šablona agentury, kontakt a cizí nabídky do textu nepatří.
        for junk in ("Doplňující informace", "osobní údaje", "MS WORD", "Další pozice v oboru",
                     "Kdo Vám pomůže", "FOBO"):
            self.assertNotIn(junk, d.description)
        self.assertEqual((d.salary_text, d.location), ("35 000 - 40 000 Kč", "Frýdek-Místek"))

    def test_more_places_joined(self):
        d = self.parse("jobscontact_detail_vice_lokalit.html", "PLC programátor | Pružná pracovní doba | Home office")
        self.assertEqual(d.location, "Nový Jičín, Moravskoslezský kraj")
        self.assertIn("Home office až 2 dny v týdnu", d.description)


class TitleNormalizationTests(unittest.TestCase):
    def test_real_pairs_match(self):
        for jobs_cz, jobscontact in SAME_REAL:
            with self.subTest(jobs_cz):
                self.assertEqual(normalize_title(jobs_cz), normalize_title(jobscontact))

    def test_salary_forms_removed(self):
        for with_salary, without in SALARY_FORMS:
            with self.subTest(with_salary):
                self.assertEqual(normalize_title(with_salary), normalize_title(without))

    def test_near_misses_stay_different(self):
        for a, b in DIFFERENT:
            with self.subTest(a):
                self.assertNotEqual(normalize_title(a), normalize_title(b))

    def test_numbers_that_are_not_salary_stay(self):
        self.assertEqual(normalize_title("Industry Engineer | 13.+14. mzda"), "industry engineer 13 14 mzda")
        self.assertIn("3094", normalize_title("Asistentka #3094"))
        self.assertIn("c net staci 2 roky", normalize_title("IT Konzultant (C#/.NET stačí 2 roky)"))


class TownTests(unittest.TestCase):
    def test_districts(self):
        self.assertEqual(town("Hlavní město Praha"), "praha")
        self.assertEqual(town("Ostrava-město"), "ostrava")
        self.assertEqual(town("Brno-město"), "brno")
        self.assertEqual(town("Plzeň-město"), "plzen")
        self.assertEqual(town("Frýdek-Místek"), town("Frýdek-Místek"))
        for district in ("Brno-venkov", "Praha-východ", "Praha-západ"):
            self.assertEqual(town(district), "", district)

    def test_dedup_key_with_fixed_company(self):
        self.assertEqual(dedup_key("Junior elektroprojektant | Absolvent VŠ | Ostrava", COMPANY, "Ostrava-město"),
                         dedup_key("Junior elektroprojektant | Absolvent VŠ | Ostrava (45-55 tis. Kč)",
                                   "Jobs Contact Personal, s.r.o.", "Ostrava"))


def job(portal, native_id, title, location, company=COMPANY):
    return Job(portal=portal, native_id=native_id, url=f"https://{portal}/{native_id}", title=title,
               company=company, location=location)


class GroupingTests(unittest.TestCase):
    def groups(self, jobs):
        return group_duplicates(jobs, collectors.priority)

    def test_real_pair_merged_jobs_cz_wins(self):
        jc = job("jobscontact.cz", "12337", "Junior elektroprojektant | Absolvent VŠ | Ostrava", "Ostrava-město")
        jz = job("jobs.cz", "2001496108", "Junior elektroprojektant | Absolvent VŠ | Ostrava (45-55 tis. Kč)", "Ostrava")
        groups = self.groups([jc, jz])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][0].portal, "jobs.cz")

    def test_district_differs_from_town_not_merged(self):
        # jobs.cz „Hranice“, jobscontact.cz okres „Přerov“: nejisté → neslučovat.
        jc = job("jobscontact.cz", "13044", "Technolog (Strojírenství) | Šance i bez praxe v oboru", "Přerov")
        jz = job("jobs.cz", "2001487282", "Technolog (Strojírenství) | Šance i bez praxe v oboru | 48-55.000 Kč",
                 "Hranice")
        self.assertEqual(len(self.groups([jc, jz])), 2)

    def test_other_company_not_merged(self):
        jc = job("jobscontact.cz", "1", "C# vývojář", "Ostrava-město")
        jz = job("jobs.cz", "2", "C# vývojář", "Ostrava", company="Jiná firma s.r.o.")
        self.assertEqual(len(self.groups([jc, jz])), 2)

    def test_brno_venkov_is_not_brno(self):
        jc = job("jobscontact.cz", "1", "Stavbyvedoucí", "Brno-venkov")
        jz = job("jobs.cz", "2", "Stavbyvedoucí", "Brno")
        self.assertEqual(len(self.groups([jc, jz])), 2)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.example.toml")
        self.cfg.searches = [Search(name="it jobscontact", url=SEARCH)]
        self.cfg.general.detail_limit = 50
        self.cfg.filters.min_salary = 50000
        self.storage = Storage(":memory:")
        self.addCleanup(self.storage.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_once(self):
        support = sample("jobscontact_detail_it_support.html")
        web = FakeFetcher({
            SEARCH + "&itemsPerPage=100": sample("jobscontact_vypis_it_40km.html"),
            MULTI: sample("jobscontact_detail_vice_lokalit.html"),
            "https://www.jobscontact.cz/prace/": support,  # ostatní detaily: stačí stejná šablona
        })
        report = pipeline.run(self.cfg, self.storage, web, dry_run=True, now=datetime(2026, 11, 8, 10),
                              unknown_samples_dir=Path(self.tmp.name))
        return web, report

    def test_one_listing_request_no_second_pass(self):
        web, report = self.run_once()
        listing = [u for u in web.requested if "/informacni-technologie/" in u]
        self.assertEqual(listing, [SEARCH + "&itemsPerPage=100"])
        self.assertEqual(report.stats[0].found, 38)
        self.assertIsNone(report.balance_error())
        self.assertFalse([w for w in report.warnings if "jobscontact" in w or "se mzdou" in w], report.warnings)

    def test_details_and_cleaning(self):
        _, report = self.run_once()
        shown = report.new_jobs + report.title_jobs + report.no_text_other
        self.assertTrue(shown)
        for j in shown:
            self.assertNotIn("osobní údaje", j.text)
            self.assertIsNone(j.hidden_salary)
        multi = [j for j in shown + report.rejected if j.native_id == "12617"][0]
        self.assertIn("Home office až 2 dny v týdnu", multi.text)
        # Viditelná mzda pod minimem (IT Support 35–40 tis.) vyřadí náš filtr.
        support = [j for j in report.rejected if j.native_id == "12631"]
        self.assertTrue(support and "pod minimem" in support[0].reject_reason)


if __name__ == "__main__":
    unittest.main()
