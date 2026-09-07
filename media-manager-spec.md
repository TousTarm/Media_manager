# Media Manager — specifikace pro implementaci

## 0. Kontext prostředí (přečti si první, určuje spoustu rozhodnutí)

- Aplikace poběží **nativně** (žádný Docker) uvnitř Debian 13 LXC kontejneru na Proxmoxu, kde už běží Jellyfin (nativní apt instalace). LXC nemá povolený nesting, takže Docker není k dispozici — vše musí jít přes systémový Python/venv + systemd.
- Datový kořen: `/media/jellyfin` (bind mount ze ZFS datasetu `tank/jellyfin` z Proxmox hosta).
- Existující struktura uvnitř `/media/jellyfin`:
  ```
  Movies/          <- kanonický zdroj, ploché i vnořené složky s videosoubory
  Series/          <- kanonický zdroj, každý seriál = složka, uvnitř sezóny/epizody
  Anime/           <- kanonický zdroj, stejná logika jako Series
  sorted/cz/Movies <- hardlinky na filmy s CZ/SK audio stopou
  sorted/cz/Series
  sorted/en/Movies <- hardlinky na filmy s EN audio (nebo fallback, když CZ chybí)
  sorted/en/Series
  sorted/en/Anime
  sorter.py            <- existující cron skript, BĚŽÍ DÁL, viz sekce 1
  sorter_db.json       <- cache {cesta: {mtime, langs, name}}, ffprobe výsledky
  sorter_cron.log
  complete/        <- dokončená stahování (zatím prázdné/nepoužité pro sorting)
  incomplete/      <- rozpracovaná stahování — NIKDY se tohoto adresáře nedotýkat
  ```
- Soubory v `sorted/**` jsou **hardlinky** (`os.link`), ne kopie — sdílejí inode se souborem v `Movies/Series/Anime`. Stejná fyzická data mohou mít 2–3 jména na disku najednou.
- Jellyfin běží na `localhost:8096` v tomtéž kontejneru, takže z aplikace je dostupný na `http://127.0.0.1:8096`.
- Cílové zařízení pro ovládání: **z 90 % mobilní telefon**. Desktop je vedlejší use case. Navrhuj UI mobile-first, ne responsive-jako-dodatek.

## 1. Vztah k existujícímu `sorter.py` — DŮLEŽITÉ, nepřepisovat od nuly

`sorter.py` je cron job (`*/15 * * * *`), který běží nezávisle na této aplikaci a bude běžet dál. Řeší automatickou klasifikaci: projede `Movies/Series/Anime`, pro každou položku spustí `ffprobe`, zjistí jazyky audio stop, a podle nich vytvoří/smaže hardlinky v `sorted/cz` a `sorted/en`. Detaily rozhodovací logiky (has_cz/has_en, anime vždy do EN, atd.) jsou v souboru samotném — přečti si ho celý na disku (`/media/jellyfin/sorter.py`) předtím, než začneš.

**Tato aplikace je nástroj pro ruční opravu, když automatická klasifikace selže** (špatně rozpoznaný jazyk, `UND` audio stopa, chybějící metadata). Musí to fungovat tak, aby cron příští run **nepřepsal** ruční rozhodnutí uživatele zpátky.

### Požadovaná úprava `sorter.py`

Přidej nový mechanismus **manual override store**, nezávislý na stávajícím `check_manual_override()` (to je jiná věc — detekuje fyzickou přítomnost souboru v CZ složce, ne explicitní rozhodnutí). Nový soubor:

```
/media/jellyfin/manual_overrides.json
```

Formát:
```json
{
  "/media/jellyfin/Movies/Nejaky Film (2024)/film.mkv": {
    "targets": ["cz"],
    "set_at": "2026-09-05T10:00:00Z"
  },
  "/media/jellyfin/Series/Nejaky Serial": {
    "targets": ["en"],
    "set_at": "2026-09-05T10:05:00Z"
  }
}
```

