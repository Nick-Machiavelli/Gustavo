import os
import json
import time
import logging
import cloudscraper
import html
import re
import tempfile
import trafilatura
import concurrent.futures
import feedparser
from urllib.parse import quote, unquote, urlparse, urlunparse
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from gnews import GNews
from ddgs import DDGS
from dateutil import parser
import hashlib

# --- CONFIGURATION ---
CONFIG = {
    'SEARCH_QUERY': 'Iran AND (Israel OR USA OR nuclear OR conflict OR sanctions OR currency OR IRGC)',
    'SEARCH_QUERIES': [
        'Iran (Israel OR Gaza OR Hezbollah OR Houthis) (attack OR strike OR missile OR drone)',
        'Iran (nuclear OR IAEA OR enrichment OR sanctions)',
        'Iran (dollar OR rial OR currency OR IRGC OR economy)',
        '(Trump OR "Donald Trump") (Iran OR "regime change" OR sanctions OR nuclear OR Israel)',
        '(Netanyahu OR "Benjamin Netanyahu") (Iran OR strike OR nuclear OR Hezbollah OR IRGC)',
        '("Reza Pahlavi" OR "شاهزاده رضا پهلوی" OR "Pahlavi") (Iran OR opposition OR transition OR speech)',
        '("IRGC" OR "Qhalibaf" OR "Qaani" OR "سپاه پاسداران") (Iran OR missile OR proxies OR threat)'
    ],
    'TARGET_SOURCES': [
        'iranintl.com', 'bbc.com/persian', 'radiofarda.com', 'independentpersian.com',
        'dw.com/fa', 'presstv.ir', 'tasnimnews.com', 'farsnews.ir', 'irna.ir', 'mehrnews.com'
    ],
    'PRIORITY_SITES': [
        'bbc.com/persian', 'radiofarda.com', 'iranintl.com',
        'independentpersian.com', 'dw.com/fa'
    ],
    'SOURCE_PRIORITY': {
        'bbc.com': 10, 'radiofarda.com': 10, 'iranintl.com': 9,
        'independentpersian.com': 8, 'dw.com': 8,
        'reuters.com': 9, 'apnews.com': 8, 'aljazeera.com': 7,
        'theguardian.com': 7, 'nytimes.com': 7,
        'tasnimnews.com': 4, 'farsnews.ir': 4, 'irna.ir': 4,
        'mehrnews.com': 4, 'presstv.ir': 3,
    },
    'FILES': {
        'NEWS': 'news.json',
        'MARKET': 'market.json',
        'DAILY_SUMMARY': 'daily_summary.json',
        'SCHEDULE_STATE': 'schedule_state.json'
    },
    # Dashboard URL – auto-corrected to the real Pages URL if env is missing or stale
    'BASE_SITE_URL': (os.environ.get('BASE_SITE_URL') or '').strip() or 'https://nick-machiavelli.github.io/Gustavo/',
    'CHANNEL_LINK': 'https://t.me/Enqelab_e_Iran',
    'TELEGRAM': {
        'BOT_TOKEN': os.environ.get('TG_BOT_TOKEN'),
        'CHANNEL_ID': os.environ.get('TG_CHANNEL_ID')
    },
    'TIMEOUT': 12,
    'AI_TIMEOUT': 45,
    'MAX_WORKERS': 3,
    'MAX_CANDIDATES': 30,
    'MAX_TEXT_CHARS': 1800,
    'MIN_TEXT_LEN': 100,
    'MIN_AI_URGENCY_HINT': 5,
    # Provider-agnostic AI settings - point these at any OpenAI-compatible
    # chat-completions endpoint (Groq, Zhipu/GLM, DeepSeek, OpenRouter, etc.)
    # Using `or` (not the dict .get default) so that an empty-string secret
    # in GitHub Actions still falls back to the Groq defaults below.
    'AI_API_KEY': os.environ.get('AI_API_KEY'),
    'AI_BASE_URL': os.environ.get('AI_BASE_URL') or 'https://api.groq.com/openai/v1/chat/completions',
    'AI_MODEL': os.environ.get('AI_MODEL') or 'llama-3.1-8b-instant',
    'AI_RETRIES': 3,
    'MAX_NEWS_AGE_HOURS': 36,
    'HISTORY_SIZE': 300,
    'RESOLVE_GOOGLE_URLS': True,
}

