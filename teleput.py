import logging
import aiosqlite
import os
import random
import tempfile
from aiogram import Bot, types
from aiogram.dispatcher import Dispatcher
from aiogram.dispatcher.webhook import SendMessage
from aiogram.utils.executor import set_webhook
from aiogram.utils.exceptions import TelegramAPIError
from aiohttp import web, hdrs


API_TOKEN = '1813494320:AAGRaD1guyIz5tLBw4OgRWuLTIQWegBDbnY'

WEBHOOK_HOST = 'https://telebot.textual.ru'
WEBHOOK_PATH = '/telegram'
WEBAPP_HOST = 'localhost'
WEBAPP_PORT = 3001
DATABASE = os.path.join(os.path.dirname(__file__), 'teleput.sqlite')
KEY_CHARS = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz123456789_-+=!@#$%&*/'
KEY_LENGTH = 10
MAX_FILE_SIZE = 10000000

logging.basicConfig(level=logging.INFO)

bot = Bot(API_TOKEN)
dp = Dispatcher(bot)
_db = None


async def get_db():
    global _db
    if _db is not None and _db._running:
        return _db
    _db = await aiosqlite.connect(DATABASE)
    _db.row_factory = aiosqlite.Row
    exists_query = ("select count(*) from sqlite_master where type = 'table' "
                    "and name = 'users'")
    async with _db.execute(exists_query) as cursor:
        has_tables = (await cursor.fetchone())[0] == 1
    if not has_tables:
        logging.info('Creating tables')
        q = '''\
        create table users (
            chat_id integer not null,
            token text not null,
            added_on timestamp not null default current_timestamp
        )'''
        await _db.execute(q)
        await _db.execute('create unique index users_id_idx on users (chat_id)')
        await _db.execute('create unique index users_token_idx on users (token)')
    return _db


async def on_shutdown(dp):
    await bot.delete_webhook()
    if _db is not None and _db._running:
        await _db.close()


async def on_startup(dp):
    await bot.set_webhook(WEBHOOK_HOST + WEBHOOK_PATH)


def generate_key() -> str:
    return ''.join(random.choices(KEY_CHARS, k=KEY_LENGTH))


async def find_key(chat_id: int, renew: bool = False) -> str:
    db = await get_db()
    if renew:
        await remove_user(chat_id)
        key = None
    else:
        # Get key from the db
        cursor = await db.execute('select token from users where chat_id = ?', (chat_id,))
        row = await cursor.fetchone()
        key = None if not row else row[0]
    if not key:
        key = generate_key()
        # TODO: process an index error with a duplicate token
        await db.execute('insert into users (chat_id, token) values (?, ?)', (chat_id, key))
    return key


async def find_chat(key: str) -> int:
    db = await get_db()
    cursor = await db.execute('select chat_id from users where token = ?', (key,))
    row = await cursor.fetchone()
    return None if not row else row[0]


async def remove_user(chat_id: int):
    db = await get_db()
    await db.execute('delete from users where chat_id = ?', (chat_id,))


@dp.message_handler(commands=['start'])
async def get_key(message: types.Message):
    key = await find_key(message.chat.id)
    reply = f'Your key is: {key}'
    return SendMessage(message.chat.id, reply)


@dp.message_handler(commands=['new'])
async def new_key(message: types.Message):
    key = await find_key(message.chat.id, True)
    reply = f'Your new key is: {key}\n\nNow update it in your tools and extensions.'
    return SendMessage(message.chat.id, reply)


@dp.message_handler(commands=['stop'])
async def stop(message: types.Message):
    await remove_user(message.chat.id)
    reply = 'You have been deleted. Click /start to continue using the bot.'
    return SendMessage(message.chat.id, reply)


@dp.message_handler()
async def hint(message: types.Message):
    return SendMessage(message.chat.id, 'Use /start to see your key or /new to generate a new one.')


async def post(request):
    if request.content_type == 'application/json':
        data = await request.json()
    else:
        data = await request.post()

    if 'key' not in data:
        raise web.HTTPBadRequest(reason='Missing key')
    chat_id = await find_chat(data['key'])
    if not chat_id:
        raise web.HTTPUnauthorized(reason='Incorrect key')
    if 'text' not in data:
        raise web.HTTPBadRequest(reason='Missing content')
    try:
        await bot.send_message(chat_id, data['text'])
    except Exception as e:
        raise web.HTTPServiceUnavailable(reason=f'Telegram received error {e}')
    return web.Response(text='OK')


async def post_file(request):
    reader = await request.multipart()
    chat_id = None
    raw = False
    text = None
    filename = None
    fobj = None
    while True:
        field = await reader.next()
        if field is None:
            break
        if field.name == 'key':
            chat_id = await find_chat(await field.text())
            if not chat_id:
                raise web.HTTPUnauthorized(reason='Incorrect key')
        elif field.name == 'raw':
            raw = (await field.text()) == '1'
        elif field.name == 'text':
            text = await field.text()
        elif field.name == 'media':
            size = 0
            logging.info('Multipart file received. Name %s, type %s',
                         field.filename, field.headers[hdrs.CONTENT_TYPE])
            filename = field.filename
            fobj = tempfile.TemporaryFile()
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_SIZE:
                    raise web.HTTPRequestEntityTooLarge(
                        text=f'Max upload size is {MAX_FILE_SIZE}')
                fobj.write(chunk)
    if not chat_id:
        raise web.HTTPBadRequest(reason='Missing key')
    if not text and not fobj:
        raise web.HTTPBadRequest(reason='Nothing to post')
    if not fobj:
        await bot.send_message(chat_id, text)
    else:
        raise web.HTTPNotImplemented(text='Uploading files is not implemented yet.')
    return web.Response(text='OK')


async def http_root(request):
    return web.Response(text='Teleput')


if __name__ == '__main__':
    app = web.Application()
    app.add_routes([
        web.get('/', http_root),
        web.post('/post', post),
        web.post('/upload', post_file),
    ])
    executor = set_webhook(
        dispatcher=dp,
        webhook_path=WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        web_app=app,
    )
    executor.run_app()
