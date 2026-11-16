"""Yaver - brain module (beyin).

MQTT'den metin alir, LLM'e sorar, cevabi cumle cumle MQTT'ye yayinlar.
Mikrofonun, hoparlorun, hatta LLM'in nerede oldugunu bilmez.

LLM saglayicisi config.yaml -> llm.provider ile secilir:
    local : OpenAI uyumlu yerel sunucu (Ollama, llama.cpp...) - internetsiz
    claude: Anthropic Claude API - kullanicinin bilincli secimi, internet ister
Ikisi ayni anda kullanilmaz; claude secilince yerel model yedek DEGILDIR.

skills.py varsa (yaver/skill/list yayinliyorsa) LLM'e arac (tool) olarak
tanitilir. Bir turda birden fazla arac, birden fazla adimda cagrilabilir
(brain.max_tool_rounds). "confirm" isaretli araclar (riskli/geri alinamaz)
kullanicidan sesli onay alinmadan calistirilmaz.

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

from common import (ROOT, guard, load_config, mqtt_client, mqtt_connect, normalize, parse_payload,
                    secret, strip_emoji, text_field)

# LLM bazen (nadiren) sistem promptunun yasagina ragmen emoji uretiyor.
# Windows konsolu varsayilan kod sayfasi (orn. cp1254) bunu print() ile
# yazdiramayip UnicodeEncodeError firlatiyordu - bu da worker thread'ini
# SESSIZCE oldurup Yaver'i bir daha hic cevap vermez hale getiriyordu
# (yasanmis). errors="replace" ile surec asla cokmez.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CONFIG = load_config()
BRAIN = CONFIG["brain"]
LLM = CONFIG["llm"]
LOCAL = LLM["local"]
CLAUDE = LLM.get("claude") or {}
PROVIDER = LLM["provider"]
if PROVIDER not in ("local", "claude"):
    raise SystemExit(f"llm.provider '{PROVIDER}' desteklenmiyor (local ya da claude).")
CHAT_URL = f"{LOCAL['server']}/v1/chat/completions"

HISTORY_FILE = ROOT / "history.json"  # son konusma turlari

history = deque()  # her eleman bir TURUN mesaj listesi (tek mesaj degil) - boylece
# bir arac turu (soru, arac cagrisi, arac sonucu, cevap) sinir asilinca
# ortadan kesilip yetim bir "tool" mesaji birakmaz. Sinir: bkz. add_turn().

client_ref = [None]        # MQTT istemcisi - --ask modunda None kalir, arac cagirma atlanir
available_tools = []       # skills.py'den ogrenilen arac listesi (LLM'e giden hali, "confirm" yok)
confirm_tools = set()      # sesli onay gerektiren arac adlari
known_memory = {}          # skills.py'nin yayinladigi kalici bilgiler (yaver/memory)
pending_skill_results = {}  # arac istek id -> {"event": Event, "result": dict|None}
pending_action = {}        # onay bekleyen arac: {"name", "params", "expires"}
cancel_event = threading.Event()  # kullanici sozu kesti (yaver/control stop)


class LLMError(Exception):
    """LLM'e ulasilamadi / istek reddedildi - kullaniciya soylenecek kisa sebep."""


# --------------------------------------------------------------------------- gecmis
def add_turn(turn):
    """Gecmise bir tur ekler. Sinir (history_turns) asilinca en eski turlar
    TEK TEK degil, BLOK halinde atilir: son history_trim_to tur kalir.

    Neden: LLM sunucusu ortak oneki (sistem promptu + gecmis) onbellekte
    tutuyor. Her turda en eski tur atilinca onek sistem promptundan hemen
    sonra degisiyor, gecmisin tamami HER SORUDA yeniden isleniyordu - olculdu:
    6. turdan itibaren ilk cumle 0,5 sn'den 1,15 sn'ye cikti."""
    history.append(turn)
    if len(history) > BRAIN["history_turns"]:
        while len(history) > BRAIN["history_trim_to"]:
            history.popleft()


