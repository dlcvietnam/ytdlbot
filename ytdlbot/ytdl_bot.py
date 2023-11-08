#!/usr/local/bin/python3
# coding: utf-8

# ytdlbot - new.py
# 8/14/21 14:37
#

__author__ = "Benny <benny.think@gmail.com>"

import logging
import os
import random
import re
import time
import traceback
import typing
from io import BytesIO

import pyrogram.errors
from apscheduler.schedulers.background import BackgroundScheduler
from pyrogram import Client, filters, types
from pyrogram.errors.exceptions.bad_request_400 import UserNotParticipant
from pyrogram.raw import functions
from pyrogram.raw import types as raw_types
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from token_bucket import Limiter, MemoryStorage
from urllib.parse import parse_qs, urlparse, unquote
from os.path import splitext, basename
from client_init import create_app
from config import (AUTHORIZED_USER, BURST, ENABLE_CELERY, ENABLE_FFMPEG,
                    ENABLE_VIP, MULTIPLY, OWNER, PROVIDER_TOKEN, QUOTA, RATE,
                    REQUIRED_MEMBERSHIP, ARCHIVE_ID, URL_ARRAY, PLAYLIST_SUPPORT)
from constant import BotText
from db import InfluxDB, MySQL, Redis
from limit import VIP, verify_payment, admin_add_vip
from tasks import app as celery_app
from tasks import (audio_entrance, direct_download_entrance, hot_patch,
                   purge_tasks, ytdl_download_entrance, image_entrance)
from utils import (auto_restart, clean_tempfile, customize_logger,
                   get_revision, get_user_settings, set_user_settings, tbcn, qr1688)

customize_logger(["pyrogram.client", "pyrogram.session.session", "pyrogram.connection.connection"])
logging.getLogger('apscheduler.executors.default').propagate = False

app = create_app()
bot_text = BotText()

logging.info("Authorized users are %s", AUTHORIZED_USER)

# rate, capacity
mem = MemoryStorage()
# 5 minutes, 2 bursts
lim = Limiter(1 / RATE, BURST, mem)


def private_use(func):
    def wrapper(client: "Client", message: "types.Message"):
        chat_id = getattr(message.from_user, "id", None)

        # message type check
        if message.chat.type != "private" and not message.text.lower().startswith("/ytdl"):
            logging.warning("%s, it's annoying me...🙄️ ", message.text)
            return

        # authorized users check
        if AUTHORIZED_USER:
            users = [int(i) for i in AUTHORIZED_USER.split(",")]
        else:
            users = []

        if users and chat_id and chat_id not in users:
            message.reply_text(bot_text.private, quote=True)
            return

        # membership check
        if REQUIRED_MEMBERSHIP:
            try:
                mship = app.get_chat_member(REQUIRED_MEMBERSHIP, chat_id).status
                if mship != "member" and mship != "creator" and mship != "administrator" and mship != "owner":
                    logging.info(mship)
                    raise UserNotParticipant()
                else:
                    logging.info(mship)
                    logging.info("user %s check passed for group/channel %s.", chat_id, REQUIRED_MEMBERSHIP)
            except UserNotParticipant:
                logging.warning("user %s is not a member of group/channel %s", chat_id, REQUIRED_MEMBERSHIP)
                message.reply_text(bot_text.membership_require, quote=True)
                return
        return func(client, message)

    return wrapper


@app.on_message(filters.command(["start"]))
def start_handler(client: "Client", message: "types.Message"):
    from_id = message.from_user.id
    # from_user = message.from_user.username
    logging.info("%s welcome to youtube-dl bot!", message.from_user.id)
    client.send_chat_action(from_id, "typing")
    greeting = bot_text.get_vip_greeting(from_id)
    quota = bot_text.remaining_quota_caption(from_id)
    custom_text = bot_text.custom_text
    text = f"{greeting}{bot_text.start}\n\n{quota}\n{custom_text}"
    client.send_message(message.chat.id, text, disable_web_page_preview=True)
    try:
        user_info = "@{} ({}) - {}".format(
            message.from_user.username or "",
            message.from_user.first_name or "" + message.from_user.last_name or "",
            message.from_user.id
        )
    except Exception:
        user_info = ""
    newuser = f"Thành viên mới \n{user_info}"
    client.send_message(ARCHIVE_ID, newuser)


@app.on_message(filters.command(["help"]))
def help_handler(client: "Client", message: "types.Message"):
    chat_id = message.chat.id
    client.send_chat_action(chat_id, "typing")
    client.send_message(chat_id, bot_text.help, disable_web_page_preview=True)


