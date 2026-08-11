"""Sistemdeki ses giris cihazlarini listeler ve secileni test eder.

Kullanim:
    python mikrofon_bul.py          -> cihazlari listeler
    python mikrofon_bul.py 3        -> 3 numarali cihazdan 5 sn dinleyip seviye gosterir
"""

import sys

import numpy as np
import sounddevice as sd

ORNEKLEME = 16000


def listele():
    print("\n--- SES GIRIS CIHAZLARI ---\n")
    varsayilan = sd.default.device[0]
    for i, c in enumerate(sd.query_devices()):
        if c["max_input_channels"] < 1:
            continue
        isaret = " <-- varsayilan" if i == varsayilan else ""
        print(f"[{i:2d}] {c['name']}{isaret}")
        print(f"     kanal: {c['max_input_channels']}, hiz: {int(c['default_samplerate'])} Hz")
    print("\nTest icin:  python mikrofon_bul.py <numara>\n")


def test(cihaz):
    print(f"\n[{cihaz}] numarali cihaz dinleniyor - 5 saniye konus.\n")
    tepe = 0.0

    def geri_cagir(veri, cerceve, zaman, durum):
        nonlocal tepe
        rms = float(np.sqrt(np.mean(veri.astype(np.float32) ** 2)))
        tepe = max(tepe, rms)
        cubuk = "#" * min(50, int(rms / 60))
        print(f"\r  seviye: {rms:7.0f} |{cubuk:<50}|", end="")

    with sd.InputStream(
        device=cihaz,
        samplerate=ORNEKLEME,
        channels=1,
        dtype="int16",
        blocksize=1280,
        callback=geri_cagir,
    ):
        sd.sleep(5000)

    print(f"\n\nEn yuksek seviye: {tepe:.0f}")
    if tepe < 200:
        print("Cok dusuk. Yanlis cihaz olabilir veya mikrofon sessizde.")
    elif tepe > 20000:
        print("Cok yuksek, kirpma riski var. Windows ses ayarlarindan kazanci dusur.")
    else:
        print("Seviye iyi. config.yaml icinde  ses.giris_cihazi: %d  yaz." % cihaz)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        test(int(sys.argv[1]))
    else:
        listele()