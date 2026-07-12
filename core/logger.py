import logging
import os
import sys
from datetime import datetime

class Logger:
    _instance = None

    def __new__(cls, name="shopee_pro"):
        if cls._instance is None:
            cls._instance = super(Logger, cls).__new__(cls)
            cls._instance._setup_logger(name)
        return cls._instance.logger

    def _setup_logger(self, name):
        self.logger = logging.getLogger(name)
        self.logger.propagate = False
        
        # Get log level from env, default to INFO
        level_name = os.getenv("LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)
        self.logger.setLevel(level)

        # Console Handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        
        # Format: [TIME] [LEVEL] [NAME] MESSAGE
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(message)s',
            datefmt='%H:%M:%S'
        )
        console_handler.setFormatter(formatter)
        
        if not self.logger.handlers:
            self.logger.addHandler(console_handler)

def get_logger(name="shopee_pro"):
    return Logger(name)
