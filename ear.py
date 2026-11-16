"""Yaver - ear module (kulak).

Mikrofonu dinler, uyandirma kelimesini yakalar, konusmayi yaziya cevirir
ve MQTT uzerinden yayinlar. Baska hicbir modulun varligini bilmez.

Kullanim:
    python ear.py              -> config.yaml'daki ayarlarla
    python ear.py --textonly   -> MQTT'ye yollamaz, sadece ekrana yazar
    python ear.py --cuda       -> CUDA kutuphane teshisi, sonra cikar
"""

import ctypes
import importlib.util
import json
import os
import queue
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
import sounddevice as sd

from common import guard, load_config, mqtt_client, mqtt_connect, normalize, parse_payload

CONFIG = load_config()
REC = CONFIG["recording"]
BARGE = CONFIG.get("barge_in") or {}
SILENT_MODE = "--textonly" in sys.argv

audio_queue: "queue.Queue[np.ndarray]" = queue.Queue()

mouth_busy = threading.Event()   # agiz konusurken kulak susar
ack_done = threading.Event()     # "efendim" bitti, kayda baslayabiliriz
followup_until = [0.0]           # bu ana kadar "yaver" demeden devam edilebilir (bkz. wakeword.followup_sec)


# --------------------------------------------------------------------------- MQTT
def start_mqtt():
    if SILENT_MODE:
        return None
    client = mqtt_client("yaver-ear")

    @guard
    def on_status(client_, userdata, msg):
        packet = parse_payload(msg)
        if packet is None or packet.get("module") != "mouth":
            return
        status = packet.get("status")
        if status == "speaking":
            mouth_busy.set()
        elif status in ("done", "ready"):
            mouth_busy.clear()
            if status == "ready":
                ack_done.set()
            else:
                # Yaver sustu: kisa bir sure tetik kelimesiz takip cumlesi kabul et
                followup_until[0] = time.time() + CONFIG["wakeword"].get("followup_sec", 0)

    def on_connect(client_, userdata, flags, rc, properties=None):
        client_.subscribe(CONFIG["mqtt"]["topic_status"])

    client.on_connect = on_connect
    client.on_message = on_status
    mqtt_connect(client, CONFIG)
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


def publish_stop(client):
    """Soz kesme: mouth sussun, brain cevabi yarida biraksin."""
    if client is None:
        print("  [yayinlanmadi] dur komutu")
        return
    client.publish(CONFIG["mqtt"]["topic_control"], json.dumps({"command": "stop", "source": "ear"}))


def publish_text(client, text):
    packet = {"text": text, "source": CONFIG["audio"]["source_name"], "timestamp": time.time()}
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


def measure_ambient():
    """Acilista ortam gurultusunu olcup sessizlik esigini belirler."""
    seconds = REC["ambient_sec"]
    print(f"Ortam gurultusu olculuyor ({seconds} sn, sessiz ol)...")
    blocks = []
    end_time = time.time() + seconds
    while time.time() < end_time:
        try:
            blocks.append(rms(audio_queue.get(timeout=1)))
        except queue.Empty:
            break
    if not blocks:
        print(f"  ses gelmedi, alt sinir kullaniliyor: {REC['min_threshold']:.0f}")
        return float(REC["min_threshold"])
    baseline = float(np.median(blocks))
    threshold = max(baseline * REC["silence_multiplier"], REC["min_threshold"])
    print(f"  ortam: {baseline:.0f}  ->  konusma esigi: {threshold:.0f}")
    return threshold


# --------------------------------------------------------------------------- CUDA
def nvidia_lib_dirs():
    """pip ile kurulan nvidia-* paketlerinin (orn. nvidia-cublas-cu12)
    kutuphane klasorleri. Kurulum yolu isletim sistemine ve venv duzenine
    gore degistigi icin sabit yol yazmiyoruz: 'nvidia' paketini Python'a
    sorup altinda .dll/.so iceren HER klasoru buluyoruz."""
    spec = importlib.util.find_spec("nvidia")
    if spec is None or not spec.submodule_search_locations:
        return []
    dirs = set()
    for root in spec.submodule_search_locations:
        for pattern in ("*.dll", "*.so*"):
            dirs.update(p.parent for p in Path(root).rglob(pattern))
    return sorted(dirs)


