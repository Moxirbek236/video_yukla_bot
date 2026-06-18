#!/usr/bin/env python3
# coding: utf-8

# ytdlbot - __init__.py.py

import logging

from dotenv import load_dotenv

load_dotenv()

from .config import *
from .constant import *

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s %(filename)s:%(lineno)d %(levelname).1s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
