# Source Sirena Web

Веб-приложение для Raspberry Pi 4/5 на стороне источника сирены. Оно пишет
NMEA, состояние входа активации сирены и, опционально, электрический сигнал
рупора в бинарный контейнер EGO и загружает завершённые файлы в S3.

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

## Формат данных

Используется тот же 72-байтовый `EGO_FRAME_HEADER` и CRC32:

- `SESSION_STARTED` / `SESSION_ENDED` — паспорт;
- `CONFIG_SNAPSHOT` — конфигурация Source;
- `GPS_FIX` — разобранные NMEA GGA/RMC;
- `TIME_STATUS` — связь monotonic time Raspberry Pi с UTC из RMC;
- `MARKER_EVENT` — JSON изменения входа сирены;
- `AUDIO_BLOCK` — PCM от ALSA.

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

## NMEA

Поддерживаются:

- USB serial: например `/dev/ttyUSB0` или стабильный путь
  `/dev/serial/by-id/...`;
- UART: обычно `/dev/serial0`;
- LAN UDP: bind-адрес и порт.

Распознаются GGA и RMC с проверкой checksum. Сырые предложения учитываются
счётчиками; в лог пишется нормализованный `GPS_FIX`.

## Вход включения сирены

Вход Raspberry Pi допускает только `0..3.3 V`. Для внешнего автомобильного
сигнала нужен формирователь:

- оптопара или цифровой изолятор;
- ограничение тока и защита от выбросов;
- общий провод только при выбранной неизолированной схеме;
- на GPIO не должно попадать 5/12/24 V.

В конфигурации используется номер GPIO в нумерации BCM. `mock=true` разрешает
кнопки имитации ON/OFF в веб-интерфейсе.

## Оцифровка сигнала рупора

У Raspberry Pi нет встроенного аналогового входа. Выход усилителя сирены нельзя
подключать к GPIO или обычному линейному входу напрямую: он может быть
дифференциальным, мостовым и иметь десятки вольт.

Рекомендуемый порядок:

1. Если усилитель имеет линейный/monitor/service output, использовать его.
2. Иначе применить рассчитанный высокоомный делитель, ограничители и
   гальваническую развязку аудиотрансформатором.
3. Подать нормированный сигнал на внешний USB Audio ADC или I2S ADC.
4. Проверить отсутствие постоянной составляющей и пиков выше диапазона АЦП.

Backend использует ALSA `arecord`. Проверка устройства:

```bash
arecord -l
arecord -D hw:1,0 -f S32_LE -r 48000 -c 1 -d 5 test.raw
```

Параметры устройства задаются на странице «Интерфейсы». Для оценки формы и
частоты достаточно одного канала 48 кГц; для измерения абсолютного напряжения
потребуется калибровка всего входного тракта.

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

## Проверки

```bash
python -m unittest discover -s tests
python -m py_compile server.py ego_log.py inputs.py
```
