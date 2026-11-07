"""Yaver - skills module (yetenekler).

LLM'in arac (tool) olarak cagirabilecegi fonksiyonlari barindirir. brain.py'den
MQTT uzerinden istek alir, ilgili fonksiyonu calistirir, sonucu geri yayinlar.
Baska hicbir modulun varligini bilmez.

Yeni bir arac eklemek icin: asagidaki gibi @skill dekoratoruyla bir fonksiyon
yaz. Cekirdek koda (main, run_skill, MQTT akisi) dokunmak gerekmez - brain.py
da yeni araci otomatik ogrenir (bkz. tool_definitions/publish_tool_list).

Kullanim:
    python skills.py
"""

import json

from common import guard, load_config, mqtt_client, mqtt_connect, parse_payload

CONFIG = load_config()

SKILLS = {}


def skill(name, description, parameters):
    """Fonksiyonu SKILLS registry'sine kaydeden dekorator.

    parameters: OpenAI/llama.cpp tool-calling'in bekledigi JSON-schema sozlugu.
    """
    def decorator(func):
        SKILLS[name] = {"func": func, "description": description, "parameters": parameters}
        return func
    return decorator


def devices_by_type(device_type):
    """config.yaml'daki genel cihaz kayit defterinden belirli turdeki (orn.
    "led") tum cihazlari dondurur. Yeni bir skill de kendi turunu boyle
    sorgular - cihaz eklemek/cikarmak icin skill kodu degismez."""
    return {
        name: cfg for name, cfg in CONFIG.get("devices", {}).items()
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
        "description": f"Hangi cihaz kontrol edilecek. Secenekler: {descriptions}",
    }


# --------------------------------------------------------------------------- araclar
@skill(
    name="led_control",
    description="Kayitli bir LED cihazini yakar veya sondurur.",
    parameters={
        "type": "object",
        "properties": {
            "device": device_param_schema("led"),
            "state": {"type": "string", "enum": ["on", "off"], "description": "LED'in yeni durumu"},
        },
        "required": ["device", "state"],
    },
)
def led_control(client, device, state):
    devices = devices_by_type("led")
    if device not in devices:
        raise ValueError(f"bilinmeyen LED cihazi: {device}")
    client.publish(devices[device]["topic"], state)
    return {"device": device, "state": state}


# --------------------------------------------------------------------------- MQTT
def tool_definitions():
    """brain.py'nin LLM'e tanitacagi OpenAI uyumlu arac sema listesi."""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": entry["description"],
                "parameters": entry["parameters"],
            },
        }
        for name, entry in SKILLS.items()
    ]


def publish_tool_list(client):
    """Arac listesini retained mesaj olarak yayinlar - brain.py sonradan
    baslasa bile (ya da yeniden baglansa) mevcut araclari hemen ogrenir."""
    client.publish(
        CONFIG["skills"]["topic_list"],
        json.dumps(tool_definitions(), ensure_ascii=False),
        retain=True,
    )


def run_skill(client, name, params):
    entry = SKILLS.get(name)
    if entry is None:
        return {"status": "error", "data": f"bilinmeyen arac: {name}"}
    try:
        data = entry["func"](client, **(params or {}))
        return {"status": "ok", "data": data}
    except Exception as error:
        return {"status": "error", "data": str(error)}


def main():
    client = mqtt_client("yaver-skills")
    # Son vasiyet (LWT): skills kapanirsa (Ctrl+C, cokme, main.py'nin
    # terminate'i - hepsinde baglanti DISCONNECT'siz kopar) broker bos bir
    # retained yuk yayinlayip arac listesini siler. brain bunu gorunce
    # araclari LLM'e tanitmayi birakir - yoksa model olmayan bir araci
    # cagirip zaman asimina dusuyordu.
    client.will_set(CONFIG["skills"]["topic_list"], payload=b"", retain=True)

    @guard
    def on_message(client_, userdata, msg):
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
        print(f'  istek: {name}({params})')
        result = run_skill(client_, name, params)
        result["id"] = request_id
        result["name"] = name
        print(f'  sonuc: {result}')
        client_.publish(CONFIG["skills"]["topic_result"], json.dumps(result, ensure_ascii=False))

    def on_connect(client_, userdata, flags, rc, properties=None):
        client_.subscribe(CONFIG["skills"]["topic_request"])
        publish_tool_list(client_)
        print(f"Dinleniyor: {CONFIG['skills']['topic_request']}")
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