def read_history():
    if not HISTORY_FILE.exists():
        return
    try:
        flat = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    turn = []
    for m in flat:
        if m.get("role") == "assistant" and isinstance(m.get("content"), str):
            m["content"] = strip_emoji(m["content"])
        if m.get("role") == "user" and turn:
            add_turn(turn)
            turn = []
        turn.append(m)
    if turn:
        add_turn(turn)


def write_history():
    flat = [m for turn in history for m in turn]
    HISTORY_FILE.write_text(json.dumps(flat, ensure_ascii=False, indent=1), encoding="utf-8")


def recent_sentences(turns):
    """Son N turda Yaver'in soyledigi cumleler (normalize edilmis) - tekrar filtresi icin."""
    seen = set()
    for turn in list(history)[-turns:]:
        for m in turn:
            if m.get("role") == "assistant" and isinstance(m.get("content"), str):
                for part in re.split(r"(?<=[.!?])\s+", m["content"]):
                    if normalize(part):
                        seen.add(normalize(part))
    return seen


def is_repeat(sentence, seen, spoken_count):
    """Bu cumle son cevaplardan birinde birebir gectiyse ve bu cevapta zaten
    bir sey soylendiyse atlanir. Kucuk model kendi dolgu cumlesini ("Lutfen
    daha net bir sekilde sor.") her cevabin sonuna ekliyordu (yasanmis).
    Cevabin ILK cumlesi asla atilmaz - "LED'i yaktim." gibi mesru tekrar
    cevapsiz kalmasin."""
    return spoken_count > 0 and normalize(sentence) in seen


def system_prompt(tools_enabled=True):
    """Tarih/saat KASITLI OLARAK yok - get_datetime araci var. Sistem promptu
    turlar arasi birebir sabit kalmali (LLM onek onbellegi). Sadece arac
    durumu ya da hafiza degisince degisir, o anda bir kez yenilenir."""
    text = BRAIN["system_prompt"].strip()
    if not tools_enabled:
        text += "\n" + BRAIN["no_tools_note"].strip()
    if known_memory:
        lines = "\n".join(f"- {k}: {v}" for k, v in known_memory.items())
        text += f"\n\nKullanıcı hakkında bildiklerin:\n{lines}"
    return text


# --------------------------------------------------------------------------- araclar (skills)
def call_skill(name, params, on_slow=None):
    """skills.py'ye MQTT uzerinden istek gonderir, sonucu (timeout'lu) bekler.

    Worker thread'inden cagrilir - MQTT ag thread'i serbest kalir, boylece
    sonuc mesaji gelip event'i tetikleyebilir. on_slow: sonuc
    brain.tool_filler_sec icinde gelmezse bir kez cagrilir ("Bir saniye.")."""
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
    timeout = CONFIG["skills"]["timeout_sec"]
    filler = BRAIN.get("tool_filler_sec")
    got = entry["event"].wait(min(filler, timeout) if filler and on_slow else timeout)
    if not got and filler and on_slow:
        on_slow()
        got = entry["event"].wait(max(timeout - filler, 0))
    pending_skill_results.pop(request_id, None)
    if not got or entry["result"] is None:
        return {"status": "error", "data": "araç zaman aşımına uğradı, işlem yapılamadı"}
    return {k: v for k, v in entry["result"].items() if k in ("status", "data")}


def needs_confirmation_result(name):
    return {
        "status": "needs_confirmation",
        "data": (f"'{name}' aracı henüz ÇALIŞMADI, sesli onay gerekiyor. Kullanıcıya ne "
                 "yapacağını tek cümleyle söyle ve 'Onaylıyor musun?' diye sor. Onaylarsa "
                 "sistem aracı kendisi çalıştıracak, aracı tekrar çağırma."),
    }


YES_WORDS = {"evet", "onayliyorum", "onayla", "yap", "tamam", "olur", "eminim", "devam", "kesinlikle", "aynen"}
NO_WORDS = {"hayir", "iptal", "vazgec", "vazgectim", "yapma", "dur", "istemiyorum", "olmaz"}


