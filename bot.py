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
import random
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp_socks import ProxyConnector

# ================= Configuration & Globals =================
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
if not BOT_TOKEN or BOT_TOKEN == 'YOUR_BOT_TOKEN_HERE':
    print("WARNING: TELEGRAM_BOT_TOKEN not set!")

bot = telebot.TeleBot(BOT_TOKEN)

user_data = {}
active_sessions = {}

# Background Asyncio Loop for Matrix Engine
loop = asyncio.new_event_loop()

def start_background_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

bg_thread = threading.Thread(target=start_background_loop, args=(loop,), daemon=True)
bg_thread.start()

# ================= Core Logic: Parsing & Ping =================
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
                
        # Enable Multiplexing (Mux) for maximum performance
        outbound["mux"] = {"enabled": True, "concurrency": 8}
        
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

# ================= Matrix Engine =================
class CoreInstance:
    def __init__(self, core_id):
        self.core_id = core_id
        self.port = get_free_port()
        self.config_file = f"config_core_{self.core_id}_{self.port}.json"
        self.process = None
        self.dl_bytes = 0
        self.ul_bytes = 0
        self.last_bytes = 0

class MatrixTrafficGenerator:
    def __init__(self, chat_id, message_id, config_link, limit_type, limit_value, core_count):
        self.chat_id = chat_id
        self.message_id = message_id
        self.config_link = config_link
        self.limit_type = limit_type
        self.limit_value = float(limit_value)
        self.core_count = int(core_count)
        
        self.cores = [CoreInstance(i + 1) for i in range(self.core_count)]
        self.is_running = True
        self.start_time = time.time()
        self.stall_counter = 0
        self.tasks = []

    def start_xray_cores(self):
        host, port, outbound = parse_config_link(self.config_link)
        if not outbound:
            return False

        for core in self.cores:
            xray_conf = {
                "log": {"loglevel": "error"},
                "inbounds": [{"port": core.port, "listen": "127.0.0.1", "protocol": "socks", "settings": {"udp": True}}],
                "outbounds": [outbound]
            }
            with open(core.config_file, "w") as f:
                json.dump(xray_conf, f, indent=2)

            core.process = subprocess.Popen(
                ["./xray/xray", "-c", core.config_file],
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL
            )
        
        time.sleep(3) # Wait for all Xray cores to bind ports
        return True

    async def _download_task(self, core):
        connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{core.port}')
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive"
        }
        
        target_urls = [
            "https://speed.cloudflare.com/__down?bytes=1073741824",
            "http://updates-http.cdn-apple.com/2020/windows/012-34071-20200508-C6926D0A-8F25-11EA-8BA6-24E81CE97D11/AppleX64RecoveryInit.exe",
            "https://dl.google.com/dl/android/studio/install/2023.2.1.25/android-studio-2023.2.1.25-windows.exe"
        ]
        
        timeout = aiohttp.ClientTimeout(total=None, sock_read=60)
        
        async with aiohttp.ClientSession(connector=connector, headers=headers, timeout=timeout) as session:
            while self.is_running:
                base_url = random.choice(target_urls)
                
                if "cloudflare.com" not in base_url:
                    sep = "&" if "?" in base_url else "?"
                    url = f"{base_url}{sep}rand={os.urandom(8).hex()}"
                else:
                    url = base_url
                    
                try:
                    async with session.get(url) as response:
                        if response.status in [200, 206]:
                            while self.is_running:
                                chunk = await response.content.read(2 * 1024 * 1024)
                                if not chunk: 
                                    break
                                core.dl_bytes += len(chunk)
                        else:
                            await asyncio.sleep(1)
                except Exception:
                    await asyncio.sleep(1)

    async def _upload_task(self, core):
        dummy_data = os.urandom(1024 * 1024) # 1MB dummy data
        connector = ProxyConnector.from_url(f'socks5://127.0.0.1:{core.port}')
        async with aiohttp.ClientSession(connector=connector) as session:
            while self.is_running:
                url = "http://speedtest.tele2.net/upload.php"
                try:
                    async with session.post(url, data=dummy_data, timeout=60) as response:
                        await response.text()
                        core.ul_bytes += len(dummy_data)
                except Exception:
                    await asyncio.sleep(1)

    async def _live_reporter(self):
        while self.is_running:
            await asyncio.sleep(3)
            if not self.is_running:
                break
                
            elapsed = time.time() - self.start_time
            
            total_dl = 0
            total_ul = 0
            total_speed = 0
            
            core_reports = []
            
            for core in self.cores:
                total_dl += core.dl_bytes
                total_ul += core.ul_bytes
                
                core_speed = (core.dl_bytes + core.ul_bytes - core.last_bytes) / 3 / (1024 * 1024)
                core.last_bytes = core.dl_bytes + core.ul_bytes
                total_speed += core_speed
                
                core_reports.append(
                    f"▫️ هسته {core.core_id}: 📥 {core.dl_bytes/(1024**2):.1f}MB | 📤 {core.ul_bytes/(1024**2):.1f}MB | ⚡ {core_speed:.1f} MB/s"
                )

            total_mb = (total_dl + total_ul) / (1024 * 1024)

            if total_speed < 0.05:
                self.stall_counter += 3
            else:
                self.stall_counter = 0

            status_msg = f"🟢 **ماتریکس در حال اجرا ({self.core_count} هسته)**\n\n"
            status_msg += "\n".join(core_reports) + "\n\n"
            status_msg += "📊 **گزارش کلی:**\n"
            status_msg += f"⏱ زمان: `{int(elapsed)} ثانیه`\n"
            status_msg += f"📥 مجموع دانلود: `{total_dl / (1024**2):.2f} MB`\n"
            status_msg += f"📤 مجموع آپلود: `{total_ul / (1024**2):.2f} MB`\n"
            status_msg += f"⚡ سرعت کل: `{total_speed:.2f} MB/s`\n"
            status_msg += f"🎯 هدف: `{self.limit_value} {self.limit_type}`"
            
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🛑 توقف دستی", callback_data="stop_matrix"))
            
            try:
                bot.edit_message_text(chat_id=self.chat_id, message_id=self.message_id, text=status_msg, reply_markup=markup, parse_mode="Markdown")
            except Exception:
                pass

            # Auto-stop conditions
            if self.limit_type == 'GB' and total_mb >= (self.limit_value * 1024):
                await self.stop("✅ مأموریت موفق: رسیدن به حجم هدف")
            elif self.limit_type == 'Min' and elapsed >= (self.limit_value * 60):
                await self.stop("✅ مأموریت موفق: رسیدن به زمان هدف")
            elif self.stall_counter >= 15:
                await self.stop("❌ قطعی سرور: سرعت برای ۱۵ ثانیه صفر بود")

    async def start(self):
        if not self.start_xray_cores():
            bot.edit_message_text("❌ خطا در پارس کانفیگ یا راه‌اندازی Xray.", chat_id=self.chat_id, message_id=self.message_id)
            if self.chat_id in active_sessions:
                del active_sessions[self.chat_id]
            return
            
        for core in self.cores:
            # 5 Download tasks and 2 Upload tasks per core
            self.tasks += [asyncio.create_task(self._download_task(core)) for _ in range(5)]
            self.tasks += [asyncio.create_task(self._upload_task(core)) for _ in range(2)]
            
        self.tasks.append(asyncio.create_task(self._live_reporter()))
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def stop(self, status_reason="🛑 توقف دستی"):
        if not self.is_running:
            return
        self.is_running = False
        
        # 1. Cancel Tasks
        for task in self.tasks:
            if not task.done():
                task.cancel()
                
        # 2. Terminate Processes & Cleanup Configs
        total_dl = 0
        total_ul = 0
        for core in self.cores:
            total_dl += core.dl_bytes
            total_ul += core.ul_bytes
            
            if core.process:
                core.process.terminate()
                core.process.wait()
                
            if os.path.exists(core.config_file):
                try:
                    os.remove(core.config_file)
                except Exception:
                    pass

        # 3. Final Report
        elapsed = time.time() - self.start_time
        total_mb = (total_dl + total_ul) / (1024 * 1024)
        avg_speed = total_mb / elapsed if elapsed > 0 else 0
        
        final_msg = (
            f"📄 **فاکتور نهایی عملیات ماتریکس**\n\n"
            f"وضعیت: {status_reason}\n"
            f"تعداد هسته‌ها: `{self.core_count}`\n"
            f"⏱ زمان کل سپری شده: `{int(elapsed)} ثانیه`\n"
            f"📦 مجموع دیتای ردوبدل شده: `{total_mb:.2f} MB`\n"
            f"⚡ میانگین سرعت کل: `{avg_speed:.2f} MB/s`\n"
        )
        
        try:
            bot.edit_message_text("عملیات پایان یافت.", chat_id=self.chat_id, message_id=self.message_id)
            bot.send_message(self.chat_id, final_msg, parse_mode="Markdown")
        except Exception:
            pass
            
        if self.chat_id in active_sessions:
            del active_sessions[self.chat_id]

