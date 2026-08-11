"""Yaver - beyin modulu.

MQTT'den metin alir, llama-server'a sorar, cevabi MQTT'ye yayinlar.
Mikrofonun, hoparlorun, hatta LLM'in nerede oldugunu bilmez.

Kullanim:
    python beyin.py
    python beyin.py --yaz "saat kac"    -> MQTT'siz tek soru sorup cikar
"""

import json
import re
import sys
import time
from collections import deque
from pathlib import Path

import httpx
import yaml

KOK = Path(__file__).parent
AYAR = yaml.safe_load((KOK / "config.yaml").read_text(encoding="utf-8"))
B = AYAR["beyin"]

HAFIZA_DOSYA = KOK / "hafiza.json"      # kalici bilgiler (ad, tercihler)
GECMIS_DOSYA = KOK / "gecmis.json"      # son konusma turlari

gecmis = deque(maxlen=B["gecmis_tur"] * 2)


# --------------------------------------------------------------------------- hafiza
def hafiza_oku():
    if not HAFIZA_DOSYA.exists():
        return {}
    try:
        return json.loads(HAFIZA_DOSYA.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def gecmis_oku():
    if GECMIS_DOSYA.exists():
        try:
            for m in json.loads(GECMIS_DOSYA.read_text(encoding="utf-8")):
                gecmis.append(m)
        except json.JSONDecodeError:
            pass


def gecmis_yaz():
    GECMIS_DOSYA.write_text(
        json.dumps(list(gecmis), ensure_ascii=False, indent=1), encoding="utf-8"
    )


def sistem_promptu():
    metin = B["sistem_promptu"].strip()
    bilinen = hafiza_oku()
    if bilinen:
        satirlar = "\n".join(f"- {k}: {d}" for k, d in bilinen.items())
        metin += f"\n\nKullanici hakkinda bildiklerin:\n{satirlar}"
    metin += f"\n\nSu anki tarih ve saat: {time.strftime('%d.%m.%Y %H:%M')}"
    return metin


# --------------------------------------------------------------------------- LLM
CUMLE_SONU = re.compile(r"(?<=[.!?:;])\s+|\n+")


def cumleye_bol(tampon):
    """Tamponda tamamlanmis cumle varsa (cumle, kalan) dondurur."""
    eslesme = CUMLE_SONU.search(tampon)
    if not eslesme:
        return None, tampon
    kesim = eslesme.end()
    cumle = tampon[:kesim].strip()
    if len(cumle) < 12:  # cok kisa parca, biraz daha bekle
        return None, tampon
    return cumle, tampon[kesim:]


def sor(soru, cumle_geldi=None):
    """LLM'e sorar. cumle_geldi verilirse her tamamlanan cumlede cagirir."""
    mesajlar = [{"role": "system", "content": sistem_promptu()}]
    mesajlar += list(gecmis)
    mesajlar.append({"role": "user", "content": soru})

    govde = {
        "messages": mesajlar,
        "temperature": B["sicaklik"],
        "max_tokens": B["en_fazla_token"],
        "stream": bool(B["akis"] and cumle_geldi),
    }

    t0 = time.time()
    tam = ""

    if not govde["stream"]:
        y = httpx.post(f"{B['sunucu']}/v1/chat/completions", json=govde, timeout=120)
        y.raise_for_status()
        tam = y.json()["choices"][0]["message"]["content"].strip()
        if cumle_geldi:
            cumle_geldi(tam)
    else:
        tampon = ""
        ilk = None
        with httpx.stream(
            "POST", f"{B['sunucu']}/v1/chat/completions", json=govde, timeout=120
        ) as y:
            y.raise_for_status()
            for satir in y.iter_lines():
                if not satir.startswith("data: "):
                    continue
                veri = satir[6:]
                if veri.strip() == "[DONE]":
                    break
                try:
                    delta = json.loads(veri)["choices"][0]["delta"].get("content", "")
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                if not delta:
                    continue
                if ilk is None:
                    ilk = time.time() - t0
                tampon += delta
                tam += delta
                while True:
                    cumle, tampon = cumleye_bol(tampon)
                    if not cumle:
                        break
                    cumle_geldi(cumle)
        if tampon.strip():
            cumle_geldi(tampon.strip())
        if ilk:
            print(f"  (ilk token {ilk:.2f} sn, toplam {time.time() - t0:.2f} sn)")

    tam = tam.strip()
    gecmis.append({"role": "user", "content": soru})
    gecmis.append({"role": "assistant", "content": tam})
    gecmis_yaz()
    return tam


# --------------------------------------------------------------------------- MQTT
def main():
    import paho.mqtt.client as mqtt

    try:
        istemci = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="yaver-beyin")
    except AttributeError:
        istemci = mqtt.Client(client_id="yaver-beyin")

    def yayinla_cumle(cumle):
        print(f"  -> {cumle}")
        istemci.publish(
            AYAR["mqtt"]["konu_cevap"],
            json.dumps({"metin": cumle, "zaman": time.time()}, ensure_ascii=False),
        )

    def mesaj_geldi(istemci_, kullanici, mesaj):
        try:
            soru = json.loads(mesaj.payload.decode("utf-8")).get("metin", "").strip()
        except json.JSONDecodeError:
            return
        if not soru:
            return
        print(f'\nSORU: "{soru}"')
        try:
            sor(soru, yayinla_cumle)
        except httpx.HTTPError as hata:
            print(f"  LLM hatasi: {hata}")
            yayinla_cumle("Beynime ulasamiyorum, sunucu kapali olabilir.")

    def baglandi(istemci_, kullanici, bayrak, kod, ozellik=None):
        istemci_.subscribe(AYAR["mqtt"]["konu_metin"])
        print(f"Dinleniyor: {AYAR['mqtt']['konu_metin']}")

    istemci.on_connect = baglandi
    istemci.on_message = mesaj_geldi
    istemci.connect(AYAR["mqtt"]["sunucu"], AYAR["mqtt"]["port"], 60)

    gecmis_oku()
    print(f"Beyin hazir. LLM: {B['sunucu']}  (gecmis: {len(gecmis)//2} tur)")
    istemci.loop_forever()


if __name__ == "__main__":
    if "--yaz" in sys.argv:
        gecmis_oku()
        soru = " ".join(sys.argv[sys.argv.index("--yaz") + 1:])
        print(sor(soru, lambda c: print(f"  -> {c}")))
    else:
        try:
            main()
        except KeyboardInterrupt:
            print("\nbeyin kapandi.")
