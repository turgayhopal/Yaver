"""Yaver - ear module (kulak).

Mikrofonu dinler, uyandirma kelimesini yakalar, konusmayi yaziya cevirir
ve MQTT uzerinden yayinlar. Baska hicbir modulun varligini bilmez.

Kullanim:
    python ear.py              -> config.yaml'daki ayarlarla
    python ear.py --textonly   -> MQTT'ye yollamaz, sadece ekrana yazar
"""

import os
import sys
from pathlib import Path

# CTranslate2 (Whisper motoru) CUDA DLL'lerini sistemde arar ama pip ile kurulan
# nvidia paketleri PATH'e girmez. Burada, faster_whisper import edilmeden ONCE
# tanitiyoruz. NVIDIA klasor duzenini surumden surume degistirdigi icin sabit
# yol yazmiyoruz: nvidia altinda DLL iceren HER klasoru buluyoruz.
if sys.platform == "win32":
    _root = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    _dirs = sorted({p.parent for p in _root.rglob("*.dll")}) if _root.exists() else []
    for _d in _dirs:
        try:
            os.add_dll_directory(str(_d))
        except OSError:
            pass
    # Bazi yukleyiciler add_dll_directory yerine PATH'e bakar, ikisini de doldur.
    if _dirs:
        os.environ["PATH"] = os.pathsep.join(str(d) for d in _dirs) + os.pathsep + os.environ["PATH"]
    if "--dll" in sys.argv:
        print(f"nvidia koku: {_root}  (var mi: {_root.exists()})")
        for _d in _dirs:
            print("  +", _d)
            for _f in sorted(_d.glob("*.dll"))[:6]:
                print("      ", _f.name)
        sys.exit(0)

import json
import queue
import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd
import yaml

CONFIG = yaml.safe_load((Path(__file__).parent / "config.yaml").read_text(encoding="utf-8"))
SILENT_MODE = "--textonly" in sys.argv

audio_queue: "queue.Queue[np.ndarray]" = queue.Queue()

mouth_busy = threading.Event()   # agiz konusurken kulak susar
ack_done = threading.Event()     # "efendim" bitti, kayda baslayabiliriz


# --------------------------------------------------------------------------- MQTT
def mqtt_connect():
    if SILENT_MODE:
        return None
    import paho.mqtt.client as mqtt

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="yaver-ear")
    except AttributeError:  # paho 1.x
        client = mqtt.Client(client_id="yaver-ear")

    def on_status(client_, userdata, msg):
        try:
            packet = json.loads(msg.payload.decode("utf-8"))
        except json.JSONDecodeError:
            return
        if packet.get("module") != "mouth":
            return
        status = packet.get("status")
        if status == "speaking":
            mouth_busy.set()
        elif status in ("done", "ready"):
            mouth_busy.clear()
            if status == "ready":
                ack_done.set()

    def on_connect(client_, userdata, flags, rc, properties=None):
        client_.subscribe(CONFIG["mqtt"]["topic_status"])

    client.on_connect = on_connect
    client.on_message = on_status
    client.connect(CONFIG["mqtt"]["server"], CONFIG["mqtt"]["port"], 60)
    client.loop_start()
    print(f"MQTT baglandi: {CONFIG['mqtt']['server']}:{CONFIG['mqtt']['port']}")
    return client


def publish_status(client, status):
    if client is None:
        return
    client.publish(
        CONFIG["mqtt"]["topic_status"],
        json.dumps({"module": "ear", "status": status}, ensure_ascii=False),
    )


def publish_text(client, text):
    packet = {"text": text, "source": "ear-pc", "timestamp": time.time()}
    if client is None:
        print(f"  [yayinlanmadi] {packet}")
        return
    client.publish(CONFIG["mqtt"]["topic_text"], json.dumps(packet, ensure_ascii=False))


# --------------------------------------------------------------------------- ses
def audio_callback(data, frames, timestamp, status):
    if status:
        print(f"  ses uyarisi: {status}", file=sys.stderr)
    audio_queue.put(data.copy().flatten())


def rms(block):
    return float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))


def measure_ambient(seconds=1.5):
    """Acilista ortam gurultusunu olcup sessizlik esigini belirler."""
    print(f"Ortam gurultusu olculuyor ({seconds} sn, sessiz ol)...")
    blocks = []
    end_time = time.time() + seconds
    while time.time() < end_time:
        try:
            blocks.append(rms(audio_queue.get(timeout=1)))
        except queue.Empty:
            break
    baseline = float(np.median(blocks)) if blocks else 100.0
    threshold = max(baseline * CONFIG["recording"]["silence_multiplier"], 180.0)
    print(f"  ortam: {baseline:.0f}  ->  konusma esigi: {threshold:.0f}")
    return threshold


# --------------------------------------------------------------------------- moduller
def load_whisper():
    from faster_whisper import WhisperModel

    w = CONFIG["whisper"]

    def try_load(device, compute_type):
        model = WhisperModel(w["model"], device=device, compute_type=compute_type)
        # Isitma: 1 sn sessizlik cevir. Hem CUDA'yi burada test etmis oluruz,
        # hem de ilk gercek konusma yavas olmaz.
        list(model.transcribe(np.zeros(16000, dtype=np.float32), language=w["language"])[0])
        return model

    print(f"Whisper yukleniyor: {w['model']} ({w['device']}/{w['compute_type']})...")
    try:
        model = try_load(w["device"], w["compute_type"])
        print(f"  hazir ({w['device']})")
        return model
    except Exception as error:
        if w["device"] == "cpu":
            raise
        print(f"  GPU acilmadi: {error}")
        print("  CPU'ya dusuluyor. Hizlandirmak icin:")
        print("    pip install nvidia-cublas-cu12 nvidia-cudnn-cu12")
        model = try_load("cpu", "int8")
        print("  hazir (cpu/int8)")
        return model


