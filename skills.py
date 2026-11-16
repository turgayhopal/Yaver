"""Yaver - skills module (yetenekler).

LLM'in arac (tool) olarak cagirabilecegi fonksiyonlari barindirir. brain.py'den
MQTT uzerinden istek alir, ilgili fonksiyonu calistirir, sonucu geri yayinlar.
Baska hicbir modulun varligini bilmez.

Yeni bir arac eklemek icin: asagidaki gibi @skill dekoratoruyla bir fonksiyon
yaz. Cekirdek koda (main, run_skill, MQTT akisi) dokunmak gerekmez - brain.py
da yeni araci otomatik ogrenir (bkz. tool_definitions/publish_tool_list).

Araclar MQTT ag thread'inde DEGIL, bir thread havuzunda calisir (bkz.
skills.workers): yavas bir arac (cihaz onayi beklemek, komut calistirmak)
diger istekleri kilitlemez. Uzun isler (zamanlayici, delegate_task) hemen
"basladim" doner, bitince ctx.notify() ile yaver/skill/event'e haber verir.

Kullanim:
    python skills.py
"""

import ast
import json
import operator
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from common import guard, load_config, mqtt_client, mqtt_connect, parse_payload, resolve_path

CONFIG = load_config()
SK = CONFIG["skills"]

SKILLS = {}


def skill(name, description, parameters, confirm=False):
    """Fonksiyonu SKILLS registry'sine kaydeden dekorator.

    parameters: OpenAI/llama.cpp tool-calling'in bekledigi JSON-schema sozlugu.
    confirm=True: geri alinamaz/riskli arac - brain onu kullanicidan SESLI
    onay almadan calistirmaz (bkz. brain.py "onay").
    Fonksiyon ilk arguman olarak bir SkillContext alir (MQTT, cihaz onayi,
    arka plan bildirimi)."""
    def decorator(func):
        SKILLS[name] = {"func": func, "description": description,
                        "parameters": parameters, "confirm": confirm}
        return func
    return decorator


def devices_by_type(device_type):
    """config.yaml'daki genel cihaz kayit defterinden belirli turdeki (orn.
    "led") tum cihazlari dondurur. Yeni bir skill de kendi turunu boyle
    sorgular - cihaz eklemek/cikarmak icin skill kodu degismez."""
    return {
        name: cfg for name, cfg in (CONFIG.get("devices") or {}).items()
        if cfg.get("type") == device_type
    }


def device_param_schema(device_type):
    """Bir skill'in 'device' parametresi icin, kayitli cihazlardan turetilen
    JSON-schema (enum + LLM'in 'hangi cihaz' ayrimini yapmasi icin aciklama)."""
    devices = devices_by_type(device_type)
    descriptions = "; ".join(f"{name} = {cfg['label']}" for name, cfg in devices.items())
    return {
        "type": "string",
        "enum": list(devices.keys()),
        "description": f"Hangi cihaz kontrol edilecek. Seçenekler: {descriptions}",
    }


# --------------------------------------------------------------------------- baglam
class SkillContext:
    """Bir aracin MQTT'ye erisimi. Cihaz onayi (ack) bekleme ve arka plan
    isinin sonucunu brain'e bildirme buradan yapilir."""

    def __init__(self, client):
        self.client = client
        self._waiters = {}   # topic -> [(Event, holder)]
        self._lock = threading.Lock()

    def publish(self, topic, payload, retain=False):
        if self.client is not None:
            self.client.publish(topic, payload, retain=retain)

    def publish_and_wait(self, topic, payload, ack_topic, timeout):
        """Mesaji yayinlar, cihazin ack_topic'e cevap vermesini bekler.
        Cevap (str) ya da zaman asiminda None dondurur. Bekleyici YAYINDAN
        ONCE kaydedilir - hizli bir cihazin cevabi kacmasin."""
        event, holder = threading.Event(), {}
        with self._lock:
            self._waiters.setdefault(ack_topic, []).append((event, holder))
        try:
            self.publish(topic, payload)
            event.wait(timeout)
        finally:
            with self._lock:
                waiters = self._waiters.get(ack_topic, [])
                if (event, holder) in waiters:
                    waiters.remove((event, holder))
        return holder.get("payload")

    def deliver(self, topic, payload):
        """MQTT ag thread'inden: bir ack konusuna gelen mesaji bekleyenlere ilet."""
        with self._lock:
            waiters = list(self._waiters.get(topic, []))
        for event, holder in waiters:
            holder["payload"] = payload
            event.set()

    def notify(self, name, data):
        """Arka planda biten bir isi brain'e bildirir (yaver/skill/event) -
        brain bunu LLM'e verip kullaniciya sesli soyletir."""
        self.publish(SK["topic_event"], json.dumps(
            {"name": name, "data": data, "timestamp": time.time()}, ensure_ascii=False))