# ================= Telegram Bot Handlers =================

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "سلام! به ربات تولید ترافیک ماتریکس خوش آمدید. کانفیگ خود (vmess/vless/trojan) را ارسال کنید.")

@bot.message_handler(commands=['stop'])
def stop_cmd(message):
    chat_id = message.chat.id
    if chat_id in active_sessions:
        session = active_sessions[chat_id]
        asyncio.run_coroutine_threadsafe(session.stop("🛑 متوقف شده توسط دستور کاربر"), loop)
    else:
        bot.send_message(chat_id, "عملیاتی در حال اجرا نیست.")

@bot.message_handler(func=lambda msg: msg.text.startswith(('vmess://', 'vless://', 'trojan://')))
def handle_config(message):
    chat_id = message.chat.id
    
    if chat_id in active_sessions:
        bot.send_message(chat_id, "⚠️ شما در حال حاضر یک ماتریکس فعال دارید. ابتدا با /stop آن را لغو کنید.")
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
        bot.answer_callback_query(call.id, "داده‌ها منقضی شده، مجدداً کانفیگ بفرستید.")
        return
    user_data[chat_id]['limit_type'] = limit_type
    
    msg = bot.edit_message_text(f"مقدار {'حجم (گیگابایت)' if limit_type == 'GB' else 'زمان (دقیقه)'} را در چت ارسال کنید:", chat_id, call.message.message_id)
    bot.register_next_step_handler(msg, process_limit_value)

