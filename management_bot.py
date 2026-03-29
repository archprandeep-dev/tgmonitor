"""
Management Bot - Control all Instagram monitor clients
Runs independently and controls all client instances
"""

import asyncio
import logging
import json
import os
import re
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional

from telethon import TelegramClient, events, Button
from dotenv import load_dotenv

# ─────────────────────────────────────────────
# Setup paths and load .env
# ─────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.resolve()
load_dotenv(PROJECT_ROOT / ".env")

# ─────────────────────────────────────────────
# Setup logging
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(PROJECT_ROOT / 'management_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logging.getLogger('telethon').setLevel(logging.WARNING)
logger = logging.getLogger("management_bot")

# ─────────────────────────────────────────────
# Configuration from .env
# ─────────────────────────────────────────────
BOT_TOKEN  = os.getenv("BOT_TOKEN")
ADMIN_ID   = int(os.getenv("ADMIN_ID", "5740574752"))
OWNER_ID   = int(os.getenv("OWNER_ID"))
OWNER_HASH = os.getenv("OWNER_HASH")

if not all([BOT_TOKEN, OWNER_ID, OWNER_HASH]):
    print("❌ Missing BOT_TOKEN, OWNER_ID, or OWNER_HASH in .env")
    exit(1)

# ─────────────────────────────────────────────
# Systemd service names per client
# Change these to match your actual service names:
#   sudo systemctl status ig-monitor-client1
# ─────────────────────────────────────────────
SERVICE_NAMES = {
    # folder_name : systemd_service_name
    # Run: systemctl status <service_name>  to find your service names
    "keo":      "keo",        # if service name == folder name, use same string
    "ligarius": "ligarius",   # add all your client folders here
    # "client1": "ig-monitor-client1",  # example for client1
}

CLIENTS_DIR = PROJECT_ROOT / "clients"


# ─────────────────────────────────────────────
# Utility functions
# ─────────────────────────────────────────────

def run_service_cmd(action: str, client_name: str) -> tuple:
    """Run systemctl command. Returns (success, output)"""
    service = SERVICE_NAMES.get(client_name)
    if not service:
        return False, f"No service name configured for {client_name}. Edit SERVICE_NAMES in management_bot.py"
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", action, service],
            capture_output=True, text=True, timeout=10
        )
        ok  = result.returncode == 0
        out = (result.stdout + result.stderr).strip()
        return ok, out or "OK"
    except subprocess.TimeoutExpired:
        return False, "Command timed out"
    except PermissionError:
        return False, "Permission denied — add sudoers rule (see setup notes)"
    except FileNotFoundError:
        return False, "systemctl not found at /usr/bin/systemctl"
    except Exception as e:
        return False, str(e)


def get_service_status(client_name: str) -> str:
    """Returns emoji + text status of systemd service"""
    service = SERVICE_NAMES.get(client_name)
    if not service:
        return "⚪ No service"
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", service],
            capture_output=True, text=True, timeout=5
        )
        status = result.stdout.strip()
        return {
            "active":     "🟢 Running",
            "inactive":   "🔴 Stopped",
            "failed":     "❌ Failed",
            "activating": "🟡 Starting",
        }.get(status, f"⚪ {status}")
    except Exception as e:
        return f"⚪ Unknown"


def update_env_file(client_name: str, key: str, value: str) -> bool:
    """Update or add a key in the client's .env file"""
    env_path = CLIENTS_DIR / client_name / ".env"
    if not env_path.exists():
        logger.error(f".env not found: {env_path}")
        return False
    try:
        with open(env_path, 'r') as f:
            lines = f.readlines()

        updated  = False
        new_lines = []
        for line in lines:
            if re.match(rf'^{re.escape(key)}\s*=', line):
                new_lines.append(f"{key}={value}\n")
                updated = True
            else:
                new_lines.append(line)

        if not updated:           # key not present yet — append
            new_lines.append(f"\n{key}={value}\n")

        with open(env_path, 'w') as f:
            f.writelines(new_lines)

        logger.info(f"Updated {client_name}/.env  {key}=***")
        return True
    except Exception as e:
        logger.error(f"update_env_file error: {e}")
        return False


