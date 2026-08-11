"""Yaver - kulak modulu.

Mikrofonu dinler, uyandirma kelimesini yakalar, konusmayi yaziya cevirir
ve MQTT uzerinden yayinlar. Baska hicbir modulun varligini bilmez.

Kullanim:
    python kulak.py              -> config.yaml'daki ayarlarla
    python kulak.py --yaziyok    -> MQTT'ye yollamaz, sadece ekrana yazar
"""

import os
import sys
from pathlib import Path

# CTranslate2 (Whisper motoru) CUDA DLL'lerini sistemde arar ama pip ile kurulan
# nvidia paketleri PATH'e girmez. Burada, faster_whisper import edilmeden ONCE
# tanitiyoruz. NVIDIA klasor duzenini surumden surume degistirdigi icin sabit
# yol yazmiyoruz: nvidia altinda DLL iceren HER klasoru buluyoruz.
if sys.platform == "win32":
    _kok = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    _klasorler = sorted({p.parent for p in _kok.rglob("*.dll")}) if _kok.exists() else []
    for _k in _klasorler:
        try:
            os.add_dll_directory(str(_k))
        except OSError:
            pass
    # Bazi yukleyiciler add_dll_directory yerine PATH'e bakar, ikisini de doldur.
    if _klasorler:
        os.environ["PATH"] = os.pathsep.join(str(k) for k in _klasorler) + os.pathsep + os.environ["PATH"]
    if "--dll" in sys.argv:
        print(f"nvidia koku: {_kok}  (var mi: {_kok.exists()})")
        for _k in _klasorler:
            print("  +", _k)
            for _d in sorted(_k.glob("*.dll"))[:6]:
                print("      ", _d.name)
        sys.exit(0)

import json
import queue
import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd
import yaml

AYAR = yaml.safe_load((Path(__file__).parent / "config.yaml").read_text(encoding="utf-8"))
SESSIZ_MOD = "--yaziyok" in sys.argv

ses_kuyrugu: "queue.Queue[np.ndarray]" = queue.Queue()

agiz_mesgul = threading.Event()   # agiz konusurken kulak susar
onay_bitti = threading.Event()    # "efendim" bitti, kayda baslayabiliriz


# --------------------------------------------------------------------------- MQTT
def mqtt_baglan():
    if SESSIZ_MOD:
        return None
    import paho.mqtt.client as mqtt

    try:
        istemci = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="yaver-kulak")
    except AttributeError:  # paho 1.x
        istemci = mqtt.Client(client_id="yaver-kulak")

    def durum_geldi(istemci_, kullanici, mesaj):
        try:
            paket = json.loads(mesaj.payload.decode("utf-8"))
        except json.JSONDecodeError:
            return
        if paket.get("modul") != "agiz":
            return
        durum = paket.get("durum")
        if durum == "konusuyor":
            agiz_mesgul.set()
        elif durum in ("sustu", "hazir"):
            agiz_mesgul.clear()
            if durum == "hazir":
                onay_bitti.set()

    def baglandi(istemci_, kullanici, bayrak, kod, ozellik=None):
        istemci_.subscribe(AYAR["mqtt"]["konu_durum"])

    istemci.on_connect = baglandi
    istemci.on_message = durum_geldi
    istemci.connect(AYAR["mqtt"]["sunucu"], AYAR["mqtt"]["port"], 60)
    istemci.loop_start()
    print(f"MQTT baglandi: {AYAR['mqtt']['sunucu']}:{AYAR['mqtt']['port']}")
    return istemci


def durum_yayinla(istemci, durum):
    if istemci is None:
        return
    istemci.publish(
        AYAR["mqtt"]["konu_durum"],
        json.dumps({"modul": "kulak", "durum": durum}, ensure_ascii=False),
    )


def yayinla(istemci, metin):
    paket = {"metin": metin, "kaynak": "kulak-pc", "zaman": time.time()}
    if istemci is None:
        print(f"  [yayinlanmadi] {paket}")
        return
    istemci.publish(AYAR["mqtt"]["konu_metin"], json.dumps(paket, ensure_ascii=False))