BAD_IMAGE_HOSTS = (
    'lh3.googleusercontent.com',
    'lh4.googleusercontent.com',
    'lh5.googleusercontent.com',
    'lh6.googleusercontent.com',
    'encrypted-tbn0.gstatic.com',
    'encrypted-tbn1.gstatic.com',
    'encrypted-tbn2.gstatic.com',
    'encrypted-tbn3.gstatic.com',
    'news.google.com',
    'www.google.com',
    'google.com',
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()


class Gustavo:
    def __init__(self):
        self.scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
        self.scraper.headers.update({
            'Accept-Language': 'en-US,en;q=0.9,fa;q=0.8',
            'Cache-Control': 'no-cache',
        })
        self.existing_news = self._load_existing_news()

        self.seen_urls = set()
        self.seen_titles = set()
        self.recent_title_hashes = set()
        self.failed_hosts = set()

        for item in self.existing_news:
            if item.get('url'):
                self.seen_urls.add(self._clean_url(item['url']))
            for key in ('title_en', 'title_fa'):
                if item.get(key):
                    self.seen_titles.add(self._normalize_text(item[key]))
                    self.recent_title_hashes.add(self._title_hash(item[key]))

        if len(self.recent_title_hashes) > 200:
            self.recent_title_hashes = set(list(self.recent_title_hashes)[-150:])

        self.gnews_en = GNews(language='en', country='US', period='4h', max_results=5)

    # ───────────────────────── helpers ─────────────────────────

    def _get_tehran_time(self):
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo("Asia/Tehran"))
        except ImportError:
            return datetime.now(timezone(timedelta(hours=3, minutes=30)))

    def _is_schedule_already_sent(self, slot_key):
        path = CONFIG['FILES']['SCHEDULE_STATE']
        if not os.path.exists(path):
            return False
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get(slot_key, False)
        except Exception:
            return False

    def _mark_schedule_as_sent(self, slot_key):
        path = CONFIG['FILES']['SCHEDULE_STATE']
        data = {}
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[slot_key] = True
        self._atomic_json_dump(path, data)

    def _load_previous_daily_summary(self):
        path = CONFIG['FILES']['DAILY_SUMMARY']
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None

    def _clean_url(self, url):
        if not url:
            return ""
        try:
            parsed = urlparse(url)
            clean = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
            return clean.rstrip('/')
        except Exception:
            return url

    def _normalize_text(self, text):
        if not text:
            return ""
        text = text.replace('ي', 'ی').replace('ك', 'ک').replace('\u200c', ' ')
        clean = re.sub(r'[^\w\s]', '', text.lower())
        return re.sub(r'\s+', '', clean)

    def _fix_persian_orthography(self, text):
        """Fix common Persian spelling/orthography errors (zero-width, ye/ke, spacing, common typos).
        Applied to every AI-generated Persian field before saving/posting."""
        if not text or not isinstance(text, str):
            return text
        # 1. Normalize Arabic ye/ke to Persian
        text = text.replace('ي', 'ی').replace('ك', 'ک').replace('ۀ', 'هٔ')
        # 2. Fix common misspellings (without ZWNJ issues – use plain forms)
        fixes = {
            'مسول': 'مسئول',
            'مسولین': 'مسئولان',
            'مشکلات ': 'مشکلات ',
            'اتهامات': 'اتهام‌ها',
            'اقدامات': 'اقدام‌ها',
            'اطلاعات': 'اطلاعات',
            'ساختمان': 'ساختمان',
            'انفجار ': 'انفجار ',
            'هسته ای': 'هسته‌ای',
            'هسته‌ای ': 'هسته‌ای ',
            'موشکی': 'موشکی',
            'پهپادی': 'پهپادی',
            'تحریم ها': 'تحریم‌ها',
            'تحریمها': 'تحریم‌ها',
            'حمله ها': 'حمله‌ها',
            'حمله‌ها ': 'حمله‌ها ',
            'نظامی ': 'نظامی ',
            'اقتصادی ': 'اقتصادی ',
            'سیاسی ': 'سیاسی ',
            'آمریکا ': 'آمریکا ',
            'اسراییل': 'اسرائیل',
            'اسراییلی': 'اسرائیلی',
            'اطلاعات ': 'اطلاعات ',
            'جمهوری اسلامی': 'جمهوری اسلامی',
            'سپاه پاسداران': 'سپاه پاسداران',
            'وزارت خارجه': 'وزارت خارجه',
            '  ': ' ',
        }
        for wrong, correct in fixes.items():
            text = text.replace(wrong, correct)
        # 3. Fix verb prefixes: "می رود" -> "می‌رود", "نمی شود" -> "نمی‌شود"
        text = re.sub(r'\bمی\s+', 'می‌', text)
        text = re.sub(r'\bنمی\s+', 'نمی‌', text)
        # 4. Fix plural suffix spacing: "ها " with preceding space -> "‌ها"
        text = re.sub(r'(\S)\s+ها\b', r'\1‌ها', text)
        # but revert if it broke valid words like "تنها"
        text = text.replace('تن‌ها', 'تنها')
        # 5. Collapse multiple spaces, trim
        text = re.sub(r'\s+', ' ', text).strip()
        # 6. Fix punctuation spacing: no space before ، ؛ : . ! ?
        text = re.sub(r'\s+([،؛:!.؟])', r'\1', text)
        text = re.sub(r'([،؛:])\s*', r'\1 ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def _proofread_item(self, item):
        """Apply orthography fix to all Persian fields of a news/bulletin item."""
        if not isinstance(item, dict):
            return item
        for key in ('title_fa', 'summary', 'impact', 'tag', 'lead_paragraph', 'headline'):
            if key in item and item[key]:
                if isinstance(item[key], list):
                    item[key] = [self._fix_persian_orthography(s) for s in item[key]]
                elif isinstance(item[key], str):
                    item[key] = self._fix_persian_orthography(item[key])
        # also key_findings, bullets, themes etc.
        for list_key in ('key_findings', 'bullets', 'themes'):
            if list_key in item and isinstance(item[list_key], list):
                item[list_key] = [self._fix_persian_orthography(s) for s in item[list_key]]
        return item

    def _title_hash(self, title):
        return hashlib.md5(self._normalize_text(title).encode('utf-8')).hexdigest()

    def _get_tokens(self, text):
        if not text:
            return set()
        stop_words = {
            'a', 'an', 'the', 'and', 'or', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by',
            'is', 'are', 'was', 'were', 'be', 'been', 'news', 'report', 'reports', 'breaking',
            'live', 'updates', 'latest', 'warns', 'warning', 'says', 'vows', 'issues', 'pushes',
            'threat', 'threats', 'vs', 'economic', 'war', 'warfare', 'iran', 'tehran',
            'از', 'به', 'در', 'که', 'و', 'این', 'آن', 'را', 'برای', 'با', 'است', 'شد',
            'شده', 'می', 'بر', 'یک', 'خود', 'تا', 'کرد', 'نیز', 'ایران', 'تهران', 'خبر', 'فوری'
        }
        text = text.replace('ي', 'ی').replace('ك', 'ک').replace('\u200c', ' ')
        clean = re.sub(r'[^\w\s]', '', text.lower())
        tokens = set()
        for word in clean.split():
            if word not in stop_words and len(word) > 2:
                # Normalize common prefixes/suffixes
                if word.startswith(('un', 're', 'dis')):
                    word = word[2:]
                tokens.add(word)
        return tokens

    def _is_duplicate_fuzzy(self, new_title, comparison_pool):
        norm_title = self._normalize_text(new_title)
        if norm_title in self.seen_titles:
            return True
            
        new_tokens = self._get_tokens(new_title)
        if len(new_tokens) < 2:
            return False

        # Key entity sets for cross-story syndication detection
        key_entity_groups = [
            {'trump', 'china', 'allies', 'sanctions', 'pressure'},
            {'trump', 'china', 'oil', 'trade', 'buyers'},
            {'tanker', 'qatar', 'oil', 'hormuz'},
            {'iaea', 'nuclear', 'enrichment', 'grossi'},
            {'netanyahu', 'israel', 'strike', 'nuclear'}
        ]

        pool = comparison_pool[:120] if len(comparison_pool) > 120 else comparison_pool
        for item in pool:
            existing_title = item.get('title_en') or item.get('title_fa') or item.get('title', '')
            existing_tokens = self._get_tokens(existing_title)
            if not existing_tokens:
                continue

            inter = new_tokens.intersection(existing_tokens)
            union = new_tokens.union(existing_tokens)
            
            # Jaccard threshold 0.60 - relaxed to allow more distinct stories (was 0.55)
            if union and (len(inter) / len(union)) >= 0.60:
                return True

            # If 3 or more distinct key topical tokens match with high overlap, treat as duplicate
            if len(inter) >= 3 and len(inter) / min(len(new_tokens), len(existing_tokens)) >= 0.65:
                return True

            # Match against known entity-event cluster groups (require 3+ overlaps to avoid false positives)
            for group in key_entity_groups:
                if len(new_tokens.intersection(group)) >= 3 and len(existing_tokens.intersection(group)) >= 3:
                    return True

        return False

    def _load_existing_news(self):
        if not os.path.exists(CONFIG['FILES']['NEWS']):
            return []
        try:
            with open(CONFIG['FILES']['NEWS'], 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            return []

    def _domain_score(self, url, publisher=""):
        try:
            host = urlparse(url or '').netloc.lower().replace('www.', '')
            for domain, score in CONFIG['SOURCE_PRIORITY'].items():
                if domain in host:
                    return score
        except Exception:
            pass
        pub = (publisher or '').lower()
        for domain, score in CONFIG['SOURCE_PRIORITY'].items():
            if domain.split('.')[0] in pub:
                return score
        return 3

    def _cheap_urgency_hint(self, title, publisher=""):
        t = (title or '').lower()
        score = 3
        high = [
            'attack', 'strike', 'missile', 'killed', 'nuclear', 'drone', 'war',
            'حمله', 'موشک', 'هسته‌ای', 'پهپاد', 'کشته', 'انفجار', 'تشدید'
        ]
        mid = [
            'sanction', 'dollar', 'currency', 'irgc', 'protest',
            'تحریم', 'دلار', 'ارز', 'سپاه', 'اعتراض'
        ]
        if any(w in t for w in high):
            score += 3
        if any(w in t for w in mid):
            score += 2
        if self._domain_score('', publisher) >= 8:
            score += 1
        return min(score, 9)

    def _guess_tag_from_title(self, title):
        """Lightweight heuristic tag used only when AI enrichment is unavailable."""
        t = (title or '').lower()
        if any(w in t for w in ['hormuz', 'strait', 'ناو', 'کشتی', 'دریایی', 'خلیج', 'navy', 'ship']):
            return 'دریایی'
        if any(w in t for w in ['هسته‌ای', 'اتمی', 'iaea', 'غنی‌سازی', 'nuclear', 'atomic']):
            return 'هسته‌ای'
        if any(w in t for w in ['tether', 'تحریم', 'dollar', 'currency', 'ارز', 'dollar', 'rial', 'تومان', 'اقتصاد']):
            return 'اقتصاد'
        if any(w in t for w in ['نظامی', 'military', 'missile', 'پهپاد', 'موشک', 'حمله', 'ارتش', 'strike', 'army']):
            return 'نظامی'
        if any(w in t for w in ['نیابتی', 'مقاومت', 'حزب‌الله', 'حوثی', 'proxy', 'Houthis', 'Hezbollah']):
            return 'نیابتی'
        return 'سیاسی'

    def _generate_news_id(self, clean_url):
        return hashlib.md5((clean_url or str(time.time())).encode('utf-8')).hexdigest()[:10]

    def _is_valid_image_url(self, url):
        if not url or not isinstance(url, str):
            return False
        u = url.strip()
        if not u.startswith(('http://', 'https://')):
            return False
        if u.startswith('data:'):
            return False
        try:
            host = urlparse(u).netloc.lower().replace('www.', '')
            if any(bad in host for bad in BAD_IMAGE_HOSTS):
                return False
            if 'googleusercontent.com' in host and ('=s0' in u or 'w300' in u or '-rw' in u):
                return False
        except Exception:
            return False
        return True

    def _get_fallback_image(self, text_or_tag):
        # Diverse pool per topic — hash picks one to avoid all same-tag same-image duplicates
        pools = {
            'sea': [
                'https://images.unsplash.com/photo-1509316975850-ff9c5deb0cd9?auto=format&fit=crop&w=800&q=80',
                'https://images.unsplash.com/photo-1518837695005-2083093ee35b?auto=format&fit=crop&w=800&q=80',
                'https://images.unsplash.com/photo-1505118380757-91f5f5632de0?auto=format&fit=crop&w=800&q=80',
            ],
            'military': [
                'https://images.unsplash.com/photo-1585829365295-ab7cd400c167?auto=format&fit=crop&w=800&q=80',
                'https://images.unsplash.com/photo-1590732593895-6c9339736a9b?auto=format&fit=crop&w=800&q=80',
                'https://images.unsplash.com/photo-1550100136-e074fa46d424?auto=format&fit=crop&w=800&q=80',
                'https://images.unsplash.com/photo-1577896859042-9235620b2473?auto=format&fit=crop&w=800&q=80',
            ],
            'nuclear': [
                'https://images.unsplash.com/photo-1581092160607-ee22621dd758?auto=format&fit=crop&w=800&q=80',
                'https://images.unsplash.com/photo-1507413245164-6160d8298b31?auto=format&fit=crop&w=800&q=80',
                'https://images.unsplash.com/photo-1518709594023-6eab9bab7b23?auto=format&fit=crop&w=800&q=80',
            ],
            'economy': [
                'https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?auto=format&fit=crop&w=800&q=80',
                'https://images.unsplash.com/photo-1559526324-4b87b5e36e44?auto=format&fit=crop&w=800&q=80',
                'https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?auto=format&fit=crop&w=800&q=80',
                'https://images.unsplash.com/photo-1526628953301-3e589a6a8b74?auto=format&fit=crop&w=800&q=80',
            ],
            'diplomacy': [
                'https://images.unsplash.com/photo-1529107386315-e1a2ed48a620?auto=format&fit=crop&w=800&q=80',
                'https://images.unsplash.com/photo-1517245386807-bb43f82c33c4?auto=format&fit=crop&w=800&q=80',
                'https://images.unsplash.com/photo-1531973576160-7125cd663d86?auto=format&fit=crop&w=800&q=80',
            ],
            'proxy': [
                'https://images.unsplash.com/photo-1521295121783-8a321d551ad2?auto=format&fit=crop&w=800&q=80',
                'https://images.unsplash.com/photo-1504711434969-e33886168f5c?auto=format&fit=crop&w=800&q=80',
                'https://images.unsplash.com/photo-1495020689067-958852a7765e?auto=format&fit=crop&w=800&q=80',
            ],
            'generic': [
                'https://images.unsplash.com/photo-1504711434969-e33886168f5c?auto=format&fit=crop&w=800&q=80',
                'https://images.unsplash.com/photo-1495020689067-958852a7765e?auto=format&fit=crop&w=800&q=80',
                'https://images.unsplash.com/photo-1518709268805-4e9042af9f23?auto=format&fit=crop&w=800&q=80',
                'https://images.unsplash.com/photo-1521295121783-8a321d551ad2?auto=format&fit=crop&w=800&q=80',
            ],
        }
        t = str(text_or_tag).lower()
        if any(w in t for w in ['ship', 'navy', 'sea', 'strait', 'hormuz', 'دریایی', 'کشتی', 'خلیج', 'ناو']):
            key = 'sea'
        elif any(w in t for w in ['missile', 'strike', 'war', 'army', 'military', 'نظامی', 'موشک', 'پهپاد', 'حمله', 'ارتش']):
            key = 'military'
        elif any(w in t for w in ['nuclear', 'atomic', 'iaea', 'هسته‌ای', 'غنی‌سازی', 'اتمی']):
            key = 'nuclear'
        elif any(w in t for w in ['currency', 'dollar', 'economy', 'تومان', 'دلار', 'تحریم', 'ارز', 'اقتصاد', 'نفت', 'بازار']):
            key = 'economy'
        elif any(w in t for w in ['diplomacy', 'دیپلماسی', 'مذاکره', 'سیاسی', 'وزیر', 'سفیر', 'ترامپ', 'پهپاد']):
            # more specific: trump/politics vs economy already handled
            if any(w in t for w in ['تحریم', 'ارز', 'اقتصاد']): key = 'economy'
            else: key = 'diplomacy'
        elif any(w in t for w in ['نیابتی', 'مقاومت', 'حزب‌الله', 'حوثی', 'محور']):
            key = 'proxy'
        else:
            key = 'generic'
        pool = pools[key]
        # hash-based pick for diversity within same tag
        h = int(hashlib.md5(str(text_or_tag).encode('utf-8')).hexdigest(), 16)
        return pool[h % len(pool)]

    def _is_safe_url(self, url):
        """Validate a URL is well-formed and uses http/https scheme.
        Used to gate source-article links in Telegram posts so a malformed
        URL (e.g. 'None', empty, or javascript:) never breaks the link in the
        caption, which previously raised NameError because the symbol was
        referenced but never defined."""
        if not url or not isinstance(url, str):
            return False
        url = url.strip()
        if url.lower().startswith(('javascript:', 'data:', 'mailto:')):
            return False
        parsed = urlparse(url)
        return parsed.scheme in ('http', 'https') and bool(parsed.netloc)

    def _pick_image(self, *candidates, fallback_text=''):
        for c in candidates:
            if self._is_valid_image_url(c):
                # reject tiny bing thumbnails that often are irrelevant (w<200 style)
                if 'bing.com/th?id=' in c and 'w=100' in c:
                    continue
                return c
        return self._get_fallback_image(fallback_text)

    # ───────────────────────── market (LIVE) ─────────────────────────
    def fetch_market_rates(self):
        """Live: 10 forex (world+ME) + 15 cryptos + 4 oils, dual USD+TMN."""
        # ---- schema ----
        data = {
            "usd": "نامشخص", "usd_raw": None,
            "eur": "نامشخص", "gbp": "نامشخص", "aed": "نامشخص", "try": "نامشخص",
            "sar": "نامشخص", "kwd": "نامشخص", "qar": "نامشخص", "chf": "نامشخص",
            "jpy": "نامشخص", "cad": "نامشخص",
            # 15 cryptos
            "btc_usd": "—", "btc_tmn": "نامشخص",
            "eth_usd": "—", "eth_tmn": "نامشخص",
            "usdt_usd": "1.00", "usdt_tmn": "نامشخص",
            "bnb_usd": "—", "bnb_tmn": "نامشخص",
            "sol_usd": "—", "sol_tmn": "نامشخص",
            "xrp_usd": "—", "xrp_tmn": "نامشخص",
            "usdc_usd": "1.00", "usdc_tmn": "نامشخص",
            "doge_usd": "—", "doge_tmn": "نامشخص",
            "ada_usd": "—", "ada_tmn": "نامشخص",
            "trx_usd": "—", "trx_tmn": "نامشخص",
            "avax_usd": "—", "avax_tmn": "نامشخص",
            "shib_usd": "—", "shib_tmn": "نامشخص",
            "dot_usd": "—", "dot_tmn": "نامشخص",
            "link_usd": "—", "link_tmn": "نامشخص",
            "ltc_usd": "—", "ltc_tmn": "نامشخص",
            # oils
            "brent_usd": "—", "brent_tmn": "نامشخص",
            "wti_usd": "—", "wti_tmn": "نامشخص",
            "opec_usd": "—", "opec_tmn": "نامشخص",
            "dubai_usd": "—", "dubai_tmn": "نامشخص",
            "oil": "نامشخص",
            "updated": "--:--",
            "updated_iso": None
        }
        usd_tmn_raw = None

        # ---- 1) forex: 10 currencies via alanchand ----
        # alanchand slugs: usd, eur, gbp, aed, try, chf, cad, jpy, sar, qar, kwd ...
        forex_slugs = {
            "usd": "usd", "eur": "eur", "gbp": "gbp", "aed": "aed", "try": "try",
            "sar": "sar", "kwd": "kwd", "qar": "qar", "chf": "chf", "jpy": "jpy"
        }
        for key, slug in forex_slugs.items():
            try:
                resp = self.scraper.get(f"https://alanchand.com/en/currencies-price/{slug}", timeout=10)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, 'lxml')
                    inp = soup.find('input', attrs={'data-curr': 'tmn'})
                    if inp:
                        val = inp.get('data-price') or inp.get('value')
                        if val:
                            raw = int(val.replace(',', '').strip())
                            tmn = int(raw / 10)
                            data[key] = f"{tmn:,}"
                            if key == "usd":
                                usd_tmn_raw = tmn
                                data["usd_raw"] = tmn
                                data["usdt_tmn"] = f"{tmn:,}"
                                data["usdc_tmn"] = f"{tmn:,}"
            except Exception:
                pass

        # fallback for sar/kwd/qar/jpy if alanchand missing: estimate via USD cross from exchangerate.host
        missing = [k for k in forex_slugs if data.get(k) == "نامشخص" and k != "usd"]
        if missing and usd_tmn_raw:
            try:
                # exchangerate.host is free no-key, CORS ok for backend
                er = self.scraper.get("https://api.exchangerate.host/latest?base=USD&symbols=EUR,GBP,AED,TRY,SAR,KWD,QAR,CHF,JPY,CAD", timeout=10)
                if er.status_code == 200:
                    rates = er.json().get("rates", {})
                    # rates are units per USD. To get TMN: usd_tmn / USD_per_target? Actually rate = target per USD, so target_tmn = usd_tmn * (1/rate?) Wait: 1 USD = X EUR => 1 EUR = USD/EUR ? Simpler: 1 EUR in TMN = usd_tmn / rate_EUR? No: rate_EUR = EUR per USD, so USD->EUR = rate. EUR->USD = 1/rate. So EUR in TMN = usd_tmn / rate_EUR
                    inv_map = {"eur":"EUR","gbp":"GBP","aed":"AED","try":"TRY","sar":"SAR","kwd":"KWD","qar":"QAR","chf":"CHF","jpy":"JPY","cad":"CAD"}
                    for k in missing:
                        sym = inv_map.get(k)
                        if sym and sym in rates and rates[sym]:
                            try:
                                per_usd = float(rates[sym])
                                if per_usd and per_usd != 0:
                                    tmn = int(usd_tmn_raw / per_usd)
                                    data[k] = f"{tmn:,}"
                            except Exception:
                                pass
            except Exception:
                pass

        # ---- 2) 15 cryptos via CoinGecko (single call) ----
        cg_ids = "bitcoin,ethereum,tether,binancecoin,solana,ripple,usd-coin,dogecoin,cardano,tron,avalanche-2,shiba-inu,polkadot,chainlink,litecoin"
        cg_map = {
            "bitcoin": ("btc_usd", "btc_tmn"),
            "ethereum": ("eth_usd", "eth_tmn"),
            "tether": ("usdt_usd", "usdt_tmn"),
            "binancecoin": ("bnb_usd", "bnb_tmn"),
            "solana": ("sol_usd", "sol_tmn"),
            "ripple": ("xrp_usd", "xrp_tmn"),
            "usd-coin": ("usdc_usd", "usdc_tmn"),
            "dogecoin": ("doge_usd", "doge_tmn"),
            "cardano": ("ada_usd", "ada_tmn"),
            "tron": ("trx_usd", "trx_tmn"),
            "avalanche-2": ("avax_usd", "avax_tmn"),
            "shiba-inu": ("shib_usd", "shib_tmn"),
            "polkadot": ("dot_usd", "dot_tmn"),
            "chainlink": ("link_usd", "link_tmn"),
            "litecoin": ("ltc_usd", "ltc_tmn"),
        }
        try:
            cg = self.scraper.get(f"https://api.coingecko.com/api/v3/simple/price?ids={cg_ids}&vs_currencies=usd", timeout=12)
            if cg.status_code == 200:
                j = cg.json()
                for cid, (k_usd, k_tmn) in cg_map.items():
                    v = j.get(cid, {}).get("usd")
                    if v is not None:
                        try:
                            fv = float(v)
                            # format
                            if fv >= 1000:
                                data[k_usd] = f"{fv:,.0f}"
                            elif fv >= 1:
                                data[k_usd] = f"{fv:,.2f}"
                            elif fv >= 0.01:
                                data[k_usd] = f"{fv:,.4f}"
                            else:
                                data[k_usd] = f"{fv:.6f}".rstrip("0").rstrip(".")
                            if usd_tmn_raw and cid not in ("tether", "usd-coin"):
                                data[k_tmn] = f"{int(fv * usd_tmn_raw):,}"
                            elif usd_tmn_raw:
                                # stablecoins
                                data[k_tmn] = f"{usd_tmn_raw:,}"
                        except Exception:
                            pass
        except Exception:
            pass

        # ---- 3) oils: Brent + WTI (+ OPEC/Dubai fallback to Brent) ----
        def _fetch_oil(url, key_usd, key_tmn):
            try:
                resp = self.scraper.get(url, timeout=10)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, 'lxml')
                    el = soup.select_one(".last_price")
                    if el:
                        txt = el.get_text().strip().replace("$","").replace(",","")
                        try:
                            val = float(txt)
                            data[key_usd] = f"{val:.2f}"
                            data["oil"] = f"{val:.2f}"
                            if usd_tmn_raw:
                                data[key_tmn] = f"{int(val * usd_tmn_raw):,}"
                            return True
                        except Exception:
                            data[key_usd] = txt
            except Exception:
                pass
            return False

        _fetch_oil("https://oilprice.com/oil-price-charts/46", "brent_usd", "brent_tmn")
        _fetch_oil("https://oilprice.com/oil-price-charts/45", "wti_usd", "wti_tmn")
        # OPEC basket & Dubai: try same endpoints with fallback copy
        if not _fetch_oil("https://oilprice.com/oil-price-charts/1", "opec_usd", "opec_tmn"):
            if data["brent_usd"] != "—":
                data["opec_usd"] = data["brent_usd"]
                data["opec_tmn"] = data["brent_tmn"]
        if not _fetch_oil("https://oilprice.com/oil-price-charts/47", "dubai_usd", "dubai_tmn"):
            if data["brent_usd"] != "—":
                data["dubai_usd"] = data["brent_usd"]
                data["dubai_tmn"] = data["brent_tmn"]

        data["updated"] = time.strftime("%H:%M")
        try:
            from datetime import datetime, timezone as _tz
            data["updated_iso"] = datetime.now(_tz.utc).isoformat()
        except Exception:
            pass
        return data

    # ───────────────────────── news search ─────────────────────────

    def fetch_gnews(self):
        results = []
        try:
            results = self.gnews_en.get_news(CONFIG['SEARCH_QUERY']) or []
        except Exception as e:
            logger.error(f"GNews Error: {e}")
        return results

    def fetch_duckduckgo(self, query, region='wt-wt', max_results=8):
        results = []
        try:
            ddgs = DDGS()
            ddg_gen = ddgs.news(
                query=query, region=region, safesearch="off",
                timelimit="d", max_results=max_results
            )
            for r in ddg_gen:
                results.append({
                    'title': r.get('title'),
                    'url': r.get('url'),
                    'publisher': {'title': r.get('source')},
                    'published date': r.get('date'),
                    'description': r.get('body'),
                    'image': r.get('image')
                })
        except Exception as e:
            logger.warning(f"DDG blocked/failed ({query[:30]}), falling back to Bing RSS: {e}")
            # Fallback to Bing RSS when DuckDuckGo fails (403 on GitHub Actions)
            return self.fetch_bing_rss(query)
        
        return results

    def fetch_bing_rss(self, query):
        results = []
        try:
            encoded_query = quote(query)
            url = f"https://www.bing.com/news/search?q={encoded_query}&format=rss"
            feed = feedparser.parse(url)
            for entry in feed.entries:
                publisher = "Bing News"
                if hasattr(entry, 'news_source'):
                    publisher = entry.news_source
                elif hasattr(entry, 'source') and hasattr(entry.source, 'title'):
                    publisher = entry.source.title

                final_link = entry.link
                if "apiclick.aspx" in final_link:
                    match = re.search(r'[?&]url=([^&]+)', final_link)
                    if match:
                        final_link = unquote(match.group(1))

                image_url = None
                try:
                    if hasattr(entry, 'news_image'):
                        raw_url = entry.news_image
                        image_url = (
                            raw_url.replace('{0}', '700').replace('{1}', '400')
                            if '{0}' in raw_url else raw_url
                        )
                except Exception:
                    pass

                results.append({
                    'title': entry.title,
                    'url': final_link,
                    'publisher': {'title': publisher},
                    'published date': entry.published,
                    'description': entry.summary if hasattr(entry, 'summary') else entry.title,
                    'image': image_url
                })
        except Exception as e:
            logger.error(f"Bing RSS Error: {e}")
        return results

    def fetch_manual_url(self, url):
        try:
            resp = self.scraper.get(url, timeout=15)
            soup = BeautifulSoup(resp.text, 'lxml')
            title = soup.title.string if soup.title else "Unknown Title"
            og_title = soup.find("meta", property="og:title")
            if og_title:
                title = og_title.get("content")
            publisher = "Manual Source"
            og_site = soup.find("meta", property="og:site_name")
            if og_site:
                publisher = og_site.get("content")
            image = None
            og_image = soup.find("meta", property="og:image")
            if og_image:
                image = og_image.get("content")
            return [{
                'title': title,
                'url': url,
                'publisher': {'title': publisher},
                'published date': datetime.now(timezone.utc).isoformat(),
                'description': "Manual Submission",
                'image': image
            }]
        except Exception as e:
            logger.warning(f"Manual Fetch Error: {e} — attempting single-candidate fallback.")
            # Fallback: build a minimal candidate from the URL alone so the rest
            # of the pipeline (scrape_article_data → AI → Telegram) still runs.
            return [{
                'title': url.rstrip('/').split('/')[-1] or 'Manual Article',
                'url': url,
                'publisher': {'title': 'Manual Source'},
                'published date': datetime.now(timezone.utc).isoformat(),
                'description': f'Manual submission: {url}',
                'image': None,
            }]

    def get_combined_news(self):
        all_entries = []
        futs = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            # 1. Main News Queries
            futs.append(ex.submit(self.fetch_gnews))
            futs.append(ex.submit(self.fetch_bing_rss, CONFIG['SEARCH_QUERY']))
            
            # 2. Iterate through all specialized queries including key figures
            for q in CONFIG.get('SEARCH_QUERIES', []):
                futs.append(ex.submit(self.fetch_duckduckgo, q, 'wt-wt', 6))
            
            # 3. Dedicated Figure Site Searches
            figure_sites = [
                'site:independentpersian.com شاهزاده رضا پهلوی OR ترامپ OR نتانیاهو',
                'site:iranintl.com ترامپ OR نتانیاهو OR پهلوی OR سپاه',
                'site:radiofarda.com رضا پهلوی OR ترامپ OR نتانیاهو'
            ]
            for f_q in figure_sites:
                futs.append(ex.submit(self.fetch_duckduckgo, f_q, 'wt-wt', 4))

            for fut in concurrent.futures.as_completed(futs):
                try:
                    batch = fut.result() or []
                    all_entries.extend(batch)
                except Exception as e:
                    logger.warning(f"Search worker failed: {e}")

        logger.info(f"Raw search hits (including key figures): {len(all_entries)}")
        return all_entries

    # ───────────────────────── URL resolve ─────────────────────────

    def _resolve_final_url(self, url, raw_title=None):
        if not url:
            return None
        if "news.google.com" not in url:
            return url

        # Attempt 1: Decode base64 Google News URL (works for older CB... format)
        try:
            match = re.search(r'articles/([^?&]+)', url)
            if match:
                encoded = match.group(1)
                padded = encoded + '=' * (-len(encoded) % 4)
                import base64
                decoded_bytes = base64.urlsafe_b64decode(padded.encode('ascii'))
                urls_found = re.findall(rb'https?://[a-zA-Z0-9.\-_~:/?#\[\]@!$&\'()*+,;=%]+', decoded_bytes)
                for u in urls_found:
                    u_str = u.decode('utf-8', errors='ignore')
                    if "google.com" not in u_str:
                        return u_str
        except Exception:
            pass

        # Attempt 2: Follow redirect with scraper (handles newer format where base64 decode fails)
        # Use a browser-like User-Agent already set on self.scraper; try twice with different timeout
        for timeout in (8, 15):
            try:
                resp = self.scraper.get(url, allow_redirects=True, timeout=timeout)
                if resp.status_code == 200 and "news.google.com" not in resp.url:
                    # Also try to extract <meta http-equiv="refresh"> or JS redirect from body
                    if resp.url == url and "news.google.com" in resp.text[:2000]:
                        m = re.search(r'\"(https?://[^\"]+)\"', resp.text)
                        if m and "google.com" not in m.group(1):
                            return m.group(1)
                    return resp.url
            except Exception as e:
                logger.warning(f"Failed to resolve Google URL {url} (timeout {timeout}): {e}")

        # Attempt 3: If still unresolved, return original – caller will still try to scrape it
        # but log that resolution failed so it is visible in Actions logs
        logger.warning(f"Could not resolve Google News URL, using as-is (may fail to scrape): {url[:120]}")
        return url

    # ───────────────────────── content grab ─────────────────────────

    def scrape_article_data(self, final_url, fallback_snippet, raw_image=None):
        if not final_url or final_url.lower().endswith('.pdf'):
            return fallback_snippet, self._get_fallback_image(fallback_snippet)

        host = urlparse(final_url).netloc.lower()
        if host in self.failed_hosts:
            return fallback_snippet, self._pick_image(raw_image, fallback_text=fallback_snippet)

        extracted_text = fallback_snippet
        extracted_image = raw_image if self._is_valid_image_url(raw_image) else None
        max_chars = CONFIG.get('MAX_TEXT_CHARS', 1800)

        try:
            downloaded = trafilatura.fetch_url(final_url)
            if downloaded:
                text = trafilatura.extract(
                    downloaded,
                    include_comments=False,
                    include_tables=False,
                    favor_precision=True,
                )
                if text and len(text.strip()) > CONFIG.get('MIN_TEXT_LEN', 100):
                    extracted_text = re.sub(r'\s+', ' ', text).strip()[:max_chars]
                try:
                    meta = trafilatura.extract_metadata(downloaded)
                    if meta and getattr(meta, 'image', None) and self._is_valid_image_url(meta.image):
                        extracted_image = extracted_image or meta.image
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"trafilatura failed {final_url}: {e}")
            self.failed_hosts.add(host)

        need_soup = (
            not extracted_image
            or extracted_text == fallback_snippet
            or len(extracted_text) < CONFIG.get('MIN_TEXT_LEN', 100)
        )
        if need_soup:
            try:
                resp = self.scraper.get(final_url, timeout=CONFIG['TIMEOUT'])
                soup = BeautifulSoup(resp.text, 'lxml')

                if extracted_text == fallback_snippet or len(extracted_text) < CONFIG.get('MIN_TEXT_LEN', 100):
                    for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe']):
                        tag.decompose()
                    paras = [
                        p.get_text(strip=True)
                        for p in soup.find_all('p')
                        if len(p.get_text(strip=True)) > 40
                    ]
                    clean = ' '.join(paras[:12])
                    if len(clean) > CONFIG.get('MIN_TEXT_LEN', 100):
                        extracted_text = clean[:max_chars]

                if not extracted_image:
                    for prop in (
                        ('property', 'og:image'),
                        ('property', 'og:image:secure_url'),
                        ('name', 'twitter:image'),
                        ('name', 'twitter:image:src'),
                        ('itemprop', 'image'),
                    ):
                        tag = soup.find('meta', attrs={prop[0]: prop[1]})
                        if tag and tag.get('content') and self._is_valid_image_url(tag['content']):
                            extracted_image = tag['content'].strip()
                            break

                    if not extracted_image:
                        for img in soup.find_all('img', src=True):
                            src = img.get('src') or ''
                            if src.startswith('//'):
                                src = 'https:' + src
                            if not src.startswith('http'):
                                continue
                            if not self._is_valid_image_url(src):
                                continue
                            w = img.get('width') or img.get('data-width') or ''
                            h = img.get('height') or img.get('data-height') or ''
                            try:
                                if w and int(str(w).replace('px', '')) < 120:
                                    continue
                                if h and int(str(h).replace('px', '')) < 80:
                                    continue
                            except Exception:
                                pass
                            extracted_image = src
                            break
            except Exception as e:
                logger.warning(f"Soup fallback failed {final_url}: {e}")
                self.failed_hosts.add(host)

        extracted_image = self._pick_image(
            extracted_image,
            raw_image,
            fallback_text=extracted_text or fallback_snippet
        )
        return extracted_text, extracted_image

    # ───────────────────────── AI analysis ─────────────────────────

    def _autodetect_ai_model(self):
        """
        Auto-discovers a usable model from the provider's /models endpoint,
        so nobody has to hunt down and paste an exact model id by hand.
        Caches the result on CONFIG so we only do this once per run.
        """
        try:
            models_url = re.sub(r'/chat/completions/?$', '/models', CONFIG['AI_BASE_URL'])
            headers = {"Authorization": f"Bearer {CONFIG['AI_API_KEY']}"}
            resp = self.scraper.get(models_url, headers=headers, timeout=15)
            if resp.status_code != 200:
                logger.error(f"Could not list models ({resp.status_code}): {resp.text[:200]}")
                return None

            data = resp.json().get('data', [])
            ids = [m.get('id', '') for m in data if m.get('id')]

            # Skip anything that clearly isn't a general chat/text model
            skip_words = ['whisper', 'tts', 'guard', 'moderation', 'embed', 'vision', 'image', 'audio']
            candidates = [i for i in ids if not any(w in i.lower() for w in skip_words)]

            # Prefer common, capable chat models if present
            preferred_order = ['llama-3.1-8b-instant', 'llama-3.3-70b-versatile', 'gpt-oss-120b', 'gpt-oss-20b']
            for pref in preferred_order:
                match = next((c for c in candidates if pref in c), None)
                if match:
                    logger.info(f"Auto-detected AI model: {match}")
                    return match

            if candidates:
                logger.info(f"Auto-detected AI model (first available): {candidates[0]}")
                return candidates[0]
        except Exception as e:
            logger.error(f"Model auto-detect failed: {e}")
        return None

    def _call_ai(self, system_prompt, user_prompt, temperature=0.2):
        """
        Calls an OpenAI-compatible chat-completions endpoint.
        Provider-agnostic on purpose - controlled entirely by 3 env vars:
          AI_API_KEY   - the provider's API key
          AI_BASE_URL  - full chat/completions URL (defaults to Groq)
          AI_MODEL     - model name for that provider (defaults to a Groq model,
                         and is auto-corrected below if that default ever goes stale)
        This means switching providers (Groq, DeepSeek, Zhipu/GLM, OpenRouter,
        Together, etc.) never requires touching this code again - just change
        the env vars.
        """
        if not CONFIG.get('AI_API_KEY'):
            logger.error("AI_API_KEY is not set.")
            return None

        url = CONFIG['AI_BASE_URL']
        headers = {
            "Authorization": f"Bearer {CONFIG['AI_API_KEY']}",
            "Content-Type": "application/json",
        }

        def build_payload(model):
            return {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": temperature,
                "response_format": {"type": "json_object"},
            }

        model_retried = False

        for attempt in range(CONFIG['AI_RETRIES']):
            try:
                payload = build_payload(CONFIG['AI_MODEL'])
                resp = self.scraper.post(
                    url, headers=headers, json=payload, timeout=CONFIG.get('AI_TIMEOUT', 45)
                )
                if resp.status_code == 200:
                    result = resp.json()
                    raw_text = result['choices'][0]['message']['content']
                    clean = re.sub(r'```json\s*|```', '', raw_text).strip()
                    return json.loads(clean)

                # If the model name itself is invalid/stale, auto-detect a working one
                # and permanently switch to it for the rest of this run (only tried once).
                is_model_error = resp.status_code in (404, 400) and (
                    'model_not_found' in resp.text or 'does not exist' in resp.text
                )
                if is_model_error and not model_retried:
                    model_retried = True
                    new_model = self._autodetect_ai_model()
                    if new_model:
                        CONFIG['AI_MODEL'] = new_model
                        continue  # retry immediately with the corrected model

                logger.error(f"AI API error {resp.status_code}: {resp.text[:200]}")
                time.sleep(1)
            except Exception as e:
                logger.error(f"AI Attempt {attempt + 1} failed: {e}")
                time.sleep(2)
        return None

    def batch_analyze_with_ai(self, candidates_data):
        """
        Analyzes multiple candidate news articles in a SINGLE AI API request.
        candidates_data format: list of dicts with {'index', 'source', 'headline', 'text'}
        """
        if not candidates_data or not CONFIG.get('AI_API_KEY'):
            return {}

        system_prompt = (
            "تو یک تحلیل‌گر ارشد و تیزبین ژئوپلیتیک، مسلط به ادبیات کانال‌های تحلیلی تلگرام فارسی (مانند تحلیل‌گران مستقل و اپوزیسیون ایرانی) هستی.\n"
            "وظیفه تو تبدیل اخبار خام و سخنرانی‌های چهره‌های کلیدی به تحلیل‌های کوتاه، ضربتی، کاملاً انسانی، به فارسی روان است.\n\n"
            "🎯 **دستورالعمل ویژه پوشش سخنرانی‌ها و مواضع چهره‌های اصلی (مهم):**\n"
            " - **دونالد ترامپ:** موضع وی در قبال ایران، تحریم‌ها، فشار حداکثری یا اقدامات نظامی را شفاف، صریح و بدون سانسور ترجمه و تحلیل کن.\n"
            " - **بنیامین نتانیاهو:** هشدارها، طرح‌های ضربتی علیه تاسیسات هسته‌ای یا سپاه را مستقیماً پوشش بده.\n"
            " - **شاهزاده رضا پهلوی (اپوزیسیون):** فراخوان‌ها، پیام‌ها به ملت ایران، و طرح‌های گذار از جمهوری اسلامی را با لحن محترمانه، روان و پوشش کامل خبری منعکس کن.\n"
            " - **فرماندهان سپاه (سلامی، قاآنی و...):** ادعاها و تهدیدهای آنان را افشا کرده و واقعیت پشت خط کلامی (جنگ روانی) را تحلیل کن.\n\n"
            "🔴 **قانون حیاتی حذف اخبار تکراری و هم‌پوشان (Deduplication):**\n"
            "- اگر چند خبر به یک رویداد واحد پرداخته‌اند (مثلاً چند خبرگزاری مختلف هشدار ترامپ به چین درباره نفت ایران را مخابره کرده‌اند)، "
            "فقط و فقط یک مورد (کامل‌ترین منبع) را در خروجی بیاور و بقیه ایندکس‌های تکراری را از خروجی JSON حذف کن (آرایه فقط شامل آیتم‌های کاملاً مجزا و غیرتکراری باشد).\n\n"
            "🔴 قوانین حیاتی نگارش و انسانی‌سازی (مهم - حتماً رعایت شود):\n"
            "۱. **روانی، شفافیت و سادگی زبان (مهم):**\n"
            " - از کلمات قلم‌به‌سلم، پیچیده و عجیب دانشگاهی (مثل: 'گره مشخص'، 'تعمیم روایت'، 'شکل‌گیری محاسبات') مطلقاً استفاده نکن.\n"
            " - **ممنوعیت ترجمه تحت‌اللفظی:** عبارات انگلیسی را کلمه به کلمه ترجمه نکن (مثلاً اصطلاح 'drone threat' را 'تهدید پهپادی' بنویس، نه 'تهدید پرنده'!).\n"
            " - جملات باید بسیار روان، صریح و شفاف باشند تا مخاطب با یک‌بار خواندن متوجه اصل ماجرا شود.\n\n"
            "۲. **ممنوعیت مطلق عبارت‌های کلیشه‌ای رباتیک:**\n"
            " استفاده از این عبارات مطلقاً ممنوع است: ('به نظر می‌رسد'، 'نشان‌دهنده این است که'، 'لازم به ذکر است'، 'در نهایت'، 'پیامدهای عمیق'، 'ابعاد جدیدی از'، 'در این راستا'، 'شایان ذکر است').\n\n"
            "۳. **تنوع در ساختار جملات:**\n"
            " جملات نباید همه با یک فرمول شروع شوند. گاهی با یک فعل حاد، گاهی با یک آمار، و گاهی با یک ارزیابی مستقیم شروع کن.\n\n"
            "۴. **تعداد نقطه‌نظرات شناور:**\n"
            " بخش summary می‌تواند بین ۲ تا ۴ مورد باشد. اگر خبر کوتاه است ۲ نکته عمیق و روان کافیست، برای خبرهای مهم ۴ نکته بنویس. خودت را به ۳ نقطه اجباری محدود نکن.\n\n"
            "۵. **حذف القاب رسمی و حاکمیتی:**\n"
            " از القاب مانند (آیت‌الله، سردار، شهید، حجت‌الاسلام، عمومی) استفاده نکن. فقط نام و سمت رسمی.\n\n"
            "۶. **تغییر لحن بر اساس اهمیت (Urgency):**\n"
            " - اگر خبر نظامی/فوریت بالاست (۸ تا ۱۰): لحن ضربتی، کوتاه و صریح باشد.\n"
            " - اگر خبر اقتصادی/سیاسی است (۴ تا ۷): لحن تحلیلی و افشاگرانه باشد.\n\n"
            "۷. **دقت املایی و نگارشی (بسیار مهم - غلط ممنوع):**\n"
            " - تمام خروجی باید بدون حتی یک غلط املایی و نگارشی باشد. املای فارسی را کاملاً رعایت کن.\n"
            " - نیم‌فاصله را درست به کار ببر: 'می‌رود' نه 'می رود'، 'تحریم‌ها' نه 'تحریم ها'، 'هسته‌ای' نه 'هسته ای'.\n"
            " - از حرف 'ی' و 'ک' فارسی استفاده کن نه عربی (ی/ك عربی ممنوع).\n"
            " - اعداد، تاریخ و اسامی خاص را دقیق بنویس و از حدس املایی پرهیز کن.\n\n"
            "قواعد امتیازبندی فوریت (Urgency Score 1-10):\n"
            "- 9-10: درگیری مستقیم نظامی، کشته شدن مقامات ارشد، ضربه به تاسیسات اتمی/نظامی.\n"
            "- 7-8: تحریم‌های خفه کننده جدید، سقوط شدید ارزی، اعتراضات سراسری، حملات نیابتی سنگین.\n"
            "- 4-6: تحرکات دیپلماتیک مهم، تنش‌های لفظی مسئولان، مانورهای منطقه‌ای.\n"
            "- 1-3: اظهارات routine، دیدارهای تشریفاتی.\n\n"
            "تو فهرستی از آیتم‌های خبری با شناسه index دریافت می‌کنی. خروجی باید یک JSON object معتبر با یک کلید \"items\" باشد که مقدارش آرایه‌ای از تحلیل تک تک این آیتم‌ها با ساختار زیر است:\n"
            "{\n"
            '  "items": [\n'
            "    {\n"
            '      "index": 0,\n'
            '      "title_fa": "تیتر جذاب، روان، غیرتکراری و بدون کلمات خنثی (حداکثر ۱۰ کلمه)",\n'
            '      "summary": ["نکته تحلیلی ۱ به فارسی روان و بدون کلمات اضافه", "نکته تحلیلی ۲ با تمرکز بر واقعیت پشت خبر"],\n'
            '      "impact": "تأثیر عملیاتی یا اقتصادی خبر در یک جمله کوتاه، روان و ضربتی",\n'
            '      "tag": "کلمه کلیدی اصلی (مثلاً: نظامی، ارز، تحریم، نیابتی)",\n'
            '      "urgency": عدد بین 1 تا 10,\n'
            '      "sentiment": عدد بین -1.0 تا 1.0\n'
            "    }\n"
            "  ]\n"
            "}"
        )

        items_input = []
        for item in candidates_data:
            items_input.append(
                f"--- ITEM INDEX: {item['index']} ---\n"
                f"SOURCE: {item['source']}\n"
                f"HEADLINE: {item['headline']}\n"
                f"TEXT: {item['text'][:1000]}\n"
            )

        user_prompt = "لطفاً تمامی آیتم‌های زیر را تحلیل و در قالب JSON مشخص‌شده برگردان:\n\n" + "\n".join(items_input)

        data = self._call_ai(system_prompt, user_prompt, temperature=0.25)
        items_list = data.get('items') if isinstance(data, dict) else data
        if isinstance(items_list, list):
            # Proofread Persian orthography before returning
            return {item.get('index'): self._proofread_item(item) for item in items_list if 'index' in item}
        return {}
        
    def generate_daily_summary(self):
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        todays_items = [
            item for item in self.existing_news
            if datetime.fromtimestamp(item.get("timestamp", 0), timezone.utc) >= today_start
        ]
        if len(todays_items) < 3:
            return None
        todays_items.sort(key=lambda x: x.get("urgency", 0), reverse=True)
        news_context = []
        for item in todays_items[:20]:
            news_context.append(
                f"Title: {item.get('title_en')}\nSource: {item.get('source')}\n"
                f"Urgency: {item.get('urgency')}\nTag: {item.get('tag')}\n"
                f"Impact: {item.get('impact')}\nSummary: {' '.join(item.get('summary', []))}"
            )
        news_block = "\n\n".join(news_context)
        previous_summary = self._load_previous_daily_summary()
        previous_block = ""
        if previous_summary:
            previous_block = (
                f"Previous Strategic Assessment:\nThemes: {previous_summary.get('themes')}\n"
                f"Strategic Assessment: {previous_summary.get('strategic_assessment')}\n"
                f"Market Impact: {previous_summary.get('market_impact')}\n"
                f"Risk Level: {previous_summary.get('risk_level')}"
            )
        return self.analyze_daily_summary_with_ai(news_block, previous_block)

    def analyze_daily_summary_with_ai(self, news_block, previous_block):
        system_prompt = """
You are a senior geopolitical intelligence analyst aligned with the Iranian nationalist opposition.
This is a rolling daily strategic assessment.
You will receive:
1) All today's news events
2) The previous run's strategic summary (if available)
Your job:
- Detect evolution compared to previous assessment.
- Identify new escalation or de-escalation signals.
- Strip away regime propaganda and expose their true vulnerabilities.
- Provide highly analytical predictive intelligence based on geopolitical realities.
OUTPUT LANGUAGE: Persian (Farsi)
STRICT OUTPUT JSON:
{
  "date": "YYYY-MM-DD HH:MM",
  "executive_tldr": "1 punchy sentence summarizing the day's geopolitical reality",
  "themes": [3-5 bullet points],
  "regime_vulnerabilities": {
    "regime_internal_friction": "1 sentence exposing IRGC vs Government infighting or purges",
    "infrastructure_vulnerability": "1 sentence on energy shortages, cyber-attacks, or systemic failures",
    "sanctions_evasion_watch": "1 sentence on oil smuggling or banking evasion exposed today"
  },
  "proxy_network_status": "1 sentence analyzing the health/actions of regional proxies (Hezbollah, Houthis, etc.)",
  "opposition_momentum": "1 sentence on diaspora actions, internal strikes, or civil disobedience",
  "regime_narrative": "1 concise sentence explaining what propaganda state media is pushing today",
  "predicted_regime_response": "1 sentence predicting their next move (e.g., proxy attack, internal crackdown, diplomatic deception)",
  "forecast": {
    "most_likely_scenario": "1 paragraph predicting the realistic outcome over the next 3-7 days",
    "regime_worst_case_scenario": "1 paragraph detailing the specific events that could fracture regime stability this week",
    "flashpoint_indicator": "The specific trigger event/red line that signals immediate severe escalation"
  },
  "probability_matrix": {
    "military_escalation_percent": "integer (0-100)",
    "economic_shock_percent": "integer (0-100)",
    "domestic_unrest_percent": "integer (0-100)",
    "regime_defection_risk_percent": "integer (0-100)"
  },
  "key_figures_in_focus": ["Name 1 - Reason", "Name 2 - Reason"],
  "strategic_assessment": "1-2 paragraphs of hardline, realistic geopolitical analysis",
  "market_impact": "1 paragraph on economic vulnerabilities and sanctions impact",
  "currency_outlook": "جهش دلار | نوسان بالا | ثبات شکننده",
  "risk_level": "integer (1-10)",
  "change_from_previous": "افزایش | کاهش | بدون تغییر"
}
"""
        user_prompt = f"TODAY NEWS:\n{news_block}\n\nPREVIOUS SUMMARY:\n{previous_block}"
        result = self._call_ai(system_prompt, user_prompt, temperature=0.2)
        # Proofread Persian fields
        if isinstance(result, dict):
            return self._proofread_item(result)
        return result

    # ───────────────────────── process item ─────────────────────────
    def send_special_report_to_telegram(self, report):
        """Format and send Special Topic Report to Telegram nightly (uses standard sendMessage)."""
        token = CONFIG['TELEGRAM']['BOT_TOKEN']
        chat_id = CONFIG['TELEGRAM']['CHANNEL_ID']
        if not token or not chat_id:
            logger.error("TG_BOT_TOKEN or TG_CHANNEL_ID is not set – cannot send Special Report. Set them in GitHub Secrets.")
            return False
        if not report:
            logger.warning("Special Report is empty. Skipping TG dispatch.")
            return False

        def esc(s):
            return html.escape(str(s or ''), quote=False)

        tehran_now = self._get_tehran_time()
        time_str = tehran_now.strftime("%H:%M")
        date_str = tehran_now.strftime("%Y/%m/%d")

        base_site = CONFIG['BASE_SITE_URL']
        tag = esc(report.get('topic_tag', 'پرونده ویژه')).replace(' ', '_')
        headline = esc(report.get('headline', 'گزارش ویژه'))
        lead = esc(report.get('lead_paragraph', ''))
        regime_vs_reality = esc(report.get('regime_vs_reality', ''))
        strategic_outlook = esc(report.get('strategic_outlook', ''))

        inline_keyboard = {
            "inline_keyboard": [[
                {"text": "📊 مطالعه پرونده در داشبورد", "url": base_site}
            ]]
        }

        findings_text = "".join([f"🔹 {esc(f)}\n" for f in report.get('key_findings', [])])
        text = (
            f"📂 <b>پرونده ویژه شبانگاهی: {headline}</b>\n"
            f"⏱ <b>زمان:</b> {time_str} — {date_str} | 🏷 #{tag}\n\n"
            f"📌 <b>اصل ماجرا:</b>\n{lead}\n\n"
            f"🔍 <b>یافته‌های کلیدی:</b>\n{findings_text}\n"
            f"⚔️ <b>واقعیت میدانی:</b>\n{regime_vs_reality}\n\n"
            f"🔮 <b>چشم‌انداز:</b>\n{strategic_outlook}\n\n"
            f"📊 <a href=\"{base_site}\">مشاهده کامل در داشبورد زنده</a> | 🆔 <a href=\"https://t.me/Enqelab_e_Iran\">@Enqelab_e_Iran</a>\n\nانقلاب | Shir o Khorshid 🦁🔆"
        )

        standard_api = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            resp = self.scraper.post(standard_api, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": inline_keyboard
            }, timeout=30)
            if resp.status_code != 200:
                logger.error(f"Special Report sendMessage failed {resp.status_code}: {resp.text[:500]}")
                return False
            logger.info(">>> Special Topic Report sent to Telegram.")
            return True
        except Exception as e:
            logger.error(f"Special Report send error: {e}")
            return False

    def send_daily_summary_to_telegram(self, summary):
        """Format and send Daily Summary using standard Telegram HTML (sendMessage)."""
        token = CONFIG['TELEGRAM']['BOT_TOKEN']
        chat_id = CONFIG['TELEGRAM']['CHANNEL_ID']
        if not token or not chat_id:
            logger.error("TG_BOT_TOKEN or TG_CHANNEL_ID is not set – cannot send Daily Summary.")
            return False
        if not summary:
            logger.warning("Daily Summary is empty. Skipping TG dispatch.")
            return False

        def esc(s):
            return html.escape(str(s or ''), quote=False)

        tehran_now = self._get_tehran_time()
        time_str = tehran_now.strftime("%H:%M")
        date_str = tehran_now.strftime("%Y/%m/%d")

        base_site = CONFIG['BASE_SITE_URL']
        forecast = summary.get('forecast', {})
        most_likely = esc(forecast.get('most_likely_scenario', ''))
        vulns = summary.get('regime_vulnerabilities', {})
        vuln_text = esc(vulns.get('regime_internal_friction') or vulns.get('infrastructure_vulnerability') or '')

        inline_keyboard = {
            "inline_keyboard": [[
                {"text": "📊 بولتن و داشبورد زنده", "url": base_site}
            ]]
        }

        text = (
            f"📊 <b>ارزیابی استراتژیک و جمع‌بندی روزانه</b>\n"
            f"⏱ <b>زمان:</b> {time_str} — {date_str} (تهران)\n\n"
            f"📌 <b>چکیده مدیریتی:</b>\n{esc(summary.get('executive_tldr'))}\n\n"
            f"🧠 <b>تحلیل استراتژیک:</b>\n{esc(summary.get('strategic_assessment'))}\n\n"
            f"🔮 <b>پیش‌بینی سناریو:</b>\n{most_likely}\n\n"
            f"📈 <b>سطح ریسک:</b> <b>{summary.get('risk_level', '?')}/10</b>\n\n"
            f"🔗 <a href=\"{base_site}\">مشاهده کامل در داشبورد زنده</a> | 🆔 <a href=\"https://t.me/Enqelab_e_Iran\">@Enqelab_e_Iran</a>\n\nانقلاب | Shir o Khorshid 🦁🔆"
        )

        standard_api = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            resp = self.scraper.post(standard_api, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": inline_keyboard
            }, timeout=30)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Daily Summary standard fallback error: {e}")
            return False

    def send_bulletin_to_telegram(self, bulletin):
        """Format and send Scheduled Bulletin using standard Telegram HTML (sendMessage)."""
        token = CONFIG['TELEGRAM']['BOT_TOKEN']
        chat_id = CONFIG['TELEGRAM']['CHANNEL_ID']
        if not token or not chat_id:
            logger.error("TG_BOT_TOKEN or TG_CHANNEL_ID is not set – cannot send Bulletin.")
            return False
        if not bulletin:
            logger.warning("Bulletin is empty. Skipping TG dispatch.")
            return False

        def esc(s):
            return html.escape(str(s or ''), quote=False)

        title = esc(bulletin.get('title', 'بولتن خبری'))
        date_str = esc(bulletin.get('date', ''))
        time_str = esc(bulletin.get('time', '23:00'))
        base_site = CONFIG['BASE_SITE_URL']
        bottom_line = esc(bulletin.get('bottom_line', ''))

        inline_keyboard = {
            "inline_keyboard": [[
                {"text": "📊 مطالعه بولتن در داشبورد", "url": base_site}
            ]]
        }

        bullets_text = "".join([f"🔹 {esc(b)}\n\n" for b in bulletin.get('bullets', [])])
        text = (
            f"🗞 <b>{title}</b>\n"
            f"⏱ <b>زمان:</b> {time_str} — {date_str} (تهران)\n"
            f"───────────────────\n\n"
            f"{bullets_text}"
            f"💡 <b>جمع‌بندی نهایی:</b>\n{bottom_line}\n\n"
            f"📊 <a href=\"{base_site}\">مشاهده جزییات بیشتر در داشبورد</a> | 🆔 <a href=\"https://t.me/Enqelab_e_Iran\">@Enqelab_e_Iran</a>\n\nانقلاب | Shir o Khorshid 🦁🔆"
        )

        standard_api = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            resp = self.scraper.post(standard_api, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": inline_keyboard
            }, timeout=30)
            if resp.status_code != 200:
                logger.error(f"Bulletin sendMessage failed {resp.status_code}: {resp.text[:500]}")
                return False
            logger.info(">>> Bulletin sent to Telegram.")
            return True
        except Exception as e:
            logger.error(f"Bulletin send error: {e}")
            return False

    def send_digest_to_telegram(self, items):
            """Send each news item to Telegram as a simple post:
            - Title
            - Source
            - Signature: انقلاب | Shir o Khorshid 🦁🔆
            """
            token = CONFIG['TELEGRAM']['BOT_TOKEN']
            chat_id = CONFIG['TELEGRAM']['CHANNEL_ID']
            if not token or not chat_id:
                logger.error("TG_BOT_TOKEN or TG_CHANNEL_ID is not set – cannot send news digest. Set them in GitHub Secrets.")
                return
            if not items:
                return

            items.sort(key=lambda x: x.get('urgency', 3), reverse=True)

            def esc(s):
                return html.escape(str(s or ''), quote=False)

            send_message_api = f"https://api.telegram.org/bot{token}/sendMessage"

            for item in items:
                title = esc(item.get('title_fa') or item.get('title_en'))
                source = esc(item.get('source', 'نامشخص'))

                # Simple format: Title + Source + Signature
                caption = f"{title}\n\n📰 منبع: {source}\n\n---\nانقلاب | Shir o Khorshid 🦁🔆"

                payload = {
                    "chat_id": chat_id,
                    "text": caption,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }

                try:
                    resp = self.scraper.post(send_message_api, json=payload, timeout=20)
                    if resp.status_code == 200:
                        logger.info(f"Posted to Telegram: {title[:60]}")
                    else:
                        logger.error(f"sendMessage failed: {resp.status_code} | {resp.text[:300]}")
                except Exception as e:
                    logger.error(f"TG send error for item {item.get('id')}: {e}")

                # Small delay between posts to avoid rate limits
                time.sleep(2)

    # ───────────────────────── save ─────────────────────────

    def _atomic_json_dump(self, file_path, data):
        dir_name = os.path.dirname(file_path) or '.'
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile('w', dir=dir_name, delete=False, encoding='utf-8') as tf:
                json.dump(data, tf, indent=4, ensure_ascii=False)
                temp_name = tf.name
            os.replace(temp_name, file_path)
        except Exception as e:
            logger.error(f"Atomic dump failed for {file_path}: {e}")
            if temp_name and os.path.exists(temp_name):
                os.remove(temp_name)

    def save_news(self, new_items):
        try:
            all_news = new_items + self.existing_news
            seen_u = set()
            unique_news = []
            for item in all_news:
                u = self._clean_url(item.get('url'))
                if u and u not in seen_u:
                    seen_u.add(u)
                    item['image'] = self._pick_image(
                        item.get('image'),
                        fallback_text=item.get('title_en') or item.get('title_fa') or ''
                    )
                    unique_news.append(item)
            unique_news.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
            final_list = unique_news[:CONFIG['HISTORY_SIZE']]
            self._atomic_json_dump(CONFIG['FILES']['NEWS'], final_list)
            logger.info(">>> news.json updated successfully.")
            return final_list
        except Exception as e:
            logger.error(f"Save Failed: {e}")
            return self.existing_news

    def save_daily_summary(self, summary):
        if not summary:
            return
        try:
            self._atomic_json_dump(CONFIG['FILES']['DAILY_SUMMARY'], summary)
            logger.info(">>> daily_summary.json updated successfully.")
        except Exception as e:
            logger.error(f"Failed to save daily summary: {e}")

    def generate_scheduled_bulletin(self):
        tehran_time = self._get_tehran_time()
        hour = tehran_time.hour
        if 6 <= hour < 12:
            edition_key, edition_title = "morning", "بولتـن صبحگاهی"
        elif 12 <= hour < 18:
            edition_key, edition_title = "midday", "بولتـن نیمروزی"
        else:
            edition_key, edition_title = "evening", "بولتـن شبانگاهی (جمع‌بندی روز)"

        top_items = sorted(self.existing_news, key=lambda x: x.get('urgency', 0), reverse=True)[:5]
        if not top_items:
            return None

        news_text = "\n".join([
            f"- {item.get('title_fa')}: {' '.join(item.get('summary', []))}"
            for item in top_items
        ])
        system_prompt = f"""
تو سردبیر ارشد بخش اخبار فوری هستی. برای "{edition_title}" یک خلاصه خبر ۳ دقیقه‌ای روان، ضربتی و بسیار جذاب به فارسی بنویس.
⚠️ تمام خروجی باید بدون حتی یک غلط املایی/نگارشی باشد — نیم‌فاصله را درست رعایت کن (می‌رود، تحریم‌ها، هسته‌ای) و از ی/ک فارسی استفاده کن.
خروجی باید JSON زیر باشد:
{{
  "edition": "{edition_key}",
  "title": "{edition_title}",
  "time": "{tehran_time.strftime('%H:%M')}",
  "date": "{tehran_time.strftime('%Y/%m/%d')}",
  "bullets": ["نکته ۱", "نکته ۲", "نکته ۳", "نکته ۴"],
  "bottom_line": "نتیجه‌گیری در یک جمله کوتاه"
}}
"""
        data = self._call_ai(system_prompt, news_text, temperature=0.2)
        if data:
            data = self._proofread_item(data)
            self._atomic_json_dump('bulletins.json', data)
            logger.info(f">>> Scheduled Bulletin ({edition_title}) generated successfully.")
        else:
            logger.warning(f"Scheduled Bulletin ({edition_title}) could not be generated (AI returned no result — AI_API_KEY likely missing).")
        return data

    def generate_special_topic_report(self):
        if len(self.existing_news) < 5:
            return None
        tag_clusters = {}
        for item in self.existing_news[:30]:
            tag = item.get('tag', 'عمومی')
            tag_clusters.setdefault(tag, []).append(item)
        top_tag = max(tag_clusters, key=lambda k: len(tag_clusters[k]))
        cluster_items = tag_clusters[top_tag]
        if len(cluster_items) < 2:
            return None

        cluster_context = "\n---\n".join([
            f"منبع: {i.get('source')}\nتیتر: {i.get('title_fa')}\n"
            f"تحلیل: {i.get('impact')}\nخلاصه: {' '.join(i.get('summary', []))}"
            for i in cluster_items[:6]
        ])
        system_prompt = """
تو تیم تحریریه پرونده‌های ویژه خبری هستی. بر اساس گزارش‌های ورودی که همگی درباره یک موضوع پرخبر امروز هستند، یک «پرونده ویژه اختصاصی» به فارسی روان، جذاب و تحلیل‌گرایانه بنویس.
⚠️ تمام خروجی باید بدون غلط املایی/نگارشی باشد — نیم‌فاصله را درست رعایت کن (می‌رود، تحریم‌ها، هسته‌ای) و از ی/ک فارسی استفاده کن.
خروجی باید JSON زیر باشد:
{
  "topic_tag": "موضوع پرونده",
  "headline": "تیتر اصلی و جذاب پرونده ویژه",
  "lead_paragraph": "مقدمه و اصل ماجرا در دو جمله بسیار روان",
  "key_findings": [
    "یافته و زاویه دید ۱",
    "یافته و زاویه دید ۲",
    "یافته و زاویه دید ۳"
  ],
  "regime_vs_reality": "مقایسه ادعای رسانه‌های حکومتی با واقعیت میدانی در یک پاراگراف",
  "strategic_outlook": "پیش‌بینی ادامه روند این پرونده در هفته آینده"
}
"""
        data = self._call_ai(system_prompt, f"موضوع: {top_tag}\n\nگزارش‌ها:\n{cluster_context}", temperature=0.25)
        if data:
            data = self._proofread_item(data)
            self._atomic_json_dump('special_reports.json', data)
            logger.info(f">>> Special Report on ({top_tag}) generated successfully.")
        return data

    # ───────────────────────── main run ─────────────────────────

    def run(self):
        logger.info(">>> Gustavo radar started (optimized search + extract + photos)...")

        with open(CONFIG['FILES']['MARKET'], 'w', encoding='utf-8') as f:
            json.dump(self.fetch_market_rates(), f, ensure_ascii=False)

        manual_url = os.environ.get('MANUAL_URL')

        if manual_url and manual_url.strip():
            logger.info(f"!!! MANUAL MODE: {manual_url} !!!")
            results = self.fetch_manual_url(manual_url)
            candidates = results
        else:
            results = self.get_combined_news()
            candidates = []
            seen_batch_titles = set()
            cutoff_date = datetime.now(timezone.utc) - timedelta(hours=CONFIG['MAX_NEWS_AGE_HOURS'])

            # 1. First pass: filter by age, seen URLs, and exact hashes (with counters for debugging)
            cnt_age = cnt_url = cnt_title = cnt_hash = 0
            for item in results:
                try:
                    p_date = item.get('published date')
                    if p_date:
                        dt = parser.parse(p_date)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if dt < cutoff_date:
                            cnt_age += 1
                            continue
                except Exception:
                    pass

                raw_url = item.get('url', '')
                clean_u = self._clean_url(raw_url)
                if clean_u in self.seen_urls:
                    cnt_url += 1
                    continue

                t = item.get('title', '').rsplit(' - ', 1)[0].strip()
                norm_t = self._normalize_text(t)
                th = self._title_hash(t)

                if norm_t in self.seen_titles or norm_t in seen_batch_titles:
                    cnt_title += 1
                    continue
                if th in self.recent_title_hashes:
                    cnt_hash += 1
                    continue

                seen_batch_titles.add(norm_t)
                candidates.append(item)

            if cnt_age or cnt_url or cnt_title or cnt_hash:
                logger.info(f"  filtered: age={cnt_age} url_dup={cnt_url} title_dup={cnt_title} hash_dup={cnt_hash} -> kept {len(candidates)} before fuzzy")

            # 2. Sort by domain reliability first so top sources are preferred
            candidates.sort(
                key=lambda x: self._domain_score(
                    x.get('url'),
                    x.get('publisher', {}).get('title', '')
                ),
                reverse=True
            )

            # 3. Second pass: Cross-deduplicate against historical news AND within current batch
            accepted_candidates = []
            cnt_fuzzy = 0
            for item in candidates:
                raw_t = item.get('title', '').rsplit(' - ', 1)[0].strip()
                
                # Check against historical news AND candidates already accepted in this run
                if self._is_duplicate_fuzzy(raw_t, self.existing_news) or self._is_duplicate_fuzzy(raw_t, accepted_candidates):
                    cnt_fuzzy += 1
                    continue

                accepted_candidates.append(item)

            if cnt_fuzzy:
                logger.info(f"  fuzzy-dedup filtered {cnt_fuzzy} -> {len(accepted_candidates)} remain")
            candidates = accepted_candidates[:CONFIG.get('MAX_CANDIDATES', 15)]

        logger.info(
            f"Total Fetched: {len(results)} | Candidates (new/recent/capped): {len(candidates)}"
        )

        new_processed_items = []
        if candidates:
            # 1. Parallel Content Extraction
            scraped_items = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=CONFIG['MAX_WORKERS']) as exc:
                future_to_cand = {}
                for idx, cand in enumerate(candidates):
                    raw_title = cand.get('title', '').rsplit(' - ', 1)[0].strip()
                    publisher = cand.get('publisher', {}).get('title', 'Unknown')
                    final_url = self._resolve_final_url(cand.get('url'), raw_title)
                    if not final_url:
                        continue
                    clean_u = self._clean_url(final_url)
                    snippet = cand.get('description', raw_title)
                    f = exc.submit(self.scrape_article_data, final_url, snippet, cand.get('image'))
                    future_to_cand[f] = (idx, cand, raw_title, publisher, final_url, clean_u, snippet)

                for fut in concurrent.futures.as_completed(future_to_cand):
                        idx, cand, raw_title, publisher, final_url, clean_u, snippet = future_to_cand[fut]
                        try:
                            text, photo = fut.result()
                            scraped_items.append({
                                'index': idx,
                                'cand': cand,
                                'headline': raw_title,
                                'source': publisher,
                                'url': final_url,
                                'clean_url': clean_u,
                                'snippet': snippet,
                                'text': text,
                                'photo': photo
                            })
                        except Exception as e:
                            logger.error(f"Scrape worker error: {e}")

                # Fallback: if NO candidate scraped successfully, still build scraped_items
                # directly from the raw candidates so the AI/urgency layer and Telegram
                # dispatch always run (previously: scrape-failure on all candidates -> zero posts).
                if not scraped_items and candidates:
                    logger.warning("All content extractions failed — building scraped_items from raw snippets.")
                    for idx, cand in enumerate(candidates):
                        raw_title = cand.get('title', '').rsplit(' - ', 1)[0].strip()
                        snippet = cand.get('description', raw_title)
                        scraped_items.append({
                            'index': idx,
                            'cand': cand,
                            'headline': raw_title,
                            'source': cand.get('publisher', {}).get('title', 'Unknown'),
                            'url': cand.get('url', ''),
                            'clean_url': self._clean_url(cand.get('url', '')),
                            'snippet': snippet,
                            'text': snippet,
                            'photo': self._pick_image(cand.get('image'), fallback_text=raw_title),
                        })

            # 2. Batch AI Analysis in ONE Request
            ai_batch_results = {}
            if CONFIG.get('AI_API_KEY'):
                ai_batch_results = self.batch_analyze_with_ai(scraped_items)
                if not ai_batch_results or not any(ai_batch_results.values()):
                    logger.warning(
                        "AI batch analysis returned no results — falling back to raw snippets."
                    )
            else:
                logger.warning(
                    "AI_API_KEY is not set in this run — skipping AI enrichment.\n"
                    "Falling back to raw snippets (urgency defaulted, no AI summary).\n"
                    "Fix: set AI_API_KEY/AI_BASE_URL/AI_MODEL secrets at "
                    "https://github.com/Nick-Machiavelli/Gustavo/settings/secrets/actions"
                )
            logger.info(
                f"AI enriched {len(ai_batch_results)}/{len(scraped_items)} candidate items."
            )

            # Heuristic urgency hint in case AI is unavailable — keeps items flowing.
            for item in scraped_items:
                if item['index'] in ai_batch_results:
                    continue
                # Fallback: build a minimal pseudo-AI result from the raw candidate data
                ai_batch_results[item['index']] = {
                    'title_fa': item['headline'],
                    'title_en': item['headline'],
                    'summary': [item['snippet'] or item['headline']],
                    'impact': '...',
                    'tag': self._guess_tag_from_title(item['headline']),
                    'urgency': self._cheap_urgency_hint(item['headline'], item['source']),
                    'sentiment': 0,
                }

            for item in scraped_items:
                ai = ai_batch_results.get(item['index'])
                if not ai:
                    # Last-resort fallback: if even our pseudo-fill missed something
                    ai = {
                        'title_fa': item['headline'],
                        'title_en': item['headline'],
                        'summary': [item['snippet'] or item['headline']],
                        'impact': '...',
                        'tag': self._guess_tag_from_title(item['headline']),
                        'urgency': self._cheap_urgency_hint(item['headline'], item['source']),
                        'sentiment': 0,
                    }
                try:
                    urgency_val = int(ai.get('urgency', 3))
                except (TypeError, ValueError):
                    urgency_val = 3
                try:
                    ts = parser.parse(item['cand'].get('published date')).timestamp()
                except Exception:
                    ts = time.time()

                photo_url = self._pick_image(item['photo'], item['cand'].get('image'), fallback_text=item['headline'])
                news_id = self._generate_news_id(item['clean_url'])

                res = self._proofread_item({
                    "id": news_id,
                    "title_fa": ai.get('title_fa', item['headline']),
                    "title_en": item['headline'],
                    "summary": ai.get('summary', [item['snippet']]),
                    "impact": ai.get('impact', '...'),
                    "tag": ai.get('tag', 'General'),
                    "urgency": urgency_val,
                    "sentiment": ai.get('sentiment', 0),
                    "source": item['source'],
                    "url": item['url'],
                    "clean_url": item['clean_url'],
                    "image": photo_url,
                    "timestamp": ts
                })
                new_processed_items.append(res)
                self.seen_urls.add(res['clean_url'])
                self.recent_title_hashes.add(self._title_hash(res.get('title_en', '')))
                logger.info(f"   Built processed item index={item['index']} -> {res.get('title_en','')[:50]}")
            logger.info(f"Total processed items queued for dispatch: {len(new_processed_items)}")

        if new_processed_items:
            self.existing_news = self.save_news(new_processed_items)

            # Post every new item immediately - no urgency filtering.
            logger.info(f"Sending {len(new_processed_items)} new items to Telegram immediately.")
            self.send_digest_to_telegram(new_processed_items)
        else:
            logger.info(">>> No valid new items found.")

        # ───────────────────────── SCHEDULED DISPATCHES ─────────────────────────
        tehran_now = self._get_tehran_time()
        curr_hour = tehran_now.hour
        today_date_str = tehran_now.strftime("%Y-%m-%d")

        # NIGHTLY SPECIAL REPORT DISPATCH (Target Window: 20:00 -> 02:00 Tehran Time)
        report_date_str = today_date_str
        if 0 <= curr_hour < 2:
            yesterday = tehran_now - timedelta(days=1)
            report_date_str = yesterday.strftime("%Y-%m-%d")

        if curr_hour >= 20 or curr_hour < 2:
            special_report_slot = f"special_report_night_{report_date_str}"
            if not self._is_schedule_already_sent(special_report_slot):
                logger.info(f"Generating nightly Special Topic Report for slot: {special_report_slot}")
                special_report = self.generate_special_topic_report()
                if special_report:
                    sent_ok = self.send_special_report_to_telegram(special_report)
                    if sent_ok:
                        self._mark_schedule_as_sent(special_report_slot)
            else:
                logger.info(f"Nightly Special Report slot [{special_report_slot}] was already sent today.")

        # Always generate and save daily_summary JSON for local dashboard use only (not dispatched to TG)
        daily_summary = self.generate_daily_summary()
        if daily_summary:
            self.save_daily_summary(daily_summary)

        # 23:00 Bulletin Window
        bulletin_date_str = today_date_str
        if 0 <= curr_hour < 2:
            yesterday = tehran_now - timedelta(days=1)
            bulletin_date_str = yesterday.strftime("%Y-%m-%d")

        if curr_hour >= 22 or curr_hour < 2:
            bulletin_slot = f"bulletin_23_{bulletin_date_str}"
            if not self._is_schedule_already_sent(bulletin_slot):
                scheduled_bulletin = self.generate_scheduled_bulletin()
                if scheduled_bulletin:
                    logger.info(f"Triggering 23:00 Bulletin for slot: {bulletin_slot}")
                    sent_ok = self.send_bulletin_to_telegram(scheduled_bulletin)
                    if sent_ok:
                        scheduled_bulletin['telegram_sent'] = True
                        scheduled_bulletin['sent_slot'] = bulletin_slot
                        self._atomic_json_dump('bulletins.json', scheduled_bulletin)
                        self._mark_schedule_as_sent(bulletin_slot)
            else:
                logger.info(f"23:00 Bulletin slot [{bulletin_slot}] was already confirmed sent.")

        logger.info(
            f">>> Done. New={len(new_processed_items)} | "
            f"Failed hosts this run={len(self.failed_hosts)}"
        )


if __name__ == "__main__":
    Gustavo().run()