def update_config_json(client_name: str, key: str, value) -> bool:
    """Update a key in the client's config.json"""
    config_path = CLIENTS_DIR / client_name / "config.json"
    if not config_path.exists():
        return False
    try:
        with open(config_path) as f:
            config = json.load(f)
        config[key] = value
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"update_config_json error: {e}")
        return False


def load_clients() -> Dict:
    """Scan clients/ and load each client's config + monitored data"""
    clients = {}
    if not CLIENTS_DIR.exists():
        logger.warning(f"Clients dir not found: {CLIENTS_DIR}")
        return clients

    for d in sorted(CLIENTS_DIR.iterdir()):
        if not d.is_dir():
            continue  # skip files, load ANY subfolder

        config    = {}
        monitored = {}
        try:
            with open(d / "config.json") as f:
                config = json.load(f)
        except:
            pass
        try:
            with open(d / "monitored.json") as f:
                monitored = json.load(f)
        except:
            pass

        clients[d.name] = {
            'dir':           d,
            'config_path':   d / "config.json",
            'monitored_path':d / "monitored.json",
            'env_path':      d / ".env",
            'config':        config,
            'monitored':     monitored,
        }
        logger.info(f"✅ Loaded {d.name}  ({len(monitored)} accounts)")

    return clients


# ─────────────────────────────────────────────
# Bot class
# ─────────────────────────────────────────────

