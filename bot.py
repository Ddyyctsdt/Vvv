import os
import time
import asyncio
import aiohttp
import telebot
import socket
import json
import base64
import urllib.parse
import subprocess
import threading
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp_socks import ProxyConnector

# ================= Configuration & Globals =================
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
if not BOT_TOKEN or BOT_TOKEN == 'YOUR_BOT_TOKEN_HERE':
    print("WARNING: TELEGRAM_BOT_TOKEN not set!")

bot = telebot.TeleBot(BOT_TOKEN)

user_data = {}
active_sessions = {}

# Background Asyncio Loop for Traffic Generation
loop = asyncio.new_event_loop()

def start_background_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

bg_thread = threading.Thread(target=start_background_loop, args=(loop,), daemon=True)
bg_thread.start()

# ================= Core Logic: Link Parsing & Ping =================
def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

def parse_config_link(link):
    outbound = {}
    host = ""
    port = 443

    try:
        if link.startswith("vmess://"):
            b64_str = link[8:]
            b64_str += "=" * ((4 - len(b64_str) % 4) % 4)
            data = json.loads(base64.b64decode(b64_str).decode('utf-8'))
            host = data.get("add", "")
            port = int(data.get("port", 443))
            
            stream_settings = {"network": data.get("net", "tcp")}
            if data.get("tls") == "tls":
                stream_settings["security"] = "tls"
                stream_settings["tlsSettings"] = {"serverName": data.get("sni", host) or host}
            
            if data.get("net") == "ws":
                stream_settings["wsSettings"] = {"path": data.get("path", "/"), "headers": {"Host": data.get("host", host)}}
            elif data.get("net") == "grpc":
                stream_settings["grpcSettings"] = {"serviceName": data.get("path", "")}

            outbound = {
                "protocol": "vmess",
                "settings": {
                    "vnext": [{"address": host, "port": port, "users": [{"id": data.get("id", ""), "alterId": int(data.get("aid", 0))}]}]
                },
                "streamSettings": stream_settings
            }

        elif link.startswith("vless://") or link.startswith("trojan://"):
            parsed = urllib.parse.urlparse(link)
            protocol = "vless" if link.startswith("vless") else "trojan"
            uuid_pass = parsed.username
            host = parsed.hostname
            port = parsed.port or 443
            params = urllib.parse.parse_qs(parsed.query)
            
            stream_settings = {"network": params.get("type", ["tcp"])[0]}
            security = params.get("security", ["none"])[0]
            if security != "none":
                stream_settings["security"] = security
                if security == "tls" or security == "reality":
                    tls_settings = {"serverName": params.get("sni", [host])[0]}
                    if security == "reality":
                        tls_settings["publicKey"] = params.get("pbk", [""])[0]
                        tls_settings["shortId"] = params.get("sid", [""])[0]
                        stream_settings["realitySettings"] = tls_settings
                    else:
                        stream_settings["tlsSettings"] = tls_settings
            
            if stream_settings["network"] == "ws":
                stream_settings["wsSettings"] = {"path": params.get("path", ["/"])[0], "headers": {"Host": params.get("host", [host])[0]}}
            elif stream_settings["network"] == "grpc":
                stream_settings["grpcSettings"] = {"serviceName": params.get("serviceName", [""])[0]}

            if protocol == "vless":
                outbound = {
                    "protocol": "vless",
                    "settings": {"vnext": [{"address": host, "port": port, "users": [{"id": uuid_pass, "encryption": "none"}]}]},
                    "streamSettings": stream_settings
                }
            else:
                outbound = {
                    "protocol": "trojan",
                    "settings": {"servers": [{"address": host, "port": port, "password": uuid_pass}]},
                    "streamSettings": stream_settings
                }
    except Exception as e:
        print(f"Parsing error: {e}")
        return None, None, None

    return host, port, outbound

def tcp_ping(host, port):
    if not host or not port:
        return None
    start = time.time()
    try:
        sock = socket.create_connection((host, int(port)), timeout=3)
        sock.close()
        return int((time.time() - start) * 1000)
    except Exception:
        return None