# --------------------------------------------------------------------------- yardimcilar (saf)
ONES = ["", "bir", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz"]
TENS = ["", "on", "yirmi", "otuz", "kırk", "elli", "altmış", "yetmiş", "seksen", "doksan"]
DAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos",
          "Eylül", "Ekim", "Kasım", "Aralık"]


def number_to_turkish(n):
    """Tam sayiyi Turkce yaziya cevirir (0 - 999999). Piper rakamlari bazen
    yanlis okuyor, LLM'e sesli okunacak hali hazir verilir."""
    n = int(n)
    if n == 0:
        return "sıfır"
    if n < 0:
        return "eksi " + number_to_turkish(-n)
    words = []
    thousands, rest = divmod(n, 1000)
    if thousands:
        if thousands > 1:
            words.append(number_to_turkish(thousands))
        words.append("bin")
    hundreds, rest = divmod(rest, 100)
    if hundreds:
        if hundreds > 1:
            words.append(ONES[hundreds])
        words.append("yüz")
    tens, ones = divmod(rest, 10)
    if tens:
        words.append(TENS[tens])
    if ones:
        words.append(ONES[ones])
    return " ".join(words)


def spoken_time(hour, minute):
    """'14:05' -> 'on dört sıfır beş' degil, dogal: 'on dört beş'."""
    if minute == 0:
        return f"{number_to_turkish(hour)} sıfır sıfır"
    return f"{number_to_turkish(hour)} {number_to_turkish(minute)}"


def describe_now(now=None):
    """Simdiki tarih/saat - hem rakamla hem okunacak yaziyla."""
    t = time.localtime(now)
    return {
        "gun": DAYS[t.tm_wday],
        "tarih": f"{t.tm_mday} {MONTHS[t.tm_mon - 1]} {t.tm_year}",
        "tarih_yazi": f"{number_to_turkish(t.tm_mday)} {MONTHS[t.tm_mon - 1]} "
                      f"{number_to_turkish(t.tm_year)}",
        "saat": f"{t.tm_hour:02d}:{t.tm_min:02d}",
        "saat_yazi": spoken_time(t.tm_hour, t.tm_min),
    }


SAFE_OPERATORS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def safe_eval(expression):
    """Sadece sayi ve + - * / // % ** ( ) iceren ifadeyi hesaplar. eval()
    KULLANILMAZ - ifade LLM'den (dolayisiyla ag uzerinden) gelir."""
    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in SAFE_OPERATORS:
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("üs çok büyük")
            return SAFE_OPERATORS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in SAFE_OPERATORS:
            return SAFE_OPERATORS[type(node.op)](walk(node.operand))
        raise ValueError("sadece sayılar ve + - * / % ** kullanılabilir")

    expression = str(expression).replace("×", "*").replace("÷", "/").replace(",", ".")
    result = walk(ast.parse(expression, mode="eval"))
    if isinstance(result, float) and result.is_integer():
        result = int(result)
    return round(result, 6) if isinstance(result, float) else result


# --------------------------------------------------------------------------- hafiza
MEMORY_FILE = resolve_path(SK.get("memory_file", "memory.json"))
memory_lock = threading.Lock()


def read_memory():
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def publish_memory(ctx):
    """Hafizayi retained mesaj olarak yayinlar - brain sistem promptuna ekler.
    Hafiza dosyasinin sahibi skills; brain dosyayi okumaz, sadece MQTT dinler."""
    ctx.publish(SK["topic_memory"], json.dumps(read_memory(), ensure_ascii=False), retain=True)


