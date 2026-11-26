#!/usr/bin/env python3
"""
Descarga el bulk data oficial de Scryfall y las etiquetas de arte de Scryfall Tagger,
lo comprime en dist/ y genera un manifest.json con fechas, recuentos y hashes
para poder verificar la integridad y el origen de cada consulta.
"""
import gzip
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist"
TMP = ROOT / "tmp"
REPO = os.environ.get("GITHUB_REPOSITORY", "local")
UA = f"mtg-data-sync/1.0 (+https://github.com/{REPO})"
HEADERS = {"User-Agent": UA, "Accept": "application/json"}

# default_cards: cada impresión (en inglés si existe) -> base de casi todas las consultas
# oracle_cards: una entrada por carta (texto oracle)
# unique_artwork: una entrada por ilustración distinta
# rulings: notas de reglas oficiales
BULK_TYPES = ["default_cards", "oracle_cards", "unique_artwork", "rulings"]
if os.environ.get("INCLUDE_ALL_CARDS") == "1":
    BULK_TYPES.append("all_cards")  # todas las impresiones en todos los idiomas (muy pesado)

MIN_DEFAULT_CARDS = 50_000  # control de cordura: si baja de aquí, algo ha ido mal


def get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=900) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, length=1 << 20)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def gzip_file(src, dst):
    with open(src, "rb") as fi, gzip.open(dst, "wb", compresslevel=9) as fo:
        shutil.copyfileobj(fi, fo, length=1 << 20)