def prepare_cuda_libraries(verbose=False):
    """CTranslate2'nin (Whisper motoru) aradigi CUDA kutuphanelerini
    (libcublas.so.12 / cublas64_12.dll) pip'le kurulan nvidia paketlerinden
    tanitir - faster_whisper import edilmeden ONCE cagrilmali.

    Sistemdeki CUDA surumu farkli olabilir (orn. sadece libcublas.so.13 var)
    ve pip paketleri arama yoluna girmez. Windows'ta klasorler DLL arama
    yoluna eklenir. Linux'ta LD_LIBRARY_PATH surec basladiktan sonra
    degistirilemiyor, bu yuzden kutuphaneleri onceden RTLD_GLOBAL ile
    yukluyoruz - CTranslate2 sonra ayni adi istediginde zaten yuklu olani
    kullanir."""
    dirs = nvidia_lib_dirs()
    if sys.platform == "win32":
        for d in dirs:
            try:
                os.add_dll_directory(str(d))
            except OSError:
                pass
        # Bazi yukleyiciler add_dll_directory yerine PATH'e bakar, ikisini de doldur.
        if dirs:
            os.environ["PATH"] = os.pathsep.join(str(d) for d in dirs) + os.pathsep + os.environ["PATH"]
        loaded = [str(d) for d in dirs]
    else:
        # Kutuphaneler birbirine bagli (libcublas -> libcublasLt). Sirayi
        # bilmeden: yuklenemeyenleri tekrar dene, ilerleme olmayinca dur.
        pending = sorted(p for d in dirs for p in d.glob("lib*.so.*"))
        loaded = []
        while pending:
            failed = []
            for lib in pending:
                try:
                    ctypes.CDLL(str(lib), mode=ctypes.RTLD_GLOBAL)
                    loaded.append(lib.name)
                except OSError:
                    failed.append(lib)
            if len(failed) == len(pending):
                break
            pending = failed
    if verbose:
        print(f"nvidia kutuphane klasorleri: {len(dirs)}")
        for d in dirs:
            print("  +", d)
        print("yuklenen:", ", ".join(loaded) or "-")


# --------------------------------------------------------------------------- moduller
def load_whisper():
    w = CONFIG["whisper"]
    if w["device"] != "cpu":
        prepare_cuda_libraries()
    from faster_whisper import WhisperModel


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
        print("    pip install nvidia-cublas-cu12   (teshis: python ear.py --cuda)")
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
def record_speech(threshold, pre_roll=None, abort_on_mouth=True):
    """Sessizlik gelene kadar kaydeder, int16 numpy dizisi dondurur.

    pre_roll: tetiklenmeden onceki bloklar. Cumlenin ilk hecesi bunlarda.
    abort_on_mouth=False: soz kesme kaydi - agiz konusurken de kaydet.
    """
    print("  dinliyorum...", end="", flush=True)
    chunks = list(pre_roll) if pre_roll else []
    silence_blocks = 0
    resets = 0  # sessizlik sayaci kac kez bir gurultu patlamasiyla sifirlandi (teshis icin)
    block_sec = CONFIG["audio"]["block_size"] / CONFIG["audio"]["sample_rate"]
    required_silence = int(REC["silence_sec"] / block_sec)
    max_blocks = int(REC["max_sec"] / block_sec)
    speech_started = False

    for _ in range(max_blocks):
        block = audio_queue.get()
        # Kayit sirasinda agiz konusmaya basladiysa (orn. onceki sorunun
        # cevabi geldi) kaydi at: yoksa Yaver'in kendi sesi kayda girer,
        # cevabinda "Yaver" geciyorsa kendi kendini tetikleyebilirdi.
        if abort_on_mouth and mouth_busy.is_set():
            print(" iptal (agiz konusmaya basladi).")
            return np.array([], dtype=np.int16)
        chunks.append(block)
        if rms(block) > threshold:
            if speech_started and silence_blocks > 0:
                resets += 1
            speech_started = True
            silence_blocks = 0
        elif speech_started:
            silence_blocks += 1
            if silence_blocks >= required_silence:
                break

    print(f" bitti.{f' ({resets} kez gurultu sessizligi kesti)' if resets else ''}")
    return np.concatenate(chunks) if chunks else np.array([], dtype=np.int16)


# Whisper sessizlik/gurultu uzerine bunlari uydurur - liste config.yaml'da.
JUNK = {normalize(phrase) for phrase in CONFIG["whisper"]["junk_phrases"]}


def is_junk(text):
    normalized = normalize(text)
    return normalized in JUNK or len(normalized) < 2


TRIGGER_PATTERN = re.compile(r"\b" + re.escape(CONFIG["wakeword"]["trigger_word"]), re.IGNORECASE)


def contains_trigger(text):
    """Cumle icinde herhangi bir yerde tetik kelimesi geciyor mu (uyandirma
    modeli kapaliyken kullanilir - bkz. config.yaml wakeword.enabled)."""
    return bool(TRIGGER_PATTERN.search(text))


STOP_WORDS = {normalize(w) for w in BARGE.get("stop_words") or []}


def is_stop_command(text):
    """Yaver konusurken duyulan cumle bir "dur" komutu mu? HEM tetik kelime
    HEM bir dur kelimesi gecmeli - hoparlorden mikrofona sizan Yaver'in kendi
    sesi tek basina "dur" icerebilir, kendi kendini susturmasin."""
    words = normalize(text).split()
    return contains_trigger(text) and any(w in STOP_WORDS for w in words)