# ================= Traffic Generator Engine =================
class TrafficGenerator:
    def __init__(self, chat_id, message_id, config_link, limit_type, limit_value):
        self.chat_id = chat_id
        self.message_id = message_id
        self.config_link = config_link
        self.limit_type = limit_type
        self.limit_value = float(limit_value)
        
        self.local_port = get_free_port()
        self.config_file = f"config_{self.local_port}.json"
        self.xray_process = None
        
        self.is_running = True
        self.downloaded_bytes = 0
        self.uploaded_bytes = 0
        self.start_time = time.time()
        self.last_bytes = 0
        self.stall_counter = 0
        self.tasks = []

    def start_xray(self):
        host, port, outbound = parse_config_link(self.config_link)
        if not outbound:
            return False

        xray_conf = {
            "log": {"loglevel": "warning"},
            "inbounds": [{"port": self.local_port, "listen": "127.0.0.1", "protocol": "socks", "settings": {"udp": True}}],
            "outbounds": [outbound]
        }
        
        with open(self.config_file, "w") as f:
            json.dump(xray_conf, f, indent=2)

        self.xray_process = subprocess.Popen(["./xray/xray", "-c", self.config_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3) # Wait for Xray to bind port
        return True

    async def _download_task(self):
        connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{self.local_port}')
        async with aiohttp.ClientSession(connector=connector) as session:
            while self.is_running:
                url = f"http://speedtest.tele2.net/100MB.zip?rand={os.urandom(4).hex()}"
                try:
                    async with session.get(url, timeout=30) as response:
                        while self.is_running:
                            chunk = await response.content.read(65536)
                            if not chunk: break
                            self.downloaded_bytes += len(chunk)
                except Exception:
                    await asyncio.sleep(1)

    async def _upload_task(self):
        dummy_data = os.urandom(1024 * 1024)
        connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{self.local_port}')
        async with aiohttp.ClientSession(connector=connector) as session:
            while self.is_running:
                url = "http://speedtest.tele2.net/upload.php"
                try:
                    async with session.post(url, data=dummy_data, timeout=30) as response:
                        await response.text()
                        self.uploaded_bytes += len(dummy_data)
                except Exception:
                    await asyncio.sleep(1)

    async def _live_reporter(self):
        while self.is_running:
            await asyncio.sleep(3)
            if not self.is_running:
                break
                
            elapsed = time.time() - self.start_time
            total_mb = (self.downloaded_bytes + self.uploaded_bytes) / (1024 * 1024)
            speed = (self.downloaded_bytes + self.uploaded_bytes - self.last_bytes) / (1024 * 1024) / 3
            self.last_bytes = self.downloaded_bytes + self.uploaded_bytes

            if speed < 0.05:
                self.stall_counter += 3
            else:
                self.stall_counter = 0

            status_msg = (
                f"🚀 **عملیات در حال اجرا (All-in-One)**\n\n"
                f"⏱ زمان سپری شده: `{int(elapsed)} ثانیه`\n"
                f"📥 دانلود: `{self.downloaded_bytes / (1024**2):.2f} MB`\n"
                f"📤 آپلود: `{self.uploaded_bytes / (1024**2):.2f} MB`\n"
                f"⚡ سرعت لحظه‌ای: `{speed:.2f} MB/s`\n"
                f"🎯 هدف: `{self.limit_value} {self.limit_type}`"
            )
            
            try:
                bot.edit_message_text(chat_id=self.chat_id, message_id=self.message_id, text=status_msg, parse_mode="Markdown")
            except Exception:
                pass

            if self.limit_type == 'GB' and total_mb >= (self.limit_value * 1024):
                await self.stop("✅ عملیات با موفقیت (رسیدن به حجم هدف) پایان یافت.")
            elif self.limit_type == 'Min' and elapsed >= (self.limit_value * 60):
                await self.stop("✅ عملیات با موفقیت (رسیدن به زمان هدف) پایان یافت.")
            elif self.stall_counter >= 15:
                await self.stop("❌ توقف خودکار: سرعت برای 15 ثانیه صفر بود (احتمالاً حجم کانفیگ تمام شده است).")

    async def start(self):
        if not self.start_xray():
            bot.edit_message_text("❌ خطا در پارس کانفیگ یا راه‌اندازی Xray.", chat_id=self.chat_id, message_id=self.message_id)
            if self.chat_id in active_sessions:
                del active_sessions[self.chat_id]
            return
            
        self.tasks = [asyncio.create_task(self._download_task()) for _ in range(10)]
        self.tasks += [asyncio.create_task(self._upload_task()) for _ in range(5)]
        self.tasks.append(asyncio.create_task(self._live_reporter()))
        
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def stop(self, reason=""):
        if not self.is_running:
            return
        self.is_running = False
        
        for task in self.tasks:
            if not task.done():
                task.cancel()
                
        if self.xray_process:
            self.xray_process.terminate()
            self.xray_process.wait()
            
        if os.path.exists(self.config_file):
            try:
                os.remove(self.config_file)
            except Exception:
                pass

        if reason:
            try:
                bot.send_message(self.chat_id, reason)
                bot.edit_message_text("🛑 عملیات پایان یافت.", chat_id=self.chat_id, message_id=self.message_id)
            except Exception:
                pass
                
        if self.chat_id in active_sessions:
            del active_sessions[self.chat_id]

# ================= Telegram Bot Handlers =================

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "سلام! به ربات تولید ترافیک یکپارچه خوش آمدید. کانفیگ خود (vmess/vless/trojan) را ارسال کنید.")

