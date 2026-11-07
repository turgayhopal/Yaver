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

import httpx

from common import ROOT, guard, load_config, mqtt_client, mqtt_connect, parse_payload, text_field

# LLM bazen (nadiren) sistem promptunun yasagina ragmen emoji uretiyor (bkz.
# "Bilinen model tuhafligi"). Windows konsolu varsayilan kod sayfasi (orn.
# cp1254) bunu print() ile yazdiramayip UnicodeEncodeError firlatiyordu - bu
# da worker thread'ini SESSIZCE oldurup Yaver'i bir daha hic cevap vermez
# hale getiriyordu (yasanmis ve teshis edilmis bir cokme). errors="replace"
# ile boyle karakterler konsolda "?" olarak gorunur ama surec asla cokmez.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CONFIG = load_config()
BRAIN = CONFIG["brain"]
LLM = CONFIG["llm"]

# Hangi LLM'in kullanilacagi tamamen config.yaml -> llm.provider'da. Simdilik
# sadece "local" (OpenAI uyumlu yerel sunucu: Ollama, llama.cpp...) var;
# ileride bir internet servisi (orn. claude) ayni ayarla secilecek.
if LLM["provider"] != "local":
    raise SystemExit(f"llm.provider '{LLM['provider']}' henuz desteklenmiyor (simdilik sadece 'local').")
LOCAL = LLM["local"]
CHAT_URL = f"{LOCAL['server']}/v1/chat/completions"

MEMORY_FILE = ROOT / "memory.json"    # kalici bilgiler (ad, tercihler)
HISTORY_FILE = ROOT / "history.json"  # son konusma turlari

history = deque(maxlen=BRAIN["history_turns"])  # her eleman bir TURUN mesaj listesi
# (tek mesaj degil) - boylece bir arac turu (4 mesaj: soru, arac cagrisi, arac
# sonucu, cevap) sinir asilinca ortadan kesilip yetim bir "tool" mesaji birakmaz.

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
    if not HISTORY_FILE.exists():
        return
    try:
        flat = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    turn = []
    for m in flat:
        if m.get("role") == "user" and turn:
            history.append(turn)
            turn = []
        turn.append(m)
    if turn:
        history.append(turn)


