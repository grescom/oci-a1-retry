# oci-a1-retry

Автоматически пытается создать бесплатный (Always Free) сервер Oracle Cloud
**Ampere A1** в регионе **Стокгольм** (`eu-stockholm-1`), пока не появятся свободные мощности.

- Конфигурация: `VM.Standard.A1.Flex`, 2 OCPU / 12 ГБ RAM, Ubuntu 24.04 (ARM), диск 100 ГБ.
- Запуск: GitHub Actions (`.github/workflows/retry.yml`) каждый час; каждый запуск
  стучится в Oracle примерно раз в минуту ~340 минут, при ответе 429 («слишком много запросов») делает паузу.
  Одновременно работает только один запуск (concurrency group `oci-a1`).
- Если сервер с именем `a1-stockholm-free` уже существует, это считается успехом.

## Как узнать результат

- **Успех:** появится issue **«A1 instance created»** с OCID и публичным IP, а workflow отключится сам.
- **Фатальная ошибка** (авторизация, лимит, неверный параметр): issue **«A1 retry failed»**, workflow тоже отключится.
- «Out of host capacity» — это нормально, просто ждём.

## Секреты (Settings → Secrets and variables → Actions)

| Секрет | Что внутри |
|---|---|
| `OCI_CONFIG` | конфиг OCI, `key_file=oci_api_key.pem` |
| `OCI_API_KEY` | приватный API-ключ (`.pem`) |
| `SSH_PUBLIC_KEY` | публичный SSH-ключ для входа на сервер |
| `SETTINGS_ENV` | OCID-ы и параметры (см. `settings.env.example`) |

Ключи и конфиг в репозитории не хранятся.

## Локальный запуск

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/python launch_a1.py --dry-run
./start.sh   # / ./stop.sh
```
