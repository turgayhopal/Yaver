"""Yaver - agiz modulu.

MQTT'den cevap metni alir, Piper ile seslendirir, hoparlorden calar.
Cumleler sirayla calinir, ust uste binmez.

Kullanim:
    python agiz.py
    python agiz.py --de "merhaba ben yaver"   -> MQTT'siz tek cumle soyle
"""

import io
import json
import queue
import random
import sys
import threading
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
import yaml

KOK = Path(__file__).parent
AYAR = yaml.safe_load((KOK / "config.yaml").read_text(encoding="utf-8"))
A = AYAR["agiz"]

kuyruk: "queue.Queue[dict]" = queue.Queue()
onay_sesleri = []      # [(int16 dizi, ornekleme)] - acilista hazirlanir
istemci_ref = [None]   # calisan is parcacigi buradan yayin yapar


def ses_yukle():
    from piper import PiperVoice

    yol = Path(A["ses_modeli"])
    if not yol.exists():
        raise SystemExit(
            f"Ses modeli bulunamadi: {yol}\n"
            "tr_TR-dfki-medium.onnx ve .onnx.json dosyalarini indirip "
            "config.yaml icindeki yolu duzelt."
        )
    print(f"Ses modeli yukleniyor: {yol.name}")
    return PiperVoice.load(str(yol))


def sentezle(ses, metin):
    """Metni int16 dizi + ornekleme hizina cevirir.

    Piper'in Python API'si surumden surume degisti, ucunu de deniyoruz.
    """
    # Yeni API (2.x): synthesize(metin) -> AudioChunk ureteci
    try:
        parcalar, hiz = [], None
        for parca in ses.synthesize(metin):
            parcalar.append(np.frombuffer(parca.audio_int16_bytes, dtype=np.int16))
            hiz = parca.sample_rate
        if parcalar:
            return np.concatenate(parcalar), hiz
    except TypeError:
        pass  # eski API iki argüman ister, asagi dusuyoruz

    # Eski API (1.x): wave dosyasina yazar
    tampon = io.BytesIO()
    with wave.open(tampon, "wb") as wav:
        if hasattr(ses, "synthesize_wav"):
            ses.synthesize_wav(metin, wav)
        else:
            ses.synthesize(metin, wav)
    tampon.seek(0)
    with wave.open(tampon, "rb") as wav:
        return np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16), wav.getframerate()


def cal(veri, hiz):
    if A["ses_seviyesi"] != 1.0:
        veri = np.clip(veri * A["ses_seviyesi"], -32768, 32767).astype(np.int16)
    sd.play(veri, hiz, device=A["cikis_cihazi"])
    sd.wait()


def konus(ses, metin):
    cal(*sentezle(ses, metin))


def onaylari_hazirla(ses):
    """Onay cumlelerini acilista sentezleyip bellekte tutar - gecikme sifir olur."""
    for cumle in A.get("onay_cumleleri") or []:
        try:
            onay_sesleri.append(sentezle(ses, cumle))
        except Exception as hata:
            print(f"  onay hazirlanamadi ({cumle}): {hata}")
    print(f"  {len(onay_sesleri)} onay cumlesi hazir")


def durum_yayinla(durum):
    if istemci_ref[0] is None:
        return
    istemci_ref[0].publish(
        AYAR["mqtt"]["konu_durum"],
        json.dumps({"modul": "agiz", "durum": durum}, ensure_ascii=False),
    )


def calisan(ses):
    while True:
        is_ = kuyruk.get()
        durum_yayinla("konusuyor")
        try:
            if is_["tip"] == "onay":
                if onay_sesleri:
                    cal(*random.choice(onay_sesleri))
                durum_yayinla("hazir")   # kulak bu sinyali bekliyor
            else:
                print(f'  soyluyorum: "{is_["icerik"]}"')
                konus(ses, is_["icerik"])
        except Exception as hata:
            print(f"  seslendirme hatasi: {hata}")
            if is_["tip"] == "onay":
                durum_yayinla("hazir")
        if kuyruk.empty():
            durum_yayinla("sustu")


def main():
    import paho.mqtt.client as mqtt

    ses = ses_yukle()
    onaylari_hazirla(ses)
    threading.Thread(target=calisan, args=(ses,), daemon=True).start()

    try:
        istemci = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="yaver-agiz")
    except AttributeError:
        istemci = mqtt.Client(client_id="yaver-agiz")
    istemci_ref[0] = istemci

    def mesaj_geldi(istemci_, kullanici, mesaj):
        try:
            paket = json.loads(mesaj.payload.decode("utf-8"))
        except json.JSONDecodeError:
            return
        if mesaj.topic == AYAR["mqtt"]["konu_durum"]:
            if paket.get("modul") == "kulak" and paket.get("durum") == "uyandi":
                kuyruk.put({"tip": "onay"})
            return
        metin = paket.get("metin", "").strip()
        if metin:
            kuyruk.put({"tip": "metin", "icerik": metin})

    def baglandi(istemci_, kullanici, bayrak, kod, ozellik=None):
        istemci_.subscribe(AYAR["mqtt"]["konu_cevap"])
        istemci_.subscribe(AYAR["mqtt"]["konu_durum"])
        print(f"Dinleniyor: {AYAR['mqtt']['konu_cevap']} + {AYAR['mqtt']['konu_durum']}")

    istemci.on_connect = baglandi
    istemci.on_message = mesaj_geldi
    istemci.connect(AYAR["mqtt"]["sunucu"], AYAR["mqtt"]["port"], 60)
    print("Agiz hazir.")
    istemci.loop_forever()


if __name__ == "__main__":
    if "--de" in sys.argv:
        cumle = " ".join(sys.argv[sys.argv.index("--de") + 1:])
        konus(ses_yukle(), cumle)
    else:
        try:
            main()
        except KeyboardInterrupt:
            print("\nagiz kapandi.")