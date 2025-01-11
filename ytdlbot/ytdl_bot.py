#!/usr/local/bin/python3
# coding: utf-8

# ytdlbot - new.py
# 8/14/21 14:37
#

__author__ = "Benny <benny.think@gmail.com>"

import contextlib
import json
import logging
import os
import threading
import random
import re
import tempfile
import time
import traceback
import requests
import base64

from io import BytesIO
from typing import Any
from urllib.parse import parse_qs, urlparse, unquote
from os.path import splitext, basename

import pyrogram.errors
import qrcode
import yt_dlp
from apscheduler.schedulers.background import BackgroundScheduler
from pyrogram import Client, enums, filters, types
from pyrogram.errors.exceptions.bad_request_400 import UserNotParticipant
from pyrogram.raw import functions
from pyrogram.raw import types as raw_types
from tgbot_ping import get_runtime
from youtubesearchpython import VideosSearch

from channel import Channel
from client_init import create_app
from config import (
    AUTHORIZED_USER,
    ENABLE_CELERY,
    ENABLE_FFMPEG,
    ENABLE_VIP,
    M3U8_SUPPORT,
    OWNER,
    PLAYLIST_SUPPORT,
    PREMIUM_USER,
    PROVIDER_TOKEN,
    REQUIRED_MEMBERSHIP,
    TOKEN_PRICE,
    TRX_SIGNAL,
    URL_ARRAY,
    TOKEN,
    API_TAOBAO,
    BEARER_TOKEN,
    API_QRPAY
)
from constant import BotText
from database import InfluxDB, MySQL, Redis
from limit import Payment, TronTrx
from tasks import app as celery_app
from tasks import (
    audio_entrance,
    direct_download_entrance,
    hot_patch,
    purge_tasks,
    ytdl_download_entrance,
    cn_download_entrance,
    spdl_download_entrance,
    image_entrance,
)
from utils import auto_restart, clean_tempfile, customize_logger, get_revision, tbcn, qr1688
logging.info("Authorized users are %s", AUTHORIZED_USER)
customize_logger(["pyrogram.client", "pyrogram.session.session", "pyrogram.connection.connection"])
logging.getLogger("apscheduler.executors.default").propagate = False

app = create_app("main")
channel = Channel()
CHECK_TRANSACTION_INTERVAL = 10  # Thời gian chờ giữa các lần kiểm tra giao dịch (giây)
TRANSACTION_TIMEOUT = 600  # Thời gian chờ tối đa cho giao dịch (giây)

def private_use(func):
    def wrapper(client: Client, message: types.Message):
        chat_id = getattr(message.from_user, "id", None)

        # message type check
        if message.chat.type != enums.ChatType.PRIVATE and not getattr(message, "text", "").lower().startswith("/ytdl"):
            logging.debug("%s, it's annoying me...🙄️ ", message.text)
            return

        # authorized users check
        if AUTHORIZED_USER:
            users = [int(i) for i in AUTHORIZED_USER.split(",")]
        else:
            users = []

        if users and chat_id and chat_id not in users:
            message.reply_text(BotText.private, quote=True)
            return

        if REQUIRED_MEMBERSHIP:
            try:
                member: types.ChatMember | Any = app.get_chat_member(REQUIRED_MEMBERSHIP, chat_id)
                if member.status not in [
                    enums.ChatMemberStatus.ADMINISTRATOR,
                    enums.ChatMemberStatus.MEMBER,
                    enums.ChatMemberStatus.OWNER,
                ]:
                    raise UserNotParticipant()
                else:
                    logging.info("user %s check passed for group/channel %s.", chat_id, REQUIRED_MEMBERSHIP)
            except UserNotParticipant:
                logging.warning("user %s is not a member of group/channel %s", chat_id, REQUIRED_MEMBERSHIP)
                message.reply_text(BotText.membership_require, quote=True)
                return

        return func(client, message)

    return wrapper


@app.on_message(filters.command(["start"]))
def start_handler(client: Client, message: types.Message):
    payment = Payment()
    from_id = message.from_user.id
    logging.info("%s welcome to youtube-dl bot!", message.from_user.id)
    client.send_chat_action(from_id, enums.ChatAction.TYPING)
    is_old_user = payment.check_old_user(from_id)
    if is_old_user:
        info = ""
    if ENABLE_VIP:
        free_token, pay_token, reset = payment.get_token(from_id)
        info = f"Lượt tải miễn phí: {free_token}\nLượt tải trả phí: {pay_token}\nReset: {reset}"
    else:
        info = ""
    text = f"{BotText.start}\n\n{info}\n{BotText.custom_text}"
    client.send_message(message.chat.id, text, disable_web_page_preview=True)