def write_gz_json(obj, dst):
    with gzip.open(dst, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(obj, f, ensure_ascii=False)


def find_download_url(b):
    """Localiza la URL de descarga del bulk aunque Scryfall cambie el nombre del campo."""
    for key in ("download_uri", "download_url", "file_uri", "file_url"):
        if isinstance(b.get(key), str) and b[key].startswith("http"):
            return b[key]
    candidates = [v for v in b.values()
                  if isinstance(v, str) and v.startswith("https://")
                  and (".json" in v or "data.scryfall.io" in v)]
    if len(candidates) == 1:
        return candidates[0]
    sys.exit("ERROR: no se encuentra la URL de descarga en el índice de Scryfall. "
             "Objeto recibido:\n" + json.dumps(b, ensure_ascii=False, indent=2))


def ensure_plain_json(path):
    """Si Scryfall sirve el archivo ya comprimido en gzip, lo descomprime en el sitio."""
    with open(path, "rb") as f:
        magic = f.read(2)
    if magic == b"\x1f\x8b":
        tmp = path.with_suffix(".tmp")
        with gzip.open(path, "rb") as fi, open(tmp, "wb") as fo:
            shutil.copyfileobj(fi, fo, length=1 << 20)
        tmp.replace(path)


def fetch_bulk():
    index = get_json("https://api.scryfall.com/bulk-data")
    by_type = {b["type"]: b for b in index["data"]}
    entries = []
    for t in BULK_TYPES:
        if t not in by_type:
            sys.exit(f"ERROR: Scryfall no publica el tipo de bulk '{t}'")
        b = by_type[t]
        raw = TMP / f"{t}.json"
        size = b.get("size") or b.get("compressed_size") or b.get("content_length")
        size_txt = f"{int(size) / 1e6:.0f} MB" if size else "tamaño desconocido"
        print(f"Descargando {t} ({size_txt}, actualizado {b.get('updated_at')})...")
        print(f"  Campos del índice: {sorted(b.keys())}")
        url = find_download_url(b)
        download(url, raw)
        ensure_plain_json(raw)
        raw_hash = sha256(raw)
        count = None
        if t != "all_cards":  # all_cards es demasiado grande para cargarlo solo para contar
            with open(raw, encoding="utf-8") as f:
                count = len(json.load(f))
        if t == "default_cards" and count < MIN_DEFAULT_CARDS:
            sys.exit(f"ERROR: default_cards solo tiene {count} objetos; abortando")
        gz = OUT / f"{t}.json.gz"
        gzip_file(raw, gz)
        raw.unlink()
        entries.append({
            "type": t,
            "scryfall_updated_at": b.get("updated_at"),
            "download_uri": url,
            "file": gz.name,
            "object_count": count,
            "sha256_uncompressed": raw_hash,
            "size_bytes_gz": gz.stat().st_size,
        })
        time.sleep(1)
    return entries


def tagger_search(tag):
    query = f"art:{tag}"
    params = {"q": query, "unique": "prints",
              "include_extras": "true", "include_variations": "true"}
    url = "https://api.scryfall.com/cards/search?" + urllib.parse.urlencode(params)
    cards, total = [], 0
    while url:
        time.sleep(0.6)  # respetar los límites de la API de Scryfall
        try:
            page = get_json(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:  # la búsqueda no devuelve cartas
                return query, 0, []
            raise
        total = page.get("total_cards", total)
        for c in page["data"]:
            cards.append({
                "id": c["id"],
                "oracle_id": c.get("oracle_id"),
                "illustration_id": c.get("illustration_id"),
                "name": c["name"],
                "set": c["set"],
                "collector_number": c["collector_number"],
            })
        url = page.get("next_page") if page.get("has_more") else None
    return query, total, cards


def fetch_tagger():
    tags_file = ROOT / "art_tags.txt"
    tags = [l.strip() for l in tags_file.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")]
    entries = []
    for tag in tags:
        print(f"Consultando Tagger: art:{tag}...")
        query, total, cards = tagger_search(tag)
        if len(cards) != total:
            sys.exit(f"ERROR: art:{tag} declara {total} cartas pero se descargaron {len(cards)}")
        gz = OUT / f"tagger_art_{tag}.json.gz"
        write_gz_json({"query": query, "total_cards": total, "cards": cards}, gz)
        entries.append({"tag": tag, "query": query, "total_cards": total, "file": gz.name})
    return entries


MTGJSON_BASE = "https://mtgjson.com/api/v5"
MIN_MTGJSON_CARDS = 50_000  # control de cordura para AllPrintings


def fetch_mtgjson():
    """Descarga AllPrintings de MTGJSON (segunda fuente) y verifica su SHA-256 oficial."""
    meta = get_json(f"{MTGJSON_BASE}/Meta.json")["data"]
    gz = OUT / "mtgjson_AllPrintings.json.gz"
    print(f"Descargando MTGJSON AllPrintings (versión {meta.get('version')}, fecha {meta.get('date')})...")
    download(f"{MTGJSON_BASE}/AllPrintings.json.gz", gz)

    req = urllib.request.Request(f"{MTGJSON_BASE}/AllPrintings.json.gz.sha256",
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        expected = r.read().decode().split()[0].strip().lower()
    actual = sha256(gz)
    if actual != expected:
        sys.exit(f"ERROR: el SHA-256 de AllPrintings no coincide ({actual} != {expected})")

    with gzip.open(gz, "rt", encoding="utf-8") as f:
        data = json.load(f)["data"]
    set_count = len(data)
    card_count = sum(len(s.get("cards", [])) for s in data.values())
    token_count = sum(len(s.get("tokens", [])) for s in data.values())
    del data
    if card_count < MIN_MTGJSON_CARDS:
        sys.exit(f"ERROR: AllPrintings solo tiene {card_count} cartas; abortando")
    return {
        "file": gz.name,
        "mtgjson_version": meta.get("version"),
        "mtgjson_date": meta.get("date"),
        "sha256_gz_verified": actual,
        "set_count": set_count,
        "card_count": card_count,
        "token_count": token_count,
        "size_bytes_gz": gz.stat().st_size,
    }


def main():
    for d in (OUT, TMP):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "Scryfall bulk data + Scryfall Tagger (principal); MTGJSON AllPrintings (contraste)",
        "bulk": fetch_bulk(),
        "tagger_art": fetch_tagger(),
        "mtgjson": fetch_mtgjson(),
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.rmtree(TMP, ignore_errors=True)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
