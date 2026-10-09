"""
Trendyol CPAS hesaplarında en iyi performans gösteren creative'leri bulur ve
statik bir panel olarak `_site/` altına yazar.

Ana performans panelinden (meta-ads-sheets-sync) bağımsızdır; Google Sheet
kullanmaz, doğrudan Meta Graph API'den okuyup HTML üretir.

Kurallar:
- Katalog reklamları hariç: creative'in object_story_spec'inde `template_data`
  varsa bu, katalogdan otomatik üretilen dinamik carousel şablonudur ve elenir.
  CPAS'ta her reklam atıf için pazaryeri kataloğuna bağlı olduğundan
  "katalog dışı" burada "elle yüklenmiş video/görsel" anlamına gelir.
- Aynı video/görsel birden çok reklamda kullanılıyorsa reklam değil creative
  (video_id / image_hash) bazında toplanır.
- Sıralama: shared ROAS, eşitlikte link CTR, sonra harcama.
- Gürültüyü kesmek için dönemde en az MIN_SPEND kadar harcamış creative'ler
  sıralamaya girer.

Kullanım:
    python creative_sync.py
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

GRAPH_API_VERSION = "v26.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
ISTANBUL = ZoneInfo("Europe/Istanbul")

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "creative_template.html")
SITE_DIR = os.path.join(HERE, "_site")
THUMB_DIR = os.path.join(SITE_DIR, "thumbs")

# Marka -> (Trendyol CPAS reklam hesabı, marka token'ının env değişkeni)
ACCOUNTS = {
    "Aqua di Polo": ("1491645661756000", "META_ACCESS_TOKEN_AQUA_DI_POLO"),
    "Frime": ("1094178842462484", "META_ACCESS_TOKEN_FRIME"),
    "Pierre Cardin": ("1453774025708518", "META_ACCESS_TOKEN_PIERRE_CARDIN"),
}

# (id, etiket, gün sayısı, sıralamaya girmek için gereken en az harcama ₺)
# Eşik 30 günde ₺1.000; 7 günde aynı günlük tempoya denk gelen ₺250.
PERIODS = [
    ("7g", "Son 7 Gün", 7, 250),
    ("30g", "Son 30 Gün", 30, 1000),
]

TOP_N = 3

# Ana panelle aynı satın alma tipi; böylece iki paneldeki ciro tutarlı kalır.
PURCHASE_TYPES = ["omni_purchase", "purchase"]

INSIGHT_FIELDS = ",".join([
    "ad_id", "ad_name", "spend", "impressions", "inline_link_clicks",
    "catalog_segment_actions", "catalog_segment_value",
    "video_thruplay_watched_actions", "actions",
])

AD_FIELDS = (
    "id,name,preview_shareable_link,"
    "creative{id,object_type,video_id,image_hash,object_story_spec,asset_feed_spec}"
)

AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
         "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]


# ---------------------------------------------------------------------------
# Graph API
# ---------------------------------------------------------------------------

def token_for(env_var: str) -> str:
    """Marka token'ı varsa onu, yoksa genel token'ı döner."""
    tok = os.environ.get(env_var) or os.environ.get("META_ACCESS_TOKEN")
    if not tok:
        sys.exit(f"{env_var} ve META_ACCESS_TOKEN tanımlı değil.")
    return tok


def graph_get(path: str, token: str, **params) -> dict:
    resp = requests.get(f"{GRAPH}/{path}", params={**params, "access_token": token}, timeout=90)
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"{path}: {data['error'].get('message')}")
    return data


def graph_all(path: str, token: str, **params) -> list:
    """Sayfalı uç noktayı sonuna kadar gezer."""
    out = []
    data = graph_get(path, token, **params)
    while True:
        out.extend(data.get("data", []))
        nxt = data.get("paging", {}).get("next")
        if not nxt:
            return out
        resp = requests.get(nxt, timeout=90)
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"{path} (sayfa): {data['error'].get('message')}")


def action_value(items, types) -> float:
    """Meta aynı olayı birden çok kırılımla döndürür; ilk bulunan tipi al,
    toplama — aksi halde omni_purchase ile purchase iki kez sayılır."""
    if not items:
        return 0.0
    by_type = {i.get("action_type"): i.get("value") for i in items}
    for t in types:
        if t in by_type:
            return float(by_type[t] or 0)
    return 0.0


# ---------------------------------------------------------------------------
# Dönem ve creative sınıflandırma
# ---------------------------------------------------------------------------

def period_range(days: int):
    """Dünün sonunda biten `days` günlük aralık (İstanbul saati)."""
    today = datetime.now(ISTANBUL).date()
    until = today - timedelta(days=1)
    since = today - timedelta(days=days)
    return since, until


def gun(d) -> str:
    return f"{d.day} {AYLAR[d.month - 1]}"


def is_catalog(creative: dict) -> bool:
    oss = creative.get("object_story_spec") or {}
    return "template_data" in oss


def creative_key_and_format(creative: dict):
    """Aynı videoyu/görseli kullanan reklamları birleştirmek için anahtar."""
    oss = creative.get("object_story_spec") or {}
    afs = creative.get("asset_feed_spec") or {}

    video_id = (creative.get("video_id")
                or (oss.get("video_data") or {}).get("video_id")
                or next((v.get("video_id") for v in afs.get("videos", []) if v.get("video_id")), None))
    if video_id:
        return f"v{video_id}", "Video"

    link = oss.get("link_data") or {}
    image = (link.get("image_hash")
             or creative.get("image_hash")
             or next((i.get("hash") for i in afs.get("images", []) if i.get("hash")), None))
    if image:
        return f"i{image}", "Görsel"

    return f"c{creative.get('id')}", "Görsel"


def fetch_ads(account: str, token: str, ad_ids: list) -> dict:
    """v26'da `ids=` kaldırıldı; reklamları hesap altından id filtresiyle alıyoruz."""
    ads = {}
    for i in range(0, len(ad_ids), 25):
        chunk = ad_ids[i:i + 25]
        rows = graph_all(
            f"act_{account}/ads", token, limit=100, fields=AD_FIELDS,
            filtering=json.dumps([{"field": "id", "operator": "IN", "value": chunk}]),
        )
        for a in rows:
            ads[a["id"]] = a
    return ads


