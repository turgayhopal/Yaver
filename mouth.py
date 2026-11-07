"""Yaver - mouth module (agiz).

MQTT'den cevap metni alir, Piper ile seslendirir, hoparlorden calar.
Cumleler sirayla calinir, ust uste binmez.

Kullanim:
    python mouth.py
    python mouth.py --say "merhaba ben yaver"   -> MQTT'siz tek cumle soyle
"""

import io
import json
import queue
import random
import re
import sys
import threading
import wave

import numpy as np
import sounddevice as sd

from common import guard, load_config, mqtt_client, mqtt_connect, parse_payload, resolve_path, text_field

CONFIG = load_config()
MOUTH = CONFIG["mouth"]

speech_queue: "queue.Queue[dict]" = queue.Queue()
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


# LLM sistem promptunda emoji kullanma diyor ama bazen yine de sizdiriyor,
# Piper bunlari tuhaf sekilde okumaya calisiyor - seslendirmeden once temizle.
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U00002300-\U000023FF"
    "\U0001F1E6-\U0001F1FF"
    "\U0000FE0F\U0000200D"
    "]+",
    flags=re.UNICODE,
)


def strip_emoji(text):
    text = EMOJI_PATTERN.sub("", text)
    return re.sub(r" {2,}", " ", text).strip()


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


def worker(voice):
    while True:
        job = speech_queue.get()
        publish_status("speaking")
        while True:
            try:
                if job["type"] == "ack":
                    if ack_audio:
                        play(*random.choice(ack_audio))
                    publish_status("ready")   # kulak bu sinyali bekliyor
                else:
                    print(f'  soyluyorum: "{job["content"]}"')
                    speak(voice, job["content"])
            except Exception as error:
                print(f"  seslendirme hatasi: {error}")
                if job["type"] == "ack":
                    publish_status("ready")
            # brain cumle cumle yayinliyor, kuyruk anlik bos olsa bile bir
            # sonraki cumle hemen ardindan gelebilir - "done" demeden once
            # kisa bir sure bekle, yoksa ear erken "mouth sustu" sanip
            # mouth'un kendi sesini dinlemeye baslar (geri besleme dongusu).
            try:
                job = speech_queue.get(timeout=MOUTH["reply_gap_sec"])
            except queue.Empty:
                break
        publish_status("done")


def main():
    voice = load_voice()
    prepare_acks(voice)
    threading.Thread(target=worker, args=(voice,), daemon=True).start()

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
        if msg.topic == CONFIG["mqtt"]["topic_status"]:
            if packet.get("module") == "ear" and packet.get("status") == "woke":
                speech_queue.put({"type": "ack"})
            return
        text = text_field(packet)
        if text:
            speech_queue.put({"type": "text", "content": text})

    def on_connect(client_, userdata, flags, rc, properties=None):
        client_.subscribe(CONFIG["mqtt"]["topic_reply"])
        client_.subscribe(CONFIG["mqtt"]["topic_status"])
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