def load_wakeword():
    import openwakeword
    from openwakeword.model import Model

    name = CONFIG["wakeword"]["model"]
    try:
        oww = Model(wakeword_models=[name], inference_framework="onnx")
    except Exception:
        print("Uyandirma modelleri indiriliyor (tek seferlik)...")
        openwakeword.utils.download_models()
        oww = Model(wakeword_models=[name], inference_framework="onnx")
    print(f"Uyandirma modeli hazir: {name}")
    return oww


# --------------------------------------------------------------------------- akis
def record_speech(threshold, pre_roll=None):
    """Sessizlik gelene kadar kaydeder, int16 numpy dizisi dondurur.

    pre_roll: tetiklenmeden onceki bloklar. Cumlenin ilk hecesi bunlarda.
    """
    print("  dinliyorum...", end="", flush=True)
    chunks = list(pre_roll) if pre_roll else []
    silence_blocks = 0
    block_sec = CONFIG["audio"]["block_size"] / CONFIG["audio"]["sample_rate"]
    required_silence = int(CONFIG["recording"]["silence_sec"] / block_sec)
    max_blocks = int(CONFIG["recording"]["max_sec"] / block_sec)
    speech_started = False

    for _ in range(max_blocks):
        block = audio_queue.get()
        chunks.append(block)
        if rms(block) > threshold:
            speech_started = True
            silence_blocks = 0
        elif speech_started:
            silence_blocks += 1
            if silence_blocks >= required_silence:
                break

    print(" bitti.")
    return np.concatenate(chunks) if chunks else np.array([], dtype=np.int16)


# Whisper sessizlik/gurultu uzerine bunlari uydurur. Coplugu at.
JUNK = {
    "them", "you", "thank you", "thanks for watching", "bye",
    "altyazi m.k.", "abone ol", "abone olmayi unutmayin",
    "izlediginiz icin tesekkurler", "tesekkurler", "altyazi",
}


def is_junk(text):
    normalized = text.lower().strip(" .,!?").replace("ı", "i").replace("ş", "s").replace("ğ", "g")
    return normalized in JUNK or len(normalized) < 2


def transcribe(whisper, audio):
    duration = len(audio) / CONFIG["audio"]["sample_rate"]
    if duration < CONFIG["recording"]["min_sec"]:
        return ""
    t0 = time.time()
    segments, info = whisper.transcribe(
        audio.astype(np.float32) / 32768.0,
        language=CONFIG["whisper"]["language"],
        beam_size=1,
        temperature=0.0,
        vad_filter=True,
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
    )
    text = " ".join(s.text.strip() for s in segments).strip()
    print(f"  ({duration:.1f} sn ses -> {time.time() - t0:.2f} sn cevrim)")
    if is_junk(text):
        print(f'  (cop filtrelendi: "{text}")')
        return ""
    return text


def main():
    whisper = load_whisper()
    oww = load_wakeword() if CONFIG["wakeword"]["enabled"] else None
    client = mqtt_connect()

    stream = sd.InputStream(
        device=CONFIG["audio"]["input_device"],
        samplerate=CONFIG["audio"]["sample_rate"],
        channels=1,
        dtype="int16",
        blocksize=CONFIG["audio"]["block_size"],
        callback=audio_callback,
    )

    pre_buffer = deque(maxlen=int(0.6 * CONFIG["audio"]["sample_rate"] / CONFIG["audio"]["block_size"]))

    with stream:
        time.sleep(0.5)  # akisin oturmasini bekle, yoksa olcum sifir cikar
        while not audio_queue.empty():
            audio_queue.get()
        threshold = measure_ambient()
        if oww:
            print(f"\nHazir. '{CONFIG['wakeword']['model']}' de.  (Ctrl+C ile cik)\n")
        else:
            print("\nHazir. Uyandirma kapali - konusmaya basla.  (Ctrl+C ile cik)\n")

        while True:
            block = audio_queue.get()

            # Agiz konusurken hicbir seyi tetikleme, kendi sesini duymasin.
            if mouth_busy.is_set():
                pre_buffer.clear()
                if oww:
                    oww.reset()
                continue

            pre_buffer.append(block)

            if oww:
                scores = oww.predict(block)
                if max(scores.values()) < CONFIG["wakeword"]["threshold"]:
                    continue
                print(f"\n>> uyandim (skor {max(scores.values()):.2f})")
                oww.reset()
                pre_buffer.clear()  # uyandirma kelimesi kayda girmesin

                # Agiza "efendim" dedirt, bitmesini bekle.
                ack_done.clear()
                publish_status(client, "woke")
                if client is not None:
                    ack_done.wait(CONFIG["wakeword"]["ack_wait_sec"])
                    time.sleep(0.15)  # hoparlorun sonlanmasi icin kucuk pay
                while not audio_queue.empty():
                    audio_queue.get()
            else:
                if rms(block) < threshold:
                    continue
                print("\n>> ses algilandi")

            audio = record_speech(threshold, pre_buffer)
            pre_buffer.clear()
            text = transcribe(whisper, audio)

            if text:
                print(f'  ANLADIM: "{text}"')
                publish_text(client, text)
            else:
                print("  (bos, atlandi)")

            time.sleep(CONFIG["wakeword"]["cooldown_sec"] if oww else 0.3)
            while not audio_queue.empty():
                audio_queue.get()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nkulak kapandi.")
