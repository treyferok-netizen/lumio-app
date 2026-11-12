"""Интерактивное сообщение-виджет Lumio (Bot API 10.4, sendWidget).

Главная → «Рассчитать цену» → анкета → заявка в CRM; «Эскиз бесплатно» → та же анкета.
Цены, длительности, поводы и праздник берутся из CONFIG в ../index.html (тот же источник, что у Mini App),
чтобы не держать цены в третьем месте. Если разобрать CONFIG не удалось — берутся значения ниже.

Ограничения виджетов: HTML ≤ 32 КБ и ≤ 4 КБ в сжатом виде, без сети, без видео;
запрос боту — 1–64 байта, ответ — до 1024 байт и не позже 15 секунд; в продакшене — только личный чат.
"""
import datetime
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "..", "index.html")

DEFAULT = {
    "lengths": [["30 сек", 3900, 4], ["1 мин", 6900, 5], ["2 мин", 11900, 7], ["3 мин", 15900, 9]],
    "heroes": [["1–2 героя", 0], ["3–4 героя", 1500], ["5 и больше", 3000]],
    "extras": [["Диктор", 1500, 0], ["Песня с именами", 3000, 0], ["Питомцы", 900, 0], ["Срочно за 3 дня", 0, 30]],
    "occasions": [["birthday", "День рождения"], ["mom", "День матери"], ["newyear", "Новый год"],
                  ["wedding", "Свадьба"], ["love", "История любви"], ["family", "Семейная история"],
                  ["memory", "В память о близком"], ["business", "Для бизнеса"], ["other", "Другой повод"]],
}
WHO = ["Ребёнок", "Мама", "Папа", "Пара", "Семья", "Бабушка, дедушка", "Питомец"]
SHORT = {"30 секунд": "30 сек", "1 минута": "1 мин", "2 минуты": "2 мин", "3 минуты": "3 мин",
         "Озвучка диктором": "Диктор", "Песня с вашими именами": "Песня с именами",
         "Добавить питомцев": "Питомцы", "Срочно — за 3 дня": "Срочно за 3 дня"}
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]


def _objs(block):
    """Достаёт из куска JS-массива объекты вида {id:"30", name:"…", price:3900, …}."""
    out = []
    for m in re.finditer(r"\{([^{}]*)\}", block):
        o = {}
        for k, v in re.findall(r'(\w+)\s*:\s*("(?:[^"\\]|\\.)*"|-?\d+)', m.group(1)):
            o[k] = json.loads(v) if v.startswith('"') else int(v)
        out.append(o)
    return out


def config():
    """Цены и праздник из CONFIG Mini App; при любой ошибке — значения по умолчанию."""
    cfg = json.loads(json.dumps(DEFAULT))
    cfg["holiday"] = ""
    try:
        with open(INDEX_HTML, encoding="utf-8") as f:
            src = f.read()
        src = src[src.index("const CONFIG"):]
        src = src[:src.index("\n};")]

        def arr(name):
            m = re.search(name + r"\s*:\s*\[([\s\S]*?)\n\s*\]", src)
            return _objs(m.group(1)) if m else []
        L, H, E, O = arr("lengths"), arr("heroes"), arr("extras"), arr("occasions")
        if L:
            cfg["lengths"] = [[SHORT.get(x["name"], x["name"]), x["price"], x["days"]] for x in L]
        if H:
            cfg["heroes"] = [[x["name"], x.get("add", 0)] for x in H]
        if E:
            cfg["extras"] = [[SHORT.get(x["name"], x["name"]), x.get("add", 0), x.get("pct", 0)] for x in E]
        if O:
            cfg["occasions"] = [[x["id"], x["name"]] for x in O]
        today = datetime.date.today().isoformat()
        for h in arr("holidays"):
            if h.get("until", "") >= today:
                d = datetime.date.fromisoformat(h["until"])
                cfg["holiday"] = f"{h['title']} — заказы до {d.day} {MONTHS[d.month - 1]}"
                break
    except Exception as e:  # CONFIG поменяли так, что не читается, — виджет всё равно работает
        print("widget config:", repr(e))
    return cfg