@app.on_message(filters.command(["help"]))
def help_handler(client: Client, message: types.Message):
    chat_id = message.chat.id
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    client.send_message(chat_id, BotText.help, disable_web_page_preview=True)


@app.on_message(filters.command(["about"]))
def about_handler(client: Client, message: types.Message):
    chat_id = message.chat.id
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    client.send_message(chat_id, BotText.about)


@app.on_message(filters.command(["sub"]))
def subscribe_handler(client: Client, message: types.Message):
    chat_id = message.chat.id
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    if message.text == "/sub":
        result = channel.get_user_subscription(chat_id)
    else:
        link = message.text.split()[1]
        try:
            result = channel.subscribe_channel(chat_id, link)
        except (IndexError, ValueError):
            result = f"Error: \n{traceback.format_exc()}"
    client.send_message(chat_id, result or "You have no subscription.", disable_web_page_preview=True)


@app.on_message(filters.command(["unsub"]))
def unsubscribe_handler(client: Client, message: types.Message):
    chat_id = message.chat.id
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    text = message.text.split(" ")
    if len(text) == 1:
        client.send_message(chat_id, "/unsub channel_id", disable_web_page_preview=True)
        return

    rows = channel.unsubscribe_channel(chat_id, text[1])
    if rows:
        text = f"Unsubscribed from {text[1]}"
    else:
        text = "Unable to find the channel."
    client.send_message(chat_id, text, disable_web_page_preview=True)


@app.on_message(filters.command(["patch"]))
def patch_handler(client: Client, message: types.Message):
    username = message.from_user.username
    chat_id = message.chat.id
    if username == OWNER:
        celery_app.control.broadcast("hot_patch")
        client.send_chat_action(chat_id, enums.ChatAction.TYPING)
        client.send_message(chat_id, "Oorah!")
        hot_patch()


@app.on_message(filters.command(["uncache"]))
def uncache_handler(client: Client, message: types.Message):
    username = message.from_user.username
    link = message.text.split()[1]
    if username == OWNER:
        count = channel.del_cache(link)
        message.reply_text(f"{count} cache(s) deleted.", quote=True)


@app.on_message(filters.command(["purge"]))
def purge_handler(client: Client, message: types.Message):
    username = message.from_user.username
    if username == OWNER:
        message.reply_text(purge_tasks(), quote=True)


@app.on_message(filters.command(["ping"]))
def ping_handler(client: Client, message: types.Message):
    redis = Redis()
    chat_id = message.chat.id
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    message_sent = False
    def send_message_and_measure_ping():
        start_time = int(round(time.time() * 1000))
        reply = client.send_message(chat_id, "Starting Ping...")
        end_time = int(round(time.time() * 1000))
        ping_time = int(round(end_time - start_time))
        message_sent = True
        if message_sent:
            message.reply_text(f"Ping: {ping_time:.2f} ms", quote=True)
        time.sleep(0.5)
        client.edit_message_text(chat_id=reply.chat.id, message_id=reply.id, text="Ping Calculation Complete.")
        time.sleep(1)
        client.delete_messages(chat_id=reply.chat.id, message_ids=reply.id)
            
    thread = threading.Thread(target=send_message_and_measure_ping)
    thread.start()


@app.on_message(filters.command(["stats"]))
def stats_handler(client: Client, message: types.Message):
    redis = Redis()
    chat_id = message.chat.id
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    if os.uname().sysname == "Darwin" or ".heroku" in os.getenv("PYTHONHOME", ""):
        bot_info = "Stats Unavailable."
    else:
        bot_info = get_runtime("ytdlbot_ytdl_1", "YouTube-dl")
    if message.chat.username == OWNER:
        stats = BotText.ping_worker()[:1000]
        client.send_document(chat_id, redis.generate_file(), caption=f"{bot_info}\n\n{stats}")
    else:
        client.send_message(chat_id, f"{bot_info.split('CPU')[0]}")


@app.on_message(filters.command(["sub_count"]))
def sub_count_handler(client: Client, message: types.Message):
    username = message.from_user.username
    chat_id = message.chat.id
    if username == OWNER:
        with BytesIO() as f:
            f.write(channel.sub_count().encode("u8"))
            f.name = "subscription count.txt"
            client.send_document(chat_id, f)


@app.on_message(filters.command(["show_history"]))
def show_history(client: Client, message: types.Message):
    chat_id = message.chat.id
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    data = MySQL().show_history(chat_id)
    if data:
        client.send_message(chat_id, data, disable_web_page_preview=True)
    else:
        client.send_message(chat_id, "No history found.")