def confirmation_answer(text):
    """Onay bekleyen bir islemden sonra gelen cumle: 'yes', 'no' ya da None
    (konuyla ilgisiz). Karar LLM'e degil koda ait - LLM onayi kendi kendine
    veremesin. 'Hayir' kelimesi 'evet'ten once kontrol edilir ("evet, hayir
    yapma" gibi karisik cumle iptal sayilir)."""
    words = set(normalize(text).split())
    if words & NO_WORDS:
        return "no"
    if words & YES_WORDS:
        return "yes"
    return None


# --------------------------------------------------------------------------- cumle bolme
SENTENCE_END = re.compile(r"(?<=[.!?:;])\s+|\n+")


def split_sentence(buffer):
    """Tamponda tamamlanmis cumle varsa (cumle, kalan) dondurur.

    Cok kisa bir parca ("Tamam.") tek basina yayinlanmaz, bir SONRAKI cumle
    sonuyla birlestirilir ("Tamam. Lamba acik.")."""
    for match in SENTENCE_END.finditer(buffer):
        sentence = buffer[:match.end()].strip()
        if len(sentence) >= BRAIN["min_sentence_chars"]:
            return sentence, buffer[match.end():]
    return None, buffer


class SentenceStream:
    """Gelen metin parcalarini biriktirip tamamlanan her cumleyi on_sentence'a verir."""

    def __init__(self, on_sentence):
        self.on_sentence = on_sentence
        self.buffer = ""
        self.text = ""

    def feed(self, chunk):
        self.buffer += chunk
        self.text += chunk
        while self.on_sentence:
            sentence, self.buffer = split_sentence(self.buffer)
            if not sentence:
                break
            self.on_sentence(sentence)

    def finish(self):
        if self.on_sentence and self.buffer.strip():
            self.on_sentence(self.buffer.strip())
        self.buffer = ""
        return self.text.strip()


# --------------------------------------------------------------------------- saglayici: local
# Kalici bir Client - her istekte yeniden baglanti kurmak yerine ayni
# baglantiyi (keep-alive) tekrar kullanir.
http_client = httpx.Client(timeout=LOCAL["timeout_sec"])


def request_body(messages, **fields):
    """Yerel sunucu isteginin ortak govdesi. model: Ollama hangi modeli
    kullanacagini bundan bilir. request_options: sunucuya ozel ek alanlar."""
    body = {
        "model": LOCAL["model"],
        "messages": messages,
        "temperature": BRAIN["temperature"],
        "max_tokens": BRAIN["max_tokens"],
        **fields,
    }
    body.update(LOCAL.get("request_options") or {})
    return body


def openai_messages(system, messages):
    """Ic mesaj formati zaten OpenAI uyumlu; sadece Claude'a ozel ek alanlar silinir."""
    out = [{"role": "system", "content": system}]
    for m in messages:
        out.append({k: v for k, v in m.items() if k != "claude_content"})
    return out


