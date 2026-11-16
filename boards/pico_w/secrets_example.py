# Bu dosyayi secrets.py olarak kopyala ve kendi bilgilerinle doldur.
# secrets.py .gitignore'da - asla git'e eklenmez.

WIFI_SSID = "AGINIZIN_ADI"
WIFI_PASSWORD = "AGINIZIN_SIFRESI"
MQTT_SERVER = "192.168.1.193"  # Yaver PC'sinin ag IP'si (Linux: `ip -4 addr`), DHCP ile degisebilir
# Mosquitto'da kimlik dogrulama acildiysa (bkz. CLAUDE.md). Acik degilse None birak.
MQTT_USER = None
MQTT_PASSWORD = None