@app.on_message(filters.command(["clear_history"]))
def clear_history(client: Client, message: types.Message):
    chat_id = message.chat.id
    MySQL().clear_history(chat_id)
    message.reply_text("History cleared.", quote=True)


@app.on_message(filters.command(["direct"]))
def direct_handler(client: Client, message: types.Message):
    redis = Redis()
    chat_id = message.from_user.id
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    url = re.sub(r"/direct\s*", "", message.text)
    logging.info("direct start %s", url)
    if not re.findall(r"^https?://", url.lower()):
        redis.update_metrics("bad_request")
        message.reply_text("Send me a DIRECT LINK.", quote=True)
        return

    bot_msg = message.reply_text("Request received.", quote=True)
    redis.update_metrics("direct_request")
    direct_download_entrance(client, bot_msg, url)

@app.on_message(filters.command(["spdl"]))
def spdl_handler(client: Client, message: types.Message):
    redis = Redis()
    chat_id = message.from_user.id
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    url = re.sub(r"/spdl\s*", "", message.text)
    logging.info("spdl start %s", url)
    if not re.findall(r"^https?://", url.lower()):
        redis.update_metrics("bad_request")
        message.reply_text("Something wrong 🤔.\nCheck your URL and send me again.", quote=True)
        return

    bot_msg = message.reply_text("Request received.", quote=True)
    redis.update_metrics("direct_request")
    spdl_download_entrance(client, bot_msg, url)


@app.on_message(filters.command(["settings"]))
def settings_handler(client: Client, message: types.Message):
    chat_id = message.chat.id
    payment = Payment()
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    data = MySQL().get_user_settings(chat_id)
    set_mode = data[3]
    text = {"Local": "Celery", "Celery": "Local"}.get(set_mode, "Local")
    mode_text = f"Download mode: **{set_mode}**\nHistory record: {data[4]}"
    if message.chat.username == OWNER or payment.get_pay_token(chat_id):
        extra = [types.InlineKeyboardButton(f"Change download mode to {text}", callback_data=text)]
    else:
        extra = []

    markup = types.InlineKeyboardMarkup(
        [
            [  # First row
                types.InlineKeyboardButton("send as document", callback_data="document"),
                types.InlineKeyboardButton("send as video", callback_data="video"),
                types.InlineKeyboardButton("send as audio", callback_data="audio"),
            ],
            [  # second row
                types.InlineKeyboardButton("High Quality", callback_data="high"),
                types.InlineKeyboardButton("Medium Quality", callback_data="medium"),
                types.InlineKeyboardButton("Low Quality", callback_data="low"),
            ],
            [
                types.InlineKeyboardButton("Toggle History", callback_data=f"history-{data[4]}"),
            ],
            extra,
        ]
    )

    try:
        client.send_message(chat_id, BotText.settings.format(data[1], data[2]) + mode_text, reply_markup=markup)
    except:
        client.send_message(
            chat_id, BotText.settings.format(data[1] + ".", data[2] + ".") + mode_text, reply_markup=markup
        )


@app.on_message(filters.command(["buy"]))
def buy_handler(client: Client, message: types.Message):
    # process as chat.id, not from_user.id
    chat_id = message.chat.id
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    # currency USD
    token_count = message.text.replace("/buy", "").strip()
    if token_count.isdigit():
        price = int(int(token_count) / TOKEN_PRICE * 100)
    else:
        price = 100

    markup = types.InlineKeyboardMarkup(
        [
            [
                types.InlineKeyboardButton("Quét mã QR", callback_data=f"bot-payments-{price}"),
                types.InlineKeyboardButton("TRON(TRX)", callback_data="tron-trx"),
            ],
        ]
    )
    client.send_message(chat_id, BotText.buy, disable_web_page_preview=True, reply_markup=markup)


@app.on_callback_query(filters.regex(r"tron-trx"))
def tronpayment_btn_calback(client: Client, callback_query: types.CallbackQuery):
    callback_query.answer("Generating QR code...")
    chat_id = callback_query.message.chat.id
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)

    addr = TronTrx().get_payment_address(chat_id)
    with BytesIO() as bio:
        qr = qrcode.make(addr)
        qr.save(bio)
        client.send_photo(chat_id, bio, caption=f"Send any amount of TRX to `{addr}`")


@app.on_callback_query(filters.regex(r"premium.*"))
def premium_click(client: Client, callback_query: types.CallbackQuery):
    data = callback_query.data
    if data == "premium-yes":
        callback_query.answer("Seeking premium user...")
        callback_query.message.edit_text("Please wait patiently...no progress bar will be shown.")
        replied = callback_query.message.reply_to_message
        data = {"url": replied.text, "user_id": callback_query.message.chat.id}
        client.send_message(PREMIUM_USER, json.dumps(data), disable_notification=True, disable_web_page_preview=True)
    else:
        callback_query.answer("Cancelled.")
        original_text = callback_query.message.text
        callback_query.message.edit_text(original_text.split("\n")[0])


