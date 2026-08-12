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
from pathlib import Path

import yaml

CONFIG = yaml.safe_load((Path(__file__).parent / "config.yaml").read_text(encoding="utf-8"))

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
    import paho.mqtt.client as mqtt

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="yaver-skills")
    except AttributeError:
        client = mqtt.Client(client_id="yaver-skills")

    def on_message(client_, userdata, msg):
        try:
            packet = json.loads(msg.payload.decode("utf-8"))
        except json.JSONDecodeError:
            return
        request_id = packet.get("id")
        name = packet.get("name")
        params = packet.get("params")
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
    client.connect(CONFIG["mqtt"]["server"], CONFIG["mqtt"]["port"], 60)
    print("Skills hazir.")
    client.loop_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nskills kapandi.")
