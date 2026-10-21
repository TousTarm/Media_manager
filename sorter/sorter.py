import os
import subprocess
import json
import time
import logging
import shutil

from sorter_overrides import load_overrides, override_key, lookup

# --- KONFIGURACE ---
BASE_DIR = "/media/jellyfin"
CACHE_FILE = os.path.join(BASE_DIR, "sorter_db.json")
LOG_FILE = os.path.join(BASE_DIR, "sorter_new_items.log")
SORTED_ROOT = "/media/jellyfin/sorted"
MEDIA_ROOT = BASE_DIR

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)

WATCH_LIST = [
    {
        "name": "Movies",
        "type": "movie",
        "source": "/media/jellyfin/Movies",
        "dest_cz": "/media/jellyfin/sorted/cz/Movies",
        "dest_en": "/media/jellyfin/sorted/en/Movies"
    },
    {
        "name": "Series",
        "type": "series",
        "source": "/media/jellyfin/Series",
        "dest_cz": "/media/jellyfin/sorted/cz/Series",
        "dest_en": "/media/jellyfin/sorted/en/Series"
    },
    {
        "name": "Anime",
        "type": "anime",
        "source": "/media/jellyfin/Anime",
        "dest_cz": None,
        "dest_en": "/media/jellyfin/sorted/en/Anime"
    }
]

VIDEO_EXTENSIONS = ('.mkv', '.mp4', '.avi', '.mov', '.wmv')

stats = {
    "created": 0,
    "skipped": 0,
    "deleted": 0,
    "errors": 0,
    "overrides": 0
}
valid_sorted_paths = set()

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_cache(cache_data):
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Nelze uložit cache: {e}")

def get_audio_languages_smart(filepath, cache_data, item_name_for_log, is_folder_scan=False):
    try:
        mtime = os.path.getmtime(filepath)
    except OSError:
        return []

    if filepath in cache_data:
        entry = cache_data[filepath]
        if entry.get('mtime') == mtime:
            return entry.get('langs', [])

    langs = run_ffprobe(filepath)

    log_name = item_name_for_log if is_folder_scan else os.path.basename(filepath)
    formatted_langs = ", ".join(langs).upper() if langs else "UND (Unknown)"
    logging.info(f"Nové/Změněné: [{log_name}] -> Jazyky: {formatted_langs}")

    cache_data[filepath] = {
        'mtime': mtime,
        'langs': langs,
        'name': log_name
    }
    return langs

def run_ffprobe(filepath):
    try:
        cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-select_streams", "a", filepath
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        data = json.loads(result.stdout)

        languages = []
        for stream in data.get("streams", []):
            tags = stream.get("tags", {})
            lang = tags.get("language", "und")
            if lang not in languages:
                languages.append(lang.lower())
        return languages
    except Exception:
        return []

def create_hardlink(src_file, root_source, dest_root):
    if dest_root is None: return

    rel_path = os.path.relpath(src_file, root_source)
    full_dest_path = os.path.join(dest_root, rel_path)

    valid_sorted_paths.add(full_dest_path)

    if os.path.exists(full_dest_path):
        stats["skipped"] += 1
        return

    os.makedirs(os.path.dirname(full_dest_path), exist_ok=True)
    try:
        os.link(src_file, full_dest_path)
        stats["created"] += 1
    except OSError as e:
        stats["errors"] += 1
        logging.error(f"Chyba linkování {src_file}: {e}")

def check_manual_override(src_file, root_source, dest_cz):
    if dest_cz is None:
        return False

    rel_path = os.path.relpath(src_file, root_source)
    full_dest_path_cz = os.path.join(dest_cz, rel_path)

    if os.path.exists(full_dest_path_cz):
        valid_sorted_paths.add(full_dest_path_cz)
        return True

    return False

def cleanup_orphans():
    logging.info("--- ČIŠTĚNÍ NEAKTIVNÍCH LINKŮ ---")
    if not os.path.exists(SORTED_ROOT): return

    for root, dirs, files in os.walk(SORTED_ROOT):
        for file in files:
            full_path = os.path.join(root, file)
            if full_path not in valid_sorted_paths:
                try:
                    os.remove(full_path)
                    logging.info(f"Odstraněn mrtvý link (nebo přesunuto do CZ): {file}")
                    stats["deleted"] += 1
                except OSError as e:
                    logging.error(f"Nelze smazat {file}: {e}")

        for d in dirs:
            dir_path = os.path.join(root, d)
            try:
                os.rmdir(dir_path)
            except OSError:
                pass