@app.on_callback_query(filters.regex(r"bot-payments-.*"))
def bot_payment_btn_calback(client: Client, callback_query: types.CallbackQuery):
    callback_query.answer("Vui lòng quét mã QR...")
    chat_id = callback_query.message.chat.id
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)

    data = callback_query.data
    price = int(data.split("-")[-1])
    data = callback_query.data
    price = int(data.split("-")[-1])

    # Tạo transaction_id duy nhất (có thể sử dụng UUID)
    transaction_id = f"BOT-{chat_id}-{int(time.time())}"
    # Gọi hàm tạo QR code và theo dõi thanh toán
    generate_qr_code_and_track_payment(client, chat_id, price, transaction_id)


@app.on_message(filters.command(["redeem"]))
def redeem_handler(client: Client, message: types.Message):
    payment = Payment()
    chat_id = message.chat.id
    text = message.text.strip()
    unique = text.replace("/redeem", "").strip()
    msg = payment.verify_payment(chat_id, unique)
    message.reply_text(msg, quote=True)


@app.on_message(filters.command(["add"]))
def add_vip_handler(client: Client, message: types.Message):
    username = message.from_user.username
    if username == OWNER:
       payment = Payment()
       chat_id = message.chat.id
       text = message.text.strip()

       # Parse the input values
       try:
           parts = text.split()
           if len(parts) != 3:
               raise ValueError("Invalid input format. Please use /add chat_id amount.")
        
           command, chat_id_str, amount_str = parts
           chat_id = int(chat_id_str)
           amount = int(amount_str)

           # Verify the payment
           msg = payment.admin_add_token(chat_id, amount)
           message.reply_text(msg, quote=True)
       except ValueError as e:
           message.reply_text(str(e), quote=True)


@app.on_message(filters.user(PREMIUM_USER) & filters.incoming & filters.caption)
def premium_forward(client: Client, message: types.Message):
    media = message.video or message.audio or message.document
    target_user = media.file_name.split(".")[0]
    client.forward_messages(target_user, message.chat.id, message.id)


@app.on_message(filters.command(["ban"]) & filters.user(PREMIUM_USER))
def ban_handler(client: Client, message: types.Message):
    replied = message.reply_to_message.text
    user_id = json.loads(replied).get("user_id")
    redis = Redis()
    redis.r.hset("ban", user_id, 1)
    message.reply_text(f"Done, banned {user_id}.", quote=True)


def generate_invoice(amount: int, title: str, description: str, payload: str):
    invoice = raw_types.input_media_invoice.InputMediaInvoice(
        invoice=raw_types.invoice.Invoice(
            currency="USD", prices=[raw_types.LabeledPrice(label="price", amount=amount)]
        ),
        title=title,
        description=description,
        provider=PROVIDER_TOKEN,
        provider_data=raw_types.DataJSON(data="{}"),
        payload=payload.encode(),
        start_param=payload,
    )
    return invoice


def link_checker(url: str) -> str:
    if url.startswith("https://www.instagram.com"):
        return ""

    if url.startswith("https://fb.watch"):
        return "fb.watch link is blocked. Please copy this link to Browser and wait for new link. Copy new link (start by m.facebook.com, facebook.com... and send new link to bot"

    if not PLAYLIST_SUPPORT and (
        re.findall(r"^https://www\.youtube\.com/channel/", Channel.extract_canonical_link(url)) or "list" in url
    ):
        return "Playlist or channel links are disabled."

    if not M3U8_SUPPORT and (re.findall(r"m3u8|\.m3u8|\.m3u$", url.lower())):
        return "m3u8 links are disabled."



def search_ytb(kw: str):
    videos_search = VideosSearch(kw, limit=10)
    text = ""
    results = videos_search.result()["result"]
    for item in results:
        title = item.get("title")
        link = item.get("link")
        index = results.index(item) + 1
        text += f"{index}. {title}\n{link}\n\n"
    return text


