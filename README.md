# Media Manager

Ruční korekce jazykové klasifikace Jellyfin knihovny. `sorter.py` (cron, každých
15 min) rozhoduje automaticky podle ffprobe; tahle appka je nástroj pro případy,
kdy se splete — a hlavně zajistí, že další běh cronu ruční rozhodnutí
**nepřepíše zpátky**.

Mobile-first: spodní navigační lišta, bottom sheet místo modálů, dotykové cíle
≥ 44 px, mazání na podržení místo náhodného tapu. Na desktopu se totéž rozloží
do horních tabů a bočního panelu.

## Čtyři taby

| Tab | Co v něm je |
| --- | --- |
| **Auto** | Fronta na kontrolu: všechno, co ještě nemá ruční rozhodnutí. Přepínače jazyka jsou přímo v řádku, **Potvrdit** uzamkne stav a položka ze seznamu zmizí. |
| **Movies** | Mřížka filmů. Složka s jedním videem = jedna dlaždice; klasifikace stejně míří na soubor uvnitř. |
| **Series** | Mřížka seriálů. Dlaždice = kořenová složka show, dovnitř se dá nahlédnout, ale jazyk se nastavuje za celou show. |
| **Anime** | Totéž co Series. |

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

Pak `http://<server-ip>:8181`, přihlášení `APP_USER` + `APP_PASSWORD`.

Po startu appka projede prostředí a co nesedí, napíše do logu
(`journalctl -u media-manager`). Kompletní obraz je na **`/debug`** — odkaz je
i vpravo nahoře v hlavičce. Kontroluje se právo zápisu do knihovny, jestli
`sorted/` leží na stejném filesystému jako `Movies/` (jinak hardlinky skončí
na EXDEV), jestli je `sorter.py` opatchovaný, dostupnost ffprobe a Jellyfinu.
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
| `APP_USER` / `APP_PASSWORD` | `admin` / — | HTTP Basic; bez hesla appka odmítne vše |
| `APP_HOST` / `APP_PORT` | `0.0.0.0` / `8181` | kde poslouchat |
| `LOG_LEVEL` | `INFO` | |
| `FFPROBE_PATH` / `FFPROBE_TIMEOUT` | `ffprobe` / `90` | |

Systemd unit spouští `python -m app.run`, ne `uvicorn` s parametry — jinak by
`APP_HOST`, `APP_PORT` a `LOG_LEVEL` v `.env` byly jen dekorace.

## Nutný krok: úprava `sorter.py`

Bez ní appka funguje, ale cron ruční rozhodnutí za 15 minut přepíše zpátky.
Postup je v [SORTER_PATCH.md](SORTER_PATCH.md) — nakopírovat
`sorter_overrides.py` vedle `sorter.py` a přidat do něj tři kusy kódu.

## Jak to uvnitř funguje

**Zdroj pravdy je filesystem, ne cache.** Badge `CZ`/`EN` u položky se počítá
porovnáním inode mezi souborem ve zdroji a jeho protějškem v `sorted/**`, ne
z `langs` v `sorter_db.json` — to je jen výsledek ffprobe měření a o skutečném
umístění hardlinků nevypovídá.

**Klíčování override.** U filmu je klíčem přesná cesta k souboru, u seriálu a
anime kořenová složka show. Tapneš-li v appce na epizodu, detail i override se
stejně vztáhnou na celou show; sorter si stejný klíč odvodí ze své sample cesty
přes `sorter_overrides.show_root()`.

**Auto tab a co znamená „potvrdit“.** Seznam drží položky bez override, tedy
to, co pořád řídí cron. Potvrzení zapíše aktuální (nebo tebou upravený) stav do
override store — od té chvíle je rozhodnutí tvoje a cron ho nesahá. Proto
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

Vedle HTML je k dispozici JSON API (stejné Basic Auth):

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
  auth.py       HTTP Basic
  diagnostics.py kontrola prostředí pro /debug a start
  run.py        spouštěč, aby APP_HOST/APP_PORT/LOG_LEVEL platily
  config.py     .env -> Settings
sorter_overrides.py   helper k nakopírování vedle sorter.py (stdlib only)
```