# ---------------------------------------------------------------------------
# Hesap / dönem başına sıralama
# ---------------------------------------------------------------------------

def rank_creatives(account: str, token: str, days: int, min_spend: float) -> dict:
    since, until = period_range(days)
    insights = graph_all(
        f"act_{account}/insights", token, level="ad", limit=500, fields=INSIGHT_FIELDS,
        time_range=json.dumps({"since": str(since), "until": str(until)}),
    )
    insights = [r for r in insights if float(r.get("spend") or 0) > 0]
    ads = fetch_ads(account, token, [r["ad_id"] for r in insights])

    total_spend = 0.0
    groups = {}
    for r in insights:
        spend = float(r.get("spend") or 0)
        total_spend += spend
        ad = ads.get(r["ad_id"])
        if not ad:
            continue
        creative = ad.get("creative") or {}
        if is_catalog(creative):
            continue

        key, fmt = creative_key_and_format(creative)
        g = groups.setdefault(key, {
            "key": key, "format": fmt, "spend": 0.0, "impressions": 0, "link_clicks": 0,
            "purchases": 0.0, "value": 0.0, "video_3s": 0.0, "thruplays": 0.0,
            "ads": 0, "_top_spend": -1.0,
        })
        g["spend"] += spend
        g["impressions"] += int(r.get("impressions") or 0)
        g["link_clicks"] += int(r.get("inline_link_clicks") or 0)
        g["purchases"] += action_value(r.get("catalog_segment_actions"), PURCHASE_TYPES)
        g["value"] += action_value(r.get("catalog_segment_value"), PURCHASE_TYPES)
        g["video_3s"] += action_value(r.get("actions"), ["video_view"])
        g["thruplays"] += action_value(r.get("video_thruplay_watched_actions"), ["video_view"])
        g["ads"] += 1
        # Kartta gösterilecek ad/önizleme: bu creative'i en çok harcayan reklam.
        if spend > g["_top_spend"]:
            g["_top_spend"] = spend
            g["name"] = ad.get("name", "")
            g["preview"] = ad.get("preview_shareable_link")
            g["creative_id"] = creative.get("id")

    manual_spend = sum(g["spend"] for g in groups.values())
    eligible = []
    for g in groups.values():
        if g["spend"] < min_spend:
            continue
        imp = g["impressions"]
        g["roas"] = g["value"] / g["spend"] if g["spend"] else 0.0
        g["ctr"] = g["link_clicks"] / imp * 100 if imp else 0.0
        is_video = g["format"] == "Video"
        g["hook"] = g["video_3s"] / imp * 100 if is_video and imp else None
        g["thruplay_rate"] = g["thruplays"] / imp * 100 if is_video and imp else None
        eligible.append(g)

    eligible.sort(key=lambda g: (-g["roas"], -g["ctr"], -g["spend"]))
    top = eligible[:TOP_N]
    for g in top:
        g.pop("_top_spend", None)
        g["purchases"] = int(round(g["purchases"]))

    return {
        "range": f"{gun(since)} – {gun(until)} {until.year}",
        "total_spend": total_spend,
        "manual_spend": manual_spend,
        "manual_count": len(groups),
        "eligible_count": len(eligible),
        "top": top,
    }


