"""Yaver - mouth module (agiz).

MQTT'den cevap metni alir, Piper ile seslendirir, hoparlorden calar.
Cumleler sirayla calinir, ust uste binmez.

Iki thread: biri sentezler, digeri calar. Bir cumle calarken siradaki
zaten sentezleniyor - cumleler arasinda sentez suresi kadar sessizlik
kalmiyor. yaver/control "stop" gelince calma aninda kesilir.

Kullanim:
    python mouth.py
    python mouth.py --say "merhaba ben yaver"   -> MQTT'siz tek cumle soyle
"""

import io
import json
import queue
import random
import sys
import threading
import time
import wave

import numpy as np
import sounddevice as sd

from common import (guard, load_config, mqtt_client, mqtt_connect, parse_payload, resolve_path,
                    strip_emoji, text_field)

CONFIG = load_config()
MOUTH = CONFIG["mouth"]

speech_queue: "queue.Queue[dict]" = queue.Queue()   # metin isleri -> sentez thread'i
audio_queue: "queue.Queue[dict]" = queue.Queue()    # hazir ses -> calma thread'i
stop_flag = threading.Event()   # soz kesildi: calani kes, kuyruklari bosalt
generation = [0]                # her soz kesmede artar; eski kusaga ait isler (yolda olan sentez) atilir
muted_until = [0.0]             # soz kesildikten sonra yarim cevabin gec gelen cumlelerini yut
ack_audio = []       # [(int16 dizi, ornekleme)] - acilista hazirlanir
client_ref = [None]  # calisan is parcacigi buradan yayin yapar


def load_voice():
    from piper import PiperVoice

    path = resolve_path(MOUTH["voice_model"])
    if not path.exists():
        raise SystemExit(
            f"Ses modeli bulunamadi: {path}\n"
            "tr_TR-dfki-medium.onnx ve .onnx.json dosyalarini indirip "
            "config.yaml icindeki yolu duzelt."
        )
    print(f"Ses modeli yukleniyor: {path.name}")
    return PiperVoice.load(str(path))


def synthesize(voice, text):
    """Metni int16 dizi + ornekleme hizina cevirir.

    Piper'in Python API'si surumden surume degisti, ucunu de deniyoruz.
    """
    # Yeni API (2.x): synthesize(metin, syn_config) -> AudioChunk ureteci.
    # syn_config: config.yaml'daki mouth.synthesis ayarlari - dinleyerek
    # secildi (bkz. "3-cok temiz+deterministik" varyanti), varsayilan
    # degerlerden daha net/dogal geldi.
    try:
        from piper.config import SynthesisConfig
        syn_config = SynthesisConfig(**MOUTH["synthesis"])
        chunks, rate = [], None
        for chunk in voice.synthesize(text, syn_config=syn_config):
            chunks.append(np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16))
            rate = chunk.sample_rate
        if chunks:
            return np.concatenate(chunks), rate
    except (TypeError, ImportError):
        pass  # eski API iki arguman ister ya da piper.config yok, asagi dusuyoruz

    # Eski API (1.x): wave dosyasina yazar
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        if hasattr(voice, "synthesize_wav"):
            voice.synthesize_wav(text, wav)
        else:
            voice.synthesize(text, wav)
    buffer.seek(0)
    with wave.open(buffer, "rb") as wav:
        return np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16), wav.getframerate()


def play(data, rate):
    if MOUTH["volume"] != 1.0:
        data = np.clip(data * MOUTH["volume"], -32768, 32767).astype(np.int16)
    sd.play(data, rate, device=MOUTH["output_device"])
    sd.wait()


def speak(voice, text):
    text = strip_emoji(text)
    if not text:
        return
    play(*synthesize(voice, text))


def prepare_acks(voice):
    """Onay cumlelerini acilista sentezleyip bellekte tutar - gecikme sifir olur."""
    for phrase in MOUTH.get("ack_phrases") or []:
        try:
            ack_audio.append(synthesize(voice, phrase))
        except Exception as error:
            print(f"  onay hazirlanamadi ({phrase}): {error}")
    print(f"  {len(ack_audio)} onay cumlesi hazir")


def publish_status(status):
    if client_ref[0] is None:
        return
    client_ref[0].publish(
        CONFIG["mqtt"]["topic_status"],
        json.dumps({"module": "mouth", "status": status}, ensure_ascii=False),
    )


def drain(q):
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


