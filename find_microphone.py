"""Sistemdeki ses giris cihazlarini listeler ve secileni test eder.

Kullanim:
    python find_microphone.py          -> cihazlari listeler
    python find_microphone.py 3        -> 3 numarali cihazdan 5 sn dinleyip seviye gosterir
"""

import sys

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000


def list_devices():
    print("\n--- SES GIRIS CIHAZLARI ---\n")
    default_device = sd.default.device[0]
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] < 1:
            continue
        marker = " <-- varsayilan" if i == default_device else ""
        print(f"[{i:2d}] {dev['name']}{marker}")
        print(f"     kanal: {dev['max_input_channels']}, hiz: {int(dev['default_samplerate'])} Hz")
    print("\nTest icin:  python find_microphone.py <numara>\n")


def test_device(device):
    print(f"\n[{device}] numarali cihaz dinleniyor - 5 saniye konus.\n")
    peak = 0.0

    def callback(data, frames, time_info, status):
        nonlocal peak
        rms = float(np.sqrt(np.mean(data.astype(np.float32) ** 2)))
        peak = max(peak, rms)
        bar = "#" * min(50, int(rms / 60))
        print(f"\r  seviye: {rms:7.0f} |{bar:<50}|", end="")

    with sd.InputStream(
        device=device,
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=1280,
        callback=callback,
    ):
        sd.sleep(5000)

    print(f"\n\nEn yuksek seviye: {peak:.0f}")
    if peak < 200:
        print("Cok dusuk. Yanlis cihaz olabilir veya mikrofon sessizde.")
    elif peak > 20000:
        print("Cok yuksek, kirpma riski var. Windows ses ayarlarindan kazanci dusur.")
    else:
        print("Seviye iyi. config.yaml icinde  audio.input_device: %d  yaz." % device)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        test_device(int(sys.argv[1]))
    else:
        list_devices()