@app.on_message(filters.command(["sub"]))
def subscribe_handler(client: "Client", message: "types.Message"):
    vip = VIP()
    chat_id = message.chat.id
    client.send_chat_action(chat_id, "typing")
    if message.text == "/sub":
        result = vip.get_user_subscription(chat_id)
    else:
        link = message.text.split()[1]
        try:
            result = vip.subscribe_channel(chat_id, link)
        except (IndexError, ValueError):
            result = f"Error: \n{traceback.format_exc()}"
    client.send_message(chat_id, result or "You have no subscription.", disable_web_page_preview=True)


@app.on_message(filters.command(["unsub"]))
def unsubscribe_handler(client: "Client", message: "types.Message"):
    vip = VIP()
    chat_id = message.chat.id
    client.send_chat_action(chat_id, "typing")
    text = message.text.split(" ")
    if len(text) == 1:
        client.send_message(chat_id, "/unsubscribe channel_id", disable_web_page_preview=True)
        return

    rows = vip.unsubscribe_channel(chat_id, text[1])
    if rows:
        text = f"Unsubscribed from {text[1]}"
    else:
        text = "Unable to find the channel."
    client.send_message(chat_id, text, disable_web_page_preview=True)


@app.on_message(filters.command(["patch"]))
def patch_handler(client: "Client", message: "types.Message"):
    username = message.from_user.username
    chat_id = message.chat.id
    if username == OWNER:
        celery_app.control.broadcast("hot_patch")
        client.send_chat_action(chat_id, "typing")
        client.send_message(chat_id, "Oorah!")
        hot_patch()


@app.on_message(filters.command(["uncache"]))
def uncache_handler(client: "Client", message: "types.Message"):
    username = message.from_user.username
    link = message.text.split()[1]
    if username == OWNER:
        count = VIP().del_cache(link)
        message.reply_text(f"{count} cache(s) deleted.", quote=True)


@app.on_message(filters.command(["purge"]))
def purge_handler(client: "Client", message: "types.Message"):
    username = message.from_user.username
    if username == OWNER:
        message.reply_text(purge_tasks(), quote=True)


@app.on_message(filters.command(["ping"]))
def ping_handler(client: "Client", message: "types.Message"):
    chat_id = message.chat.id
    client.send_chat_action(chat_id, "typing")
    stats = bot_text.ping_worker()[:1000]
    # if os.uname().sysname == "Darwin" or ".heroku" in os.getenv("PYTHONHOME", ""):
    #     bot_info = "ping unavailable."
    # else:
    #     bot_info = get_runtime("ytdlbot-ytdl-1", "Taobao Bot")
    if message.chat.username == OWNER:
        client.send_document(chat_id, Redis().generate_file(), caption=f"{stats}")
    else:
        client.send_message(chat_id, f"{stats}")


@app.on_message(filters.command(["about"]))
def about_handler(client: "Client", message: "types.Message"):
    chat_id = message.chat.id
    client.send_chat_action(chat_id, "typing")
    client.send_message(chat_id, bot_text.about)


@app.on_message(filters.command(["sub_count"]))
def sub_count_handler(client: "Client", message: "types.Message"):
    username = message.from_user.username
    chat_id = message.chat.id
    if username == OWNER:
        with BytesIO() as f:
            f.write(VIP().sub_count().encode("u8"))
            f.name = "subscription count.txt"
            client.send_document(chat_id, f)


@app.on_message(filters.command(["direct"]))
def direct_handler(client: "Client", message: "types.Message"):
    chat_id = message.from_user.id
    client.send_chat_action(chat_id, "typing")
    url = re.sub(r'/direct\s*', '', message.text)
    logging.info("direct start %s", url)
    if not re.findall(r"^https?://", url.lower()):
        Redis().update_metrics("bad_request")
        message.reply_text("Send me a DIRECT LINK.", quote=True)
        return

    bot_msg = message.reply_text("Request received.", quote=True)
    Redis().update_metrics("direct_request")
    direct_download_entrance(bot_msg, client, url)


@app.on_message(filters.command(["settings"]))
def settings_handler(client: "Client", message: "types.Message"):
    chat_id = message.chat.id
    client.send_chat_action(chat_id, "typing")
    data = get_user_settings(str(chat_id))
    set_mode = (data[-1])
    text = {"Local": "Celery", "Celery": "Local"}.get(set_mode, "Local")
    mode_text = f"Download mode: **{set_mode}**"
    if message.chat.username == OWNER:
        extra = [InlineKeyboardButton(f"Change download mode to {text}", callback_data=text)]
    else:
        extra = []

    markup = InlineKeyboardMarkup(
        [
            [  # First row
                InlineKeyboardButton("send as document", callback_data="document"),
                InlineKeyboardButton("send as video", callback_data="video"),
                InlineKeyboardButton("send as audio", callback_data="audio")
            ],
            [  # second row
                InlineKeyboardButton("High Quality", callback_data="high"),
                InlineKeyboardButton("Medium Quality", callback_data="medium"),
                InlineKeyboardButton("Low Quality", callback_data="low"),
            ],
            extra
        ]
    )

    client.send_message(chat_id, bot_text.settings.format(data[1], data[2]) + mode_text, reply_markup=markup)