def process_limit_value(message):
    chat_id = message.chat.id
    if chat_id not in user_data:
        return
    try:
        val = float(message.text)
        user_data[chat_id]['limit_value'] = val
        
        markup = InlineKeyboardMarkup(row_width=4)
        markup.add(
            InlineKeyboardButton("۱ هسته", callback_data="cores_1"),
            InlineKeyboardButton("۳ هسته", callback_data="cores_3"),
            InlineKeyboardButton("۵ هسته", callback_data="cores_5"),
            InlineKeyboardButton("۱۰ هسته", callback_data="cores_10")
        )
        bot.send_message(chat_id, "⚙️ قدرت ماتریکس (تعداد موتورهای موازی Xray) را انتخاب کنید:", reply_markup=markup)
    except ValueError:
        bot.send_message(chat_id, "مقدار نامعتبر. دوباره کانفیگ بفرستید.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('cores_'))
def handle_matrix_cores(call):
    chat_id = call.message.chat.id
    if chat_id not in user_data:
        return
    
    cores_count = int(call.data.split('_')[1])
    user_data[chat_id]['cores'] = cores_count
    
    data = user_data[chat_id]
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🚀 شروع عملیات ماتریکس", callback_data="start_matrix"))
    
    text = (
        f"**پیش‌فاکتور عملیات ماتریکس**\n\n"
        f"🔹 سرور: `{data['host']}:{data['port']}`\n"
        f"🔹 پینگ: `{data['ping']}ms`\n"
        f"🔹 محدودیت: `{data['limit_value']} {data['limit_type']}`\n"
        f"🔥 قدرت ماتریکس: `{cores_count} هسته همزمان`\n\n"
        f"برای شروع عملیات کلیک کنید."
    )
    bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == 'start_matrix')
def start_matrix_btn(call):
    chat_id = call.message.chat.id
    
    if chat_id in active_sessions:
        bot.answer_callback_query(call.id, "عملیات از قبل در حال اجراست!", show_alert=True)
        return
        
    data = user_data.get(chat_id)
    if not data:
        return
        
    wait_msg = bot.edit_message_text("⏳ در حال استارت موتورهای ماتریکس Xray و تسک‌های موازی...", chat_id, call.message.message_id)
    
    session = MatrixTrafficGenerator(
        chat_id, wait_msg.message_id, 
        data['config'], data['limit_type'], data['limit_value'], data['cores']
    )
    active_sessions[chat_id] = session
    
    asyncio.run_coroutine_threadsafe(session.start(), loop)
    bot.answer_callback_query(call.id, "ماتریکس فعال شد.")

@bot.callback_query_handler(func=lambda call: call.data == 'stop_matrix')
def stop_matrix_btn(call):
    chat_id = call.message.chat.id
    if chat_id in active_sessions:
        bot.answer_callback_query(call.id, "در حال توقف ماتریکس...")
        session = active_sessions[chat_id]
        asyncio.run_coroutine_threadsafe(session.stop("🛑 متوقف شده توسط کاربر (دکمه)"), loop)
    else:
        bot.answer_callback_query(call.id, "عملیات یافت نشد یا قبلاً متوقف شده است.")

# ================= Entry Point =================
if __name__ == "__main__":
    print("Bot is running in MATRIX All-In-One Mode. Waiting for messages...")
    bot.infinity_polling(timeout=10, long_polling_timeout=5)
