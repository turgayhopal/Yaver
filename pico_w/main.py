"""Yaver - Pico W LED demo.

Yaver'in "led_control" skill'inden gelen MQTT mesajlarini dinler, dahili
LED'i yakar/sondurur. skills.py bu cihaza yaver/device/pico-led konusunda
"on" ya da "off" yayinlar (bkz. C:\\yaver\\config.yaml devices.pico_led).

Kurulum:
    1. Thonny'de Tools > Manage Packages ile "micropython-umqtt.simple" kur
       (ya da REPL'den: import mip; mip.install("umqtt.simple")).
    2. secrets_example.py'yi secrets.py olarak kopyala, kendi WiFi ve
       Mosquitto sunucu bilgilerinle doldur (secrets.py .gitignore'da,
       asla commit edilmez).
    3. main.py ve secrets.py dosyalarini Thonny ile Pico W'ye yukle, calistir.
"""

import time

import network
from machine import Pin
from umqtt.simple import MQTTClient

import secrets

TOPIC = b"yaver/device/pico-led"
led = Pin("LED", Pin.OUT)


def wifi_connect():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print("WiFi'ye baglaniliyor:", secrets.WIFI_SSID)
        wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)
        while not wlan.isconnected():
            time.sleep(0.5)
    print("WiFi baglandi:", wlan.ifconfig()[0])


def on_message(topic, msg):
    print("mesaj:", topic, msg)
    if msg == b"on":
        led.value(1)
    elif msg == b"off":
        led.value(0)


def main():
    wifi_connect()
    client = MQTTClient("yaver-pico-led", secrets.MQTT_SERVER, port=1883)
    client.set_callback(on_message)
    client.connect()
    client.subscribe(TOPIC)
    print("MQTT baglandi, dinleniyor:", TOPIC)

    while True:
        try:
            client.wait_msg()
        except OSError as error:
            print("baglanti hatasi, yeniden deneniyor:", error)
            time.sleep(2)
            try:
                client.disconnect()
            except OSError:
                pass
            wifi_connect()
            client.connect()
            client.subscribe(TOPIC)


if __name__ == "__main__":
    main()
