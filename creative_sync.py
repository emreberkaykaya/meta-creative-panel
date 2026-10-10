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
- Yalnızca yayında olan reklamlar: durumu ACTIVE ve dün en az 1 gösterim
  almış. Kapatılmış reklamların creative'leri sıralamaya girmez.
- Aynı creative tek kart: önce video_id / image_hash ile, sonra kapak
  resminin görsel parmak iziyle (dHash) birleştirilir. Reklam kopyalanınca
  video çoğu zaman yeniden yüklenip yeni ID alıyor; ID'ye bakmak aynı videoyu
  5 kartın 4'üne yerleştiriyordu. İsme de bakılamaz: aynı isim altında farklı
  videolar olabiliyor. Ölçülen mesafeler iki tepeli: birebir aynı videolar 0,
  farklı videolar 11 ve üstü — eşik 5.
- Çan eğrisi: creative'lerin gösterimleri logaritmik ölçekte çan eğrisine
  benziyor. Ortalamanın yarım standart sapma altında kalan kuyruk (o hesap
  için az veri) elenir. Sabit ₺/gösterim eşiği yerine her hesap kendi dağılımıyla
  değerlendiriliyor.
- Sıralama: ROAS'ı olan her zaman önde, kendi içinde en yüksek ROAS birinci;
  ROAS'ı olmayanlar en yüksek CTR'ye göre.

Kullanım:
    python creative_sync.py