@bot.message_handler(commands=['stop'])
def stop_operation(message):
    chat_id = message.chat.id
    if chat_id in active_sessions:
        session = active_sessions[chat_id]
        asyncio.run_coroutine_threadsafe(session.stop("🛑 عملیات به صورت دستی متوقف شد."), loop)
    else:
        bot.send_message(chat_id, "عملیاتی در حال اجرا نیست.")

@bot.message_handler(func=lambda msg: msg.text.startswith(('vmess://', 'vless://', 'trojan://')))
def handle_config(message):
    chat_id = message.chat.id
    
    if chat_id in active_sessions:
        bot.send_message(chat_id, "⚠️ شما در حال حاضر یک عملیات فعال دارید. ابتدا با /stop آن را لغو کنید.")
        return

    config_link = message.text
    wait_msg = bot.send_message(chat_id, "⏳ در حال بررسی کانفیگ و پینگ سرور...")
    
    host, port, outbound = parse_config_link(config_link)
    if not outbound:
        bot.edit_message_text("❌ لینک نامعتبر است یا پشتیبانی نمی‌شود.", chat_id, wait_msg.message_id)
        return
        
    ping_ms = tcp_ping(host, port)
    ping_text = f"{ping_ms}ms" if ping_ms else "خطا در اتصال (Timeout)"
    
    user_data[chat_id] = {'config': config_link, 'host': host, 'port': port, 'ping': ping_ms}
    
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("مصرف بر اساس حجم (GB)", callback_data="type_vol"),
        InlineKeyboardButton("مصرف بر اساس زمان (Min)", callback_data="type_time")
    )
    bot.edit_message_text(f"✅ کانفیگ تایید شد.\n🌐 سرور: `{host}:{port}`\n🏓 پینگ: `{ping_text}`\n\nنوع محدودیت را انتخاب کنید:", chat_id, wait_msg.message_id, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data in ['type_vol', 'type_time'])
def handle_limit_type(call):
    chat_id = call.message.chat.id
    limit_type = 'GB' if call.data == 'type_vol' else 'Min'
    if chat_id not in user_data:
        bot.send_message(chat_id, "لطفاً مجدداً کانفیگ را ارسال کنید.")
        return
    user_data[chat_id]['limit_type'] = limit_type
    
    msg = bot.send_message(chat_id, f"مقدار {'حجم (گیگابایت)' if limit_type == 'GB' else 'زمان (دقیقه)'} را وارد کنید:")
    bot.register_next_step_handler(msg, process_limit_value)

def process_limit_value(message):
    chat_id = message.chat.id
    if chat_id not in user_data:
        return
    try:
        val = float(message.text)
        user_data[chat_id]['limit_value'] = val
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🚀 شروع تولید ترافیک", callback_data="start_traffic"))
        
        l_type = user_data[chat_id]['limit_type']
        text = f"**پیش‌فاکتور عملیات**\n\n🔹 سرور: `{user_data[chat_id]['host']}:{user_data[chat_id]['port']}`\n🔹 پینگ: `{user_data[chat_id]['ping']}ms`\n🔹 نوع: `{l_type}`\n🔹 مقدار: `{val}`\n\nبرای شروع مستقیم روی همین سرور کلیک کنید."
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
    except ValueError:
        bot.send_message(chat_id, "مقدار نامعتبر. دوباره کانفیگ بفرستید.")

@bot.callback_query_handler(func=lambda call: call.data == 'start_traffic')
def start_traffic_btn(call):
    chat_id = call.message.chat.id
    
    if chat_id in active_sessions:
        bot.answer_callback_query(call.id, "عملیات از قبل در حال اجراست!", show_alert=True)
        return
        
    data = user_data.get(chat_id)
    if not data:
        return
        
    wait_msg = bot.send_message(chat_id, "⏳ در حال استارت هسته Xray و تسک‌های موازی...")
    
    # Initialize Engine Session
    session = TrafficGenerator(chat_id, wait_msg.message_id, data['config'], data['limit_type'], data['limit_value'])
    active_sessions[chat_id] = session
    
    # Push to asyncio background thread without blocking Telebot
    asyncio.run_coroutine_threadsafe(session.start(), loop)
    bot.answer_callback_query(call.id, "سیستم فعال شد.")

# ================= Entry Point =================
if __name__ == "__main__":
    print("Bot is running in ALL-IN-ONE Mode. Waiting for messages...")
    bot.infinity_polling(timeout=10, long_polling_timeout=5)