def process():
    logging.info("==========================================")
    logging.info("      SPUŠTĚNÍ TŘÍDÍCÍHO SKRIPTU          ")
    logging.info("==========================================")

    cache = load_cache()
    overrides = load_overrides(MEDIA_ROOT)

    for item in WATCH_LIST:
        section_name = item['name'].upper()

        src = item["source"]
        if not os.path.exists(src): continue

        for root, dirs, files in os.walk(src):
            video_files = [f for f in files if f.lower().endswith(VIDEO_EXTENSIONS)]
            if not video_files: continue

            if item['type'] in ['series', 'anime']:
                sample_file = video_files[0]
                sample_path = os.path.join(root, sample_file)
                folder_name = os.path.basename(root)
                parent_name = os.path.basename(os.path.dirname(root))
                display_name = f"{parent_name}/{folder_name}" if item['type'] == 'series' else folder_name

                dest_cz = item["dest_cz"]
                dest_en = item["dest_en"]
                dest_jp = os.path.join(SORTED_ROOT, "jp", item["name"])

                key = override_key(item, sample_path)
                targets = lookup(overrides, key)

                if targets is not None:
                    final_cz = "cz" in targets
                    final_en = "en" in targets
                    final_jp = "jp" in targets
                    logging.info(f"Override aktivní: {display_name} -> {targets or ['nikam']}")
                else:
                    langs = get_audio_languages_smart(sample_path, cache, display_name, is_folder_scan=True)
                    has_cz = any(l in langs for l in ['cze', 'ces', 'slo', 'slk'])
                    has_en = 'eng' in langs
                    has_jpn = 'jpn' in langs

                    is_manual_cz = False
                    if dest_cz:
                        is_manual_cz = check_manual_override(sample_path, src, dest_cz)
                        if is_manual_cz:
                            stats["overrides"] += 1

                    final_cz = False
                    final_en = False
                    final_jp = False

                    if item['type'] == 'anime':
                        # Anime: JP audio -> JP knihovna. EN audio (dabing/dual-audio)
                        # -> EN navic. Kdyz chybi obe (UND), spadne do EN jako fallback,
                        # at polozka nezmizi z knihovny uplne.
                        final_jp = has_jpn
                        final_en = has_en or not has_jpn
                    else:
                        if is_manual_cz or has_cz:
                            final_cz = True

                        if not is_manual_cz:
                            if has_en or not has_cz:
                                final_en = True

                for file in video_files:
                    full_src_path = os.path.join(root, file)
                    if targets is None and dest_cz and check_manual_override(full_src_path, src, dest_cz):
                        pass

                    if final_cz and dest_cz: create_hardlink(full_src_path, src, dest_cz)
                    if final_en and dest_en: create_hardlink(full_src_path, src, dest_en)
                    if final_jp: create_hardlink(full_src_path, src, dest_jp)

            elif item['type'] == 'movie':
                for file in video_files:
                    full_src_path = os.path.join(root, file)

                    dest_cz = item["dest_cz"]
                    dest_en = item["dest_en"]
                    dest_jp = os.path.join(SORTED_ROOT, "jp", item["name"])

                    key = override_key(item, full_src_path)
                    targets = lookup(overrides, key)

                    if targets is not None:
                        final_cz = "cz" in targets
                        final_en = "en" in targets
                        final_jp = "jp" in targets
                        logging.info(f"Override aktivní: {file} -> {targets or ['nikam']}")
                    else:
                        langs = get_audio_languages_smart(full_src_path, cache, file, is_folder_scan=False)

                        has_cz = any(l in langs for l in ['cze', 'ces', 'slo', 'slk'])
                        has_en = 'eng' in langs

                        is_manual_cz = False
                        if dest_cz:
                            is_manual_cz = check_manual_override(full_src_path, src, dest_cz)
                            if is_manual_cz:
                                stats["overrides"] += 1

                        final_jp = False
                        final_cz = bool((has_cz or is_manual_cz) and dest_cz)

                        final_en = False
                        if not is_manual_cz:
                            if has_en or (not has_cz):
                                final_en = True

                    if final_cz and dest_cz: create_hardlink(full_src_path, src, dest_cz)
                    if final_en and dest_en: create_hardlink(full_src_path, src, dest_en)
                    if final_jp: create_hardlink(full_src_path, src, dest_jp)

    cleanup_orphans()
    save_cache(cache)

    logging.info("------------------------------------------")
    logging.info(f"STATS: Vytvořeno: {stats['created']} | Smazáno: {stats['deleted']} | Overrides (CZ): {stats['overrides']}")
    logging.info("------------------------------------------")

if __name__ == "__main__":
    process()