@app.on_message(filters.command(["vip"]))
def vip_handler(client: "Client", message: "types.Message"):
    # process as chat.id, not from_user.id
    chat_id = message.chat.id
    text = message.text.strip()
    client.send_chat_action(chat_id, "typing")
    if text == "/vip":
        client.send_message(chat_id, bot_text.vip, disable_web_page_preview=True)
    else:
        bm: typing.Union["types.Message", "typing.Any"] = message.reply_text(bot_text.vip_pay, quote=True)
        unique = text.replace("/vip", "").strip()
        msg = verify_payment(chat_id, unique, client)
        bm.edit_text(msg)


def generate_invoice(amount: "int", title: "str", description: "str", payload: "str"):
    invoice = raw_types.input_media_invoice.InputMediaInvoice(
        invoice=(
            raw_types.invoice.Invoice(currency="USD", prices=[raw_types.LabeledPrice(label="price", amount=amount)])),
        title=title,
        description=description,
        provider=PROVIDER_TOKEN,
        provider_data=raw_types.DataJSON(data="{}"),
        payload=payload.encode(),
        start_param=payload
    )
    return invoice


# payment related
@app.on_message(filters.command(["topup"]))
def topup_handler(client: "Client", message: "types.Message"):
    chat_id = message.chat.id
    client.send_chat_action(chat_id, "typing")
    invoice = generate_invoice(100, bot_text.topup_title, bot_text.topup_description,
                               f"{message.chat.id}-topup")

    app.send(
        functions.messages.SendMedia(
            peer=(raw_types.InputPeerUser(user_id=chat_id, access_hash=0)),
            media=invoice,
            random_id=app.rnd_id(),
            message="Please use your card to pay for more traffic"
        )
    )


@app.on_message(filters.command(["tgvip"]))
def tgvip_handler(client: "Client", message: "types.Message"):
    chat_id = message.chat.id
    client.send_chat_action(chat_id, "typing")
    invoice = generate_invoice(1000, "VIP1", f"pay USD${MULTIPLY} for VIP1", f"{message.chat.id}-vip1".encode())

    app.send(
        functions.messages.SendMedia(
            peer=(raw_types.InputPeerUser(user_id=chat_id, access_hash=0)),
            media=invoice,
            random_id=app.rnd_id(),
            message="Please use your card to pay for more traffic"
        )
    )


@app.on_message(filters.command(["addvip"]))
def add_vip_handler(client: "Client", message: "types.Message"):
    # process as chat.id, not from_user.id
    chat_id = message.chat.id
    text = message.text.strip()
    if message.chat.username == OWNER:
        client.send_chat_action(chat_id, "typing")
        if text == "/addvip":
            client.send_message(chat_id, bot_text.vip, disable_web_page_preview=True)
        else:
            bm: typing.Union["types.Message", "typing.Any"] = message.reply_text(bot_text.vip_pay, quote=True)
            user_id = text.replace("/addvip", "").strip()
            msg = admin_add_vip(user_id)
            bm.edit_text(msg)
    else:
        client.send_message(chat_id, "You are not Admin")