# --------------------------------------------------------------------------- ses
def ses_geri_cagir(veri, cerceve, zaman, durum):
    if durum:
        print(f"  ses uyarisi: {durum}", file=sys.stderr)
    ses_kuyrugu.put(veri.copy().flatten())


def rms(blok):
    return float(np.sqrt(np.mean(blok.astype(np.float32) ** 2)))


def ortam_olc(saniye=1.5):
    """Acilista ortam gurultusunu olcup sessizlik esigini belirler."""
    print(f"Ortam gurultusu olculuyor ({saniye} sn, sessiz ol)...")
    bloklar = []
    bitis = time.time() + saniye
    while time.time() < bitis:
        try:
            bloklar.append(rms(ses_kuyrugu.get(timeout=1)))
        except queue.Empty:
            break
    taban = float(np.median(bloklar)) if bloklar else 100.0
    esik = max(taban * AYAR["kayit"]["sessizlik_carpani"], 180.0)
    print(f"  ortam: {taban:.0f}  ->  konusma esigi: {esik:.0f}")
    return esik


# --------------------------------------------------------------------------- moduller
def whisper_yukle():
    from faster_whisper import WhisperModel

    w = AYAR["whisper"]

    def dene(cihaz, tip):
        model = WhisperModel(w["model"], device=cihaz, compute_type=tip)
        # Isitma: 1 sn sessizlik cevir. Hem CUDA'yi burada test etmis oluruz,
        # hem de ilk gercek konusma yavas olmaz.
        list(model.transcribe(np.zeros(16000, dtype=np.float32), language=w["dil"])[0])
        return model

    print(f"Whisper yukleniyor: {w['model']} ({w['cihaz']}/{w['hesap_tipi']})...")
    try:
        model = dene(w["cihaz"], w["hesap_tipi"])
        print(f"  hazir ({w['cihaz']})")
        return model
    except Exception as hata:
        if w["cihaz"] == "cpu":
            raise
        print(f"  GPU acilmadi: {hata}")
        print("  CPU'ya dusuluyor. Hizlandirmak icin:")
        print("    pip install nvidia-cublas-cu12 nvidia-cudnn-cu12")
        model = dene("cpu", "int8")
        print("  hazir (cpu/int8)")
        return model


def uyandirma_yukle():
    import openwakeword
    from openwakeword.model import Model

    ad = AYAR["uyandirma"]["model"]
    try:
        oww = Model(wakeword_models=[ad], inference_framework="onnx")
    except Exception:
        print("Uyandirma modelleri indiriliyor (tek seferlik)...")
        openwakeword.utils.download_models()
        oww = Model(wakeword_models=[ad], inference_framework="onnx")
    print(f"Uyandirma modeli hazir: {ad}")
    return oww


# --------------------------------------------------------------------------- akis
def konusmayi_kaydet(esik, on_kayit=None):
    """Sessizlik gelene kadar kaydeder, int16 numpy dizisi dondurur.

    on_kayit: tetiklenmeden onceki bloklar. Cumlenin ilk hecesi bunlarda.
    """
    print("  dinliyorum...", end="", flush=True)
    parcalar = list(on_kayit) if on_kayit else []
    sessiz_blok = 0
    blok_sn = AYAR["ses"]["blok"] / AYAR["ses"]["ornekleme"]
    gereken_sessiz = int(AYAR["kayit"]["sessizlik_sn"] / blok_sn)
    en_fazla = int(AYAR["kayit"]["en_uzun_sn"] / blok_sn)
    konusma_basladi = False

    for _ in range(en_fazla):
        blok = ses_kuyrugu.get()
        parcalar.append(blok)
        if rms(blok) > esik:
            konusma_basladi = True
            sessiz_blok = 0
        elif konusma_basladi:
            sessiz_blok += 1
            if sessiz_blok >= gereken_sessiz:
                break

    print(" bitti.")
    return np.concatenate(parcalar) if parcalar else np.array([], dtype=np.int16)


