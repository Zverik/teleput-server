import logging
import aiosqlite
import random
import tempfile
import config
import aiohttp_cors
from aiogram import Bot, types
from aiogram.dispatcher import Dispatcher
from aiogram.dispatcher.webhook import SendMessage, DEFAULT_ROUTE_NAME, WebhookRequestHandler
from aiogram.utils.executor import set_webhook, Executor
from aiogram.utils import exceptions
from aiohttp import web, hdrs


# logging.basicConfig(level=logging.INFO)
bot = Bot(config.API_TOKEN)
dp = Dispatcher(bot)
_db = None


async def get_db():
    global _db
    if _db is not None and _db._running:
        return _db
    _db = await aiosqlite.connect(config.DATABASE)
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
        await _db.commit()
    return _db


async def on_shutdown(dp):
    await bot.delete_webhook()
    if _db is not None and _db._running:
        await _db.close()


async def on_startup(dp):
    await bot.set_webhook(config.WEBHOOK_HOST + config.WEBHOOK_PATH)


def generate_key() -> str:
    return ''.join(random.choices(config.KEY_CHARS, k=config.KEY_LENGTH))


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
        for i in range(3):
            try:
                key = generate_key()
                await db.execute('insert into users (chat_id, token) values (?, ?)', (chat_id, key))
                await db.commit()
                break
            except aiosqlite.IntegrityError:
                key = None
    return key


async def find_chat(key: str) -> int:
    db = await get_db()
    cursor = await db.execute('select chat_id from users where token = ?', (key,))
    row = await cursor.fetchone()
    return None if not row else row[0]


async def remove_user(chat_id: int):
    db = await get_db()
    await db.execute('delete from users where chat_id = ?', (chat_id,))
    await db.commit()


def check_group_admin(func):
    async def wrapped(message: types.Message):
        if message.chat.type in (types.ChatType.GROUP, types.ChatType.SUPERGROUP):
            member = message.chat.getChatMember(message.from_user.id)
            if not member.is_chat_admin():
                await message.reply('Only an admin can use this bot.')
                return
        return await func(message)
    return wrapped


@check_group_admin
@dp.message_handler(commands=['start'])
async def get_key(message: types.Message):
    key = await find_key(message.chat.id)
    if not key:
        return SendMessage(message.from_user.id,
                           'Failed to create a key, please try again.')
    reply = f'Your key is: {key}'
    return SendMessage(message.from_user.id, reply)


@check_group_admin
@dp.message_handler(commands=['new'])
async def new_key(message: types.Message):
    key = await find_key(message.chat.id, True)
    if not key:
        return SendMessage(message.from_user.id, 'Failed to create a key, please try again.')
    reply = f'Your new key is: {key}\n\nNow update it in your tools and extensions.'
    return SendMessage(message.from_user.id, reply)


@check_group_admin
@dp.message_handler(commands=['stop'])
async def stop(message: types.Message):
    await remove_user(message.chat.id)
    reply = 'You have been deleted. Click /start to continue using the bot.'
    return SendMessage(message.from_user.id, reply)


@check_group_admin
@dp.message_handler()
async def hint(message: types.Message):
    if message.chat.type in (types.ChatType.PRIVATE,):
        return SendMessage(
            message.chat.id,
            'Use /start to see your key or /new to generate a new one.')


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
        raise web.HTTPServiceUnavailable(reason=f'Error while sending the message: {e}')
    return web.Response(text='OK')


