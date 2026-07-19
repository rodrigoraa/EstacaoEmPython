from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError


LOCAL_TZ_NAME = "America/Campo_Grande"
try:
    LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
except ZoneInfoNotFoundError:
    # Fallback para ambientes Windows sem pacote tzdata instalado.
    # Campo Grande/MS usa UTC-04 sem horario de verao nas regras atuais.
    LOCAL_TZ = timezone(timedelta(hours=-4), LOCAL_TZ_NAME)


def agora_utc():
    return datetime.now(timezone.utc)


def agora_local():
    return agora_utc().astimezone(LOCAL_TZ)


def iso_utc(dt=None):
    dt = dt or agora_utc()
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def iso_local(dt=None):
    dt = dt or agora_utc()
    return dt.astimezone(LOCAL_TZ).replace(microsecond=0).isoformat()


def sqlite_local(dt=None):
    dt = dt or agora_utc()
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def data_local(dt=None):
    dt = dt or agora_utc()
    return dt.astimezone(LOCAL_TZ).date().isoformat()


def de_timestamp_ms_utc(timestamp_ms):
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc)


def parse_datetime(valor, assume_utc=True):
    if not valor:
        return None

    texto = str(valor).strip()
    if texto.endswith("Z"):
        texto = texto[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(texto)
    except ValueError:
        try:
            dt = datetime.strptime(texto, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    if dt.tzinfo is None:
        tz = timezone.utc if assume_utc else LOCAL_TZ
        dt = dt.replace(tzinfo=tz)

    return dt


def para_local(valor, assume_utc=True):
    dt = parse_datetime(valor, assume_utc=assume_utc)
    if not dt:
        return None
    return dt.astimezone(LOCAL_TZ)


def minutos_desde(valor, assume_utc=True):
    dt = para_local(valor, assume_utc=assume_utc)
    if not dt:
        return None

    segundos = (agora_local() - dt).total_seconds()
    return max(0, int(segundos // 60))


def formatar_local(valor, assume_utc=True):
    dt = para_local(valor, assume_utc=assume_utc)
    if not dt:
        return valor or "-"
    return dt.strftime("%d/%m/%Y %H:%M:%S")
