"""Parse PUBLIC quote cards; never infer one market from the other."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

N = r"(\d[\d,]*(?:\.\d+)?)"
WAN = r"万(?:金|金币|游戏币|币)?"
DIRECT = re.compile(r"(?<![-\d.])" + N + r"\s*元\s*[=＝:：]\s*" + N + r"\s*" + WAN)
INVERSE = re.compile(r"(?<![-\d.])" + N + r"\s*" + WAN + r"\s*[=＝:：]\s*" + N + r"\s*元")
UNIT = re.compile(r"(?<![-\d.])" + N + r"\s*元\s*/\s*" + WAN)
QUANTITY = re.compile(r"收货数量\s*[:：]?\s*" + N + r"\s*" + WAN)
MIN_RATE, MAX_RATE = Decimal("0.000001"), Decimal("1000000000")


@dataclass(frozen=True)
class ParsedQuote:
    side: str
    source_key: str
    title: str
    source_url: str
    wan_per_yuan: Decimal
    yuan_per_wan: Decimal
    quantity_wan: Decimal | None
    evidence: str


@dataclass
class ParseResult:
    quotes: list[ParsedQuote]
    warnings: list[str]
    blocked: bool = False


def decimal(text):
    return Decimal(text.replace(",", ""))


def extract_rate(text):
    """Prefer explicit yuan-to-wan display. The reciprocal is always recalculated."""
    text = re.sub(r"\s+", " ", text)
    direct = []
    for yuan, wan in DIRECT.findall(text):
        try:
            if decimal(yuan) > 0:
                direct.append(decimal(wan) / decimal(yuan))
        except (InvalidOperation, ZeroDivisionError):
            continue
    inverse = []
    for wan, yuan in INVERSE.findall(text):
        if decimal(yuan) > 0:
            inverse.append(decimal(wan) / decimal(yuan))
    candidates = direct or inverse
    if not candidates:
        candidates = [1 / decimal(value) for value in UNIT.findall(text) if decimal(value) > 0]
    if not candidates or any(not MIN_RATE <= rate <= MAX_RATE for rate in candidates):
        return None
    rate = candidates[0]
    # Multiple different cards in a parent must not collapse into a fictitious quote.
    if any(abs(other / rate - 1) > Decimal("0.005") for other in candidates[1:]):
        return None
    if direct and any(abs(other / rate - 1) > Decimal("0.02") for other in inverse):
        return None
    return rate


def safe_url(href, source):
    if not href or href.strip().startswith(("#", "javascript:", "data:")):
        return source
    target = urljoin(source, href or "")
    parsed = urlsplit(target)
    if (parsed.scheme in ("https", "http") and parsed.hostname and not parsed.username and not parsed.password
            and (parsed.hostname == "7881.com" or parsed.hostname.endswith(".7881.com"))):
        return target
    return source


def title_for(card, side):
    for tag in card.select("[title], h2, h3, h4"):
        value = tag.get("title") or tag.get_text(" ", strip=True)
        if len(value) > 6 and ("万" in value or "交易" in value):
            return value[:280]
    text = card.get_text(" ", strip=True)
    if side == "player":
        match = re.search(r"[【\[](?:当面|拍卖|邮寄).*?(?=游戏区服|商品类型|¥|￥|$)", text)
        if match:
            return match.group(0)[:280]
    return "商家收货报价" if side == "merchant" else "玩家金币挂单"


def parse_html(html, source_url, merchant_selector="", player_selector=""):
    soup = BeautifulSoup(html, "html.parser")
    for unwanted in soup.select("script, style, noscript, [hidden], [aria-hidden='true'], [data-gw-hidden='true']"):
        unwanted.decompose()
    for tag in list(soup.select("[style]")):
        if tag.parent and re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", tag.get("style", ""), re.I):
            tag.decompose()
    quotes, warnings, seen = [], [], set()

    def add(card, action, side, index):
        if id(card) in seen:
            return
        text = card.get_text(" ", strip=True)
        actions = [tag for tag in card.find_all(("a", "button", "input"))
                   if re.fullmatch(r"立即出售|马上出售|立即购买|马上购买", (tag.get("value") or tag.get_text("", strip=True)).strip(" >›»"))]
        if len(actions) > 1:
            warnings.append(f"{side} 选择范围包含多个报价卡片，已跳过；请选择单张卡片")
            return
        rate = extract_rate(text)
        if rate is None:
            return
        hrefs = [tag.get("href", "") for tag in card.select("a[title][href], h2 a[href], h3 a[href]")]
        if action:
            hrefs.append(action.get("href", ""))
        hrefs.extend(tag.get("href", "") for tag in card.select("a[href]"))
        link = next((target for href in hrefs if (target := safe_url(href, source_url)) != source_url), source_url)
        identity = f"{side}|{link}" if link != source_url else f"{side}|{source_url}|{index}"
        key = hashlib.sha256(identity.encode()).hexdigest()
        quantity = QUANTITY.search(text) if side == "merchant" else None
        quotes.append(ParsedQuote(side, key, title_for(card, side), link, rate, 1 / rate,
                                  decimal(quantity.group(1)) if quantity else None, text[:1200]))
        seen.add(id(card))

    for side, selector, action_pattern in (("merchant", merchant_selector, r"立即出售|马上出售"),
                                           ("player", player_selector, r"立即购买|马上购买")):
        if selector:
            cards = soup.select(selector)
            for index, card in enumerate(cards):
                add(card, None, side, index)
            if not cards:
                warnings.append(f"{side} 自定义选择器未匹配任何报价")
            continue
        actions = [tag for tag in soup.find_all(("a", "button", "input"))
                   if re.fullmatch(action_pattern, (tag.get("value") or tag.get_text("", strip=True)).strip(" >›»"))]
        for index, action in enumerate(actions):
            candidate = None
            for parent in list(action.parents)[:10]:
                if not isinstance(parent, Tag) or parent.name in ("body", "html", "[document]"):
                    break
                text = parent.get_text(" ", strip=True)
                if len(text) > 16000:
                    break
                opposite = r"立即购买|马上购买" if side == "merchant" else r"立即出售|马上出售"
                if any(re.fullmatch(opposite, (tag.get("value") or tag.get_text("", strip=True)).strip(" >›»"))
                       for tag in parent.find_all(("a", "button", "input"))):
                    break
                count_actions = sum(1 for tag in parent.find_all(("a", "button", "input"))
                                    if re.fullmatch(action_pattern, (tag.get("value") or tag.get_text("", strip=True)).strip(" >›»")))
                if count_actions > 1:
                    break
                if extract_rate(text) is not None:
                    candidate = parent
                    # Keep climbing to include title/stock, until the next card begins.
            if candidate is not None:
                add(candidate, action, side, index)
    text = soup.get_text(" ", strip=True)
    blocked = not quotes and bool(re.search(r"人机验证|滑动验证|请完成验证|访问过于频繁|安全验证|Access Denied|captcha", text, re.I))
    if not quotes and not blocked:
        warnings.append("未识别到带单位的报价卡片；可能需要浏览器渲染或调整两个区域的 CSS 选择器")
    return ParseResult(quotes, warnings, blocked)