# --------------------------------------------------------------------------- araclar
@skill(
    name="led_control",
    description="Kayıtlı bir LED cihazını yakar veya söndürür. Cihaz onay verirse 'ok' döner, "
                "cevap vermezse hata döner (o zaman kullanıcıya cihazın cevap vermediğini söyle).",
    parameters={
        "type": "object",
        "properties": {
            "device": device_param_schema("led"),
            "state": {"type": "string", "enum": ["on", "off"], "description": "LED'in yeni durumu"},
        },
        "required": ["device", "state"],
    },
)
def led_control(ctx, device, state):
    devices = devices_by_type("led")
    if device not in devices:
        raise ValueError(f"bilinmeyen LED cihazı: {device}")
    if state not in ("on", "off"):
        raise ValueError(f"geçersiz durum: {state}")
    cfg = devices[device]
    ack_topic = cfg.get("ack_topic")
    if not ack_topic:
        # Onay konusu tanimlanmamis eski cihaz: yayinla ve oldugu gibi kabul et.
        ctx.publish(cfg["topic"], state)
        return {"device": device, "state": state, "confirmed": False}
    reply = ctx.publish_and_wait(cfg["topic"], state, ack_topic,
                                 cfg.get("ack_timeout_sec", SK["device_ack_timeout_sec"]))
    if reply is None:
        raise RuntimeError("cihaz cevap vermedi (kapalı ya da ağa bağlı değil olabilir), işlem YAPILAMADI")
    if reply != state:
        raise RuntimeError(f"cihaz beklenmeyen bir durum bildirdi: {reply}")
    return {"device": device, "state": state, "confirmed": True}


@skill(
    name="get_datetime",
    description="Şu anki tarihi, günü ve saati verir. Saat, tarih ya da gün sorulduğunda MUTLAKA bunu çağır.",
    parameters={"type": "object", "properties": {}},
)
def get_datetime(ctx):
    return describe_now()


@skill(
    name="calculate",
    description="Bir aritmetik ifadeyi kesin olarak hesaplar (örn. '17*23', '(120-35)/5', '2**10'). "
                "Hesap sorulduğunda kafadan hesaplama, bunu çağır.",
    parameters={
        "type": "object",
        "properties": {"expression": {"type": "string", "description": "Python sözdiziminde aritmetik ifade"}},
        "required": ["expression"],
    },
)
def calculate(ctx, expression):
    result = safe_eval(expression)
    spoken = number_to_turkish(result) if isinstance(result, int) and abs(result) < 10**6 else None
    return {"expression": expression, "result": result, "result_yazi": spoken}


timers = {}  # etiket -> threading.Timer
timers_lock = threading.Lock()


@skill(
    name="set_timer",
    description="Bir zamanlayıcı kurar. Süre dolunca kullanıcıya sesli haber verilir.",
    parameters={
        "type": "object",
        "properties": {
            "seconds": {"type": "integer", "description": "Toplam süre, saniye cinsinden (örn. 10 dakika = 600)"},
            "label": {"type": "string", "description": "Zamanlayıcının kısa adı (örn. 'çay')"},
        },
        "required": ["seconds"],
    },
)
def set_timer(ctx, seconds, label="zamanlayıcı"):
    seconds = int(seconds)
    if not 1 <= seconds <= SK["timer_max_sec"]:
        raise ValueError(f"süre 1 ile {SK['timer_max_sec']} saniye arasında olmalı")

    def ring():
        with timers_lock:
            timers.pop(label, None)
        ctx.notify("set_timer", {"label": label, "message": f"'{label}' zamanlayıcısının süresi doldu."})

    timer = threading.Timer(seconds, ring)
    timer.daemon = True
    with timers_lock:
        old = timers.pop(label, None)
        if old:
            old.cancel()
        timers[label] = timer
    timer.start()
    return {"label": label, "seconds": seconds, "status": "kuruldu"}


@skill(
    name="cancel_timer",
    description="Kurulu zamanlayıcıyı iptal eder. label verilmezse hepsini iptal eder.",
    parameters={
        "type": "object",
        "properties": {"label": {"type": "string", "description": "İptal edilecek zamanlayıcının adı"}},
    },
)
def cancel_timer(ctx, label=None):
    with timers_lock:
        names = [label] if label else list(timers)
        cancelled = []
        for name in names:
            timer = timers.pop(name, None)
            if timer:
                timer.cancel()
                cancelled.append(name)
    if not cancelled:
        raise ValueError("iptal edilecek zamanlayıcı yok")
    return {"cancelled": cancelled}


@skill(
    name="remember",
    description="Kullanıcı hakkında kalıcı bir bilgiyi hafızaya yazar (örn. key='ad', value='Turgay'). "
                "Kullanıcı 'hatırla', 'unutma' dediğinde çağır.",
    parameters={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Bilginin kısa adı (örn. 'ad', 'şehir', 'sevdiği içecek')"},
            "value": {"type": "string", "description": "Bilginin kendisi"},
        },
        "required": ["key", "value"],
    },
)
def remember(ctx, key, value):
    with memory_lock:
        memory = read_memory()
        memory[str(key).strip()] = str(value).strip()
        MEMORY_FILE.write_text(json.dumps(memory, ensure_ascii=False, indent=1), encoding="utf-8")
    publish_memory(ctx)
    return {"saved": {key: value}}


@skill(
    name="forget",
    description="Hafızadaki bir bilgiyi siler.",
    parameters={
        "type": "object",
        "properties": {"key": {"type": "string", "description": "Silinecek bilginin adı"}},
        "required": ["key"],
    },
)
def forget(ctx, key):
    with memory_lock:
        memory = read_memory()
        if key not in memory:
            raise ValueError(f"hafızada '{key}' yok. Mevcut: {', '.join(memory) or '-'}")
        del memory[key]
        MEMORY_FILE.write_text(json.dumps(memory, ensure_ascii=False, indent=1), encoding="utf-8")
    publish_memory(ctx)
    return {"forgotten": key}


def run_process(args, timeout, cwd=None):
    """Komutu kabuk OLMADAN calistirir (arguman listesi), ciktiyi kirpar."""
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          cwd=cwd, encoding="utf-8", errors="replace")
    limit = SK["output_max_chars"]
    output = (proc.stdout + ("\n" + proc.stderr if proc.stderr.strip() else "")).strip()
    if len(output) > limit:
        output = output[:limit] + " ...(kırpıldı)"
    return {"exit_code": proc.returncode, "output": output}


# config.yaml -> commands: onceden tanimli, adlandirilmis komutlar. LLM sadece
# ADI secer (enum), komutun kendisini yazamaz - uzaktaki cihazlara (ssh) ve
# bu bilgisayara guvenli erisim yolu budur.
COMMANDS = CONFIG.get("commands") or {}

if COMMANDS:
    @skill(
        name="run_command",
        description="Önceden tanımlı bir komutu çalıştırır ve çıktısını döner. Seçenekler: "
                    + "; ".join(f"{name} = {cfg['label']}" for name, cfg in COMMANDS.items()),
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "enum": list(COMMANDS),
                                    "description": "Çalıştırılacak komutun adı"}},
            "required": ["name"],
        },
        # Herhangi bir komut onay istiyorsa arac onayli tanitilir; onaysiz
        # komutlar icin brain'in sordugu "Emin misin?" kucuk bir bedel.
        confirm=any(cfg.get("confirm") for cfg in COMMANDS.values()),
    )
    def run_command(ctx, name):
        cfg = COMMANDS.get(name)
        if cfg is None:
            raise ValueError(f"bilinmeyen komut: {name}")
        return run_process(cfg["command"], cfg.get("timeout_sec", SK["command_timeout_sec"]))


DELEGATE = SK.get("delegate") or {}

