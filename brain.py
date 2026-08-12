"""Yaver - brain module (beyin).

MQTT'den metin alir, llama-server'a sorar, cevabi MQTT'ye yayinlar.
Mikrofonun, hoparlorun, hatta LLM'in nerede oldugunu bilmez.

skills.py varsa (yaver/skill/list yayinliyorsa) LLM'e arac (tool) olarak
tanitilir - LLM bir arac cagirmaya karar verirse istek yaver/skill/request'e
yayinlanir, sonuc yaver/skill/result'tan beklenir, sonra LLM'e geri verilip
asil (seslendirilecek) cevap uretilir. skills.py yoksa bu adim atlanir,
davranis oncekiyle aynidir.

Kullanim:
    python brain.py
    python brain.py --ask "saat kac"    -> MQTT'siz tek soru sorup cikar
"""

import json
import queue
import re
import sys
import threading
import time
import uuid
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

client_ref = [None]        # MQTT istemcisi - --ask modunda None kalir, arac cagirma atlanir
available_tools = []       # skills.py'den ogrenilen guncel arac (tool) listesi
pending_skill_results = {}  # arac istek id -> {"event": Event, "result": dict|None}


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


# --------------------------------------------------------------------------- araclar (skills)
def call_skill(name, params):
    """skills.py'ye MQTT uzerinden istek gonderir, sonucu (timeout'lu) bekler.

    Ayri bir worker thread'inden cagrilir (bkz. main()) - MQTT ag thread'i
    bu sirada serbest kalir, boylece sonuc mesaji gelip event'i tetikleyebilir.
    Ayni thread icinde bekleseydik kilitlenirdi.
    """
    client = client_ref[0]
    if client is None:
        return {"status": "error", "data": "MQTT baglantisi yok"}

    request_id = str(uuid.uuid4())
    entry = {"event": threading.Event(), "result": None}
    pending_skill_results[request_id] = entry
    client.publish(
        CONFIG["skills"]["topic_request"],
        json.dumps({"id": request_id, "name": name, "params": params}, ensure_ascii=False),
    )
    got_result = entry["event"].wait(CONFIG["skills"]["timeout_sec"])
    pending_skill_results.pop(request_id, None)
    if not got_result or entry["result"] is None:
        return {"status": "error", "data": "arac zaman asimina ugradi"}
    return entry["result"]


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


def stream_reply(messages, on_sentence):
    """LLM'den cumle cumle cevap alir, her tamamlanan cumlede on_sentence cagirir."""
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

    return full_text.strip()


def ask(question, on_sentence=None):
    """LLM'e sorar. on_sentence verilirse her tamamlanan cumlede cagirir.

    Arac (tool) varsa once stream'siz bir "karar turu" yapilir: LLM ya
    dogrudan cevap verir ya da bir arac cagirir. Arac cagirirsa sonuc
    call_skill() ile alinir, LLM'e geri verilir, asil (seslendirilecek)
    cevap ikinci - bu sefer stream'li - turda uretilir. Arac yoksa (skills.py
    kapaliysa) bu adim tamamen atlanir, davranis oncekiyle birebir aynidir.
    """
    messages = [{"role": "system", "content": system_prompt()}]
    messages += list(history)
    messages.append({"role": "user", "content": question})

    full_text = None

    if available_tools and client_ref[0] is not None:
        decision_body = {
            "messages": messages,
            "temperature": BRAIN["temperature"],
            "max_tokens": BRAIN["max_tokens"],
            "tools": list(available_tools),
        }
        r = httpx.post(f"{BRAIN['server']}/v1/chat/completions", json=decision_body, timeout=120)
        r.raise_for_status()
        decision = r.json()["choices"][0]["message"]
        tool_calls = decision.get("tool_calls")

        if tool_calls:
            call = tool_calls[0]  # basit demo: tek arac cagrisi islenir
            name = call["function"]["name"]
            try:
                params = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                params = {}
            print(f"  [arac] {name}({params})")
            result = call_skill(name, params)
            print(f"  [arac sonucu] {result}")
            messages.append(decision)
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(result, ensure_ascii=False),
            })
        elif decision.get("content"):
            # Arac gerekmedi, cevap zaten elimizde - ikinci bir tur gerekmez.
            full_text = decision["content"].strip()
            if on_sentence:
                on_sentence(full_text)

    if full_text is None:
        full_text = stream_reply(messages, on_sentence)

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
    client_ref[0] = client

    question_queue = queue.Queue()

    def publish_sentence(sentence):
        print(f"  -> {sentence}")
        client.publish(
            CONFIG["mqtt"]["topic_reply"],
            json.dumps({"text": sentence, "timestamp": time.time()}, ensure_ascii=False),
        )

    def worker():
        while True:
            question = question_queue.get()
            print(f'\nSORU: "{question}"')
            try:
                ask(question, publish_sentence)
            except httpx.HTTPError as error:
                print(f"  LLM hatasi: {error}")
                publish_sentence("Beynime ulasamiyorum, sunucu kapali olabilir.")

    def on_message(client_, userdata, msg):
        if msg.topic == CONFIG["skills"]["topic_result"]:
            try:
                packet = json.loads(msg.payload.decode("utf-8"))
            except json.JSONDecodeError:
                return
            entry = pending_skill_results.get(packet.get("id"))
            if entry:
                entry["result"] = packet
                entry["event"].set()
            return

        if msg.topic == CONFIG["skills"]["topic_list"]:
            try:
                tools = json.loads(msg.payload.decode("utf-8"))
            except json.JSONDecodeError:
                return
            available_tools[:] = tools
            names = [t["function"]["name"] for t in available_tools]
            print(f"  {len(available_tools)} arac ogrenildi: {', '.join(names)}")
            return

        try:
            question = json.loads(msg.payload.decode("utf-8")).get("text", "").strip()
        except json.JSONDecodeError:
            return
        if question:
            question_queue.put(question)

    def on_connect(client_, userdata, flags, rc, properties=None):
        client_.subscribe(CONFIG["mqtt"]["topic_text"])
        client_.subscribe(CONFIG["skills"]["topic_result"])
        client_.subscribe(CONFIG["skills"]["topic_list"])
        print(f"Dinleniyor: {CONFIG['mqtt']['topic_text']}")

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(CONFIG["mqtt"]["server"], CONFIG["mqtt"]["port"], 60)

    read_history()
    threading.Thread(target=worker, daemon=True).start()
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