async def post_file(request):
    reader = await request.multipart()
    chat_id = None
    raw = False
    text = None
    filename = None
    mime = None
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
            filename = field.filename
            mime = field.headers.get(hdrs.CONTENT_TYPE)
            logging.info('Multipart file received. Name %s, type %s', filename, mime)
            fobj = tempfile.TemporaryFile()
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                size += len(chunk)
                if size > config.MAX_FILE_SIZE:
                    raise web.HTTPRequestEntityTooLarge(
                        text=f'Max upload size is {config.MAX_FILE_SIZE}')
                fobj.write(chunk)
            fobj.seek(0)
    if not chat_id:
        raise web.HTTPBadRequest(reason='Missing key')
    if not text and not fobj:
        raise web.HTTPBadRequest(reason='Nothing to post')
    try:
        if not fobj:
            await bot.send_message(chat_id, text)
        else:
            if not mime or raw:
                filetype = 'document'
            elif '/jpeg' in mime or '/png' in mime:
                filetype = 'photo'
            elif '/mp4' in mime:
                filetype = 'video'
            elif 'audio/mpeg' in mime or 'm4a' in mime:
                filetype = 'audio'
            elif 'audio/ogg' in mime:
                filetype = 'voice'
            elif 'image/gif' in mime:
                filetype = 'animation'
            else:
                filetype = 'document'

            input_file = types.InputFile(fobj, filename)
            if filetype == 'document':
                await bot.send_document(chat_id, input_file, caption=text)
            elif filetype == 'photo':
                await bot.send_photo(chat_id, input_file, caption=text)
            elif filetype == 'video':
                await bot.send_video(chat_id, input_file, caption=text)
            elif filetype == 'audio':
                await bot.send_audio(chat_id, input_file, caption=text)
            elif filetype == 'voice':
                await bot.send_voice(chat_id, input_file, caption=text)
            elif filetype == 'animation':
                await bot.send_animation(chat_id, input_file, caption=text)
            else:
                raise web.HTTPNotImplemented(reason=f'Cannot send file with type {filetype}.')
    except exceptions.BotBlocked:
        raise web.HTTPForbidden(reason='User has blocked the bot')
    except exceptions.ChatNotFound:
        raise web.HTTPGone(reason='Chat_id is obsolete')
    except exceptions.RetryAfter as e:
        raise web.HTTPTooManyRequests(reason=f'Flood limit exceeded, wait {e.timeout} seconds')
    except exceptions.UserDeactivated:
        raise web.HTTPGone(reason='User is deactivated')
    except exceptions.TelegramAPIError as e:
        raise web.HTTPServiceUnavailable(reason=f'Telegram API Error: {e}')
    return web.Response(text='OK')


async def http_root(request):
    return web.Response(text='Teleput')


async def set_webhook_async(
        dispatcher: Dispatcher, webhook_path: str, *, loop=None,
        skip_updates: bool = None, on_startup=None,
        on_shutdown=None, check_ip: bool = False,
        retry_after=None, route_name: str = DEFAULT_ROUTE_NAME,
        web_app=None):
    """Rewriting from aiogram/utils/executor.py to support running inside a loop."""
    executor = Executor(dispatcher, skip_updates=skip_updates, check_ip=check_ip,
                        retry_after=retry_after, loop=loop)
    if on_startup is not None:
        executor.on_startup(on_startup)
    if on_shutdown is not None:
        executor.on_shutdown(on_shutdown)

    executor._prepare_webhook(webhook_path, WebhookRequestHandler, route_name, web_app)
    await executor._startup_webhook()
    return executor


def make_app():
    app = web.Application()
    app.add_routes([
        web.get('/', http_root),
        web.post('/post', post),
        web.post('/upload', post_file),
    ])
    # Enable CORS. What an abysmal API!
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(allow_credentials=False, allow_headers='*')
    })
    for route in list(app.router.routes()):
        cors.add(route)
    return app


async def async_app():
    app = make_app()
    await set_webhook_async(
        dispatcher=dp,
        webhook_path=config.WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        web_app=app,
    )
    return app


if __name__ == '__main__':
    executor = set_webhook(
        dispatcher=dp,
        webhook_path=config.WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        web_app=make_app(),
    )
    executor.run_app(host=config.WEBAPP_HOST, port=config.WEBAPP_PORT)
