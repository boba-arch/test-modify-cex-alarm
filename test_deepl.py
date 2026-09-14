"""
Script kecil buat tes koneksi & hasil translate DeepL, terpisah dari bot.py.
Tidak butuh BOT_TOKEN / CHANNEL_ID — cuma butuh DEEPL_API_KEY.

Cara pakai:
    export DEEPL_API_KEY="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx:fx"   # (Linux/Mac)
    set DEEPL_API_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx:fx        # (Windows cmd)
    python test_deepl.py

Atau kalau mau tes di environment Railway langsung (variable sudah keisi di sana):
    railway run python test_deepl.py
"""

import os
import requests

DEEPL_API_KEY = os.environ.get("DEEPL_API_KEY")

if not DEEPL_API_KEY:
    print("❌ DEEPL_API_KEY tidak ditemukan di environment. Set dulu sebelum run script ini.")
    raise SystemExit(1)

DEEPL_API_URL = (
    "https://api-free.deepl.com/v2/translate"
    if DEEPL_API_KEY.endswith(":fx")
    else "https://api.deepl.com/v2/translate"
)

print(f"🔑 Key type : {'FREE' if DEEPL_API_KEY.endswith(':fx') else 'PRO'}")
print(f"🌐 Endpoint : {DEEPL_API_URL}")
print("-" * 60)

SAMPLES = [
    ("Binance will delist several trading pairs on 2026-09-20", "ZH"),
    ("거래지원 종료 안내 (KRW 마켓)", "EN-US"),
]

for text, target in SAMPLES:
    print(f"📝 Original ({target}): {text}")
    try:
        r = requests.post(
            DEEPL_API_URL,
            headers={"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"},
            data={"text": text, "target_lang": target},
            timeout=(5, 15),
        )
        print(f"   HTTP status: {r.status_code}")
        r.raise_for_status()
        data = r.json()
        translations = data.get("translations", [])
        if translations:
            print(f"   ✅ Hasil: {translations[0].get('text')}")
        else:
            print(f"   ⚠️ Tidak ada field 'translations' di respons: {data}")
    except requests.exceptions.Timeout:
        print("   ❌ TIMEOUT — koneksi ke DeepL macet/di-block (cek network egress Railway).")
    except requests.exceptions.HTTPError as e:
        print(f"   ❌ HTTP Error: {e} | Body: {r.text[:300]}")
    except Exception as e:
        print(f"   ❌ Error lain: {e}")
    print("-" * 60)
