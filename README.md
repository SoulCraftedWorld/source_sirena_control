# Source Sirena Web

Кроссплатформенное веб-приложение на стороне источника сирены. Основной целевой
компьютер — Raspberry Pi 4/5, но сервер и режим имитации также работают на
Windows, Linux и macOS. Приложение пишет NMEA, состояние входа активации сирены
и, опционально, звук сирены с USB-микрофона в бинарный контейнер EGO, затем
может загрузить завершённые файлы в S3.

## Идентификация и объединение логов

`source_id` задаётся в `config.json` и должен быть уникальным: `1..3`.

Имя файла:

```text
SRC<source_id>_S<session>_<test_id>_R<repeat>_<UTC>.bin
```

В `SESSION_STARTED` также записываются:

- `source_id`, `source_name`, `source_role=siren_source`;
- `correlation_key=S<session>_<test_id>_R<repeat>`;
- полный JSON паспорта испытания;
- UTC начала.

Для объединения с логом EGO используются `correlation_key`, UTC и GPS-время.
Все источники должны получить одинаковые номер сессии, сценарий и повтор.

Страница **Испытания** использует тот же каталог групп и сценариев, что и
`mic_web`. На стороне Source записываются только общие идентификаторы сессии,
тип сирены, оператор и комментарий. Скорости, дистанция, погода и покрытие
вводятся только на стороне EGO. Тип сирены выбирается из того же списка:
скорая помощь, пожарная охрана, полиция, другой тип либо не задан.

## Формат данных

Используется тот же 72-байтовый `EGO_FRAME_HEADER` и CRC32:

- `SESSION_STARTED` / `SESSION_ENDED` — паспорт;
- `CONFIG_SNAPSHOT` — конфигурация Source;
- `GPS_FIX` — разобранные NMEA GGA/RMC/GSA/GST/VTG/ZDA;
- `TIME_STATUS` — связь monotonic time Raspberry Pi с UTC из RMC/ZDA;
- `MARKER_EVENT` — JSON изменения входа сирены;
- `AUDIO_BLOCK` — PCM от USB-микрофона через ALSA.

Положительный уровень GPIO означает `siren_trigger.active=true`.

## Установка Raspberry Pi OS

```bash
cd tools/source_sirena
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
python server.py
```

Открыть `http://<IP Raspberry Pi>:8084/`.

Для UART включить serial port без login shell через `raspi-config`. Пользователь
сервиса должен иметь доступ к группам `dialout`, `gpio` и `audio`.

## Запуск на Windows или обычном ПК

Конфигурация по умолчанию использует NMEA по TCP на порту `10110` и не требует
`pyserial`. Режим триггера `auto` включает Raspberry Pi GPIO на Linux, а на
Windows/macOS автоматически использует программную имитацию:

```powershell
cd tools\source_sirena
python -m pip install -r requirements.txt
python server.py --bind 127.0.0.1 --port 8085
```

Открыть `http://127.0.0.1:8085/`. Кнопки **Имитация ON/OFF** формируют те же
события лога, что и физический вход. Для NMEA через последовательный порт
выберите `USB serial`, укажите, например, `COM8`, и установите `pyserial` через
`requirements.txt`.

Если serial выбран без `pyserial` или устройство недоступно, сервер продолжает
работать, а причина показывается на странице **Интерфейсы** и в журнале.

## NMEA

Поддерживаются:

- USB serial: например `/dev/ttyUSB0` или стабильный путь
  `/dev/serial/by-id/...`;
- UART: обычно `/dev/serial0`;
- LAN TCP: bind-адрес и порт. Source Sirena слушает порт, NMEA-источник подключается к нему клиентом и передаёт строки GGA/RMC/GSA/GST/VTG/ZDA.

Распознаются GGA, RMC, GSA, GST, VTG и ZDA с проверкой checksum. Сырые
предложения учитываются счётчиками; в лог пишется нормализованный `GPS_FIX`
с UTC, координатами, высотой, типом решения, спутниками, HDOP/PDOP/VDOP,
age/base station, скоростью, курсом и ошибками GST.

## Вход включения сирены

Вход Raspberry Pi допускает только `0..3.3 V`. Для внешнего автомобильного
сигнала нужен формирователь:

- оптопара или цифровой изолятор;
- ограничение тока и защита от выбросов;
- общий провод только при выбранной неизолированной схеме;
- на GPIO не должно попадать 5/12/24 V.

В конфигурации используется номер GPIO в нумерации BCM. Параметр `mode`
поддерживает три режима:

- `auto` — GPIO на Linux при наличии `gpiozero`, иначе имитация;
- `gpio` — строгий физический режим без автоматического fallback;
- `mock` — программная имитация ON/OFF.

## USB-микрофон

Звук сирены записывается акустически USB-микрофоном. Электрический выход
усилителя рупора к Raspberry Pi или микрофону не подключается.

Backend Raspberry Pi использует ALSA `arecord`. Проверка и выбор устройства:

```bash
arecord -l
arecord -D hw:1,0 -f S32_LE -r 48000 -c 1 -d 5 test.raw
```

Имя ALSA-устройства, частота, число каналов, формат и размер блока задаются на
странице **Интерфейсы**. Для фиксации акустического сигнала достаточно одного
канала 48 кГц. На Windows сервер запускается с выключенной аудиозаписью;
текущий backend захвата USB-микрофона использует Linux ALSA.

## S3

`boto3` поддерживает AWS S3 и совместимые хранилища. Secret key и session token,
введённые через web, хранятся только до перезапуска процесса. Для systemd:

```text
SOURCE_SIRENA_S3_SECRET_ACCESS_KEY=...
SOURCE_SIRENA_S3_SESSION_TOKEN=...
```

Остальные параметры сохраняются в `config.json`.

## systemd

Скопировать и отредактировать пример:

```bash
sudo cp source-serena.service.example /etc/systemd/system/source-serena.service
sudo systemctl daemon-reload
sudo systemctl enable --now source-serena
sudo journalctl -u source-serena -f
```

## Отправка лога в LocalPC

После Stop приложение может отправлять готовый `.bin` в LocalPC по TCP. LocalPC
слушает `source_log_receiver.py` на порту `10201`, принимает JSON header первой
строкой и затем сырые байты файла.

Минимальный блок в `config.json`:

```json
"localpc": {
  "enabled": true,
  "host": "192.168.8.11",
  "port": 10201,
  "connect_timeout_s": 3.0,
  "retry_window_s": 20.0,
  "retry_interval_s": 2.0
}
```

Если LocalPC временно недоступен, Source Sirena повторяет попытки в пределах
`retry_window_s`. Остановка записи при этом не блокируется.

## Проверки

```bash
python -m unittest discover -s tests
python -m py_compile server.py ego_log.py inputs.py
```
