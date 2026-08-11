"""Yaver - brain module (beyin).

MQTT'den metin alir, llama-server'a sorar, cevabi MQTT'ye yayinlar.
Mikrofonun, hoparlorun, hatta LLM'in nerede oldugunu bilmez.

Kullanim:
    python brain.py
    python brain.py --ask "saat kac"    -> MQTT'siz tek soru sorup cikar
"""

import json
import re
import sys
import time
from collections import deque
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
BRAIN = CONFIG["brain"]

MEMORY_FILE = ROOT / "memory.json"    # kalici bilgiler (ad, tercihler)
HISTORY_FILE = ROOT / "history.json"  # son konusma turlari

history = deque(maxlen=BRAIN["history_turns"] * 2)


# --------------------------------------------------------------------------- hafiza
def read_memory():
    if not MEMORY_FILE.exists():
        return {}
    try:
        return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def read_history():
    if HISTORY_FILE.exists():
        try:
            for m in json.loads(HISTORY_FILE.read_text(encoding="utf-8")):
                history.append(m)
        except json.JSONDecodeError:
            pass


def write_history():
    HISTORY_FILE.write_text(
        json.dumps(list(history), ensure_ascii=False, indent=1), encoding="utf-8"
    )


def system_prompt():
    """Tarih/saat KASITLI OLARAK yok - kucuk model bu bilgiyi context'te gorunce
    sorulmadan sohbete sikistirip sacmalamaya basliyor. Zaman gercekten gerekince
    skills.py ile gercek bir arac (tool call) olarak eklenecek, korlemesine
    context'e gomulmeyecek."""
    text = BRAIN["system_prompt"].strip()
    known = read_memory()
    if known:
        lines = "\n".join(f"- {k}: {v}" for k, v in known.items())
        text += f"\n\nKullanici hakkinda bildiklerin:\n{lines}"
    return text


# --------------------------------------------------------------------------- LLM
SENTENCE_END = re.compile(r"(?<=[.!?:;])\s+|\n+")


def split_sentence(buffer):
    """Tamponda tamamlanmis cumle varsa (cumle, kalan) dondurur."""
    match = SENTENCE_END.search(buffer)
    if not match:
        return None, buffer
    cut = match.end()
    sentence = buffer[:cut].strip()
    if len(sentence) < 12:  # cok kisa parca, biraz daha bekle
        return None, buffer
    return sentence, buffer[cut:]


def ask(question, on_sentence=None):
    """LLM'e sorar. on_sentence verilirse her tamamlanan cumlede cagirir."""
    messages = [{"role": "system", "content": system_prompt()}]
    messages += list(history)
    messages.append({"role": "user", "content": question})

    body = {
        "messages": messages,
        "temperature": BRAIN["temperature"],
        "max_tokens": BRAIN["max_tokens"],
        "stream": bool(BRAIN["stream"] and on_sentence),
    }

    t0 = time.time()
    full_text = ""

    if not body["stream"]:
        r = httpx.post(f"{BRAIN['server']}/v1/chat/completions", json=body, timeout=120)
        r.raise_for_status()
        full_text = r.json()["choices"][0]["message"]["content"].strip()
        if on_sentence:
            on_sentence(full_text)
    else:
        buffer = ""
        first_token_time = None
        with httpx.stream(
            "POST", f"{BRAIN['server']}/v1/chat/completions", json=body, timeout=120
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0]["delta"].get("content", "")
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                if not delta:
                    continue
                if first_token_time is None:
                    first_token_time = time.time() - t0
                buffer += delta
                full_text += delta
                while True:
                    sentence, buffer = split_sentence(buffer)
                    if not sentence:
                        break
                    on_sentence(sentence)
        if buffer.strip():
            on_sentence(buffer.strip())
        if first_token_time:
            print(f"  (ilk token {first_token_time:.2f} sn, toplam {time.time() - t0:.2f} sn)")

    full_text = full_text.strip()
    history.append({"role": "user", "content": question})
    history.append({"role": "assistant", "content": full_text})
    write_history()
    return full_text


# --------------------------------------------------------------------------- MQTT
def main():
    import paho.mqtt.client as mqtt

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="yaver-brain")
    except AttributeError:
        client = mqtt.Client(client_id="yaver-brain")

    def publish_sentence(sentence):
        print(f"  -> {sentence}")
        client.publish(
            CONFIG["mqtt"]["topic_reply"],
            json.dumps({"text": sentence, "timestamp": time.time()}, ensure_ascii=False),
        )

    def on_message(client_, userdata, msg):
        try:
            question = json.loads(msg.payload.decode("utf-8")).get("text", "").strip()
        except json.JSONDecodeError:
            return
        if not question:
            return
        print(f'\nSORU: "{question}"')
        try:
            ask(question, publish_sentence)
        except httpx.HTTPError as error:
            print(f"  LLM hatasi: {error}")
            publish_sentence("Beynime ulasamiyorum, sunucu kapali olabilir.")

    def on_connect(client_, userdata, flags, rc, properties=None):
        client_.subscribe(CONFIG["mqtt"]["topic_text"])
        print(f"Dinleniyor: {CONFIG['mqtt']['topic_text']}")

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(CONFIG["mqtt"]["server"], CONFIG["mqtt"]["port"], 60)

    read_history()
    print(f"Beyin hazir. LLM: {BRAIN['server']}  (gecmis: {len(history)//2} tur)")
    client.loop_forever()


if __name__ == "__main__":
    if "--ask" in sys.argv:
        read_history()
        question = " ".join(sys.argv[sys.argv.index("--ask") + 1:])
        print(ask(question, lambda s: print(f"  -> {s}")))
    else:
        try:
            main()
        except KeyboardInterrupt:
            print("\nbeyin kapandi.")
