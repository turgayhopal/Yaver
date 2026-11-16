"""Yaver - ortak yardimcilar (common).

Her modulun tekrar ettigi kucuk isler: config.yaml'i okumak, goreli yollari
cozmek, MQTT istemcisi kurmak ve gelen MQTT yukunu guvenle cozmek.

Bu bir kutuphane, modul (surec) degil: MQTT'ye hicbir sey yayinlamaz ve
hicbir modulun varligini bilmez. Moduller arasi iletisim yine sadece MQTT.
"""

import functools
import json
import os
import re
import traceback
import unicodedata
from pathlib import Path

import yaml

ROOT = Path(__file__).parent


def load_config():
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def load_secrets():
    """Kimlik bilgileri (API anahtari, MQTT sifresi) config.yaml'da DEGIL,
    git'e girmeyen secrets.yaml'da durur (ornek: secrets_example.yaml).
    Dosya yoksa bos sozluk - kimlik bilgisi gerektirmeyen kurulum calisir."""
    path = ROOT / "secrets.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def secret(name, env_var=None):
    """Bir kimlik bilgisini once secrets.yaml'dan, yoksa ortam degiskeninden okur."""
    value = load_secrets().get(name)
    if not value and env_var:
        value = os.environ.get(env_var)
    return value or None


def resolve_path(value):
    """config.yaml'daki bir yolu cozer: goreli yollar proje kokune gore,
    '~' kullanicinin ev dizinine gore. Boylece config.yaml'da makineye
    ozel mutlak yol (C:/..., /home/...) yazmak gerekmez."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def mqtt_client(client_id):
    """paho 2.x ve 1.x ile uyumlu MQTT istemcisi."""
    import paho.mqtt.client as mqtt

    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except AttributeError:  # paho 1.x
        return mqtt.Client(client_id=client_id)


def mqtt_connect(client, config):
    """Broker'a baglanir. Broker kapaliysa anlasilir bir mesajla cikar -
    ham bir ConnectionRefusedError yigini yerine ne yapilacagini soyler."""
    server, port = config["mqtt"]["server"], config["mqtt"]["port"]
    # Kimlik dogrulama (bkz. CLAUDE.md "Mosquitto kimlik dogrulama"): kullanici
    # adi config.yaml'da, sifre secrets.yaml'da. username null = anonim.
    username = config["mqtt"].get("username")
    if username:
        client.username_pw_set(username, secret("mqtt_password", "YAVER_MQTT_PASSWORD"))
    try:
        client.connect(server, port, 60)
    except OSError as error:
        raise SystemExit(
            f"MQTT broker'a baglanilamadi ({server}:{port}): {error}\n"
            "Mosquitto calisiyor mu? (orn. 'systemctl start mosquitto' ya da 'mosquitto -v')"
        )


def parse_payload(msg):
    """MQTT yukunu bir JSON nesnesi (dict) olarak cozer; bozuksa None.

    paho, bir callback icinde firlatilan istisnayi yeniden firlatip ag
    dongusunu (loop_forever) olduruyor - yani '123' ya da gecersiz UTF-8
    gibi tek bir bozuk mesaj tum modulu devirebiliyordu. Broker LAN'a
    kimlik dogrulamasiz acik oldugu icin bu mesaj her yerden gelebilir.
    """
    try:
        packet = json.loads(msg.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return packet if isinstance(packet, dict) else None


def text_field(packet, key="text"):
    """Paketteki metin alanini guvenle okur: yoksa, None ya da metin
    degilse bos string."""
    value = packet.get(key)
    return value.strip() if isinstance(value, str) else ""


# LLM sistem promptunda emoji kullanma dense de uretebiliyor (olculdu: gecmiste
# emoji varken 15 cevabin 15'inde). Piper bunlari tuhaf okumaya calisiyor.
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


def normalize(text):
    """Karsilastirma icin sadelestirir: kucuk harf, aksansiz (s/c/g/o/u),
    noktasiz i, noktalama yok, tek bosluk. Ornek: 'Altyazi M.K.' -> 'altyazi mk'.
    ear (cop filtresi, tetik/dur kelimesi) ve brain (tekrar filtresi) ortak kullanir."""
    text = unicodedata.normalize("NFKD", text.lower())
    # combining: aksan isaretleri (ve lower()'in 'I' (buyuk noktali) icin
    # biraktigi nokta) ayri karakter olarak duser. Noktasiz i ayristirilamaz, elle.
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).replace(chr(0x131), "i")
    text = re.sub(r"[^\w\s]", "", text)
    return " ".join(text.split())


def strip_emoji(text):
    text = EMOJI_PATTERN.sub("", text)
    return re.sub(r" {2,}", " ", text).strip()


def guard(callback):
    """MQTT callback'ini sarar: beklenmedik bir hata yazdirilir ama ag
    dongusunu oldurmez (bkz. parse_payload). Modul calismaya devam eder."""
    @functools.wraps(callback)
    def wrapper(*args, **kwargs):
        try:
            return callback(*args, **kwargs)
        except Exception:
            print(f"  {callback.__name__} hatasi (mesaj atlandi):")
            traceback.print_exc()
    return wrapper