def local_turn(system, messages, tools, on_sentence):
    """Yerel sunucuya tek bir stream'li istek. Donus: (metin, arac_cagrilari).

    Arac gerekmezse icerik parcalari geldikce cumle cumle soylenir; arac
    cagrilari (tool_calls) parca parca gelir, sonda birlestirilir."""
    fields = {"stream": True}
    if tools:
        fields["tools"] = tools
    body = request_body(openai_messages(system, messages), **fields)
    stream = SentenceStream(on_sentence)
    calls = {}  # index -> {"id", "name", "arguments"}
    t0, first = time.time(), None
    try:
        with http_client.stream("POST", CHAT_URL, json=body) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if cancel_event.is_set():
                    break
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0]["delta"]
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                for call in delta.get("tool_calls") or []:
                    entry = calls.setdefault(call.get("index", 0), {"id": None, "name": None, "arguments": ""})
                    entry["id"] = call.get("id") or entry["id"]
                    fn = call.get("function") or {}
                    entry["name"] = fn.get("name") or entry["name"]
                    entry["arguments"] += fn.get("arguments") or ""
                content = delta.get("content")
                if content:
                    first = first or time.time() - t0
                    stream.feed(content)
    except httpx.HTTPError as error:
        raise LLMError(f"yerel LLM sunucusuna ulasilamadi: {error}") from error
    text = stream.finish()
    tool_calls = [
        {"id": c["id"] or f"call_{uuid.uuid4().hex[:8]}", "type": "function",
         "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
        for _, c in sorted(calls.items()) if c["name"]
    ]
    if first:
        print(f"  (ilk token {first:.2f} sn, toplam {time.time() - t0:.2f} sn)")
    return text, tool_calls, {}


def local_warm_up(system, tools):
    """Modeli onceden yukler ve sistem promptu (+araclar) onbellege alinir -
    ilk gercek soru soguk yuklemeyi (~12 sn, olculdu) beklemesin. Araclar
    gercek sorudaki gibi gonderilmeli, yoksa baska bir onek onbellege alinir."""
    messages = openai_messages(system, [{"role": "user", "content": "."}])
    fields = {"tools": tools} if tools else {}
    t0 = time.time()
    try:
        r = http_client.post(CHAT_URL, json=request_body(messages, max_tokens=1, stream=False, **fields))
        r.raise_for_status()
        print(f"  LLM isindi ({time.time() - t0:.1f} sn)")
    except httpx.HTTPError as error:
        print(f"  LLM isitilamadi: {error}")


# --------------------------------------------------------------------------- saglayici: claude
claude_ref = [None]


def claude_client():
    """Anthropic istemcisi - kutuphane sadece claude secilince import edilir
    (yerel kurulumda paket yuklu olmak zorunda degil)."""
    if claude_ref[0] is None:
        import anthropic

        api_key = secret("anthropic_api_key", "ANTHROPIC_API_KEY")
        kwargs = {"timeout": CLAUDE.get("timeout_sec", 60), "max_retries": CLAUDE.get("max_retries", 1)}
        if api_key:
            kwargs["api_key"] = api_key
        claude_ref[0] = anthropic.Anthropic(**kwargs)
    return claude_ref[0]


def to_claude_tools(tools):
    """OpenAI arac semasi -> Anthropic arac semasi."""
    out = []
    for t in tools:
        fn = t["function"]
        out.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def to_claude_messages(messages):
    """Ic (OpenAI uyumlu) mesajlar -> Anthropic mesajlari.

    - assistant + tool_calls -> text + tool_use bloklari. Bu turda Claude'un
      kendi urettigi ham bloklar (thinking dahil) "claude_content"te varsa
      AYNEN geri gonderilir (arac dongusunde thinking bloklari degistirilmeden
      donmeli). Gecmise yazilan turlarda bu alan yok - eski turlarin
      thinking bloklari gonderilmez.
    - ardisik "tool" mesajlari TEK user mesajinda tool_result bloklari olur.
    - bos assistant metni gonderilmez (API bos text blogunu reddediyor)."""
    out = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": m.get("content") or "."})
        elif role == "assistant":
            if m.get("claude_content"):
                out.append({"role": "assistant", "content": m["claude_content"]})
                continue
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for call in m.get("tool_calls") or []:
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append({"type": "tool_use", "id": call["id"],
                               "name": call["function"]["name"], "input": args if isinstance(args, dict) else {}})
            if blocks:
                out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m.get("content") or ""}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out


def block_to_param(block):
    """Yanittaki icerik blogu -> bir sonraki istekte geri gonderilecek sozluk."""
    kind = block.type
    if kind == "text":
        return {"type": "text", "text": block.text} if block.text else None
    if kind == "thinking":
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if kind == "redacted_thinking":
        return {"type": "redacted_thinking", "data": block.data}
    if kind == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return None