# ---------------------------------------------------------------------------
# Kapak resimleri
# ---------------------------------------------------------------------------

def save_thumbnail(creative_id: str, key: str, token: str):
    """Meta'nın kapak linkleri birkaç günde sona eriyor; statik panel için
    resmi senkron sırasında indirip panelle birlikte yayınlıyoruz."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", key)
    path = os.path.join(THUMB_DIR, f"{safe}.jpg")
    if os.path.exists(path):
        return f"thumbs/{safe}.jpg"
    try:
        c = graph_get(creative_id, token, fields="thumbnail_url",
                      thumbnail_width=600, thumbnail_height=600)
        url = c.get("thumbnail_url")
        if not url:
            return None
        img = requests.get(url, timeout=60)
        if img.status_code != 200 or not img.content:
            return None
        with open(path, "wb") as fh:
            fh.write(img.content)
        return f"thumbs/{safe}.jpg"
    except (requests.RequestException, RuntimeError) as exc:
        print(f"  kapak indirilemedi ({key}): {exc}")
        return None


# ---------------------------------------------------------------------------

def main():
    os.makedirs(THUMB_DIR, exist_ok=True)
    brands = []
    periods_out = []

    for pid, label, days, min_spend in PERIODS:
        since, until = period_range(days)
        periods_out.append({"id": pid, "label": label, "min_spend": min_spend,
                            "range": f"{gun(since)} – {gun(until)} {until.year}"})

    for brand, (account, env_var) in ACCOUNTS.items():
        token = token_for(env_var)
        by_period = {}
        for pid, label, days, min_spend in PERIODS:
            print(f"Çekiliyor: {brand} / {label}...")
            res = rank_creatives(account, token, days, min_spend)
            for g in res["top"]:
                g["thumb"] = save_thumbnail(g["creative_id"], g["key"], token)
            by_period[pid] = res
            print(f"  {res['manual_count']} katalog dışı creative, "
                  f"{res['eligible_count']} eşiği geçti, ilk {len(res['top'])} seçildi")
        brands.append({"brand": brand, "account": account, "by_period": by_period})

    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    payload = {"updated": updated, "periods": periods_out, "brands": brands}

    with open(TEMPLATE, encoding="utf-8") as fh:
        template = fh.read()
    if "/*__DATA__*/" not in template:
        sys.exit("Şablonda /*__DATA__*/ yer tutucusu yok.")
    html = template.replace("/*__DATA__*/",
                            "const DATA = " + json.dumps(payload, ensure_ascii=False) + ";")
    with open(os.path.join(SITE_DIR, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"_site/index.html yazıldı — {len(brands)} marka, senkron {updated}")


if __name__ == "__main__":
    main()
