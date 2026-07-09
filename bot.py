import os
import time
import argparse
import asyncio
import aiohttp
import requests
import telebot
import socket
import json
import base64
import urllib.parse
import subprocess
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp_socks import ProxyConnector

# ================= Configuration =================
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', 'YOUR_GITHUB_PAT_HERE')
GITHUB_REPO = os.environ.get('GITHUB_REPO', 'username/repo')

bot = telebot.TeleBot(BOT_TOKEN)

# ================= Core Logic: Link Parsing & Ping =================
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

# ================= Mode 2: GitHub Worker Logic =================
class TrafficWorker:
    def __init__(self, config_link, limit_type, limit_value, chat_id, message_id):
        self.config_link = config_link
        self.limit_type = limit_type
        self.limit_value = float(limit_value)
        self.chat_id = chat_id
        self.message_id = message_id
        
        self.downloaded_bytes = 0
        self.uploaded_bytes = 0
        self.start_time = time.time()
        self.is_running = True
        self.last_bytes = 0
        self.stall_counter = 0
        self.xray_process = None

    def start_xray(self):
        host, port, outbound = parse_config_link(self.config_link)
        if not outbound:
            self.finish_operation("❌ خطا در پارس کردن لینک کانفیگ. عملیات متوقف شد.")
            self.is_running = False
            return False

        xray_conf = {
            "log": {"loglevel": "warning"},
            "inbounds": [{"port": 10808, "listen": "127.0.0.1", "protocol": "socks", "settings": {"udp": True}}],
            "outbounds": [outbound]
        }
        
        with open("config.json", "w") as f:
            json.dump(xray_conf, f, indent=2)

        print("Starting Xray core...")
        self.xray_process = subprocess.Popen(["./xray/xray", "-c", "config.json"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3) # Wait for Xray to initialize
        return True

    async def download_task(self):
        while self.is_running:
            url = f"http://speedtest.tele2.net/100MB.zip?rand={os.urandom(4).hex()}"
            try:
                connector = ProxyConnector.from_url('socks5://127.0.0.1:10808')
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.get(url, timeout=30) as response:
                        while self.is_running:
                            chunk = await response.content.read(65536)
                            if not chunk:
                                break
                            self.downloaded_bytes += len(chunk)
            except Exception:
                await asyncio.sleep(1)

    async def upload_task(self):
        dummy_data = os.urandom(1024 * 1024)
        while self.is_running:
            url = "http://speedtest.tele2.net/upload.php"
            try:
                connector = ProxyConnector.from_url('socks5://127.0.0.1:10808')
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.post(url, data=dummy_data, timeout=30) as response:
                        await response.text()
                        self.uploaded_bytes += len(dummy_data)
            except Exception:
                await asyncio.sleep(1)

    async def live_reporter(self):
        while self.is_running:
            await asyncio.sleep(3)
            elapsed = time.time() - self.start_time
            total_mb = (self.downloaded_bytes + self.uploaded_bytes) / (1024 * 1024)
            speed = (self.downloaded_bytes + self.uploaded_bytes - self.last_bytes) / (1024 * 1024) / 3
            self.last_bytes = self.downloaded_bytes + self.uploaded_bytes

            if speed < 0.05:
                self.stall_counter += 3
            else:
                self.stall_counter = 0

            status_msg = (
                f"🚀 **عملیات در حال اجرا (گیت‌هاب)**\n\n"
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
                self.is_running = False
                self.finish_operation("✅ عملیات با موفقیت (رسیدن به حجم هدف) پایان یافت.")
            elif self.limit_type == 'Min' and elapsed >= (self.limit_value * 60):
                self.is_running = False
                self.finish_operation("✅ عملیات با موفقیت (رسیدن به زمان هدف) پایان یافت.")
            elif self.stall_counter >= 15:
                self.is_running = False
                self.finish_operation("❌ توقف خودکار: سرعت برای 15 ثانیه صفر بود (احتمالاً حجم کانفیگ تمام شده است).")

    def finish_operation(self, reason):
        bot.send_message(self.chat_id, reason)
        if self.xray_process:
            self.xray_process.terminate()

    async def run(self):
        if not self.start_xray():
            return
        tasks = [self.download_task() for _ in range(10)] + \
                [self.upload_task() for _ in range(5)] + \
                [self.live_reporter()]
        await asyncio.gather(*tasks)

# ================= Mode 1: Telegram Bot Logic =================
user_data = {}

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "سلام! یک کانفیگ V2Ray (مثلاً vmess:// یا vless://) بفرستید.")

@bot.message_handler(func=lambda msg: msg.text.startswith(('vmess://', 'vless://', 'trojan://')))
def handle_config(message):
    chat_id = message.chat.id
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
        markup.add(InlineKeyboardButton("🚀 شروع عملیات", callback_data="start_dispatch"))
        
        l_type = user_data[chat_id]['limit_type']
        text = f"**پیش‌فاکتور عملیات**\n\n🔹 سرور: `{user_data[chat_id]['host']}:{user_data[chat_id]['port']}`\n🔹 پینگ: `{user_data[chat_id]['ping']}ms`\n🔹 نوع: `{l_type}`\n🔹 مقدار: `{val}`\n\nبرای اجرا روی سرور گیت‌هاب کلیک کنید."
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
    except ValueError:
        bot.send_message(chat_id, "مقدار نامعتبر. دوباره کانفیگ بفرستید.")

@bot.callback_query_handler(func=lambda call: call.data == 'start_dispatch')
def dispatch_to_github(call):
    chat_id = call.message.chat.id
    data = user_data.get(chat_id)
    if not data:
        return
        
    wait_msg = bot.send_message(chat_id, "⏳ در حال ارسال دستور...")
    
    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/main.yml/dispatches"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {GITHUB_TOKEN}"
    }
    payload = {
        "ref": "main",
        "inputs": {
            "config_link": data['config'],
            "limit_type": data['limit_type'],
            "limit_value": str(data['limit_value']),
            "chat_id": str(chat_id),
            "message_id": str(wait_msg.message_id)
        }
    }
    
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code == 204:
        bot.edit_message_text("✅ دستور با موفقیت ارسال شد. در انتظار آپدیت از گیت‌هاب...", chat_id, wait_msg.message_id)
    else:
        bot.edit_message_text(f"❌ خطا در اتصال به گیت‌هاب: {response.text}", chat_id, wait_msg.message_id)

# ================= Entry Point =================
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--config', type=str)
    parser.add_argument('--type', type=str)
    parser.add_argument('--value', type=str)
    parser.add_argument('--chat', type=str)
    parser.add_argument('--msg', type=str)
    args = parser.parse_args()

    if args.worker:
        worker = TrafficWorker(args.config, args.type, args.value, args.chat, args.msg)
        asyncio.run(worker.run())
    else:
        bot.infinity_polling()
