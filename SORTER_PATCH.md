# Úprava `sorter.py` — manual override store

`/media/jellyfin/sorter.py` není součástí tohoto repa a nebyl k dispozici při
psaní kódu, takže tady je přesný návod na **minimální diff**, ne hotový patch
soubor. Nic jiného v `sorter.py` neměň.

## 1. Nasazení helperu

```bash
cp sorter_overrides.py /media/jellyfin/sorter_overrides.py
```

Modul je stdlib-only, cron skript kvůli němu nepotřebuje venv aplikace.

## 2. Import (nahoru k ostatním importům)

```python
from sorter_overrides import load_overrides, override_key, lookup
```

Pokud cron pouští sorter z jiného pracovního adresáře, přidej před import:

```python
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
```

## 3. Načtení na začátku `process()`

```python
def process():
    overrides = load_overrides(MEDIA_ROOT)   # <- MEDIA_ROOT = "/media/jellyfin"
    ...
```

Jednou za běh, ne v cyklu — cron běží každých 15 minut a nemá smysl číst ten
samý soubor u každé položky.

## 4. Větev před ffprobe

V iteraci přes `WATCH_LIST`, **těsně před** tím, než se pro položku spočítá
`has_cz` / `has_en` z ffprobe. `path` je cesta k souboru u `type == "movie"`,
resp. k sample souboru u folder-scanu:

```python
key = override_key(item, path)
targets = lookup(overrides, key)

if targets is not None:
    # Override je explicitní rozhodnutí uživatele — ffprobe se přeskočí úplně.
    final_cz = "cz" in targets
    final_en = "en" in targets
    log(f"Override aktivní: {os.path.basename(key)} -> {targets or ['nikam']}")
else:
    langs = get_audio_languages_smart(...)      # původní kód beze změny
    has_cz, has_en = ...                        # původní kód beze změny
    final_cz = has_cz or is_manual_cz           # původní kód beze změny
    final_en = has_en or ...                    # původní kód beze změny
```

Dál pokračuje pipeline (`create_hardlink`, `cleanup_orphans`,
`valid_sorted_paths`) **beze změny** — override mění jen vstup do rozhodování,
ne mechaniku vytváření a mazání linků.

## 5. Japonština v `sorted/jp/`

Aplikace umí položku poslat i do `sorted/jp/…` (anime s anglickým dabingem,
kde má zůstat i japonský originál). Sorter sám tuhle větev nikdy nevytvoří —
ale musí ji nechat být:

- V override větvi ber všechny tři cíle, ne jen dva:
  `final_cz/final_en` doplň o `final_jp = "jp" in targets` a stejným způsobem
  jím řiď `create_hardlink` do `sorted/jp/…`.
- **Zkontroluj `cleanup_orphans()` a `valid_sorted_paths()`.** Pokud procházejí
  celý `sorted/` a mažou, co nezná, smetou `sorted/jp/**` při každém běhu.
  Buď do jejich seznamu platných cest přidej i jp větev, nebo `sorted/jp`
  z úklidu úplně vynech. Tohle je jediné místo, kde jp umí nadělat škodu.

## 6. Na co si dát pozor

- **`targets is not None`, ne `if targets:`.** Prázdný seznam znamená „nikam
  nedávat“ a je to platné rozhodnutí; pravdivostní test by ho spolkl a položka
  by spadla zpátky pod automatiku.
- **Granularita klíče.** U series/anime klíčuje `override_key()` na kořen show,
  ne na sample soubor — `show_root()` k tomu potřebuje správně vyplněné
  `item["source"]` (tj. `/media/jellyfin/Series`, ne cestu k sezóně).
- **`is_manual_cz` neruš.** Stávající `check_manual_override()` detekuje fyzickou
  přítomnost souboru v CZ složce a je to jiná věc; v override větvi se prostě
  neuplatní, mimo ni funguje dál.
- **Cache `sorter_db.json`.** Pro položku s override se ffprobe nespouští, takže
  se ani nepřepíše její záznam v cache. To je záměr.
- **Prázdná složka v `sorted/**` se počítá jako „není tam“.** Appka pozná
  zařazení show podle toho, že zrcadlová složka existuje a něco obsahuje.
  Kdyby sorter nechával po úklidu prázdné adresáře, nevadí to; kdyby v nich
  nechával cizí soubory, appka je bude považovat za zařazení.

## 7. Ověření (akceptační kritérium ze specifikace)

```bash
# 1. V appce nastav u nějakého filmu cíl na CZ a ulož.
# 2. Zkontroluj zápis:
cat /media/jellyfin/manual_overrides.json

# 3. Vynuť běh sorteru mimo cron:
python3 /media/jellyfin/sorter.py

# 4. V logu musí být "Override aktivní" místo nového ffprobe měření:
grep "Override aktivní" /media/jellyfin/sorter_new_items.log

# 5. Hardlinky zůstaly tak, jak je nastavila appka:
ls -la /media/jellyfin/sorted/cz/Movies/
```