HTML = r"""<style>
*{box-sizing:border-box;margin:0}
body{font:15px/1.4 system-ui,-apple-system,sans-serif;background:#15122b;color:#f3ecdf;padding:14px}
h1{font-size:20px;color:#f2c46d}h2{font-size:17px;margin-bottom:6px}
.s{color:#a99fc9;font-size:13px;margin:2px 0 10px}
.b{background:#1f1a3d;border:1px solid #3a3266;border-radius:12px;padding:8px 12px;margin-bottom:10px;font-size:13px}
.g{display:grid;grid-template-columns:1fr 1fr;gap:8px}
button{font:inherit;color:inherit;background:#2b2452;border:1px solid #3a3266;border-radius:12px;padding:10px;cursor:pointer}
.t{padding:14px 10px;text-align:left;font-weight:600;font-size:14px}
.c{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 10px}
.c button{padding:6px 10px;font-size:13px;border-radius:16px}
.on{background:#f2c46d;color:#2a1d05;border-color:#f2c46d}
.m{width:100%;background:#f2c46d;color:#2a1d05;font-weight:700;border:0;margin-top:4px}
.m:disabled{opacity:.4}
h4{font-size:12px;color:#a99fc9;font-weight:600}
.k{background:none;border:0;padding:0 0 8px;color:#a99fc9;font-size:13px}
input{width:100%;font:inherit;padding:9px 10px;border-radius:10px;border:1px solid #3a3266;background:#1f1a3d;color:inherit;margin:4px 0 10px}
.p{font-size:22px;font-weight:700;color:#f2c46d}
</style><div id=a></div><script>
var W=window.TelegramWidget,C=__CFG__,WHO=__WHO__,a=document.getElementById('a'),
v={l:1,h:0,e:[],o:-1,w:-1,n:'',m:'o'},S='home';
function rub(n){return n.toLocaleString('ru-RU')+' ₽'}
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return'&#'+c.charCodeAt(0)+';'})}
function tap(){try{W.HapticFeedback.selectionChanged()}catch(e){}}
function chips(k,list,lab){return'<div class=c>'+list.map(function(x,i){var on=k=='e'?v.e.indexOf(i)>=0:v[k]==i;
return'<button class="'+(on?'on':'')+'" onclick="pick(\''+k+'\','+i+')">'+esc(lab(x))+'</button>'}).join('')+'</div>'}
function pick(k,i){tap();if(k=='e'){var j=v.e.indexOf(i);j<0?v.e.push(i):v.e.splice(j,1)}else v[k]=i;draw()}
function price(){var L=C.lengths[v.l],s=L[1]+C.heroes[v.h][1],p=0;
v.e.forEach(function(i){var x=C.extras[i];x[2]?p+=x[2]:s+=x[1]});return Math.round(s*(1+p/100)/100)*100}
function days(){var d=C.lengths[v.l][2];v.e.forEach(function(i){if(C.extras[i][2])d=3});return d}
function go(s){tap();S=s;draw()}
function back(){return'<button class=k onclick="go(\'home\')">← Назад</button>'}
var V={
home:function(){return'<h1>✨ Lumio</h1><p class=s>Мультфильмы по вашей истории: ваши близкие — герои, нарисованные по фото</p>'
+(C.holiday?'<div class=b>🎁 '+esc(C.holiday)+'</div>':'')
+'<div class=g><button class=t onclick="v.m=\'s\';go(\'form\')">🎨 Эскиз героя бесплатно</button>'
+'<button class=t onclick="go(\'calc\')">🧮 Рассчитать цену</button>'
+'<button class=t onclick="W.openTelegramLink(\'https://t.me/LumioMultBot/app\')">🎬 Наши мультфильмы</button>'
+'<button class=t onclick="go(\'ask\')">💬 Задать вопрос</button></div>'},
calc:function(){return back()+'<h4>Длительность</h4>'+chips('l',C.lengths,function(x){return x[0]+' · '+rub(x[1])})
+'<h4>Герои</h4>'+chips('h',C.heroes,function(x){return x[0]+(x[1]?' +'+rub(x[1]):'')})
+'<h4>Дополнительно</h4>'+chips('e',C.extras,function(x){return x[0]+(x[2]?' +'+x[2]+'%':' +'+rub(x[1]))})
+'<p class=p>'+rub(price())+'</p><p class=s>Готово за ~'+days()+' дн. Сценарий, герой, озвучка и музыка включены</p>'
+'<button class=m onclick="v.m=\'o\';go(\'form\')">Оформить заявку →</button>'},
form:function(){var ok=v.w>=0&&v.o>=0;return back()+'<h2>'+(v.m=='s'?'🎨 Бесплатный эскиз героя':'🎬 Заявка · '+rub(price()))+'</h2>'
+'<h4>Кто главный герой</h4>'+chips('w',WHO,String)
+'<h4>Повод</h4>'+chips('o',C.occasions,function(x){return x[1]})
+'<h4>Имя героя</h4><input maxlength=16 placeholder="Например, Алина" value="'+esc(v.n)+'" oninput="v.n=this.value">'
+'<button class=m '+(ok?'':'disabled ')+'onclick="send(this)">Отправить</button>'
+(ok?'':'<p class=s>Выберите героя и повод</p>')},
ask:function(){return back()+'<h2>💬 Задайте вопрос</h2><p class=s>Просто напишите его в чат под этим сообщением — отвечу сразу. Нужен живой человек — напишите «менеджер».</p>'},
done:function(){return'<h2>✅ Заявка принята</h2><p class=s>Пришлите в этот чат 2–3 фото героя: лицо крупно, при хорошем свете. Менеджер напишет вам в течение часа.</p>'
+'<button class=m onclick="go(\'home\')">На главную</button>'},
err:function(){return back()+'<h2>Не получилось отправить</h2><p class=s>Попробуйте ещё раз или напишите нам в чат — всё передадим.</p>'}};
function send(b){b.disabled=true;b.textContent='Отправляем…';
var d=[v.m,v.m=='o'?v.l:'',v.m=='o'?v.h:'',v.m=='o'?v.e.join(''):'',v.o,v.w,v.n.replace(/\|/g,' ').trim()].join('|'),T=new TextEncoder();
while(T.encode(d).length>64)d=d.slice(0,-1);
W.request(d).then(function(){try{W.HapticFeedback.notificationOccurred('success')}catch(e){}go('done')},function(){go('err')})}
function draw(){a.innerHTML=V[S]()}
if(W&&W.chatData&&W.chatData.indexOf('done')==0)S='done';
draw();if(W){W.ready&&W.ready();W.onEvent&&W.onEvent('chatDataChanged',function(){if((W.chatData||'').indexOf('done')==0)go('done')})}
</script>"""