@app.on_message(filters.incoming & filters.document)
@private_use
def upload_handler(client: Client, message: types.Message):
    logging.info(message.from_user)
    username = message.from_user.username
    chat_id = message.from_user.id

    if ENABLE_VIP:
        redis = Redis()
        payment = Payment()
        free, pay, reset = payment.get_token(chat_id)
        logging.info(f"Payment info: Pay={pay}, Free={free}")

        # Loại bỏ kiểm tra username != OWNER
        # Bất kỳ ai cũng có thể sử dụng nếu ENABLE_VIP là True, kể cả không phải là VIP
        logging.info(f"User {username} with id {chat_id} Upload file")
        client.send_chat_action(chat_id, enums.ChatAction.TYPING)
        redis.user_count(chat_id)

        # Process document
        file = message.document
        try:
            get_file_url = f"https://api.telegram.org/bot{TOKEN}/getFile?file_id={file.file_id}"
            response = requests.get(get_file_url)
            if response.status_code == 200:
                file_info = response.json()
                logging.info(f"Result from simulating getFile: {file_info}")

                if "result" in file_info and "file_path" in file_info["result"] and file_info["result"]["file_path"].endswith(".txt") and file.file_size < 100 * 1024:
                    file_path = file_info["result"]["file_path"]
                    cdn_link = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
                    payload = {'url': cdn_link, 'userId': chat_id}
                    headers = {
                        'Content-Type': 'application/json',
                        'Authorization': f'Bearer {BEARER_TOKEN}',
                    }
                    try:
                        response = requests.post(f"https://{API_TAOBAO}/update_cookies", headers=headers, data=json.dumps(payload))
                        logging.info(f"Response from first API: {response}")
                        logging.info(f"Response content: {response.content.decode('utf-8')}")
                        logging.info(cdn_link)
                        redis.update_metrics("upload_cookies_request")
                        message.reply_text(f"Cập nhật cookies thành công", quote=True)
                        if response.status_code != 200:
                            logging.error(f"Lỗi cập nhật cookies, status code: {response.status_code}")
                            raise Exception("Lỗi kết nối API. Vui lòng thử lại 1 lần nữa hoặc thông báo cho @cpanel10x")
                    except Exception as e:
                        logging.error(f"Lỗi cập nhật cookies: {e}")
                        raise
                else:
                    message.reply_text("Có lỗi xảy ra hoặc file bạn gửi không phải là file cookies hợp lệ .txt.", quote=True)
            else:
                message.reply_text(f"Error simulating getFile: {response.status_code}", quote=True)
            return
        except Exception as e:
            message.reply_text(f"Lỗi cập nhật Cookies: {e}", quote=True)
    else:
        message.reply_text(f"Tính năng đang tắt", quote=True)


@app.on_message(filters.incoming & (filters.text))
@private_use
def download_handler(client: Client, message: types.Message):
    redis = Redis()
    payment = Payment()
    chat_id = message.from_user.id
    client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    redis.user_count(chat_id)
    if message.document:
        with tempfile.NamedTemporaryFile(mode="r+") as tf:
            logging.info("Downloading file to %s", tf.name)
            message.download(tf.name)
            contents = open(tf.name, "r").read()  # don't know why
        msgLink = contents.split()
    else:
        msgLink = [re.sub(r"/ytdl\s*", "", message.text)]
        logging.info("start %s", msgLink)
        
    for msg in msgLink:
        match = re.search(r"(?P<linkrm>https?://[^\s]+)", msg)
        if match:
            urls = match.group("linkrm")
        else:
            redis.update_metrics("bad_request")
            text = search_ytb(msg)
            message.reply_text(text, quote=True, disable_web_page_preview=True)
            return
        url = urls
        logging.info("phan tich link")
        logging.info(urls)
        if text := link_checker(url):
            message.reply_text(text, quote=True)
            redis.update_metrics("reject_link_checker")
            return
            
        # url = VIP.extract_canonical_link(rawurl)
        if "offerId" in urls:
            vid = parse_qs(urlparse(urls).query).get('offerId')
            url = "https://m.1688.com/offer/" + str(vid[0]) + ".html"
        if "intl.taobao.com" in urls:
            vid = parse_qs(urlparse(urls).query).get('id')
            url = "https://item.taobao.com/item.htm?id=" + str(vid[0])
        if "tmall.com" in urls:
            vid = parse_qs(urlparse(urls).query).get('id')
            url = "https://item.taobao.com/item.htm?id=" + str(vid[0])
        if "1688.com/offer/" in urls:
            vid = os.path.basename(urlparse(urls).path)
            url = "https://m.1688.com/offer/" + vid
            logging.info("link sau khi convert")
            logging.info(url)
        if "qr.1688.com" in urls:
            oklink = qr1688(urls)
            logging.info("link 1688 sau khi convert")
            logging.info(urls)
            url = unquote(unquote(oklink))
        if "tb.cn" in urls:
            linktb = tbcn(urls)
            vid = parse_qs(urlparse(linktb).query).get('id')
            if "a.m.taobao.com" in linktb:
                disassembled = urlparse(linktb)
                videoid, file_ext = splitext(basename(disassembled.path))
                videoid = re.sub(r"\D", "", videoid)
                url = "https://item.taobao.com/item.htm?id=" + videoid
            elif "video-fullpage" in linktb:
                plink = urlparse(linktb)
                videolink = parse_qs(plink.query)['videoUrl'][0]
                url = videolink
                logging.info("here")
                logging.info(videolink)
            else:
                videoid = str(vid[0])
                url = "https://item.taobao.com/item.htm?id=" + videoid
            logging.info("tb.cn convert xong")
            logging.info(linktb)
            logging.info(url)
        # old user is not limited by token
        if ENABLE_VIP and not payment.check_old_user(chat_id):
            free, pay, reset = payment.get_token(chat_id)
            logging.info(pay)
            logging.info(free)
            if int(free) + int(pay) <= 0:
                message.reply_text(f"Đã hết lượt tải miễn phí trong ngày. Vui lòng chờ đến {reset} hoặc mua thêm /buy .", quote=True)
                redis.update_metrics("reject_token")
                return
            else:
                payment.use_token(chat_id)

        redis.update_metrics("video_request")

        text = BotText.get_receive_link_text()
        try:
            # raise pyrogram.errors.exceptions.FloodWait(10)
            bot_msg: types.Message | Any = message.reply_text(text, quote=True)
        except pyrogram.errors.Flood as e:
            f = BytesIO()
            f.write(str(e).encode())
            f.write(b"Your job will be done soon. Just wait! Don't rush.")
            f.name = "Please don't flood me.txt"
            bot_msg = message.reply_document(
                f, caption=f"Flood wait! Please wait {e} seconds...." f"Your job will start automatically", quote=True
            )
            f.close()
            client.send_message(OWNER, f"Flood wait! 🙁 {e} seconds....")
            time.sleep(e.value)

        client.send_chat_action(chat_id, enums.ChatAction.UPLOAD_VIDEO)
        bot_msg.chat = message.chat
        logging.info(url)
        if url.startswith("https://item.taobao.com") or url.startswith("https://mobile.yangkeduo.com"):
            cn_download_entrance(client, bot_msg, url)
        else:
            ytdl_download_entrance(client, bot_msg, url)