def write_history():
    flat = [m for turn in history for m in turn]
    HISTORY_FILE.write_text(
        json.dumps(flat, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def system_prompt(tools_enabled=True):
    """Tarih/saat KASITLI OLARAK yok - kucuk model bu bilgiyi context'te gorunce
    sorulmadan sohbete sikistirip sacmalamaya basliyor. Zaman gercekten gerekince
    skills.py ile gercek bir arac (tool call) olarak eklenecek, korlemesine
    context'e gomulmeyecek.

    tools_enabled=False (skills kapali): model hicbir arac gormedigi halde
    "LED'i sondurdum" diye uyduruyordu (yasanmis) - acikca yapamayacagi
    soylenir. Bu not sadece araclarin durumu degisince degisir, yani onek
    onbellegi her turda degil sadece o anda bir kez yenilenir."""
    text = BRAIN["system_prompt"].strip()
    if not tools_enabled:
        text += "\n" + BRAIN["no_tools_note"].strip()
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
# Kalici bir Client - her istekte yeniden baglanti kurmak (TCP handshake) yerine
# ayni baglantiyi (keep-alive) tekrar kullanir. httpx.post/httpx.stream modul
# fonksiyonlari her cagrida kendi gecici Client'ini acip kapatiyordu - localhost
# uzerinde bile bu olculebilir bir gecikme ekliyordu (bkz. config.yaml'daki
# 127.0.0.1 notu, ayni turden bir maliyet).
http_client = httpx.Client(timeout=LOCAL["timeout_sec"])


def request_body(messages, **fields):
    """Her LLM isteginin ortak govdesi. model: Ollama hangi modeli
    kullanacagini bundan bilir (llama.cpp tek modelli, yok sayar).
    request_options: config.yaml'dan sunucuya ozel ek alanlar."""
    body = {
        "model": LOCAL["model"],
        "messages": messages,
        "temperature": BRAIN["temperature"],
        "max_tokens": BRAIN["max_tokens"],
        **fields,
    }
    body.update(LOCAL.get("request_options") or {})
    return body


SENTENCE_END = re.compile(r"(?<=[.!?:;])\s+|\n+")


def split_sentence(buffer):
    """Tamponda tamamlanmis cumle varsa (cumle, kalan) dondurur.

    Cok kisa bir parca ("Tamam.") tek basina yayinlanmaz, bir SONRAKI cumle
    sonuyla birlestirilir ("Tamam. Lamba acik."). Onceden sadece ILK cumle
    sonuna bakiliyordu: o kisaysa sonrakilere hic bakilmiyor, cevabin
    tamami LLM bitene kadar bekliyordu - akisin tum faydasi kayboluyordu.
    """
    for match in SENTENCE_END.finditer(buffer):
        sentence = buffer[:match.end()].strip()
        if len(sentence) >= BRAIN["min_sentence_chars"]:
            return sentence, buffer[match.end():]
    return None, buffer


def iter_deltas(body):
    """LLM'e POST edip SSE akisindaki her 'delta' sozlugunu tek tek uretir.

    stream_reply VE stream_decision'in ikisi de ayni SSE okuma/ayristirma
    mantigini (satir satir oku, "data: " on ekini at, [DONE]'da dur, bozuk
    JSON'u atla) birebir tekrarliyordu - iki kopya birbirinden sapinca
    (buffer'i full_text'e tekrar ekleme hatasi gibi) hata yasandi. Artik TEK
    yerde; cagiran taraf sadece delta.get("content")/["tool_calls"] okur.
    """
    with http_client.stream("POST", CHAT_URL, json=body) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data.strip() == "[DONE]":
                break
            try:
                yield json.loads(data)["choices"][0]["delta"]
            except (json.JSONDecodeError, KeyError, IndexError):
                continue


def stream_reply(messages, on_sentence):
    """LLM'den cumle cumle cevap alir, her tamamlanan cumlede on_sentence cagirir."""
    body = request_body(messages, stream=bool(BRAIN["stream"] and on_sentence))

    t0 = time.time()
    full_text = ""

    if not body["stream"]:
        r = http_client.post(CHAT_URL, json=body)
        r.raise_for_status()
        full_text = r.json()["choices"][0]["message"]["content"].strip()
        if on_sentence:
            on_sentence(full_text)
    else:
        buffer = ""
        first_token_time = None
        for delta in iter_deltas(body):
            content = delta.get("content")
            if not content:
                continue
            if first_token_time is None:
                first_token_time = time.time() - t0
            buffer += content
            full_text += content
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


def stream_decision(messages, on_sentence):
    """Arac (tool) secenegi acikken 'karar turunu' STREAM'LI atar.

    Onceden bu tur stream'sizdi: arac gerekmediginde LLM'in urettigi TUM
    cevap (max_tokens'a kadar) once tamamen bekleniyor, sonra tek seferde
    yayinlaniyordu - streaming'in ilk-cumle-hemen-konus avantajini tamamen
    yok ediyordu (yaklasik 2-3 sn ekstra sessizlik). Simdi ayni SSE akisini
    stream_reply ile ayni mantikla okuyoruz: icerik parcalari geldikce
    cumle cumle on_sentence'a veriyoruz, tool_calls parcalarini ise
    biriktirip sonda birlestiriyoruz (Piper'a soylenecek bir sey degil).

    Donus: (full_text, tool_call). full_text zaten on_sentence ile soylenmis
    olur - tool_call None olsa da OLMASA da, cunku model bir araci cagirmadan
    ONCE kisa bir on-soz soyleyip sonra araci cagirabilir (bu sirali degil,
    ayni turde ikisi de olabilir) - full_text'i sessizce atmak, zaten
    seslendirilmis bir cumleyi gecmisten dusurup tutarsiz birakiyordu
    (yasanmis bir hata). tool_call None ise arac cagrilmamis demektir.
    """
    body = request_body(messages, tools=list(available_tools), stream=True)
    t0 = time.time()
    buffer = ""
    full_text = ""
    tool_calls_acc = {}  # index -> {"id", "name", "arguments"} - parcalar birlestirilir
    first_token_time = None

    for delta in iter_deltas(body):
        for call in delta.get("tool_calls") or []:
            idx = call.get("index", 0)
            entry = tool_calls_acc.setdefault(idx, {"id": None, "name": None, "arguments": ""})
            if call.get("id"):
                entry["id"] = call["id"]
            fn = call.get("function") or {}
            if fn.get("name"):
                entry["name"] = fn["name"]
            if fn.get("arguments"):
                entry["arguments"] += fn["arguments"]

        content = delta.get("content")
        if content:
            if first_token_time is None:
                first_token_time = time.time() - t0
            buffer += content
            full_text += content
            while True:
                sentence, buffer = split_sentence(buffer)
                if not sentence:
                    break
                on_sentence(sentence)

    if buffer.strip():
        # full_text (yukarida) her delta geldikce zaten biriktiriliyor - buffer
        # onun bir ALT KUMESI (son, henuz cumle olarak bolunmemis parca).
        # full_text'e tekrar eklemek onu ikiye katliyordu (yasanmis bir hata:
        # history.json'a "Ne istersin?Ne istersin?..." gibi tekrarlanan
        # cumleler yaziliyordu - bkz. stream_reply'deki dogru desen).
        on_sentence(buffer.strip())

    tool_call = None
    if tool_calls_acc:
        first = tool_calls_acc[min(tool_calls_acc)]
        if first["name"]:
            tool_call = {
                "id": first["id"] or f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {"name": first["name"], "arguments": first["arguments"] or "{}"},
            }

    if first_token_time:
        etiket = "arac secildi, " if tool_call else ""
        print(f"  (karar turu {etiket}ilk token {first_token_time:.2f} sn, toplam {time.time() - t0:.2f} sn)")
    return full_text.strip() or None, tool_call


def uses_tools(turn):
    return any(m.get("role") == "tool" or m.get("tool_calls") for m in turn)


def visible_history(tools_enabled):
    """LLM'e gonderilecek gecmis turlari. Araclar kapaliyken (skills
    calismiyor) arac kullanilan turlar TAMAMEN cikarilir:
    - arac mesajlari kalirsa, araci tanimayan model cagriyi DUZ METIN olarak
      yaziyordu ('{"name": "led_control", ...}</tool_call>'), hoparlorden
      okunuyordu (yasanmis);
    - sadece arac mesajlari cikarilirsa geriye "ledi yak" -> "LED acildi"
      kaliyor, model bunu taklit edip araci olmadigi halde "sondurdum" diye
      uyduruyordu (yasanmis).
    Gecmisin kendisi degismez, sadece LLM'e giden kopya."""
    if tools_enabled:
        return list(history)
    return [turn for turn in history if not uses_tools(turn)]


def build_messages(user_text):
    """Sistem promptu + gecmis + yeni soru. Donus: (messages, tools_enabled).
    Araclar kapaliyken gecmisteki arac mesajlari LLM'e gonderilmez."""
    tools_enabled = bool(available_tools) and client_ref[0] is not None
    messages = [{"role": "system", "content": system_prompt(tools_enabled)}]
    for turn in visible_history(tools_enabled):
        messages.extend(turn)
    messages.append({"role": "user", "content": user_text})
    return messages, tools_enabled


def warm_up():
    """Modeli onceden yukler ve sistem promptu + gecmisi sunucunun onek
    onbellegine alir - ilk gercek soru soguk yuklemeyi (~12 sn, olculdu)
    beklemesin. Tek token uretir; cevap kullanilmaz, gecmise yazilmaz.
    Basarisiz olursa sadece yazdirir: LLM kapali olsa da brain calismali."""
    messages, _ = build_messages(".")
    t0 = time.time()
    try:
        r = http_client.post(CHAT_URL, json=request_body(messages, max_tokens=1, stream=False))
        r.raise_for_status()
        print(f"  LLM isindi ({time.time() - t0:.1f} sn)")
    except httpx.HTTPError as error:
        print(f"  LLM isitilamadi: {error}")


def ask(question, on_sentence=None):
    """LLM'e sorar. on_sentence verilirse her tamamlanan cumlede cagirir.

    Arac (tool) varsa stream'li bir "karar turu" yapilir (bkz. stream_decision):
    LLM ya dogrudan cevap verir (cumle cumle hemen soylenir) ya da bir arac
    cagirir. Arac cagirirsa sonuc call_skill() ile alinir, LLM'e geri verilir,
    asil (seslendirilecek) cevap ikinci - stream'li - turda uretilir. Arac
    yoksa (skills.py kapaliysa) bu adim tamamen atlanir, davranis oncekiyle
    birebir aynidir.
    """
    messages, tools_enabled = build_messages(question)

    full_text = None
    tool_messages = []  # bu turda kullanildiysa: [decision, tool_sonucu] - gecmise de yazilir

    if tools_enabled:
        content, tool_call = stream_decision(messages, on_sentence)

        if tool_call:
            name = tool_call["function"]["name"]
            try:
                params = json.loads(tool_call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                params = {}
            print(f"  [arac] {name}({params})")
            result = call_skill(name, params)
            print(f"  [arac sonucu] {result}")
            # content: model araci cagirmadan once kisa bir on-soz soylemis
            # olabilir (bkz. stream_decision docstring) - gecmise dogru
            # yansitalim, sessizce atmayalim.
            decision_message = {"role": "assistant", "content": content, "tool_calls": [tool_call]}
            tool_result_message = {
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": json.dumps(result, ensure_ascii=False),
            }
            messages.append(decision_message)
            messages.append(tool_result_message)
            tool_messages = [decision_message, tool_result_message]
        elif content:
            # Arac gerekmedi, cevap stream_decision icinde zaten soylendi.
            full_text = content

    if full_text is None:
        full_text = stream_reply(messages, on_sentence)

    # Tum tur (soru + varsa arac cagrisi/sonucu + cevap) TEK GRUP olarak eklenir -
    # boylece maxlen asilinca grup butun halinde dusurulur, ortadan kesilmez.
    turn = [{"role": "user", "content": question}]
    turn.extend(tool_messages)  # arac gercekten cagirildiysa bunu da hatirla,
    turn.append({"role": "assistant", "content": full_text})  # yoksa model sadece
    history.append(turn)  # onceki onay cumlesini taklit edip araci atlamaya basliyor
    write_history()
    return full_text


# --------------------------------------------------------------------------- MQTT
def main():
    client = mqtt_client("yaver-brain")
    client_ref[0] = client

    question_queue = queue.Queue()

    def publish_sentence(sentence):
        print(f"  -> {sentence}")
        client.publish(
            CONFIG["mqtt"]["topic_reply"],
            json.dumps({"text": sentence, "timestamp": time.time()}, ensure_ascii=False),
        )

    def publish_status(status):
        # face.py (masaustu widget) bunu dinler - LLM/ses hattina hicbir
        # etkisi yok, atesle-unut (fire-and-forget) bir MQTT yayini.
        client.publish(
            CONFIG["mqtt"]["topic_status"],
            json.dumps({"module": "brain", "status": status}, ensure_ascii=False),
        )

    def worker():
        # Isitma burada, kuyruktan once: MQTT ag thread'ini bloklamaz, ama
        # gelen ilk soru isinma bitene kadar kuyrukta bekler (zaten LLM'i
        # bekleyecekti).
        if LOCAL.get("warm_up"):
            warm_up()
        while True:
            question = question_queue.get()
            print(f'\nSORU: "{question}"')
            publish_status("thinking")
            try:
                ask(question, publish_sentence)
            except httpx.HTTPError as error:
                print(f"  LLM hatasi: {error}")
                publish_sentence("Beynime ulasamiyorum, sunucu kapali olabilir.")
            except Exception as error:
                # Beklenmedik bir hata worker thread'ini oldurup Yaver'i tamamen
                # sagir birakmasin - bir turun basarisiz olmasi butun sureci
                # devirmemeli (bkz. stdout.reconfigure yorumu, ayni prensip).
                print(f"  beklenmedik hata: {error}")
                publish_sentence("Bir hata oldu, tekrar dener misin?")
            finally:
                publish_status("done")

    @guard
    def on_message(client_, userdata, msg):
        if msg.topic == CONFIG["skills"]["topic_list"]:
            # Arac listesi bir JSON DIZISI (diger mesajlar gibi nesne degil).
            # Bos yuk = skills.py kapandi (retained mesaj temizlendi): araclar
            # LLM'e artik tanitilmasin, yoksa model olmayan bir araci cagirip
            # zaman asimina dusuyordu.
            if not msg.payload:
                available_tools.clear()
                print("  araclar kaldirildi (skills kapali)")
                return
            try:
                tools = json.loads(msg.payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return
            if not isinstance(tools, list):
                return
            available_tools[:] = tools
            names = [t["function"]["name"] for t in available_tools]
            print(f"  {len(available_tools)} arac ogrenildi: {', '.join(names)}")
            return

        packet = parse_payload(msg)
        if packet is None:
            return

        if msg.topic == CONFIG["skills"]["topic_result"]:
            entry = pending_skill_results.get(packet.get("id"))
            if entry:
                entry["result"] = packet
                entry["event"].set()
            return

        question = text_field(packet)
        if question:
            question_queue.put(question)

    def on_connect(client_, userdata, flags, rc, properties=None):
        client_.subscribe(CONFIG["mqtt"]["topic_text"])
        client_.subscribe(CONFIG["skills"]["topic_result"])
        client_.subscribe(CONFIG["skills"]["topic_list"])
        print(f"Dinleniyor: {CONFIG['mqtt']['topic_text']}")

    client.on_connect = on_connect
    client.on_message = on_message
    mqtt_connect(client, CONFIG)

    read_history()
    threading.Thread(target=worker, daemon=True).start()
    print(f"Beyin hazir. LLM: {LOCAL['model']} @ {LOCAL['server']}  (gecmis: {len(history)} tur)")
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