def claude_turn(system, messages, tools, on_sentence, final_round=False):
    """Claude'a tek bir stream'li istek. Donus: (metin, arac_cagrilari, ek_alanlar).

    final_round: arac sonuclari zaten mesajlarda, artik sadece cevap
    istenir - araclar yine tanitilir (gecmiste tool_use bloklari var) ama
    tool_choice none ile cagrilmalari engellenir."""
    import anthropic

    params = {
        "model": CLAUDE["model"],
        "max_tokens": CLAUDE["max_tokens"],
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": to_claude_messages(messages),
    }
    if tools:
        params["tools"] = to_claude_tools(tools)
        if final_round:
            params["tool_choice"] = {"type": "none"}
    params.update(CLAUDE.get("request_options") or {})

    stream_out = SentenceStream(on_sentence)
    t0, first = time.time(), None
    try:
        with claude_client().messages.stream(**params) as stream:
            for event in stream:
                if cancel_event.is_set():
                    break
                if event.type == "text":
                    first = first or time.time() - t0
                    stream_out.feed(event.text)
            if cancel_event.is_set():
                return stream_out.finish(), [], {}
            final = stream.get_final_message()
    except anthropic.AuthenticationError as error:
        raise LLMError("Claude API anahtari gecersiz (secrets.yaml -> anthropic_api_key)") from error
    except TypeError as error:
        # SDK hic kimlik bilgisi bulamayinca TypeError firlatiyor. Baska bir
        # TypeError (kod hatasi) bu kiliga girmesin, oldugu gibi yukselsin.
        if "authentication" not in str(error):
            raise
        raise LLMError("Claude API anahtari yok (secrets.yaml -> anthropic_api_key)") from error
    except anthropic.APIConnectionError as error:
        raise LLMError(f"Claude API'ye ulasilamadi (internet?): {error}") from error
    except anthropic.APIStatusError as error:
        raise LLMError(f"Claude API hatasi {error.status_code}: {error.message}") from error
    text = stream_out.finish()
    if first:
        usage = final.usage
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        print(f"  (claude ilk token {first:.2f} sn, toplam {time.time() - t0:.2f} sn, "
              f"girdi {usage.input_tokens}+{cached} onbellek, cikti {usage.output_tokens})")

    if final.stop_reason == "refusal":
        return text or "Bu konuda yardımcı olamıyorum.", [], {}
    tool_calls = []
    if final.stop_reason != "max_tokens":  # kesik arac girdisi calistirilmaz
        tool_calls = [
            {"id": b.id, "type": "function",
             "function": {"name": b.name, "arguments": json.dumps(b.input, ensure_ascii=False)}}
            for b in final.content if b.type == "tool_use"
        ]
    extra = {}
    if tool_calls:
        extra["claude_content"] = [p for p in map(block_to_param, final.content) if p]
    return text, tool_calls, extra


def claude_warm_up():
    """Claude'da sunucu tarafi isinma gerekmez; bu cagri baglantiyi (TLS)
    onceden acar ve anahtar/model gecerliligini acilista kontrol eder."""
    t0 = time.time()
    try:
        claude_client().models.retrieve(CLAUDE["model"])
        print(f"  Claude hazir: {CLAUDE['model']} ({time.time() - t0:.1f} sn)")
    except Exception as error:
        print(f"  Claude'a ulasilamadi: {error}")


def llm_turn(system, messages, tools, on_sentence, final_round=False):
    if PROVIDER == "claude":
        return claude_turn(system, messages, tools, on_sentence, final_round)
    # Yerel modelde son turda araclar hic gonderilmez (tool_choice: none
    # her sunucuda desteklenmiyor).
    return local_turn(system, messages, None if final_round else tools, on_sentence)


def warm_up():
    if PROVIDER == "claude":
        claude_warm_up()
        return
    tools_enabled = bool(available_tools) and client_ref[0] is not None
    local_warm_up(system_prompt(tools_enabled), list(available_tools) if tools_enabled else None)


# --------------------------------------------------------------------------- soru -> cevap
def uses_tools(turn):
    return any(m.get("role") == "tool" or m.get("tool_calls") for m in turn)


