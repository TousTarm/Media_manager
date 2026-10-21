# Media Manager

Třídění Jellyfin knihovny podle jazyka audio stop do `sorted/{cz,en,jp}` (hardlinky).
Automatický sorter běží uvnitř služby (každých `SORTER_INTERVAL` s) a appka k němu
přidává ruční opravy, které sorter **nepřepíše**.

Mobile-first: spodní navigační lišta, bottom sheet místo modálů, dotykové cíle
≥ 44 px, mazání na podržení místo náhodného tapu. Na desktopu se totéž rozloží
do horních tabů a bočního panelu.

## Taby

| Tab | Co v něm je |
| --- | --- |
| **Auto** | Fronta na kontrolu: všechno, co ještě nemá ruční rozhodnutí. Přepínače jazyka jsou přímo v řádku, **Potvrdit** uzamkne stav a položka ze seznamu zmizí. |
| **Movies** | Mřížka filmů. Složka s jedním videem = jedna dlaždice; klasifikace stejně míří na soubor uvnitř. |
| **Series** | Mřížka seriálů. Dlaždice = kořenová složka show, dovnitř se dá nahlédnout, ale jazyk se nastavuje za celou show. |
| **Anime** | Totéž co Series. |
| **Sorter** | Poslední běh, co se změnilo, tlačítko *Spustit teď*. |

Nahoře je hledání přes všechny tři složky naráz. Názvy se v UI zobrazují zkrácené
(bez `[skupiny]`, kvality a kodeku); plný název je v tooltipu a v detailu u cesty.

**Další sezóna.** Když je sezóna 2 stažená zvlášť, Jellyfin ji bere jako jinou show.
V jejím detailu je *Patří k jiné show…* — klikneš na show, ke které patří (nejpodobnější
názvy jsou nahoře), a složka se do ní přesune jako `Season N` (číslo z názvu, jinak
další volné). Cílová složka si nechá název, takže ji Jellyfin dál pozná; pokud ještě
sezóny neměla, její epizody se přesunou do `Season 1`. Nic se nekopíruje, hardlinky,
ruční nastavení i cache ffprobe se přenesou.

Jazyky jsou tři: **CZ**, **EN**, **JP** (`sorted/cz`, `sorted/en`, `sorted/jp`).
Libovolná kombinace, včetně žádné — „nikam“ je platné rozhodnutí, ne absence
rozhodnutí.

## Nasazení (LXC 102, nativně)

Bez Dockeru — LXC nemá povolený nesting, takže vše jede přes systémový
Python, venv a systemd.

```bash
apt install python3-venv ffmpeg          # ffprobe je povinný
cd /opt && git clone <repo-url> media-manager && cd media-manager
python3 -m venv venv
venv/bin/pip install -r requirements.txt

cp .env.example .env
$EDITOR .env                              # APP_PASSWORD a JELLYFIN_API_KEY

cp media-manager.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now media-manager
systemctl status media-manager
```

Pak `http://<server-ip>:8181`, přihlášení formulářem (`APP_USER` + `APP_PASSWORD`),
session cookie platí 30 dní. Změna hesla odhlásí všechna zařízení.

Po startu appka projede prostředí a co nesedí, napíše do logu
(`journalctl -u media-manager`). Kompletní obraz je na **`/debug`** — odkaz je
i vpravo nahoře v hlavičce. Kontroluje se právo zápisu do knihovny, jestli
`sorted/` leží na stejném filesystému jako `Movies/` (jinak hardlinky skončí
na EXDEV), jestli v cronu nezůstal starý `sorter.py`, dostupnost ffprobe a Jellyfinu.
Testy zápisu vytvoří dočasný soubor a hned ho smažou.

## Konfigurace

Všechno se čte z `.env` v kořeni projektu; proměnné prostředí mají přednost.
V kódu není žádná natvrdo zadaná cesta ani heslo — vzor je `.env.example`,
`.env` samotný je v `.gitignore` a na cílovém stroji se vytváří ručně.

| Klíč | Výchozí | K čemu |
| --- | --- | --- |
| `MEDIA_ROOT` | `/media/jellyfin` | kořen knihovny |
| `JELLYFIN_URL` | `http://127.0.0.1:8096` | základ pro `/Library/Refresh` |
| `JELLYFIN_API_KEY` | — | prázdné = refresh se přeskočí, zbytek funguje |
| `JELLYFIN_TIMEOUT` | `10` | s |
| `APP_USER` / `APP_PASSWORD` | `admin` / — | login stránka; bez hesla appka odmítne vše |
| `SORTER_INTERVAL` | `60` | s mezi běhy sorteru, `0` = vypnuto |
| `APP_HOST` / `APP_PORT` | `0.0.0.0` / `8181` | kde poslouchat |
| `LOG_LEVEL` | `INFO` | |
| `FFPROBE_PATH` / `FFPROBE_TIMEOUT` | `ffprobe` / `90` | |

Systemd unit spouští `python -m app.run`, ne `uvicorn` s parametry — jinak by
`APP_HOST`, `APP_PORT` a `LOG_LEVEL` v `.env` byly jen dekorace.

## Aktualizace na serveru

```bash
git archive HEAD | ssh root@<proxmox-host> "pct exec 102 -- tar -x -C /opt/media-manager" \
  && ssh root@<proxmox-host> "pct exec 102 -- systemctl restart media-manager"
```

`.env` a `venv/` nejsou v repu, takže je to nepřepíše. Smazané soubory na
serveru zůstanou ležet — nevadí, appka je nenačítá. Když se změní
`requirements.txt`, ještě `venv/bin/pip install -r requirements.txt`.

## Sorter

`app/sorter.py`, náhrada za původní cron skript `/media/jellyfin/sorter.py`.