- Klíč = absolutní cesta. Pro **filmy** = přesná cesta k souboru (stejná granularita jako `get_audio_languages_smart` pro movie typ). Pro **series/anime** = cesta ke **kořenové složce seriálu/série** (ne k jednotlivé sezóně, ne k sample souboru) — to je hrubší granularita než sorter interně používá pro sample file, takže v `sorter.py` je nutné při iteraci `WATCH_LIST` položek typu series/anime zjistit "root" složku dané show (typicky `os.path.dirname(root)` až na úroveň bezprostředně pod `item["source"]`) a tou klíčovat lookup do override store, ne cestou k sample souboru.
- `targets` je pole s libovolnou kombinací `"cz"`, `"en"`. Prázdné pole `[]` znamená "nikam nedávat" (smaž všechny linky, i kdyby audio řeklo jinak).

**Uprav `process()` v `sorter.py`:** na začátku běhu načti `manual_overrides.json` (pokud neexistuje, prázdný dict). Před tím, než se pro danou položku (soubor nebo sample_path u folder-scan) spočítá `has_cz`/`has_en` z ffprobe, zkontroluj, jestli existuje override pro odpovídající klíč (přesná cesta souboru pro movie, root složka pro series/anime). Pokud ano:
- **Přeskoč volání ffprobe úplně** pro tuto položku (šetří čas, override je explicitní rozhodnutí uživatele, nemá smysl to znovu měřit).
- `final_cz = "cz" in targets`, `final_en = "en" in targets"`, nahraď tím původní `has_cz`/`has_en`/`is_manual_cz` logiku pro tuto konkrétní položku — zbytek pipeline (`create_hardlink`, `cleanup_orphans`, `valid_sorted_paths`) zůstává beze změny, override jen mění vstup do rozhodování, ne mechaniku vytváření/mazání linků.
- Do logu napiš zřetelně `"Override aktivní: {jméno} -> {targets}"`, ať je to v `sorter_new_items.log` dohledatelné.

Tuto úpravu proveď jako **minimální diff** do existujícího `sorter.py` — needituj nesouvisející části, needěl refactoring okolního kódu.

## 2. Tech stack

- **Backend:** Python 3, **FastAPI** + `uvicorn`. Důvod: stejný jazyk jako `sorter.py` (lze sdílet pomocné funkce, např. import `run_ffprobe`), lehké nasazení bez build kroku, dobré API pro mobilní frontend.
- **Frontend:** Server-rendered šablony (**Jinja2**) + **HTMX** pro interaktivitu (žádný Node build pipeline, žádný SPA framework — jednodušší nasazení a údržba pro jednoho člověka). Styling: **Tailwind přes CDN** (`<script src="https://cdn.tailwindcss.com">`) nebo prostý vlastní CSS soubor — vyber co půjde rychleji sestavit čistě, ale musí to vypadat jako aplikace z roku 2026, ne jako výchozí Bootstrap formulář. Mobile-first: velké dotykové cíle (min. 44px výška tlačítek), spodní navigační lišta na mobilu, žádné hover-only interakce.
- **Perzistence:** žádná databáze navíc. Zdroj pravdy je filesystem + `sorter_db.json` (read-only pro tuto appku, jen sorter do něj zapisuje) + nový `manual_overrides.json` (tuto appka zapisuje i čte).
- **Žádný Docker.** Instalace: `python3 -m venv venv`, `pip install`, systemd unit.

## 3. Datový model / co appka čte a zapisuje

### Čte (read-only)
- Filesystem strom `Movies/`, `Series/`, `Anime/`, `sorted/cz/*`, `sorted/en/*`.
- `sorter_db.json` — pro zobrazení už zjištěného jazyka bez nutnosti znovu spouštět ffprobe při každém načtení stránky.
- `sorter_new_items.log` — volitelně, pro zobrazení posledních automatických rozhodnutí (nice-to-have, ne nutnost pro v1).

### Zapisuje
- `manual_overrides.json` — při ruční změně jazyka z UI.
- Filesystem: mazání souborů/hardlinků (sekce 5).
- Volá Jellyfin API po každé změně (sekce 6).