@app.on_message(filters.incoming & filters.text)
@private_use
def download_handler(client: "Client", message: "types.Message"):
    # check remaining quota
    red = Redis()
    chat_id = message.from_user.id
    client.send_chat_action(chat_id, 'typing')
    red.user_count(chat_id)
    # url = re.sub(r'/ytdl\s*', '', message.text)
    if not re.findall(r"(?P<linkrm>https?://[^\s]+)", message.text):
        red.update_metrics("bad_request")
        message.reply_text("I think you should send me a link.", quote=True)
        return
    url = re.search(r"(?P<linkrm>https?://[^\s]+)", message.text).group("linkrm")
    # url = VIP.extract_canonical_link(rawurl)
    if "item.taobao.com" in url:
        vid = parse_qs(urlparse(url).query).get('id')
        url = "https://world.taobao.com/item/" + str(vid[0]) + ".htm"
    if "offerId" in url:
        vid = parse_qs(urlparse(url).query).get('offerId')
        url = "https://m.1688.com/offer/" + str(vid[0]) + ".html"
    if "intl.taobao.com" in url:
        vid = parse_qs(urlparse(url).query).get('id')
        url = "https://world.taobao.com/item/" + str(vid[0]) + ".htm"
    if "tmall.com" in url:
        vid = parse_qs(urlparse(url).query).get('id')
        url = "https://world.taobao.com/item/" + str(vid[0]) + ".htm"
    if "1688.com/offer/" in url:
        vid = os.path.basename(urlparse(url).path)
        url = "https://m.1688.com/offer/" + vid
        logging.info("link sau khi convert")
        logging.info(url)
    if "qr.1688.com" in url:
        oklink = qr1688(url)
        logging.info("link 1688 sau khi convert")
        logging.info(url)
        url = unquote(unquote(oklink))
    if "tb.cn" in url:
        linktb = tbcn(url)
        vid = parse_qs(urlparse(linktb).query).get('id')
        if "a.m.taobao.com" in linktb:
            disassembled = urlparse(linktb)
            videoid, file_ext = splitext(basename(disassembled.path))
            videoid = re.sub(r"\D", "", videoid)
            url = "https://world.taobao.com/item/" + videoid + ".htm"
        elif "video-fullpage" in linktb:
            plink = urlparse(linktb)
            videolink = parse_qs(plink.query)['videoUrl'][0]
            url = videolink
            logging.info("here")
            logging.info(videolink)
        else:
            videoid = str(vid[0])
            url = "https://world.taobao.com/item/" + videoid + ".htm"
        logging.info("tb.cn convert xong")
        logging.info(linktb)
        logging.info(url)
    logging.info("start get %s", url)
    # if not PLAYLIST_SUPPORT:
    #     if re.findall(r"^https://www\.youtube\.com/channel/", VIP.extract_canonical_link(url)) or "list" in url:
    #         message.reply_text("Channel/list download is disabled now. Please send me individual video link.", quote=True)
    #         red.update_metrics("reject_channel")
    #         return
    # non vip user, consume too many token
    if (not VIP().check_vip(chat_id)) and (not lim.consume(str(chat_id).encode(), 1)):
        red.update_metrics("rate_limit")
        message.reply_text(bot_text.too_fast, quote=True)
        return

    red.update_metrics("video_request")
    text = bot_text.get_receive_link_text()
    try:
        # raise pyrogram.errors.exceptions.FloodWait(10)
        bot_msg: typing.Union["types.Message", "typing.Any"] = message.reply_text(text, quote=True)
    except pyrogram.errors.Flood as e:
        f = BytesIO()
        f.write(str(e).encode())
        f.write(b"Your job will be done soon. Just wait! Don't rush.")
        f.name = "Please don't flood me.txt"
        bot_msg = message.reply_document(f, caption=f"Flood wait! Please wait {e.x} seconds...."
                                                    f"Your job will start automatically", quote=True)
        f.close()
        client.send_message(OWNER, f"Flood wait! 🙁 {e.x} seconds....")
        time.sleep(e.x)

    client.send_chat_action(chat_id, 'upload_video')
    bot_msg.chat = message.chat
    ytdl_download_entrance(bot_msg, client, url)