def visible_history(tools_enabled):
    """LLM'e gonderilecek gecmis turlari. Araclar kapaliyken (skills
    calismiyor) arac kullanilan turlar TAMAMEN cikarilir - kalirsa araci
    tanimayan model cagriyi duz metin olarak yaziyor ya da "sondurdum" diye
    uyduruyordu (yasanmis)."""
    if tools_enabled:
        return [m for turn in history for m in turn]
    return [m for turn in history if not uses_tools(turn) for m in turn]


def run_tool_call(call, on_slow):
    """Tek bir arac cagrisini calistirir (ya da onay ister). Donus: tool mesaji."""
    name = call["function"]["name"]
    try:
        params = json.loads(call["function"].get("arguments") or "{}")
    except json.JSONDecodeError:
        params = {}
    if not isinstance(params, dict):
        params = {}
    if name in confirm_tools:
        pending_action.clear()
        pending_action.update(name=name, params=params, expires=time.time() + BRAIN["confirm_timeout_sec"])
        result = needs_confirmation_result(name)
        print(f"  [arac] {name}({params}) -> sesli onay bekleniyor")
    else:
        print(f"  [arac] {name}({params})")
        result = call_skill(name, params, on_slow)
        print(f"  [arac sonucu] {result}")
    return {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)}