# Whisper sessizlik/gurultu uzerine bunlari uydurur. Coplugu at.
COP = {
    "them", "you", "thank you", "thanks for watching", "bye",
    "altyazi m.k.", "abone ol", "abone olmayi unutmayin",
    "izlediginiz icin tesekkurler", "tesekkurler", "altyazi",
}


def cop_mu(metin):
    sade = metin.lower().strip(" .,!?").replace("ı", "i").replace("ş", "s").replace("ğ", "g")
    return sade in COP or len(sade) < 2


def yaziya_cevir(whisper, ses):
    sure = len(ses) / AYAR["ses"]["ornekleme"]
    if sure < AYAR["kayit"]["en_kisa_sn"]:
        return ""
    t0 = time.time()
    parcalar, bilgi = whisper.transcribe(
        ses.astype(np.float32) / 32768.0,
        language=AYAR["whisper"]["dil"],
        beam_size=1,
        temperature=0.0,
        vad_filter=True,
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
    )
    metin = " ".join(p.text.strip() for p in parcalar).strip()
    print(f"  ({sure:.1f} sn ses -> {time.time() - t0:.2f} sn cevrim)")
    if cop_mu(metin):
        print(f'  (cop filtrelendi: "{metin}")')
        return ""
    return metin


def main():
    whisper = whisper_yukle()
    oww = uyandirma_yukle() if AYAR["uyandirma"]["aktif"] else None
    istemci = mqtt_baglan()

    akis = sd.InputStream(
        device=AYAR["ses"]["giris_cihazi"],
        samplerate=AYAR["ses"]["ornekleme"],
        channels=1,
        dtype="int16",
        blocksize=AYAR["ses"]["blok"],
        callback=ses_geri_cagir,
    )

    on_bellek = deque(maxlen=int(0.6 * AYAR["ses"]["ornekleme"] / AYAR["ses"]["blok"]))

    with akis:
        time.sleep(0.5)  # akisin oturmasini bekle, yoksa olcum sifir cikar
        while not ses_kuyrugu.empty():
            ses_kuyrugu.get()
        esik = ortam_olc()
        if oww:
            print(f"\nHazir. '{AYAR['uyandirma']['model']}' de.  (Ctrl+C ile cik)\n")
        else:
            print("\nHazir. Uyandirma kapali - konusmaya basla.  (Ctrl+C ile cik)\n")

        while True:
            blok = ses_kuyrugu.get()

            # Agiz konusurken hicbir seyi tetikleme, kendi sesini duymasin.
            if agiz_mesgul.is_set():
                on_bellek.clear()
                if oww:
                    oww.reset()
                continue

            on_bellek.append(blok)

            if oww:
                skorlar = oww.predict(blok)
                if max(skorlar.values()) < AYAR["uyandirma"]["esik"]:
                    continue
                print(f"\n>> uyandim (skor {max(skorlar.values()):.2f})")
                oww.reset()
                on_bellek.clear()  # uyandirma kelimesi kayda girmesin

                # Agiza "efendim" dedirt, bitmesini bekle.
                onay_bitti.clear()
                durum_yayinla(istemci, "uyandi")
                if istemci is not None:
                    onay_bitti.wait(AYAR["uyandirma"]["onay_bekle_sn"])
                    time.sleep(0.15)  # hoparlorun sonlanmasi icin kucuk pay
                while not ses_kuyrugu.empty():
                    ses_kuyrugu.get()
            else:
                if rms(blok) < esik:
                    continue
                print("\n>> ses algilandi")

            ses = konusmayi_kaydet(esik, on_bellek)
            on_bellek.clear()
            metin = yaziya_cevir(whisper, ses)

            if metin:
                print(f'  ANLADIM: "{metin}"')
                yayinla(istemci, metin)
            else:
                print("  (bos, atlandi)")

            time.sleep(AYAR["uyandirma"]["soguma_sn"] if oww else 0.3)
            while not ses_kuyrugu.empty():
                ses_kuyrugu.get()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nkulak kapandi.")