- Ruční override vyhrává, ffprobe se pro něj nespouští.
- Film/seriál: CZ stopa → CZ, EN stopa → EN (obojí = obě knihovny), nic → EN.
  Co někdo ručně vložil do `sorted/cz` a CZ stopu nemá, zůstane jen v CZ.
- Anime: JP stopa → JP, EN stopa nebo nic → EN.
- U seriálů rozhoduje každá složka s videi zvlášť podle svého největšího souboru.
- Soubor změněný před < 2 min se ještě stahuje: linky se nechají být, ffprobe počká.
- Co v `sorted/{lang}/{Kategorie}` nikam nepatří, smaže. Když chybí zdrojová
  složka (odpojený mount), její větev neuklízí.
- Do logu píše jen změny. Měření ffprobem běží mimo zámek, linkování a úklid
  pod zámkem sdíleným s ručními zásahy z UI.

Samotest: `venv/bin/python -m tests.test_sorter` (běží na dočasné knihovně).

## Jak to uvnitř funguje

**Zdroj pravdy je filesystem, ne cache.** Badge `CZ`/`EN` u položky se počítá
porovnáním inode mezi souborem ve zdroji a jeho protějškem v `sorted/**`, ne
z `langs` v `sorter_db.json` — to je jen výsledek ffprobe měření a o skutečném
umístění hardlinků nevypovídá.

**Klíčování override.** U filmu je klíčem přesná cesta k souboru, u seriálu a
anime složka: kořen show (platí pro celou show), nebo podsložka — sezóna. Uvnitř
show se na sezónu ťukne a nastaví se jí vlastní jazyk. Sorter bere nejbližší
nastavení od složky epizod nahoru ke kořeni show. Uložení jazyka pro celou show
nastavení sezón zruší. Při „Patří k jiné show“ se ruční nastavení přesouvané
složky stane nastavením její nové sezóny.

**Auto tab a co znamená „potvrdit“.** Seznam drží položky bez override, tedy
to, co pořád řídí sorter. Potvrzení zapíše aktuální (nebo tebou upravený) stav do
override store — od té chvíle je rozhodnutí tvoje a sorter na něj nesahá. Proto
položka ze seznamu zmizí: fronta se tím vyprazdňuje.

**Prázdné `targets` není totéž co žádný override.** `[]` znamená „nikam
nedávat“ a je to platné rozhodnutí, které sorter respektuje. Zrušit ruční režim
jde tlačítkem *Zpět na auto*, které záznam ze store smaže.

**Mazání hledá hardlinky napřed odhadem.** Nejdřív se zkusí zrcadlová umístění
v `sorted/**` a porovná se jejich počet s `st_nlink`. Když sedí, jsou známa
všechna jména souboru a walk knihovnou odpadá; když ne (link někdo přesunul),
projde se `search_roots` jedním průchodem pro všechny soubory najednou — ne
jedním walkem na soubor, což by u stočlenného seriálu trvalo věčnost.
Spec-kompatibilní `find_all_hardlinks()` v `app/library.py` zůstává k dispozici.

**Jellyfin se volá fire-and-forget.** `POST /Library/Refresh` se odpálí na
pozadí; když je Jellyfin dole, zaloguje se to a operace mazání ani override tím
nespadne.

## API

Vedle HTML je k dispozici JSON API (stejná session cookie, bez ní 401):

| Endpoint | Co dělá |
| --- | --- |
| `GET /api/browse?type=movies\|series\|anime&path=` | výpis složky se stavem jazyků a override |
| `GET /api/auto` | fronta na kontrolu (položky bez override) |
| `GET /api/item/detail?path=` | ffprobe jazyky, skutečné umístění, override |
| `POST /api/item/reanalyze` | spustí ffprobe, přepíše `sorter_db.json` |
| `POST /api/item/set-targets` | zapíše override, přelinkuje, refreshne Jellyfin |
| `POST /api/item/confirm` | uzamkne aktuální stav jako ruční rozhodnutí (linky nemění) |
| `POST /api/item/delete` | dry-run — vrátí seznam ke smazání |
| `POST /api/item/delete/confirm` | skutečné smazání (bere `token` z dry-runu) |
| `GET /api/overrides` | celý override store |
| `GET /api/debug` | výsledek kontroly prostředí |
| `GET /api/sorter` | poslední běh sorteru a události |
| `GET /healthz` | jediná route bez auth; vrací pouze `{"status":"ok"}` |

Mazání je záměrně na dva requesty: frontend nejdřív ukáže seznam, teprve pak
potvrzuje. Dry-run vrací `token` — otisk seznamu cest. Confirm ho posílá zpět,
server si plán přepočítá (cestám z prohlížeče se nevěří) a když se knihovna
mezitím změnila, mazání odmítne. Jinak by rozdělení na dva kroky nezaručovalo,
že se maže právě to, co uživatel viděl.

## Struktura

```
app/
  main.py       FastAPI routy — HTML partials i JSON API
  service.py    aplikační vrstva (detail, reanalyze, set-targets, delete)
  library.py    filesystem: browse, inode match, hardlinky, mazání
  overrides.py  manual_overrides.json
  probe.py      ffprobe + sorter_db.json
  jellyfin.py   fire-and-forget refresh
  jsonstore.py  atomické čtení/zápis JSON sdílených se sorterem
  auth.py       login formulář + podepsaná session cookie
  sorter.py     automatické třídění (běží na pozadí ve službě)
  diagnostics.py kontrola prostředí pro /debug a start
  run.py        spouštěč, aby APP_HOST/APP_PORT/LOG_LEVEL platily
  config.py     .env -> Settings
tests/test_sorter.py  samotest sorteru a přihlášení
```