"""

import io
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from PIL import Image

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

# (id, etiket, gün sayısı)
PERIODS = [
    ("7g", "Son 7 Gün", 7),
    ("30g", "Son 30 Gün", 30),
]

TOP_N = 5

# İki kapak arasındaki dHash (64 bit) Hamming mesafesi bu kadar ya da azsa
# aynı creative sayılır. 2026-10-10 ölçümü: kopyalar 0, farklı videolar ≥ 11.
SAME_IMAGE_MAX_DISTANCE = 5

# Çan eğrisi kuyruğu: log(gösterim) ortalamasının kaç standart sapma altı
# "az veri" sayılıp elenir. 2026-10-10'da üç değer denendi: 1σ fazla gevşek
# (₺39 harcamalı tek satışlık bir creative 18x ile birinci çıktı), 0σ fazla
# sıkı (Aqua di Polo'da 5 yer dolmadı), 0,5σ gürültüyü ayıklayıp her hesapta
# 5 yeri dolduruyor.
TAIL_SIGMA = 0.5
# Dağılımı tahmin etmek için en az bu kadar creative gerekir; daha azsa eleme
# yapılmaz (iki noktanın standart sapması anlamsız).
MIN_FOR_CURVE = 4

# Ana panelle aynı satın alma tipi; böylece iki paneldeki ciro tutarlı kalır.
PURCHASE_TYPES = ["omni_purchase", "purchase"]

INSIGHT_FIELDS = ",".join([
    "ad_id", "ad_name", "spend", "impressions", "inline_link_clicks",
    "catalog_segment_actions", "catalog_segment_value",
    "video_thruplay_watched_actions", "actions",
])

AD_FIELDS = (
    "id,name,effective_status,preview_shareable_link,"
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

def delivering_yesterday(account: str, token: str) -> set:
    """Dün en az 1 gösterim almış reklamlar. Durumu ACTIVE görünüp aslında
    yayın almayan reklamları elemek için durum kontrolüne ek olarak kullanılıyor."""
    y = datetime.now(ISTANBUL).date() - timedelta(days=1)
    rows = graph_all(
        f"act_{account}/insights", token, level="ad", limit=500, fields="ad_id,impressions",
        time_range=json.dumps({"since": str(y), "until": str(y)}),
    )
    return {r["ad_id"] for r in rows if int(r.get("impressions") or 0) >= 1}


def collect_groups(account: str, token: str, days: int, live_ads: set):
    """Dönemdeki aktif, katalog dışı reklamları video_id / image_hash bazında toplar."""
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
        # Yalnızca şu an yayında olan reklamlar: ACTIVE durumu (kampanya, reklam
        # seti ve reklam açık) ve dün en az 1 gösterim. Kapatılmış reklamların
        # creative'leri geçmişte iyi olsa bile listeye girmiyor.
        if ad.get("effective_status") != "ACTIVE" or r["ad_id"] not in live_ads:
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

    rng = f"{gun(since)} – {gun(until)} {until.year}"
    return list(groups.values()), total_spend, rng


SUM_FIELDS = ("spend", "impressions", "link_clicks", "purchases", "value",
              "video_3s", "thruplays", "ads")


def merge_same_image(groups: list, thumbs: "ThumbCache") -> list:
    """Kapakları görsel olarak aynı olan grupları birleştirir (union-find)."""
    hashes = [thumbs.dhash(g["creative_id"]) for g in groups]
    parent = list(range(len(groups)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            if hashes[i] is None or hashes[j] is None:
                continue
            if bin(hashes[i] ^ hashes[j]).count("1") <= SAME_IMAGE_MAX_DISTANCE:
                parent[find(i)] = find(j)

    clusters = {}
    for i, g in enumerate(groups):
        clusters.setdefault(find(i), []).append(g)

    merged = []
    for members in clusters.values():
        # Temsilci: en çok harcayan üye — adı, önizlemesi ve kapağı onun.
        lead = max(members, key=lambda g: g["spend"])
        m = {k: lead[k] for k in ("key", "name", "preview", "creative_id")}
        m["format"] = "Video" if any(g["format"] == "Video" for g in members) else lead["format"]
        for f in SUM_FIELDS:
            m[f] = sum(g[f] for g in members)
        merged.append(m)
    return merged


def curve_cutoff(items: list):
    """Çan eğrisi alt sınırı: log(gösterim) ortalamasının TAIL_SIGMA standart
    sapma altı. Gösterimler birkaç kat büyüklüğe yayıldığı için log ölçeği
    dağılımı çan eğrisine yaklaştırıyor."""
    logs = [math.log(g["impressions"]) for g in items if g["impressions"] > 0]
    if len(logs) < MIN_FOR_CURVE:
        return 1
    mu = sum(logs) / len(logs)
    sd = math.sqrt(sum((x - mu) ** 2 for x in logs) / len(logs))
    return max(1, int(math.exp(mu - TAIL_SIGMA * sd)))


def rank_creatives(account: str, token: str, days: int, live_ads: set,
                   thumbs: "ThumbCache") -> dict:
    groups, total_spend, rng = collect_groups(account, token, days, live_ads)
    merged = merge_same_image(groups, thumbs)

    for g in merged:
        imp = g["impressions"]
        g["roas"] = g["value"] / g["spend"] if g["spend"] else 0.0
        g["ctr"] = g["link_clicks"] / imp * 100 if imp else 0.0
        is_video = g["format"] == "Video"
        g["hook"] = g["video_3s"] / imp * 100 if is_video and imp else None
        g["thruplay_rate"] = g["thruplays"] / imp * 100 if is_video and imp else None
        g["purchases"] = int(round(g["purchases"]))

    cutoff = curve_cutoff(merged)
    eligible = [g for g in merged if g["impressions"] >= cutoff]

    # ROAS'ı olan her zaman önde, kendi içinde en yüksek ROAS birinci;
    # ROAS'ı olmayanlar en yüksek CTR'ye göre.
    with_roas = sorted((g for g in eligible if g["roas"] > 0),
                       key=lambda g: (-g["roas"], -g["ctr"], -g["spend"]))
    by_ctr = sorted((g for g in eligible if g["roas"] == 0),
                    key=lambda g: (-g["ctr"], -g["spend"]))
    top = (with_roas + by_ctr)[:TOP_N]
    for g in top:
        g["basis"] = "ROAS" if g["roas"] > 0 else "CTR"

    return {
        "range": rng,
        "total_spend": total_spend,
        "manual_spend": sum(g["spend"] for g in merged),
        "manual_count": len(merged),
        "duplicates_merged": len(groups) - len(merged),
        "eligible_count": len(eligible),
        "roas_count": len(with_roas),
        "min_impressions": cutoff,
        "top": top,
    }


# ---------------------------------------------------------------------------
# Kapak resimleri
# ---------------------------------------------------------------------------

class ThumbCache:
    """Kapakları creative başına bir kez indirir: hem görsel parmak izi
    (birleştirme için) hem de panelde yayınlanan dosya buradan gelir.
    Meta'nın kapak linkleri birkaç günde sona erdiği için statik panel
    resmi kendisi barındırıyor."""

    def __init__(self, token_for_creative):
        self._token = token_for_creative
        self._bytes = {}

    def bytes(self, creative_id: str):
        if creative_id not in self._bytes:
            data = None
            try:
                c = graph_get(creative_id, self._token(), fields="thumbnail_url",
                              thumbnail_width=600, thumbnail_height=600)
                url = c.get("thumbnail_url")
                if url:
                    img = requests.get(url, timeout=60)
                    if img.status_code == 200 and img.content:
                        data = img.content
            except (requests.RequestException, RuntimeError) as exc:
                print(f"  kapak indirilemedi ({creative_id}): {exc}")
            self._bytes[creative_id] = data
        return self._bytes[creative_id]

    def dhash(self, creative_id: str):
        data = self.bytes(creative_id)
        if not data:
            return None
        try:
            im = Image.open(io.BytesIO(data)).convert("L").resize((9, 8), Image.LANCZOS)
        except OSError:
            return None
        px = list(im.getdata())
        bits = 0
        for r in range(8):
            for k in range(8):
                bits = (bits << 1) | (px[r * 9 + k] > px[r * 9 + k + 1])
        return bits

    def save(self, creative_id: str, key: str):
        data = self.bytes(creative_id)
        if not data:
            return None
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", key)
        with open(os.path.join(THUMB_DIR, f"{safe}.jpg"), "wb") as fh:
            fh.write(data)
        return f"thumbs/{safe}.jpg"


# ---------------------------------------------------------------------------

def main():
    os.makedirs(THUMB_DIR, exist_ok=True)
    brands = []
    periods_out = []

    for pid, label, days in PERIODS:
        since, until = period_range(days)
        periods_out.append({"id": pid, "label": label,
                            "range": f"{gun(since)} – {gun(until)} {until.year}"})

    for brand, (account, env_var) in ACCOUNTS.items():
        token = token_for(env_var)
        thumbs = ThumbCache(lambda t=token: t)
        live_ads = delivering_yesterday(account, token)
        print(f"{brand}: dün yayın alan {len(live_ads)} reklam")
        by_period = {}
        for pid, label, days in PERIODS:
            print(f"Çekiliyor: {brand} / {label}...")
            res = rank_creatives(account, token, days, live_ads, thumbs)
            for g in res["top"]:
                g["thumb"] = thumbs.save(g["creative_id"], g["key"])
            by_period[pid] = res
            print(f"  {res['manual_count']} creative ({res['duplicates_merged']} kopya birleşti), "
                  f"eğri alt sınırı {res['min_impressions']:,} gösterim → {res['eligible_count']} uygun, "
                  f"{res['roas_count']} ROAS'lı, ilk {len(res['top'])} seçildi")
        brands.append({"brand": brand, "account": account, "by_period": by_period})

    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    payload = {"updated": updated, "periods": periods_out, "brands": brands,
               "top_n": TOP_N, "tail_sigma": TAIL_SIGMA}

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