def build_html(cfg=None):
    cfg = cfg or config()
    return (HTML.replace("__CFG__", json.dumps(cfg, ensure_ascii=False, separators=(",", ":")))
                .replace("__WHO__", json.dumps(WHO, ensure_ascii=False, separators=(",", ":"))))


def parse_query(data, cfg=None):
    """Разбирает запрос виджета «o|1|0|03|2|1|Алина» в заявку для CRM. None — запрос не наш."""
    cfg = cfg or config()
    parts = (data or "").split("|")
    if len(parts) != 7 or parts[0] not in ("o", "s"):
        return None
    mode, l, h, e, o, w, name = parts

    def at(lst, i, default=None):
        try:
            return lst[int(i)]
        except (ValueError, IndexError, TypeError):
            return default

    occ = at(cfg["occasions"], o, ["other", "Другой повод"])[1]
    who = at(WHO, w, "—")
    story = f"Главный герой: {who}" + (f", имя {name.strip()}" if name.strip() else "")
    order = {"occasion": occ, "heroes": who + (f" ({name.strip()})" if name.strip() else ""), "story": story}
    if mode == "s":
        order["calc"] = {"text": "Бесплатный эскиз героя (длительность не выбрана)"}
        order["source"] = "Виджет: эскиз"
        return order
    L = at(cfg["lengths"], l, cfg["lengths"][0])
    H = at(cfg["heroes"], h, cfg["heroes"][0])
    ex = [x for x in (at(cfg["extras"], c) for c in e if c.isdigit()) if x]
    s, pct, days = L[1] + H[1], 0, L[2]
    for x in ex:
        if x[2]:
            pct += x[2]
            days = 3
        else:
            s += x[1]
    total = round(s * (1 + pct / 100) / 100) * 100
    rub = f"{total:,}".replace(",", " ")
    text = f"{L[0]}, {H[0]}" + "".join(", " + x[0] for x in ex) + f" — {rub} ₽, ~{days} дн."
    order["calc"] = {"text": text}
    order["source"] = "Виджет: заказ"
    return order