@app.on_callback_query(filters.regex(r"document|video|audio"))
def send_method_callback(client: Client, callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    data = callback_query.data
    logging.info("Setting %s file type to %s", chat_id, data)
    MySQL().set_user_settings(chat_id, "method", data)
    callback_query.answer(f"Your send type was set to {callback_query.data}")


@app.on_callback_query(filters.regex(r"high|medium|low"))
def download_resolution_callback(client: Client, callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    data = callback_query.data
    logging.info("Setting %s file type to %s", chat_id, data)
    MySQL().set_user_settings(chat_id, "resolution", data)
    callback_query.answer(f"Your default download quality was set to {callback_query.data}")


@app.on_callback_query(filters.regex(r"history.*"))
def set_history_callback(client: Client, callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    data = callback_query.data.split("-")[-1]

    r = "OFF" if data == "ON" else "ON"
    logging.info("Setting %s file type to %s", chat_id, data)
    MySQL().set_user_settings(chat_id, "history", r)
    callback_query.answer("History setting updated.")


@app.on_inline_query()
def inline_query(client: Client, inline_query: types.InlineQuery):
    kw = inline_query.query
    user_id = inline_query.from_user.id
    data = MySQL().search_history(user_id, kw)
    if data:
        results = [
            types.InlineQueryResultArticle(
                id=str(i),
                title=item[1],
                description=item[2],
                input_message_content=types.InputTextMessageContent(item[1]),
            )
            for i, item in enumerate(data)
        ]
        client.answer_inline_query(inline_query.id, results)


@app.on_callback_query(filters.regex(r"convert"))
def audio_callback(client: Client, callback_query: types.CallbackQuery):
    vmsg = callback_query.message
    url: "str" = re.findall(r"https?://.*", vmsg.caption)[0]
    redis = Redis()
    if not ENABLE_FFMPEG:
        callback_query.answer("Request rejected.")
        callback_query.message.reply_text("Audio conversion is disabled now.")
        return
    for link in URL_ARRAY:
        if link in url:
            callback_query.answer("Request rejected")
            callback_query.message.reply_text("Không hỗ trợ convert audio từ Shop")
            return    
    callback_query.answer(f"Converting to audio...please wait patiently")
    redis.update_metrics("audio_request")
    audio_entrance(client, callback_query.message)


@app.on_callback_query(filters.regex(r"getimg"))
def getimg_callback(client: Client, callback_query: types.CallbackQuery):
    vmsg = callback_query.message
    url: "str" = re.findall(r"https?://.*", vmsg.caption)[0]
    redis = Redis()
    url_processed = False
    for link in URL_ARRAY:
        if link in url:
            callback_query.answer("Đang lấy ảnh...")
            redis.update_metrics("images_request")
            image_entrance(client, callback_query.message)
            url_processed = True
            break 
    if not url_processed:
        callback_query.answer("Chỉ hỗ trợ lấy lại ảnh từ Shop")
        callback_query.message.reply_text("Chỉ hỗ trợ lấy lại ảnh từ Shop")


@app.on_callback_query(filters.regex(r"Local|Celery"))
def owner_local_callback(client: Client, callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    MySQL().set_user_settings(chat_id, "mode", callback_query.data)
    callback_query.answer(f"Download mode was changed to {callback_query.data}")


def periodic_sub_check():
    exceptions = pyrogram.errors.exceptions
    for cid, uids in channel.group_subscriber().items():
        video_url = channel.has_newer_update(cid)
        if video_url:
            logging.info(f"periodic update:{video_url} - {uids}")
            for uid in uids:
                try:
                    app.send_message(uid, f"{video_url} is out. Watch it on YouTube")
                except (exceptions.bad_request_400.PeerIdInvalid, exceptions.bad_request_400.UserIsBlocked) as e:
                    logging.warning("User is blocked or deleted. %s", e)
                    channel.deactivate_user_subscription(uid)
                except Exception:
                    logging.error("Unknown error when sending message to user. %s", traceback.format_exc())
                finally:
                    time.sleep(random.random() * 3)


@app.on_raw_update()
def raw_update(client: Client, update, users, chats):
    payment = Payment()
    action = getattr(getattr(update, "message", None), "action", None)
    if update.QUALNAME == "types.UpdateBotPrecheckoutQuery":
        client.invoke(
            functions.messages.SetBotPrecheckoutResults(
                query_id=update.query_id,
                success=True,
            )
        )
    elif action and action.QUALNAME == "types.MessageActionPaymentSentMe":
        logging.info("Payment received. %s", action)
        uid = update.message.peer_id.user_id
        amount = action.total_amount / 100
        payment.add_pay_user([uid, amount, action.charge.provider_charge_id, 0, amount * TOKEN_PRICE])
        client.send_message(uid, f"Thank you {uid}. Payment received: {amount} {action.currency}")

def generate_qr_code_and_track_payment(client: Client, chat_id: int, price: int, transaction_id: str):
    """
    Tạo mã QR, gửi cho người dùng và theo dõi trạng thái thanh toán.

    Args:
        client: Pyrogram Client instance.
        chat_id: ID của chat.
        price: Số tiền cần thanh toán.
        transaction_id: ID giao dịch duy nhất.
    """
    try:
        # 1. Tạo mã QR code
        headers = {'Authorization': f'Bearer {BEARER_TOKEN}'}
        payload = {
            'transaction_id': transaction_id,
            'amount': str(price)
        }
        response = requests.post(f"{API_QRPAY}/create_transaction", json=payload, headers=headers)
        response.raise_for_status()

        # 2. Kiểm tra phản hồi và lấy dữ liệu ảnh
        if response.headers['Content-Type'] == 'image/svg+xml':
            image_bytes = BytesIO(response.content)
        elif response.headers['Content-Type'] == 'application/json':
            try:
                response_json = response.json()
                qr_code_data = response_json.get('qr_code_data')
                codebank = response_json.get('code')
                logging.info(qr_code_data)
                if qr_code_data:
                    image_bytes = BytesIO(base64.b64decode(qr_code_data))
                else:
                    raise ValueError("Không tìm thấy dữ liệu QR code trong phản hồi JSON.")
            except (json.JSONDecodeError, KeyError):
                raise ValueError("Phản hồi API không phải là JSON hợp lệ hoặc thiếu dữ liệu cần thiết.")
        else:
            raise ValueError(f"Loại nội dung phản hồi không được hỗ trợ: {response.headers['Content-Type']}")


        # 3. Gửi mã QR code cho người dùng
        client.send_photo(chat_id, image_bytes, caption=f"Vui lòng quét mã QR để thanh toán {price} VND.\nMã giao dịch: {transaction_id}\nHết hạn sau: {TRANSACTION_TIMEOUT // 60} phút")

        # 4. Theo dõi trạng thái thanh toán
        start_time = time.time()
        while time.time() - start_time < TRANSACTION_TIMEOUT:
            time.sleep(CHECK_TRANSACTION_INTERVAL)

            status, _, _, code, description = get_transaction_status(codebank)

            if status == 'completed':
                client.send_message(chat_id, f"Thanh toán thành công cho giao dịch {transaction_id}!")
                addvip(chat_id, price)
                return
            elif status == 'expired':
                client.send_message(chat_id, f"Giao dịch {transaction_id} đã hết hạn.")
                return
            elif status == 'received_after_expired':
                client.send_message(chat_id, f"Thanh toán thành công cho giao dịch {transaction_id}! Nhưng thời gian chờ đã hết hạn")
                return
            elif status == 'pending':
                continue
            else:
                client.send_message(chat_id, f"Giao dịch {transaction_id} không thành công. Vui lòng thử lại sau. ({description})")
                return

        client.send_message(chat_id, f"Đã hết thời gian chờ thanh toán cho giao dịch {transaction_id}.")

    except requests.exceptions.RequestException as e:
        logging.info(f"Lỗi khi tạo mã QR hoặc kiểm tra giao dịch: {e}")
        client.send_message(chat_id, f"Có lỗi xảy ra khi tạo mã QR hoặc kiểm tra giao dịch: {e}")
    except ValueError as e:
        logging.info(f"Lỗi xử lý phản hồi từ API QRPAY: {e}")
        client.send_message(chat_id, f"Lỗi xử lý phản hồi từ API QRPAY: {e}")
    except Exception as e:
        logging.info(f"Lỗi không xác định: {e}")
        client.send_message(chat_id, "Có lỗi không xác định xảy ra. Vui lòng liên hệ admin.")

def get_transaction_status(transaction_id):
    """
    Lấy trạng thái giao dịch dựa trên transaction_id.

    Args:
        transaction_id: ID giao dịch.

    Returns:
        Tuple: (status, amount, timestamp, code, description)
               - status: Trạng thái giao dịch (pending, completed, expired, received_after_expired, ...).
               - amount: Số tiền.
               - timestamp: Timestamp của giao dịch.
               - code: mã code của giao dịch.
               - description: Mô tả giao dịch.
    """
    try:
        headers = {'Authorization': f'Bearer {BEARER_TOKEN}'}
        response = requests.get(f"{API_QRPAY}/check_transaction_status?code={transaction_id}", headers=headers)
        response.raise_for_status()

        data = response.json()
        status = data.get('status')
        amount = data.get('amount')
        timestamp = data.get('timestamp')
        code = data.get('code')
        description = data.get('description')

        return status, amount, timestamp, code, description

    except requests.exceptions.RequestException as e:
        logging.error(f"Lỗi khi kiểm tra trạng thái giao dịch: {e}")
        return None, None, None, None, None
    except (ValueError, KeyError) as e:
        logging.error(f"Lỗi xử lý phản hồi từ API QRPAY: {e}")
        return None, None, None, None, None


def trx_notify(_, **kwargs):
    user_id = kwargs.get("user_id")
    text = kwargs.get("text")
    logging.info("Sending trx notification to %s", user_id)
    app.send_message(user_id, text)


if __name__ == "__main__":
    MySQL()
    TRX_SIGNAL.connect(trx_notify)
    scheduler = BackgroundScheduler(timezone="Europe/London")
    scheduler.add_job(auto_restart, "interval", seconds=600)
    scheduler.add_job(clean_tempfile, "interval", seconds=120)
    scheduler.add_job(Redis().reset_today, "cron", hour=0, minute=0)
    scheduler.add_job(InfluxDB().collect_data, "interval", seconds=120)
    # scheduler.add_job(TronTrx().check_payment, "interval", seconds=60, max_instances=1)
    #  default quota allocation of 10,000 units per day
    # scheduler.add_job(periodic_sub_check, "interval", seconds=3600)
    scheduler.start()
    banner = f"""
▌ ▌         ▀▛▘     ▌       ▛▀▖              ▜            ▌
▝▞  ▞▀▖ ▌ ▌  ▌  ▌ ▌ ▛▀▖ ▞▀▖ ▌ ▌ ▞▀▖ ▌  ▌ ▛▀▖ ▐  ▞▀▖ ▝▀▖ ▞▀▌
 ▌  ▌ ▌ ▌ ▌  ▌  ▌ ▌ ▌ ▌ ▛▀  ▌ ▌ ▌ ▌ ▐▐▐  ▌ ▌ ▐  ▌ ▌ ▞▀▌ ▌ ▌
 ▘  ▝▀  ▝▀▘  ▘  ▝▀▘ ▀▀  ▝▀▘ ▀▀  ▝▀   ▘▘  ▘ ▘  ▘ ▝▀  ▝▀▘ ▝▀▘

By @BennyThink, VIP mode: {ENABLE_VIP}, Celery Mode: {ENABLE_CELERY}
Version: {get_revision()}
    """
    print(banner)
    app.run()