def ask(question, on_sentence=None, is_event=False):
    """LLM'e sorar. on_sentence verilirse her tamamlanan cumlede cagirir.

    Akis (bkz. brain.max_tool_rounds):
      1. LLM ya dogrudan cevap verir (cumle cumle hemen soylenir) ya da bir
         veya birkac arac cagirir.
      2. Araclar calistirilir, sonuclari LLM'e verilir, LLM yeniden karar
         verir (baska arac ya da cevap). En fazla max_tool_rounds adim.
      3. Son adimda araclar kapatilir, LLM cevabi soyler.
    Onay bekleyen bir islem varsa ve bu cumle "evet" ise arac LLM'e
    sorulmadan dogrudan calistirilir; "hayir" ise iptal edilir.
    is_event: arka plan bildirimi - bekleyen onayi tuketmez."""
    cancel_event.clear()
    tools_enabled = bool(available_tools) and client_ref[0] is not None
    tools = list(available_tools) if tools_enabled else None
    system = system_prompt(tools_enabled)
    messages = visible_history(tools_enabled) + [{"role": "user", "content": question}]

    seen = recent_sentences(BRAIN["repeat_window_turns"])
    spoken = []  # bu turun son adiminda gercekten soylenen cumleler

    def speak(sentence):
        sentence = strip_emoji(sentence)
        if not sentence or cancel_event.is_set():
            return
        if is_repeat(sentence, seen, len(spoken)):
            print(f"  (tekrar, atlandi: {sentence})")
            return
        spoken.append(sentence)
        if on_sentence:
            on_sentence(sentence)

    filler_said = []

    def on_slow():
        if not filler_said:
            filler_said.append(True)
            speak(BRAIN["tool_filler"])

    turn_messages = []  # gecmise yazilacak arac adimlari

    # Onay bekleyen islem: karar koda ait (bkz. confirmation_answer).
    if pending_action and not is_event:
        action = dict(pending_action)
        pending_action.clear()
        answer = confirmation_answer(question) if time.time() < action["expires"] else None
        if answer == "yes" and tools_enabled:
            call = {"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                    "function": {"name": action["name"], "arguments": json.dumps(action["params"], ensure_ascii=False)}}
            print(f"  [onaylandi] {action['name']}({action['params']})")
            result = call_skill(action["name"], action["params"], on_slow)
            print(f"  [arac sonucu] {result}")
            decision = {"role": "assistant", "content": None, "tool_calls": [call]}
            tool_msg = {"role": "tool", "tool_call_id": call["id"],
                        "content": json.dumps(result, ensure_ascii=False)}
            messages += [decision, tool_msg]
            turn_messages += [decision, tool_msg]
        elif answer == "no":
            print(f"  [iptal edildi] {action['name']}")

    final_text = ""
    max_rounds = BRAIN["max_tool_rounds"] if tools_enabled else 0
    executed = set()  # bu turda calistirilan (ad, parametre) - kucuk model ayni araci dongude tekrar cagirabiliyor
    force_final = False
    for round_no in range(max_rounds + 1):
        final_round = force_final or round_no == max_rounds
        spoken.clear()
        text, tool_calls, extra = llm_turn(system, messages, tools, speak, final_round=final_round)
        if cancel_event.is_set():
            final_text = " ".join(spoken)
            print("  (soz kesildi, cevap yarida birakildi)")
            break
        if not tool_calls:
            final_text = " ".join(spoken) or strip_emoji(text)
            break
        signatures = {(c["function"]["name"], c["function"]["arguments"]) for c in tool_calls}
        if final_round or signatures <= executed:
            # Ayni cagrilar zaten yapildi (ya da son adimda arac istedi):
            # calistirma, cevabi araclar kapali bir son adimda al.
            if final_round:
                final_text = " ".join(spoken) or strip_emoji(text)
                break
            force_final = True
            continue
        executed |= signatures
        decision = {"role": "assistant", "content": strip_emoji(text) or None, "tool_calls": tool_calls, **extra}
        messages.append(decision)
        turn_messages.append(decision)
        for call in tool_calls:
            tool_msg = run_tool_call(call, on_slow)
            messages.append(tool_msg)
            turn_messages.append(tool_msg)

    # Tum tur TEK GRUP olarak eklenir. Arac mesajlari da yazilir - yoksa model
    # onceki onay cumlesini taklit edip araci atlamaya basliyordu (yasanmis).
    # claude_content (thinking bloklari) gecmise yazilmaz: sonraki turlarda
    # gecmis kirpilinca/sistem promptu degisince gecersiz kalirlar.
    turn = [{"role": "user", "content": question}]
    turn += [{k: v for k, v in m.items() if k != "claude_content"} for m in turn_messages]
    turn.append({"role": "assistant", "content": final_text})
    add_turn(turn)
    write_history()
    return final_text


def event_prompt(packet):
    """Arka planda biten bir aracin bildirimi -> LLM'e verilecek kullanici mesaji."""
    data = json.dumps(packet.get("data"), ensure_ascii=False)
    return (f"(Sistem bildirimi, kullanıcı söylemedi) Arka plandaki '{packet.get('name')}' işi bitti. "
            f"Sonuç: {data}. Bunu kullanıcıya kısaca, doğal bir şekilde söyle.")


# --------------------------------------------------------------------------- MQTT
def apply_tool_list(tools):
    """skills'in yayinladigi listeyi uygular: "confirm" alanini ayirir
    (LLM'e gitmez). Donus: liste degisti mi."""
    clean, confirm = [], set()
    for t in tools:
        if not isinstance(t, dict) or "function" not in t:
            continue
        if t.get("confirm"):
            confirm.add(t["function"]["name"])
        clean.append({k: v for k, v in t.items() if k != "confirm"})
    changed = clean != available_tools or confirm != confirm_tools
    available_tools[:] = clean
    confirm_tools.clear()
    confirm_tools.update(confirm)
    return changed


def main():
    client = mqtt_client("yaver-brain")
    client_ref[0] = client

    question_queue = queue.Queue()
    WARM_UP = object()  # kuyrukta "soru degil, onbellegi isit" isareti
    tools_known = threading.Event()  # skills'in arac listesi (ya da yoklugu) ogrenildi

    def publish_sentence(sentence):
        print(f"  -> {sentence}")
        client.publish(
            CONFIG["mqtt"]["topic_reply"],
            json.dumps({"text": sentence, "timestamp": time.time()}, ensure_ascii=False),
        )

    def publish_status(status):
        client.publish(
            CONFIG["mqtt"]["topic_status"],
            json.dumps({"module": "brain", "status": status}, ensure_ascii=False),
        )

    def worker():
        # Isitma burada: once skills'in arac listesini (retained) bekle,
        # araclarsiz isinma yanlis istemi onbellege alirdi.
        if LLM.get("warm_up"):
            tools_known.wait(CONFIG["skills"]["list_wait_sec"])
            warm_up()
        while True:
            item = question_queue.get()
            if item is WARM_UP:
                warm_up()
                continue
            kind, question = item
            print(f'\n{"BILDIRIM" if kind == "event" else "SORU"}: "{question}"')
            publish_status("thinking")
            try:
                ask(question, publish_sentence, is_event=(kind == "event"))
            except LLMError as error:
                print(f"  LLM hatasi: {error}")
                publish_sentence(BRAIN["llm_error_reply"])
            except Exception as error:
                # Bir turun basarisiz olmasi butun sureci devirmemeli.
                print(f"  beklenmedik hata: {error!r}")
                publish_sentence("Bir hata oldu, tekrar dener misin?")
            finally:
                publish_status("done")

    def on_tools(payload):
        # Bos yuk = skills.py kapandi (retained mesaj temizlendi).
        if not payload:
            tools = []
        else:
            try:
                tools = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return
            if not isinstance(tools, list):
                return
        changed = apply_tool_list(tools)
        if available_tools:
            names = [t["function"]["name"] for t in available_tools]
            print(f"  {len(available_tools)} arac ogrenildi: {', '.join(names)}")
        else:
            print("  arac yok (skills kapali)")
        if not tools_known.is_set():
            tools_known.set()
        elif changed and LLM.get("warm_up") and PROVIDER == "local":
            question_queue.put(WARM_UP)

    @guard
    def on_message(client_, userdata, msg):
        if msg.topic == CONFIG["skills"]["topic_list"]:
            on_tools(msg.payload)
            return

        packet = parse_payload(msg)
        if packet is None:
            return

        if msg.topic == CONFIG["skills"]["topic_memory"]:
            memory = {str(k): str(v) for k, v in packet.items()}
            if memory != known_memory:
                known_memory.clear()
                known_memory.update(memory)
                print(f"  hafiza: {len(known_memory)} bilgi")
                if tools_known.is_set() and LLM.get("warm_up") and PROVIDER == "local":
                    question_queue.put(WARM_UP)
            return

        if msg.topic == CONFIG["skills"]["topic_result"]:
            entry = pending_skill_results.get(packet.get("id"))
            if entry:
                entry["result"] = packet
                entry["event"].set()
            return

        if msg.topic == CONFIG["skills"]["topic_event"]:
            question_queue.put(("event", event_prompt(packet)))
            return

        if msg.topic == CONFIG["mqtt"]["topic_control"]:
            if packet.get("command") == "stop":
                cancel_event.set()
                print("  [kontrol] soz kesildi")
            return

        question = text_field(packet)
        if question:
            question_queue.put(("speech", question))

    def on_connect(client_, userdata, flags, rc, properties=None):
        for topic in (CONFIG["mqtt"]["topic_text"], CONFIG["mqtt"]["topic_control"],
                      CONFIG["skills"]["topic_result"], CONFIG["skills"]["topic_list"],
                      CONFIG["skills"]["topic_event"], CONFIG["skills"]["topic_memory"]):
            client_.subscribe(topic)
        print(f"Dinleniyor: {CONFIG['mqtt']['topic_text']}")

    client.on_connect = on_connect
    client.on_message = on_message
    mqtt_connect(client, CONFIG)

    read_history()
    threading.Thread(target=worker, daemon=True).start()
    model = CLAUDE["model"] if PROVIDER == "claude" else f"{LOCAL['model']} @ {LOCAL['server']}"
    print(f"Beyin hazir. LLM: {PROVIDER} / {model}  (gecmis: {len(history)} tur)")
    client.loop_forever()


if __name__ == "__main__":
    if "--ask" in sys.argv:
        read_history()
        question = " ".join(sys.argv[sys.argv.index("--ask") + 1:])
        try:
            print(ask(question, lambda s: print(f"  -> {s}")))
        except LLMError as error:
            print(f"LLM hatasi: {error}")
    else:
        try:
            main()
        except KeyboardInterrupt:
            print("\nbeyin kapandi.")
