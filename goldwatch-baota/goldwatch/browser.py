"""Shared browser selection and sanitized diagnostics; no browser is kept resident."""
import os
from pathlib import Path
import re
from urllib.parse import urlsplit, unquote

from .config import BASE_DIR


def browser_path():
    explicit = os.getenv("CHROMIUM_EXECUTABLE_PATH") or os.getenv("CHROMIUM_PATH")
    if explicit:
        return explicit
    root = Path(os.getenv("PLAYWRIGHT_BROWSERS_PATH", ".browsers"))
    root = root if root.is_absolute() else BASE_DIR / root
    revisions = sorted((p for p in root.glob("chromium-*") if p.name.split("-")[-1].isdigit()),
                       key=lambda p: int(p.name.split("-")[-1]), reverse=True)
    paths = [p / folder / "chrome" for p in revisions for folder in ("chrome-linux64", "chrome-linux")]
    paths += [Path(p) for p in ("/usr/bin/chromium", "/usr/bin/google-chrome", "/usr/bin/chromium-browser")]
    return next((str(p) for p in paths if p.is_file()), "")


def launch_options():
    options = {"headless": True, "timeout": 30000, "args": ["--disable-dev-shm-usage"]}
    executable = browser_path()
    if executable:
        options["executable_path"] = executable
    address = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("https_proxy") or os.getenv("http_proxy")
    if address:
        url = urlsplit(address)
        host = url.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        options["proxy"] = {"server": f"{url.scheme}://{host}" + (f":{url.port}" if url.port else "")}
        if url.username:
            options["proxy"].update(username=unquote(url.username), password=unquote(url.password or ""))
    return options


def error_details(error):
    messages, item = [], error
    for _ in range(4):
        if item is None:
            break
        messages.append(f"{type(item).__name__}: {item}")
        item = item.__cause__ or item.__context__
    text = "\n".join(messages)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip() and "<launching>" not in line)
    text = re.sub(r"([a-z][a-z\d+.-]*://)[^\s/@]+@", r"\1[redacted]@", text, flags=re.I)
    values = [os.getenv("MYSQL_PASSWORD", ""), os.getenv("ADMIN_TOKEN", "")]
    for key in ("DATABASE_URL", "HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        try:
            password = urlsplit(os.getenv(key, "")).password
            if password:
                values.extend((password, unquote(password)))
        except ValueError:
            pass
    for value in sorted(set(values), key=len, reverse=True):
        if value:
            text = text.replace(value, "[redacted]")
    return text[:1800]