@app.on_callback_query(filters.regex(r"document|video|audio"))
def send_method_callback(client: "Client", callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    data = callback_query.data
    logging.info("Setting %s file type to %s", chat_id, data)
    set_user_settings(chat_id, "method", data)
    callback_query.answer(f"Your send type was set to {callback_query.data}")


@app.on_callback_query(filters.regex(r"high|medium|low"))
def download_resolution_callback(client: "Client", callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    data = callback_query.data
    logging.info("Setting %s file type to %s", chat_id, data)
    set_user_settings(chat_id, "resolution", data)
    callback_query.answer(f"Your default download quality was set to {callback_query.data}")


@app.on_callback_query(filters.regex(r"convert"))
def audio_callback(client: "Client", callback_query: types.CallbackQuery):
    vmsg = callback_query.message
    url: "str" = re.findall(r"https?://.*", vmsg.caption)[0]
    if not ENABLE_FFMPEG:
        callback_query.answer("Audio conversion is disabled now.")
        callback_query.message.reply_text("Audio conversion is disabled now.")
        return
    for link in URL_ARRAY:
        if link in url:
            callback_query.answer("Không hỗ trợ convert audio từ Shop")
            callback_query.message.reply_text("Không hỗ trợ convert audio từ Shop")
            return
    callback_query.answer("Converting to audio...please wait patiently")
    Redis().update_metrics("audio_request")
    audio_entrance(vmsg, client, url)


@app.on_callback_query(filters.regex(r"getimg"))
def getimg_callback(client: "Client", callback_query: types.CallbackQuery):
    vmsg = callback_query.message
    url: "str" = re.findall(r"https?://.*", vmsg.caption)[0]
    for link in URL_ARRAY:
        if link in url:
            callback_query.answer("Đang lấy ảnh...")
            Redis().update_metrics("images_request")
            image_entrance(vmsg, client, url)
        else:
            callback_query.answer("Chỉ hỗ trợ lấy lại ảnh từ Shop")
            callback_query.message.reply_text("Chỉ hỗ trợ lấy lại ảnh từ Shop")
            return


@app.on_callback_query(filters.regex(r"Local|Celery"))
def owner_local_callback(client: "Client", callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    set_user_settings(chat_id, "mode", callback_query.data)
    callback_query.answer(f"Download mode was changed to {callback_query.data}")


def periodic_sub_check():
    vip = VIP()
    exceptions = pyrogram.errors.exceptions
    for cid, uids in vip.group_subscriber().items():
        video_url = vip.has_newer_update(cid)
        if video_url:
            logging.info(f"periodic update:{video_url} - {uids}")
            for uid in uids:
                try:
                    bot_msg = app.send_message(uid, f"{video_url} is downloading...", disable_web_page_preview=True)
                    ytdl_download_entrance(bot_msg, app, video_url)
                except (exceptions.bad_request_400.PeerIdInvalid, exceptions.bad_request_400.UserIsBlocked) as e:
                    logging.warning("User is blocked or deleted. %s", e)
                    vip.deactivate_user_subscription(uid)
                except Exception as e:
                    logging.error("Unknown error when sending message to user. %s", traceback.format_exc())
                    logging.error(e)
                finally:
                    time.sleep(random.random() * 3)


@app.on_raw_update()
def raw_update(client: "Client", update, users, chats):
    action = getattr(getattr(update, "message", None), "action", None)
    if update.QUALNAME == 'types.UpdateBotPrecheckoutQuery':
        client.send(
            functions.messages.SetBotPrecheckoutResults(
                query_id=update.query_id,
                success=True,
            )
        )
    elif action and action.QUALNAME == 'types.MessageActionPaymentSentMe':
        logging.info("Payment received. %s", action)
        uid = update.message.peer_id.user_id
        vip = VIP()
        amount = f"{action.total_amount / 100} {action.currency}"
        if "vip" in action.payload.decode():
            ud = {
                "user_id": uid,
                "username": users.get(uid).username,
                "payment_amount": 10,
                "payment_id": 0,
                "level": 1,
                "quota": QUOTA * 2
            }
            vip.direct_add_vip(ud)
            client.send_message(uid, f"Thank you {uid}. VIP payment received: {amount}")

        else:
            vip.set_topup(uid)
            client.send_message(uid, f"Thank you {uid}. Top up payment received: {amount}")


if __name__ == '__main__':
    MySQL()
    scheduler = BackgroundScheduler(timezone="Europe/Stockholm", job_defaults={'max_instances': 5})
    scheduler.add_job(Redis().reset_today, 'cron', hour=0, minute=0)
    scheduler.add_job(auto_restart, 'interval', seconds=60)
    scheduler.add_job(clean_tempfile, 'interval', seconds=60)
    scheduler.add_job(InfluxDB().collect_data, 'interval', seconds=60)
    scheduler.add_job(periodic_sub_check, 'interval', seconds=60 * 60)
    logging.info(URL_ARRAY)
    scheduler.start()
    banner = f"""
▌ ▌         ▀▛▘     ▌       ▛▀▖              ▜            ▌
▝▞  ▞▀▖ ▌ ▌  ▌  ▌ ▌ ▛▀▖ ▞▀▖ ▌ ▌ ▞▀▖ ▌  ▌ ▛▀▖ ▐  ▞▀▖ ▝▀▖ ▞▀▌
 ▌  ▌ ▌ ▌ ▌  ▌  ▌ ▌ ▌ ▌ ▛▀  ▌ ▌ ▌ ▌ ▐▐▐  ▌ ▌ ▐  ▌ ▌ ▞▀▌ ▌ ▌
 ▘  ▝▀  ▝▀▘  ▘  ▝▀▘ ▀▀  ▝▀▘ ▀▀  ▝▀   ▘▘  ▘ ▘  ▘ ▝▀  ▝▀▘ ▝▀▘

By @BennyThink, VIP mode: {ENABLE_VIP}, Distribution: {ENABLE_CELERY}
Version: {get_revision()}
    """
    print(banner)
    app.run()