## 4. Browsing & klasifikace UI — hlavní obrazovka

### Navigace
Tři karty/taby nahoře (nebo bottom nav na mobilu): **Movies**, **Series**, **Anime**. V každé kartě file browser:
- Filmy: plochý/vnořený seznam souborů a složek — zobrazuj jak jednotlivé video soubory (pokud leží přímo v `Movies/`), tak podsložky (typicky `Nazev Filmu (Rok)/soubor.mkv`). Tap na složku = vstup dovnitř, tap na video soubor = detail panel.
- Series/Anime: seznam složek na top úrovni (= jednotlivé seriály/anime). Tap = detail panel **pro celou show** (klasifikace je na úrovni show root, viz sekce 1), s možností dál rozkliknout vnitřní strukturu (sezóny/epizody) jen pro náhled, ne pro nastavení jazyka zvlášť u epizody.

### Zobrazované informace u každé položky v seznamu
- Název (odvozený z názvu souboru/složky).
- Badge s aktuálním stavem: `CZ`, `EN`, `CZ+EN`, nebo `❓ Nezařazeno` (odvoď z toho, jestli existuje hardlink v `sorted/cz/...` a/nebo `sorted/en/...` odpovídající danému zdroji — **nespoléhej jen na `sorter_db.json` langs pole**, to je jen ffprobe výsledek, ne skutečný stav hardlinků; skutečný stav zjisti přímo dotazem na filesystem přes inode match, viz sekce 5.1).
- Pokud existuje manuální override pro danou položku, zobraz malou ikonku/label `✋ ruční` vedle badge.

### Detail panel (po tapnutí na položku)
- Název, cesta ke zdrojovému souboru.
- Zjištěné jazyky z ffprobe (z cache, nebo tlačítko "Přeanalyzovat" které spustí ffprobe on-demand a přepíše záznam v `sorter_db.json` stejným formátem, jaký používá `sorter.py`, aby si to cron příště nepřepsal zbytečně znovu).
- Aktuální umístění (do kterých `sorted/*` složek skutečně ukazuje hardlink — zjištěno přes 5.1, ne přes cache).
- **Volič cílového jazyka:** multi-select/toggle buttons `CZ` / `EN` (obě mohou být aktivní zároveň, nebo žádná). Tlačítko **Uložit**.
  - Po uložení: zapiš do `manual_overrides.json` (klíč podle pravidel v sekci 1), a **rovnou proveď fyzickou změnu hardlinků** (nečekej na příští cron běh) — appka sama vytvoří/smaže potřebné hardlinky podle nového `targets` pole, stejnou logikou jako `create_hardlink`/cleanup v `sorter.py` (viz 5.2, sdílej kód spíš než duplikuj).
  - Po fyzické změně zavolej Jellyfin API refresh (sekce 6).
- Tlačítko **Smazat** (červené, s potvrzovacím dialogem — na mobilu ne `confirm()` browser alert, udělej vlastní modal, ať to nejde omylem proklikat). Zobraz v potvrzení počet nalezených hardlinků, co se smaže (`"Smaže se 3 soubory (originál + 2 linky)"`).

## 5. Mazání — algoritmus (kritická část, udělej pečlivě)

### 5.1 Nalezení všech hardlinků k dané položce

Pro daný zdrojový soubor (přesná cesta v `Movies/Series/Anime`):

```python
import os

def find_all_hardlinks(source_path: str, search_roots: list[str]) -> list[str]:
    """Najde všechny cesty na stejném zařízení se stejným inode jako source_path."""
    st = os.stat(source_path)
    target_dev, target_ino = st.st_dev, st.st_ino
    matches = []
    for root_dir in search_roots:
        for dirpath, _, filenames in os.walk(root_dir):
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                try:
                    s = os.stat(full)
                except OSError:
                    continue
                if s.st_dev == target_dev and s.st_ino == target_ino:
                    matches.append(full)
    return matches
```