def accept_utterance(text, has_wakeword, now, followup_deadline):
    """Taninan cumle brain'e gonderilsin mi? Uyandirma kelimesiyle
    baslatildiysa ya da cumlede tetik kelime geciyorsa evet; Yaver az once
    konustuysa (takip penceresi) tetik kelime de gerekmez."""
    return has_wakeword or contains_trigger(text) or now < followup_deadline


def transcribe(whisper, audio):
    w = CONFIG["whisper"]
    duration = len(audio) / CONFIG["audio"]["sample_rate"]
    if duration < REC["min_sec"]:
        return ""
    t0 = time.time()
    segments, info = whisper.transcribe(
        audio.astype(np.float32) / 32768.0,
        language=w["language"],
        beam_size=w["beam_size"],
        temperature=w["temperature"],
        vad_filter=w["vad_filter"],
        condition_on_previous_text=False,
        no_speech_threshold=w["no_speech_threshold"],
        # Nadir/yabanci kelimeler ("Yaver", "LED" gibi) Whisper tarafindan siklikla
        # benzer sesli yaygin kelimelere cevriliyordu (Ya var, Lady, L'de...).
        # hotwords bunlarin taninma olasiligini artirir - bkz. config.yaml.
        hotwords=w["hotwords"],
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
    client = start_mqtt()

    stream = sd.InputStream(
        device=CONFIG["audio"]["input_device"],
        samplerate=CONFIG["audio"]["sample_rate"],
        channels=1,
        dtype="int16",
        blocksize=CONFIG["audio"]["block_size"],
        callback=audio_callback,
    )

    pre_buffer = deque(maxlen=int(REC["pre_roll_sec"] * CONFIG["audio"]["sample_rate"] / CONFIG["audio"]["block_size"]))

    with stream:
        time.sleep(CONFIG["audio"]["settle_sec"])  # akisin oturmasini bekle, yoksa olcum sifir cikar
        while not audio_queue.empty():
            audio_queue.get()
        threshold = measure_ambient()
        if oww:
            print(f"\nHazir. '{CONFIG['wakeword']['model']}' de.  (Ctrl+C ile cik)\n")
        else:
            print(f"\nHazir. Konusmaya basla, cumlende '{CONFIG['wakeword']['trigger_word']}' "
                  f"gecerse cevap veririm.  (Ctrl+C ile cik)\n")

        while True:
            block = audio_queue.get()

            # Agiz konusurken normal tetikleme yok (kendi sesini duymasin).
            # Soz kesme acikken sadece yuksek sesli konusma kaydedilip
            # "Yaver dur" olup olmadigina bakilir.
            if mouth_busy.is_set():
                if oww:
                    oww.reset()
                pre_buffer.append(block)
                if (not oww and BARGE.get("enabled")
                        and rms(block) > threshold * BARGE["threshold_multiplier"]):
                    print("\n>> konusurken ses algilandi (soz kesme?)")
                    audio = record_speech(threshold * BARGE["threshold_multiplier"], pre_buffer,
                                          abort_on_mouth=False)
                    pre_buffer.clear()
                    text = transcribe(whisper, audio)
                    if text and is_stop_command(text):
                        print(f'  DUR: "{text}"')
                        publish_stop(client)
                    elif text:
                        print(f'  (soz kesme degil: "{text}")')
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
                    time.sleep(CONFIG["wakeword"]["ack_tail_sec"])  # hoparlorun sonlanmasi icin kucuk pay
                while not audio_queue.empty():
                    audio_queue.get()
            else:
                if rms(block) < threshold:
                    continue
                print("\n>> ses algilandi")
            heard_at = time.time()  # takip penceresi konusmanin BASLADIGI ana gore

            publish_status(client, "listening")
            audio = record_speech(threshold, pre_buffer)
            publish_status(client, "idle")
            pre_buffer.clear()
            text = transcribe(whisper, audio)

            if not text:
                print("  (bos, atlandi)")
            elif accept_utterance(text, bool(oww), heard_at, followup_until[0]):
                print(f'  ANLADIM: "{text}"')
                followup_until[0] = 0.0  # pencere tek cumlelik; Yaver cevap verince yeniden acilir
                publish_text(client, text)
            else:
                print(f'  (tetik kelime yok, atlandi: "{text}")')

            time.sleep(CONFIG["wakeword"]["cooldown_sec"] if oww else REC["cooldown_sec"])
            while not audio_queue.empty():
                audio_queue.get()


if __name__ == "__main__":
    if "--cuda" in sys.argv:
        prepare_cuda_libraries(verbose=True)
        sys.exit(0)
    try:
        main()
    except KeyboardInterrupt:
        print("\nkulak kapandi.")
