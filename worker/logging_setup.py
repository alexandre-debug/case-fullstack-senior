import json, logging, sys
from datetime import datetime, timezone

# Mesmo formato de api/logging_setup.py (a API e o worker são imagens separadas, cada uma com seu contexto de build).
class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "service": "worker",
            "msg": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)

def configure_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)

logger = logging.getLogger("relay.worker")

def log(msg: str, level: int = logging.INFO, exc_info=None, **fields):
    logger.log(level, msg, exc_info=exc_info, extra={"fields": fields})