`search_roots` = `["/media/jellyfin/Movies", "/media/jellyfin/Series", "/media/jellyfin/Anime", "/media/jellyfin/sorted"]`. Toto je O(n) walk přes celou knihovnu při každém mazání — pro knihovnu v řádu tisíců souborů je to v pořádku (běží to jen na explicitní akci uživatele, ne na každý page load). Pokud se v budoucnu ukáže jako pomalé, lze optimalizovat přes `find <root> -inum <ino> -xdev` subprocess místo Python walk, ale pro v1 stačí čistě pythonové řešení.

Pro **series/anime** (kde se maže celá show, ne jeden soubor): projdi všechny video soubory uvnitř show root složky ve zdroji, pro každý najdi hardlinky výše popsaným způsobem, sesbírej dohromady.

### 5.2 Vlastní mazání
1. Zavolej `find_all_hardlinks`.
2. Ukaž seznam v potvrzovacím modalu.
3. Po potvrzení: `os.remove()` na každé nalezené cestě (včetně originálu v `Movies/Series/Anime` — to uživatel explicitně chce, toto je "smaž film", ne "smaž jen link").
4. Odstraň prázdné rodičovské složky po smazání, pokud zůstaly prázdné (`os.rmdir` v cyklu směrem nahoru, ignoruj `OSError` když složka není prázdná nebo je to kořenová `Movies/`).
5. Odstraň odpovídající záznam z `sorter_db.json` (pokud tam je, podle klíče = cesta k souboru).
6. Odstraň odpovídající záznam z `manual_overrides.json`, pokud existoval.
7. Zavolej Jellyfin API refresh (sekce 6).

### 5.3 Změna cíle (CZ/EN toggle) bez mazání originálu
Když uživatel jen mění, kam má co ukazovat (ne úplné smazání):
- Pro cíle, které mají být **odebrané** (byly aktivní, teď ne): smaž jen hardlink v příslušné `sorted/{cz,en}/...` složce (ne originál ve zdroji).
- Pro cíle, které mají být **přidané**: vytvoř nový hardlink (`os.link(zdroj, cíl)`, `os.makedirs(exist_ok=True)` na cestu k cíli předtím) — cestu k cíli odvoď stejně jako `create_hardlink()` v `sorter.py` (`os.path.relpath` vůči zdrojovému rootu, `os.path.join` s cílovým rootem).

## 6. Jellyfin API integrace

- Vytvoř API klíč v Jellyfin: Dashboard → API Keys → Add. Ulož do `.env` appky jako `JELLYFIN_API_KEY`.
- Base URL: `http://127.0.0.1:8096` (appka běží ve stejném kontejneru).
- Po každé změně (delete, override save) zavolej:
  ```
  POST http://127.0.0.1:8096/Library/Refresh
  Header: X-Emby-Token: {JELLYFIN_API_KEY}
  ```
  Toto spustí scan všech knihoven. Je to plošné, ne cílené na jednu knihovnu — v v1 to stačí, případná optimalizace na cílený refresh konkrétní knihovny (`/Items/{Id}/Refresh`) je nice-to-have pro pozdější verzi, vyžadovalo by mapování cest na Jellyfin library/item ID přes `/Items` query, což zatím neřeš.
- Volání ať je **asynchronní/fire-and-forget** vůči uživatelské odpovědi — appka nemá čekat na dokončení Jellyfin scanu, jen odpálit request a rovnou vrátit uživateli potvrzení akce. Loguj chybu, pokud request selže (Jellyfin dole apod.), ale nesmí to shodit samotnou operaci mazání/override.

## 7. Autentizace

Jedno sdílené heslo z `.env` (`APP_PASSWORD`). Implementuj přes **HTTP Basic Auth** (FastAPI `HTTPBasic` security dependency na všech routách) — nejjednodušší na implementaci, mobilní prohlížeče (Safari i Chrome) to zvládají nativně včetně zapamatování v rámci relace/keychainu. Uživatelské jméno může být cokoliv statického (např. `admin`), ověřuje se jen heslo přes `secrets.compare_digest`, ať to není timing-attackable (kosmeticky správně, ne že by to tu reálně hrozilo).