def synth_worker(voice):
    """Metni sese cevirip calma kuyruguna koyar - calma ile paralel."""
    while True:
        job = speech_queue.get()
        if job["gen"] != generation[0]:
            continue
        try:
            if job["type"] == "ack":
                audio = random.choice(ack_audio) if ack_audio else None
            else:
                text = strip_emoji(job["content"])
                audio = synthesize(voice, text) if text else None
        except Exception as error:
            print(f"  seslendirme hatasi: {error}")
            audio = None
        audio_queue.put({**job, "audio": audio})


def play_worker():
    while True:
        job = audio_queue.get()
        if job["gen"] != generation[0]:
            continue
        publish_status("speaking")
        while True:
            try:
                if job["type"] == "text":
                    print(f'  soyluyorum: "{job["content"]}"')
                if job["audio"] is not None:
                    play(*job["audio"])
            except Exception as error:
                print(f"  calma hatasi: {error}")
            if job["type"] == "ack":
                publish_status("ready")   # kulak bu sinyali bekliyor
            if stop_flag.is_set():
                break
            # brain cumle cumle yayinliyor, kuyruk anlik bos olsa bile bir
            # sonraki cumle hemen ardindan gelebilir - "done" demeden once
            # kisa bir sure bekle, yoksa ear erken "mouth sustu" sanip
            # mouth'un kendi sesini dinlemeye baslar (geri besleme dongusu).
            try:
                job = audio_queue.get(timeout=MOUTH["reply_gap_sec"])
            except queue.Empty:
                break
            if stop_flag.is_set() or job["gen"] != generation[0]:
                break
        publish_status("done")


def stop_speaking():
    """Soz kesme (MQTT thread'inden): calani durdur, bekleyenleri at."""
    stop_flag.set()
    generation[0] += 1
    muted_until[0] = time.time() + (CONFIG.get("barge_in") or {}).get("mute_sec", 3.0)
    drain(speech_queue)
    drain(audio_queue)
    sd.stop()
    print("  [kontrol] sustum")
    # play_worker bu bayragi gorup "done" yayinlar; kisa bir sure sonra
    # bayrak kalkar ki yeni cevaplar calinabilsin.
    threading.Timer(0.3, stop_flag.clear).start()


def main():
    voice = load_voice()
    prepare_acks(voice)
    threading.Thread(target=synth_worker, args=(voice,), daemon=True).start()
    threading.Thread(target=play_worker, daemon=True).start()

    client = mqtt_client("yaver-mouth")
    client_ref[0] = client
    # Son vasiyet (LWT): agiz konusurken cokerse "done" hic yayinlanmaz ve
    # ear "agiz konusuyor" sanip sonsuza kadar sagir kalirdi. Baglanti
    # koparsa broker "done"u agiz adina kendisi yayinlar.
    client.will_set(
        CONFIG["mqtt"]["topic_status"],
        json.dumps({"module": "mouth", "status": "done"}),
    )

    @guard
    def on_message(client_, userdata, msg):
        packet = parse_payload(msg)
        if packet is None:
            return
        if msg.topic == CONFIG["mqtt"]["topic_control"]:
            if packet.get("command") == "stop":
                stop_speaking()
            return
        if msg.topic == CONFIG["mqtt"]["topic_status"]:
            if packet.get("module") == "ear" and packet.get("status") == "woke":
                speech_queue.put({"type": "ack", "gen": generation[0]})
            elif packet.get("module") == "brain" and packet.get("status") in ("done", "thinking"):
                # Kesilen turun cumleleri brain'in "done"undan ONCE gelir; yeni bir
                # soru ("thinking") ya da turun bitisi yutmayi sonlandirir.
                muted_until[0] = 0.0
            return
        text = text_field(packet)
        if text:
            if time.time() < muted_until[0]:
                print(f'  (soz kesildi, atlandi: "{text}")')
                return
            speech_queue.put({"type": "text", "content": text, "gen": generation[0]})

    def on_connect(client_, userdata, flags, rc, properties=None):
        client_.subscribe(CONFIG["mqtt"]["topic_reply"])
        client_.subscribe(CONFIG["mqtt"]["topic_status"])
        client_.subscribe(CONFIG["mqtt"]["topic_control"])
        print(f"Dinleniyor: {CONFIG['mqtt']['topic_reply']} + {CONFIG['mqtt']['topic_status']}")

    client.on_connect = on_connect
    client.on_message = on_message
    mqtt_connect(client, CONFIG)
    print("Agiz hazir.")
    client.loop_forever()


if __name__ == "__main__":
    if "--say" in sys.argv:
        phrase = " ".join(sys.argv[sys.argv.index("--say") + 1:])
        speak(load_voice(), phrase)
    else:
        try:
            main()
        except KeyboardInterrupt:
            print("\nagiz kapandi.")
