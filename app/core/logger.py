import logging
import sys


# ------- Formatter with Colors -------
class ColorFormatter(logging.Formatter):

    COLORS = {
        'DEBUG': "\033[0;37m",      # grey
        'INFO': "\033[0;36m",       # cyan
        'WARNING': "\033[0;33m",    # yellow
        'ERROR': "\033[0;31m",      # red
        'CRITICAL': "\033[1;41m",   # bright red bg
        'ENDC': "\033[0m",
    }

    def format(self, record):
        color = self.COLORS.get(record.levelname, "")
        endc = self.COLORS["ENDC"]
        record.msg = f"{color}{record.msg}{endc}"
        return super().format(record)


# ------- Logger Factory -------
def get_logger(name: str = "erp_ai.policy") -> logging.Logger:
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger  # already configured

    logger.setLevel(logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)

    formatter = ColorFormatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)

    logger.addHandler(handler)
    logger.propagate = False

    return logger


# Global project-level logger
logger = get_logger("erp_ai.policy")
