"""Точка входа веб-сервера: argparse + uvicorn."""

from __future__ import annotations

import os
import socket

import share_config
from server import listen_port, share_mode, share_token


def _lan_ipv4() -> list[str]:
    """Локальные IPv4 (кроме loopback) — подсказка для LAN-доступа."""
    found: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in found:
                found.append(ip)
    except OSError:
        pass
    if not found:
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect(("8.8.8.8", 80))
                ip = probe.getsockname()[0]
                if ip and not ip.startswith("127."):
                    found.append(ip)
            finally:
                probe.close()
        except OSError:
            pass
    return found


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Qwen Orchestra web chat")
    parser.add_argument(
        "--share",
        action="store_true",
        help="Слушать 0.0.0.0 (LAN / проброшенный порт). Один общий сеанс без аккаунтов.",
    )
    parser.add_argument(
        "--token",
        default="",
        help="Пароль для --share (HTTP Basic). Пусто = без пароля (не рекомендуется в интернет).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Порт. В --share по умолчанию из share.json (иначе 8787).",
    )
    parser.add_argument(
        "--host",
        default="",
        help="Bind-адрес. По умолчанию: 127.0.0.1 или 0.0.0.0 при --share.",
    )
    args = parser.parse_args()

    if args.share:
        os.environ["QWEN_SHARE"] = "1"
    if args.token.strip():
        os.environ["QWEN_SHARE_TOKEN"] = args.token.strip()

    sharing = share_mode()
    if sharing:
        public_ip = share_config.ensure_public_ip(refresh=True)
        port = share_config.resolve_share_port(args.port)
        share_config.remember_share_endpoint(public_ip, port)
        os.environ["QWEN_PORT"] = str(port)
    else:
        if args.port > 0:
            os.environ["QWEN_PORT"] = str(args.port)
        port = listen_port()
        public_ip = None

    host = (args.host.strip() or ("0.0.0.0" if sharing else "127.0.0.1"))

    if sharing:
        guest = share_config.guest_url(public_ip, port)
        print("Режим SHARE: сервер доступен с сети (один общий чат/настройки).")
        print(f"  bind: {host}:{port}")
        print(f"  локально: http://127.0.0.1:{port}")
        if guest:
            print(f"  гостю: {guest}")
        else:
            print("  гостю: задайте public_ip/port в share.json")
        for ip in _lan_ipv4():
            print(f"  в LAN: http://{ip}:{port}")
        print(f"  конфиг: {share_config.share_config_path()}")
        if share_token():
            print("  пароль: задан (--token / QWEN_SHARE_TOKEN), браузер спросит логин/пароль")
            print("          (логин любой, пароль = ваш токен)")
        else:
            print("  ВНИМАНИЕ: пароль не задан — любой, кто дойдёт до порта, увидит чаты")
            print("            и сможет менять настройки/OpenRouter-ключ.")
            print("            Запуск с паролем: python server.py --share --token СЕКРЕТ")
        print()
    else:
        print(f"Локальный режим: http://127.0.0.1:{port}")

    uvicorn.run(
        "server:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )
