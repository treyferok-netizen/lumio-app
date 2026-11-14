"""Магазины-примерочные на одном сервере. Код общий (svs_bot.py, svs_app.py, svs_app.html), настройки — здесь.

SVS — «Сила в Стиле» (мужской, /svs/, @sila_v_stile_bot), исходный экземпляр модулей.
WL  — женский магазин (/wl/), второй экземпляр тех же модулей с SHOP="WL".

Любой текст можно переопределить на сервере: lumio-set WL_NAME "…", WL_MANAGER, WL_DELIVERY, WL_SUB.
Переменные магазина: <SHOP>_BOT_TOKEN, <SHOP>_MANAGER, <SHOP>_CHANNEL_LINK, <SHOP>_DAILY_FREE,
<SHOP>_SUBSCRIBERS_ONLY, <SHOP>_AUTO_APPROVE, <SHOP>_JOIN_WELCOME, <SHOP>_ADMIN_IDS, <SHOP>_CHANNEL_ID.
"""
import importlib
import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))

THEMES = {
    "dark": {"bg": "#0c0c0d", "card": "#161618", "line": "#26262a", "text": "#f2efe9", "muted": "#8d8a85",
             "accent": "#c9a46a", "accent_ink": "#1a1408", "ph": "#1d1d20", "overlay": "rgba(12,12,13,.72)",
             "skel1": "#1b1b1e", "skel2": "#242428", "toast": "#222", "good": "#3d5a3a", "warn": "#6b5328"},
    "light": {"bg": "#f6f1ec", "card": "#ffffff", "line": "#e6dcd3", "text": "#2a211c", "muted": "#8f8178",
              "accent": "#b07a62", "accent_ink": "#ffffff", "ph": "#efe7e0", "overlay": "rgba(255,255,255,.82)",
              "skel1": "#efe7e0", "skel2": "#f7f2ee", "toast": "#2a211c", "good": "#9cc29a", "warn": "#d9b37a"},
}

SHOPS = {
    "SVS": {"name": "Сила в Стиле", "sub": "For man · примерочная", "path": "/svs", "store": "svs",
            "manager": "romanov_roman_svs", "bot": "sila_v_stile_bot", "female": False, "emoji": "🖤",
            "theme": "dark", "delivery": "🚚 Доставка с примеркой по Москве, отправка по России."},
    "WL": {"name": "Women's Line", "sub": "примерочная", "path": "/wl", "store": "wl",
           "manager": "romanov_roman_svs", "bot": "Womens_line_bot", "female": True, "emoji": "🤍",
           "theme": "light", "delivery": "🚚 Доставка по России."},
}


def cfg(shop):
    c = dict(SHOPS[shop])
    for k in ("name", "sub", "manager", "delivery", "theme"):
        v = os.environ.get(f"{shop}_{k.upper()}", "").strip()
        if v:
            c[k] = v
    c["shop"] = shop
    return c


def theme_css(shop):
    t = THEMES.get(cfg(shop)["theme"], THEMES["dark"])
    return ("--bg:{bg}; --card:{card}; --line:{line}; --text:{text}; --muted:{muted}; --accent:{accent}; "
            "--accent-ink:{accent_ink}; --ph:{ph}; --overlay:{overlay}; --skel1:{skel1}; --skel2:{skel2}; "
            "--toast:{toast}; --good:{good}; --warn:{warn};").format(**t), t["bg"]


# ---------------------------------------------------------------- загрузка экземпляров
def _load(name, file, inject):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    mod = importlib.util.module_from_spec(spec)
    mod.__dict__.update(inject)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub_stylist():
    """Женский магазин — без ИИ-стилиста (только проверка «две вещи одного типа»)."""
    import shop_rules
    return types.SimpleNamespace(ready=lambda: False, conflict=shop_rules.conflict, catalog_changed=lambda *a: None,
                                 start=lambda: None, allowed=lambda uid: False)


BOTS, APPS = {}, {}


def load_all():
    """Возвращает {путь: модуль витрины}; ботов запускает каждый runner через start_all()."""
    if APPS:
        return APPS
    import svs_app
    import svs_bot
    BOTS["SVS"], APPS[SHOPS["SVS"]["path"]] = svs_bot, svs_app
    for shop in SHOPS:
        if shop == "SVS":
            continue
        stub = _stub_stylist()
        bot = _load(f"{shop.lower()}_bot", "svs_bot.py", {"SHOP": shop, "_STYLIST": stub})
        try:        # ИИ-стилист магазина (тот же svs_stylist.py со своим каталогом); не загрузился — заглушка
            stylist = _load(f"{shop.lower()}_stylist", "svs_stylist.py", {"SHOP": shop, "_BOT": bot})
        except Exception as e:
            print(shop, "stylist load error", repr(e)[:200], flush=True)
            stylist = stub
        bot._STYLIST = stylist
        app = _load(f"{shop.lower()}_app", "svs_app.py", {"SHOP": shop, "_BOT": bot, "_STYLIST": stylist})
        BOTS[shop], APPS[SHOPS[shop]["path"]] = bot, app
    return APPS


def start_all():
    load_all()
    for bot in BOTS.values():
        bot.start()


def app_for(path):
    for prefix, app in load_all().items():
        if path == prefix or path.startswith(prefix + "/"):
            return app
    return None


def bot(shop="SVS"):
    load_all()
    return BOTS[shop.upper()]