class ManagementBot:

    def __init__(self):
        self.bot     = None
        self.clients : Dict          = {}
        self.pending : Dict[int, tuple] = {}   # { user_id: (action, client_name) }

    # ── Init ──────────────────────────────────

    async def initialize(self):
        self.bot     = TelegramClient('management_bot', OWNER_ID, OWNER_HASH)
        await self.bot.start(bot_token=BOT_TOKEN)
        self.clients = load_clients()

    # ── Menu helpers ──────────────────────────

    async def show_main_menu(self, event):
        buttons = [
            [Button.inline("📊 All Clients Status",  b"view_all")],
            [Button.inline("📋 Monitored Accounts",  b"view_accounts")],
            [Button.inline("📝 View Logs",           b"view_logs")],
            [Button.inline("⚙️  Client Settings",    b"settings")],
            [Button.inline("🔧 Service Control",     b"service_control")],
            [Button.inline("🔄 Reload Configs",      b"reload")],
        ]
        text = "🤖 **Instagram Monitor — Management Bot**\n\nSelect an option:"
        try:
            await event.edit(text, buttons=buttons)
        except:
            await event.reply(text, buttons=buttons)

    async def show_client_picker(self, event, action: str, title: str = "Select a client:"):
        if not self.clients:
            await event.edit("❌ No clients found in `clients/` folder",
                             buttons=[[Button.inline("« Back", b"menu")]])
            return
        buttons = []
        for name in sorted(self.clients.keys()):
            svc   = get_service_status(name)
            label = f"{name}  {svc}"
            buttons.append([Button.inline(label, f"{action}:{name}".encode())])
        buttons.append([Button.inline("« Back", b"menu")])
        await event.edit(title, buttons=buttons)

    def build_summary(self) -> str:
        if not self.clients:
            return "❌ No clients found.\nMake sure `clients/` folder exists."
        text  = "📊 **All Clients Status**\n\n"
        total = 0
        for name in sorted(self.clients.keys()):
            c     = self.clients[name]
            count = len(c['monitored'])
            total += count
            cfg   = c['config']
            min_i = cfg.get('min_check_interval', 300) // 60
            max_i = cfg.get('max_check_interval', 600) // 60
            svc   = get_service_status(name)
            text += f"**{name}**  {svc}\n"
            text += f"  └ Accounts : {count}\n"
            text += f"  └ Interval : {min_i}–{max_i} min\n\n"
        text += f"**Total: {total} accounts monitored**"
        return text

    # ── Handlers ──────────────────────────────

    def setup_handlers(self):

        # /start
        @self.bot.on(events.NewMessage(pattern='/start'))
        async def _start(event):
            if event.sender_id != ADMIN_ID:
                await event.reply("🚫 Unauthorized"); return
            await self.show_main_menu(event)

        # /status
        @self.bot.on(events.NewMessage(pattern='/status'))
        async def _status(event):
            if event.sender_id != ADMIN_ID: return
            self.clients = load_clients()
            await event.reply(self.build_summary(), parse_mode='md')

        # ── Text input (handles all pending prompts) ──
        @self.bot.on(events.NewMessage(func=lambda e: e.is_private and not e.text.startswith('/')))
        async def _text_input(event):
            if event.sender_id != ADMIN_ID: return
            uid = event.sender_id

            if uid not in self.pending:
                return   # Not waiting for anything from this user

            action, client_name = self.pending.pop(uid)
            text = event.text.strip()

            # ── Interval ──────────────────────
            if action == 'set_interval':
                m = re.match(r'^(\d+)-(\d+)$', text)
                if not m:
                    await event.reply("❌ Format: `MIN-MAX`  e.g. `2-5`\nSend again or press Cancel.")
                    self.pending[uid] = (action, client_name)   # keep pending
                    return
                min_v, max_v = int(m.group(1)), int(m.group(2))
                if min_v < 1 or max_v < min_v:
                    await event.reply("❌ Min ≥ 1 and Max ≥ Min required.")
                    self.pending[uid] = (action, client_name)
                    return
                update_config_json(client_name, 'min_check_interval', min_v * 60)
                update_config_json(client_name, 'max_check_interval', max_v * 60)
                self.clients = load_clients()
                await event.reply(
                    f"✅ **{client_name}** interval → **{min_v}–{max_v} min**\n"
                    f"⚠️ Restart client to apply.",
                    parse_mode='md'
                )

            # ── Sessions ──────────────────────
            elif action == 'set_session':
                sessions = [s.strip() for s in text.split(',') if s.strip()]
                if not sessions:
                    await event.reply("❌ No sessions found. Paste comma-separated session IDs.")
                    self.pending[uid] = (action, client_name)
                    return

                ok_env = update_env_file(client_name, 'INSTAGRAM_SESSIONS', ','.join(sessions))

                # Update session.json too
                ok_json = False
                session_path = CLIENTS_DIR / client_name / "session.json"
                try:
                    data = {}
                    if session_path.exists():
                        with open(session_path) as f:
                            data = json.load(f)
                    data['sessions']      = sessions
                    data['current_index'] = 0
                    with open(session_path, 'w') as f:
                        json.dump(data, f, indent=2)
                    ok_json = True
                except Exception as e:
                    logger.error(f"session.json update error: {e}")

                await event.reply(
                    f"🔑 **{client_name}** sessions updated ({len(sessions)} session(s))\n"
                    f"  └ .env updated        : {'✅' if ok_env  else '❌'}\n"
                    f"  └ session.json updated: {'✅' if ok_json else '❌'}\n\n"
                    f"⚠️ Restart client to apply.",
                    parse_mode='md'
                )

            # ── Proxy ─────────────────────────
            elif action == 'set_proxy':
                if not (text.startswith('http://') or text.startswith('https://')):
                    await event.reply("❌ Proxy must start with `http://` or `https://`\nSend again or Cancel.")
                    self.pending[uid] = (action, client_name)
                    return
                ok_env  = update_env_file(client_name,    'PROXY_URL',  text)
                ok_json = update_config_json(client_name, 'proxy_url',  text)
                self.clients = load_clients()
                await event.reply(
                    f"🔌 **{client_name}** proxy updated\n"
                    f"  └ .env updated       : {'✅' if ok_env  else '❌'}\n"
                    f"  └ config.json updated: {'✅' if ok_json else '❌'}\n\n"
                    f"⚠️ Restart client to apply.",
                    parse_mode='md'
                )

        # ── Callback queries ──────────────────
        @self.bot.on(events.CallbackQuery)
        async def _callback(event):
            if event.sender_id != ADMIN_ID:
                await event.answer("Unauthorized", alert=True); return

            data = event.data.decode()
            uid  = event.sender_id

            # Cancel pending input when user hits any button
            if uid in self.pending and not data.startswith("set_"):
                self.pending.pop(uid, None)

            # ── Main menu ─────────────────────
            if data == "menu":
                await self.show_main_menu(event)

            elif data == "reload":
                self.clients = load_clients()
                await event.answer(f"✅ Reloaded {len(self.clients)} client(s)", alert=True)

            # ── View all ──────────────────────
            elif data == "view_all":
                self.clients = load_clients()
                await event.edit(
                    self.build_summary(),
                    buttons=[[Button.inline("🔄 Refresh", b"view_all"),
                              Button.inline("« Back",    b"menu")]],
                    parse_mode='md'
                )

            # ── Monitored accounts ────────────
            elif data == "view_accounts":
                await self.show_client_picker(event, "accounts", "📋 Select client:")

            elif data.startswith("accounts:"):
                name     = data.split(":", 1)[1]
                self.clients = load_clients()
                monitored = self.clients.get(name, {}).get('monitored', {})

                if not monitored:
                    text = f"📋 **{name}**\n\nNo accounts monitored"
                else:
                    text = f"📋 **{name}** — {len(monitored)} account(s)\n\n"
                    for uname, info in monitored.items():
                        added = datetime.fromisoformat(info['added_at']).strftime('%d/%m %H:%M')
                        text += f"• @{uname}  (added {added})\n"

                await event.edit(text, buttons=[
                    [Button.inline("🔄 Refresh", f"accounts:{name}".encode()),
                     Button.inline("« Back",    b"view_accounts")]
                ])

            # ── Logs ──────────────────────────
            elif data == "view_logs":
                await self.show_client_picker(event, "logs", "📝 Select client:")

            elif data.startswith("logs:"):
                name     = data.split(":", 1)[1]
                log_path = CLIENTS_DIR / name / f"{name}.log"

                if not log_path.exists():
                    await event.edit(f"❌ Log not found for {name}",
                                     buttons=[[Button.inline("« Back", b"view_logs")]])
                    return

                try:
                    with open(log_path, encoding='utf-8') as f:
                        lines = f.readlines()
                    last = ''.join(lines[-30:])
                    # Remove verbose response previews
                    last = re.sub(r'Response preview:.*?\.\.\.', '[response omitted]', last)
                    last = re.sub(r'Response structure.*?\n', '', last)
                    text = f"📝 **{name} — last 30 lines**\n\n```\n{last[-3500:]}\n```"
                except Exception as e:
                    text = f"❌ Error: {e}"

                await event.edit(text, buttons=[
                    [Button.inline("🔄 Refresh", f"logs:{name}".encode()),
                     Button.inline("« Back",    b"view_logs")]
                ], parse_mode='md')

            # ── Settings ──────────────────────
            elif data == "settings":
                await self.show_client_picker(event, "settings_menu", "⚙️ Select client:")

            elif data.startswith("settings_menu:"):
                name = data.split(":", 1)[1]
                c    = self.clients.get(name, {})
                cfg  = c.get('config', {})
                min_i = cfg.get('min_check_interval', 300) // 60
                max_i = cfg.get('max_check_interval', 600) // 60
                svc   = get_service_status(name)

                text = (
                    f"⚙️ **Settings — {name}**\n\n"
                    f"Status      : {svc}\n"
                    f"Interval    : {min_i}–{max_i} min\n"
                    f"Screenshots : {'✅' if cfg.get('generate_screenshots', True) else '❌'}\n"
                    f".env        : {'✅ exists' if (CLIENTS_DIR/name/'.env').exists() else '❌ missing'}\n"
                )
                await event.edit(text, buttons=[
                    [Button.inline("⏱️ Change Interval", f"set_interval:{name}".encode())],
                    [Button.inline("🔑 Change Sessions",  f"set_session:{name}".encode())],
                    [Button.inline("🔌 Change Proxy",     f"set_proxy:{name}".encode())],
                    [Button.inline("« Back", b"settings")]
                ], parse_mode='md')

            # ── Prompts ───────────────────────
            elif data.startswith("set_interval:"):
                name  = data.split(":", 1)[1]
                cfg   = self.clients.get(name, {}).get('config', {})
                min_i = cfg.get('min_check_interval', 300) // 60
                max_i = cfg.get('max_check_interval', 600) // 60
                self.pending[uid] = ('set_interval', name)
                await event.edit(
                    f"⏱️ **Change Interval — {name}**\n\n"
                    f"Current : **{min_i}–{max_i} min**\n\n"
                    f"Send `MIN-MAX` in minutes, e.g. `2-5`",
                    buttons=[[Button.inline("✖ Cancel", f"settings_menu:{name}".encode())]],
                    parse_mode='md'
                )

            elif data.startswith("set_session:"):
                name = data.split(":", 1)[1]
                self.pending[uid] = ('set_session', name)
                await event.edit(
                    f"🔑 **Change Sessions — {name}**\n\n"
                    f"Send one or more Instagram session IDs separated by commas:\n\n"
                    f"`sessionid_one,sessionid_two`\n\n"
                    f"📌 How to get session ID:\n"
                    f"Instagram in browser → F12 → Application → Cookies → `sessionid`",
                    buttons=[[Button.inline("✖ Cancel", f"settings_menu:{name}".encode())]],
                    parse_mode='md'
                )

            elif data.startswith("set_proxy:"):
                name    = data.split(":", 1)[1]
                current = self.clients.get(name, {}).get('config', {}).get('proxy_url', 'Not set')
                self.pending[uid] = ('set_proxy', name)
                await event.edit(
                    f"🔌 **Change Proxy — {name}**\n\n"
                    f"Current : `{current}`\n\n"
                    f"Send new proxy:\n`http://user:pass@host:port`",
                    buttons=[[Button.inline("✖ Cancel", f"settings_menu:{name}".encode())]],
                    parse_mode='md'
                )

            # ── Service control ───────────────
            elif data == "service_control":
                await self.show_client_picker(event, "service_menu", "🔧 Select client:")

            elif data.startswith("service_menu:"):
                name = data.split(":", 1)[1]
                svc  = get_service_status(name)
                svc_name = SERVICE_NAMES.get(name, "not configured")
                await event.edit(
                    f"🔧 **Service Control — {name}**\n\n"
                    f"Service : `{svc_name}`\n"
                    f"Status  : {svc}",
                    buttons=[
                        [Button.inline("▶️ Start",   f"svc_start:{name}".encode()),
                         Button.inline("⏹ Stop",    f"svc_stop:{name}".encode())],
                        [Button.inline("🔄 Restart", f"svc_restart:{name}".encode()),
                         Button.inline("📋 Status",  f"svc_status:{name}".encode())],
                        [Button.inline("« Back", b"service_control")]
                    ],
                    parse_mode='md'
                )

            elif data.startswith("svc_"):
                parts  = data.split(":", 1)
                action = parts[0][4:]   # start / stop / restart / status
                name   = parts[1]

                if action == "status":
                    svc = get_service_status(name)
                    await event.answer(f"{name}: {svc}", alert=True)
                    return

                await event.answer(f"⏳ Running systemctl {action}…")
                ok, out = run_service_cmd(action, name)
                svc     = get_service_status(name)
                icon    = "✅" if ok else "❌"

                await event.edit(
                    f"{icon} **{name}** — `systemctl {action}`\n\n"
                    f"Output : `{out[:200]}`\n"
                    f"Status : {svc}",
                    buttons=[
                        [Button.inline("▶️ Start",   f"svc_start:{name}".encode()),
                         Button.inline("⏹ Stop",    f"svc_stop:{name}".encode())],
                        [Button.inline("🔄 Restart", f"svc_restart:{name}".encode()),
                         Button.inline("📋 Status",  f"svc_status:{name}".encode())],
                        [Button.inline("« Back", b"service_control")]
                    ],
                    parse_mode='md'
                )

    # ── Start / Stop ──────────────────────────

    async def start(self):
        logger.info("="*50)
        logger.info("Starting Management Bot")
        logger.info("="*50)
        await self.initialize()
        self.setup_handlers()
        logger.info(f"✅ Admin ID : {ADMIN_ID}")
        logger.info(f"✅ Clients  : {list(self.clients.keys())}")
        logger.info("="*50)
        await self.bot.run_until_disconnected()

    async def stop(self):
        logger.info("Stopping…")
        if self.bot:
            await self.bot.disconnect()


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

async def main():
    bot = ManagementBot()
    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("⚠️ Interrupted")
    except Exception as e:
        logger.error(f"❌ Fatal: {e}", exc_info=True)
    finally:
        await bot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...")