if DELEGATE.get("enabled"):
    @skill(
        name="delegate_task",
        description="Karmaşık, çok adımlı bir işi (kod yazma, proje derleme, uzak bir sunucuda "
                    "inceleme, dosya düzenleme) arka planda çalışan güçlü bir kodlama ajanına "
                    "(Claude Code) devreder. Hemen 'başladı' döner; iş bitince sonuç kullanıcıya "
                    "ayrıca sesli bildirilir. Basit sorular ve cihaz kontrolü için KULLANMA.",
        parameters={
            "type": "object",
            "properties": {"task": {"type": "string", "description": "Yapılacak işin açık, eksiksiz tarifi"}},
            "required": ["task"],
        },
        confirm=True,
    )
    def delegate_task(ctx, task):
        args = list(DELEGATE["command"]) + [str(task)]

        def work():
            try:
                result = run_process(args, DELEGATE["timeout_sec"], cwd=str(resolve_path(DELEGATE["workdir"])))
            except subprocess.TimeoutExpired:
                result = {"exit_code": None, "output": "zaman aşımına uğradı, iş yarıda kaldı"}
            except OSError as error:
                result = {"exit_code": None, "output": f"başlatılamadı: {error}"}
            ctx.notify("delegate_task", {"task": task, **result})

        threading.Thread(target=work, daemon=True).start()
        return {"status": "başladı", "task": task}


# --------------------------------------------------------------------------- MQTT
def tool_definitions():
    """brain.py'nin LLM'e tanitacagi OpenAI uyumlu arac sema listesi.
    "confirm" alani OpenAI semasinin parcasi degil - brain onu okuyup
    LLM'e gondermeden once siler."""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": entry["description"],
                "parameters": entry["parameters"],
            },
            **({"confirm": True} if entry["confirm"] else {}),
        }
        for name, entry in SKILLS.items()
    ]


def publish_tool_list(client):
    """Arac listesini retained mesaj olarak yayinlar - brain.py sonradan
    baslasa bile (ya da yeniden baglansa) mevcut araclari hemen ogrenir."""
    client.publish(
        SK["topic_list"],
        json.dumps(tool_definitions(), ensure_ascii=False),
        retain=True,
    )


def run_skill(ctx, name, params):
    entry = SKILLS.get(name)
    if entry is None:
        return {"status": "error", "data": f"bilinmeyen arac: {name}"}
    try:
        data = entry["func"](ctx, **(params or {}))
        return {"status": "ok", "data": data}
    except Exception as error:
        return {"status": "error", "data": str(error)}


def ack_topics():
    return {cfg["ack_topic"] for cfg in (CONFIG.get("devices") or {}).values() if cfg.get("ack_topic")}


def main():
    client = mqtt_client("yaver-skills")
    ctx = SkillContext(client)
    pool = ThreadPoolExecutor(max_workers=SK["workers"])
    acks = ack_topics()
    # Son vasiyet (LWT): skills kapanirsa (Ctrl+C, cokme, main.py'nin
    # terminate'i - hepsinde baglanti DISCONNECT'siz kopar) broker bos bir
    # retained yuk yayinlayip arac listesini siler. brain bunu gorunce
    # araclari LLM'e tanitmayi birakir - yoksa model olmayan bir araci
    # cagirip zaman asimina dusuyordu.
    client.will_set(SK["topic_list"], payload=b"", retain=True)

    def handle(request_id, name, params):
        print(f'  istek: {name}({params})')
        result = run_skill(ctx, name, params)
        result["id"] = request_id
        result["name"] = name
        print(f'  sonuc: {result}')
        client.publish(SK["topic_result"], json.dumps(result, ensure_ascii=False))

    @guard
    def on_message(client_, userdata, msg):
        if msg.topic in acks:
            ctx.deliver(msg.topic, msg.payload.decode("utf-8", errors="replace").strip())
            return
        packet = parse_payload(msg)
        if packet is None:
            return
        request_id = packet.get("id")
        name = packet.get("name")
        if not request_id or not name:
            return  # kimsenin beklemedigi bozuk istek - sonuc yayinlamanin anlami yok
        params = packet.get("params")
        if not isinstance(params, dict):
            params = {}
        # Ag thread'i serbest kalsin: cihaz onayi bu thread'e gelecek.
        pool.submit(handle, request_id, name, params)

    def on_connect(client_, userdata, flags, rc, properties=None):
        client_.subscribe(SK["topic_request"])
        for topic in acks:
            client_.subscribe(topic)
        publish_tool_list(client_)
        publish_memory(ctx)
        print(f"Dinleniyor: {SK['topic_request']}")
        print(f"  {len(SKILLS)} arac hazir: {', '.join(SKILLS)}")

    client.on_connect = on_connect
    client.on_message = on_message
    mqtt_connect(client, CONFIG)
    print("Skills hazir.")
    client.loop_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nskills kapandi.")
