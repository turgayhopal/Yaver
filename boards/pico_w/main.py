"""Yaver - Pico W LED demo (cihaz kimligi: pico_led_masa).

Yaver'in "led_control" skill'inden gelen MQTT mesajlarini dinler, dahili
LED'i yakar/sondurur ve uyguladigi durumu STATE_TOPIC'e yazar - skill bu
onayi bekler, gelmezse Yaver "cihaz cevap vermedi" der (onceden Pico
kapaliyken de "yaktim" diyordu).

Baska bir Pico W/LED eklerken: config.yaml'a yeni bir devices girdisi ekle
(farkli topic/ack_topic ile), bu dosyayi kopyalayip DEVICE_ID'yi degistir.

Dayaniklilik: WiFi ya da broker yokken (acilista ya da sonradan) cokmez,
RETRY_SEC aralikla yeniden dener. Keepalive ping'i ile kopan baglanti fark edilir.

Kurulum:
    1. Thonny'de Tools > Manage Packages ile "micropython-umqtt.simple" kur
       (ya da REPL'den: import mip; mip.install("umqtt.simple")).
    2. secrets_example.py'yi secrets.py olarak kopyala, doldur
       (secrets.py .gitignore'da, asla commit edilmez).
    3. main.py ve secrets.py dosyalarini Thonny ile Pico W'ye yukle.
"""

import time

import network
from machine import Pin
from umqtt.simple import MQTTClient

import secrets

DEVICE_ID = "pico_led_masa"
TOPIC = b"yaver/device/" + DEVICE_ID.encode()
STATE_TOPIC = TOPIC + b"/state"
KEEPALIVE_SEC = 30
PING_SEC = 10
RETRY_SEC = 3

led = Pin("LED", Pin.OUT)
client = None


def wifi_connect():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        return
    print("WiFi'ye baglaniliyor:", secrets.WIFI_SSID)
    wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)
    for _ in range(40):  # ~20 sn
        if wlan.isconnected():
            print("WiFi baglandi:", wlan.ifconfig()[0])
            return
        time.sleep(0.5)
    raise OSError("WiFi baglanamadi")


def publish_state():
    client.publish(STATE_TOPIC, b"on" if led.value() else b"off", retain=True)


def on_message(topic, msg):
    print("mesaj:", topic, msg)
    if msg == b"on":
        led.value(1)
    elif msg == b"off":
        led.value(0)
    else:
        return
    publish_state()  # onay: skill bunu bekliyor


def mqtt_connect():
    global client
    client = MQTTClient("yaver-" + DEVICE_ID, secrets.MQTT_SERVER, port=1883,
                        user=getattr(secrets, "MQTT_USER", None),
                        password=getattr(secrets, "MQTT_PASSWORD", None),
                        keepalive=KEEPALIVE_SEC)
    client.set_callback(on_message)
    client.connect()
    client.subscribe(TOPIC)
    publish_state()
    print("MQTT baglandi, dinleniyor:", TOPIC)


def run():
    wifi_connect()
    mqtt_connect()
    last_ping = time.time()
    while True:
        client.check_msg()  # bloklamaz
        if time.time() - last_ping >= PING_SEC:
            client.ping()
            last_ping = time.time()
        time.sleep(0.05)


def main():
    while True:
        try:
            run()
        except Exception as error:  # OSError, MQTTException... hepsinde yeniden dene
            print("baglanti yok, yeniden denenecek:", error)
            try:
                if client:
                    client.disconnect()
            except Exception:
                pass
            time.sleep(RETRY_SEC)


if __name__ == "__main__":
    main()
