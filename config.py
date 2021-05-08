import os

WEBAPP_HOST = 'localhost'
WEBAPP_PORT = 3001
DATABASE = os.path.join(os.path.dirname(__file__), 'teleput.sqlite')
KEY_CHARS = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz123456789_-+=!@#$%&*/'
KEY_LENGTH = 10
MAX_FILE_SIZE = 10000000

# Override these in config_local.py
API_TOKEN = ''
WEBHOOK_HOST = 'https://teleput.org'
WEBHOOK_PATH = '/'

from config_local import *  # noqa