## 8. Konfigurace — `.env`

```
MEDIA_ROOT=/media/jellyfin
JELLYFIN_URL=http://127.0.0.1:8096
JELLYFIN_API_KEY=<vyplnit po vytvoření v Jellyfin Dashboard>
APP_PASSWORD=<vyplnit>
APP_PORT=8181
```

Appka čte přes `python-dotenv` nebo `pydantic-settings`.

## 9. API endpointy (návrh, uprav dle potřeby při implementaci)

```
GET  /api/browse?type=movies|series|anime&path=<relativní cesta>
     -> seznam položek (soubory + složky) s badge stavem (cz/en/none) a info, jestli má override

GET  /api/item/detail?path=<absolutní cesta ke zdroji>
     -> detail: ffprobe jazyky (z cache), skutečné umístění hardlinků, override stav

POST /api/item/reanalyze
     body: {path}
     -> spustí ffprobe znovu, přepíše sorter_db.json, vrátí nové jazyky

POST /api/item/set-targets
     body: {path, targets: ["cz","en"]}
     -> zapíše override, provede fyzickou změnu hardlinků (5.3), zavolá Jellyfin refresh

POST /api/item/delete
     body: {path}
     -> najde všechny hardlinky (5.1), vrátí seznam ke potvrzení (dry-run mode)

POST /api/item/delete/confirm
     body: {path}
     -> provede skutečné smazání (5.2), zavolá Jellyfin refresh
```

Rozdělení `delete` na dry-run + confirm dvěma requesty je záměrné — frontend nejdřív zavolá dry-run, ukáže modal se seznamem, teprve po potvrzení pošle confirm. Zabraňuje to nechtěnému smazání jedním tapem.

## 10. Nasazení

Cílová cesta: `/opt/media-manager` v LXC 102 (Jellyfin kontejner).

```bash
cd /opt
git clone <repo-url> media-manager
cd media-manager
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# ruční úprava .env — API klíč, heslo
```

Systemd unit `/etc/systemd/system/media-manager.service`:
```ini
[Unit]
Description=Media Manager
After=network.target jellyfin.service

[Service]
Type=simple
WorkingDirectory=/opt/media-manager
ExecStart=/opt/media-manager/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8181
Restart=on-failure
User=root

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now media-manager
```

Přístup: `http://<server-ip>:8181` (LAN/Tailscale). Veřejné vystavení přes Cloudflare Tunnel neřeš v v1 — toto je interní nástroj.

## 11. Akceptační kritéria pro v1

- [ ] Otevřu appku na mobilu, přihlásím se heslem.
- [ ] Vidím seznam Movies/Series/Anime, u každé položky badge (CZ/EN/obojí/nezařazeno).
- [ ] Tapnu na film, vidím detekovaný jazyk, změním cíl na CZ, uložím — hardlink se reálně vytvoří/smaže na disku, Jellyfin dostane refresh signál.
- [ ] Smažu film — potvrzovací dialog ukáže přesný počet a seznam mazaných cest (originál + linky), po potvrzení všechny reálně zmizí z disku.
- [ ] Cron `sorter.py` po další automatické periodě **nepřepíše** ručně nastavenou položku zpátky (ověř: nastav override, počkej 15+ min, zkontroluj že se stav nezměnil a v logu je vidět `"Override aktivní"` hláška místo nového ffprobe měření).
- [ ] Aplikace přežije restart LXC (systemd `enable`).

## 12. Co NEŘEŠIT ve v1 (explicitně mimo scope)

- Žádné stahování — to řeší samostatný nástroj.
- Žádné úpravy metadat filmů/herců (postery, TMDb match) — to zůstává na Jellyfinu samotném.
- Žádná multi-user auth s rolemi — jedno sdílené heslo stačí.
- Žádný build pipeline pro frontend (webpack/vite/atd.) — server-rendered šablony.
- Žádná databáze (Postgres/SQLite) navíc k existujícím JSON souborům.
